"""Offline bootstrap staging: fake processes/CDP only; no browser or receipt writes."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


harness = load("bootstrap_diagnostic_harness", "qualify_shared_runtime.py")
core = load("bootstrap_diagnostic_core", "browser_session.py")
OLD, NEW, EXTRA = "OLD000001", "NEW000001", "EXTRA0001"
URL = "http://127.0.0.1:9182/"


def tabs(*items):
    return {"success": True, "data": {"tabs": [
        {"targetId": target, "url": url, "active": active}
        for target, url, active in items]}}


class BootstrapDiagnosticTests(unittest.TestCase):
    def patch(self, obj, name, *args, **kwargs):
        patcher = mock.patch.object(obj, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.home.chmod(0o700)
        self.executable = self.home / "agent-browser"
        self.executable.touch()  # Never executed.
        self.config = self.home / "empty.json"
        self.config.write_text("{}", encoding="utf-8")
        self.env = {"HOME": str(self.home),
                    "AGENT_BROWSER_SOCKET_DIR": str(self.home / ".ab" / "legacy")}
        self.browser = {"port": 9123}
        self.args = [str(self.executable), "--config", str(self.config),
                     "--session", "fixture", "--cdp", "9123", "--pin-tab", "--json",
                     "open", URL]
        self.patch(core, "checked_process", side_effect=AssertionError("no fallback processes"))
        self.process = self.patch(harness.subprocess, "run",
                                  side_effect=AssertionError("no actual subprocess"))
        self.patch(harness.subprocess, "Popen", side_effect=AssertionError("no services"))
        self.patch(harness, "FixtureHTTPServer", side_effect=AssertionError("no HTTP server"))
        self.patch(core.urllib.request, "urlopen", side_effect=AssertionError("no network"))
        self.targets = self.patch(core, "cdp_target_ids", side_effect=[{OLD}, {OLD, NEW}])
        self.good = tabs((OLD, "about:blank", False), (NEW, "about:blank", True))

    def wrapper(self, staged=True):
        return harness.fixture_cli_diagnostics(
            core, self.home, self.browser, "fixture", str(self.executable),
            staged_bootstrap=staged, fixture_url=URL)

    def response(self, payload, code=0):
        return subprocess.CompletedProcess([], code, json.dumps(payload), "private synthetic stderr")

    def call(self, checked, args=None, **kwargs):
        return checked(self.args if args is None else args, cwd=self.home,
                       env=self.env, timeout=45, accepted=(0, 1), **kwargs)

    def records(self):
        return [json.loads(path.read_text()) for path in sorted((self.home / "diagnostics").glob("*.json"))]

    def assert_code(self, code, checked):
        with self.assertRaises(core.BrowserSessionError) as caught:
            self.call(checked)
        self.assertEqual(code, caught.exception.code)

    def test_preflight_then_exact_open_once_with_private_elapsed(self):
        opened = self.response({"success": True})
        self.process.side_effect = [self.response(self.good), opened, self.response(self.good)]
        checked = self.wrapper()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertIs(opened, self.call(checked))
            self.call(checked, self.args[:9] + ["tab", "list"])
        self.assertEqual("", stdout.getvalue())
        calls = self.process.call_args_list
        self.assertEqual([self.args[:9] + ["tab", "list"], self.args,
                          self.args[:9] + ["tab", "list"]], [call.args[0] for call in calls])
        self.assertEqual([75, 45, 45], [call.kwargs["timeout"] for call in calls])
        self.assertTrue(checked.bootstrapPreflightDone)
        self.assertEqual(2, self.targets.call_count)
        records = self.records()
        self.assertEqual(["bootstrap", "open", "fixture-cli"], [item["stage"] for item in records])
        for record in records:
            self.assertGreaterEqual(record["elapsedSeconds"], 0)
            self.assertEqual("private synthetic stderr", record["stderr"])
            self.assertTrue(record["fixtureOnly"])
        for path in (self.home / "diagnostics").iterdir():
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE((self.home / "diagnostics").stat().st_mode))

    def test_unknown_preflight_blocks_open_and_preserves_native_code(self):
        self.process.return_value = self.response({"success": False, "code": "outcome_unknown"}, 35)
        self.process.side_effect = None
        checked = self.wrapper()
        self.assert_code("command_outcome_unknown", checked)
        self.assertFalse(checked.bootstrapPreflightDone)
        self.process.assert_called_once()
        self.assertEqual(["tab", "list"], self.process.call_args.args[0][-2:])
        self.assertEqual(35, self.records()[0]["returncode"])

    def test_preflight_ownership_and_blank_are_strict(self):
        cases = [
            (self.good, {OLD}),  # no new target
            (tabs((OLD, "about:blank", True), (NEW, "about:blank", False)), {OLD, NEW}),
            (self.good, {NEW}),  # old target lost
            (self.good, {OLD, NEW, EXTRA}),  # multiple new targets
            (tabs((OLD, "about:blank", True), (NEW, "about:blank", True)), {OLD, NEW}),
            (tabs((OLD, "about:blank", False), (NEW, "about:blank#not-exact", True)), {OLD, NEW}),
            (tabs((OLD, "about:blank", False), (NEW, URL, True)), {OLD, NEW}),
        ]
        for index, (payload, after) in enumerate(cases):
            with self.subTest(index=index):
                # A fresh private home keeps exclusive diagnostic filenames unique.
                with tempfile.TemporaryDirectory(dir=self.home) as temporary:
                    home = Path(temporary).resolve()
                    config = home / "empty.json"
                    config.write_text("{}")
                    args = self.args.copy()
                    args[2] = str(config)
                    env = {"HOME": str(home), "AGENT_BROWSER_SOCKET_DIR": str(home / ".ab" / "legacy")}
                    self.targets.side_effect = [{OLD}, after]
                    self.process.reset_mock()
                    self.process.side_effect = [self.response(payload)]
                    checked = harness.fixture_cli_diagnostics(
                        core, home, self.browser, "fixture", str(self.executable), staged_bootstrap=True)
                    with self.assertRaises(core.BrowserSessionError) as caught:
                        checked(args, cwd=home, env=env, timeout=45, accepted=(0, 1))
                    self.assertEqual("ownership_unverified", caught.exception.code)
                    self.assertFalse(checked.bootstrapPreflightDone)
                    self.process.assert_called_once()

    def test_unknown_schema_and_nonzero_preflight_block_open(self):
        for payload, code, expected in [
            ({"success": True, "data": {"unknown": []}}, 0, "invalid_json"),
            (self.good, 1, "command_failed"),
        ]:
            with self.subTest(code=code):
                # Each wrapper has a fresh private record directory.
                with tempfile.TemporaryDirectory(dir=self.home) as temporary:
                    home = Path(temporary).resolve()
                    config = home / "empty.json"
                    config.write_text("{}")
                    args = self.args.copy()
                    args[2] = str(config)
                    self.targets.side_effect = [{OLD}]
                    self.process.reset_mock()
                    self.process.side_effect = [self.response(payload, code)]
                    checked = harness.fixture_cli_diagnostics(
                        core, home, self.browser, "fixture", str(self.executable), staged_bootstrap=True)
                    with self.assertRaises(core.BrowserSessionError) as caught:
                        checked(args, cwd=home, env={"HOME": str(home),
                                "AGENT_BROWSER_SOCKET_DIR": str(home / ".ab")}, timeout=45)
                    self.assertEqual(expected, caught.exception.code)
                    self.process.assert_called_once()

    def test_preflight_timeout_is_bounded_and_blocks_open(self):
        self.process.side_effect = subprocess.TimeoutExpired(self.args, 75, output=b"private timeout")
        checked = self.wrapper()
        self.assert_code("command_timeout", checked)
        self.assertFalse(checked.bootstrapPreflightDone)
        self.process.assert_called_once()
        record = self.records()[0]
        self.assertTrue(record["timeout"])
        self.assertIn("elapsedSeconds", record)

    def test_open_unknown_is_not_retried(self):
        self.process.side_effect = [self.response(self.good),
                                    self.response({"success": False, "code": "outcome_unknown"}, 35)]
        checked = self.wrapper()
        self.assert_code("command_outcome_unknown", checked)
        self.assertTrue(checked.bootstrapPreflightDone)
        self.assertEqual(2, self.process.call_count)
        self.assertEqual("open", self.records()[1]["stage"])

    def test_normal_mode_first_open_unchanged(self):
        response = self.response({"success": True})
        self.process.side_effect = [response]
        checked = self.wrapper(staged=False)
        self.assertIs(response, self.call(checked))
        self.assertFalse(checked.bootstrapPreflightDone)
        self.process.assert_called_once()
        self.assertEqual(self.args, self.process.call_args.args[0])
        self.assertEqual(45, self.process.call_args.kwargs["timeout"])
        self.targets.assert_not_called()

    def test_scope_validation_precedes_any_process_or_cdp(self):
        invalid = []
        for index, value in [(4, "other-session"), (6, "9999"), (7, "--other"),
                             (10, "http://127.0.0.1:9182/not-the-fixture")]:
            args = self.args.copy()
            args[index] = value
            invalid.append((args, self.home, self.env))
        invalid.extend([(self.args, self.home, {**self.env, "HOME": "/not-fixture"}),
                        (self.args, self.home.parent, self.env),
                        (self.args, self.home, {**self.env, "AGENT_BROWSER_SOCKET_DIR": "/outside"})])
        checked = self.wrapper()
        for args, cwd, env in invalid:
            with self.subTest(args=args, cwd=cwd, env=env):
                with self.assertRaises(harness.QualificationFailure):
                    checked(args, cwd=cwd, env=env)
        self.config.write_text('{"credentialManager": "forbidden"}')
        with self.assertRaises(harness.QualificationFailure):
            self.call(checked)
        self.process.assert_not_called()
        self.targets.assert_not_called()

    def main_output(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = harness.main(argv)
        return status, json.loads(output.getvalue())

    def main_mocks(self):
        migration = types.SimpleNamespace(record_qualification=mock.Mock())
        self.patch(harness.sys, "platform", "darwin")
        self.patch(harness.Path, "home", return_value=self.home)
        self.patch(harness.tempfile, "mkdtemp", return_value=str(self.home))
        self.patch(harness.os, "fsencode", return_value=b"short-fixture-path")
        environment = {"HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"}
        self.patch(harness, "clean_environment", return_value=environment)
        exercise = self.patch(harness, "exercise", return_value=(migration, {"passed": True}))
        return migration, exercise

    def test_diagnostic_success_skips_receipt_and_restores_environment(self):
        migration, exercise = self.main_mocks()
        before = dict(os.environ)
        bytecode = sys.dont_write_bytecode
        status, output = self.main_output(["--allow-browser-launch", "--diagnose-bootstrap"])
        self.assertEqual(0, status)
        self.assertTrue(output["success"])
        self.assertTrue(output["diagnosticOnly"])
        self.assertFalse(output["qualificationIssued"])
        self.assertFalse(output["checks"]["receiptWritten"])
        migration.record_qualification.assert_not_called()
        exercise.assert_called_once_with(self.home, mock.ANY, diagnose_bootstrap=True)
        self.assertEqual(before, dict(os.environ))
        self.assertEqual(bytecode, sys.dont_write_bytecode)

    def test_diagnostic_failure_reports_only_fixed_status_and_no_receipt(self):
        migration, exercise = self.main_mocks()
        exercise.side_effect = core.BrowserSessionError("command_outcome_unknown")
        before = dict(os.environ)
        bytecode = sys.dont_write_bytecode
        status, output = self.main_output(["--allow-browser-launch", "--diagnose-bootstrap"])
        self.assertEqual(1, status)
        self.assertTrue(output["diagnosticOnly"])
        self.assertFalse(output["success"])
        self.assertFalse(output["qualificationIssued"])
        self.assertFalse(output["checks"]["receiptWritten"])
        self.assertEqual("command_outcome_unknown", output["failureCode"])
        self.assertNotIn("private synthetic", json.dumps(output))
        migration.record_qualification.assert_not_called()
        self.assertEqual(before, dict(os.environ))
        self.assertEqual(bytecode, sys.dont_write_bytecode)

    def test_normal_main_keeps_receipt_path_and_old_exercise_call(self):
        migration, exercise = self.main_mocks()
        status, output = self.main_output(["--allow-browser-launch"])
        self.assertEqual(0, status)
        self.assertFalse(output["diagnosticOnly"])
        self.assertTrue(output["qualificationIssued"])
        self.assertTrue(output["checks"]["receiptWritten"])
        exercise.assert_called_once_with(self.home, mock.ANY)
        migration.record_qualification.assert_called_once_with({"passed": True}, home=self.home)

    def test_launch_guard_is_required_with_or_without_diagnostic(self):
        migration, exercise = self.main_mocks()
        for argv in ([], ["--diagnose-bootstrap"]):
            with self.subTest(argv=argv):
                status, output = self.main_output(argv)
                self.assertEqual(1, status)
                self.assertFalse(output["success"])
                self.assertFalse(output["qualificationIssued"])
                self.assertFalse(output["checks"]["approvedLaunchFlag"])
        exercise.assert_not_called()
        migration.record_qualification.assert_not_called()


if __name__ == "__main__":
    unittest.main()
