"""Absence is not inferred through inaccessible directory ancestors."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit-paths.sh"


class PathInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def collect(self, target):
        result = subprocess.run(["/bin/sh", str(SCRIPT), "--exact", str(target)],
                                env=dict(os.environ, HOME=str(self.home)),
                                capture_output=True, text=True, check=True, timeout=10)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_confirmed_missing_file(self):
        records = self.collect(self.home / "missing")
        self.assertEqual(records[0]["status"], "NOT-FOUND")
        self.assertEqual(records[-1]["status"], "COMPLETE")

    def test_symlink_loop_is_not_missing(self):
        link = self.home / "loop"
        link.symlink_to(link)
        records = self.collect(link)
        self.assertEqual(records[0]["status"], "PARTIAL")
        self.assertEqual(records[-1]["status"], "PARTIAL")

    @unittest.skipIf(os.geteuid() == 0, "root bypasses ordinary permissions")
    def test_inaccessible_symlink_referent_is_not_missing(self):
        parent = self.home / "denied"
        parent.mkdir()
        target = parent / "target"
        target.mkdir()
        link = self.home / "accessible-link"
        link.symlink_to(target)
        parent.chmod(0o000)
        try:
            records = self.collect(link)
            self.assertEqual(records[0]["status"], "PARTIAL")
            self.assertEqual(records[-1]["status"], "PARTIAL")
        finally:
            parent.chmod(0o700)

    @unittest.skipIf(os.geteuid() == 0, "root bypasses ordinary permissions")
    def test_inaccessible_ancestor_is_partial(self):
        parent = self.home / "blocked"
        parent.mkdir()
        (parent / "child").mkdir()
        parent.chmod(0o000)
        try:
            records = self.collect(parent / "child" / "target")
            self.assertEqual(records[0]["status"], "PARTIAL")
            self.assertIsNone(records[0]["allocated_bytes"])
            self.assertEqual(records[-1]["status"], "PARTIAL")
        finally:
            parent.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
