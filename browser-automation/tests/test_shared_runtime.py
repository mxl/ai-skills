"""Offline shared-transport contracts; all HOME and process boundaries are isolated."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
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


sr = load("shared_runtime_under_test", "shared_runtime.py")
bs = load("shared_runtime_browser_under_test", "browser_session.py")
remote = load("shared_runtime_remote_under_test", "remote_access.py")
KEY = "a" * 64
SECOND = "b" * 64
THIRD = "c" * 64
BROWSER = {"pid": 123, "started": "fixed-time", "executable": "/fake/Chrome",
           "port": 12345, "endpoint_hash": "hash", "headed": False}


class SharedRuntimeTests(unittest.TestCase):
    def setUp(self):
        # macOS's default TMPDIR is too long for the <=52-byte socket contract.
        temporary = tempfile.TemporaryDirectory(prefix="", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.patch(Path, "home", return_value=self.home)
        modules = mock.patch.dict(sys.modules, {"_browser_automation_shared_runtime": sr})
        modules.start()
        self.addCleanup(modules.stop)
        self.patch(bs, "checked_process", side_effect=AssertionError("no subprocess"))
        self.patch(bs.subprocess, "Popen", side_effect=AssertionError("no launch"))
        self.patch(bs, "verify_browser", return_value=copy.deepcopy(BROWSER))
        self.patch(bs, "cdp_target_ids", return_value={"TARGET001", "TARGET002"})
        self.patch(bs, "discover_processes", side_effect=AssertionError("no discovery"))
        self.socket = self.home / ".ab" / "shared"
        self.path = sr.policy_path()
        self.project = bs.Project(self.home / "project-one", KEY, False)
        self.project.root.mkdir()
        self.resolver = self.patch(bs, "resolve_project", return_value=self.project)
        self.patch(sr, "_load_core", return_value=bs)

    def patch(self, obj, name, **kwargs):
        patcher = mock.patch.object(obj, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises((sr.SharedRuntimeError, bs.BrowserSessionError)) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def policy(self, **changes):
        return {"version": 1, "socketDirectory": str(self.socket),
                "projects": [KEY], **changes}

    def write_policy(self, value=None, raw=None):
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.path.parent.chmod(0o700)
        self.path.write_text(raw if raw is not None else json.dumps(
            self.policy() if value is None else value), encoding="utf-8")
        self.path.chmod(0o600)

    def coordinator(self, project=None):
        project = project or self.project
        store = bs.StateStore(self.home / "state", project)
        return bs.Coordinator(project, store)

    def task(self, coordinator, name="existing", target="TARGET001", **fields):
        value = {"session": bs.session_name(coordinator.project.key, name),
                 "status": "active", "targetId": target,
                 "origin": "https://example.com", "auth": False, **fields}
        coordinator.state["tasks"][name] = value
        coordinator.state["browser"] = bs.browser_identity(BROWSER)
        coordinator.store.save(coordinator.state)
        return value

    def test_absent_policy_is_legacy_and_read_only(self):
        before = set(self.home.rglob("*"))
        self.assertIsNone(sr.read_policy())
        self.assertEqual(self.home / ".ab" / KEY[:24], sr.project_socket_directory(KEY))
        self.assertEqual(before, set(self.home.rglob("*")))

    def test_policy_schema_rejects_bool_version_unknown_fields_and_invalid_full_keys(self):
        invalid = [[], {}, self.policy(version=True), self.policy(version=2),
                   self.policy(extra="TOP_SECRET"), self.policy(projects=KEY),
                   self.policy(projects=[KEY, KEY]), self.policy(socketDirectory=1)]
        invalid += [self.policy(projects=[key]) for key in (
            "a" * 24, "a" * 63, "a" * 65, "A" * 64, "g" * 64, 1, True, None)]
        for value in invalid:
            with self.subTest(value=value):
                self.write_policy(value)
                self.assertCode("invalid_state", sr.read_policy)
        self.write_policy()
        self.assertEqual(self.policy(), sr.read_policy())

    def test_duplicate_json_keys_rejected_at_every_object_level(self):
        for raw in ('{"version":1,"version":1,"socketDirectory":"TOP_SECRET","projects":[]}',
                    '{"version":1,"socketDirectory":"TOP_SECRET","projects":[{"x":1,"x":2}]}'):
            self.write_policy(raw=raw)
            self.assertCode("invalid_state", sr.read_policy)

    def test_policy_requires_owner_0600_regular_single_link_file(self):
        self.write_policy()
        for mode in (0o644, 0o400, 0o660, 0o700):
            self.path.chmod(mode)
            self.assertCode("unsafe_path", sr.read_policy)
        self.path.chmod(0o600)
        with mock.patch.object(sr.os, "getuid", return_value=os.getuid() + 1):
            self.assertCode("unsafe_path", sr.read_policy)
            self.assertCode("unsafe_path", sr._open_private, self.path, os.O_RDONLY)
        self.path.parent.chmod(0o755)
        self.assertCode("unsafe_path", sr.read_policy)
        self.path.parent.chmod(0o700)
        other = self.home / "hardlink"
        os.link(self.path, other)
        self.assertCode("unsafe_path", sr.read_policy)
        other.unlink()
        self.path.unlink()
        self.path.mkdir(mode=0o700)
        self.assertCode("unsafe_path", sr.read_policy)

    def test_policy_symlink_and_symlink_parent_fail_closed(self):
        self.path.parent.mkdir(parents=True, mode=0o700)
        self.path.symlink_to(self.home / "missing")
        self.assertCode("unsafe_path", sr.read_policy)
        self.path.unlink()
        self.path.parent.rmdir()
        self.path.parent.symlink_to(self.home / "missing-parent")
        self.assertCode("unsafe_path", sr.read_policy)

    def test_socket_root_is_short_canonical_direct_child_without_writes(self):
        self.assertEqual(self.socket, sr.validate_socket_directory(str(self.socket)))
        exact = self.home / ".ab" / ("x" * (52 - len(os.fsencode(self.home / ".ab")) - 1))
        self.assertEqual(52, len(os.fsencode(exact)))
        self.assertEqual(exact, sr.validate_socket_directory(exact))
        invalid = [str(exact) + "x", "relative", self.home, self.home / ".ab",
                   self.socket / "nested", str(self.socket) + "/", str(self.socket) + "/../other",
                   self.home / ".ab" / "has space", self.home / ".ab" / ".hidden", True]
        for value in invalid:
            with self.subTest(value=value):
                self.assertCode("unsafe_path", sr.validate_socket_directory, value)
        self.assertFalse((self.home / ".ab").exists())

    def test_socket_root_permissions_owner_and_symlinks(self):
        base = self.socket.parent
        base.mkdir(mode=0o755)
        base.chmod(0o755)  # Existing legacy namespace need not be private.
        self.socket.mkdir(mode=0o700)
        self.assertEqual(self.socket, sr.validate_socket_directory(self.socket))
        self.socket.chmod(0o755)
        self.assertCode("unsafe_path", sr.validate_socket_directory, self.socket)
        self.socket.chmod(0o700)
        with mock.patch.object(sr.os, "getuid", return_value=os.getuid() + 1):
            self.assertCode("unsafe_path", sr.validate_socket_directory, self.socket)
        self.socket.rmdir()
        self.socket.symlink_to(self.home / "missing")
        self.assertCode("unsafe_path", sr.validate_socket_directory, self.socket)
        self.socket.unlink()
        base.rmdir()
        base.symlink_to(self.project.root)
        self.assertCode("unsafe_path", sr.validate_socket_directory, self.socket)

    def test_plan_is_read_only_even_when_all_directories_are_absent(self):
        before = set(self.home.rglob("*"))
        result = sr.plan(self.socket, [self.project.root])
        self.assertEqual({"status": "planned", "policy": self.policy(),
                          "controllersMoved": False}, result)
        self.assertEqual(before, set(self.home.rglob("*")))
        self.assertFalse(self.path.parent.exists())
        self.assertFalse(self.socket.parent.exists())

    def test_configure_requires_approval_before_resolution_or_any_write(self):
        before = set(self.home.rglob("*"))
        self.assertCode("confirmation_required", sr.configure, self.socket, [self.project.root])
        self.resolver.assert_not_called()
        self.assertEqual(before, set(self.home.rglob("*")))

    def test_invalid_preflight_never_creates_policy_or_socket_directories(self):
        self.assertCode("unsafe_path", sr.configure, self.home / "outside", [self.project.root], allow_write=True)
        self.assertFalse(self.path.parent.exists())
        self.assertFalse(self.socket.parent.exists())

    def test_configure_atomic_private_permissions_and_global_flock(self):
        events = []
        real_replace = sr.os.replace
        real_flock = sr.fcntl.flock
        def replace(source, destination):
            events.append("replace")
            self.assertEqual(self.path.parent, Path(source).parent)
            self.assertEqual(0o600, stat.S_IMODE(Path(source).stat().st_mode))
            return real_replace(source, destination)
        def flock(fd, operation):
            self.assertEqual(sr.fcntl.LOCK_EX, operation)
            self.assertEqual((self.path.parent / "shared-runtime.lock").stat().st_ino,
                             os.fstat(fd).st_ino)
            events.append("lock")
            return real_flock(fd, operation)
        with mock.patch.object(sr.fcntl, "flock", side_effect=flock), \
                mock.patch.object(sr.os, "replace", side_effect=replace), \
                mock.patch.object(sr.os, "fsync", side_effect=lambda fd: events.append("sync")):
            result = sr.configure(self.socket, [self.project.root], allow_write=True)
        self.assertEqual(["lock", "sync", "replace", "sync"], events)
        self.assertEqual("configured", result["status"])
        self.assertFalse(result["controllersMoved"])
        self.assertEqual(self.policy(), sr.read_policy())
        for directory in (self.path.parent, self.path.parent.parent, self.path.parent.parent.parent):
            self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))
        for path in (self.path, self.path.parent / "shared-runtime.lock"):
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertFalse(self.socket.exists())

    def test_atomic_replace_failure_preserves_policy_and_cleans_temporary_file(self):
        self.write_policy()
        before = self.path.read_bytes()
        with mock.patch.object(sr.os, "replace", side_effect=OSError("TOP_SECRET")):
            self.assertCode("unsafe_path", sr.configure, self.socket, [self.project.root], allow_write=True)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual([], list(self.path.parent.glob(".shared-runtime-*")))

    def test_configure_merges_members_again_under_global_lock(self):
        self.write_policy()
        self.resolver.return_value = bs.Project(self.project.root, SECOND, False)
        real_flock = sr.fcntl.flock
        def concurrent_enrollment(fd, operation):
            real_flock(fd, operation)
            self.write_policy(self.policy(projects=[KEY, THIRD]))
        with mock.patch.object(sr.fcntl, "flock", side_effect=concurrent_enrollment):
            result = sr.configure(self.socket, [self.project.root], allow_write=True)
        self.assertEqual([KEY, SECOND, THIRD], result["policy"]["projects"])
        self.assertEqual([KEY, SECOND, THIRD], sr.read_policy()["projects"])

    def test_existing_shared_root_cannot_be_switched_by_plan_or_configure(self):
        self.write_policy()
        before = self.path.read_bytes()
        for function, kwargs in ((sr.plan, {}), (sr.configure, {"allow_write": True})):
            self.assertCode("shared_root_change_required", function, self.home / ".ab" / "other",
                            [self.project.root], **kwargs)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse((self.path.parent / "shared-runtime.lock").exists())

    def test_enrollment_preserves_existing_transport_and_only_changes_default(self):
        legacy = sr.legacy_socket_directory(KEY)
        self.write_policy()
        self.assertEqual(self.socket, sr.project_socket_directory(KEY))
        self.assertEqual(legacy, sr.task_socket_directory(KEY, {}))
        self.assertEqual(legacy, sr.task_socket_directory(KEY, {"socketDir": str(legacy)}))
        self.assertEqual(self.socket, sr.task_socket_directory(KEY, {"socketDir": str(self.socket)}))
        self.assertEqual(sr.legacy_socket_directory(SECOND), sr.project_socket_directory(SECOND))
        self.assertNotEqual(legacy, sr.legacy_socket_directory(SECOND))
        self.assertCode("invalid_state", sr.task_socket_directory, SECOND, {"socketDir": str(self.socket)})
        self.assertCode("invalid_state", sr.task_socket_directory, KEY, {"socketDir": None})
        self.assertCode("invalid_state", sr.project_socket_directory, KEY[:24])

    def test_new_task_records_configured_default_while_old_task_remains_legacy(self):
        c = self.coordinator()
        old = self.task(c)
        self.write_policy()
        with mock.patch.object(bs, "cdp_target_ids", side_effect=[{"TARGET001"}, {"TARGET001", "TARGET002"}]), \
                mock.patch.object(c, "agent", return_value=[{"targetId": "TARGET002", "url": "https://example.com", "active": True}]):
            c.attach("new-task", "https://example.com")
        self.assertNotIn("socketDir", old)
        self.assertEqual(str(self.socket), c.store.load()["tasks"]["new-task"]["socketDir"])
        self.assertEqual(sr.legacy_socket_directory(KEY), sr.task_socket_directory(KEY, old))

    def test_core_agent_forces_actual_task_root_over_enrolled_global_default(self):
        self.write_policy()
        c = self.coordinator()
        task = self.task(c)
        fake = subprocess.CompletedProcess([], 0, '{"success":true,"data":{}}', "")
        with mock.patch.object(bs.shutil, "which", return_value="/fake/agent-browser"), \
                mock.patch.object(bs, "checked_process", return_value=fake) as process:
            c.agent(BROWSER, task, ["reload"])
            self.assertEqual(str(sr.legacy_socket_directory(KEY)),
                             process.call_args.kwargs["env"]["AGENT_BROWSER_SOCKET_DIR"])
            task["socketDir"] = str(self.socket)
            c.agent(BROWSER, task, ["reload"])
            self.assertEqual(str(self.socket), process.call_args.kwargs["env"]["AGENT_BROWSER_SOCKET_DIR"])
            self.assertEqual(c.store.directory, process.call_args.kwargs["cwd"])
            args = process.call_args.args[0]
            self.assertEqual(task["session"], args[args.index("--session") + 1])

    def test_shared_transport_does_not_merge_profiles_state_sessions_or_human_leases(self):
        self.write_policy(self.policy(projects=[KEY, SECOND]))
        second_root = self.home / "project-two"
        second_root.mkdir()
        a, b = self.coordinator(), self.coordinator(bs.Project(second_root, SECOND, False))
        one = self.task(a, status="human", auth=True, socketDir=str(self.socket))
        two = self.task(b, socketDir=str(self.socket))
        a.state["auth_task"] = "existing"
        a.store.save(a.state)
        self.assertNotEqual(a.project.profile, b.project.profile)
        self.assertNotEqual(a.store.directory, b.store.directory)
        self.assertNotEqual(one["session"], two["session"])
        self.assertEqual(sr.task_socket_directory(KEY, one), sr.task_socket_directory(SECOND, two))
        self.assertIsNone(b.store.load()["auth_task"])
        self.assertCode("auth_lease", a.guard, one)
        self.assertCode("auth_lease", a.guard)
        b.guard(two)
        a.state["auth_task"] = None
        one["auth"] = False
        self.assertCode("human_lease", a.guard, one)

    def test_migration_marker_blocks_normal_task_operations_before_ipc(self):
        self.write_policy()
        c = self.coordinator()
        task = self.task(c, socketDir=str(self.socket), controllerMigration={"status": "in_progress"})
        c.store.load()  # Valid reserved marker must not imply permission to act.
        with mock.patch.object(bs, "checked_process") as process, mock.patch.object(bs, "verify_browser") as browser:
            for function, args in ((c.guard, (task,)), (c.run, ("existing", ["reload"])),
                                   (c.agent, (BROWSER, task, ["reload"])),
                                   (c.handoff, ("existing", False, True)),
                                   (c.release, ("existing", "failure"))):
                self.assertCode("reconciliation_required", function, *args)
            process.assert_not_called()
            browser.assert_not_called()
        for marker in ({}, {"status": True}, {"status": "unknown"}, {"status": "completed", "extra": True}):
            self.assertCode("invalid_state", sr.validate_controller_migration, KEY, marker)
        sr.validate_controller_migration(KEY, {"status": "completed", "fromSocketDir": str(sr.legacy_socket_directory(KEY)),
                                               "toSocketDir": str(self.socket)})

    def test_viewer_and_inspect_report_actual_task_roots_and_task_ids(self):
        self.write_policy()
        c = self.coordinator()
        self.task(c, name="old-task", status="human")
        self.task(c, name="new-task", target="TARGET002", socketDir=str(self.socket))
        with mock.patch.object(c, "owned", return_value={"origin": "https://example.com"}), \
                mock.patch.object(c, "agent", side_effect=AssertionError("no stream start")):
            result = c.viewer("old-task", False)
        self.assertFalse(result["serviceStarted"])
        self.assertEqual(str(sr.legacy_socket_directory(KEY)), result["socketDirectory"])
        inspection = c.inspect()
        self.assertEqual(str(self.socket), inspection["configuredSocketDirectory"])
        rows = {row["taskId"]: row for row in inspection["tasks"]}
        self.assertEqual({"old-task", "new-task"}, set(rows))
        self.assertEqual(str(sr.legacy_socket_directory(KEY)), rows["old-task"]["socketDirectory"])
        self.assertEqual(str(self.socket), rows["new-task"]["socketDirectory"])

    def test_full_migration_journal_survives_store_reload_and_preserves_human_lease(self):
        self.write_policy()
        c = self.coordinator()
        journal = {"status": "in_progress", "phase": "select_original",
                   "fromSocketDir": str(sr.legacy_socket_directory(KEY)),
                   "toSocketDir": str(self.socket), "targetId": "TARGET001",
                   "temporaryTargetId": "TEMP0001"}
        self.task(c, status="human", auth=True, controllerMigration=journal)
        c.state["auth_task"] = "existing"
        c.store.save(c.state)
        reloaded = bs.Coordinator(c.project, c.store)
        self.assertEqual(journal, reloaded.task("existing")["controllerMigration"])
        self.assertTrue(reloaded.inspect()["authLease"])
        before = c.store.path.read_bytes()
        with mock.patch.object(bs, "checked_process") as process, \
                mock.patch.object(bs.shutil, "which") as which:
            for function, args in ((reloaded.run, ("existing", ["reload"])),
                                   (reloaded.resume, ("existing", True, True)),
                                   (reloaded.viewer, ("existing", False)),
                                   (reloaded.viewer, ("existing", True, 45678))):
                self.assertCode("reconciliation_required", function, *args)
            process.assert_not_called()
            which.assert_not_called()
        self.assertEqual(before, c.store.path.read_bytes())
        self.assertEqual("human", c.store.load()["tasks"]["existing"]["status"])
        self.assertEqual("existing", c.store.load()["auth_task"])

    def test_migration_schema_reload_rejects_unknown_fields_types_phases_and_targets(self):
        self.write_policy()
        c = self.coordinator()
        task = self.task(c)
        valid = {"status": "in_progress", "phase": "disconnect_source",
                 "fromSocketDir": str(sr.legacy_socket_directory(KEY)),
                 "toSocketDir": str(self.socket), "targetId": "TARGET001"}
        for phase in ("disconnect_source", "attach_destination", "select_original",
                      "close_temporary_blank", "uncertain"):
            task["controllerMigration"] = {**valid, "phase": phase}
            c.store.save(c.state)
            self.assertEqual(phase, c.store.load()["tasks"]["existing"]["controllerMigration"]["phase"])
        invalid = [None, [], {}, {**valid, "unknown": True}]
        for field, values in (("status", (True, 1, None, [], "inprogress")),
                              ("phase", (True, 1, None, [], "launch_browser")),
                              ("targetId", (True, 1, None, [], "short", "x" * 129, "bad/path")),
                              ("temporaryTargetId", (False, "short", "x" * 129)),
                              ("fromSocketDir", (None, 1)), ("toSocketDir", (False, []))):
            invalid.extend({**valid, field: value} for value in values)
        for value in invalid:
            with self.subTest(marker=value):
                task["controllerMigration"] = value
                c.store.save(c.state)
                self.assertCode("invalid_state", c.store.load)
        task["controllerMigration"] = {**valid, "status": "completed"}
        c.store.save(c.state)
        bs.controller_migration_guard(c.store.load()["tasks"]["existing"])

    def test_remote_dashboard_accepts_owned_0755_ab_parent_but_requires_private_root(self):
        self.write_policy()
        self.socket.parent.mkdir(mode=0o755)
        self.socket.parent.chmod(0o755)
        self.socket.mkdir(mode=0o700)
        loaded = {"bundle": self.home / "bundle", "manifest": {}, "plan": {
            "projects": [{"id": "alpha", "socketDir": str(self.socket),
                          "dashboardPort": 9223, "origin": "https://alpha.private.test"}],
            "executables": {"agentBrowser": "/fake/agent-browser"}}}
        with mock.patch.object(remote, "load_bundle", return_value=loaded), \
                mock.patch.object(remote.sys, "platform", "darwin"), \
                mock.patch.object(remote, "clean_environment", return_value={"HOME": str(self.home)}), \
                mock.patch.object(remote, "reviewed_runtime_path", return_value="/fake"), \
                mock.patch.object(remote, "parse_dashboard_response", return_value={
                    "pid": 123, "port": 9223, "url": "https://alpha.private.test/"}), \
                mock.patch.object(remote, "listener_ready", return_value=True), \
                mock.patch.object(remote, "private_write") as write, \
                mock.patch.object(remote.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, "fake response", "")) as process:
            self.assertTrue(remote.start_dashboard(self.home / "bundle", "alpha", allow_start=True)["ready"])
            self.assertEqual(str(self.socket), process.call_args.kwargs["env"]["AGENT_BROWSER_SOCKET_DIR"])
            self.assertEqual(0o755, stat.S_IMODE(self.socket.parent.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(self.socket.stat().st_mode))
            write.assert_called_once()
            process.reset_mock()
            for parent_mode, root_mode in ((0o775, 0o700), (0o755, 0o755)):
                self.socket.parent.chmod(parent_mode)
                self.socket.chmod(root_mode)
                with self.assertRaises(remote.RemoteAccessError) as caught:
                    remote.start_dashboard(self.home / "bundle", "alpha", allow_start=True)
                self.assertEqual("unsafe_path", caught.exception.code)
                process.assert_not_called()

    def test_malformed_policy_fails_closed_without_secret_stdout(self):
        for raw in ('{"TOP_SECRET":', 'TOP_SECRET', '\ufffdTOP_SECRET'):
            self.write_policy(raw=raw)
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                self.assertCode("invalid_state", sr.read_policy)
                self.assertCode("invalid_state", sr.project_socket_directory, KEY)
                self.assertCode("invalid_state", bs.child_environment, KEY)
                rc = sr.main(["plan", "--socket-dir", str(self.socket), "--project", "TOP_SECRET"])
            self.assertEqual(1, rc)
            text = stream.getvalue()
            self.assertNotIn("TOP_SECRET", text)
            self.assertNotIn("Traceback", text)
            self.assertFalse(json.loads(text)["success"])


if __name__ == "__main__":
    unittest.main()
