#!/usr/bin/env python3
"""Synthetic, read-only tests for the structured audit orchestrator."""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import signal
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
audit_all = importlib.import_module("audit_all")


class FakeResult:
    def __init__(
        self,
        returncode: int | None = 0,
        *,
        outcome: str = "finished",
        timed_out: bool = False,
    ):
        self.returncode = returncode
        self.outcome = outcome
        self.timed_out = timed_out
        self.duration_seconds = 0.01


def record(record_type: str, module: str, status: str, **fields: object) -> str:
    value = {
        "schema_version": "1",
        "record_type": record_type,
        "module": module,
        "status": status,
    }
    value.update(fields)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="macos-cleanup-orchestrator-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.module_dir = self.root / "modules"
        self.module_dir.mkdir()
        for module in audit_all.KNOWN_MODULES:
            script = self.module_dir / f"audit-{module}.sh"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_literal_unicode_separators_are_not_jsonl_delimiters(self) -> None:
        target = '<home>/cache\u0085part\u2028part\u2029part'
        data = record(
            "cleanup_candidate", "caches", "MEASURED", path=target, owner="fixture",
            allocated_bytes=4096, logical_bytes=4096, classification="REVIEW-ONLY",
            proposed_action=None, recovery="rebuild", risk="unknown activity",
            confidence="high", actionable=False, sensitive=True,
        ) + "\n" + record("module_status", "caches", "COMPLETE") + "\n"
        self.assertIn("\u2028", data)
        parsed = audit_all._parse_module_output(data, "caches", str(self.home))
        self.assertEqual(len(parsed.records), 2)
        self.assertEqual(parsed.records[0]["path"], target)
        self.assertEqual(parsed.malformed_line_count, 0)
        self.assertTrue(parsed.final_status_seen)

    def run_main(
        self,
        output: Path,
        outputs: dict[str, str | bytes],
        *,
        modules: str = "caches",
        results: dict[str, FakeResult] | None = None,
        guard: bool = True,
        signal_during_parse: bool = False,
    ) -> tuple[int, list[list[str]]]:
        calls: list[list[str]] = []
        results = results or {}

        def fake_guard() -> None:
            if not guard:
                raise audit_all.GuardUnavailable()

        def fake_run(argv: list[str], **kwargs: object) -> FakeResult:
            module = Path(argv[0]).name.removeprefix("audit-").removesuffix(".sh")
            calls.append(argv)
            child_output = outputs.get(module, "")
            if isinstance(child_output, bytes):
                Path(kwargs["stdout_path"]).write_bytes(child_output)
            else:
                Path(kwargs["stdout_path"]).write_text(child_output, encoding="utf-8")
            Path(kwargs["stderr_path"]).write_text("PRIVATE secret token", encoding="utf-8")
            return results.get(module, FakeResult())

        original_parse = audit_all._parse_module_output

        def fake_parse(*args: object, **kwargs: object) -> audit_all.ParsedModule:
            parsed = original_parse(*args, **kwargs)
            if signal_during_parse:
                os.kill(os.getpid(), signal.SIGTERM)
            return parsed

        args = [
            "--target",
            str(self.home),
            "--output",
            str(output),
            "--timeout",
            "1",
            "--modules",
            modules,
        ]
        with mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "MACOS_CLEANUP_MODULE_DIR": str(self.module_dir),
            },
            clear=False,
        ), mock.patch.object(audit_all, "enable_no_hydration", fake_guard), mock.patch.object(
            audit_all, "run_command", fake_run
        ), mock.patch.object(audit_all, "_parse_module_output", fake_parse), contextlib.redirect_stdout(io.StringIO()):
            code = audit_all.main(args)
        return code, calls

    def read_inventory(self, output: Path) -> list[dict[str, object]]:
        return [json.loads(line) for line in (output / "inventory.jsonl").read_text().splitlines()]

    def test_initial_guard_failure_precedes_user_filesystem_access(self) -> None:
        output = self.root / "guard-failure"
        with mock.patch.object(
            audit_all, "enable_no_hydration", side_effect=audit_all.GuardUnavailable()
        ), mock.patch.object(audit_all, "_validate_config") as validate, mock.patch.object(
            audit_all, "_create_fresh_output"
        ) as create_output, contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(
            io.StringIO()
        ) as stderr:
            code = audit_all.main(
                ["--target", str(self.home), "--output", str(output), "--modules", "cloud"]
            )
        self.assertEqual(code, audit_all.GUARD_EXIT_CODE)
        validate.assert_not_called()
        create_output.assert_not_called()
        self.assertFalse(output.exists())
        machine_status = json.loads(stdout.getvalue())
        self.assertEqual(machine_status["status"], "UNAVAILABLE")
        self.assertEqual(machine_status["outcome"], "guard-unavailable")
        self.assertEqual(stderr.getvalue(), audit_all.INITIAL_GUARD_DIAGNOSTIC + "\n")

    def test_valid_records_survive_malformed_lines_and_candidates_are_demoted(self) -> None:
        candidate_path = self.home / "name`<script>\nfile"
        output = self.root / "run"
        child_output = "\n".join(
            [
                record(
                    "capacity",
                    "caches",
                    "COMPLETE",
                    path="/TestVolume",
                    total_bytes=1000,
                    used_bytes=500,
                    available_bytes=500,
                    sensitive=False,
                ),
                '{"schema_version":"1","record_type":"broken",',
                record("future_record", "caches", "COMPLETE", sensitive=False),
                record("orchestrator_module", "caches", "COMPLETE", sensitive=False),
                record("audit_summary", "caches", "COMPLETE", sensitive=False),
                '{"schema_version":"1","record_type":"path","module":"caches","status":"MEASURED","allocated_bytes":NaN,"sensitive":true}',
                '{"schema_version":"1","record_type":"path","module":"caches","status":"MEASURED","allocated_bytes":1,"allocated_bytes":2,"sensitive":true}',
                '{"schema_version":"1","record_type":"path","module":"caches","status":"MEASURED","allocated_bytes":1,"logical_bytes":1,"sensitive":true,"unknown":1e999}',
                "[" * 1200 + "]" * 1200,
                record(
                    "cleanup_candidate",
                    "caches",
                    "MEASURED",
                    path=str(candidate_path),
                    owner="owner@example.com",
                    allocated_bytes=100,
                    logical_bytes=120,
                    estimated_reclaimable_bytes=None,
                    classification="REVIEW-ONLY",
                    proposed_action=None,
                    review_action="Review owner activity and supported cleanup scope",
                    owner_activity="unknown",
                    scope_state="unverified",
                    verification_state="required",
                    recovery="rebuild",
                    risk="review",
                    confidence="high",
                    actionable=True,
                    command="rm -rf /private/secret",
                    nested={"status": "COMPLETE", "actionable": True},
                    sensitive=True,
                ),
                record("module_status", "caches", "COMPLETE", evidence="fixture complete"),
            ]
        ) + "\n"
        code, _ = self.run_main(output, {"caches": child_output})
        self.assertEqual(code, 0)

        module_lines = (output / "modules/caches.jsonl").read_text().splitlines()
        self.assertEqual(len(module_lines), 3)
        module_records = [json.loads(line) for line in module_lines]
        candidate = next(item for item in module_records if item["record_type"] == "cleanup_candidate")
        self.assertFalse(candidate["actionable"])
        self.assertEqual(candidate["classification"], "REVIEW-ONLY")
        self.assertIsNone(candidate["proposed_action"])
        self.assertIsNone(candidate["estimated_reclaimable_bytes"])
        self.assertEqual(candidate["review_action"], "Review owner activity and supported cleanup scope")
        self.assertNotIn("command", candidate)
        self.assertTrue(candidate["nested"]["actionable"])

        inventory = self.read_inventory(output)
        summary = inventory[-1]
        self.assertEqual(summary["actionable_candidate_count"], 0)
        self.assertEqual(summary["review_only_candidate_count"], 1)
        self.assertEqual(summary["malformed_line_count"], 5)
        self.assertEqual(summary["invalid_record_count"], 2)
        self.assertEqual(summary["unknown_record_count"], 1)
        self.assertEqual(summary["status"], "PARTIAL")

        report = (output / "report.md").read_text()
        self.assertIn("## Review-Only Candidates", report)
        self.assertNotIn("owner@example.com", report)
        self.assertNotIn("<script>", report)
        self.assertIn("&lt;script&gt;", report)
        self.assertIn("&#96;", report)
        self.assertIn("review guidance", report)
        self.assertIn("Review owner activity and supported cleanup scope", report)
        self.assertIn("No cleanup commands invoked; collectors launched with no-hydration policy.", report)
        self.assertNotIn("No files were deleted, moved, downloaded, or evicted.", report)
        self.assertNotIn(str(self.home), report)
        self.assertNotIn("PRIVATE secret token", report)

    def test_footer_and_nonzero_exit_downgrade_module(self) -> None:
        output = self.root / "missing-footer"
        child_output = "\n".join(
            [
                record("module_status", "paths", "COMPLETE", evidence="status was not the footer"),
                record(
                    "path",
                    "paths",
                    "MEASURED",
                    path="<home>",
                    allocated_bytes=1,
                    logical_bytes=1,
                    sensitive=True,
                ),
            ]
        )
        self.run_main(output, {"paths": child_output}, modules="paths")
        inventory = self.read_inventory(output)
        module = next(item for item in inventory if item.get("record_type") == "orchestrator_module")
        self.assertEqual(module["status"], "PARTIAL")
        self.assertFalse(module["final_status_seen"])
        self.assertEqual(module["outcome"], "missing-final-status")

        nonzero_output = self.root / "nonzero"
        status_only = record("module_status", "paths", "COMPLETE", evidence="fixture") + "\n"
        self.run_main(
            nonzero_output,
            {"paths": status_only},
            modules="paths",
            results={"paths": FakeResult(3)},
        )
        inventory = self.read_inventory(nonzero_output)
        module = next(item for item in inventory if item.get("record_type") == "orchestrator_module")
        self.assertEqual(module["status"], "PARTIAL")
        self.assertEqual(module["exit_code"], 3)
        self.assertEqual(module["outcome"], "nonzero-exit")

    def test_timeout_does_not_block_next_module(self) -> None:
        output = self.root / "timeout"
        cache_output = record(
            "cleanup_candidate",
            "caches",
            "MEASURED",
            path="<home>/.cache",
            owner="test",
            allocated_bytes=10,
            logical_bytes=10,
            classification="APPROVAL-REQUIRED",
            proposed_action="review only",
            recovery="rebuild",
            risk="rebuild",
            confidence="low",
            actionable=True,
            sensitive=True,
        ) + "\n"
        cloud_output = record(
            "cloud_root",
            "cloud",
            "DISCOVERED",
            path="<home>/Library/CloudStorage/<provider>",
            owner="provider",
            provider_kind="test-cloud",
            domain_label="test-cloud account 1",
            allocated_bytes=None,
            logical_bytes=None,
            sync_state="unknown",
            local_state="unknown",
            size_relation="unknown",
            eviction_method="unverified",
            sensitive=True,
        ) + "\n" + record("module_status", "cloud", "PARTIAL", evidence="item state unavailable") + "\n"
        _, calls = self.run_main(
            output,
            {"caches": cache_output, "cloud": cloud_output},
            modules="caches,cloud",
            results={"caches": FakeResult(timed_out=True)},
        )
        self.assertEqual([Path(argv[0]).name for argv in calls], ["audit-caches.sh", "audit-cloud.sh"])
        inventory = self.read_inventory(output)
        cache_status = next(item for item in inventory if item.get("child_module") == "caches")
        self.assertEqual(cache_status["outcome"], "timeout")
        self.assertTrue(any(item.get("record_type") == "cloud_root" for item in inventory))

    def test_runtime_cancel_stops_remaining_modules_and_returns_130(self) -> None:
        output = self.root / "cancelled"
        statuses = {
            "paths": record("module_status", "paths", "COMPLETE") + "\n",
            "cloud": record("module_status", "cloud", "COMPLETE") + "\n",
        }
        code, calls = self.run_main(
            output,
            statuses,
            modules="paths,cloud",
            results={"paths": FakeResult(0, outcome="cancelled")},
        )
        self.assertEqual(code, 130)
        self.assertEqual([Path(argv[0]).name for argv in calls], ["audit-paths.sh"])
        inventory = self.read_inventory(output)
        paths_status = next(item for item in inventory if item.get("child_module") == "paths")
        cloud_status = next(item for item in inventory if item.get("child_module") == "cloud")
        summary = inventory[-1]
        self.assertEqual(paths_status["outcome"], "cancelled")
        self.assertEqual(paths_status["status"], "PARTIAL")
        self.assertEqual(cloud_status["status"], "NOT-RUN")
        self.assertEqual(cloud_status["outcome"], "cancelled")
        self.assertEqual(summary["attempted_modules"], ["paths"])
        self.assertEqual(summary["executed_modules"], ["paths"])
        self.assertEqual(summary["not_run_modules"], ["cloud"])
        self.assertEqual(summary["termination_reason"], "cancelled")
        self.assertEqual((output / "modules/cloud.jsonl").read_text(), "")

    def test_sigterm_during_parse_cleans_temp_files_restores_handler_and_stops(self) -> None:
        output = self.root / "sigterm-during-parse"
        statuses = {
            "caches": record("module_status", "caches", "COMPLETE") + "\n",
            "cloud": record("module_status", "cloud", "COMPLETE") + "\n",
        }
        original_handler = signal.getsignal(signal.SIGTERM)
        code, calls = self.run_main(
            output,
            statuses,
            modules="caches,cloud",
            signal_during_parse=True,
        )
        self.assertEqual(code, 130)
        self.assertEqual([Path(argv[0]).name for argv in calls], ["audit-caches.sh"])
        self.assertEqual(signal.getsignal(signal.SIGTERM), original_handler)
        inventory = self.read_inventory(output)
        cache_status = next(item for item in inventory if item.get("child_module") == "caches")
        cloud_status = next(item for item in inventory if item.get("child_module") == "cloud")
        self.assertEqual(cache_status["status"], "PARTIAL")
        self.assertEqual(cache_status["outcome"], "cancelled")
        self.assertEqual(cloud_status["status"], "NOT-RUN")
        self.assertNotIn("PRIVATE secret token", "".join(path.read_text() for path in output.rglob("*") if path.is_file()))
        self.assertEqual([path.name for path in output.iterdir() if path.name.startswith(".")], [])

    def test_runtime_guard_loss_stops_remaining_modules(self) -> None:
        output = self.root / "guard-loss"
        statuses = {
            "paths": record("module_status", "paths", "COMPLETE") + "\n",
            "cloud": record("module_status", "cloud", "COMPLETE") + "\n",
        }
        _, calls = self.run_main(
            output,
            statuses,
            modules="paths,cloud",
            results={"paths": FakeResult(0, outcome="guard-unavailable")},
        )
        self.assertEqual([Path(argv[0]).name for argv in calls], ["audit-paths.sh"])
        inventory = self.read_inventory(output)
        paths_status = next(item for item in inventory if item.get("child_module") == "paths")
        cloud_status = next(item for item in inventory if item.get("child_module") == "cloud")
        summary = inventory[-1]
        self.assertEqual(paths_status["status"], "UNAVAILABLE")
        self.assertEqual(cloud_status["status"], "NOT-RUN")
        self.assertEqual(summary["attempted_modules"], ["paths"])
        self.assertEqual(summary["executed_modules"], [])
        self.assertEqual(summary["not_run_modules"], ["cloud"])

    def test_output_limit_and_missing_return_code_are_not_success(self) -> None:
        output = self.root / "output-limit"
        status_only = record("module_status", "caches", "COMPLETE") + "\n"
        self.run_main(
            output,
            {"caches": status_only},
            results={"caches": FakeResult(0, outcome="output-limit")},
        )
        inventory = self.read_inventory(output)
        module = next(item for item in inventory if item.get("child_module") == "caches")
        self.assertEqual(module["status"], "PARTIAL")
        self.assertEqual(module["outcome"], "output-limit")
        self.assertTrue(module["final_status_seen"])
        self.assertEqual(inventory[-1]["status"], "PARTIAL")

        missing_rc_output = self.root / "missing-return-code"
        self.run_main(
            missing_rc_output,
            {"caches": status_only},
            results={"caches": FakeResult(None)},
        )
        inventory = self.read_inventory(missing_rc_output)
        module = next(item for item in inventory if item.get("record_type") == "orchestrator_module")
        self.assertEqual(module["status"], "PARTIAL")
        self.assertEqual(module["outcome"], "invalid-return-code")

    def test_subset_reports_omitted_and_unsupported_coverage(self) -> None:
        output = self.root / "subset"
        cloud_output = record("module_status", "cloud", "COMPLETE", evidence="fixture") + "\n"
        self.run_main(output, {"cloud": cloud_output}, modules="cloud")
        summary = self.read_inventory(output)[-1]
        self.assertEqual(summary["status"], "COMPLETE")
        self.assertEqual(summary["coverage_status"], "PARTIAL")
        self.assertEqual(summary["requested_modules"], ["cloud"])
        self.assertEqual(summary["omitted_modules"], ["capabilities", "paths", "caches"])
        self.assertEqual(summary["unsupported_modules"], list(audit_all.COVERAGE_CATEGORIES))
        report = (output / "report.md").read_text()
        self.assertIn("Omitted supported modules", report)
        self.assertIn("Unsupported broad categories", report)
        self.assertNotIn("no providers", report.lower())

    def test_guard_unavailable_fails_closed_before_output_creation(self) -> None:
        output = self.root / "guard-unavailable"
        code, calls = self.run_main(
            output,
            {module: record("module_status", module, "COMPLETE") + "\n" for module in audit_all.KNOWN_MODULES},
            modules="capabilities,paths",
            guard=False,
        )
        self.assertEqual(code, audit_all.GUARD_EXIT_CODE)
        self.assertEqual(calls, [])
        self.assertFalse(output.exists())

    def test_default_paths_are_narrow_and_files_have_private_modes(self) -> None:
        output = self.root / "modes"
        statuses = {module: record("module_status", module, "COMPLETE") + "\n" for module in audit_all.KNOWN_MODULES}
        _, calls = self.run_main(output, statuses, modules="paths")
        self.assertEqual(calls[0][1:], ["--exact", "/Applications", str(self.home / "Library/Caches")])
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((output / "modules").stat().st_mode), 0o700)
        for path in (output / "inventory.jsonl", output / "report.md", output / "modules/paths.jsonl", output / "modules/paths.stderr"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual([name for name in os.listdir(output) if name.startswith(".")], [])

    def test_capacity_drift_requires_one_target_identity(self) -> None:
        records = [
            {
                "record_type": "capacity",
                "available_bytes": 100,
                "path": "/volume-a",
                "volume_device": "disk-a",
            },
            {
                "record_type": "capacity",
                "available_bytes": 80,
                "path": "/volume-b",
                "volume_device": "disk-b",
            },
        ]
        drift = audit_all._capacity_drift(records)
        self.assertEqual(drift["status"], "NOT-MEASURED")
        self.assertIn("target-volume identity", drift["reason"])

    def test_leaf_creation_race_does_not_accept_or_clobber_sentinel(self) -> None:
        output = self.root / "raced-run"
        original_mkdir = audit_all.os.mkdir
        sentinel = output / "sentinel"

        def racing_mkdir(path: str | bytes, mode: int = 0o777) -> None:
            if os.fspath(path) == os.fspath(output):
                original_mkdir(path, mode)
                sentinel.write_text("preserve me", encoding="utf-8")
                raise FileExistsError()
            original_mkdir(path, mode)

        with mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "MACOS_CLEANUP_MODULE_DIR": str(self.module_dir)},
            clear=False,
        ), mock.patch.object(audit_all.os, "mkdir", side_effect=racing_mkdir):
            code = audit_all.main(
                ["--target", str(self.home), "--output", str(output), "--modules", "cloud"]
            )
        self.assertEqual(code, 2)
        self.assertEqual(sentinel.read_text(), "preserve me")
        self.assertFalse((output / "inventory.jsonl").exists())

    def test_invalid_utf8_after_valid_data_is_not_silently_accepted(self) -> None:
        output = self.root / "invalid-utf8"
        valid = record("module_status", "caches", "COMPLETE") + "\n"
        code, _ = self.run_main(output, {"caches": valid.encode("utf-8") + b"\xff\n"})
        self.assertEqual(code, 0)
        inventory = self.read_inventory(output)
        module = next(item for item in inventory if item.get("child_module") == "caches")
        self.assertFalse(module["final_status_seen"])
        self.assertEqual(module["status"], "PARTIAL")
        self.assertEqual(module["malformed_line_count"], 1)
        self.assertEqual(len((output / "modules/caches.jsonl").read_text().splitlines()), 1)

    def test_collision_and_symlink_ancestry_are_refused(self) -> None:
        existing = self.root / "existing"
        existing.mkdir()
        before = sorted(existing.iterdir())
        with mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "MACOS_CLEANUP_MODULE_DIR": str(self.module_dir)},
            clear=False,
        ):
            self.assertEqual(
                audit_all.main(
                    ["--target", str(self.home), "--output", str(existing), "--modules", "cloud"]
                ),
                2,
            )
        self.assertEqual(sorted(existing.iterdir()), before)

        real_parent = self.root / "real-parent"
        real_parent.mkdir()
        symlink_parent = self.root / "symlink-parent"
        symlink_parent.symlink_to(real_parent, target_is_directory=True)
        unsafe = symlink_parent / "run"
        with mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "MACOS_CLEANUP_MODULE_DIR": str(self.module_dir)},
            clear=False,
        ):
            self.assertEqual(
                audit_all.main(
                    ["--target", str(self.home), "--output", str(unsafe), "--modules", "cloud"]
                ),
                2,
            )
        self.assertFalse(unsafe.exists())

    def test_module_argument_validation_happens_before_output_creation(self) -> None:
        for modules in ("", "cloud,cloud", "cloud,unknown", ",cloud", "cloud,"):
            output = self.root / f"invalid-{len(list(self.root.iterdir()))}"
            with self.subTest(modules=modules), mock.patch.dict(
                os.environ,
                {"HOME": str(self.home), "MACOS_CLEANUP_MODULE_DIR": str(self.module_dir)},
                clear=False,
            ):
                with self.assertRaises(SystemExit):
                    audit_all.main(
                        ["--target", str(self.home), "--output", str(output), "--modules", modules]
                    )
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
