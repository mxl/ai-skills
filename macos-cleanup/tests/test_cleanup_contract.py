"""Contract tests use fake guard calls and local temporary fixtures only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os
import stat
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cleanup_contract  # noqa: E402


class CleanupContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.cache = self.home / "Library" / "Caches" / "uv"
        self.cache.mkdir(parents=True)
        (self.cache / "one").write_bytes(b"one")
        (self.cache / "nested").mkdir()
        (self.cache / "nested" / "two").write_bytes(b"two")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.real_executable = self.bin / "uv-real"
        self.real_executable.write_text("#!/bin/sh\nexit 0\n")
        self.real_executable.chmod(0o700)
        self.executable_link = self.bin / "uv"
        self.executable_link.symlink_to(self.real_executable)
        self.guard = mock.patch.object(
            cleanup_contract, "enable_no_hydration", autospec=True
        )
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def plan(self, *, now: float | None = None) -> dict:
        if now is None:
            now = time.time()
        return cleanup_contract.make_plan(
            "uv",
            str(self.cache),
            str(self.executable_link),
            "0.9.0",
            home=str(self.home),
            now=now,
        )

    def test_plan_resolves_executable_and_has_private_name_free_fingerprint(self):
        plan = self.plan()
        self.assertEqual(plan["executable"], os.path.realpath(self.real_executable))
        self.assertEqual(len(plan["target_state"]["fingerprint"]), 64)
        self.assertNotIn("one", plan["target_state"]["fingerprint"])
        self.assertNotIn("nested", plan["target_state"]["fingerprint"])
        self.assertEqual(plan["action"], "uv-cache-prune")
        self.assertIsNone(plan["estimated_reclaimable_bytes"])

    def test_digest_is_canonical_and_does_not_depend_on_current_time(self):
        plan = self.plan()
        digest = cleanup_contract.plan_digest(plan)
        reordered = {key: plan[key] for key in reversed(list(plan))}
        self.assertEqual(digest, cleanup_contract.plan_digest(reordered))
        self.assertEqual(len(digest), hashlib.sha256().digest_size * 2)
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.validate_plan(plan, now=1000.0)
        self.assertEqual(digest, cleanup_contract.plan_digest(plan))

    def test_cloud_cache_namespace_is_not_a_local_cleanup_plan(self):
        for relative in ("Library/CloudStorage/provider/cache", "Library/Mobile Documents/cache", "Dropbox/cache"):
            with self.subTest(relative=relative):
                target = str(self.home / relative)
                with mock.patch.object(cleanup_contract.os, "lstat") as lstat:
                    with self.assertRaises(cleanup_contract.PlanError):
                        cleanup_contract.capture_target(target, home=str(self.home))
                    lstat.assert_not_called()
                plan = self.plan()
                plan["target"] = target
                with self.assertRaises(cleanup_contract.PlanError):
                    cleanup_contract.validate_plan(plan)

    def test_data_volume_alias_does_not_bypass_cloud_scope_exclusion(self):
        target = "/System/Volumes/Data" + str(self.home.resolve() / "Library/CloudStorage/provider/cache")
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.capture_target(target, home=str(self.home.resolve()))

    def test_target_change_and_executable_change_are_detected(self):
        plan = self.plan()
        self.assertTrue(cleanup_contract.target_unchanged(plan))
        (self.cache / "one").write_bytes(b"changed")
        self.assertFalse(cleanup_contract.target_unchanged(plan))

        plan = self.plan()
        self.assertTrue(cleanup_contract.target_unchanged(plan))
        self.real_executable.write_text("#!/bin/sh\nexit 1\n")
        self.assertFalse(cleanup_contract.target_unchanged(plan))

    def test_strict_parser_rejects_duplicate_keys_and_nonfinite_values(self):
        path = self.root / "plan.json"
        path.write_text('{"x": 1, "x": 2}')
        path.chmod(0o600)
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.load_plan(str(path))

        plan = self.plan()
        plan["created_at"] = float("nan")
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.validate_plan(plan, now=100.0)

    def test_validate_rejects_forged_identity_shapes_and_extra_fields(self):
        plan = self.plan()
        plan["target_state"]["root"]["ino"] = True
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.validate_plan(plan, now=100.0)

        plan = self.plan(now=300.0)
        plan["argv"] = ["rm", "-rf"]
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.plan_digest(plan)

    def test_capture_rejects_symlink_and_entry_bound(self):
        link = self.home / "cache-link"
        link.symlink_to(self.cache, target_is_directory=True)
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.capture_target(
                str(link), home=str(self.home), max_entries=100
            )
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.capture_target(
                str(self.cache), home=str(self.home), max_entries=1
            )

    def test_private_json_is_exclusive_and_mode_0600(self):
        output = self.root / "private.json"
        cleanup_contract.write_private_json(str(output), {"ok": True})
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.write_private_json(str(output), {"ok": False})

    def test_guard_failure_happens_before_any_filesystem_operation(self):
        failure = cleanup_contract.PlanError("no-hydration guard unavailable")
        with mock.patch.object(
            cleanup_contract, "enable_no_hydration", side_effect=failure
        ), mock.patch.object(cleanup_contract.os, "lstat") as lstat, mock.patch.object(
            cleanup_contract.os, "scandir"
        ) as scandir, mock.patch.object(cleanup_contract.os, "open") as open_file:
            for operation in (
                lambda: cleanup_contract.capture_target(
                    str(self.cache), home=str(self.home)
                ),
                lambda: cleanup_contract.make_plan(
                    "uv",
                    str(self.cache),
                    str(self.executable_link),
                    "0.9.0",
                    home=str(self.home),
                ),
                lambda: cleanup_contract.write_private_json(
                    str(self.root / "out.json"), {"ok": True}
                ),
                lambda: cleanup_contract.load_plan(str(self.root / "plan.json")),
            ):
                with self.assertRaises(cleanup_contract.PlanError):
                    operation()
        lstat.assert_not_called()
        scandir.assert_not_called()
        open_file.assert_not_called()

    def test_dataless_root_and_home_aliases_are_rejected(self):
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract._check_dataless(
                SimpleNamespace(st_flags=cleanup_contract.SF_DATALESS)
            )
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.capture_target(str(self.home), home=str(self.home))
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.capture_target(os.sep, home=str(self.home))
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.capture_target(str(self.cache), home=os.sep)

    def test_crossed_mount_and_capture_timeout_are_blocked(self):
        original_lstat = cleanup_contract.os.lstat
        child = self.cache / "one"

        def foreign_child(path):
            value = original_lstat(path)
            if os.path.normpath(path) != os.path.normpath(str(child)):
                return value
            return SimpleNamespace(
                st_dev=value.st_dev + 1,
                st_ino=value.st_ino,
                st_mode=value.st_mode,
                st_nlink=value.st_nlink,
                st_uid=value.st_uid,
                st_gid=value.st_gid,
                st_size=value.st_size,
                st_blocks=getattr(value, "st_blocks", 0),
                st_blksize=getattr(value, "st_blksize", 0),
                st_rdev=value.st_rdev,
                st_mtime_ns=value.st_mtime_ns,
                st_ctime_ns=value.st_ctime_ns,
                st_birthtime_ns=getattr(value, "st_birthtime_ns", 0),
                st_flags=getattr(value, "st_flags", 0),
            )

        with mock.patch.object(cleanup_contract.os, "lstat", side_effect=foreign_child):
            with self.assertRaises(cleanup_contract.PlanError):
                cleanup_contract.capture_target(str(self.cache), home=str(self.home))
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.capture_target(
                str(self.cache), home=str(self.home), timeout=0.000001
            )

    def test_ancestor_change_and_semantic_identity_validation_are_blocked(self):
        original_ancestors = cleanup_contract._ancestor_metadata
        calls = 0

        def changed_ancestors(path, deadline=None):
            nonlocal calls
            calls += 1
            result = original_ancestors(path, deadline)
            if calls == 2:
                result[-1] = dict(result[-1])
                result[-1]["mode"] ^= 0o100
            return result

        with mock.patch.object(
            cleanup_contract, "_ancestor_metadata", side_effect=changed_ancestors
        ):
            with self.assertRaises(cleanup_contract.PlanError):
                cleanup_contract.capture_target(str(self.cache), home=str(self.home))

        plan = self.plan()
        plan["target_state"]["root"]["mode"] = stat.S_IFREG | 0o600
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.validate_plan(plan)
        plan = self.plan()
        plan["executable_state"]["identity"]["mode"] = stat.S_IFDIR | 0o755
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.validate_plan(plan)
        plan = self.plan()
        plan["target_state"]["ancestors"][0]["ino"] += 1
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.validate_plan(plan)

    def test_private_write_failure_does_not_remove_sentinel_and_retries_partial_writes(self):
        sentinel = self.root / "sentinel.json"
        sentinel.write_text("sentinel")
        sentinel.chmod(0o600)
        original_open = cleanup_contract.os.open
        with mock.patch.object(cleanup_contract.os, "open", side_effect=OSError("open")):
            with self.assertRaises(cleanup_contract.PlanError):
                cleanup_contract.write_private_json(str(sentinel), {"new": True})
        self.assertEqual(sentinel.read_text(), "sentinel")

        output = self.root / "partial.json"
        original_write = cleanup_contract.os.write

        def partial_write(fd, data):
            if len(data) > 1:
                return original_write(fd, data[:1])
            return original_write(fd, data)

        with mock.patch.object(cleanup_contract.os, "write", side_effect=partial_write):
            cleanup_contract.write_private_json(str(output), {"ok": "partial"})
        self.assertEqual(json.loads(output.read_text()), {"ok": "partial"})

        failed_output = self.root / "failed-partial.json"
        wrote_once = False

        def fail_after_partial(fd, data):
            nonlocal wrote_once
            if not wrote_once:
                wrote_once = True
                return original_write(fd, data[:1])
            raise OSError("write")

        with mock.patch.object(cleanup_contract.os, "write", side_effect=fail_after_partial):
            with self.assertRaises(cleanup_contract.PlanError):
                cleanup_contract.write_private_json(
                    str(failed_output), {"ok": "failed"}
                )
        self.assertFalse(failed_output.exists())
        self.assertIsNotNone(original_open)

    def test_load_plan_rejects_fifo_bad_mode_hardlink_huge_and_deep_inputs(self):
        fifo = self.root / "plan.fifo"
        os.mkfifo(fifo)
        started = time.monotonic()
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.load_plan(str(fifo))
        self.assertLess(time.monotonic() - started, 1.0)

        bad_mode = self.root / "bad-mode.json"
        bad_mode.write_text("{}")
        bad_mode.chmod(0o644)
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.load_plan(str(bad_mode))

        source = self.root / "source.json"
        source.write_text("{}")
        source.chmod(0o600)
        hardlink = self.root / "hardlink.json"
        os.link(source, hardlink)
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.load_plan(str(hardlink))

        huge = self.root / "huge.json"
        huge.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
        huge.chmod(0o600)
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.load_plan(str(huge))

        deep = self.root / "deep.json"
        deep.write_bytes(b"[" * 5000 + b"0" + b"]" * 5000)
        deep.chmod(0o600)
        with self.assertRaises(cleanup_contract.PlanError):
            cleanup_contract.load_plan(str(deep))

    def test_load_plan_rejects_stat_mutation_during_read(self):
        plan_path = self.root / "valid-plan.json"
        cleanup_contract.write_private_json(str(plan_path), self.plan())
        original_read = cleanup_contract.os.read
        changed = False

        def mutate_after_read(fd, size):
            nonlocal changed
            chunk = original_read(fd, size)
            if chunk and not changed:
                changed = True
                now = time.time_ns() + 1_000_000
                os.utime(plan_path, ns=(now, now))
            return chunk

        with mock.patch.object(cleanup_contract.os, "read", side_effect=mutate_after_read):
            with self.assertRaises(cleanup_contract.PlanError):
                cleanup_contract.load_plan(str(plan_path))

    def test_sibling_private_outputs_do_not_stale_a_valid_target_plan(self):
        plan = self.plan()
        plan_path = self.root / "plan.json"
        journal_path = self.root / "journal.json"
        cleanup_contract.write_private_json(str(plan_path), plan)
        self.assertTrue(cleanup_contract.target_unchanged(plan))
        cleanup_contract.write_private_json(str(journal_path), {"digest": "x"})
        self.assertTrue(cleanup_contract.target_unchanged(plan))

        ancestor = self.cache.parent
        original_mode = stat.S_IMODE(ancestor.stat().st_mode)
        try:
            ancestor.chmod(original_mode & ~0o200)
            self.assertFalse(cleanup_contract.target_unchanged(plan))
        finally:
            ancestor.chmod(original_mode)

        moved = self.root / "replaced-caches"
        ancestor.rename(moved)
        try:
            ancestor.mkdir(mode=original_mode)
            self.assertFalse(cleanup_contract.target_unchanged(plan))
        finally:
            if ancestor.exists():
                ancestor.rmdir()
            moved.rename(ancestor)


if __name__ == "__main__":
    unittest.main()
