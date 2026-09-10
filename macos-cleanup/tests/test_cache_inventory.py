"""Cache discovery tests use fake owners and local temporary files only."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit-caches.sh"


class CacheInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.cache = self.home / "fixture-cache"
        self.cache.mkdir()
        (self.cache / "payload").write_bytes(b"x" * 8192)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("brew", "npm", "pnpm", "go", "uv"):
            tool = self.bin / name
            tool.write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_CACHE"\n')
            tool.chmod(0o700)
        self.env = dict(os.environ, HOME=str(self.home), FAKE_CACHE=str(self.cache),
                        PATH=str(self.bin) + ":/usr/bin:/bin")

    def run_inventory(self):
        result = subprocess.run(["/bin/sh", str(SCRIPT)], env=self.env,
                                capture_output=True, text=True, timeout=20, check=True)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_measured_is_never_actionable(self):
        records = self.run_inventory()
        candidates = [r for r in records if r["record_type"] == "cleanup_candidate"]
        self.assertEqual(len(candidates), 5)
        for candidate in candidates:
            self.assertFalse(candidate["actionable"])
            self.assertEqual(candidate["classification"], "REVIEW-ONLY")
            self.assertIsNone(candidate["proposed_action"])
            self.assertIsNone(candidate["estimated_reclaimable_bytes"])
            self.assertEqual(candidate["owner_activity"], "unknown")
            self.assertEqual(candidate["scope_state"], "unverified")
            self.assertNotIn(str(self.home), candidate["path"])
        self.assertTrue((self.cache / "payload").exists())

    def test_owner_failure_is_partial_not_empty_success(self):
        (self.bin / "uv").write_text("#!/bin/sh\nexit 3\n")
        records = self.run_inventory()
        self.assertEqual(records[-1]["status"], "PARTIAL")
        self.assertIn("uv configured cache path unavailable", records[-1]["evidence"])

    def test_empty_cache_stays_review_only(self):
        empty = self.home / "empty-cache"
        empty.mkdir()
        self.env["FAKE_CACHE"] = str(empty)
        records = self.run_inventory()
        candidates = [r for r in records if r["record_type"] == "cleanup_candidate"]
        self.assertEqual(len(candidates), 5)
        self.assertTrue(all(r["actionable"] is False for r in candidates))
        self.assertTrue(all(r["estimated_reclaimable_bytes"] is None for r in candidates))

    def test_configuration_path_with_spaces_quotes_and_newline(self):
        unusual = self.home / 'cache "quoted"\nnext line'
        unusual.mkdir()
        (unusual / "payload").write_bytes(b"fixture")
        self.env["FAKE_CACHE"] = str(unusual)
        candidates = [r for r in self.run_inventory()
                      if r["record_type"] == "cleanup_candidate"]
        self.assertEqual(len(candidates), 5)
        self.assertTrue(all(r["path"].endswith('cache "quoted"\nnext line') for r in candidates))
        self.assertTrue(all(r["actionable"] is False for r in candidates))

    def test_root_home_relative_and_symlink_are_not_candidates(self):
        link = self.home / "link"
        link.symlink_to(self.cache, target_is_directory=True)
        for target in ("/", "/./", str(self.home), str(self.home) + "/",
                       str(self.home) + "/.", "relative", str(link), str(link) + "/.",
                       str(link / "nested"), str(self.home / "fixture-cache" / "..")):
            with self.subTest(target=target):
                self.env["FAKE_CACHE"] = target
                records = self.run_inventory()
                self.assertFalse(any(r["record_type"] == "cleanup_candidate" for r in records))
                self.assertEqual(records[-1]["status"], "PARTIAL")

    def test_missing_directory_is_reported_not_silent_success(self):
        self.env["FAKE_CACHE"] = str(self.home / "missing")
        records = self.run_inventory()
        self.assertEqual(records[-1]["status"], "PARTIAL")
        self.assertIn("absent or inaccessible", records[-1]["evidence"])

    def test_firmlink_alias_of_temporary_home_is_not_a_cache(self):
        alias = Path("/System/Volumes/Data") / str(self.home.resolve()).lstrip("/")
        if not alias.exists() or not alias.samefile(self.home):
            self.skipTest("temporary directory has no Data-volume firmlink alias")
        self.env["FAKE_CACHE"] = str(alias)
        records = self.run_inventory()
        self.assertFalse(any(r["record_type"] == "cleanup_candidate" for r in records))
        self.assertEqual(records[-1]["status"], "PARTIAL")

    @unittest.skipIf(os.geteuid() == 0, "root bypasses ordinary permissions")
    def test_unsearchable_parent_does_not_look_empty(self):
        parent = self.home / "denied"
        parent.mkdir()
        target = parent / "cache"
        target.mkdir()
        parent.chmod(0o000)
        try:
            self.env["FAKE_CACHE"] = str(target)
            records = self.run_inventory()
            self.assertEqual(records[-1]["status"], "PARTIAL")
            self.assertIn("absent or inaccessible", records[-1]["evidence"])
        finally:
            parent.chmod(0o700)

    def test_homebrew_discovery_disables_incidental_maintenance(self):
        (self.bin / "brew").write_text(
            '#!/bin/sh\n[ "$HOMEBREW_NO_AUTO_UPDATE" = 1 ] || exit 3\n'
            '[ "$HOMEBREW_NO_INSTALL_CLEANUP" = 1 ] || exit 3\n'
            '[ "$HOMEBREW_NO_AUTOREMOVE" = 1 ] || exit 3\n'
            'printf "%s\\n" "$FAKE_CACHE"\n'
        )
        self.assertEqual(self.run_inventory()[-1]["status"], "COMPLETE")


if __name__ == "__main__":
    unittest.main()
