"""Offline migration contracts: real isolated journals, fake controller/CDP boundaries."""
import copy
import importlib.util
import json
import os
from pathlib import Path
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


core = load("migration_test_core", "browser_session.py")
runtime = load("migration_test_runtime", "shared_runtime.py")
migration = load("migration_test_helper", "migrate_controller.py")
KEY = "a" * 64
ORIGINAL = "ORIGINAL001"
OTHER = "OTHER000001"
BLANK = "BLANK000001"
SECRET = "SYNTHETIC_SECRET_DO_NOT_EXPOSE"
BROWSER = {"pid": 123, "started": "fixed", "executable": "/fake/Chrome",
           "port": 12345, "endpoint_hash": "fixed-hash", "headed": False}
APPROVAL = dict(allow_migrate=True, human_paused=True, captures_stopped=True)


class ControllerMigrationTests(unittest.TestCase):
    def patch(self, obj, name, *args, **kwargs):
        patcher = mock.patch.object(obj, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def setUp(self):
        # Resolve /tmp on macOS: socket roots must be canonical and <=52 bytes.
        temporary = tempfile.TemporaryDirectory(prefix="", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.patch(Path, "home", return_value=self.home)
        modules = mock.patch.dict(sys.modules, {"_browser_automation_shared_runtime": runtime})
        modules.start()
        self.addCleanup(modules.stop)
        self.patch(migration, "_modules", return_value=(core, runtime))
        self.patch(migration.sys, "platform", "darwin")
        self.patch(migration, "qualification_verified", lambda: True)
        self.process = self.patch(core, "checked_process", side_effect=AssertionError("no processes"))
        self.patch(core.subprocess, "Popen", side_effect=AssertionError("no services"))
        self.patch(core, "discover_processes", side_effect=AssertionError("no discovery"))
        self.patch(core.urllib.request, "urlopen", side_effect=AssertionError("no network"))
        self.project = core.Project(self.home / "project", KEY, False)
        self.project.root.mkdir(mode=0o700)
        self.old = runtime.legacy_socket_directory(KEY)
        self.new = self.home / ".ab" / "shared"
        self.old.parent.mkdir(mode=0o700)
        self.old.mkdir(mode=0o700)
        path = runtime.policy_path()
        path.parent.mkdir(mode=0o700, parents=True)
        path.write_text(json.dumps({"version": 1, "projects": [KEY],
                                    "socketDirectory": str(self.new)}))
        path.chmod(0o600)
        self.store = core.StateStore(self.home / "state", self.project)
        self.task = {"session": core.session_name(KEY, "login"), "status": "human",
                     "targetId": ORIGINAL, "origin": "https://example.test",
                     "auth": True, "operation": {"kind": "checkpoint"}}
        self.state = {"version": 1, "project": KEY, "browser": core.browser_identity(BROWSER),
                      "auth_task": "login", "tasks": {"login": self.task}}
        self.store.save(self.state)
        self.targets = {ORIGINAL, OTHER}
        self.selected = ORIGINAL
        self.pages = None
        self.attach_added = {BLANK}
        self.attach_selected = BLANK
        self.attach_url = "about:blank"
        self.fail_command = None
        self.after_command = None
        self.events = []
        self.coordinator = types.SimpleNamespace(
            project=self.project, store=self.store, state=self.state,
            browser=mock.Mock(side_effect=lambda: copy.deepcopy(BROWSER)),
            owned=mock.Mock(side_effect=self.owned), agent=mock.Mock(side_effect=self.agent))
        self.profile = self.patch(core, "check_profile", return_value=None)
        self.cdp = self.patch(core, "cdp_target_ids", side_effect=lambda browser: set(self.targets))
        self.json = self.patch(core, "cdp_json", side_effect=self.cdp_json)
        # Source ownership is an independent boundary; never create a real daemon/socket.
        self.sidecars = self.patch(migration, "_source_sidecars", return_value=None)

    def owned(self, browser, task):
        self.events.append(("owned", task["socketDir"], self.selected))
        if self.selected != task["targetId"]:
            core.fail("ownership_unverified")
        return {"targetId": self.selected, "active": True, "origin": "https://example.test"}

    def cdp_json(self, port, route):
        self.assertEqual((BROWSER["port"], "/json/list"), (port, route))
        return self.pages if self.pages is not None else [
            {"id": target, "type": "page", "url": self.attach_url if target == BLANK
             else "https://example.test"} for target in self.targets]

    def agent(self, browser, task, args):
        self.assertNotIn("controllerMigration", task)
        self.assertEqual(core.browser_identity(BROWSER), core.browser_identity(browser))
        command = tuple(args)
        self.events.append(("agent", task["socketDir"], command))
        durable = self.store.load()["tasks"]["login"]["controllerMigration"]
        phases = {("close",): "disconnect_source", ("tab", "list"): "attach_destination",
                  ("tab", ORIGINAL): "select_original",
                  ("tab", "close", BLANK): "close_temporary_blank"}
        self.assertEqual(phases[command], durable["phase"])
        if command == self.fail_command:
            raise RuntimeError(SECRET)
        if command == ("close",):
            self.assertEqual(str(self.old), task["socketDir"])
        elif command == ("tab", "list"):
            self.assertEqual(str(self.new), task["socketDir"])
            self.targets.update(self.attach_added)
            self.selected = self.attach_selected
        elif command == ("tab", ORIGINAL):
            self.selected = ORIGINAL
        elif command == ("tab", "close", BLANK):
            self.assertEqual(ORIGINAL, self.selected)
            self.targets.remove(BLANK)
        else:
            self.fail("Unexpected command (capture, resume, global close, or retake)")
        if self.after_command:
            self.after_command(command)
        return [{"targetId": target, "url": self.attach_url if target == BLANK
                 else "https://example.test", "active": target == self.selected}
                for target in sorted(self.targets)]

    def save(self):
        self.store.save(self.state)

    def migrate(self, **kwargs):
        return migration.migrate(self.coordinator, "login", **(APPROVAL | kwargs))

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(migration.MigrationError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)
        self.assertNotIn(SECRET, str(caught.exception))
        return caught.exception

    def assertUncertain(self):
        # Use a newly constructed real StateStore, not cached in-memory fixture state.
        with core.StateStore(self.home / "state", self.project).lock():
            reloaded = core.StateStore(self.home / "state", self.project).load()
        task = reloaded["tasks"]["login"]
        marker = task.pop("controllerMigration")
        self.assertEqual("in_progress", marker["status"])
        self.assertEqual("uncertain", marker["phase"])
        self.assertEqual(str(self.old), marker["fromSocketDir"])
        self.assertEqual(str(self.new), marker["toSocketDir"])
        self.assertEqual(self.task, task)
        self.assertEqual("login", reloaded["auth_task"])
        return reloaded, marker

    def test_plan_is_read_only_and_exposes_qualification_blocker(self):
        before = self.store.path.read_bytes()
        with mock.patch.object(migration, "qualification_verified", return_value=False):
            result = migration.plan_migration(self.coordinator, "login")
        self.assertEqual("blocked", result["status"])
        self.assertEqual(["qualification_required"], result["blockers"])
        self.assertFalse(result["createsServices"])
        self.assertTrue(result["requiresHumanResume"])
        self.assertEqual(before, self.store.path.read_bytes())
        self.coordinator.agent.assert_not_called()
        self.coordinator.owned.assert_not_called()
        self.coordinator.browser.assert_not_called()
        self.cdp.assert_not_called()
        self.sidecars.assert_not_called()

    def test_each_approval_must_be_explicit_true_before_any_probe(self):
        for flag in APPROVAL:
            for value in (False, None, 1, "yes"):
                with self.subTest(flag=flag, value=value):
                    self.assertCode("confirmation_required", self.migrate, **{flag: value})
        self.assertCode("confirmation_required", migration.migrate, self.coordinator, "login")
        self.coordinator.agent.assert_not_called()
        self.coordinator.browser.assert_not_called()

    def test_unqualified_execution_blocks_before_agent(self):
        with mock.patch.object(migration, "qualification_verified", return_value=False):
            self.assertCode("qualification_required", self.migrate)
        self.coordinator.agent.assert_not_called()

    def test_only_human_or_paused_checkpoints(self):
        for status in ("active", "attaching", "uncertain", "releasing", "released"):
            with self.subTest(status=status):
                self.task["status"] = status
                self.state["auth_task"] = None
                self.save()
                self.assertCode("task_checkpoint_required", self.migrate)
        self.coordinator.agent.assert_not_called()

    def test_other_active_or_unsettled_project_tasks_block(self):
        for status in ("active", "attaching", "uncertain", "releasing"):
            with self.subTest(status=status):
                self.state["tasks"]["other"] = {"session": core.session_name(KEY, "other"),
                    "status": status, "targetId": OTHER, "auth": False}
                self.save()
                self.assertCode("project_unsettled", self.migrate)
        self.state["tasks"]["other"].update(status="paused", controllerMigration={"status": "in_progress"})
        self.save()
        self.assertCode("project_unsettled", self.migrate)
        self.coordinator.agent.assert_not_called()

    def test_existing_marker_blocks_even_if_marked_completed(self):
        for status in ("in_progress", "completed"):
            self.task["controllerMigration"] = {"status": status}
            self.save()
            self.assertCode("migration_pending", self.migrate)
        self.coordinator.agent.assert_not_called()

    def test_destination_sidecar_collisions_are_never_adopted(self):
        self.new.mkdir(mode=0o700)
        for suffix in (".pid", ".sock", ".target", ".stream"):
            with self.subTest(suffix=suffix):
                path = self.new / (self.task["session"] + suffix)
                path.symlink_to(self.home / "missing")
                self.assertCode("destination_occupied", self.migrate)
                self.assertTrue(path.is_symlink())
                path.unlink()
        self.coordinator.agent.assert_not_called()

    def test_verified_external_browser_success_preserves_checkpoint_and_every_target(self):
        original = copy.deepcopy(self.task)
        result = self.migrate()
        self.assertEqual("migration_complete", result["status"])
        self.assertTrue(result["requiresHumanResume"])
        self.assertFalse(result["authenticationVerified"])
        state = self.store.load()
        self.assertEqual(original | {"socketDir": str(self.new)}, state["tasks"]["login"])
        self.assertEqual("login", state["auth_task"])
        self.assertEqual(core.browser_identity(BROWSER), state["browser"])
        self.assertEqual({ORIGINAL, OTHER}, self.targets)
        self.assertEqual(ORIGINAL, self.selected)
        self.assertEqual([
            ("agent", str(self.old), ("close",)),
            ("agent", str(self.new), ("tab", "list")),
            ("agent", str(self.new), ("tab", ORIGINAL)),
            ("agent", str(self.new), ("tab", "close", BLANK))],
            [event for event in self.events if event[0] == "agent"])
        self.sidecars.assert_called_once_with(self.old, original["session"])
        self.profile.assert_called_with(self.project)
        self.process.assert_not_called()

    def test_real_agent_transport_uses_exact_root_external_cdp_and_pinning(self):
        coordinator = core.Coordinator(self.project, self.store)
        self.process.side_effect = None
        self.process.return_value = types.SimpleNamespace(
            stdout=json.dumps({"success": True, "data": {"ok": True}}), returncode=0)
        with mock.patch.object(core.shutil, "which", return_value="/fake/agent-browser"), \
                mock.patch.object(core, "child_environment", return_value={}):
            for root, command in ((self.old, ["close"]), (self.new, ["tab", "list"])):
                with self.subTest(command=command):
                    shadow = migration._shadow(self.task, root)
                    coordinator.agent(copy.deepcopy(BROWSER), shadow, command)
                    args, kwargs = self.process.call_args
                    argv = args[0]
                    self.assertEqual(["/fake/agent-browser", "--config",
                                      str(self.store.empty_config()), "--session",
                                      self.task["session"], "--cdp", str(BROWSER["port"]),
                                      "--pin-tab", "--json", *command], argv)
                    self.assertEqual({"AGENT_BROWSER_SOCKET_DIR": str(root)}, kwargs["env"])
                    self.assertEqual(self.store.directory, kwargs["cwd"])
                    self.assertNotIn("--profile", argv)
                    self.assertNotIn("--all", argv)

    def test_paused_checkpoint_and_false_auth_are_preserved(self):
        self.task.update(status="paused", auth=False)
        self.state["auth_task"] = None
        self.save()
        self.migrate()
        state = self.store.load()
        self.assertEqual("paused", state["tasks"]["login"]["status"])
        self.assertIs(False, state["tasks"]["login"]["auth"])
        self.assertIsNone(state["auth_task"])

    def test_each_command_failure_leaves_reloadable_uncertain_intent_without_secret(self):
        commands = [("close",), ("tab", "list"), ("tab", ORIGINAL), ("tab", "close", BLANK)]
        for index, command in enumerate(commands):
            with self.subTest(command=command):
                self.targets = {ORIGINAL, OTHER}
                self.selected = ORIGINAL
                self.events.clear()
                self.save()
                self.fail_command = command
                self.assertCode("migration_uncertain", self.migrate)
                _, marker = self.assertUncertain()
                self.assertNotIn(SECRET, self.store.path.read_text())
                self.assertEqual(commands[:index + 1], [event[2] for event in self.events if event[0] == "agent"])
                if index >= 2:
                    self.assertEqual(BLANK, marker["temporaryTargetId"])

    def test_browser_identity_loss_after_disconnect_never_retakes_or_attaches(self):
        def disappear(command):
            if command == ("close",):
                self.coordinator.browser.side_effect = lambda: BROWSER | {"pid": 999}
        self.after_command = disappear
        self.assertCode("migration_uncertain", self.migrate)
        self.assertUncertain()
        self.assertEqual([("close",)], [event[2] for event in self.events if event[0] == "agent"])

    def test_preexisting_target_loss_after_disconnect_never_retakes(self):
        self.after_command = lambda command: self.targets.discard(OTHER)
        self.assertCode("migration_uncertain", self.migrate)
        self.assertUncertain()
        self.assertEqual([("close",)], [event[2] for event in self.events if event[0] == "agent"])

    def test_initial_identity_or_original_target_loss_fails_before_disconnect(self):
        self.coordinator.browser.side_effect = lambda: BROWSER | {"pid": 999}
        self.assertCode("browser_unverified", self.migrate)
        self.coordinator.browser.side_effect = lambda: copy.deepcopy(BROWSER)
        self.targets.remove(ORIGINAL)
        self.assertCode("source_unverified", self.migrate)
        self.coordinator.agent.assert_not_called()
        self.assertNotIn("controllerMigration", self.store.load()["tasks"]["login"])

    def test_unknown_multiple_or_wrong_selected_fresh_pages_are_not_closed(self):
        variants = [({BLANK, "UNKNOWN0001"}, BLANK, "about:blank"),
                    (set(), ORIGINAL, "about:blank"),
                    ({BLANK}, ORIGINAL, "about:blank"),
                    ({BLANK}, BLANK, "data:text/plain,opaque"),
                    ({BLANK}, BLANK, "about:blank#fragment")]
        for added, selected, url in variants:
            with self.subTest(added=added, selected=selected, url=url):
                self.targets = {ORIGINAL, OTHER}
                self.selected = ORIGINAL
                self.attach_added, self.attach_selected, self.attach_url = added, selected, url
                self.events.clear()
                self.save()
                self.assertCode("migration_uncertain", self.migrate)
                self.assertUncertain()
                self.assertEqual([("close",), ("tab", "list")],
                                 [event[2] for event in self.events if event[0] == "agent"])
                self.assertTrue({ORIGINAL, OTHER} <= self.targets)

    def test_literal_blank_requires_exactly_one_matching_page_record(self):
        for pages in ([], [{"id": BLANK, "type": "iframe", "url": "about:blank"}],
                      [{"id": BLANK, "type": "page", "url": "about:blank"}] * 2):
            with self.subTest(pages=pages):
                self.targets = {ORIGINAL, OTHER}
                self.selected = ORIGINAL
                self.pages = pages
                self.events.clear()
                self.save()
                self.assertCode("migration_uncertain", self.migrate)
                self.assertUncertain()
                self.assertFalse(any(event[2][:2] == ("tab", "close")
                                     for event in self.events if event[0] == "agent"))

    def test_marker_round_trip_and_real_coordinator_operations_fail_closed(self):
        self.fail_command = ("tab", ORIGINAL)
        self.assertCode("migration_uncertain", self.migrate)
        self.assertUncertain()
        # Exercise actual guards/agent code, not the minimal migration coordinator.
        coordinator = core.Coordinator(self.project, self.store)
        with mock.patch.object(coordinator, "browser", return_value=copy.deepcopy(BROWSER)), \
                mock.patch.object(core.shutil, "which", side_effect=AssertionError("no CLI lookup")):
            for name, args in (("run", ("login", ["snapshot"])),
                               ("resume", ("login", True, True)),
                               ("viewer", ("login", True, 12346))):
                with self.subTest(operation=name):
                    with self.assertRaises(core.BrowserSessionError) as caught:
                        getattr(coordinator, name)(*args)
                    self.assertEqual("reconciliation_required", caught.exception.code)
        self.process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
