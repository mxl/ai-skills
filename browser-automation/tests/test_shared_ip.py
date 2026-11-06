"""Offline shared-IP contracts: fake policy/tools, disposable bundles, no services."""
from pathlib import Path
import copy
import hashlib
import importlib.util
import io
import json
import os
import plistlib
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "remote_access.py"
SPEC = importlib.util.spec_from_file_location("shared_ip_remote_access_tests", SOURCE)
remote = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(remote)
SECRET = "a7" * 32


class SharedIPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.bundle = self.root / "bundle"
        self.socket = self.root / "sockets" / "shared"
        self.manifest = {
            "version": 1, "mode": "shared-ip",
            "hubOrigin": "https://100.64.10.2:8443", "bindAddress": "100.64.10.2",
            "allowedPeers": ["100.64.10.0/24", "fd12::/64"],
            "tls": {"mode": "internal"}, "projects": [
                {"id": name, "root": str(self.root / name),
                 "origin": "https://100.64.10.2:8443", "dashboardPort": 9223}
                for name in ("alpha", "beta", "gamma")],
        }
        self.helper = SimpleNamespace(
            resolve_project=mock.Mock(side_effect=lambda path: SimpleNamespace(
                root=Path(path).resolve(), key=hashlib.sha256(str(path).encode()).hexdigest())),
            child_environment=mock.Mock(return_value={"AGENT_BROWSER_SOCKET_DIR": str(self.socket)}),
            verify_listener=mock.Mock(),
        )
        # Never import the real sibling or consult global runtime enrollment/policy.
        self.patch(remote, "sibling", return_value=self.helper)
        self.process = self.patch(remote.subprocess, "run", side_effect=AssertionError(
            "Unexpected external process"))
        self.patch(remote.shutil, "which", side_effect=lambda name, **kwargs: "/fake/" + name)

    def patch(self, target, attribute, **kwargs):
        patcher = mock.patch.object(target, attribute, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(remote.RemoteAccessError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(SECRET, str(caught.exception))

    def prepare(self):
        # Only the repository-detection probe is simulated here.
        with mock.patch.object(remote.subprocess, "run", return_value=SimpleNamespace(returncode=128)):
            return remote.prepare_bundle(self.manifest, self.bundle)

    def response(self, url=None, **changes):
        data = {"port": 9223, "pid": 421, "access_urls": [url or (
            self.manifest["hubOrigin"] + "/#dashboard-access-token=" + SECRET)]}
        data.update(changes)
        return json.dumps({"success": True, "data": data})

    def start(self, member="alpha", response=None):
        with mock.patch.object(remote.sys, "platform", "darwin"), \
                mock.patch.object(remote.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=response or self.response(), stderr="")) as process:
            result = remote.start_dashboard(self.bundle, member, allow_start=True)
        return result, process

    def test_omitted_mode_preserves_hostname_manifest_and_rendering(self):
        value = copy.deepcopy(self.manifest)
        del value["mode"]
        value["hubOrigin"] = "https://hub.private.test"
        for index, project in enumerate(value["projects"]):
            project["origin"] = "https://" + project["id"] + ".private.test"
            project["dashboardPort"] += index
        normalized = remote.validate_manifest(value)
        expected = {**value, "caddyAdminPort": 2029}
        self.assertEqual(normalized, expected)
        self.assertNotIn("mode", normalized)
        text = remote.render_caddy(normalized, self.bundle)
        self.assertEqual(text.count("reverse_proxy "), 3)
        self.assertEqual(text.count("file_server"), 1)
        explicit = remote.validate_manifest({**value, "mode": "project-hostnames"})
        self.assertEqual(remote.render_caddy(explicit, self.bundle), text)
        self.process.assert_not_called()

    def test_origin_default_remains_dns_only(self):
        self.assertEqual(remote.origin("https://Alpha.private.test:443/"), "https://alpha.private.test")
        for value in ("https://100.64.10.2", "https://[fd12::1]"):
            with self.subTest(value=value):
                self.error("invalid_manifest", remote.origin, value)
                self.error("invalid_manifest", remote.origin, value, allow_ip=False)

    def test_ip_manifest_requires_explicit_shared_mode(self):
        for mode in (None, "project-hostnames", "shared", True, False):
            value = copy.deepcopy(self.manifest)
            if mode is None:
                del value["mode"]
            else:
                value["mode"] = mode
            with self.subTest(mode=mode):
                self.error("invalid_manifest", remote.validate_manifest, value)

    def test_private_ipv4_and_ipv6_exact_bind_and_origin_are_supported(self):
        for address in ("10.1.2.3", "172.16.0.2", "192.168.1.2", "100.64.10.2", "fd12::1"):
            for port in ("", ":8443"):
                value = copy.deepcopy(self.manifest)
                host = "[" + address + "]" if ":" in address else address
                value["bindAddress"] = address
                value["hubOrigin"] = "https://" + host + port
                for project in value["projects"]:
                    project["origin"] = value["hubOrigin"]
                with self.subTest(address=address, port=port):
                    normalized = remote.validate_manifest(value)
                    self.assertEqual(normalized["bindAddress"], address)
                    self.assertEqual(normalized["hubOrigin"], value["hubOrigin"])
                    self.assertEqual(remote.origin(value["hubOrigin"], allow_ip=True), value["hubOrigin"])

    def test_public_loopback_and_other_unsafe_ips_are_rejected_even_with_opt_in(self):
        for address in ("8.8.8.8", "127.0.0.1", "::1", "::", "0.0.0.0", "fe80::1", "169.254.1.2"):
            value = copy.deepcopy(self.manifest)
            host = "[" + address + "]" if ":" in address else address
            value["bindAddress"] = address
            value["hubOrigin"] = "https://" + host
            for project in value["projects"]:
                project["origin"] = value["hubOrigin"]
            with self.subTest(address=address):
                self.error("invalid_manifest", remote.origin, value["hubOrigin"], allow_ip=True)
                self.error("invalid_manifest", remote.validate_manifest, value)

    def test_shared_hub_must_match_exact_private_bind_not_a_domain(self):
        for hub in ("https://100.64.10.3:8443", "https://[fd12::1]:8443", "https://hub.private.test:8443"):
            value = copy.deepcopy(self.manifest)
            value["hubOrigin"] = hub
            for project in value["projects"]:
                project["origin"] = hub
            with self.subTest(hub=hub):
                self.error("invalid_manifest", remote.validate_manifest, value)

    def test_every_project_must_use_one_origin_and_dashboard_port(self):
        for index in range(3):
            for field, changed in (("origin", "https://100.64.10.3:8443"),
                                   ("origin", "https://100.64.10.2:8444"),
                                   ("origin", "https://beta.private.test:8443"),
                                   ("dashboardPort", 9224)):
                value = copy.deepcopy(self.manifest)
                value["projects"][index][field] = changed
                with self.subTest(index=index, field=field, changed=changed):
                    self.error("invalid_manifest", remote.validate_manifest, value)

    def test_boolean_ports_and_listener_collisions_are_rejected(self):
        for boolean in (True, False):
            for field in ("caddyAdminPort", "dashboardPort"):
                value = copy.deepcopy(self.manifest)
                target = value if field == "caddyAdminPort" else value["projects"][0]
                target[field] = boolean
                with self.subTest(field=field, boolean=boolean):
                    self.error("invalid_manifest", remote.validate_manifest, value)
        for admin, dashboard in ((9223, 9223), (8443, 9223), (2029, 8443)):
            value = copy.deepcopy(self.manifest)
            value["caddyAdminPort"] = admin
            for project in value["projects"]:
                project["dashboardPort"] = dashboard
            self.error("invalid_manifest", remote.validate_manifest, value)

    def test_duplicate_canonical_roots_fail_even_for_different_member_paths(self):
        self.helper.resolve_project.side_effect = lambda _: SimpleNamespace(root=self.root / "main", key="same")
        self.error("invalid_manifest", remote.validate_manifest, self.manifest)
        self.process.assert_not_called()

    def test_plan_uses_policy_selected_shared_socket_for_every_member(self):
        manifest = remote.validate_manifest(self.manifest)
        plan = remote.make_plan(manifest, self.bundle)
        self.assertEqual(len(plan["projects"]), 3)
        self.assertEqual({project["socketDir"] for project in plan["projects"]}, {str(self.socket)})
        self.assertEqual(len({project["key"] for project in plan["projects"]}), 3)
        self.assertEqual(self.helper.child_environment.call_count, 3)
        self.process.assert_not_called()
        self.assertFalse(self.socket.exists())

    def test_plan_rejects_any_member_not_enrolled_in_the_same_socket_directory(self):
        manifest = remote.validate_manifest(self.manifest)
        for outsider in range(3):
            environments = [{"AGENT_BROWSER_SOCKET_DIR": str(
                self.root / "sockets" / "other" if index == outsider else self.socket)}
                for index in range(3)]
            self.helper.child_environment.side_effect = environments
            with self.subTest(outsider=outsider):
                self.error("invalid_project", remote.make_plan, manifest, self.bundle)
        self.assertFalse(self.socket.exists())
        self.process.assert_not_called()

    def test_bundle_reload_rejects_changed_runtime_enrollment(self):
        self.prepare()
        self.helper.child_environment.side_effect = [
            {"AGENT_BROWSER_SOCKET_DIR": str(self.socket)},
            {"AGENT_BROWSER_SOCKET_DIR": str(self.socket)},
            {"AGENT_BROWSER_SOCKET_DIR": str(self.root / "sockets" / "other")},
        ]
        self.error("invalid_bundle", remote.load_bundle, self.bundle)
        self.process.assert_not_called()

    def test_generated_caddy_has_one_ip_site_and_one_proxy_no_hub_or_raw_cdp(self):
        self.prepare()
        for name in ("Caddyfile.sites", "Caddyfile.standalone"):
            text = (self.bundle / name).read_text()
            with self.subTest(name=name):
                self.assertEqual(text.count('"' + self.manifest["hubOrigin"] + '" {'), 1)
                self.assertEqual(text.count("reverse_proxy "), 1)
                self.assertIn("reverse_proxy 127.0.0.1:9223", text)
                self.assertEqual(text.count('bind "100.64.10.2"'), 1)
                self.assertEqual(text.count('remote_ip "100.64.10.0/24" "fd12::/64"'), 1)
                self.assertEqual(text.count('respond "Forbidden" 403'), 1)
                for forbidden in ("file_server", "root *", "/hub", "devtools", "9341", "9222",
                                  "header_up", "log_credentials", "dashboard-access-token", SECRET):
                    self.assertNotIn(forbidden, text)
        self.process.assert_not_called()

    def test_one_shared_launchagent_template_never_installs_or_launches_chrome(self):
        self.prepare()
        templates = list((self.bundle / "launchd").iterdir())
        self.assertEqual([path.name for path in templates], ["shared.plist"])
        plist = plistlib.loads(templates[0].read_bytes())
        args = plist["ProgramArguments"]
        self.assertEqual(args[args.index("--project") + 1], "alpha")
        self.assertIn("dashboard-start", args)
        self.assertIn("--allow-start", args)
        self.assertNotIn("KeepAlive", plist)
        self.assertEqual(plist["StartInterval"], 60)
        self.assertEqual(plist["Umask"], 0o077)
        for forbidden in ("launchctl", "Chrome", "dashboard-access-token", SECRET):
            self.assertNotIn(forbidden, str(plist))
        self.process.assert_not_called()

    def test_any_member_starts_same_instance_and_writes_only_shared_bootstrap(self):
        self.prepare()
        for member in ("gamma", "beta", "alpha"):
            with self.subTest(member=member):
                result, process = self.start(member)
                target = self.bundle / "bootstrap" / "shared.txt"
                self.assertEqual(result["bootstrapFile"], str(target))
                self.assertEqual(result["projectId"], member)
                self.assertTrue(result["ready"])
                self.assertEqual(target.read_text(), json.loads(self.response())["data"]["access_urls"][0] + "\n")
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                self.assertEqual([path.name for path in target.parent.iterdir()], ["shared.txt"])
                self.assertEqual(process.call_args.args[0], [
                    "/fake/agent-browser", "--config", str(self.bundle / "agent-browser-empty.json"),
                    "--json", "dashboard", "start", "--port", "9223", "--allowed-origins",
                    self.manifest["hubOrigin"]])
                self.assertEqual(process.call_args.kwargs["env"]["AGENT_BROWSER_SOCKET_DIR"], str(self.socket))
                self.assertNotIn(SECRET, str((result, process.call_args)))

    def test_ip_bootstrap_parser_accepts_only_expected_ip_origin_and_port(self):
        for expected in (self.manifest["hubOrigin"], "https://[fd12::1]:8443"):
            for slash in ("", "/"):
                url = expected + slash + "#dashboard-access-token=" + SECRET
                for reused in ({}, {"already_running": True}):
                    with self.subTest(expected=expected, slash=slash, reused=reused):
                        self.assertEqual(remote.parse_dashboard_response(self.response(url, **reused), expected, 9223),
                                         {"url": url, "pid": 421, "port": 9223})
            wrong = ("https://100.64.10.3:8443", "https://100.64.10.2:8444",
                     "https://[fd12::2]:8443", "https://[fd12::1]:8444", "https://alpha.private.test:8443",
                     "https://8.8.8.8:8443", "https://127.0.0.1:8443")
            for base in wrong:
                self.error("invalid_response", remote.parse_dashboard_response,
                           self.response(base + "/#dashboard-access-token=" + SECRET), expected, 9223)
            for port in (9224, True, "9223"):
                self.error("invalid_response", remote.parse_dashboard_response,
                           self.response(expected + "/#dashboard-access-token=" + SECRET, port=port), expected, 9223)

    def test_invalid_ip_bootstrap_cannot_replace_previous_shared_bearer(self):
        self.prepare()
        self.start("beta")
        target = self.bundle / "bootstrap" / "shared.txt"
        previous = target.read_bytes()
        self.error("invalid_response", self.start, "gamma", self.response(
            "https://100.64.10.3:8443/#dashboard-access-token=" + SECRET))
        self.assertEqual(target.read_bytes(), previous)

    def test_start_requires_approval_before_loading_or_spawning(self):
        with mock.patch.object(remote, "load_bundle") as load:
            self.error("approval_required", remote.start_dashboard, self.bundle, "beta")
            load.assert_not_called()
        self.process.assert_not_called()

    def test_shared_cli_output_and_errors_never_disclose_bearer(self):
        self.prepare()
        outcomes = (
            SimpleNamespace(returncode=0, stdout=self.response(), stderr=SECRET),
            SimpleNamespace(returncode=1, stdout=SECRET, stderr=SECRET),
            SimpleNamespace(returncode=0, stdout=SECRET, stderr=SECRET),
            subprocess.TimeoutExpired(["agent-browser"], 45, output=SECRET, stderr=SECRET),
        )
        for index, outcome in enumerate(outcomes):
            with self.subTest(index=index), mock.patch.object(remote.sys, "platform", "darwin"), \
                    mock.patch.object(remote.subprocess, "run", **(
                        {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome})), \
                    mock.patch.object(remote.sys, "stdout", new_callable=io.StringIO) as output:
                code = remote.main(["dashboard-start", "--bundle", str(self.bundle),
                                    "--project", "beta", "--allow-start"])
                self.assertEqual(code, 0 if index == 0 else 1)
                self.assertNotIn(SECRET, output.getvalue())
                self.assertIsInstance(json.loads(output.getvalue()), dict)

    def test_shared_start_ignores_unapproved_environment_and_config(self):
        self.prepare()
        with mock.patch.dict(os.environ, {"AGENT_BROWSER_ACCESS_TOKEN": SECRET,
                                         "AGENT_BROWSER_SOCKET_DIR": "/wrong", "NODE_OPTIONS": SECRET,
                                         "API_KEY": SECRET, "HTTPS_PROXY": SECRET}):
            result, process = self.start("beta")
        env = process.call_args.kwargs["env"]
        self.assertLessEqual(set(env), {"HOME", "PATH", "LANG", "TMPDIR", "LC_ALL", "LC_CTYPE",
                                       "LC_MESSAGES", "AGENT_BROWSER_SOCKET_DIR"})
        self.assertEqual(env["AGENT_BROWSER_SOCKET_DIR"], str(self.socket))
        self.assertEqual((self.bundle / "agent-browser-empty.json").read_text(), "{}\n")
        self.assertNotIn(SECRET, str((result, process.call_args)))

    def test_shared_adapt_is_parser_only_and_suppresses_config_and_warnings(self):
        self.prepare()
        with mock.patch.object(remote, "run", return_value=SimpleNamespace(
                stdout=json.dumps({"secret": SECRET}), stderr="WARN " + SECRET)) as run:
            result = remote.adapt_bundle(self.bundle, "/fake/caddy")
        run.assert_called_once_with([
            "/fake/caddy", "adapt", "--config", str(self.bundle / "Caddyfile.standalone"),
            "--adapter", "caddyfile"], cwd=self.bundle)
        self.assertEqual(result, {"status": "parsed", "parserWarnings": 1, "qualificationRequired": True})
        self.assertNotIn(SECRET, str(result))
        self.process.assert_not_called()

    def test_source_example_manifest_selects_one_shared_dashboard(self):
        path = SOURCE.parents[1] / "assets" / "shared-ip-manifest.example.json"
        example = remote.read_manifest(path)
        self.assertEqual(example["mode"], "shared-ip")
        self.assertGreaterEqual(len(example["projects"]), 2)
        self.assertEqual({project["origin"] for project in example["projects"]}, {example["hubOrigin"]})
        self.assertEqual(len({project["dashboardPort"] for project in example["projects"]}), 1)
        self.assertEqual(remote.urlsplit(example["hubOrigin"]).hostname, example["bindAddress"])
        self.process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
