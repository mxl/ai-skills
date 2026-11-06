"""Symlinked tool discovery stays usable inside the qualification sandbox."""
from pathlib import Path
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import subprocess
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "qualify_shared_runtime.py"
SPEC = importlib.util.spec_from_file_location("qualification_path_tests", SOURCE)
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


class QualificationPathTests(unittest.TestCase):
    def test_symlinked_tools_remain_discoverable_without_inheriting_full_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "fixture-home"
            home.mkdir(mode=0o700)
            entry = root / "public-bin"
            native = root / "native-bin"
            entry.mkdir()
            native.mkdir()
            tools = {}
            for name in ("agent-browser", "node", "git"):
                target = native / (name + "-native")
                target.write_text("#!/bin/sh\nexit 0\n")
                target.chmod(0o700)
                link = entry / name
                link.symlink_to(target)
                tools[name] = str(link)
            with mock.patch.object(mod.shutil, "which", side_effect=lambda name, **kw: tools[name]):
                environment = mod.clean_environment(home, {"PATH": str(entry) + ":/unapproved/bin",
                                                           "BW_SESSION": "synthetic-do-not-inherit"})
            directories = environment["PATH"].split(os.pathsep)
            self.assertIn(str(entry), directories)
            self.assertIn(str(native), directories)
            self.assertNotIn("/unapproved/bin", directories)
            self.assertNotIn("BW_SESSION", environment)
            self.assertEqual(environment["HOME"], str(home))
            self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
            for name in tools:
                self.assertEqual(shutil.which(name, path=environment["PATH"]), tools[name])


class FixtureDiagnosticsTests(unittest.TestCase):
    def test_only_verified_fixture_cli_is_recorded_privately(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            executable = home / "agent-browser"
            executable.touch()
            fallback = mock.Mock(return_value="unrecorded")
            core = types.SimpleNamespace(checked_process=fallback,
                                         fail=mock.Mock(side_effect=RuntimeError("blocked")))
            checked = mod.fixture_cli_diagnostics(core, home, {"port": 9123}, "fixture", str(executable))
            args = [str(executable), "--session", "fixture", "--cdp", "9123", "--json", "open", "http://127.0.0.1/"]
            env = {"HOME": str(home), "AGENT_BROWSER_SOCKET_DIR": str(home / ".ab" / "test")}
            response = subprocess.CompletedProcess(args, 0, '{"success":true}', "synthetic diagnostic")
            with mock.patch.object(mod.subprocess, "run", return_value=response) as process:
                self.assertIs(checked(args, cwd=home, env=env), response)
                with self.assertRaises(mod.QualificationFailure):
                    checked(args, cwd=home, env={**env, "HOME": "/not-the-fixture"})
                with self.assertRaises(mod.QualificationFailure):
                    checked([str(executable), "--session", "fixture", "--cdp", "9999"], cwd=home, env=env)
                self.assertEqual(process.call_count, 1)
            files = list((home / "diagnostics").iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
            self.assertIn("synthetic diagnostic", files[0].read_text())
            self.assertEqual(checked(["/bin/ps"], cwd=home), "unrecorded")
            fallback.assert_called_once()
            self.assertEqual(len(list((home / "diagnostics").iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
