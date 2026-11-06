"""Offline executable selection tests: never spawn Chrome, query metadata, or issue receipts."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location(
    "qualification_browser_choice_harness",
    Path(__file__).resolve().parents[1] / "scripts" / "qualify_shared_runtime.py")
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


class BrowserChoiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.home.chmod(0o700)
        self.chrome = self.home / "Installed Browser With Spaces" / "Google Chrome for Testing"
        self.chrome.parent.mkdir()
        self.chrome.write_text("synthetic executable, never run\n", encoding="utf-8")
        self.chrome.chmod(0o700)
        self.original_popen = mock.Mock(return_value=object())
        self.core = types.SimpleNamespace(subprocess=types.SimpleNamespace(Popen=self.original_popen))
        self.root = self.home / "project"
        self.root.mkdir()
        self.profile = self.root / ".chrome-user-data"
        self.coordinator = types.SimpleNamespace(
            project=types.SimpleNamespace(root=self.root, profile=self.profile), start=mock.Mock())

    def run_main(self, args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = harness.main(args)
        return status, json.loads(output.getvalue())

    def test_main_forwards_path_only_when_selected_and_keeps_diagnostic_gates(self):
        before = dict(os.environ)
        for flags in ([], ["--diagnose-bootstrap"],
                      ["--diagnose-bootstrap", "--observe-daemon-response"]):
            for selected in (False, True):
                with self.subTest(flags=flags, selected=selected):
                    migration = types.SimpleNamespace(record_qualification=mock.Mock())
                    metadata = {"synthetic": True}
                    args = ["--allow-browser-launch", *flags]
                    if selected:
                        args += ["--chrome", str(self.chrome)]
                    with mock.patch.object(harness.sys, "platform", "darwin"), \
                            mock.patch.object(harness.tempfile, "mkdtemp", return_value=str(self.home)), \
                            mock.patch.object(harness.os, "fsencode", return_value=b"short-fixture"), \
                            mock.patch.object(harness, "clean_environment", return_value={"HOME": str(self.home)}), \
                            mock.patch.object(harness, "exercise", return_value=(migration, metadata)) as exercise:
                        status, output = self.run_main(args)
                    self.assertEqual(0, status)
                    expected = {}
                    if flags:
                        expected["diagnose_bootstrap"] = True
                    if "--observe-daemon-response" in flags:
                        expected["observe_daemon_response"] = True
                    if selected:
                        expected["chrome"] = str(self.chrome)
                    exercise.assert_called_once_with(self.home, mock.ANY, **expected)
                    self.assertEqual(bool(flags), output["diagnosticOnly"])
                    self.assertEqual(not bool(flags), output["qualificationIssued"])
                    self.assertEqual({"success", "checks", "failureCode", "diagnosticOnly",
                                      "qualificationIssued", "scratchPath"}, set(output))
                    if not flags:
                        self.assertEqual({"approvedLaunchFlag", "metadataReady", "fixtureProjectReady",
                                          "fixtureBrowser", "fixtureUsesMockKeychain", "syntheticCheckpoint",
                                          "policyRetainsLegacyTask", "migrationPreservesIdentityAndTargets",
                                          "leaseAndRecordedRootPreserved", "normalAutomationBlocked",
                                          "syntheticFormAndNoncePreserved", "cleanupComplete", "receiptWritten"},
                                         set(output["checks"]))
                        self.assertTrue(all(type(value) is bool for value in output["checks"].values()))
                    if flags:
                        migration.record_qualification.assert_not_called()
                    else:
                        migration.record_qualification.assert_called_once_with(metadata, home=Path(before["HOME"]).resolve())
                    self.assertEqual(before, dict(os.environ))

    def invalid_choices(self):
        nonexecutable = self.home / "Chromium"
        nonexecutable.write_text("not executable", encoding="utf-8")
        nonexecutable.chmod(0o600)
        wrong = self.home / "other-browser"
        wrong.write_text("not Chrome", encoding="utf-8")
        wrong.chmod(0o700)
        return ["relative/Google Chrome", str(nonexecutable), str(wrong),
                str(self.home / "missing" / "Chromium"), str(self.chrome.parent),
                str(self.chrome) + " --user-data-dir=/real-profile",
                "--user-data-dir=/real-profile", str(self.chrome) + "\n",
                str(self.chrome) + ";true", str(self.chrome) + "$(true)",
                str(self.home / "--user-data-dir=real" / "Chromium")]

    def test_invalid_choices_rejected_before_scratch_import_or_start(self):
        with mock.patch.object(harness.tempfile, "mkdtemp") as scratch, \
                mock.patch.object(harness, "load_migration") as importing, \
                mock.patch.object(harness, "exercise") as exercise:
            for choice in self.invalid_choices():
                with self.subTest(choice=choice):
                    status, output = self.run_main(["--allow-browser-launch", "--chrome", choice])
                    self.assertEqual(1, status)
                    self.assertIsNone(output["scratchPath"])
                    self.assertFalse(output["qualificationIssued"])
                    with self.assertRaises(harness.QualificationFailure):
                        harness.start_fixture_browser(self.core, self.coordinator, self.home, chrome=choice)
            scratch.assert_not_called()
            importing.assert_not_called()
            exercise.assert_not_called()
        self.coordinator.start.assert_not_called()
        self.original_popen.assert_not_called()

    def test_no_arbitrary_browser_args_or_selector_without_launch_approval(self):
        with mock.patch.object(harness.tempfile, "mkdtemp") as scratch:
            for args in (["--chrome", str(self.chrome)],
                         ["--allow-browser-launch", "--chrome", str(self.chrome), "--user-data-dir=/real"],
                         ["--allow-browser-launch", "--chrome", str(self.chrome), "--chrome-args=--headed"]):
                self.assertEqual(1, self.run_main(args)[0])
            scratch.assert_not_called()

    def launch(self, *, allow_launch, headed, chrome):
        self.assertTrue(allow_launch)
        self.assertFalse(headed)
        executable = chrome or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        argv = [executable, "--user-data-dir=" + str(self.profile),
                "--enable-features=ForceMacVSyncTimerForDebugging", "--headless=new", "about:blank"]
        self.core.subprocess.Popen(argv, env={"HOME": str(self.home)}, stdin=subprocess.DEVNULL)
        return {"status": "started"}

    def test_selected_spaces_and_default_preserve_exact_fixture_launch(self):
        for chrome in (None, str(self.chrome)):
            with self.subTest(chrome=chrome):
                self.coordinator.start.reset_mock()
                self.original_popen.reset_mock()
                self.coordinator.start.side_effect = self.launch
                self.assertTrue(harness.start_fixture_browser(self.core, self.coordinator, self.home, chrome=chrome))
                self.coordinator.start.assert_called_once_with(allow_launch=True, headed=False, chrome=chrome)
                argv = self.original_popen.call_args.args[0]
                self.assertEqual(chrome or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", argv[0])
                self.assertEqual(["--user-data-dir=" + str(self.profile)],
                                 [arg for arg in argv if arg.startswith("--user-data-dir")])
                self.assertIn("--enable-features=ForceMacVSyncTimerForDebugging", argv)
                self.assertIn("--headless=new", argv)
                self.assertEqual(["--password-store=basic", "--use-mock-keychain", "about:blank"], argv[-3:])
                self.assertEqual(str(self.home), self.original_popen.call_args.kwargs["env"]["HOME"])
                self.assertIs(self.original_popen, self.core.subprocess.Popen)

    def test_selected_browser_cannot_bypass_profile_home_or_shell_guards(self):
        def launch_bad(*, chrome, **unused):
            args = [chrome, "--user-data-dir=" + str(self.profile), "about:blank"]
            kwargs = {"env": {"HOME": str(self.home)}}
            if violation == "profile":
                args[1] = "--user-data-dir=/real-profile"
            elif violation == "home":
                kwargs["env"]["HOME"] = "/real-home"
            elif violation == "shell":
                kwargs["shell"] = True
            self.core.subprocess.Popen(args, **kwargs)
            return {"status": "started"}
        self.coordinator.start.side_effect = launch_bad
        for violation in ("profile", "home", "shell"):
            with self.subTest(violation=violation):
                with self.assertRaises(harness.QualificationFailure):
                    harness.start_fixture_browser(self.core, self.coordinator, self.home, chrome=str(self.chrome))
                self.original_popen.assert_not_called()
                self.assertIs(self.original_popen, self.core.subprocess.Popen)

    def test_selected_popen_failure_restores_source_popen(self):
        self.coordinator.start.side_effect = self.launch
        self.original_popen.side_effect = OSError("synthetic")
        with self.assertRaises(OSError):
            harness.start_fixture_browser(self.core, self.coordinator, self.home, chrome=str(self.chrome))
        self.assertIs(self.original_popen, self.core.subprocess.Popen)

    def test_exercise_forwards_selector_before_any_browser_or_metadata_query(self):
        # Stop at the mocked fixture launcher, before HTTP threads or real queries.
        class StopFixture(Exception):
            pass
        for chrome in (None, str(self.chrome)):
            with self.subTest(chrome=chrome), tempfile.TemporaryDirectory() as directory:
                home = Path(directory).resolve()
                project = types.SimpleNamespace(root=home / "project", profile=home / "project" / ".chrome-user-data", key="fixture")
                store = types.SimpleNamespace(lock=contextlib.nullcontext)
                coordinator = types.SimpleNamespace(project=project)
                core = types.SimpleNamespace(
                    checked_process=mock.Mock(), resolve_project=mock.Mock(return_value=project),
                    StateStore=mock.Mock(return_value=store), DEFAULT_STATE_DIR="unused",
                    Coordinator=mock.Mock(return_value=coordinator), session_name=mock.Mock(return_value="fixture"),
                    verify_browser=mock.Mock(return_value=None), discover_processes=mock.Mock(return_value=[]))
                runtime = types.SimpleNamespace(legacy_socket_directory=lambda key: home / ".ab" / "legacy")
                migration = types.SimpleNamespace(_modules=lambda: (core, runtime), qualification_metadata=mock.Mock(return_value={}))
                with mock.patch.object(harness, "load_migration", return_value=migration), \
                        mock.patch.object(harness.shutil, "which", return_value="/synthetic/git"), \
                        mock.patch.object(harness.os, "fsencode", return_value=b"short-fixture"), \
                        mock.patch.object(harness, "start_fixture_browser", side_effect=StopFixture) as start:
                    with self.assertRaises(StopFixture):
                        harness.exercise(home, {}, chrome=chrome)
                if chrome is None:
                    start.assert_called_once_with(core, coordinator, home)
                else:
                    start.assert_called_once_with(core, coordinator, home, chrome=chrome)

    def test_help_explicitly_limits_selector_to_disposable_fixture(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            harness.main(["--help"])
        help_text = " ".join(output.getvalue().split())
        self.assertIn("--chrome PATH", help_text)
        self.assertIn("disposable HEADLESS fixture only", help_text)
        self.assertIn("does not upgrade an existing profile", help_text)


if __name__ == "__main__":
    unittest.main()
