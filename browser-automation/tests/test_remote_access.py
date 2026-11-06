"""Offline remote-access contracts; no deployment, services, or real dashboard starts."""
from pathlib import Path
import copy
import hashlib
import importlib.util
import io
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "remote_access.py"
SPEC = importlib.util.spec_from_file_location("remote_access_tests", SOURCE)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)
REAL_RUN = subprocess.run
CADDY = shutil.which("caddy")
SECRET = "a7" * 32


class RemoteAccessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.root.chmod(0o700)
        self.bundle = self.root / "bundle"
        self.manifest = {
            "version": 1, "hubOrigin": "https://hub.private.test",
            "bindAddress": "100.64.10.2", "allowedPeers": ["100.64.10.0/24", "fd12::/64"],
            "tls": {"mode": "internal"}, "projects": [
                {"id": "alpha", "root": str(self.root / "project"),
                 "origin": "https://alpha.private.test", "dashboardPort": 9223}],
        }
        self.helper = SimpleNamespace(
            resolve_project=mock.Mock(side_effect=lambda path: SimpleNamespace(
                root=Path(path).resolve(), key=hashlib.sha256(str(path).encode()).hexdigest())),
            child_environment=mock.Mock(return_value={
                "AGENT_BROWSER_SOCKET_DIR": str(self.root / "sockets" / "alpha")}),
            verify_listener=mock.Mock(),
        )
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(mod, "sibling", return_value=self.helper).start()
        # Every process call is blocked unless a test explicitly supplies a fake.
        self.process = mock.patch.object(mod.subprocess, "run", side_effect=AssertionError(
            "Unexpected external process")).start()
        mock.patch.object(mod.shutil, "which", side_effect=lambda name, **kw: "/fake/" + name).start()

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(mod.RemoteAccessError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(SECRET, str(caught.exception))

    def prepare(self):
        with mock.patch.object(mod.subprocess, "run", return_value=SimpleNamespace(returncode=128)):
            return mod.prepare_bundle(self.manifest, self.bundle)

    def response(self, url=None, **changes):
        data = {"port": 9223, "pid": 421, "access_urls": [url or (
            self.manifest["projects"][0]["origin"] + "/#dashboard-access-token=" + SECRET)]}
        data.update(changes)
        return json.dumps({"success": True, "data": data})

    def start(self, response=None):
        with mock.patch.object(mod.sys, "platform", "darwin"), \
                mock.patch.object(mod.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=response or self.response(), stderr="")) as process:
            result = mod.start_dashboard(self.bundle, "alpha", allow_start=True)
        return result, process

    def test_strict_json_rejects_duplicate_keys_nested_constants_and_invalid_json(self):
        for raw in ('{"version":1,"version":1}', '{"tls":{"mode":1,"mode":2}}',
                    'NaN', 'Infinity', '{', b'\xff'):
            with self.subTest(raw=raw):
                self.error("invalid_manifest", mod.strict_json, raw)

    def test_unknown_and_missing_fields_rejected_at_each_schema_level(self):
        for target in ("top", "tls", "project"):
            for operation in ("extra", "missing"):
                value = copy.deepcopy(self.manifest)
                obj = value if target == "top" else value["tls"] if target == "tls" else value["projects"][0]
                if operation == "extra":
                    obj["unexpected"] = True
                else:
                    del obj[next(iter(obj))]
                with self.subTest(target=target, operation=operation):
                    self.error("invalid_manifest", mod.validate_manifest, value)

    def test_injection_strings_never_reach_rendering(self):
        for bad in ("\nrespond hacked", "$ENV", "{\nadmin off\n}", "x\rbind 0.0.0.0", "x\x00"):
            for field in ("hubOrigin", "bindAddress", "root", "id", "peer", "certificate"):
                value = copy.deepcopy(self.manifest)
                if field in ("root", "id"):
                    value["projects"][0][field] = bad
                elif field == "peer":
                    value["allowedPeers"] = [bad]
                elif field == "certificate":
                    value["tls"] = {"mode": "existing", "certificate": "/tmp/" + bad,
                                    "privateKey": "/tmp/key"}
                else:
                    value[field] = bad
                with self.subTest(field=field, bad=bad):
                    self.error("invalid_manifest", mod.validate_manifest, value)

    def test_origins_are_https_dns_only_and_canonical(self):
        self.assertEqual(mod.origin("https://Alpha.private.test:443/"), "https://alpha.private.test")
        self.assertEqual(mod.origin("https://K.EXAMPLE"), "https://k.example")
        for text in ("http://alpha.private.test", "https://localhost", "https://a.localhost",
                     "https://*.private.test", "https://127.0.0.1", "https://[fd12::1]",
                     "https://K.example",
                     "https://alpha.private.test.", "https://u:p@alpha.private.test",
                     "https://alpha.private.test/path", "https://alpha.private.test?",
                     "https://alpha.private.test#", "https://alpha.private.test:80"):
            with self.subTest(text=text):
                self.error("invalid_manifest", mod.origin, text)

    def test_hostname_not_ports_is_cookie_isolation_boundary(self):
        for origin in ("https://hub.private.test:8443", "https://alpha.private.test:8443"):
            value = copy.deepcopy(self.manifest)
            value["projects"].append({"id": "beta", "root": str(self.root / "other"),
                                      "origin": origin, "dashboardPort": 9224})
            self.error("invalid_manifest", mod.validate_manifest, value)

    def test_worktrees_resolving_to_same_canonical_root_are_rejected(self):
        canonical = self.root / "main"
        self.helper.resolve_project.side_effect = lambda _: SimpleNamespace(root=canonical, key="key")
        value = copy.deepcopy(self.manifest)
        value["projects"].append({"id": "beta", "root": str(self.root / "worktree"),
                                  "origin": "https://beta.private.test", "dashboardPort": 9224})
        self.error("invalid_manifest", mod.validate_manifest, value)

    def test_canonical_root_replaces_worktree_input_without_git(self):
        self.helper.resolve_project.side_effect = lambda _: SimpleNamespace(root=self.root / "main", key="key")
        self.assertEqual(mod.validate_manifest(self.manifest)["projects"][0]["root"],
                         str(self.root / "main"))
        self.process.assert_not_called()

    def test_allowed_address_accepts_only_explicit_private_ranges(self):
        for address in ("10.1.2.3", "172.16.0.1", "172.31.255.254", "192.168.1.1",
                        "100.64.0.1", "100.127.255.254", "fc00::1", "fdff::1"):
            self.assertEqual(mod.allowed_address(address), address)
        for network in ("10.1.0.0/16", "172.16.0.0/24", "192.168.0.0/24",
                        "100.64.0.0/24", "fc00::/64", "fd12::/64"):
            self.assertEqual(mod.allowed_address(network, network=True), network)

    def test_public_wildcard_loopback_multicast_linklocal_and_boundary_addresses_denied(self):
        for address in ("0.0.0.0", "::", "127.0.0.1", "::1", "8.8.8.8", "169.254.1.2",
                        "fe80::1", "ff02::1", "224.0.0.1", "100.63.255.255", "100.128.0.0",
                        "172.15.255.255", "172.32.0.0", "192.169.0.1", "fbff::1", "fe00::1"):
            with self.subTest(address=address):
                self.error("invalid_manifest", mod.allowed_address, address)
                self.error("invalid_manifest", mod.allowed_address,
                           address + ("/128" if ":" in address else "/32"), network=True)
        for network in ("0.0.0.0/0", "::/0", "10.1.2.3/24", "fd12::1%en0/128", "10.1.2.3"):
            self.error("invalid_manifest", mod.allowed_address, network, network=True)

    def test_ports_types_ranges_and_all_collision_classes(self):
        for value in (True, False, 443, 1023, 65536, "9223", 9223.0):
            self.error("invalid_manifest", mod.high_port, value)
        for target in ("admin-dashboard", "admin-public", "dashboard-public", "duplicate-dashboard"):
            value = copy.deepcopy(self.manifest)
            if target == "admin-dashboard":
                value["caddyAdminPort"] = 9223
            elif target == "admin-public":
                value["hubOrigin"] += ":2029"
            elif target == "dashboard-public":
                value["hubOrigin"] += ":9223"
            else:
                value["projects"].append({"id": "beta", "root": str(self.root / "other"),
                                          "origin": "https://beta.private.test", "dashboardPort": 9223})
            self.error("invalid_manifest", mod.validate_manifest, value)

    def test_existing_certificate_paths_are_metadata_not_key_reads(self):
        self.manifest["tls"] = {"mode": "existing", "certificate": str(self.root / "absent.pem"),
                                "privateKey": str(self.root / "absent-key.pem")}
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("key read")), \
                mock.patch.object(Path, "read_text", side_effect=AssertionError("key read")):
            self.prepare()
        text = (self.bundle / "Caddyfile.sites").read_text()
        self.assertIn('tls "' + self.manifest["tls"]["certificate"] + '" "' +
                      self.manifest["tls"]["privateKey"] + '"', text)

    def test_prepare_private_modes_and_integrity_roundtrip(self):
        self.assertEqual(self.prepare()["status"], "prepared")
        for path in (self.bundle, *self.bundle.rglob("*")):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        self.assertEqual(mod.load_bundle(self.bundle)["manifest"]["bindAddress"], "100.64.10.2")

    def test_prepare_rejects_nonempty_directory_without_changes(self):
        self.bundle.mkdir(mode=0o700)
        marker = self.bundle / "keep"
        marker.write_text("unchanged")
        with mock.patch.object(mod, "outside_git"):
            self.error("unsafe_path", mod.prepare_bundle, self.manifest, self.bundle)
        self.assertEqual(marker.read_text(), "unchanged")

    def test_prepare_rejects_symlink_and_git_output(self):
        self.bundle.symlink_to(self.root, target_is_directory=True)
        self.error("unsafe_path", mod.prepare_bundle, self.manifest, self.bundle)
        self.bundle.unlink()
        with mock.patch.object(mod.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as process:
            self.error("output_in_git", mod.prepare_bundle, self.manifest, self.bundle)
        self.assertIn("rev-parse", process.call_args.args[0])
        self.assertFalse(self.bundle.exists())

    def test_bundle_equal_to_or_containing_project_is_rejected(self):
        with mock.patch.object(mod, "outside_git"):
            for output in (self.root, self.root / "project"):
                self.error("invalid_manifest", mod.prepare_bundle, self.manifest, output)

    def test_manifest_version_boolean_and_duplicate_peers_rejected(self):
        for version in (True, False, 0, 2, "1"):
            value = copy.deepcopy(self.manifest)
            value["version"] = version
            self.error("invalid_manifest", mod.validate_manifest, value)
        value = copy.deepcopy(self.manifest)
        value["allowedPeers"] *= 2
        self.error("invalid_manifest", mod.validate_manifest, value)

    def test_rendered_sites_each_have_explicit_private_scope(self):
        value = mod.validate_manifest(self.manifest)
        text = mod.render_caddy(value, self.bundle, standalone=True)
        self.assertIn("skip_install_trust", text)
        self.assertIn("auto_https disable_redirects", text)
        self.assertIn("protocols h1 h2", text)
        self.assertIn("admin 127.0.0.1:2029", text)
        for directive in ('bind "100.64.10.2"', 'remote_ip "100.64.10.0/24" "fd12::/64"',
                          "tls internal", 'respond "Forbidden" 403'):
            self.assertEqual(text.count(directive), 2)
        self.assertIn("reverse_proxy 127.0.0.1:9223", text)
        for forbidden in ("header_up", "Origin", "Host", "devtools", "9341", "HAR", "log_credentials"):
            self.assertNotIn(forbidden, text)
        self.assertFalse(mod.render_caddy(value, self.bundle).startswith("{"))

    def test_hub_links_are_only_nonsecret_project_origins(self):
        text = mod.render_hub(mod.validate_manifest(self.manifest))
        self.assertIn('href="https://alpha.private.test"', text)
        for forbidden in (str(self.root), SECRET, "dashboard-access-token", "9223", "socket", "devtools"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(text.count("href="), 1)

    def test_launchd_is_template_with_no_keepalive_token_or_install(self):
        plist = plistlib.loads(mod.render_launchd(self.manifest["projects"][0], self.bundle))
        self.assertNotIn("KeepAlive", plist)
        self.assertEqual(plist["StartInterval"], 60)
        self.assertEqual(plist["Umask"], 0o077)
        self.assertIn("--allow-start", plist["ProgramArguments"])
        self.assertIn("dashboard-start", plist["ProgramArguments"])
        self.assertEqual(set(plist["EnvironmentVariables"]), {"HOME", "PATH", "LANG"})
        for forbidden in (SECRET, "dashboard-access-token", "launchctl", "bootstrap", "install", "Chrome"):
            self.assertNotIn(forbidden, str(plist))
        self.process.assert_not_called()

    def test_hash_and_generated_content_tampering_fail_closed(self):
        self.prepare()
        for name in ("Caddyfile.sites", "hub/index.html", "plan.json", "integrity.json", "agent-browser-empty.json"):
            path = self.bundle / name
            old = path.read_bytes()
            path.write_bytes(old + b"\n# tampered")
            self.error("invalid_bundle", mod.load_bundle, self.bundle)
            path.write_bytes(old)
        mod.load_bundle(self.bundle)

    def test_world_readable_or_symlink_bundle_files_rejected(self):
        self.prepare()
        path = self.bundle / "manifest.json"
        path.chmod(0o644)
        self.error("invalid_bundle", mod.load_bundle, self.bundle)
        path.chmod(0o600)
        old = path.read_bytes()
        path.unlink()
        elsewhere = self.root / "manifest"
        elsewhere.write_bytes(old)
        elsewhere.chmod(0o600)
        path.symlink_to(elsewhere)
        self.error("invalid_bundle", mod.load_bundle, self.bundle)

    def test_dashboard_start_requires_guard_before_bundle_or_process_access(self):
        with mock.patch.object(mod, "load_bundle") as load:
            self.error("approval_required", mod.start_dashboard, self.bundle, "alpha")
            load.assert_not_called()
        self.process.assert_not_called()
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            self.assertEqual(mod.main(["dashboard-start", "--bundle", str(self.bundle), "--project", "alpha"]), 1)
        self.assertEqual(json.loads(output.getvalue())["code"], "approval_required")

    def test_dashboard_start_exact_socket_config_clean_environment_and_no_secret_argv(self):
        self.prepare()
        with mock.patch.dict(os.environ, {"AGENT_BROWSER_ACCESS_TOKEN": SECRET,
                                         "NODE_OPTIONS": "--inspect", "HTTPS_PROXY": "http://proxy",
                                         "AGENT_BROWSER_SOCKET_DIR": "/wrong", "API_KEY": SECRET}):
            result, process = self.start()
        args = process.call_args.args[0]
        self.assertEqual(args, ["/fake/agent-browser", "--config", str(self.bundle / "agent-browser-empty.json"),
                               "--json", "dashboard", "start", "--port", "9223",
                               "--allowed-origins", "https://alpha.private.test"])
        kw = process.call_args.kwargs
        self.assertEqual(kw["env"]["AGENT_BROWSER_SOCKET_DIR"], str(self.root / "sockets" / "alpha"))
        self.assertLessEqual(set(kw["env"]), {"HOME", "PATH", "LANG", "TMPDIR", "LC_ALL", "LC_CTYPE",
                                             "LC_MESSAGES", "AGENT_BROWSER_SOCKET_DIR"})
        self.assertEqual((self.bundle / "agent-browser-empty.json").read_text(), "{}\n")
        self.assertEqual(kw["cwd"], self.bundle)
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertNotIn(SECRET, str((args, kw, result)))
        self.assertTrue(result["ready"])
        target = self.bundle / "bootstrap" / "alpha.txt"
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.read_text(), json.loads(self.response())["data"]["access_urls"][0] + "\n")

    def test_prepared_non_system_tools_survive_launchd_path_without_rediscovery(self):
        tools = {"agentBrowser": str(self.root / "user-bin" / "agent-browser"),
                 "node": str(self.root / "node-bin" / "node")}
        approved_path = os.pathsep.join([str(self.root / "node-bin"),
                                         str(self.root / "user-bin"),
                                         "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
        discovery_path = os.pathsep.join([str(self.root / "user-bin"),
                                          str(self.root / "node-bin"), os.defpath])

        def discover(name, *, path):
            if name == "git":
                return "/fake/git"
            self.assertEqual(path, discovery_path)
            return tools[{"agent-browser": "agentBrowser", "node": "node"}[name]]

        with mock.patch.dict(os.environ, {"PATH": discovery_path}), \
                mock.patch.object(mod.shutil, "which", side_effect=discover):
            self.prepare()
        plan = json.loads((self.bundle / "plan.json").read_text())
        self.assertEqual(plan["executables"], tools)
        plist = plistlib.loads((self.bundle / "launchd" / "alpha.plist").read_bytes())
        self.assertEqual(plist["EnvironmentVariables"]["PATH"], approved_path)
        with mock.patch.dict(os.environ, {"PATH": os.defpath}), \
                mock.patch.object(mod.shutil, "which", side_effect=AssertionError("Runtime rediscovery")):
            loaded = mod.load_bundle(self.bundle)
            result, process = self.start()
        self.assertEqual(loaded["plan"]["executables"], tools)
        self.assertEqual(process.call_args.args[0][0], tools["agentBrowser"])
        self.assertEqual(process.call_args.kwargs["env"]["PATH"], approved_path)
        self.assertTrue(result["ready"])
        self.assertNotIn(SECRET, str(process.call_args))

    def test_missing_prepared_agent_browser_fails_closed_without_rediscovery(self):
        with mock.patch.object(mod.shutil, "which", side_effect=lambda name, **kw:
                               "/fake/git" if name == "git" else None):
            self.prepare()
        self.assertEqual(mod.load_bundle(self.bundle)["plan"]["executables"],
                         {"agentBrowser": None, "node": None})
        with mock.patch.object(mod.sys, "platform", "darwin"), \
                mock.patch.object(mod.shutil, "which", side_effect=AssertionError("Runtime rediscovery")):
            self.error("command_failed", mod.start_dashboard, self.bundle, "alpha", allow_start=True)
        self.process.assert_not_called()
        self.assertEqual(list((self.bundle / "bootstrap").iterdir()), [])

    def test_saved_executable_path_tampering_fails_integrity(self):
        self.prepare()
        path = self.bundle / "plan.json"
        original = path.read_bytes()
        for key in ("agentBrowser", "node"):
            plan = json.loads(original)
            plan["executables"][key] = str(self.root / "unapproved" / key)
            path.write_text(mod.json_text(plan))
            self.error("invalid_bundle", mod.load_bundle, self.bundle)
        path.write_bytes(original)
        mod.load_bundle(self.bundle)

    def test_null_saved_executables_fail_without_discovery(self):
        self.prepare()
        path = self.bundle / "plan.json"
        plan = json.loads(path.read_text())
        plan["executables"] = None
        path.write_text(mod.json_text(plan))
        with mock.patch.object(mod.shutil, "which", side_effect=AssertionError("Unexpected discovery")):
            self.error("invalid_bundle", mod.load_bundle, self.bundle)
        self.process.assert_not_called()

    def test_reviewed_runtime_path_deduplicates_and_rejects_relative_tools(self):
        self.assertEqual(mod.reviewed_runtime_path({"agentBrowser": "/approved/bin/agent-browser",
                                                   "node": "/approved/bin/node"}),
                         "/approved/bin:/usr/bin:/bin:/usr/sbin:/sbin")
        for tools in ({"agentBrowser": "relative/agent-browser", "node": None},
                      {"agentBrowser": None, "node": "/tmp/node\n/bin"},
                      {"agentBrowser": None, "node": None, "extra": "/bin/tool"}):
            self.error("invalid_manifest", mod.approved_executables, tools)

    def test_response_accepts_exact_root_fragment_and_reuse_envelope(self):
        for slash in ("", "/"):
            url = "https://alpha.private.test" + slash + "#dashboard-access-token=" + SECRET
            parsed = mod.parse_dashboard_response(self.response(url, already_running=True),
                                                  "https://alpha.private.test", 9223)
            self.assertEqual(parsed, {"url": url, "pid": 421, "port": 9223})

    def test_response_rejects_cross_origin_query_credentials_paths_and_wrong_token(self):
        good = "https://alpha.private.test"
        token = "#dashboard-access-token=" + SECRET
        for url in ("https://beta.private.test/" + token, good + ":8443/" + token,
                    "http://alpha.private.test/" + token, "https://u:p@alpha.private.test/" + token,
                    good + "/path" + token, good + "/?" + token, good + "/?token=secret" + token,
                    good + "/#dashboard-access-token=short", good + "/" + token + "&extra=1",
                    good + "/" + token + "\n"):
            self.error("invalid_response", mod.parse_dashboard_response, self.response(url), good, 9223)

    def test_response_requires_exact_typed_envelope(self):
        good = "https://alpha.private.test"
        for changes in ({"port": True}, {"port": 9224}, {"pid": True}, {"pid": 0},
                        {"access_urls": []}, {"access_urls": [good, good]},
                        {"access_urls": [{"url": good}]}, {"already_running": False}, {"extra": SECRET}):
            self.error("invalid_response", mod.parse_dashboard_response, self.response(**changes), good, 9223)
        for raw in ('{}', '{"success":false,"data":{}}', self.response().replace('"success": true',
                    '"success": true, "unexpected": true')):
            self.error("invalid_response", mod.parse_dashboard_response, raw, good, 9223)

    def test_start_errors_and_cli_output_do_not_disclose_raw_bearer(self):
        self.prepare()
        for outcome in (SimpleNamespace(returncode=1, stdout=SECRET, stderr=SECRET),
                        SimpleNamespace(returncode=0, stdout=SECRET, stderr=SECRET),
                        subprocess.TimeoutExpired(["agent-browser"], 45, output=SECRET, stderr=SECRET)):
            output = io.StringIO()
            with mock.patch.object(mod.sys, "platform", "darwin"), \
                    mock.patch.object(mod.subprocess, "run", **(
                        {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome})), \
                    mock.patch("sys.stdout", output):
                self.assertEqual(mod.main(["dashboard-start", "--bundle", str(self.bundle),
                                           "--project", "alpha", "--allow-start"]), 1)
            self.assertNotIn(SECRET, output.getvalue())
        with mock.patch.object(mod.sys, "platform", "darwin"), \
                mock.patch.object(mod.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=self.response(), stderr=SECRET)) as process, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(mod.main(["dashboard-start", "--bundle", str(self.bundle),
                                       "--project", "alpha", "--allow-start"]), 0)
        self.assertNotIn(SECRET, output.getvalue())
        self.assertNotIn(SECRET, str(process.call_args))

    def test_atomic_bootstrap_write_preserves_previous_on_failure(self):
        self.prepare()
        self.start()
        target = self.bundle / "bootstrap" / "alpha.txt"
        old = target.read_bytes()
        with mock.patch.object(mod.os, "replace", side_effect=OSError("synthetic")):
            self.error("unsafe_path", self.start, self.response(
                "https://alpha.private.test/#dashboard-access-token=" + "b8" * 32))
        self.assertEqual(target.read_bytes(), old)
        self.assertEqual(list(target.parent.glob(".write-*")), [])
        with mock.patch.object(mod.os, "replace", wraps=os.replace) as replace:
            self.start()
        source, destination = replace.call_args.args
        self.assertEqual(Path(source).parent, target.parent)
        self.assertEqual(destination, target)

    def test_invalid_response_does_not_overwrite_existing_bootstrap(self):
        self.prepare()
        self.start()
        target = self.bundle / "bootstrap" / "alpha.txt"
        old = target.read_bytes()
        self.error("invalid_response", self.start, self.response("https://other.private.test/#dashboard-access-token=" + SECRET))
        self.assertEqual(target.read_bytes(), old)

    def test_readiness_is_bounded_mocked_listener_verification(self):
        with mock.patch.object(mod.time, "sleep") as sleep:
            self.helper.verify_listener.side_effect = [RuntimeError(), RuntimeError(), None]
            self.assertTrue(mod.listener_ready(421, 9223))
            self.assertEqual(self.helper.verify_listener.call_args_list, [mock.call(421, 9223)] * 3)
            self.assertEqual(sleep.call_args_list, [mock.call(0.2), mock.call(0.5)])
            self.helper.verify_listener.side_effect = RuntimeError()
            self.assertFalse(mod.listener_ready(421, 9223))
        self.process.assert_not_called()

    def test_adapt_exact_parser_only_argv_and_suppressed_config(self):
        self.prepare()
        with mock.patch.object(mod, "run", return_value=SimpleNamespace(
                stdout=json.dumps({"secret": SECRET}), stderr="WARN synthetic\n")) as run:
            result = mod.adapt_bundle(self.bundle, "/fake/caddy")
        run.assert_called_once_with(["/fake/caddy", "adapt", "--config", str(self.bundle / "Caddyfile.standalone"),
                                     "--adapter", "caddyfile"], cwd=self.bundle)
        self.assertEqual(result, {"status": "parsed", "parserWarnings": 1, "qualificationRequired": True})
        self.assertNotIn(SECRET, str(result))
        self.process.assert_not_called()

    def test_run_uses_clean_environment_and_suppresses_failure_details(self):
        with mock.patch.object(mod.subprocess, "run", return_value=SimpleNamespace(returncode=1,
                stdout=SECRET, stderr=SECRET)) as process:
            self.error("command_failed", mod.run, ["/fake/caddy", "adapt"], cwd=self.root)
        self.assertEqual(process.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertFalse(process.call_args.kwargs["check"])
        with mock.patch.object(mod.subprocess, "run", side_effect=subprocess.TimeoutExpired("caddy", 30, output=SECRET)):
            self.error("command_timeout", mod.run, ["/fake/caddy", "adapt"], cwd=self.root)

    @unittest.skipUnless(CADDY, "Optional installed Caddy parser not available")
    def test_installed_caddy_adapt_disposable_standalone_only(self):
        self.prepare()
        # Explicitly permit only this parser invocation: no --validate or runtime listener.
        args = [CADDY, "adapt", "--config", str(self.bundle / "Caddyfile.standalone"), "--adapter", "caddyfile"]
        result = REAL_RUN(args, cwd=self.bundle, env=mod.clean_environment(), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, "Standalone Caddyfile parser failed")
        self.assertIsInstance(json.loads(result.stdout), dict)


if __name__ == "__main__":
    unittest.main()
