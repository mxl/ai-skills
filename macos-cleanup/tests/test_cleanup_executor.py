"""Executor tests use local fixtures and mocked owner/runtime dependencies."""

from __future__ import annotations

import json
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cache_adapters
import cleanup_contract
import cleanup_executor


class FakeResult:
    def __init__(self, returncode=0, outcome="finished"):
        self.returncode = returncode
        self.outcome = outcome


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanup-executor-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.cache = self.home / "cache"
        self.cache.mkdir(mode=0o700)
        (self.cache / "entry").write_bytes(b"entry")
        self.tool = self.root / "owner-tool"
        self.tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.tool.chmod(0o700)
        self.journal_temp = tempfile.TemporaryDirectory(prefix="cleanup-executor-journal-", dir="/tmp")
        self.journal = Path(self.journal_temp.name) / "journal"
        self.plan = {
            "schema_version": 1,
            "adapter": "go",
            "action": "go-build-cache-clean",
            "target": str(self.cache),
            "home": str(self.home),
            "target_state": {"fingerprint": "same"},
            "executable": str(self.tool),
            "executable_state": {},
            "version": "go version go1.24.0 darwin/arm64",
            "created_at": 100.0,
            "expires_at": 200.0,
            "scope": "owner-managed-dynamic-cache",
            "risk": "fixture",
            "estimated_reclaimable_bytes": None,
        }
        self.digest = "a" * 64
        self.capture_calls = 0

        def capture(path, *, home=None):
            self.capture_calls += 1
            entry = (Path(path) / "entry").stat() if (Path(path) / "entry").exists() else None
            return {
                "fingerprint": "same" if entry is not None else "changed",
                "root_identity": "fixture-root",
                "allocated_bytes": 512 if entry is not None else 0,
                "logical_bytes": entry.st_size if entry is not None else 0,
            }

        self.capture = capture
        self.validate = mock.Mock(side_effect=lambda plan, now=None: dict(plan))
        self.preflight = mock.Mock(return_value={
            "ok": True,
            "evidence": {
                "owner_activity": "idle",
                "target": str(self.cache),
                "executable": str(self.tool),
                "version": self.plan["version"],
            },
        })
        self.target_unchanged = mock.Mock(return_value=True)
        self.build = mock.Mock(return_value=([str(self.tool), "clean", "-cache"], {"GOCACHE": str(self.cache)}))
        self.run = mock.Mock(return_value=FakeResult())
        self.patches = mock.patch.multiple(
            cleanup_executor,
            enable_no_hydration=mock.Mock(),
            capture_target=self.capture,
            validate_plan=self.validate,
            plan_digest=mock.Mock(return_value=self.digest),
            target_unchanged=self.target_unchanged,
            preflight=self.preflight,
            build_command=self.build,
            run_command=self.run,
        )
        self.patches.start()
        self.addCleanup(self.patches.stop)
        self.addCleanup(self.journal_temp.cleanup)
        self.addCleanup(self.temp.cleanup)

    def execute(self, **kwargs):
        values = {"confirmed_digest": self.digest, "journal_dir": str(self.journal), "now": 100.0}
        values.update(kwargs)
        return cleanup_executor.execute(self.plan, **values)

    def test_missing_and_wrong_digest_are_checked_before_paths(self):
        for digest in (None, "0" * 64):
            with self.subTest(digest=digest):
                cleanup_executor.enable_no_hydration.reset_mock()
                self.preflight.reset_mock()
                result = self.execute(confirmed_digest=digest)
                self.assertEqual(result["status"], "BLOCKED")
                self.assertFalse(self.journal.exists())
                self.preflight.assert_not_called()
        self.assertTrue(cleanup_executor.enable_no_hydration.called)

    def test_expired_and_unknown_action_are_blocked(self):
        expired = dict(self.plan, expires_at=150.0)
        result = cleanup_executor.execute(expired, confirmed_digest=self.digest, journal_dir=str(self.journal), now=150.0)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("expired", result["reason"])

        unknown = dict(self.plan, adapter="npm", action="npm-cache-clean")
        result = cleanup_executor.execute(unknown, confirmed_digest=self.digest, journal_dir=str(self.journal), now=100.0)
        self.assertEqual(result["status"], "BLOCKED")

    def test_changed_target_and_active_owner_are_blocked_and_journaled(self):
        self.target_unchanged.return_value = False
        result = self.execute()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(self.run.call_count, 0)
        self.assertTrue((self.journal / (self.digest + ".jsonl")).exists())

        self.target_unchanged.reset_mock(return_value=True)
        self.preflight.reset_mock(return_value={"ok": True, "evidence": {"owner_activity": "active"}})
        result = self.execute(journal_dir=str(Path(self.journal_temp.name) / "active-journal"))
        self.assertEqual(result["status"], "BLOCKED")
        self.run.assert_not_called()

    def test_unknown_plan_keys_are_rejected(self):
        unknown = dict(self.plan, unexpected="not-in-contract")
        result = cleanup_executor.execute(
            unknown,
            confirmed_digest=self.digest,
            journal_dir=str(Path(self.journal_temp.name) / "unknown-journal"),
            now=100.0,
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.preflight.assert_not_called()

    def test_symlink_and_existing_permissions_are_never_changed(self):
        real = self.root / "real-journal"
        real.mkdir(mode=0o700)
        link = self.root / "link-journal"
        link.symlink_to(real, target_is_directory=True)
        result = self.execute(journal_dir=str(link))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(link.is_symlink())

        existing = self.root / "existing-journal"
        existing.mkdir(mode=0o755)
        mode_before = stat.S_IMODE(existing.stat().st_mode)
        result = self.execute(journal_dir=str(existing))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(stat.S_IMODE(existing.stat().st_mode), mode_before)

    def test_replay_blocks_even_after_previous_failure(self):
        self.run.return_value = FakeResult(returncode=3)
        first = self.execute()
        self.assertEqual(first["status"], "PARTIAL")
        self.run.reset_mock()
        second = self.execute()
        self.assertEqual(second["status"], "BLOCKED")
        self.run.assert_not_called()

    def test_timeout_and_unknown_rc0_are_partial(self):
        for index, fake in enumerate((FakeResult(returncode=0, outcome="timeout"), FakeResult(returncode=0, outcome="mystery"), {"returncode": 0})):
            with self.subTest(fake=fake):
                self.run.return_value = fake
                result = self.execute(journal_dir=str(Path(self.journal_temp.name) / ("journal-" + str(index))))
                self.assertEqual(result["status"], "PARTIAL")
                self.assertNotIn("secret", json.dumps(result))

    def test_rc0_unchanged_is_distinct_from_executed(self):
        self.run.return_value = FakeResult()
        result = self.execute()
        self.assertEqual(result["status"], "UNCHANGED")
        self.assertIsInstance(result["observed_available_delta_bytes"], int)

    def test_successful_isolated_fixture_reports_metadata_effect(self):
        def mutate(*args, **kwargs):
            (self.cache / "entry").unlink()
            return FakeResult()

        self.run.side_effect = mutate
        result = self.execute()
        self.assertEqual(result["status"], "EXECUTED")
        self.assertTrue(result["after"]["logical_bytes"] < result["before"]["logical_bytes"])
        journal_file = self.journal / (self.digest + ".jsonl")
        self.assertTrue(journal_file.exists())
        self.assertEqual(len(journal_file.read_text().splitlines()), 2)

    def test_postcheck_failure_is_partial(self):
        self.run.return_value = FakeResult()
        capture = mock.Mock(side_effect=[
            {"fingerprint": "same", "root_identity": "fixture-root", "allocated_bytes": 1, "logical_bytes": 1},
            {"fingerprint": "same", "root_identity": "fixture-root", "allocated_bytes": 1, "logical_bytes": 1},
            {"fingerprint": "after", "root_identity": "other-root", "allocated_bytes": 0, "logical_bytes": 0},
        ])
        with mock.patch.object(cleanup_executor, "capture_target", capture):
            result = self.execute()
        self.assertEqual(result["status"], "PARTIAL")
        self.run.assert_called_once()

    def test_ancestor_only_change_is_not_an_execution_effect(self):
        self.run.return_value = FakeResult()
        capture = mock.Mock(side_effect=[
            {"fingerprint": "same", "root_identity": "fixture-root", "entry_count": 1,
             "allocated_bytes": 1, "logical_bytes": 1, "ancestors": ["a"]},
            {"fingerprint": "same", "root_identity": "fixture-root", "entry_count": 1,
             "allocated_bytes": 1, "logical_bytes": 1, "ancestors": ["a"]},
            {"fingerprint": "same", "root_identity": "fixture-root", "entry_count": 1,
             "allocated_bytes": 1, "logical_bytes": 1, "ancestors": ["volatile-b"]},
        ])
        with mock.patch.object(cleanup_executor, "capture_target", capture):
            result = self.execute(journal_dir=str(Path(self.journal_temp.name) / "ancestor-journal"))
        self.assertEqual(result["status"], "UNCHANGED")

    def test_capacity_drift_retries_once_then_blocks(self):
        samples = iter((100, 50, 90, 40))

        def capacity(_target):
            return {"device": 1, "available_bytes": next(samples), "total_bytes": 1000}

        with mock.patch.object(cleanup_executor, "_capacity_sample", side_effect=capacity):
            result = self.execute()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("capacity", result["reason"])
        self.run.assert_not_called()

    def test_actual_contract_plan_and_complete_adapter_environment(self):
        with mock.patch.object(cleanup_contract, "enable_no_hydration"):
            actual_plan = cleanup_contract.make_plan(
                "go",
                str(self.cache),
                str(self.tool),
                "go version go1.24.0 darwin/arm64",
                home=str(self.home),
                now=time.time(),
                ttl_seconds=60,
            )
        expected_argv, expected_env = cache_adapters.build_command(actual_plan)

        def mutate(*args, **kwargs):
            (self.cache / "entry").unlink()
            return FakeResult()

        preflight = {"ok": True, "evidence": {
            "owner_activity": "idle",
            "target": actual_plan["target"],
            "executable": actual_plan["executable"],
            "version": actual_plan["version"],
        }}
        run_mock = mock.Mock(side_effect=mutate)
        with mock.patch.multiple(
            cleanup_executor,
            enable_no_hydration=mock.Mock(),
            validate_plan=cleanup_contract.validate_plan,
            plan_digest=cleanup_contract.plan_digest,
            capture_target=cleanup_contract.capture_target,
            target_unchanged=cleanup_contract.target_unchanged,
            preflight=mock.Mock(return_value=preflight),
            build_command=cache_adapters.build_command,
            run_command=run_mock,
        ):
            result = cleanup_executor.execute(
                actual_plan,
                confirmed_digest=cleanup_contract.plan_digest(actual_plan),
                journal_dir=str(self.journal),
            )
        self.assertEqual(result["status"], "EXECUTED")
        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.args[0], expected_argv)
        self.assertEqual(run_mock.call_args.kwargs["env"], expected_env)
        self.assertEqual(run_mock.call_args.kwargs["guarded"], True)

    def test_plan_expiry_is_rechecked_after_final_dispatch_guards(self):
        expiring = dict(self.plan, expires_at=101.0)
        with mock.patch.object(cleanup_executor.time, "monotonic", side_effect=(0.0, 0.0, 2.0)):
            result = cleanup_executor.execute(
                expiring,
                confirmed_digest=self.digest,
                journal_dir=str(Path(self.journal_temp.name) / "expiring-journal"),
                now=100.0,
            )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("expired", result["reason"])
        self.run.assert_not_called()

    def test_output_unlink_failure_is_partial_and_retained_output_is_disclosed(self):
        output_paths = []

        def owner(*args, **kwargs):
            for name in ("stdout_path", "stderr_path"):
                path = Path(kwargs[name])
                path.write_bytes(b"PRIVATE-OWNER-OUTPUT")
                path.chmod(0o600)
                output_paths.append(path)
            return FakeResult()

        self.run.side_effect = owner
        with mock.patch.object(cleanup_executor, "_cleanup_output", return_value=False):
            result = self.execute(journal_dir=str(Path(self.journal_temp.name) / "retained-journal"))
        self.assertEqual(result["status"], "PARTIAL")
        self.assertTrue(result["private_output_retained"])
        self.assertIn("private owner output was retained", result["reason"])
        self.assertNotIn("PRIVATE-OWNER-OUTPUT", json.dumps(result))
        self.assertTrue(all(path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600 for path in output_paths))

    def test_signal_handler_is_restored_when_preflight_is_cancelled(self):
        original_term = cleanup_executor.signal.getsignal(cleanup_executor.signal.SIGTERM)
        original_int = cleanup_executor.signal.getsignal(cleanup_executor.signal.SIGINT)
        self.preflight.side_effect = KeyboardInterrupt
        result = self.execute(journal_dir=str(Path(self.journal_temp.name) / "cancel-journal"))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(cleanup_executor.signal.getsignal(cleanup_executor.signal.SIGTERM), original_term)
        self.assertEqual(cleanup_executor.signal.getsignal(cleanup_executor.signal.SIGINT), original_int)
        self.run.assert_not_called()

    def test_normal_user_path_ancestry_is_allowed_but_writable_ancestor_is_not(self):
        users = self.root / "Users"
        users.mkdir(mode=0o755)
        user = users / "fixture-user"
        user.mkdir(mode=0o755)
        private = user / "private-journal"
        private.mkdir(mode=0o700)
        self.assertEqual(
            cleanup_executor._check_directory(str(private), create_missing=False),
            cleanup_executor._system_alias_path(str(private)),
        )

        writable = user / "writable"
        writable.mkdir(mode=0o700)
        writable.chmod(0o770)
        unsafe = writable / "journal"
        with self.assertRaises(cleanup_executor._Blocked):
            cleanup_executor._check_directory(str(unsafe), create_missing=True)
        self.assertFalse(unsafe.exists())

    def test_public_failure_does_not_contain_raw_owner_output(self):
        self.run.return_value = FakeResult(returncode=9, outcome="finished")
        result = self.execute()
        self.assertNotIn(str(self.tool), json.dumps(result))
        self.assertNotIn("stderr", json.dumps(result).lower())


if __name__ == "__main__":
    unittest.main()
