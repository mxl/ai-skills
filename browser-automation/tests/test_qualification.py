"""Offline receipt and disposable-harness guards; never live qualification evidence."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SECRET = "SYNTHETIC_SECRET_MUST_NOT_LEAK"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    # These modules have no import-time HOME reads or browser startup.
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


migration = load("qualification_test_migration", "migrate_controller.py")
harness = load("qualification_test_harness", "qualify_shared_runtime.py")


class IsolatedTests(unittest.TestCase):
    def patch(self, obj, name, *args, **kwargs):
        patcher = mock.patch.object(obj, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def setUp(self):
        # Canonical short root avoids macOS /tmp symlink and socket-length traps.
        temporary = tempfile.TemporaryDirectory(prefix="q", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve(strict=True)
        self.home.chmod(0o700)
        environment = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.patch(Path, "home", return_value=self.home)
        self.receipt = self.home / "evidence" / "receipt.json"
        self.standard_receipt_path = migration.receipt_path
        self.receipt_path_mock = self.patch(migration, "receipt_path", return_value=self.receipt)
        self.process = self.patch(migration.subprocess, "run", side_effect=AssertionError("no subprocess"))
        self.popen = self.patch(migration.subprocess, "Popen", side_effect=AssertionError("no launch"))
        self.kill = self.patch(os, "kill", side_effect=AssertionError("no process signals"))
        self.which = self.patch(migration.shutil, "which", side_effect=AssertionError("no binary discovery"))
        self.patch(harness, "FixtureHTTPServer", side_effect=AssertionError("no network"))
        self.patch(harness, "load_migration", side_effect=AssertionError("no runtime imports"))
        self.executable = self.home / "agent-browser"
        self.executable.write_bytes(b"offline fixture, never execute\n")
        self.executable.chmod(0o700)
        self.metadata = {"agentBrowserPath": str(self.executable),
                         "agentBrowserSha256": hashlib.sha256(self.executable.read_bytes()).hexdigest(),
                         "agentBrowserVersion": "agent-browser fixture-1"}
        self.evidence = dict(self.metadata, version=1, passed=True, chromeVersion="Chrome/fixture-1")

    def write_receipt(self, value=None):
        self.receipt.parent.mkdir(mode=0o700, exist_ok=True)
        self.receipt.write_text(json.dumps(self.evidence if value is None else value), encoding="utf-8")
        self.receipt.chmod(0o600)

    def stat_with(self, info, **changes):
        fields = {key: getattr(info, key) for key in
                  ("st_mode", "st_uid", "st_nlink", "st_size")}
        return types.SimpleNamespace(**(fields | changes))


class MetadataTests(IsolatedTests):
    def test_version_query_uses_only_private_sandbox_config_home_cwd_and_tmpdir(self):
        self.which.side_effect = lambda name: str(self.executable) if name == "agent-browser" else None
        sandboxes = []
        def version(argv, **kwargs):
            sandbox = Path(kwargs["cwd"])
            config = Path(argv[2])
            self.assertEqual([str(self.executable), "--config", str(config), "--version"], argv)
            self.assertTrue(config.is_absolute())
            self.assertEqual(sandbox, config.parent)
            self.assertNotEqual(self.home, sandbox)
            self.assertEqual(0o700, stat.S_IMODE(sandbox.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(config.stat().st_mode))
            self.assertEqual(os.getuid(), config.stat().st_uid)
            self.assertEqual({}, json.loads(config.read_text()))
            env = kwargs["env"]
            self.assertEqual(str(sandbox), env["HOME"])
            self.assertEqual(str(sandbox), env["TMPDIR"])
            self.assertEqual("1", env["PYTHONDONTWRITEBYTECODE"])
            self.assertEqual({"HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE"}, set(env))
            self.assertIs(kwargs["stdin"], migration.subprocess.DEVNULL)
            self.assertIs(kwargs["stdout"], migration.subprocess.PIPE)
            self.assertIs(kwargs["stderr"], migration.subprocess.PIPE)
            self.assertTrue(kwargs["check"])
            self.assertEqual(5, kwargs["timeout"])
            sandboxes.append(sandbox)
            return types.SimpleNamespace(stdout=self.metadata["agentBrowserVersion"] + "\n")
        self.process.side_effect = version
        self.assertEqual(self.metadata, migration.qualification_metadata())
        self.process.assert_called_once()
        self.assertFalse(sandboxes[0].exists())
        self.assertFalse(self.receipt.exists())

    def test_child_environment_disables_bytecode(self):
        self.which.side_effect = lambda name, **kwargs: str(self.executable)
        env = harness.clean_environment(self.home, {"PATH": "/offline"})
        self.assertEqual("1", env["PYTHONDONTWRITEBYTECODE"])
        self.assertEqual(str(self.home), env["HOME"])
        self.assertEqual(str(self.home / "tmp"), env["TMPDIR"])
        self.process.assert_not_called()


class ReceiptTests(IsolatedTests):
    def test_explicit_home_selects_fixed_receipt_without_changing_home(self):
        selected = self.home / "other-home"
        selected.mkdir(mode=0o700)
        expected = selected / "Library" / "Application Support" / "browser-automation" / "controller-migration-qualification.json"
        self.assertEqual(expected, self.standard_receipt_path(selected))
        self.assertEqual(self.home, Path.home())
        self.patch(migration, "qualification_metadata", return_value=self.metadata)
        migration.record_qualification(self.evidence, home=selected)
        self.receipt_path_mock.assert_called_once_with(selected)
        self.assertEqual(str(self.home), os.environ["HOME"])

    def test_absent_receipt_returns_before_metadata_or_binary_discovery(self):
        metadata = self.patch(migration, "qualification_metadata")
        self.assertFalse(migration.qualification_verified())
        metadata.assert_not_called()
        self.which.assert_not_called()
        self.process.assert_not_called()
        self.assertFalse(self.receipt.parent.exists())

    def test_matching_mock_metadata_verifies_then_changed_executable_invalidates(self):
        self.write_receipt()
        def current():
            return self.metadata | {"agentBrowserSha256": hashlib.sha256(self.executable.read_bytes()).hexdigest()}
        self.patch(migration, "qualification_metadata", side_effect=current)
        self.assertTrue(migration.qualification_verified())
        self.executable.write_bytes(b"changed fixture\n")
        self.assertFalse(migration.qualification_verified())
        self.process.assert_not_called()

    def test_mode_owner_nlink_and_size_fail_before_metadata(self):
        self.write_receipt()
        metadata = self.patch(migration, "qualification_metadata")
        info = self.receipt.stat()
        for changes in ({"st_mode": stat.S_IFREG | 0o644},
                        {"st_uid": os.getuid() + 1}, {"st_nlink": 2},
                        {"st_size": 16385}, {"st_mode": stat.S_IFDIR | 0o600}):
            with self.subTest(changes=changes), mock.patch.object(
                    migration.os, "fstat", return_value=self.stat_with(info, **changes)):
                self.assertFalse(migration.qualification_verified())
        metadata.assert_not_called()

    def test_actual_hardlink_and_permissive_mode_rejected(self):
        self.write_receipt()
        metadata = self.patch(migration, "qualification_metadata")
        self.receipt.chmod(0o644)
        self.assertFalse(migration.qualification_verified())
        self.receipt.chmod(0o600)
        os.link(self.receipt, self.home / "hardlink")
        self.assertFalse(migration.qualification_verified())
        metadata.assert_not_called()

    def test_symlink_receipt_and_parent_rejected(self):
        self.write_receipt()
        target = self.home / "target.json"
        target.write_bytes(self.receipt.read_bytes())
        target.chmod(0o600)
        self.receipt.unlink()
        self.receipt.symlink_to(target)
        self.assertFalse(migration.qualification_verified())
        self.receipt.unlink()
        self.receipt.parent.rmdir()
        directory = self.home / "real-evidence"
        directory.mkdir(mode=0o700)
        (directory / self.receipt.name).write_bytes(target.read_bytes())
        (directory / self.receipt.name).chmod(0o600)
        self.receipt.parent.symlink_to(directory, target_is_directory=True)
        self.assertFalse(migration.qualification_verified())
        self.which.assert_not_called()

    def test_unknown_missing_fields_and_invalid_types_rejected(self):
        metadata = self.patch(migration, "qualification_metadata", return_value=self.metadata)
        variants = [self.evidence | {"unknown": True},
                    {key: value for key, value in self.evidence.items() if key != "chromeVersion"},
                    [], None]
        for field, values in {
            "version": [True, "1", 2, None], "passed": [1, False, "true"],
            "agentBrowserPath": [1, "relative", "", str(self.home / "missing")],
            "agentBrowserSha256": [None, "A" * 64, "x" * 64, "a" * 63],
            "agentBrowserVersion": [False, "", "version\nsecret", "x" * 4097],
            "chromeVersion": [1, "", "Firefox/1", "Chrome/1\nsecret"],
        }.items():
            variants.extend(self.evidence | {field: value} for value in values)
        for index, value in enumerate(variants):
            with self.subTest(index=index):
                # None is intentionally serialized as null, not the helper default.
                self.write_receipt()
                self.receipt.write_text(json.dumps(value), encoding="utf-8")
                self.assertFalse(migration.qualification_verified())
        metadata.assert_not_called()

    def test_duplicate_fields_and_malformed_json_rejected(self):
        self.write_receipt()
        for payload in (b"{", b"\xff", (json.dumps(self.evidence)[:-1] + ', "version": 1}').encode()):
            with self.subTest(payload=payload):
                self.receipt.write_bytes(payload)
                self.assertFalse(migration.qualification_verified())
        self.which.assert_not_called()

    def test_metadata_path_hash_and_version_mismatches_rejected(self):
        self.write_receipt()
        for field, value in (("agentBrowserPath", str(self.home / "other")),
                             ("agentBrowserSha256", "0" * 64),
                             ("agentBrowserVersion", "changed-version")):
            with self.subTest(field=field), mock.patch.object(
                    migration, "qualification_metadata", return_value=self.metadata | {field: value}):
                self.assertFalse(migration.qualification_verified())

    def test_metadata_errors_fail_closed(self):
        self.write_receipt()
        for error in (ValueError(SECRET), OSError(SECRET), RuntimeError(SECRET),
                      migration.subprocess.SubprocessError(SECRET)):
            with self.subTest(error=type(error)), mock.patch.object(
                    migration, "qualification_metadata", side_effect=error):
                self.assertFalse(migration.qualification_verified())

    def test_record_is_atomic_private_and_readable(self):
        self.patch(migration, "qualification_metadata", return_value=self.metadata)
        replace = migration.os.replace
        observations = []
        def checked_replace(source, destination):
            source = Path(source)
            self.assertEqual(self.receipt.parent, source.parent)
            self.assertEqual(0o600, stat.S_IMODE(source.stat().st_mode))
            self.assertEqual(self.evidence, json.loads(source.read_text()))
            observations.append(destination)
            return replace(source, destination)
        self.patch(migration.os, "replace", side_effect=checked_replace)
        migration.record_qualification(self.evidence)
        self.assertEqual([self.receipt], observations)
        self.assertEqual(0o700, stat.S_IMODE(self.receipt.parent.stat().st_mode))
        self.assertEqual(os.getuid(), self.receipt.stat().st_uid)
        self.assertEqual(0o600, stat.S_IMODE(self.receipt.stat().st_mode))
        self.assertEqual([self.receipt], list(self.receipt.parent.iterdir()))
        self.assertTrue(migration.qualification_verified())

    def test_record_rejects_invalid_data_before_creating_parent(self):
        metadata = self.patch(migration, "qualification_metadata", return_value=self.metadata)
        for value in (self.evidence | {"unknown": 1}, self.evidence | {"passed": 1},
                      self.evidence | {"version": True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                migration.record_qualification(value)
        metadata.assert_not_called()
        self.assertFalse(self.receipt.parent.exists())

    def test_record_rejects_changed_metadata_without_writes(self):
        self.patch(migration, "qualification_metadata", return_value=self.metadata | {"agentBrowserVersion": "changed"})
        with self.assertRaises(ValueError):
            migration.record_qualification(self.evidence)
        self.assertFalse(self.receipt.parent.exists())

    def test_record_requires_private_owned_parent(self):
        self.patch(migration, "qualification_metadata", return_value=self.metadata)
        self.receipt.parent.mkdir(mode=0o755)
        self.receipt.parent.chmod(0o755)
        with self.assertRaises(ValueError):
            migration.record_qualification(self.evidence)
        self.receipt.parent.chmod(0o700)
        original_lstat = Path.lstat
        def wrong_owner(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            return self.stat_with(info, st_uid=os.getuid() + 1) if path == self.receipt.parent else info
        with mock.patch.object(Path, "lstat", wrong_owner), self.assertRaises(ValueError):
            migration.record_qualification(self.evidence)
        self.assertFalse(self.receipt.exists())

    def test_record_rejects_existing_unsafe_receipt_and_symlink(self):
        self.patch(migration, "qualification_metadata", return_value=self.metadata)
        self.write_receipt()
        before = self.receipt.read_bytes()
        self.receipt.chmod(0o644)
        with self.assertRaises(ValueError):
            migration.record_qualification(self.evidence)
        self.assertEqual(before, self.receipt.read_bytes())
        self.receipt.unlink()
        self.receipt.symlink_to(self.home / "absent")
        with self.assertRaises(ValueError):
            migration.record_qualification(self.evidence)
        self.assertTrue(self.receipt.is_symlink())
        self.assertFalse((self.home / "absent").exists())

    def test_failed_atomic_replace_preserves_prior_receipt_and_removes_temporary(self):
        self.patch(migration, "qualification_metadata", return_value=self.metadata)
        self.write_receipt()
        before = self.receipt.read_bytes()
        self.patch(migration.os, "replace", side_effect=OSError(SECRET))
        with self.assertRaises(OSError):
            migration.record_qualification(self.evidence)
        self.assertEqual(before, self.receipt.read_bytes())
        self.assertEqual([self.receipt], list(self.receipt.parent.iterdir()))


class HarnessGuardTests(IsolatedTests):
    def test_fixture_home_and_bytecode_guards_survive_through_receipt_write(self):
        temporary = tempfile.TemporaryDirectory(prefix="b", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        fixture = Path(temporary.name).resolve(strict=True)
        fixture.chmod(0o700)
        self.patch(harness.sys, "platform", "darwin")
        self.patch(harness.tempfile, "mkdtemp", return_value=str(fixture))
        self.patch(harness, "clean_environment", return_value={
            "HOME": str(fixture), "TMPDIR": str(fixture),
            "PATH": "/offline", "PYTHONDONTWRITEBYTECODE": "1"})
        events = []
        def module_load():
            self.assertTrue(sys.dont_write_bytecode)
            self.assertEqual(str(fixture), os.environ["HOME"])
            events.append("load")
            return migration
        self.patch(harness, "load_migration", side_effect=module_load)
        def exercise(home, checks):
            self.assertEqual(fixture, home)
            return harness.load_migration(), self.evidence
        self.patch(harness, "exercise", side_effect=exercise)
        def record(info, *, home=None):
            self.assertEqual(self.evidence, info)
            self.assertEqual(self.home, home)
            self.assertEqual(str(fixture), os.environ["HOME"])
            self.assertEqual("1", os.environ["PYTHONDONTWRITEBYTECODE"])
            self.assertTrue(sys.dont_write_bytecode)
            events.append("record")
        recorder = self.patch(migration, "record_qualification", side_effect=record)
        environment = dict(os.environ)
        # Start false to prove the harness sets the flag rather than inheriting it.
        with mock.patch.object(sys, "dont_write_bytecode", False):
            for failure in (False, True):
                with self.subTest(receipt_failure=failure):
                    def maybe_fail(info, *, home=None):
                        record(info, home=home)
                        if failure:
                            raise RuntimeError(SECRET)
                    recorder.side_effect = maybe_fail
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        self.assertEqual(1 if failure else 0, harness.main(["--allow-browser-launch"]))
                    self.assertEqual(not failure, json.loads(stdout.getvalue())["success"])
                    self.assertFalse(sys.dont_write_bytecode)
                    self.assertEqual(environment, dict(os.environ))
                    self.assertNotIn(SECRET, stdout.getvalue() + stderr.getvalue())
                    self.assertEqual("", stderr.getvalue())
        self.assertEqual(["load", "record", "load", "record"], events)
        self.assertFalse(self.receipt.exists())
        self.process.assert_not_called()

    def test_bytecode_flag_restored_when_module_load_fails(self):
        self.patch(harness.sys, "platform", "darwin")
        self.patch(harness.tempfile, "mkdtemp", return_value=str(self.home))
        self.patch(harness, "clean_environment", return_value={
            "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"})
        def failed_load():
            self.assertTrue(sys.dont_write_bytecode)
            raise RuntimeError(SECRET)
        loader = self.patch(harness, "load_migration", side_effect=failed_load)
        self.patch(harness, "exercise", side_effect=lambda home, checks: harness.load_migration())
        environment = dict(os.environ)
        with mock.patch.object(sys, "dont_write_bytecode", False):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(1, harness.main(["--allow-browser-launch"]))
            self.assertFalse(sys.dont_write_bytecode)
        loader.assert_called_once()
        self.assertEqual(environment, dict(os.environ))
        self.assertNotIn(SECRET, stdout.getvalue() + stderr.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_missing_approval_and_invalid_arguments_have_no_side_effects(self):
        exercise = self.patch(harness, "exercise", side_effect=AssertionError("no harness"))
        clean = self.patch(harness, "clean_environment", side_effect=AssertionError("no environment setup"))
        mkdtemp = self.patch(harness.tempfile, "mkdtemp", side_effect=AssertionError("no scratch"))
        mkdir = self.patch(Path, "mkdir", side_effect=AssertionError("no directories"))
        record = self.patch(migration, "record_qualification", side_effect=AssertionError("no receipt"))
        self.patch(harness.sys, "platform", "darwin")
        environment = dict(os.environ)
        for arguments in ([], ["--unknown=" + SECRET]):
            with self.subTest(arguments=arguments):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    self.assertEqual(1, harness.main(arguments))
                result = json.loads(stdout.getvalue())
                self.assertFalse(result["success"])
                self.assertFalse(any(result["checks"].values()))
                self.assertIsNone(result["scratchPath"])
                self.assertEqual("", stderr.getvalue())
                self.assertNotIn(SECRET, stdout.getvalue())
                self.assertEqual(environment, dict(os.environ))
        for boundary in (exercise, clean, mkdtemp, mkdir, record, self.popen, self.process, self.which):
            boundary.assert_not_called()

    def test_secret_exception_is_sanitized_without_traceback(self):
        self.patch(harness.sys, "platform", "darwin")
        scratch = self.patch(harness.tempfile, "mkdtemp", side_effect=RuntimeError(SECRET))
        exercise = self.patch(harness, "exercise", side_effect=AssertionError("no harness"))
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(1, harness.main(["--allow-browser-launch"]))
        self.assertFalse(json.loads(stdout.getvalue())["success"])
        self.assertNotIn(SECRET, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())
        self.assertEqual("", stderr.getvalue())
        scratch.assert_called_once()
        exercise.assert_not_called()
        self.assertFalse(self.receipt.exists())

    def test_ambiguous_process_identity_never_signals(self):
        core = mock.Mock()
        for current in ("different-start-time",):
            with mock.patch.object(harness, "process_start", return_value=current):
                with self.assertRaises(harness.QualificationFailure):
                    harness.terminate_verified(core, 424242, "expected-start-time")
        self.kill.assert_not_called()
        core.assert_not_called()

    def test_already_gone_process_requires_no_signal(self):
        with mock.patch.object(harness, "process_start", return_value=""):
            self.assertTrue(harness.terminate_verified(mock.Mock(), 424242, "expected"))
        self.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
