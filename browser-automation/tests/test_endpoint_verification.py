"""Endpoint/process verification with fake process tables and HTTP responses only."""
from pathlib import Path
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "browser_session.py"
SPEC = importlib.util.spec_from_file_location("browser_endpoint_tests", SOURCE)
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


class EndpointVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.project = mod.Project(root, mod.digest(str(root)), False)
        self.project.profile.mkdir()
        (self.project.profile / "DevToolsActivePort").write_text("9341\n/devtools/browser/browser-target\n")
        self.process = {"pid": 421, "started": "Mon Nov 2 10:00:00 2026",
                        "executable": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                        "headed": False, "features": ["ForceMacVSyncTimerForDebugging"]}
        self.version = {"Browser": "HeadlessChrome/142.0", "webSocketDebuggerUrl":
                        "ws://127.0.0.1:9341/devtools/browser/browser-target"}

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(mod.BrowserSessionError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def verify(self, version=None, expected=None):
        with mock.patch.object(mod, "discover_processes", side_effect=lambda _: [dict(self.process)]), \
                mock.patch.object(mod, "verify_listener") as listener, \
                mock.patch.object(mod, "cdp_json", return_value=version or self.version):
            result = mod.verify_browser(self.project, expected)
            listener.assert_called_once_with(421, 9341)
            return result

    def test_valid_dynamic_endpoint_returns_identity_not_websocket_secret(self):
        result = self.verify()
        self.assertEqual(result["port"], 9341)
        self.assertEqual(result["endpoint_hash"], mod.digest("/devtools/browser/browser-target"))
        self.assertNotIn("webSocketDebuggerUrl", result)

    def test_browser_endpoint_substitution_rejected(self):
        bad_urls = [
            "ws://example.com:9341/devtools/browser/browser-target",
            "ws://127.0.0.1:9999/devtools/browser/browser-target",
            "ws://127.0.0.1:9341/devtools/browser/different-target",
            "ws://user:password@127.0.0.1:9341/devtools/browser/browser-target",
            "ws://127.0.0.1:9341/devtools/browser/browser-target?token=private",
            "ws://127.0.0.1:9341/devtools/browser/browser-target#private",
        ]
        for url in bad_urls:
            with self.subTest(url=url):
                self.error("browser_unverified", self.verify,
                           {"Browser": "Chrome/142", "webSocketDebuggerUrl": url})

    def test_missing_required_feature_rejected_without_restart(self):
        self.process["features"] = ["SomeOtherFeature"]
        self.error("browser_unverified", self.verify)

    def test_process_identity_change_cannot_rebind(self):
        expected = {**self.process, "port": 9341, "endpoint_hash": mod.digest("/devtools/browser/browser-target")}
        expected["pid"] = 999
        self.error("stale_browser", self.verify, expected=mod.browser_identity(expected))

    def test_pid_reuse_during_verification_rejected(self):
        replacement = {**self.process, "started": "different creation time"}
        with mock.patch.object(mod, "discover_processes", side_effect=[[dict(self.process)], [replacement]]), \
                mock.patch.object(mod, "verify_listener"), \
                mock.patch.object(mod, "cdp_json", return_value=self.version):
            self.error("stale_browser", mod.verify_browser, self.project)

    def test_unknown_browser_absent_is_not_an_endpoint_guess(self):
        with mock.patch.object(mod, "discover_processes", return_value=[]), \
                mock.patch.object(mod, "cdp_json") as http:
            self.assertIsNone(mod.verify_browser(self.project))
            http.assert_not_called()

    def test_endpoint_file_symlink_is_rejected(self):
        endpoint = self.project.profile / "DevToolsActivePort"
        target = self.project.root / "other-endpoint"
        target.write_text(endpoint.read_text())
        endpoint.unlink()
        endpoint.symlink_to(target)
        with mock.patch.object(mod, "discover_processes", return_value=[dict(self.process)]):
            self.error("unsafe_path", mod.verify_browser, self.project)

    def test_listener_accepts_ipv4_and_ipv6_loopback_for_one_owner(self):
        output = subprocess.CompletedProcess([], 0, "p421\nn127.0.0.1:9341\nn[::1]:9341\n", "")
        with mock.patch.object(mod, "checked_process", return_value=output):
            mod.verify_listener(421, 9341)

    def test_listener_rejects_wildcards_remote_hosts_wrong_ports_and_owners(self):
        for text in ["p421\nn*:9341\n", "p421\nn0.0.0.0:9341\n", "p421\nn[::]:9341\n",
                     "p421\nn192.0.2.10:9341\n", "p421\nn127.0.0.1:9999\n",
                     "p999\nn127.0.0.1:9341\n", "p421\nn127.0.0.1:9341\np999\nn[::1]:9341\n"]:
            with self.subTest(listener=text), mock.patch.object(mod, "checked_process", return_value=
                    subprocess.CompletedProcess([], 0, text, "")):
                self.error("browser_unverified", mod.verify_listener, 421, 9341)


if __name__ == "__main__":
    unittest.main()
