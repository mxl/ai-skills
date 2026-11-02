"""Offline contract tests. Never launch Chrome or an agent-browser daemon.

Only GitIdentityTests may invoke a real subprocess: disposable local git commands.
All other process, browser discovery, and CDP boundaries are explicitly mocked.
"""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "browser_session_under_test", Path(__file__).resolve().parents[1] / "scripts" / "browser_session.py"
)
bs = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bs
_previous_bytecode_setting = sys.dont_write_bytecode
try:
    sys.dont_write_bytecode = True
    SPEC.loader.exec_module(bs)
finally:
    sys.dont_write_bytecode = _previous_bytecode_setting


class ErrorAssertions:
    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(bs.BrowserSessionError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)
        return caught.exception


class PureTests(ErrorAssertions, unittest.TestCase):
    def test_session_names_are_stable_scoped_and_do_not_embed_task(self):
        name = bs.session_name("a" * 64, "sensitive-task")
        self.assertEqual(name, bs.session_name("a" * 64, "sensitive-task"))
        self.assertNotEqual(name, bs.session_name("b" * 64, "sensitive-task"))
        self.assertNotEqual(name, bs.session_name("a" * 64, "other"))
        self.assertNotIn("sensitive-task", name)
        self.assertRegex(name, r"^ba-[a-f0-9]{16}-[a-f0-9]{24}$")

    def test_invalid_task_ids(self):
        for task in ("", "a" * 129, "../x", "a b", "тест", "x\n", "--session=x/y"):
            with self.subTest(task=task):
                self.assertCode("invalid_task", bs.session_name, "a" * 64, task)
        bs.session_name("a" * 64, "A_0.x-1")

    def test_safe_urls(self):
        for url in ("https://example.com/path", "http://localhost:3000/", "http://127.0.0.1:1/", "http://[::1]:65535/", "https://example.com/%20"):
            with self.subTest(url=url):
                self.assertEqual(url, bs.validate_url(url))

    def test_url_rejections(self):
        for url in ("http://example.com", "file:///tmp/x", "javascript:alert(1)", "https://user:secret@example.com", "https://example.com?", "https://example.com#", "https://example.com/?token=secret", "https://example.com/a b", "https://example.com\\x", "https://example.com:0", "https://example.com:65536", "https://[bad", "https://example.com:\n443", "http://localhost.evil.test"):
            with self.subTest(url=url):
                self.assertCode("invalid_url", bs.validate_url, url)

    def test_origin_removes_userinfo_path_query_fragment(self):
        self.assertEqual("https://example.com:8443", bs.origin("https://user:secret@example.com:8443/private?token=x#secret"))
        self.assertEqual("http://[::1]:3000", bs.origin("http://[::1]:3000/private"))
        for value in (None, "file:///secret", "https://[bad", "https://host:invalid", "data:secret"):
            self.assertEqual("opaque", bs.origin(value))

    def test_child_environment_strips_configuration_and_proxies_case_insensitively(self):
        dirty = {"PATH": "/safe/bin", "HOME": "/home/test", "AGENT_BROWSER_SESSION": "other", "agent_browser_config": "secret", "BW_SESSION": "secret", "BWS_ACCESS_TOKEN": "secret", "AAC_TOKEN": "secret", "BROWSER_USE_API_KEY": "secret", "AI_GATEWAY_KEY": "secret", "HTTPS_PROXY": "secret", "http_proxy": "secret", "ALL_PROXY": "secret", "NO_PROXY": "secret"}
        with mock.patch.dict(os.environ, dirty, clear=True):
            clean = bs.child_environment("project")
        self.assertEqual({"PATH": "/safe/bin", "HOME": "/home/test", "AGENT_BROWSER_NAMESPACE": "ba-project"}, clean)
        self.assertEqual("other", dirty["AGENT_BROWSER_SESSION"])

    def test_allowed_commands(self):
        for command in (["back"], ["forward"], ["reload"], ["open", "https://example.com"], ["snapshot", "-i", "--compact"], ["click", "@e12"], ["fill", "@e1", "value with spaces"], ["press", "Enter"], ["scroll", "down", "10000"], ["wait", "0"], ["get", "text", "body"], ["is", "visible", "@e1"]):
            with self.subTest(command=command):
                self.assertEqual(command, bs.validate_run_command(command))

    def test_commands_cannot_override_session_profile_endpoint_or_export(self):
        for command in ([], ["--session", "victim", "reload"], ["reload", "--cdp", "9222"], ["open", "https://example.com", "--profile", "/tmp/victim"], ["tab", "close"], ["get", "value", "@e1"], ["get", "value", "body"], ["eval", "1"], ["screenshot", "/tmp/out"], ["stream", "enable"], ["snapshot", "--session=other"], ["fill", "@e1", "--config=evil"], ["click", "body"], ["press", "Control+l"], ["scroll", "down", "10001"], ["wait", "10001"], ["fill", "@e1", "x\x00y"], ["snapshot"] * 17):
            with self.subTest(command=command):
                self.assertCode("command_denied", bs.validate_run_command, command)

    def test_tabs_supported_envelopes_redact_urls(self):
        rows = [{"targetId": "TARGET001", "url": "https://user:secret@example.com/path?token=x", "active": True}, {"targetId": "TARGET002", "url": "about:blank", "selected": False}]
        expected = [{"targetId": "TARGET001", "origin": "https://example.com", "active": True}, {"targetId": "TARGET002", "origin": "opaque", "active": False}]
        for payload in (rows, {"tabs": rows}, {"success": True, "data": {"tabs": rows}}, {"success": True, "data": rows}):
            self.assertEqual(expected, bs.parse_tabs(payload))

    def test_unknown_tab_responses_fail_closed(self):
        for payload in (None, [], {}, {"success": False, "data": []}, [{"id": "TARGET001", "url": "https://example.com", "active": True}], [{"targetId": "short", "url": "https://example.com", "active": True}]):
            with self.subTest(payload=payload):
                self.assertCode("invalid_json", bs.parse_tabs, payload)
        for selection in ({}, {"active": 1}, {"active": True, "selected": False}, {"active": False}):
            self.assertCode("ownership_unverified", bs.parse_tabs, [{"targetId": "TARGET001", "url": "about:blank", **selection}])
        row = {"targetId": "TARGET001", "url": "about:blank", "active": True}
        self.assertCode("invalid_json", bs.parse_tabs, [row, row])
        self.assertCode("ownership_unverified", bs.parse_tabs, [row, {**row, "targetId": "TARGET002"}])


@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitIdentityTests(ErrorAssertions, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "main checkout"
        self.git("init", "-q", str(self.repo), cwd=self.root)

    def git(self, *args, cwd=None):
        # No user config, hooks, network, or repository are used.
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.run([shutil.which("git"), "-c", "core.hooksPath=" + os.devnull, *args], cwd=cwd or self.repo, env=env, capture_output=True, text=True, check=True, timeout=15)

    def test_worktree_and_subdirectories_share_identity_but_clone_does_not(self):
        tree = self.root / "linked worktree"
        self.git("worktree", "add", "--orphan", "-q", "-b", "test-branch", str(tree))
        sub = tree / "sub"
        sub.mkdir()
        canonical = bs.resolve_project(self.repo)
        self.assertEqual(canonical, bs.resolve_project(cwd=sub))
        self.assertEqual(self.repo, canonical.root)
        clone = self.root / "clone"
        self.git("clone", "-q", str(self.repo), str(clone))
        self.assertNotEqual(canonical.key, bs.resolve_project(clone).key)

    def test_explicit_non_git_root_only(self):
        other = self.root / "not git"
        other.mkdir()
        project = bs.resolve_project(other)
        self.assertEqual(other, project.root)
        self.assertFalse(project.is_git)
        self.assertCode("invalid_project", bs.resolve_project, cwd=other)
        self.assertCode("invalid_project", bs.resolve_project, self.root / "missing")
        self.assertCode("invalid_project", bs.resolve_project, self.repo / ".git" / "HEAD")

    def test_profile_requires_ignore_and_rejects_tracked_data(self):
        project = bs.resolve_project(self.repo)
        self.assertCode("onboarding_required", bs.check_profile, project)
        (self.repo / ".gitignore").write_text("/.chrome-user-data/\n")
        bs.check_profile(project)
        project.profile.mkdir()
        (project.profile / "Cookies").write_text("not-real-cookies")
        self.git("add", "-f", ".chrome-user-data/Cookies")
        self.assertCode("onboarding_required", bs.check_profile, project)

    def test_selective_probe_ignore_is_not_a_profile_ignore(self):
        # Regression: ignoring only the known probe leaves Cookies and other
        # browser data eligible for accidental git add. Do not bless this state.
        project = bs.resolve_project(self.repo)
        (self.repo / ".gitignore").write_text("/.chrome-user-data/browser-helper-ignore-probe\n")
        self.assertCode("onboarding_required", bs.check_profile, project)

    def test_bare_repository_rejected(self):
        bare = self.root / "bare"
        self.git("init", "--bare", "-q", str(bare))
        self.assertCode("invalid_project", bs.resolve_project, bare)


class StorageTests(ErrorAssertions, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = bs.Project(self.root, "a" * 64, False)
        self.base = self.root / "private-state"

    def store(self):
        return bs.StateStore(self.base, self.project)

    def test_profile_legacy_symlinks_and_types(self):
        legacy = self.root / ".agent-browser-profile"
        legacy.mkdir()
        bs.check_profile(self.project)
        (legacy / "data").write_text("dummy")
        self.assertCode("migration_required", bs.check_profile, self.project)
        (legacy / "data").unlink()
        legacy.rmdir()
        legacy.symlink_to(self.root / "missing")
        self.assertCode("migration_required", bs.check_profile, self.project)
        legacy.unlink()
        self.project.profile.symlink_to(self.root / "missing")
        self.assertCode("unsafe_path", bs.check_profile, self.project)
        self.project.profile.unlink()
        self.project.profile.write_text("not directory")
        self.assertCode("unsafe_path", bs.check_profile, self.project)

    def test_owner_only_state_lock_config_and_roundtrip(self):
        store = self.store()
        with store.lock():
            initial = store.load()
            store.save(initial)
            self.assertEqual(initial, store.load())
            config = store.empty_config()
            self.assertEqual({}, json.loads(config.read_text()))
        for path in (self.base, store.directory):
            self.assertEqual(0o700, stat.S_IMODE(path.stat().st_mode))
        for path in (store.path, store.directory / "lock", config):
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_state_parent_permissions_and_symlinks_rejected(self):
        self.base.mkdir(mode=0o755)
        self.base.chmod(0o755)
        self.assertCode("unsafe_path", self.store)
        self.base.rmdir()
        self.base.symlink_to(self.root / "missing")
        self.assertCode("unsafe_path", self.store)

    def test_state_file_permissions_symlinks_and_hardlinks_rejected(self):
        store = self.store()
        store.save(store.load())
        store.path.chmod(0o644)
        self.assertCode("unsafe_path", store.load)
        store.path.unlink()
        outside = self.root / "outside"
        outside.write_text("private sentinel")
        outside.chmod(0o600)
        store.path.symlink_to(outside)
        self.assertCode("unsafe_path", store.load)
        self.assertCode("unsafe_path", store.save, {})
        self.assertEqual("private sentinel", outside.read_text())
        store.path.unlink()
        os.link(outside, store.path)
        self.assertCode("unsafe_path", store.load)

    def test_lock_uses_flock_and_does_not_follow_symlink(self):
        store = self.store()
        with mock.patch.object(bs.fcntl, "flock", wraps=bs.fcntl.flock) as flock:
            with store.lock():
                self.assertEqual(bs.fcntl.LOCK_EX, flock.call_args.args[1])
        (store.directory / "lock").unlink()
        (store.directory / "lock").symlink_to(self.root / "missing")
        self.assertCode("unsafe_path", lambda: store.lock().__enter__())

    def test_atomic_failure_preserves_prior_json_and_removes_temporary_file(self):
        store = self.store()
        before = store.load()
        store.save(before)
        with mock.patch.object(bs.os, "replace", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                store.save({**before, "launch_pending": True})
        self.assertEqual(before, store.load())
        self.assertEqual([], list(store.directory.glob(".state-*")))

    def test_save_fsyncs_before_replace_and_syncs_directory_after(self):
        store = self.store()
        order = []
        real_replace = os.replace
        with mock.patch.object(bs.os, "fsync", side_effect=lambda fd: order.append("sync")), mock.patch.object(bs.os, "replace", side_effect=lambda src, dst: (order.append("replace"), real_replace(src, dst))[-1]):
            store.save(store.load())
        self.assertEqual(["sync", "replace", "sync"], order)

    def test_invalid_json_identity_task_and_auth_lease_rejected(self):
        store = self.store()
        blank = store.load()
        variants = [[], {**blank, "version": 2}, {**blank, "project": "wrong"}, {**blank, "tasks": []}, {**blank, "auth_task": "missing"}, {**blank, "tasks": {"task": {"session": "wrong", "status": "active"}}}]
        for value in variants:
            with self.subTest(value=value):
                store.save(value)
                self.assertCode("invalid_state", store.load)
        store.path.write_text("{broken")
        self.assertCode("invalid_state", store.load)

    def test_empty_config_tampering_rejected(self):
        store = self.store()
        path = store.empty_config()
        path.write_text('{"plugins":["unsafe"]}')
        self.assertCode("invalid_state", store.empty_config)


BROWSER = {"pid": 123, "started": "fixed-time", "executable": "/fake/Google Chrome", "port": 12345, "endpoint_hash": "hash", "headed": False}
TARGET = "OWNED0001"
OTHER = "OTHER0001"


class CoordinatorTests(ErrorAssertions, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = bs.Project(self.root, "a" * 64, False)
        self.store = bs.StateStore(self.root / "state", self.project)
        self.coordinator = bs.Coordinator(self.project, self.store)
        self.c = self.coordinator
        self.verify = self.patch("verify_browser", return_value=copy.deepcopy(BROWSER))
        self.targets = self.patch("cdp_target_ids", return_value={TARGET, OTHER})
        self.discover = self.patch("discover_processes", return_value=[])
        self.process = self.patch("checked_process", side_effect=AssertionError("unexpected subprocess"))
        popen_patch = mock.patch.object(bs.subprocess, "Popen", side_effect=AssertionError("real launch forbidden"))
        self.popen = popen_patch.start()
        self.addCleanup(popen_patch.stop)
        agent_patch = mock.patch.object(self.c, "agent", side_effect=self.agent_response)
        self.agent = agent_patch.start()
        self.addCleanup(agent_patch.stop)
        self.add_task("task")

    def patch(self, name, **kwargs):
        patcher = mock.patch.object(bs, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def add_task(self, name, target=TARGET, status="active", auth=False):
        task = {"session": bs.session_name(self.project.key, name), "status": status, "targetId": target, "origin": "https://example.com", "auth": auth}
        self.c.state["tasks"][name] = task
        self.c.state["browser"] = bs.browser_identity(BROWSER)
        self.store.save(self.c.state)
        return task

    def agent_response(self, browser, task, args):
        if args == ["tab", "list"]:
            return [{"targetId": task["targetId"] or TARGET, "url": "https://example.com/private?secret=value", "active": True}]
        return {"success": True, "data": {"ok": True}}

    def test_run_journals_uncertainty_before_command_and_revalidates_binding(self):
        observed = []
        def respond(browser, task, args):
            if args == ["reload"]:
                observed.append(self.store.load()["tasks"]["task"]["status"])
            return self.agent_response(browser, task, args)
        self.agent.side_effect = respond
        self.c.run("task", ["reload"])
        self.assertEqual(["uncertain"], observed)
        self.assertEqual("active", self.store.load()["tasks"]["task"]["status"])
        self.assertEqual(2, self.verify.call_count)
        self.assertEqual([["tab", "list"], ["reload"], ["tab", "list"]], [call.args[2] for call in self.agent.call_args_list])
        self.assertNotIn("secret", json.dumps(self.store.load()))

    def test_run_failure_preserves_uncertain_state_and_blocks_replay(self):
        def respond(browser, task, args):
            if args == ["reload"]:
                raise bs.BrowserSessionError("command_timeout")
            return self.agent_response(browser, task, args)
        self.agent.side_effect = respond
        self.assertCode("command_timeout", self.c.run, "task", ["reload"])
        self.assertEqual("uncertain", self.store.load()["tasks"]["task"]["status"])
        self.agent.reset_mock()
        self.assertCode("task_paused", self.c.run, "task", ["reload"])
        self.agent.assert_not_called()

    def test_missing_target_never_adopts_other_tab(self):
        self.targets.return_value = {OTHER}
        self.assertCode("tab_gone", self.c.run, "task", ["reload"])
        self.agent.assert_not_called()
        self.assertEqual(TARGET, self.c.task("task")["targetId"])

    def test_inactive_owned_target_never_selected_or_retargeted(self):
        self.agent.return_value = None
        self.agent.side_effect = lambda *args: [{"targetId": TARGET, "url": "about:blank", "active": False}, {"targetId": OTHER, "url": "about:blank", "active": True}]
        self.assertCode("ownership_unverified", self.c.run, "task", ["reload"])
        self.assertEqual([["tab", "list"]], [call.args[2] for call in self.agent.call_args_list])

    def test_stale_browser_does_not_rebind(self):
        self.verify.side_effect = bs.BrowserSessionError("stale_browser")
        self.assertCode("stale_browser", self.c.run, "task", ["reload"])
        self.agent.assert_not_called()
        self.verify.assert_called_once_with(self.project, bs.browser_identity(BROWSER))

    def test_auth_handoff_blocks_other_tasks_and_remains_paused_until_resume(self):
        self.add_task("other", OTHER)
        result = self.c.handoff("task", True, True)
        self.assertTrue(result["authLease"])
        self.assertEqual("human", self.store.load()["tasks"]["task"]["status"])
        self.agent.reset_mock()
        for action in (lambda: self.c.run("other", ["reload"]), lambda: self.c.attach("new", "https://example.com"), lambda: self.c.release("other", "failure"), lambda: self.c.start(True, False, None)):
            self.assertCode("auth_lease", action)
        self.agent.assert_not_called()
        self.assertCode("confirmation_required", self.c.resume, "task", True, False)
        self.assertEqual("task", self.store.load()["auth_task"])
        self.c.resume("task", True, True)
        self.assertIsNone(self.store.load()["auth_task"])
        self.assertEqual("active", self.c.task("task")["status"])

    def test_handoff_requires_capture_declaration_and_viewer_does_not_resume_or_launch(self):
        self.assertCode("confirmation_required", self.c.handoff, "task", False, False)
        self.agent.assert_not_called()
        self.c.handoff("task", False, True)
        result = self.c.viewer("task", False)
        self.assertFalse(result["serviceStarted"])
        self.assertNotIn("streamCommandTemplate", result)
        self.assertNotIn("cdpPort", result)
        self.assertEqual("human", self.store.load()["tasks"]["task"]["status"])
        self.process.assert_not_called()
        self.popen.assert_not_called()
        self.assertCode("human_lease", self.c.run, "task", ["reload"])
        self.assertCode("confirmation_required", self.c.resume, "task", False, True)
        self.c.resume("task", True, False)
        self.assertCode("task_paused", self.c.resume, "task", True, True)

    def test_viewer_requires_human_lease_and_never_launches_a_cold_browser(self):
        self.assertCode("human_lease", self.c.viewer, "task", True, 45678)
        self.verify.assert_not_called()
        self.c.task("task")["status"] = "human"
        self.verify.return_value = None
        self.assertCode("stale_browser", self.c.viewer, "task", True, 45678)
        self.agent.assert_not_called()
        self.process.assert_not_called()
        self.popen.assert_not_called()

    def test_viewer_refuses_another_tasks_authentication_lease(self):
        self.c.task("task")["status"] = "human"
        self.add_task("other", OTHER, "human", True)
        self.c.state["auth_task"] = "other"
        self.store.save(self.c.state)
        self.assertCode("auth_lease", self.c.viewer, "task", True, 45678)
        self.agent.assert_not_called()
        self.process.assert_not_called()

    def test_viewer_explicit_port_alone_does_not_authorize_start(self):
        self.c.handoff("task", False, True)
        self.agent.reset_mock()
        self.assertCode("confirmation_required", self.c.viewer, "task", False, 45678)
        self.assertEqual([["tab", "list"]], [call.args[2] for call in self.agent.call_args_list])
        self.process.assert_not_called()
        self.popen.assert_not_called()

    def test_viewer_approved_start_requires_valid_explicit_non_cdp_port(self):
        self.c.handoff("task", False, True)
        for port in (None, True, False, "45678", 45678.0, 0, 1023, 65536, BROWSER["port"]):
            with self.subTest(port=port):
                self.agent.reset_mock()
                self.assertCode("invalid_arguments", self.c.viewer, "task", True, port)
                self.assertEqual([["tab", "list"]], [call.args[2] for call in self.agent.call_args_list])
        self.process.assert_not_called()
        self.popen.assert_not_called()

    def test_viewer_refuses_existing_listener_before_stream_mutation(self):
        self.c.handoff("task", False, True)
        self.agent.reset_mock()
        self.process.side_effect = None
        self.process.return_value = subprocess.CompletedProcess([], 0, "p999\nn127.0.0.1:45678\n", "")
        self.assertCode("browser_unverified", self.c.viewer, "task", True, 45678)
        self.assertEqual([["tab", "list"]], [call.args[2] for call in self.agent.call_args_list])
        self.popen.assert_not_called()
        self.assertEqual("human", self.store.load()["tasks"]["task"]["status"])

    def test_viewer_stream_uses_session_agent_path_and_leaves_auth_handoff_paused(self):
        self.c.handoff("task", True, True)
        self.agent.reset_mock()
        self.verify.reset_mock()
        self.process.side_effect = [subprocess.CompletedProcess([], 1, "", ""), subprocess.CompletedProcess([], 0, "p999\nn127.0.0.1:45678\nn[::1]:45678\n", "")]
        with mock.patch.object(bs.shutil, "which", return_value="/fake/lsof"):
            result = self.c.viewer("task", True, 45678)
        self.assertEqual("stream_ready_dashboard_required", result["viewerStatus"])
        self.assertTrue(result["serviceStarted"])
        self.assertEqual(45678, result["streamPort"])
        self.assertEqual("loopback_only", result["listenerScope"])
        self.assertNotIn("url", result)
        self.assertNotIn("dashboardUrl", result)
        self.assertNotIn("streamCommandTemplate", result)
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual([["tab", "list"], ["stream", "enable", "--port", "45678"], ["tab", "list"]], [call.args[2] for call in self.agent.call_args_list])
        self.assertEqual(2, self.verify.call_count)
        for call in self.process.call_args_list:
            self.assertEqual(["/fake/lsof", "-nP", "-iTCP:45678", "-sTCP:LISTEN", "-Fpn"], call.args[0])
            self.assertEqual((0, 1), call.kwargs["accepted"])
        self.popen.assert_not_called()
        saved = self.store.load()
        self.assertEqual("human", saved["tasks"]["task"]["status"])
        self.assertEqual("task", saved["auth_task"])

    def test_viewer_listener_validation_fails_closed_after_stream_start(self):
        self.c.handoff("task", True, True)
        for listener in ("", "p999\n", "n127.0.0.1:45678\n", "pnot-numeric\nn127.0.0.1:45678\n", "p999\np998\nn127.0.0.1:45678\n", "p999\nn*:45678\n", "p999\nn0.0.0.0:45678\n", "p999\nn[::]:45678\n", "p999\nn192.168.1.1:45678\n", "p999\nn127.0.0.1:45679\n", "p999\nn127.0.0.1:45678\nn0.0.0.0:45678\n"):
            with self.subTest(listener=listener):
                self.process.side_effect = [subprocess.CompletedProcess([], 1, "", ""), subprocess.CompletedProcess([], 0, listener, "")]
                self.assertCode("browser_unverified", self.c.viewer, "task", True, 45678)
                self.assertEqual("human", self.store.load()["tasks"]["task"]["status"])
                self.assertEqual("task", self.store.load()["auth_task"])
        self.popen.assert_not_called()

    def test_viewer_stream_failure_preserves_lease_without_cleanup_or_launch(self):
        self.c.handoff("task", True, True)
        self.process.side_effect = None
        self.process.return_value = subprocess.CompletedProcess([], 1, "", "")
        self.agent.reset_mock()
        def respond(browser, task, args):
            if args[:2] == ["stream", "enable"]:
                raise bs.BrowserSessionError("command_timeout")
            return self.agent_response(browser, task, args)
        self.agent.side_effect = respond
        self.assertCode("command_timeout", self.c.viewer, "task", True, 45678)
        self.assertEqual([["tab", "list"], ["stream", "enable", "--port", "45678"]], [call.args[2] for call in self.agent.call_args_list])
        self.assertEqual("human", self.store.load()["tasks"]["task"]["status"])
        self.assertEqual("task", self.store.load()["auth_task"])
        self.popen.assert_not_called()

    def test_viewer_revalidates_target_after_stream_start_before_success(self):
        self.c.handoff("task", False, True)
        self.targets.side_effect = [{TARGET, OTHER}, {OTHER}]
        self.process.side_effect = None
        self.process.return_value = subprocess.CompletedProcess([], 1, "", "")
        self.assertCode("tab_gone", self.c.viewer, "task", True, 45678)
        self.process.assert_called_once()
        self.assertEqual("human", self.store.load()["tasks"]["task"]["status"])
        self.popen.assert_not_called()

    def test_unsettled_tasks_cannot_be_handed_to_human_without_reconciliation(self):
        for status in ("uncertain", "attaching", "releasing"):
            with self.subTest(status=status):
                self.c.task("task")["status"] = status
                self.store.save(self.c.state)
                self.assertCode("reconciliation_required", self.c.handoff, "task", False, True)
                self.assertEqual(status, self.store.load()["tasks"]["task"]["status"])
        self.agent.assert_not_called()

    def test_failure_release_cannot_launder_uncertainty_into_paused(self):
        for status in ("uncertain", "attaching", "releasing"):
            with self.subTest(status=status):
                self.c.task("task")["status"] = status
                self.store.save(self.c.state)
                self.assertCode("reconciliation_required", self.c.release, "task", "failure")
                self.assertEqual(status, self.store.load()["tasks"]["task"]["status"])
        self.agent.assert_not_called()

    def test_auth_handoff_refuses_other_unsettled_tasks(self):
        for status in ("uncertain", "attaching", "releasing"):
            with self.subTest(status=status):
                self.add_task("other", OTHER, status)
                self.assertCode("reconciliation_required", self.c.handoff, "task", True, True)
                self.assertIsNone(self.store.load()["auth_task"])
                self.assertEqual("active", self.c.task("task")["status"])
        self.agent.assert_not_called()

    def test_existing_browser_reused_in_matching_headed_or_headless_mode(self):
        for headed in (False, True):
            with self.subTest(headed=headed):
                browser = {**BROWSER, "headed": headed}
                self.verify.return_value = browser
                self.c.state["browser"] = bs.browser_identity(browser)
                self.assertEqual({"status": "already_running", "headed": headed}, self.c.start(False, headed, None))
        self.popen.assert_not_called()

    def test_existing_browser_cannot_change_mode(self):
        for headed in (False, True):
            with self.subTest(headed=headed):
                browser = {**BROWSER, "headed": not headed}
                self.verify.return_value = browser
                self.c.state["browser"] = bs.browser_identity(browser)
                self.assertCode("mode_transition_required", self.c.start, True, headed, None)
        self.popen.assert_not_called()

    def test_resume_checks_target_before_clearing_human_lease(self):
        self.c.handoff("task", True, True)
        self.targets.return_value = {OTHER}
        self.assertCode("tab_gone", self.c.resume, "task", True, True)
        self.assertEqual("human", self.store.load()["tasks"]["task"]["status"])
        self.assertEqual("task", self.store.load()["auth_task"])

    def test_success_closes_only_owned_target_and_leaves_tombstone(self):
        self.add_task("other", OTHER)
        self.targets.side_effect = [{TARGET, OTHER}, {OTHER}]
        result = self.c.release("task", "success")
        self.assertEqual("released", result["status"])
        self.assertEqual([["tab", "list"], ["tab", "close", TARGET]], [call.args[2] for call in self.agent.call_args_list])
        self.assertEqual("active", self.store.load()["tasks"]["other"]["status"])
        self.assertCode("task_exists", self.c.attach, "task", "https://example.com")
        self.assertCode("task_paused", self.c.run, "task", ["reload"])

    def test_failure_release_preserves_target_without_browser_calls(self):
        self.c.release("task", "failure")
        self.agent.assert_not_called()
        self.verify.assert_not_called()
        task = self.store.load()["tasks"]["task"]
        self.assertEqual("paused", task["status"])
        self.assertEqual(TARGET, task["targetId"])

    def test_close_failure_retains_releasing_journal_and_never_retries(self):
        def respond(browser, task, args):
            if args[:2] == ["tab", "close"]:
                raise bs.BrowserSessionError("command_timeout")
            return self.agent_response(browser, task, args)
        self.agent.side_effect = respond
        self.assertCode("command_timeout", self.c.release, "task", "success")
        self.assertEqual("releasing", self.store.load()["tasks"]["task"]["status"])
        self.agent.reset_mock()
        self.assertCode("task_paused", self.c.release, "task", "success")
        self.agent.assert_not_called()

    def test_attach_success_requires_exactly_one_new_owned_target(self):
        self.c.state["tasks"] = {}
        self.targets.side_effect = [{OTHER}, {OTHER, TARGET}]
        self.c.attach("new", "https://example.com")
        task = self.store.load()["tasks"]["new"]
        self.assertEqual("active", task["status"])
        self.assertEqual(TARGET, task["targetId"])
        self.assertEqual("https://example.com", task["origin"])

    def test_attach_uncertain_does_not_adopt_or_retry_after_unknown_response(self):
        self.targets.side_effect = [{TARGET, OTHER}, {TARGET, OTHER}]
        self.assertCode("ownership_unverified", self.c.attach, "new", "https://example.com")
        task = self.store.load()["tasks"]["new"]
        self.assertEqual("uncertain", task["status"])
        self.assertIsNone(task["targetId"])
        self.agent.reset_mock()
        self.assertCode("task_exists", self.c.attach, "new", "https://example.com")
        self.agent.assert_not_called()

    def test_attach_subprocess_failure_durably_records_intent(self):
        def fail_after_intent(*args):
            self.assertEqual("attaching", self.store.load()["tasks"]["new"]["status"])
            raise bs.BrowserSessionError("invalid_json")
        self.agent.side_effect = fail_after_intent
        self.assertCode("invalid_json", self.c.attach, "new", "https://example.com")
        self.assertEqual("uncertain", self.store.load()["tasks"]["new"]["status"])

    def test_agent_wrapper_pins_session_and_uses_empty_config_and_sanitized_environment(self):
        self.process.side_effect = None
        self.process.return_value = subprocess.CompletedProcess([], 0, '{"success":true,"data":{}}', "")
        with mock.patch.object(bs.shutil, "which", return_value="/fake/agent-browser"), mock.patch.dict(os.environ, {"AGENT_BROWSER_SESSION": "victim", "HTTPS_PROXY": "private-proxy"}):
            bs.Coordinator.agent(self.c, BROWSER, self.c.task("task"), ["reload"])
        args = self.process.call_args.args[0]
        self.assertEqual("/fake/agent-browser", args[0])
        self.assertEqual(self.c.task("task")["session"], args[args.index("--session") + 1])
        self.assertEqual(str(BROWSER["port"]), args[args.index("--cdp") + 1])
        self.assertEqual(str(self.store.empty_config()), args[args.index("--config") + 1])
        self.assertIn("--pin-tab", args)
        self.assertEqual("reload", args[-1])
        env = self.process.call_args.kwargs["env"]
        self.assertNotIn("AGENT_BROWSER_SESSION", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertEqual("ba-" + self.project.key, env["AGENT_BROWSER_NAMESPACE"])
        self.assertEqual(self.store.directory, self.process.call_args.kwargs["cwd"])

    def test_agent_wrapper_unknown_json_never_succeeds(self):
        self.process.side_effect = None
        with mock.patch.object(bs.shutil, "which", return_value="/fake/agent-browser"):
            for output in ("", "not-json-secret", "null", "42", "{}", '{"success":false,"error":"secret"}'):
                self.process.return_value = subprocess.CompletedProcess([], 0, output, "secret")
                self.assertCode("invalid_json", bs.Coordinator.agent, self.c, BROWSER, self.c.task("task"), ["reload"])

    def prepare_launch(self):
        self.c.state["tasks"] = {}
        self.c.state["browser"] = None
        self.verify.return_value = None
        executable = self.root / "Google Chrome"
        executable.write_text("fake executable; must never run")
        executable.chmod(0o700)
        return str(executable)

    def test_launch_requires_approval_and_rechecks_for_duplicate_process(self):
        chrome = self.prepare_launch()
        self.assertCode("launch_approval_required", self.c.start, False, False, chrome)
        self.discover.return_value = [BROWSER]
        self.assertCode("browser_unverified", self.c.start, True, False, chrome)
        self.popen.assert_not_called()

    def test_launch_locks_and_pending_journal_prevent_duplicate_start(self):
        chrome = self.prepare_launch()
        self.project.profile.mkdir()
        (self.project.profile / "SingletonLock").symlink_to("missing")
        self.assertCode("browser_locked", self.c.start, True, False, chrome)
        (self.project.profile / "SingletonLock").unlink()
        self.c.state["launch_pending"] = True
        self.assertCode("launch_uncertain", self.c.start, True, False, chrome)
        self.popen.assert_not_called()

    def test_mocked_launch_journals_before_popen_and_verifies_before_success(self):
        chrome = self.prepare_launch()
        self.verify.side_effect = [None, copy.deepcopy(BROWSER)]
        def launch(*args, **kwargs):
            self.assertTrue(self.store.load()["launch_pending"])
            return mock.Mock(pid=123)
        self.popen.side_effect = launch
        result = self.c.start(True, False, chrome)
        self.assertEqual("started", result["status"])
        self.popen.assert_called_once()
        args = self.popen.call_args.args[0]
        self.assertIn("--enable-features=ForceMacVSyncTimerForDebugging", args)
        self.assertIn("--remote-debugging-address=127.0.0.1", args)
        self.assertIn("--headless=new", args)
        self.assertIn("--user-data-dir=" + str(self.project.profile), args)
        self.assertFalse(self.store.load()["launch_pending"])

    def test_popen_failure_preserves_pending_and_blocks_retry(self):
        chrome = self.prepare_launch()
        self.popen.side_effect = OSError("secret path")
        self.assertCode("launch_uncertain", self.c.start, True, False, chrome)
        self.assertTrue(self.store.load()["launch_pending"])
        self.assertCode("launch_uncertain", self.c.start, True, False, chrome)
        self.popen.assert_called_once()


class CliTests(unittest.TestCase):
    def invoke(self, argv):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            result = bs.main(argv)
        text = stream.getvalue()
        self.assertNotIn("TOP_SECRET", text)
        self.assertNotIn("Traceback", text)
        return result, json.loads(text)

    def test_parser_error_suppresses_argument_values(self):
        result, value = self.invoke(["--TOP_SECRET"])
        self.assertEqual(1, result)
        self.assertEqual("invalid_arguments", value["error"]["code"])

    def test_runtime_errors_suppress_raw_exception_text(self):
        for error, expected, rc in ((OSError("TOP_SECRET"), "command_failed", 1), (ValueError("TOP_SECRET"), "command_failed", 1), (bs.BrowserSessionError("invalid_state"), "invalid_state", 1), (KeyboardInterrupt("TOP_SECRET"), "command_timeout", 130)):
            with self.subTest(error=error), mock.patch.object(bs.sys, "platform", "darwin"), mock.patch.object(bs, "resolve_project", side_effect=error):
                result, value = self.invoke(["inspect"])
                self.assertEqual(rc, result)
                self.assertEqual(expected, value["error"]["code"])

    def test_cli_cannot_select_alternate_state_directory(self):
        with mock.patch.object(bs, "resolve_project") as resolve:
            result, value = self.invoke(["--state-dir", "/TOP_SECRET/lease-bypass", "inspect"])
        self.assertEqual(1, result)
        self.assertEqual("invalid_arguments", value["error"]["code"])
        resolve.assert_not_called()

    def test_cli_uses_single_default_state_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project = bs.Project(root, "a" * 64, False)
            default = root / "invariant-state"
            with mock.patch.object(bs.sys, "platform", "darwin"), mock.patch.object(bs, "DEFAULT_STATE_DIR", default), mock.patch.object(bs, "resolve_project", return_value=project), mock.patch.object(bs, "check_profile"), mock.patch.object(bs.Coordinator, "inspect", return_value={"status": "not_running"}), mock.patch.object(bs, "StateStore", wraps=bs.StateStore) as constructor:
                result, value = self.invoke(["inspect"])
            self.assertEqual(0, result)
            self.assertTrue(value["success"])
            constructor.assert_called_once_with(default, project)
            self.assertTrue((default / project.key / "lock").is_file())

    def test_non_macos_rejected_before_project_access(self):
        with mock.patch.object(bs.sys, "platform", "linux"), mock.patch.object(bs, "resolve_project") as resolve:
            result, value = self.invoke(["inspect"])
        self.assertEqual(1, result)
        self.assertEqual("unsupported_platform", value["error"]["code"])
        resolve.assert_not_called()

    def test_process_failure_and_timeout_output_never_reaches_cli(self):
        for response, code in ((subprocess.CompletedProcess([], 1, "TOP_SECRET", "TOP_SECRET"), "command_failed"), (subprocess.TimeoutExpired("TOP_SECRET", 1, output="TOP_SECRET"), "command_timeout")):
            def resolve(*args):
                return bs.checked_process(["fake-only"])
            kwargs = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
            with mock.patch.object(bs.sys, "platform", "darwin"), mock.patch.object(bs, "resolve_project", side_effect=resolve), mock.patch.object(bs.subprocess, "run", **kwargs):
                result, value = self.invoke(["inspect"])
                self.assertEqual(1, result)
                self.assertEqual(code, value["error"]["code"])


if __name__ == "__main__":
    unittest.main()
