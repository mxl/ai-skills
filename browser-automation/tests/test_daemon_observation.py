"""Offline one-shot native observation: fake sockets/processes, never live IPC."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
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


harness = load("daemon_observation_harness", "qualify_shared_runtime.py")
core = load("daemon_observation_core", "browser_session.py")
DAEMON_IDENTITY = harness.daemon_identity
URL = "http://127.0.0.1:9182/"
OLD, NEW = "OLD000001", "NEW000001"
TABS = {"success": True, "data": {"tabs": [
    {"targetId": OLD, "url": "about:blank", "active": False},
    {"targetId": NEW, "url": "about:blank", "active": True}]}}


class DaemonObservationTests(unittest.TestCase):
    def patch(self, obj, name, *args, **kwargs):
        patcher = mock.patch.object(obj, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.home.chmod(0o700)
        self.root = self.home / ".ab" / "legacy"
        self.root.mkdir(parents=True, mode=0o700)
        self.executable = self.home / "agent-browser"
        self.executable.touch()  # Never executed.
        self.config = self.home / "empty.json"
        self.config.write_text("{}")
        self.browser = {"port": 9123}
        self.env = {"HOME": str(self.home), "AGENT_BROWSER_SOCKET_DIR": str(self.root)}
        self.args = [str(self.executable), "--config", str(self.config), "--session", "fixture",
                     "--cdp", "9123", "--pin-tab", "--json", "open", URL]
        self.patch(core, "checked_process", side_effect=AssertionError("no fallback process"))
        self.process = self.patch(harness.subprocess, "run", return_value=self.response(TABS))
        self.patch(harness.subprocess, "Popen", side_effect=AssertionError("no services"))
        self.patch(harness, "FixtureHTTPServer", side_effect=AssertionError("no HTTP fixture"))
        self.patch(core.urllib.request, "urlopen", side_effect=AssertionError("no network"))
        self.targets = self.patch(core, "cdp_target_ids", side_effect=[{OLD}, {OLD, NEW}])
        self.identity = self.patch(harness, "daemon_identity", return_value=(123, "fixture-start"))
        self.socket_factory = self.patch(harness.socket, "socket")
        self.client = self.socket_factory.return_value.__enter__.return_value
        self.client.recv.side_effect = [b'{"success":true,"data":{"url":"fixture"}}\n']

    def response(self, payload, code=0):
        return subprocess.CompletedProcess([], code, json.dumps(payload), "private-cli")

    def wrapper(self, observe=True, staged=True):
        return harness.fixture_cli_diagnostics(
            core, self.home, self.browser, "fixture", str(self.executable),
            staged_bootstrap=staged, fixture_url=URL, observe_daemon_response=observe)

    def call(self, checked, args=None, **kwargs):
        return checked(self.args if args is None else args, cwd=self.home,
                       env=kwargs.pop("env", self.env), timeout=45, accepted=(0, 1), **kwargs)

    def records(self):
        return [json.loads(path.read_text())
                for path in sorted((self.home / "diagnostics").glob("*.json"))]

    def assert_failure(self, code, checked):
        with self.assertRaises(core.BrowserSessionError) as caught:
            self.call(checked)
        self.assertEqual(code, caught.exception.code)

    def test_exact_one_native_request_after_preflight_private_response(self):
        checked = self.wrapper()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.call(checked)
        self.assertEqual("", output.getvalue())
        self.assertEqual(0, result.returncode)
        self.assertTrue(json.loads(result.stdout)["success"])
        self.process.assert_called_once()
        self.assertEqual(self.args[:9] + ["tab", "list"], self.process.call_args.args[0])
        self.assertEqual(75, self.process.call_args.kwargs["timeout"])
        self.identity.assert_called_once_with(core, self.home, self.root, "fixture")
        self.socket_factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
        self.client.connect.assert_called_once_with(str(self.root / "fixture.sock"))
        self.client.sendall.assert_called_once()
        wire = self.client.sendall.call_args.args[0]
        self.assertTrue(wire.endswith(b"\n"))
        self.assertEqual(1, wire.count(b"\n"))
        request = json.loads(wire)
        self.assertEqual({"id", "action", "url", "pinTab"}, set(request))
        self.assertEqual("navigate", request["action"])
        self.assertEqual(URL, request["url"])
        self.assertIs(True, request["pinTab"])
        self.assertTrue(request["id"].startswith("fixture-observe-"))
        self.assertTrue(checked.bootstrapPreflightDone)
        self.assertTrue(checked.daemonResponseReceived)
        records = self.records()
        self.assertEqual(["bootstrap", "daemon_observation"], [r["stage"] for r in records])
        record = records[-1]
        self.assertEqual(request["id"], record["requestId"])
        self.assertEqual(result.stdout, record["rawDaemonResponse"])
        self.assertEqual(70, record["observationSeconds"])
        self.assertGreaterEqual(record["elapsedSeconds"], 0)
        self.assertEqual(0o700, stat.S_IMODE((self.home / "diagnostics").stat().st_mode))
        for path in (self.home / "diagnostics").iterdir():
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        for call in self.client.settimeout.call_args_list:
            self.assertGreater(call.args[0], 0)
            self.assertLessEqual(call.args[0], 70)
        # A second open is never a replay or a fallback CLI open.
        self.assert_failure("command_outcome_unknown", checked)
        self.client.sendall.assert_called_once()
        self.process.assert_called_once()

    def test_native_failure_returns_exit_one_and_error_stays_private(self):
        raw = b'{"success":false,"data":null,"error":"CDP command timed out: Page.navigate"}\n'
        self.client.recv.side_effect = [raw]
        checked = self.wrapper()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.call(checked)
        self.assertEqual("", output.getvalue())
        self.assertEqual(1, result.returncode)
        self.assertEqual(raw.decode(), result.stdout)
        self.assertEqual(raw.decode(), self.records()[-1]["rawDaemonResponse"])
        self.assertTrue(checked.daemonResponseReceived)

    def test_timeout_and_partial_write_never_reconnect_or_resend(self):
        checked = self.wrapper()
        self.client.recv.side_effect = [b'{"success":', socket.timeout()]
        self.assert_failure("command_outcome_unknown", checked)
        self.assert_failure("command_outcome_unknown", checked)
        self.socket_factory.assert_called_once()
        self.client.sendall.assert_called_once()
        record = self.records()[-1]
        self.assertTrue(record["timeout"])
        self.assertFalse(record["daemonResponseReceived"])
        self.assertEqual('{"success":', record["rawDaemonResponse"])
        self.process.assert_called_once()

    def test_partial_send_error_is_unknown_without_retry(self):
        self.client.sendall.side_effect = OSError("private send failure")
        checked = self.wrapper()
        self.assert_failure("command_outcome_unknown", checked)
        self.assert_failure("command_outcome_unknown", checked)
        self.client.sendall.assert_called_once()
        self.client.recv.assert_not_called()
        self.socket_factory.assert_called_once()

    def test_oversized_response_is_bounded_without_retry(self):
        # recv obeys its requested bound, like a real socket.
        data = bytearray(b"x" * (2 * 1024 * 1024 + 1))
        def recv(size):
            chunk = bytes(data[:size])
            del data[:size]
            return chunk
        self.client.recv.side_effect = recv
        checked = self.wrapper()
        self.assert_failure("invalid_json", checked)
        self.assertLessEqual(len(self.records()[-1]["rawDaemonResponse"]), 2 * 1024 * 1024 + 1)
        self.client.sendall.assert_called_once()
        self.assert_failure("command_outcome_unknown", checked)
        self.socket_factory.assert_called_once()

    def test_response_requires_native_json_boolean_and_single_newline(self):
        for raw in (b'{}\n', b'[]\n', b'{"success":1}\n', b'{"success":true,"type":"cli"}\n',
                    b'{"success":true,"error":{}}\n', b'{"success":true}\nextra',
                    b'{"success":true,"id":"other-request"}\n', b'not-json\n'):
            with self.subTest(raw=raw):
                # Each request gets a fresh wrapper and independent private record names.
                for path in (self.home / "diagnostics").glob("*.json"):
                    path.unlink()
                self.targets.side_effect = [{OLD}, {OLD, NEW}]
                self.client.recv.side_effect = [raw]
                checked = self.wrapper()
                self.assert_failure("invalid_json", checked)
                self.assertFalse(checked.daemonResponseReceived)

    def test_eof_without_newline_is_unknown(self):
        self.client.recv.side_effect = [b'{"success":true}', b'']
        self.assert_failure("command_outcome_unknown", self.wrapper())
        self.client.sendall.assert_called_once()

    def test_unknown_native_code_stops(self):
        self.client.recv.side_effect = [b'{"success":false,"code":"outcome_unknown"}\n']
        checked = self.wrapper()
        self.assert_failure("command_outcome_unknown", checked)
        self.assertTrue(checked.daemonResponseReceived)
        self.assert_failure("command_outcome_unknown", checked)
        self.client.sendall.assert_called_once()

    def test_ownership_guard_before_socket_creation(self):
        self.identity.return_value = None
        self.assert_failure("ownership_unverified", self.wrapper())
        self.socket_factory.assert_not_called()
        self.client.sendall.assert_not_called()

    def prepare_identity_proof(self, lsof_output):
        pidfile = self.root / "fixture.pid"
        pidfile.write_text("123")
        pidfile.chmod(0o600)
        sock = self.root / "fixture.sock"
        original_lstat = Path.lstat
        def lstat(path, *args, **kwargs):
            if path == sock:
                return types.SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600,
                                             st_uid=os.getuid(), st_nlink=1)
            return original_lstat(path, *args, **kwargs)
        self.patch(Path, "lstat", lstat)
        self.patch(harness, "process_start", return_value="verified-start")
        self.patch(core, "checked_process", return_value=subprocess.CompletedProcess(
            [], 0, lsof_output, ""))
        self.identity.side_effect = DAEMON_IDENTITY

    def test_pid_start_and_exact_lsof_socket_proof_before_send(self):
        self.prepare_identity_proof("p123\nn" + str(self.root / "fixture.sock") + "\n")
        self.call(self.wrapper())
        harness.process_start.assert_called_once_with(core, 123)
        self.assertIn("-U", core.checked_process.call_args.args[0])
        self.client.sendall.assert_called_once()

    def test_wrong_lsof_socket_blocks_send(self):
        self.prepare_identity_proof("p123\nn/unrelated/fixture.sock\n")
        with self.assertRaises(harness.QualificationFailure):
            self.call(self.wrapper())
        self.socket_factory.assert_not_called()
        self.client.sendall.assert_not_called()

    def test_deadline_is_total_not_per_chunk(self):
        # Bootstrap start/end, observation start/connect/send/read/next-read/finally.
        self.patch(harness.time, "monotonic", side_effect=[1, 2, 100, 101, 102, 103, 171, 172])
        self.client.recv.side_effect = [b'{"success":']
        self.assert_failure("command_outcome_unknown", self.wrapper())
        self.client.recv.assert_called_once()
        self.assertEqual([69, 68, 67], [c.args[0] for c in self.client.settimeout.call_args_list])
        self.assertEqual(72, self.records()[-1]["elapsedSeconds"])
        self.client.sendall.assert_called_once()

    def test_failed_blank_preflight_prevents_identity_or_socket(self):
        self.targets.side_effect = [{OLD}, {OLD}]
        self.assert_failure("ownership_unverified", self.wrapper())
        self.identity.assert_not_called()
        self.socket_factory.assert_not_called()

    def test_scope_checks_precede_preflight_and_socket(self):
        checked = self.wrapper()
        for index, value in ((4, "other"), (6, "9999"), (7, "--profile"),
                             (10, "http://localhost:9182/"), (10, URL + "other")):
            with self.subTest(index=index, value=value):
                args = self.args.copy()
                args[index] = value
                with self.assertRaises(harness.QualificationFailure):
                    self.call(checked, args)
        for env in ({**self.env, "HOME": "/outside"},
                    {**self.env, "AGENT_BROWSER_SOCKET_DIR": "/outside"}):
            with self.assertRaises(harness.QualificationFailure):
                self.call(checked, env=env)
        self.process.assert_not_called()
        self.targets.assert_not_called()
        self.identity.assert_not_called()
        self.socket_factory.assert_not_called()

    def test_socket_root_symlink_never_connects(self):
        alias = self.home / ".ab" / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(harness.QualificationFailure):
            self.call(self.wrapper(), env={**self.env, "AGENT_BROWSER_SOCKET_DIR": str(alias)})
        self.identity.assert_not_called()
        self.socket_factory.assert_not_called()

    def test_normal_and_staged_sequences_unchanged_without_observation(self):
        normal = self.wrapper(observe=False, staged=False)
        self.call(normal)
        self.assertEqual(self.args, self.process.call_args.args[0])
        self.targets.assert_not_called()
        self.process.reset_mock()
        for path in (self.home / "diagnostics").iterdir():
            path.unlink()
        staged = self.wrapper(observe=False)
        self.call(staged)
        self.assertEqual([self.args[:9] + ["tab", "list"], self.args],
                         [call.args[0] for call in self.process.call_args_list])
        self.identity.assert_not_called()
        self.socket_factory.assert_not_called()

    def test_wrapper_observation_requires_staging_and_exact_fixture(self):
        with self.assertRaises(harness.QualificationFailure):
            self.wrapper(staged=False)
        with self.assertRaises(harness.QualificationFailure):
            harness.fixture_cli_diagnostics(core, self.home, self.browser, "fixture",
                                           str(self.executable), staged_bootstrap=True,
                                           observe_daemon_response=True)
        self.process.assert_not_called()
        self.socket_factory.assert_not_called()

    def main_output(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = harness.main(argv)
        return status, json.loads(output.getvalue())

    def test_main_requires_both_flags_before_temp_import_or_process(self):
        temporary = self.patch(harness.tempfile, "mkdtemp")
        importing = self.patch(harness, "load_migration")
        exercise = self.patch(harness, "exercise")
        for args in (["--observe-daemon-response"],
                     ["--observe-daemon-response", "--allow-browser-launch"],
                     ["--observe-daemon-response", "--diagnose-bootstrap"]):
            status, output = self.main_output(args)
            self.assertEqual(1, status)
            self.assertFalse(output["qualificationIssued"])
            self.assertIsNone(output["scratchPath"])
        temporary.assert_not_called()
        importing.assert_not_called()
        exercise.assert_not_called()
        self.process.assert_not_called()
        self.socket_factory.assert_not_called()

    def main_mocks(self):
        migration = types.SimpleNamespace(record_qualification=mock.Mock())
        self.patch(harness.sys, "platform", "darwin")
        self.patch(harness.Path, "home", return_value=self.home)
        self.patch(harness.tempfile, "mkdtemp", return_value=str(self.home))
        self.patch(harness.os, "fsencode", return_value=b"short-fixture-path")
        self.patch(harness, "clean_environment", return_value={"HOME": str(self.home)})
        exercise = self.patch(harness, "exercise", return_value=(migration, {"passed": True}))
        return migration, exercise

    def test_main_success_is_always_diagnostic_no_receipt(self):
        migration, exercise = self.main_mocks()
        before = dict(os.environ)
        status, output = self.main_output(["--allow-browser-launch", "--diagnose-bootstrap",
                                          "--observe-daemon-response"])
        self.assertEqual(0, status)
        self.assertTrue(output["diagnosticOnly"])
        self.assertFalse(output["qualificationIssued"])
        self.assertFalse(output["checks"]["receiptWritten"])
        self.assertNotIn("observationSeconds", json.dumps(output))
        exercise.assert_called_once_with(self.home, mock.ANY, diagnose_bootstrap=True,
                                         observe_daemon_response=True)
        migration.record_qualification.assert_not_called()
        self.assertEqual(before, dict(os.environ))

    def test_main_failure_never_exposes_daemon_error_or_writes_receipt(self):
        migration, exercise = self.main_mocks()
        exercise.side_effect = core.BrowserSessionError("invalid_json")
        status, output = self.main_output(["--allow-browser-launch", "--diagnose-bootstrap",
                                          "--observe-daemon-response"])
        self.assertEqual(1, status)
        self.assertEqual("invalid_json", output["failureCode"])
        self.assertNotIn("CDP command", json.dumps(output))
        self.assertFalse(output["qualificationIssued"])
        migration.record_qualification.assert_not_called()


class FixtureMockKeychainTests(unittest.TestCase):
    """Only the fixture start temporarily wraps Popen; no real process is spawned."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.root = self.home / "project"
        self.root.mkdir(mode=0o700)
        self.profile = self.root / ".chrome-user-data"
        self.original = mock.Mock(return_value=object())
        self.core = types.SimpleNamespace(subprocess=types.SimpleNamespace(Popen=self.original))
        self.coordinator = types.SimpleNamespace(
            project=types.SimpleNamespace(root=self.root, profile=self.profile), start=mock.Mock())
        self.argv = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                     "--user-data-dir=" + str(self.profile), "--remote-debugging-port=0",
                     "--enable-features=ForceMacVSyncTimerForDebugging", "--headless=new", "about:blank"]
        self.env = {"HOME": str(self.home), "LANG": "C"}
        self.kwargs = {"env": self.env, "stdin": subprocess.DEVNULL,
                       "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                       "start_new_session": True, "close_fds": True}

    def launch(self, argv=None, kwargs=None, **start_options):
        self.core.subprocess.Popen(self.argv if argv is None else argv,
                                   **(self.kwargs if kwargs is None else kwargs))
        return {"status": "started"}

    def test_only_exact_fixture_launch_gets_mock_crypto_and_flags_preserved(self):
        self.coordinator.start.side_effect = self.launch
        before = self.argv.copy()
        self.assertTrue(harness.start_fixture_browser(self.core, self.coordinator, self.home))
        self.coordinator.start.assert_called_once_with(allow_launch=True, headed=False, chrome=None)
        self.original.assert_called_once_with(
            before[:-1] + ["--password-store=basic", "--use-mock-keychain", "about:blank"],
            **self.kwargs)
        self.assertEqual(before, self.argv)
        self.assertIs(self.original, self.core.subprocess.Popen)
        self.assertEqual(str(self.home), self.env["HOME"])

    def test_unrelated_ps_and_git_calls_delegate_unchanged(self):
        ps = ["/bin/ps", "-axo", "pid=,command="]
        git = ["/usr/bin/git", "status"]
        def start(**kwargs):
            self.core.subprocess.Popen(ps, stdout=subprocess.PIPE)
            self.core.subprocess.Popen(git, cwd=self.root, env={"HOME": "/irrelevant-for-git"})
            return self.launch()
        self.coordinator.start.side_effect = start
        self.assertTrue(harness.start_fixture_browser(self.core, self.coordinator, self.home))
        self.assertEqual(mock.call(ps, stdout=subprocess.PIPE), self.original.call_args_list[0])
        self.assertEqual(mock.call(git, cwd=self.root, env={"HOME": "/irrelevant-for-git"}),
                         self.original.call_args_list[1])
        self.assertIs(self.original, self.core.subprocess.Popen)

    def test_foreign_profile_or_environment_rejected_before_popen_and_restored(self):
        cases = []
        foreign = self.argv.copy()
        foreign[1] = "--user-data-dir=/not-fixture/real-profile"
        cases.append((foreign, self.kwargs))
        duplicate = self.argv[:-1] + ["--user-data-dir=/outside", "about:blank"]
        cases.append((duplicate, self.kwargs))
        split = self.argv.copy()
        split[1:2] = ["--user-data-dir", str(self.profile)]
        cases.append((split, self.kwargs))
        cases.append(("Chromium --user-data-dir=/outside", {**self.kwargs, "shell": True}))
        cases.extend([(self.argv, {**self.kwargs, "env": {"HOME": "/real-home"}}),
                      (self.argv, {key: value for key, value in self.kwargs.items() if key != "env"}),
                      (self.argv, {**self.kwargs, "shell": True})])
        for argv, kwargs in cases:
            with self.subTest(argv=argv, kwargs=kwargs):
                self.coordinator.start.side_effect = lambda **unused: self.launch(argv, kwargs)
                with self.assertRaises(harness.QualificationFailure):
                    harness.start_fixture_browser(self.core, self.coordinator, self.home)
                self.original.assert_not_called()
                self.assertIs(self.original, self.core.subprocess.Popen)

    def test_project_and_profile_root_guards_precede_start(self):
        for root, profile in ((self.home.parent, self.profile),
                              (self.root, self.home.parent / "real-profile"),
                              (self.root, self.home),
                              (self.root, Path("relative-profile"))):
            with self.subTest(root=root, profile=profile):
                self.coordinator.project = types.SimpleNamespace(root=root, profile=profile)
                with self.assertRaises(harness.QualificationFailure):
                    harness.start_fixture_browser(self.core, self.coordinator, self.home)
                self.coordinator.start.assert_not_called()
                self.original.assert_not_called()
                self.assertIs(self.original, self.core.subprocess.Popen)

    def test_symlink_profile_cannot_escape_fixture_home(self):
        self.profile.symlink_to(self.home.parent, target_is_directory=True)
        with self.assertRaises(harness.QualificationFailure):
            harness.start_fixture_browser(self.core, self.coordinator, self.home)
        self.coordinator.start.assert_not_called()
        self.original.assert_not_called()

    def test_popen_and_start_failures_restore_wrapper(self):
        self.coordinator.start.side_effect = self.launch
        self.original.side_effect = OSError("synthetic Popen failure")
        with self.assertRaises(OSError):
            harness.start_fixture_browser(self.core, self.coordinator, self.home)
        self.assertIs(self.original, self.core.subprocess.Popen)
        self.original.assert_called_once()
        self.original.reset_mock(side_effect=True)
        self.coordinator.start.side_effect = RuntimeError("synthetic start failure")
        with self.assertRaises(RuntimeError):
            harness.start_fixture_browser(self.core, self.coordinator, self.home)
        self.assertIs(self.original, self.core.subprocess.Popen)
        self.original.assert_not_called()

    def test_start_without_matching_launch_cannot_claim_mock_keychain(self):
        for result in ({"status": "started"}, {"status": "already_running"}):
            with self.subTest(result=result):
                self.coordinator.start.return_value = result
                with self.assertRaises(harness.QualificationFailure):
                    harness.start_fixture_browser(self.core, self.coordinator, self.home)
                self.original.assert_not_called()
                self.assertIs(self.original, self.core.subprocess.Popen)

    def test_conflicting_crypto_flags_rejected_and_existing_safe_flags_not_duplicated(self):
        unsafe = self.argv[:-1] + ["--password-store=keychain", "about:blank"]
        self.coordinator.start.side_effect = lambda **unused: self.launch(unsafe)
        with self.assertRaises(harness.QualificationFailure):
            harness.start_fixture_browser(self.core, self.coordinator, self.home)
        self.original.assert_not_called()
        safe = self.argv[:-1] + ["--password-store=basic", "--use-mock-keychain", "about:blank"]
        self.coordinator.start.side_effect = lambda **unused: self.launch(safe)
        self.assertTrue(harness.start_fixture_browser(self.core, self.coordinator, self.home))
        self.original.assert_called_once_with(safe, **self.kwargs)
        self.assertIs(self.original, self.core.subprocess.Popen)

    def test_second_matching_launch_is_blocked(self):
        def start(**kwargs):
            self.launch()
            return self.launch()
        self.coordinator.start.side_effect = start
        with self.assertRaises(harness.QualificationFailure):
            harness.start_fixture_browser(self.core, self.coordinator, self.home)
        self.original.assert_called_once()
        self.assertIs(self.original, self.core.subprocess.Popen)


if __name__ == "__main__":
    unittest.main()
