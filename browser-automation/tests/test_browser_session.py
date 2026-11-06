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
        with tempfile.TemporaryDirectory(prefix="", dir="/tmp") as temporary:
            home = Path(temporary).resolve()
            with mock.patch.dict(os.environ, dirty, clear=True), mock.patch.object(bs.Path, "home", return_value=home):
                clean = bs.child_environment("a" * 64)
        self.assertEqual({"PATH": "/safe/bin", "HOME": "/home/test", "AGENT_BROWSER_SOCKET_DIR": str(home / ".ab" / ("a" * 24))}, clean)
        self.assertEqual("other", dirty["AGENT_BROWSER_SESSION"])

    def test_allowed_commands(self):
        for command in (["back"], ["forward"], ["reload"], ["open", "https://example.com"], ["snapshot", "-i", "--compact"], ["click", "@e12"], ["fill", "@e1", "value with spaces"], ["press", "Enter"], ["scroll", "down", "10000"], ["wait", "0"], ["get", "text", "body"], ["is", "visible", "@e1"]):
            with self.subTest(command=command):
                self.assertEqual(command, bs.validate_run_command(command))

    def test_widget_command_grammar_is_scoped_and_positional_only(self):
        for command in (["inspect-widget", "@e9"],
                        ["click-widget", "@e9", "div:nth-child(2) > label:nth-child(1)",
                         "--expected-text", "Child folders"]):
            self.assertEqual(command, bs.validate_run_command(command))
        bad_paths = ["body", "#outside", "*", "div", "div:nth-child(0)",
            "div:nth-child(257)", "div:nth-child(01)", "div:nth-child(1), body",
            "div:nth-child(1) + button:nth-child(2)", "div:nth-child(1) ~ span:nth-child(2)",
            "div:nth-child(1) span:nth-child(2)", "div:nth-child(1)[for=outside]",
            "div:nth-child(1):has(input)", "div:nth-child(1) > *",
            "div:nth-child(1)\n", " > ".join(["div:nth-child(1)"] * 13)]
        for path in bad_paths:
            self.assertCode("command_denied", bs.validate_run_command,
                ["click-widget", "@e9", path, "--expected-text", "Child folders"])
        for command in (["inspect-widget", "body"], ["inspect-widget", "@e1", "--html"],
                        ["click-widget", "@e9", "label:nth-child(1)"],
                        ["click-widget", "@e9", "label:nth-child(1)", "--expected-text", ""],
                        ["click-widget", "@e9", "label:nth-child(1)", "--expected-text", "a" * 257],
                        ["click-widget", "@e9", "label:nth-child(1)", "--expected-text", "x\ny"],
                        ["click-widget", "@e9", "label:nth-child(1)", "--expected-text", "Child folders", "--force"]):
            self.assertCode("command_denied", bs.validate_run_command, command)

    def test_widget_menu_paths_and_inert_anchor_hrefs_are_allowed(self):
        command = ["click-widget", "@e9",
                   "ul:nth-child(1) > li:nth-child(2) > a:nth-child(1) > span:nth-child(1)",
                   "--expected-text", "Properties"]
        self.assertEqual(command, bs.validate_run_command(command))
        self.assertTrue(bs.widget_path("ol:nth-child(1) > li:nth-child(256)"))
        for href in (None, "", "#", "javascript:void(0)", "javascript:void(0);",
                     "javascript:;", "  JAVASCRIPT:VOID(0);  "):
            markup = '<a' + ('' if href is None else ' href="' + href + '"') + '>Properties</a>'
            self.assertEqual(markup, bs.checked_widget_markup(markup))

    def test_widget_anchor_navigation_queries_javascript_and_escapes_are_denied(self):
        for href in ("https://example.test/", "https://user:TOP_SECRET@example.test/",
                     "/path?token=TOP_SECRET", "?auth=TOP_SECRET", "#token=TOP_SECRET",
                     "javascript:alert(1)", "javascript:void(0);alert(1)",
                     "java&#x73;cript:alert(1)", "&#47;path?token=TOP_SECRET",
                     "data:text/html,TOP_SECRET", "javascript:\\x76oid(0)"):
            error = self.assertCode("command_denied", bs.checked_widget_markup,
                                    '<a href="' + href + '">Properties</a>')
            self.assertNotIn("TOP_SECRET", str(error))

    def test_widget_markup_screening_allows_benign_hidden_but_rejects_credentials(self):
        markup = '<label><input type="checkbox" value="1">Child folders</label><input type="hidden" name="position" value="2">'
        self.assertEqual(markup, bs.checked_widget_markup(markup))
        for markup in ('<input type="PASSWORD" value="TOP_SECRET">',
                       '<input type="hidden" name="csrf_token" value="TOP_SECRET">',
                       '<input type="hidden" name="api_key" value="TOP_SECRET">',
                       '<input type="text" value="TOP_SECRET">', '<textarea>TOP_SECRET</textarea>',
                       '<form></form>', '<script>TOP_SECRET</script>',
                       '<div data-token="TOP_SECRET"></div>', '<span onclick="TOP_SECRET"></span>',
                       "x" * 65537, "界" * 22000):
            self.assertCode("command_denied", bs.checked_widget_markup, markup)

    def test_find_visible_text_exact_click_only_accepts_bounded_literal_labels(self):
        for label in ("Child folders", "Дочерние папки", "子文件夹", "a" * 256):
            command = ["find", "text", label, "click", "--exact"]
            with self.subTest(label=label):
                self.assertEqual(command, bs.validate_run_command(command))

    def test_find_rejects_other_actions_selectors_flags_and_invalid_labels(self):
        commands = [
            ["find", "text", "Child folders", "click"],
            ["find", "text", "Child folders", "click", "--exact", "--new-tab"],
            ["find", "text", "Child folders", "click", "--exact", "--nth", "1"],
            ["find", "nth", "0", "click", "--exact"],
            ["find", "css", "input", "click", "--exact"],
            ["find", "regex", ".*", "click", "--exact"],
            ["find", "text", "Child folders", "fill", "--exact"],
            ["find", "text", "Child folders", "eval", "--exact"],
            ["find", "text", "Child folders", "dblclick", "--exact"],
            ["find", "text", "Child folders", "click", "--exact=true"],
            ["find", "text", "Child folders", "--exact", "click"],
        ]
        commands += [["find", "text", label, "click", "--exact"] for label in (
            "", "   ", "a" * 257, "--new-tab", "line\nline", "line\rline",
            "line\x00line", "line\tline", "line\x7fline", "line\x85line",
            "line\u2028line", "line\u2029line")]
        for command in commands:
            with self.subTest(command=command):
                self.assertCode("command_denied", bs.validate_run_command, command)

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

    def test_separate_git_directory_and_linked_worktree_share_identity(self):
        external = self.root / "external git dir"
        self.git("init", "-q", "--separate-git-dir", str(external), str(self.repo))
        self.git("config", "core.worktree", str(self.repo))
        self.assertTrue((self.repo / ".git").is_file())
        canonical = bs.resolve_project(self.repo)
        self.assertEqual(self.repo, canonical.root)
        tree = self.root / "linked external worktree"
        self.git("worktree", "add", "--orphan", "-q", "-b", "external-branch", str(tree))
        sub = tree / "sub"
        sub.mkdir()
        self.assertEqual(canonical, bs.resolve_project(tree))
        self.assertEqual(canonical, bs.resolve_project(cwd=sub))

    def test_separate_git_directory_without_main_location_fails_closed(self):
        external = self.root / "external git dir"
        self.git("init", "-q", "--separate-git-dir", str(external), str(self.repo))
        self.assertCode("invalid_project", bs.resolve_project, self.repo)

    def test_separate_git_directory_relative_main_location(self):
        external = self.root / "external git dir"
        self.git("init", "-q", "--separate-git-dir", str(external), str(self.repo))
        self.git("config", "core.worktree", "../main checkout")
        self.assertEqual(self.repo, bs.resolve_project(self.repo).root)

    def test_separate_git_directory_wrong_main_location_fails_closed(self):
        external = self.root / "external git dir"
        self.git("init", "-q", "--separate-git-dir", str(external), str(self.repo))
        other = self.root / "other checkout"
        self.git("init", "-q", str(other))
        self.git("config", "core.worktree", str(other))
        self.assertCode("invalid_project", bs.resolve_project, self.repo)

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
        # Keep the fake HOME short enough for validated Unix socket paths.
        self.temp = tempfile.TemporaryDirectory(prefix="", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        home_patch = mock.patch.object(bs.Path, "home", return_value=self.root)
        home_patch.start()
        self.addCleanup(home_patch.stop)
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

    def widget_response(self, browser, task, args):
        if args == ["tab", "list"]:
            return self.agent_response(browser, task, args)
        if args[:2] == ["get", "attr"]:
            data = {"value": "listbox" if args[-1] == "role" else "ext-gen123"}
        elif args[:2] == ["get", "count"]:
            data = {"count": 1}
        elif args[:2] == ["get", "html"]:
            data = {"html": '<div><label><input type="checkbox">Child folders</label></div>'}
        elif args[:2] == ["get", "text"]:
            data = {"text": "  Child  folders  "}
        elif args[:2] == ["is", "visible"]:
            data = {"visible": True}
        elif args[0] == "click":
            data = {"clicked": True}
        else:
            raise AssertionError("Unexpected widget IPC")
        return {"success": True, "data": data}

    def test_menu_ul_root_inspection_then_properties_path_sends_one_verified_click(self):
        markup = '<li><a href="#"><span>Properties</span></a></li>'
        def response(browser, task, args):
            if args[:2] == ["get", "attr"] and args[-1] == "role":
                return {"success": True, "data": {"value": "menu"}}
            if args[:2] == ["get", "html"]:
                return {"success": True, "data": {"html": markup}}
            if args[:2] == ["get", "text"]:
                return {"success": True, "data": {"text": "Properties"}}
            return self.widget_response(browser, task, args)
        self.agent.side_effect = response
        result = self.c.run("task", ["inspect-widget", "@e9"])
        self.assertEqual({"widgetId": "ext-gen123", "role": "menu", "html": markup}, result)
        self.agent.reset_mock()
        path = "li:nth-child(1) > a:nth-child(1) > span:nth-child(1)"
        self.c.run("task", ["click-widget", "@e9", path, "--expected-text", "Properties"])
        selector = "#ext-gen123 > " + path
        commands = [c.args[2] for c in self.agent.call_args_list]
        self.assertEqual([["click", selector]], [a for a in commands if a[0] == "click"])
        self.assertIn(["get", "count", "div#ext-gen123, ul#ext-gen123, ol#ext-gen123"], commands)
        self.assertIn(["get", "text", selector], commands)
        self.assertIn(["is", "visible", selector], commands)
        self.assertEqual(2, commands.count(["get", "count", selector]))
        self.assertEqual("active", self.store.load()["tasks"]["task"]["status"])

    def test_menu_unsafe_anchor_href_never_returns_markup_or_clicks(self):
        def response(browser, task, args):
            if args[:2] == ["get", "attr"] and args[-1] == "role":
                return {"success": True, "data": {"value": "menu"}}
            if args[:2] == ["get", "html"]:
                return {"success": True, "data": {"html": '<li><a href="/path?token=TOP_SECRET">Properties</a></li>'}}
            return self.widget_response(browser, task, args)
        self.agent.side_effect = response
        error = self.assertCode("command_denied", self.c.run, "task",
            ["click-widget", "@e9", "li:nth-child(1) > a:nth-child(1)", "--expected-text", "Properties"])
        self.assertNotIn("TOP_SECRET", str(error))
        self.assertNotIn("TOP_SECRET", self.store.path.read_text())
        self.assertFalse(any(c.args[2][0] == "click" for c in self.agent.call_args_list))
        self.assertEqual("uncertain", self.c.task("task")["status"])

    def test_inspect_widget_returns_only_verified_bounded_widget_markup(self):
        self.agent.side_effect = self.widget_response
        result = self.c.run("task", ["inspect-widget", "@e9"])
        self.assertEqual({"widgetId", "role", "html"}, set(result))
        self.assertEqual("ext-gen123", result["widgetId"])
        self.assertEqual("listbox", result["role"])
        self.assertIn("Child folders", result["html"])
        self.assertNotIn("Child folders", self.store.path.read_text())
        self.assertFalse(any(call.args[2][0] == "click" for call in self.agent.call_args_list))
        self.assertEqual("active", self.c.task("task")["status"])

    def test_click_widget_builds_unique_direct_child_css_and_checks_text_and_visibility(self):
        observed = []
        def response(browser, task, args):
            if args != ["tab", "list"]:
                observed.append(self.store.load()["tasks"]["task"]["status"])
            return self.widget_response(browser, task, args)
        self.agent.side_effect = response
        self.c.run("task", ["click-widget", "@e9",
            "div:nth-child(1) > label:nth-child(1)", "--expected-text", "Child folders"])
        commands = [c.args[2] for c in self.agent.call_args_list]
        selector = "#ext-gen123 > div:nth-child(1) > label:nth-child(1)"
        self.assertEqual([["click", selector]], [a for a in commands if a[0] == "click"])
        self.assertIn(["get", "text", selector], commands)
        self.assertIn(["is", "visible", selector], commands)
        self.assertEqual(2, commands.count(["get", "count", selector]))
        self.assertEqual({"uncertain"}, set(observed))
        self.assertFalse(any(a[0] in {"eval", "open", "cookies", "storage", "close"} for a in commands))
        self.popen.assert_not_called()

    def test_widget_rejects_nonwidget_roles_and_unsafe_ids_before_markup(self):
        for role, widget_id in (("dialog", "ext-gen123"), ("form", "ext-gen123"),
                ("textbox", "ext-gen123"), ("body", "ext-gen123"), ("grid", "ext-gen123"),
                ("listbox", "body, #outside"), ("tree", "x\\:escape"),
                ("listbox", "x" * 129), ("listbox", "")):
            self.add_task("task")
            def response(browser, task, args):
                if args[:2] == ["get", "attr"]:
                    return {"success": True, "data": {"value": role if args[-1] == "role" else widget_id}}
                return self.widget_response(browser, task, args)
            self.agent.reset_mock()
            self.agent.side_effect = response
            self.assertCode("command_denied", self.c.run, "task", ["inspect-widget", "@e9"])
            self.assertFalse(any(c.args[2][:2] == ["get", "html"] for c in self.agent.call_args_list))

    def test_widget_rejects_spoofed_body_form_or_dialog_tag_even_with_listbox_role(self):
        def response(browser, task, args):
            if args[:2] == ["get", "count"] and args[2].startswith("div#"):
                return {"success": True, "data": {"count": 0}}
            return self.widget_response(browser, task, args)
        self.agent.side_effect = response
        self.assertCode("ownership_unverified", self.c.run, "task", ["inspect-widget", "@e9"])
        self.assertFalse(any(c.args[2][:2] == ["get", "html"] for c in self.agent.call_args_list))

    def test_widget_nonunique_wrong_text_or_invisible_target_never_clicks(self):
        selector = "#ext-gen123 > label:nth-child(1)"
        cases = [(["get", "count", selector], {"count": 2}),
                 (["get", "text", selector], {"text": "Save"}),
                 (["is", "visible", selector], {"visible": False}),
                 (["get", "count", "#ext-gen123"], {"count": 2})]
        for bad_command, bad_data in cases:
            self.add_task("task")
            self.agent.reset_mock()
            def response(browser, task, args):
                return ({"success": True, "data": bad_data} if args == bad_command
                        else self.widget_response(browser, task, args))
            self.agent.side_effect = response
            self.assertCode("ownership_unverified", self.c.run, "task",
                ["click-widget", "@e9", "label:nth-child(1)", "--expected-text", "Child folders"])
            self.assertFalse(any(c.args[2][0] == "click" for c in self.agent.call_args_list))
            self.assertEqual("uncertain", self.store.load()["tasks"]["task"]["status"])

    def test_widget_markup_credentials_never_return_or_reach_click(self):
        def response(browser, task, args):
            if args[:2] == ["get", "html"]:
                return {"success": True, "data": {"html": '<input type="hidden" name="csrf_token" value="TOP_SECRET">'}}
            return self.widget_response(browser, task, args)
        self.agent.side_effect = response
        error = self.assertCode("command_denied", self.c.run, "task",
            ["click-widget", "@e9", "label:nth-child(1)", "--expected-text", "Child folders"])
        self.assertNotIn("TOP_SECRET", str(error))
        self.assertNotIn("TOP_SECRET", self.store.path.read_text())
        self.assertFalse(any(c.args[2][0] == "click" for c in self.agent.call_args_list))

    def test_widget_commands_keep_lease_pause_and_missing_target_guards(self):
        commands = [["inspect-widget", "@e9"],
                    ["click-widget", "@e9", "label:nth-child(1)", "--expected-text", "Child folders"]]
        for command in commands:
            self.c.task("task")["status"] = "human"
            self.assertCode("human_lease", self.c.run, "task", command)
            self.c.task("task")["status"] = "uncertain"
            self.assertCode("task_paused", self.c.run, "task", command)
            self.c.task("task")["status"] = "active"
            self.targets.return_value = {OTHER}
            self.assertCode("tab_gone", self.c.run, "task", command)
        self.agent.assert_not_called()

    def test_widget_real_wrapper_timeout_invalidates_receipt_without_click_or_replay(self):
        self.agent.side_effect = lambda *a: bs.Coordinator.agent(self.c, *a)
        def process(args, **kwargs):
            command = args[args.index("--json") + 1:]
            if command[:2] == ["get", "text"]:
                raise bs.BrowserSessionError("command_timeout")
            response = self.widget_response(BROWSER, self.c.task("task"), command)
            return subprocess.CompletedProcess([], 0, json.dumps(response), "")
        self.process.side_effect = process
        command = ["click-widget", "@e9", "label:nth-child(1)", "--expected-text", "Child folders"]
        with mock.patch.object(bs.shutil, "which", return_value="/fake/agent-browser"):
            self.assertCode("command_timeout", self.c.run, "task", command)
        operation = self.store.load()["tasks"]["task"]["operation"]
        self.assertEqual({"completed": False, "outcome": "unknown"}, operation["last_call"])
        self.assertFalse(any(c.args[2][0] == "click" for c in self.agent.call_args_list))
        self.agent.reset_mock()
        self.assertCode("task_paused", self.c.run, "task", command)
        self.agent.assert_not_called()

    def test_find_exact_click_uses_normal_run_journal_and_rejects_extra_flags_before_ipc(self):
        denied = ["find", "text", "Child folders", "click", "--exact", "--new-tab"]
        self.assertCode("command_denied", self.c.run, "task", denied)
        self.agent.assert_not_called()
        self.verify.assert_not_called()
        command = ["find", "text", "Child folders", "click", "--exact"]
        observed = []
        def response(browser, task, args):
            if args == command:
                observed.append(self.store.load()["tasks"]["task"]["status"])
            return self.agent_response(browser, task, args)
        self.agent.side_effect = response
        self.c.run("task", command)
        self.assertEqual(["uncertain"], observed)
        self.assertEqual([["tab", "list"], command, ["tab", "list"]],
                         [call.args[2] for call in self.agent.call_args_list])
        self.assertEqual("active", self.store.load()["tasks"]["task"]["status"])

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

    def enable_cross_site(self, enabled=True):
        (self.root / "browser-concurrency.json").write_text(json.dumps({
            "version": 1, "allow_cross_site_handoffs": enabled,
            "site_roots": sorted(bs.SUPPORTED_SITE_ROOTS),
        }))

    def scoped_auth(self):
        self.enable_cross_site()
        human = self.c.task("task")
        human.update(status="human", auth=True, origin="https://login.selectel.ru")
        self.c.state["auth_task"] = "task"
        other = self.add_task("other", OTHER)
        other["origin"] = "https://blog.psychodemia.ru"
        self.store.save(self.c.state)
        return human, other

    def test_cross_site_guard_is_opt_in_and_has_no_browser_or_state_side_effects(self):
        human, other = self.scoped_auth()
        before = copy.deepcopy(self.c.state)
        self.c.guard(other)
        self.c.guard(requested_origin="https://miin.ru")
        self.assertEqual(before, self.c.state)
        self.verify.assert_not_called()
        self.agent.assert_not_called()
        self.process.assert_not_called()
        self.popen.assert_not_called()
        for enabled in (False, None):
            if enabled is None:
                (self.root / "browser-concurrency.json").unlink()
            else:
                self.enable_cross_site(enabled)
            self.assertCode("auth_lease", self.c.guard, other)
            self.assertCode("auth_lease", self.c.guard, requested_origin="https://miin.ru")

    def test_cross_site_guard_requires_known_active_unique_owned_target(self):
        human, other = self.scoped_auth()
        self.assertCode("auth_lease", self.c.guard, copy.deepcopy(other))
        for status in ("paused", "uncertain", "attaching", "released", "human"):
            other["status"] = status
            self.assertCode("auth_lease", self.c.guard, other)
        other["status"] = "active"
        for target in (None, "short", human["targetId"]):
            other["targetId"] = target
            self.assertCode("auth_lease", self.c.guard, other)
        self.agent.assert_not_called()

    def test_cross_site_guard_denies_same_site_subdomains_unknown_or_ambiguous_origins(self):
        human, other = self.scoped_auth()
        for value in ("https://selectel.ru", "https://panel.selectel.ru:8443", None,
                      "opaque", "about:blank", "https://127.0.0.1", "https://localhost",
                      "https://psychodemia.ru.evil.com", "https://unknown.ru",
                      "https://a.blogspot.com", "https://psychodemia.ru.",
                      "https://user@psychodemia.ru", "https://psychodemia.ru/path",
                      "https://psychodemia.ru?secret=x", "https://-bad.psychodemia.ru",
                      "https://psychodemia.ru:0", "http://psychodemia.ru"):
            with self.subTest(value=value):
                other["origin"] = value
                self.assertCode("auth_lease", self.c.guard, other)
                self.assertCode("auth_lease", self.c.guard, requested_origin=value)
        other["origin"] = "https://psychodemia.ru"
        human["origin"] = "opaque"
        self.assertCode("auth_lease", self.c.guard, other)
        self.agent.assert_not_called()

    def test_cross_site_guard_protects_all_human_leases_even_without_auth(self):
        human, other = self.scoped_auth()
        human.update(auth=False)
        self.c.state["auth_task"] = None
        self.c.guard(other)
        self.assertCode("human_lease", self.c.guard, human)
        extra = self.add_task("human-two", "HUMAN002", "human")
        extra["origin"] = "https://psychodemia.ru"
        self.assertCode("human_lease", self.c.guard, other)
        extra["origin"] = "https://miin.ru"
        self.c.guard(other)
        other["origin"] = "https://school.miin.ru"
        self.assertCode("human_lease", self.c.guard, other)

    def test_cross_site_guard_covers_exact_eight_audit_domains(self):
        human, other = self.scoped_auth()
        domains = ("mosdigitals.ru", "miin.ru", "maed.ru", "fashionfactoryschool.com",
                   "school-xyz.com", "bangbangeducation.ru", "psychodemia.ru", "psychodemia.com")
        for domain in domains:
            with self.subTest(domain=domain):
                other["origin"] = "https://" + domain
                self.c.guard(other)
                self.c.guard(requested_origin="https://www." + domain)
        human["origin"] = "https://account.fashionfactoryschool.com"
        other["origin"] = "https://www.fashionfactoryschool.com"
        self.assertCode("auth_lease", self.c.guard, other)
        human["origin"] = "https://account.psychodemia.com"
        other["origin"] = "https://www.psychodemia.com"
        self.assertCode("auth_lease", self.c.guard, other)

    def test_cross_site_guard_groups_legacy_school_alias_with_current_site(self):
        human, other = self.scoped_auth()
        human["origin"] = "https://fashionfactoryschool.ru"
        other["origin"] = "https://blog.fashionfactory.ru"
        self.assertCode("auth_lease", self.c.guard, other)

    def test_cross_site_start_same_task_and_second_auth_remain_blocked(self):
        human, other = self.scoped_auth()
        self.assertCode("auth_lease", self.c.guard, human)
        self.assertCode("auth_lease", self.c.start, True, False, None)
        self.assertCode("auth_lease", self.c.handoff, "other", True, True)
        self.assertCode("auth_lease", self.c.run, "other", ["open", "https://panel.selectel.ru"])
        self.agent.assert_not_called()
        self.verify.assert_not_called()
        self.assertEqual("task", self.c.state["auth_task"])
        self.assertEqual("human", human["status"])

    def test_cross_site_run_rechecks_actual_origin_before_page_command(self):
        human, other = self.scoped_auth()
        self.agent.side_effect = lambda *args: [{"targetId": OTHER, "url": "https://panel.selectel.ru", "active": True}]
        self.assertCode("auth_lease", self.c.run, "other", ["reload"])
        self.assertEqual([["tab", "list"]], [call.args[2] for call in self.agent.call_args_list])
        self.assertEqual("human", human["status"])

    def test_cross_site_run_and_attach_during_auth_preserve_human_lease(self):
        human, other = self.scoped_auth()
        self.agent.side_effect = lambda browser, task, args: ([{"targetId": task["targetId"] or "NEWTAB01", "url": "https://psychodemia.ru", "active": True}] if args == ["tab", "list"] else {"ok": True})
        self.c.run("other", ["reload"])
        self.targets.side_effect = [{TARGET, OTHER}, {TARGET, OTHER, "NEWTAB01"}]
        self.c.attach("new", "https://psychodemia.ru")
        self.assertEqual("active", self.c.task("new")["status"])
        self.assertEqual("task", self.store.load()["auth_task"])
        self.assertEqual("human", human["status"])

    def test_cross_site_redirect_leaves_uncertainty_without_clearing_auth(self):
        for action in ("run", "attach"):
            with self.subTest(action=action):
                human, other = self.scoped_auth()
                def response(browser, task, args):
                    if args == ["tab", "list"]:
                        return [{"targetId": task["targetId"] or "NEWTAB01", "url": "https://panel.selectel.ru" if self.agent.call_count > 1 else "https://psychodemia.ru", "active": True}]
                    return {"ok": True}
                self.agent.reset_mock()
                self.agent.side_effect = response
                if action == "run":
                    self.assertCode("auth_lease", self.c.run, "other", ["reload"])
                    name = "other"
                else:
                    self.targets.side_effect = [{TARGET, OTHER}, {TARGET, OTHER, "NEWTAB01"}]
                    self.assertCode("auth_lease", self.c.attach, "new", "https://psychodemia.ru")
                    name = "new"
                self.assertEqual("uncertain", self.store.load()["tasks"][name]["status"])
                self.assertEqual("task", self.store.load()["auth_task"])
                self.assertEqual("human", human["status"])

    def test_cross_site_policy_strict_schema_and_safe_file(self):
        self.enable_cross_site()
        path = self.root / "browser-concurrency.json"
        valid = json.loads(path.read_text())
        for policy in ([], {**valid, "version": True}, {**valid, "version": 2},
                       {**valid, "extra": True}, {**valid, "allow_cross_site_handoffs": 1},
                       {**valid, "site_roots": []}, {**valid, "site_roots": ["co.uk"]},
                       {**valid, "site_roots": ["blog.psychodemia.ru"]},
                       {**valid, "site_roots": ["miin.ru", "miin.ru"]},
                       {**valid, "site_roots": [123]}):
            path.write_text(json.dumps(policy))
            self.assertCode("invalid_policy", bs.concurrency_roots, self.project)
        path.write_text('{"version":1,"version":1,"allow_cross_site_handoffs":true,"site_roots":["miin.ru"]}')
        self.assertCode("invalid_policy", bs.concurrency_roots, self.project)
        path.write_text("{broken")
        self.assertCode("invalid_policy", bs.concurrency_roots, self.project)
        self.enable_cross_site()
        path.chmod(0o666)
        self.assertCode("invalid_policy", bs.concurrency_roots, self.project)
        path.unlink()
        path.symlink_to(self.root / "missing")
        self.assertCode("unsafe_path", bs.concurrency_roots, self.project)

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
        self.assertNotIn("AGENT_BROWSER_NAMESPACE", env)
        self.assertEqual(str(self.root / ".ab" / self.project.key[:24]), env["AGENT_BROWSER_SOCKET_DIR"])
        self.assertEqual(self.store.directory, self.process.call_args.kwargs["cwd"])

    def retire(self, **overrides):
        args = dict(task_id="task", expected_target=TARGET,
                    human_confirmed=True, discard_unsaved=True)
        args.update(overrides)
        return self.c.retire(**args)

    def uncertain_task(self):
        task = self.c.task("task")
        task.update(status="uncertain", operation={"id": "b" * 32, "kind": "run",
            "browser": bs.browser_identity(BROWSER), "session": task["session"],
            "targetId": TARGET, "last_call": {"completed": False, "outcome": "unknown"}})
        self.store.save(self.c.state)
        return task

    def test_retire_requires_both_approval_declarations_before_any_browser_work(self):
        self.uncertain_task()
        before = self.store.path.read_bytes()
        for flags in ({"human_confirmed": False}, {"discard_unsaved": False}):
            self.assertCode("confirmation_required", self.retire, **flags)
        self.assertEqual(before, self.store.path.read_bytes())
        self.verify.assert_not_called()
        self.targets.assert_not_called()
        self.agent.assert_not_called()

    def test_retire_rejects_wrong_target_duplicate_owner_and_nonuncertain_status(self):
        self.uncertain_task()
        for target in (OTHER, "short", None):
            self.assertCode("ownership_unverified", self.retire, expected_target=target)
        self.add_task("duplicate", TARGET)
        self.assertCode("ownership_unverified", self.retire)
        del self.c.state["tasks"]["duplicate"]
        for status in ("active", "paused", "releasing", "released", "attaching"):
            self.c.task("task")["status"] = status
            self.assertCode("reconciliation_required", self.retire)
        self.verify.assert_not_called()
        self.agent.assert_not_called()

    def test_retire_rejects_all_human_auth_and_stale_auth_leases_before_browser(self):
        self.uncertain_task()
        other = self.add_task("other", OTHER, "human")
        self.assertCode("human_lease", self.retire)
        other.update(status="paused", auth=True)
        self.assertCode("auth_lease", self.retire)
        other.update(status="human")
        self.c.state["auth_task"] = "other"
        self.assertCode("auth_lease", self.retire)
        self.verify.assert_not_called()
        self.agent.assert_not_called()

    def test_retire_requires_recorded_matching_live_browser(self):
        self.uncertain_task()
        self.c.state["browser"] = None
        self.assertCode("browser_unverified", self.retire)
        self.verify.assert_not_called()
        self.c.state["browser"] = bs.browser_identity(BROWSER)
        self.verify.return_value = None
        self.assertCode("stale_browser", self.retire)
        self.verify.side_effect = bs.BrowserSessionError("stale_browser")
        self.assertCode("stale_browser", self.retire)
        self.agent.assert_not_called()
        self.assertNotIn("retirement", self.c.task("task"))

    def test_retire_one_exact_close_journals_intent_preserves_unknown_and_other_tasks(self):
        task = self.uncertain_task()
        original = copy.deepcopy(task["operation"])
        other = self.add_task("other", OTHER)
        original_other = copy.deepcopy(other)
        self.targets.side_effect = [{TARGET, OTHER}, {TARGET, OTHER}, {OTHER}]
        def response(browser, task, args):
            if args[:2] == ["tab", "close"]:
                saved = self.store.load()["tasks"]["task"]
                self.assertEqual("releasing", saved["status"])
                self.assertTrue(saved["retirement"]["closeAttempted"])
                self.assertEqual(original, saved["operation"])
                self.assertCode("task_paused", self.c.run, "task", ["reload"])
            return self.agent_response(browser, task, args)
        self.agent.side_effect = response
        result = self.retire()
        self.assertEqual("released", result["status"])
        self.assertEqual("aborted_by_user_no_completion_claim", result["abortOutcome"])
        self.assertTrue(result["targetAbsentVerified"])
        self.assertFalse(result["originalOperationCompletionClaimed"])
        self.assertFalse(result["completionRecorded"])
        self.assertEqual([["tab", "list"], ["tab", "close", TARGET]],
                         [c.args[2] for c in self.agent.call_args_list])
        saved = self.store.load()
        self.assertEqual(original, saved["tasks"]["task"]["operation"])
        self.assertEqual(original_other, saved["tasks"]["other"])
        self.agent.reset_mock()
        self.assertCode("retirement_recorded", self.retire)
        self.assertCode("task_exists", self.c.attach, "task", "https://example.com")
        self.agent.assert_not_called()
        self.popen.assert_not_called()

    def test_retire_already_absent_target_needs_fresh_absence_but_no_daemon_command(self):
        task = self.uncertain_task()
        task.pop("operation")  # Legacy uncertainty must not gain a fake receipt.
        self.store.save(self.c.state)
        self.targets.return_value = {OTHER}
        result = self.retire()
        self.assertEqual("released", result["status"])
        self.assertFalse(result["closeAttempted"])
        self.assertTrue(result["targetAbsentVerified"])
        self.assertEqual(2, self.targets.call_count)
        self.assertEqual(2, self.verify.call_count)
        self.assertNotIn("operation", self.store.load()["tasks"]["task"])
        self.agent.assert_not_called()

    def test_retire_timeout_leaves_uncertain_and_never_sends_a_second_close(self):
        task = self.uncertain_task()
        original = copy.deepcopy(task["operation"])
        def response(browser, task, args):
            if args[:2] == ["tab", "close"]:
                raise bs.BrowserSessionError("command_timeout")
            return self.agent_response(browser, task, args)
        self.agent.side_effect = response
        self.assertCode("command_timeout", self.retire)
        saved = self.store.load()["tasks"]["task"]
        self.assertEqual("uncertain", saved["status"])
        self.assertEqual("unknown", saved["retirement"]["outcome"])
        self.assertTrue(saved["retirement"]["closeAttempted"])
        self.assertFalse(saved["retirement"]["targetAbsentVerified"])
        self.assertEqual(original, saved["operation"])
        self.agent.reset_mock()
        self.assertCode("retirement_recorded", self.retire)
        self.assertCode("retirement_recorded", self.reconcile)
        self.assertCode("reconciliation_required", self.c.release, "task", "failure")
        self.agent.assert_not_called()

    def test_retire_ownership_timeout_never_closes(self):
        self.uncertain_task()
        self.agent.side_effect = bs.BrowserSessionError("command_timeout")
        self.assertCode("command_timeout", self.retire)
        self.assertEqual([["tab", "list"]], [c.args[2] for c in self.agent.call_args_list])
        self.assertFalse(self.store.load()["tasks"]["task"]["retirement"]["closeAttempted"])
        self.assertCode("retirement_recorded", self.retire)

    def test_retire_close_success_response_is_not_absence_proof(self):
        self.uncertain_task()
        self.assertCode("ownership_unverified", self.retire)
        self.assertEqual(1, sum(c.args[2] == ["tab", "close", TARGET]
                                for c in self.agent.call_args_list))
        self.assertEqual("uncertain", self.store.load()["tasks"]["task"]["status"])
        self.agent.reset_mock()
        self.assertCode("retirement_recorded", self.retire)
        self.agent.assert_not_called()

    def test_retire_origin_or_browser_change_stops_without_retirement_success(self):
        self.uncertain_task()
        self.agent.side_effect = lambda *a: [
            {"targetId": TARGET, "url": "https://unexpected.example", "active": True}]
        self.assertCode("ownership_unverified", self.retire)
        self.assertFalse(self.store.load()["tasks"]["task"]["retirement"]["closeAttempted"])
        self.add_task("task")
        self.uncertain_task()
        self.agent.side_effect = self.agent_response
        self.verify.side_effect = [copy.deepcopy(BROWSER), copy.deepcopy(BROWSER),
                                  bs.BrowserSessionError("stale_browser")]
        self.assertCode("stale_browser", self.retire)
        saved = self.store.load()["tasks"]["task"]
        self.assertEqual("uncertain", saved["status"])
        self.assertTrue(saved["retirement"]["closeAttempted"])
        self.assertFalse(saved["retirement"]["targetAbsentVerified"])

    def test_retire_unrelated_exception_is_rethrown(self):
        self.uncertain_task()
        def response(browser, task, args):
            if args[:2] == ["tab", "close"]:
                raise ValueError("TOP_SECRET")
            return self.agent_response(browser, task, args)
        self.agent.side_effect = response
        with self.assertRaisesRegex(ValueError, "TOP_SECRET"):
            self.retire()
        self.assertEqual("uncertain", self.store.load()["tasks"]["task"]["status"])
        self.assertNotIn("TOP_SECRET", self.store.path.read_text())

    def test_retire_real_agent_wrapper_never_launders_original_receipt(self):
        task = self.uncertain_task()
        original = copy.deepcopy(task["operation"])
        self.targets.side_effect = [{TARGET, OTHER}, {TARGET, OTHER}, {OTHER}]
        self.agent.side_effect = lambda *a: bs.Coordinator.agent(self.c, *a)
        tabs = json.dumps({"success": True, "data": {"tabs": [
            {"targetId": TARGET, "url": "https://example.com", "active": True}]}})
        self.process.side_effect = [subprocess.CompletedProcess([], 0, tabs, ""),
            subprocess.CompletedProcess([], 0, '{"success":true,"data":{"closed":true}}', "")]
        with mock.patch.object(bs.shutil, "which", return_value="/fake/agent-browser"):
            self.retire()
        self.assertEqual(original, self.store.load()["tasks"]["task"]["operation"])
        self.assertEqual(2, self.process.call_count)

    def completed_run(self, outcome="error"):
        task = self.c.task("task")
        task.update(status="uncertain", operation={
            "id": "b" * 32, "kind": "run", "browser": bs.browser_identity(BROWSER),
            "session": task["session"], "targetId": TARGET,
            "last_call": {"completed": True, "outcome": outcome},
        })
        self.store.save(self.c.state)
        return task

    def reconcile(self, **overrides):
        args = dict(task_id="task", operation_id="b" * 32, expected_target=TARGET,
                    human_confirmed=True, captures_stopped=True)
        args.update(overrides)
        return self.c.reconcile(**args)

    def test_reconcile_completed_error_hands_to_human_then_requires_explicit_resume(self):
        self.completed_run()
        other = self.add_task("other", OTHER)
        before = copy.deepcopy(other)
        result = self.reconcile()
        self.assertEqual("human", result["status"])
        self.assertTrue(result["resumeRequired"])
        self.assertEqual(before, self.store.load()["tasks"]["other"])
        self.assertEqual([["tab", "list"]], [c.args[2] for c in self.agent.call_args_list])
        self.assertCode("human_lease", self.c.run, "task", ["reload"])
        self.assertCode("confirmation_required", self.c.resume, "task", False, False)
        self.c.resume("task", True, False)
        self.assertEqual("active", self.store.load()["tasks"]["task"]["status"])
        self.popen.assert_not_called()

    def test_reconcile_requires_both_declarations_and_does_not_probe_without_proof(self):
        self.completed_run()
        for flags in ({"human_confirmed": False}, {"captures_stopped": False}):
            self.assertCode("confirmation_required", self.reconcile, **flags)
        self.agent.assert_not_called()
        self.verify.assert_not_called()
        for receipt in (None, {}, {"completed": False, "outcome": "unknown"},
                        {"completed": 1, "outcome": "error"},
                        {"completed": True, "outcome": "timeout"}):
            self.c.task("task")["operation"]["last_call"] = receipt
            self.store.save(self.c.state)
            before = self.store.path.read_bytes()
            self.assertCode("completion_unproven", self.reconcile)
            self.assertEqual(before, self.store.path.read_bytes())
        self.c.task("task").pop("operation")  # Legacy tasks lack receipts.
        self.store.save(self.c.state)
        self.assertCode("completion_unproven", self.reconcile)
        self.agent.assert_not_called()
        self.verify.assert_not_called()

    def test_reconcile_rejects_stale_operation_session_browser_target_and_duplicate_owner(self):
        for field, value in (("id", "c" * 32), ("kind", "attach"),
                             ("session", "other-session"), ("browser", None),
                             ("targetId", OTHER)):
            task = self.completed_run()
            task["operation"][field] = value
            self.assertCode("completion_unproven", self.reconcile)
        self.completed_run()
        self.assertCode("completion_unproven", self.reconcile, expected_target=OTHER)
        self.add_task("duplicate", TARGET)
        self.assertCode("ownership_unverified", self.reconcile)
        self.agent.assert_not_called()
        self.verify.assert_not_called()

    def test_reconcile_rejects_human_and_auth_leases_including_stale_auth_flag(self):
        self.completed_run()
        other = self.add_task("other", OTHER, "human")
        self.assertCode("human_lease", self.reconcile)
        other.update(status="paused", auth=True)
        self.store.save(self.c.state)
        self.assertCode("auth_lease", self.reconcile)
        other.update(status="human")
        self.c.state["auth_task"] = "other"
        self.store.save(self.c.state)
        self.assertCode("auth_lease", self.reconcile)
        self.agent.assert_not_called()
        self.verify.assert_not_called()

    def test_reconcile_verifies_browser_and_pinned_target_without_retargeting(self):
        self.completed_run()
        self.verify.side_effect = bs.BrowserSessionError("stale_browser")
        self.assertCode("stale_browser", self.reconcile)
        self.agent.assert_not_called()
        self.verify.side_effect = None
        self.targets.return_value = {OTHER}
        self.assertCode("tab_gone", self.reconcile)
        self.agent.assert_not_called()
        self.targets.return_value = {TARGET, OTHER}
        self.agent.side_effect = lambda *a: [
            {"targetId": TARGET, "url": "https://example.com", "active": False},
            {"targetId": OTHER, "url": "https://example.com", "active": True}]
        self.assertCode("ownership_unverified", self.reconcile)
        self.assertEqual("uncertain", self.store.load()["tasks"]["task"]["status"])
        self.assertEqual(TARGET, self.c.task("task")["targetId"])

    def test_agent_records_known_completed_error_but_never_raw_error_or_args(self):
        task = self.completed_run()
        self.process.side_effect = None
        self.process.return_value = subprocess.CompletedProcess([], 1,
            json.dumps({"success": False, "code": "tab_gone", "error": "TOP_SECRET"}), "TOP_SECRET")
        with mock.patch.object(bs.shutil, "which", return_value="/fake/agent-browser"):
            self.assertCode("daemon_command_error", bs.Coordinator.agent,
                            self.c, BROWSER, task, ["fill", "@e1", "TOP_SECRET"])
        saved = self.store.load()
        self.assertEqual({"completed": True, "outcome": "error"},
                         saved["tasks"]["task"]["operation"]["last_call"])
        self.assertNotIn("TOP_SECRET", json.dumps(saved))
        self.assertEqual((0, 1), self.process.call_args.kwargs["accepted"])

    def test_agent_unknown_transport_errors_and_timeout_invalidate_old_completion(self):
        for response in ({"success": False, "code": "outcome_unknown", "error": "secret"},
                         {"success": False, "error": "secret"},
                         {"success": False, "code": "new_code", "error": "secret"},
                         {"success": False, "code": "tab_gone", "type": "transport", "error": "secret"}):
            task = self.completed_run()
            self.process.side_effect = None
            self.process.return_value = subprocess.CompletedProcess([], 1, json.dumps(response), "")
            with mock.patch.object(bs.shutil, "which", return_value="/fake/agent-browser"):
                self.assertCode("command_outcome_unknown" if response.get("code") == "outcome_unknown"
                                else "invalid_json", bs.Coordinator.agent,
                                self.c, BROWSER, task, ["tab", "list"])
            self.assertCode("completion_unproven", self.reconcile)
        task = self.completed_run()
        self.process.side_effect = bs.BrowserSessionError("command_timeout")
        with mock.patch.object(bs.shutil, "which", return_value="/fake/agent-browser"):
            self.assertCode("command_timeout", bs.Coordinator.agent,
                            self.c, BROWSER, task, ["tab", "list"])
        self.assertCode("completion_unproven", self.reconcile)
        self.agent.assert_not_called()

    def test_reconcile_ownership_probe_timeout_leaves_uncertain_and_cannot_retry(self):
        task = self.completed_run()
        self.agent.side_effect = lambda *a: bs.Coordinator.agent(self.c, *a)
        self.process.side_effect = bs.BrowserSessionError("command_timeout")
        with mock.patch.object(bs.shutil, "which", return_value="/fake/agent-browser"):
            self.assertCode("command_timeout", self.reconcile)
        self.assertCode("completion_unproven", self.reconcile)
        self.assertEqual("uncertain", self.store.load()["tasks"]["task"]["status"])
        self.assertEqual({"completed": False, "outcome": "unknown"}, task["operation"]["last_call"])

    def test_completed_error_from_run_can_reconcile_with_real_wrapper_and_resume(self):
        self.agent.side_effect = lambda *a: bs.Coordinator.agent(self.c, *a)
        tabs = json.dumps({"success": True, "data": {"tabs": [
            {"targetId": TARGET, "url": "https://example.com", "active": True}]}})
        self.process.side_effect = [subprocess.CompletedProcess([], 0, tabs, ""),
            subprocess.CompletedProcess([], 1,
                '{"success":false,"code":"tab_gone","error":"TOP_SECRET"}', ""),
            subprocess.CompletedProcess([], 0, tabs, ""),
            subprocess.CompletedProcess([], 0, tabs, "")]
        with mock.patch.object(bs.shutil, "which", return_value="/fake/agent-browser"):
            self.assertCode("daemon_command_error", self.c.run, "task", ["uncheck", "@e481"])
            operation_id = self.store.load()["tasks"]["task"]["operation"]["id"]
            self.reconcile(operation_id=operation_id)
            self.assertEqual("human", self.store.load()["tasks"]["task"]["status"])
            self.c.resume("task", True, False)
        self.assertEqual("active", self.store.load()["tasks"]["task"]["status"])
        self.assertEqual(1, sum(call.args[2] == ["uncheck", "@e481"]
                                for call in self.agent.call_args_list))
        self.assertNotIn("TOP_SECRET", self.store.path.read_text())

    def test_run_real_wrapper_receipt_replaced_before_postcheck_timeout(self):
        self.agent.side_effect = lambda *a: bs.Coordinator.agent(self.c, *a)
        tabs = json.dumps({"success": True, "data": {"tabs": [
            {"targetId": TARGET, "url": "https://example.com", "active": True}]}})
        self.process.side_effect = [subprocess.CompletedProcess([], 0, tabs, ""),
            subprocess.CompletedProcess([], 0, '{"success":true,"data":{}}', ""),
            bs.BrowserSessionError("command_timeout")]
        with mock.patch.object(bs.shutil, "which", return_value="/fake/agent-browser"):
            self.assertCode("command_timeout", self.c.run, "task", ["reload"])
        operation = self.c.task("task")["operation"]
        self.assertEqual({"completed": False, "outcome": "unknown"}, operation["last_call"])
        self.assertCode("completion_unproven", self.reconcile, operation_id=operation["id"])

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

    def test_reconcile_cli_routes_explicit_flags_under_project_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project = bs.Project(root, "a" * 64, False)
            with mock.patch.object(bs.sys, "platform", "darwin"), mock.patch.object(bs, "DEFAULT_STATE_DIR", root / "state"), mock.patch.object(bs, "resolve_project", return_value=project), mock.patch.object(bs, "check_profile"), mock.patch.object(bs.Coordinator, "reconcile", return_value={"status": "human"}) as reconcile:
                result, value = self.invoke(["reconcile", "--task", "task",
                    "--operation-id", "b" * 32, "--expected-target", TARGET,
                    "--human-confirmed", "--captures-stopped"])
            self.assertEqual(0, result)
            self.assertEqual("human", value["data"]["status"])
            reconcile.assert_called_once_with("task", "b" * 32, TARGET, True, True)
        for args in (["reconcile", "--task", "task"],
                     ["reconcile", "--task", "task", "--operation-id", "b" * 32]):
            with mock.patch.object(bs, "resolve_project") as resolve:
                result, value = self.invoke(args)
            self.assertEqual("invalid_arguments", value["error"]["code"])
            resolve.assert_not_called()

    def test_retire_cli_routes_explicit_discard_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project = bs.Project(root, "a" * 64, False)
            with mock.patch.object(bs.sys, "platform", "darwin"), mock.patch.object(bs, "DEFAULT_STATE_DIR", root / "state"), mock.patch.object(bs, "resolve_project", return_value=project), mock.patch.object(bs, "check_profile"), mock.patch.object(bs.Coordinator, "retire", return_value={"status": "released"}) as retire:
                result, value = self.invoke(["retire", "--task", "task",
                    "--expected-target", TARGET, "--human-confirmed", "--discard-unsaved"])
            self.assertEqual(0, result)
            self.assertEqual("released", value["data"]["status"])
            retire.assert_called_once_with("task", TARGET, True, True)
        with mock.patch.object(bs, "resolve_project") as resolve:
            result, value = self.invoke(["retire", "--task", "task"])
        self.assertEqual("invalid_arguments", value["error"]["code"])
        resolve.assert_not_called()

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
