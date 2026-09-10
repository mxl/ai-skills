"""Tests for the guarded, metadata-only cloud inventory."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import audit_runtime  # noqa: E402
import cloud_inventory  # noqa: E402


class _StatWithFlags:
    """Small stat wrapper for synthetic dataless flags."""

    def __init__(self, original, flags: int):
        self._original = original
        self.st_flags = flags

    def __getattr__(self, name):
        return getattr(self._original, name)


class CloudInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cloud-inventory-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "provider-root"
        self.root.mkdir()

    def inventory(self, root: Path | None = None, **kwargs):
        with mock.patch.object(cloud_inventory, "enable_no_hydration"):
            return cloud_inventory.inventory(str(root or self.root), **kwargs)

    def test_complete_enumeration_is_still_partial_for_eviction_readiness(self):
        (self.root / "file").write_bytes(b"metadata-only fixture")

        result = self.inventory()

        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["enumeration_status"], "COMPLETE")
        self.assertFalse(result["actionable"])
        self.assertEqual(result["sync_state"], "unknown")
        self.assertEqual(result["pinned"], "unknown")
        self.assertEqual(result["eviction"], "unverified")
        self.assertEqual(result["items"][0]["local_state"], "local-allocation-present")

    def test_hardcaps_and_huge_integer_timeout_are_rejected_before_filesystem_access(self):
        invalid_limits = (
            {"max_entries": cloud_inventory.MAX_ENTRIES + 1},
            {"max_depth": cloud_inventory.MAX_DEPTH + 1},
            {"timeout": cloud_inventory.MAX_TIMEOUT_SECONDS + 1},
            {"timeout": 10**10_000},
        )
        for limits in invalid_limits:
            with self.subTest(limits=limits), mock.patch.object(
                cloud_inventory, "enable_no_hydration"
            ), mock.patch.object(cloud_inventory.os, "lstat") as lstat, mock.patch.object(
                cloud_inventory.os, "scandir"
            ) as scandir:
                result = cloud_inventory.inventory(str(self.root), **limits)
            self.assertEqual(result["enumeration_status"], "NOT-RUN")
            lstat.assert_not_called()
            scandir.assert_not_called()

    def test_dot_dotdot_and_ambiguous_paths_are_not_canonicalized(self):
        invalid_roots = (
            str(self.root) + "/./child",
            str(self.root) + "/../sibling",
            str(self.root) + "//child",
            "//" + str(self.root).lstrip("/"),
        )
        for invalid_root in invalid_roots:
            with self.subTest(invalid_root=invalid_root), mock.patch.object(
                cloud_inventory, "enable_no_hydration"
            ), mock.patch.object(cloud_inventory.os, "lstat") as lstat, mock.patch.object(
                cloud_inventory.os, "scandir"
            ) as scandir:
                result = cloud_inventory.inventory(invalid_root)
            self.assertEqual(result["enumeration_status"], "NOT-RUN")
            lstat.assert_not_called()
            scandir.assert_not_called()

        trailing = self.inventory(str(self.root) + "/")
        self.assertEqual(trailing["enumeration_status"], "COMPLETE")

    def test_dataless_directory_is_detected_before_child_scandir(self):
        dataless = self.root / "dataless"
        dataless.mkdir()
        (dataless / "must-not-enter").write_bytes(b"payload")
        original_lstat = cloud_inventory.os.lstat

        def fake_lstat(path):
            metadata = original_lstat(path)
            if os.fspath(path) == str(dataless):
                return _StatWithFlags(metadata, cloud_inventory.SF_DATALESS)
            return metadata

        scanned = []
        original_scandir = cloud_inventory.os.scandir

        def fake_scandir(path):
            scanned.append(os.fspath(path))
            return original_scandir(path)

        with mock.patch.object(cloud_inventory, "enable_no_hydration"), mock.patch.object(
            cloud_inventory.os, "lstat", side_effect=fake_lstat
        ), mock.patch.object(cloud_inventory.os, "scandir", side_effect=fake_scandir):
            result = cloud_inventory.inventory(str(self.root))

        self.assertEqual(result["counts"]["dataless"], 1)
        self.assertNotIn(str(dataless), scanned)
        self.assertEqual(result["counts"]["files"], 0)
        self.assertEqual(result["enumeration_status"], "PARTIAL")
        self.assertEqual(result["status"], "PARTIAL")

    def test_payload_is_never_opened_or_read(self):
        (self.root / "payload").write_bytes(b"not inspected")

        with mock.patch.object(cloud_inventory, "enable_no_hydration"), mock.patch(
            "builtins.open", side_effect=AssertionError("payload open")
        ), mock.patch.object(Path, "read_bytes", side_effect=AssertionError("payload read")):
            result = cloud_inventory.inventory(str(self.root))

        self.assertEqual(result["counts"]["files"], 1)

    def test_child_symlink_is_counted_and_never_entered(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"outside")
        link = self.root / "link-to-outside"
        link.symlink_to(outside, target_is_directory=True)
        (self.root / "local").write_bytes(b"local")

        result = self.inventory()

        self.assertGreaterEqual(result["counts"]["symlink"], 1)
        self.assertEqual(result["counts"]["files"], 1)
        self.assertTrue(all(item["kind"] != "symlink" for item in result["items"]))

    def test_missing_size_metadata_uses_explicit_lower_bound_status(self):
        known = self.root / "known"
        missing = self.root / "missing-metadata"
        known.write_bytes(b"known")
        missing.write_bytes(b"unknown")
        original_lstat = cloud_inventory.os.lstat

        def fake_lstat(path):
            metadata = original_lstat(path)
            if os.fspath(path) == str(missing):
                return SimpleNamespace(
                    st_mode=metadata.st_mode,
                    st_dev=metadata.st_dev,
                    st_ino=metadata.st_ino,
                    st_uid=metadata.st_uid,
                )
            return metadata

        with mock.patch.object(cloud_inventory, "enable_no_hydration"), mock.patch.object(
            cloud_inventory.os, "lstat", side_effect=fake_lstat
        ):
            result = cloud_inventory.inventory(str(self.root))

        self.assertEqual(result["enumeration_status"], "PARTIAL")
        self.assertEqual(result["measurement_status"], "lower-bound")
        self.assertIsNone(result["allocated_bytes"])
        self.assertIsNone(result["logical_bytes"])
        self.assertGreaterEqual(result["allocated_lower_bound_bytes"], 0)
        self.assertGreaterEqual(result["logical_lower_bound_bytes"], 0)
        self.assertEqual(result["totals"]["allocated_measurement"], "lower-bound")
        self.assertIn(
            "unknown", {item["local_state"] for item in result["items"]}
        )

    def test_hardlinks_are_listed_but_allocated_total_is_deduplicated(self):
        first = self.root / "first"
        second = self.root / "second"
        first.write_bytes(b"same inode")
        os.link(first, second)
        expected = first.stat().st_blocks * 512

        result = self.inventory()
        files = [item for item in result["items"] if item["kind"] == "file"]

        self.assertEqual(len(files), 2)
        self.assertEqual(result["totals"]["file_allocated_bytes"], expected)
        self.assertEqual(result["totals"]["allocated_bytes"], expected)
        self.assertEqual(result["measurement_status"], "complete")
        self.assertEqual(result["totals"]["hardlink_deduplicated"], True)
        self.assertEqual(result["counts"]["hardlinks"], 2)
        self.assertTrue(all(item["shared_inode"] for item in files))
        self.assertTrue(all(item["exclusive_reclaim"] == "unknown" for item in files))

    def test_queued_directory_identity_change_is_skipped_before_scandir(self):
        child = self.root / "queued"
        child.mkdir()
        (child / "old-payload").write_bytes(b"must not be entered")
        replacement = self.root / "queued-replaced"
        original_scandir = cloud_inventory.os.scandir
        replaced = False

        class CloseAfterRootScan:
            def __init__(self, entries):
                self._entries = iter(entries)

            def __next__(self):
                return next(self._entries)

            def close(self):
                nonlocal replaced
                if not replaced:
                    os.rename(child, replacement)
                    child.mkdir()
                    replaced = True

        def fake_scandir(path):
            if os.fspath(path) == str(self.root) and not replaced:
                source = original_scandir(path)
                try:
                    entries = list(source)
                finally:
                    source.close()
                return CloseAfterRootScan(entries)
            return original_scandir(path)

        with mock.patch.object(cloud_inventory, "enable_no_hydration"), mock.patch.object(
            cloud_inventory.os, "scandir", side_effect=fake_scandir
        ):
            result = cloud_inventory.inventory(str(self.root))

        self.assertEqual(result["counts"]["files"], 0)
        self.assertGreaterEqual(result["counts"]["skipped"], 1)
        self.assertIn("queued directory changed before scan", result["reasons"])

    def test_depth_and_entry_limits_preserve_earlier_records(self):
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "deep").mkdir()
        (nested / "deep" / "file").write_bytes(b"deep")
        (self.root / "first").write_bytes(b"first")

        depth_result = self.inventory(max_depth=1)
        limited_result = self.inventory(max_entries=1)

        self.assertEqual(depth_result["enumeration_status"], "PARTIAL")
        self.assertGreaterEqual(depth_result["counts"]["depth_limited"], 1)
        self.assertTrue(any(item["kind"] == "directory" for item in depth_result["items"]))
        self.assertEqual(limited_result["enumeration_status"], "PARTIAL")
        self.assertEqual(limited_result["counts"]["entries_seen"], 1)
        self.assertGreaterEqual(len(limited_result["items"]), 1)

    def test_timeout_returns_partial_without_erasing_earlier_entries(self):
        (self.root / "first").write_bytes(b"first")
        (self.root / "second").write_bytes(b"second")
        clock = iter((0.0, 0.0, 0.0, 0.0, 2.0))

        with mock.patch.object(cloud_inventory, "enable_no_hydration"), mock.patch.object(
            cloud_inventory.time, "monotonic", side_effect=lambda: next(clock)
        ):
            result = cloud_inventory.inventory(str(self.root), timeout=1.0)

        self.assertEqual(result["enumeration_status"], "PARTIAL")
        self.assertIn("time limit", " ".join(result["reasons"]))
        self.assertGreaterEqual(len(result["items"]), 1)

    def test_unavailable_guard_does_not_call_scandir_or_lstat(self):
        with mock.patch.object(
            cloud_inventory, "enable_no_hydration", side_effect=audit_runtime.GuardUnavailable()
        ), mock.patch.object(cloud_inventory.os, "scandir") as scandir, mock.patch.object(
            cloud_inventory.os, "lstat"
        ) as lstat:
            result = cloud_inventory.inventory(str(self.root))

        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["enumeration_status"], "UNAVAILABLE")
        scandir.assert_not_called()
        lstat.assert_not_called()

    def test_permission_failure_is_not_reported_as_zero_bytes(self):
        with mock.patch.object(cloud_inventory, "enable_no_hydration"), mock.patch.object(
            cloud_inventory.os, "scandir", side_effect=PermissionError()
        ):
            result = cloud_inventory.inventory(str(self.root))

        self.assertEqual(result["counts"]["denied"], 1)
        self.assertIsNone(result["allocated_bytes"])
        self.assertIsNone(result["logical_bytes"])
        self.assertEqual(result["enumeration_status"], "PARTIAL")

    def test_invalid_root_aliases_are_rejected_without_enumeration(self):
        with mock.patch.object(cloud_inventory, "enable_no_hydration"), mock.patch.object(
            cloud_inventory.os, "scandir"
        ) as scandir:
            result = cloud_inventory.inventory(os.environ["HOME"])

        self.assertEqual(result["enumeration_status"], "NOT-RUN")
        scandir.assert_not_called()

    def test_item_ids_are_per_run_hmacs_without_cross_run_correlation(self):
        (self.root / "known-name").write_bytes(b"local")

        first = self.inventory()
        second = self.inventory()

        first_ids = {item["item_id"] for item in first["items"]}
        second_ids = {item["item_id"] for item in second["items"]}
        self.assertTrue(first_ids)
        self.assertTrue(first_ids.isdisjoint(second_ids))
        self.assertTrue(all(item_id.startswith("item-") for item_id in first_ids))

    def test_public_result_redacts_weird_unicode_names_and_root(self):
        account_name = "account\U0001f469\U0001f3fd\u200d\U0001f4bb\nname"
        account = self.root / account_name
        account.mkdir()
        (account / "document\u2603").write_bytes(b"local")

        result = self.inventory()
        public = json.dumps(result, ensure_ascii=False, sort_keys=True)

        self.assertNotIn(str(self.root), public)
        self.assertNotIn(account_name, public)
        self.assertEqual(result["root_label"], "redacted-cloud-root")
        self.assertTrue(all("item_id" in item for item in result["items"]))

    def test_native_guard_query_is_separate_from_user_cloud_fixtures(self):
        runtime = SCRIPT_DIR / "audit_runtime.py"
        completed = subprocess.run(
            [sys.executable, str(runtime), "--check-guard"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertIn(completed.returncode, (0, 78))
        document = json.loads(completed.stdout)
        self.assertEqual(document["guard"], "no-hydration")


if __name__ == "__main__":
    unittest.main()
