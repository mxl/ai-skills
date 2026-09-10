"""Actual plan/CLI integration using only local synthetic targets and tools."""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cleanup_actions
import cleanup_contract


class ActionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.cache = self.home / "fixture-cache"
        self.cache.mkdir(mode=0o700)
        (self.cache / "entry").write_bytes(b"fixture-cache-entry")
        self.tool = self.root / "go"
        self.tool.write_text("#!/bin/sh\nexit 0\n")
        self.tool.chmod(0o700)
        self.plan = cleanup_contract.make_plan(
            "go", str(self.cache), str(self.tool), "go version go1.24.0 darwin/arm64",
            home=str(self.home),
        )

    def test_private_roundtrip_and_cli_show_same_digest_without_subprocess(self):
        path = self.root / "plan.json"
        cleanup_contract.write_private_json(str(path), self.plan)
        loaded = cleanup_contract.load_plan(str(path))
        self.assertEqual(cleanup_contract.plan_digest(loaded), cleanup_contract.plan_digest(self.plan))
        output = io.StringIO()
        with mock.patch("subprocess.Popen", side_effect=AssertionError("No owner probe in show")), \
                contextlib.redirect_stdout(output):
            code = cleanup_actions.main(["show", "--plan", str(path)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["digest"], cleanup_contract.plan_digest(self.plan))
        self.assertTrue((self.cache / "entry").exists())

    def test_real_fingerprint_detects_changed_cache(self):
        self.assertTrue(cleanup_contract.target_unchanged(self.plan))
        (self.cache / "entry").write_bytes(b"changed-cache-payload-is-longer")
        self.assertFalse(cleanup_contract.target_unchanged(self.plan))

    def test_wrong_digest_cannot_probe_or_create_execution_journal(self):
        import cleanup_executor

        journal = self.root / "journal"
        with mock.patch("subprocess.Popen", side_effect=AssertionError("No process before consent")):
            result = cleanup_executor.execute(
                self.plan, confirmed_digest="0" * 64, journal_dir=str(journal)
            )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(journal.exists())
        self.assertEqual((self.cache / "entry").read_bytes(), b"fixture-cache-entry")

    def test_old_plan_file_is_never_overwritten(self):
        path = self.root / "plan.json"
        cleanup_contract.write_private_json(str(path), self.plan)
        original = path.read_bytes()
        with self.assertRaises((cleanup_contract.PlanError, OSError)):
            cleanup_contract.write_private_json(str(path), self.plan)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
