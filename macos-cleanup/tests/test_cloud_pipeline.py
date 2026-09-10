"""Discovered synthetic providers trigger metadata work without a second request."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import audit_all


class CloudPipelineTests(unittest.TestCase):
    def test_unclassified_listing_error_is_not_permission_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            (home / "Library" / "Mobile Documents").mkdir(parents=True)
            fake_find = root / "fake-find"
            marker = root / "called"
            fake_find.write_text('#!/bin/sh\nprintf called > "$FIND_CALLED"\nexit 1\n')
            fake_find.chmod(0o700)
            (root / "common.sh").symlink_to(SCRIPTS / "common.sh")
            collector = root / "audit-cloud.sh"
            collector.write_text((SCRIPTS / "audit-cloud.sh").read_text().replace(
                "/usr/bin/find", str(fake_find)))
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / "audit_runtime.py"), "--guard-exec", "/bin/sh", str(collector)],
                env=dict(os.environ, HOME=str(home), FIND_CALLED=str(marker)),
                capture_output=True, text=True, check=True, timeout=20,
            )
            self.assertTrue(marker.exists())
            inventory = [json.loads(line) for line in completed.stdout.split("\n") if line]
            containers = [r for r in inventory if r["record_type"] == "cloud_container_inventory"]
            self.assertEqual(containers[0]["status"], "PARTIAL")
            self.assertIsNone(containers[0]["container_count"])

    def test_discovery_collects_items_and_retains_valid_redacted_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            provider = home / "Library" / "CloudStorage" / "GoogleDrive-fixture@example.invalid"
            provider.mkdir(parents=True)
            (provider / "PRIVATE-PHOTO-NAME").write_bytes(b"synthetic local item")
            env = dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / "audit_runtime.py"), "--guard-exec", "/bin/sh",
                 str(SCRIPTS / "audit-cloud.sh")],
                env=env, capture_output=True, text=True, timeout=30, check=True,
            )
            self.assertNotIn("fixture@example.invalid", completed.stdout)
            self.assertNotIn("PRIVATE-PHOTO-NAME", completed.stdout)
            parsed = audit_all._parse_module_output(completed.stdout, "cloud", str(home))
            self.assertEqual(parsed.invalid_record_count, 0)
            self.assertEqual(parsed.malformed_line_count, 0)
            metadata = [r for r in parsed.records if r.get("inventory_scope") == "bounded-item-metadata"]
            self.assertEqual(len(metadata), 1)
            self.assertEqual(metadata[0]["counts"]["files"], 1)
            self.assertEqual(metadata[0]["enumeration_status"], "COMPLETE")
            self.assertFalse(metadata[0]["actionable"])
            self.assertEqual(metadata[0]["sync_state"], "unknown")
            self.assertEqual(parsed.observed_status, "PARTIAL")
            self.assertEqual((provider / "PRIVATE-PHOTO-NAME").read_bytes(), b"synthetic local item")


if __name__ == "__main__":
    unittest.main()
