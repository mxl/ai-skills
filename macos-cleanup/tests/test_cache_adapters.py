"""Synthetic cache-adapter tests; no installed owner command is executed."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import cache_adapters  # noqa: E402
import cleanup_contract  # noqa: E402


class CacheAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cache-adapter-test-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.cache = self.home / "cache root with spaces"
        self.cache.mkdir()
        (self.cache / "fixture").write_bytes(b"fixture")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.owner = self.bin / "owner-tool"
        self.owner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.owner.chmod(0o700)
        self.pgrep = self.bin / "pgrep"
        self.pgrep.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        self.pgrep.chmod(0o700)
        self.env = mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "UV_LINK_MODE": "copy"},
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.runtime_guard = mock.patch.object(cache_adapters.audit_runtime, "enable_no_hydration")
        self.runtime_guard.start()
        self.addCleanup(self.runtime_guard.stop)
        self.contract_guard = mock.patch.object(cleanup_contract, "enable_no_hydration")
        self.contract_guard.start()
        self.addCleanup(self.contract_guard.stop)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def which_for(self, name: str) -> str | None:
        if name in {"uv", "go", "npm", "brew", "pnpm"}:
            return str(self.owner)
        if name == "pgrep":
            return str(self.pgrep)
        return None

    def probe_for(self, adapter: str, *, writers: dict[str, object] | None = None):
        writers = writers or {}
        calls: list[tuple[str, ...]] = []

        def probe(argv: list[str], *, env: dict[str, str] | None = None):
            calls.append(tuple(argv[1:]))
            args = argv[1:]
            if adapter == "uv":
                if args == ["--version"]:
                    return self.result(stdout="uv 0.12.12\n")
                if args == ["cache", "dir"]:
                    return self.result(stdout=f"{self.cache}\n")
                if args == ["cache", "prune", "--help"]:
                    return self.result(stdout="Usage: uv cache prune\n--cache-dir <PATH>\n")
            elif adapter == "go":
                if args == ["version"]:
                    return self.result(stdout="go version go1.24.0 darwin/arm64\n")
                if args == ["env", "GOCACHE"]:
                    return self.result(stdout=f"{self.cache}\n")
                if args == ["help", "clean"]:
                    return self.result(stdout="usage: go clean [-cache] [-modcache] [-fuzzcache]\n")
            elif adapter == "npm" and args == ["--version"]:
                return self.result(stdout="10.9.0\n")
            elif adapter == "npm" and args == ["config", "get", "cache"]:
                return self.result(stdout=f"{self.cache}\n")
            elif adapter == "pnpm" and args == ["--version"]:
                return self.result(stdout="9.15.0\n")
            elif adapter == "pnpm" and args == ["store", "path"]:
                return self.result(stdout=f"{self.cache}\n")
            elif adapter in {"brew", "homebrew"} and args == ["--version"]:
                return self.result(stdout="Homebrew 4.6.0\n")
            elif adapter in {"brew", "homebrew"} and args == ["--cache"]:
                return self.result(stdout=f"{self.cache}\n")

            if len(args) == 2 and args[0] == "-x":
                writer = args[1]
                value = writers.get(writer, 1)
                if isinstance(value, BaseException):
                    raise value
                if value == "active":
                    return self.result(returncode=0, stdout="1234\n")
                if value == "multiple":
                    return self.result(returncode=0, stdout="1234\n5678\n")
                if value == "unexpected":
                    return self.result(returncode=2)
                return self.result(returncode=1)
            raise AssertionError(f"unexpected synthetic probe: {argv!r}")

        probe.calls = calls  # type: ignore[attr-defined]
        return probe

    @staticmethod
    def result(
        *, returncode: int = 0, stdout: str = "", stderr: str = "", outcome: str = "finished"
    ) -> dict[str, object]:
        return {
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "outcome": outcome,
        }

    def discover_with(self, adapter: str, probe):
        with mock.patch.object(cache_adapters.shutil, "which", side_effect=self.which_for):
            return cache_adapters.discover(adapter, probe=probe)

    def test_uv_ready_evidence_is_idle_supported_and_non_actionable(self) -> None:
        probe = self.probe_for("uv")
        evidence = self.discover_with("uv", probe)
        self.assertEqual(evidence["status"], "READY")
        self.assertTrue(evidence["supported"])
        self.assertEqual(evidence["owner_activity"], "idle")
        self.assertEqual(evidence["target"], str(self.cache))
        self.assertEqual(evidence["version"], "uv 0.12.12")
        self.assertFalse(evidence["actionable"])
        self.assertIsNone(evidence["estimated_reclaimable_bytes"])
        self.assertNotIn("stderr", evidence)
        self.assertIn(("cache", "prune", "--help"), probe.calls)  # type: ignore[attr-defined]

    def test_go_ready_evidence_checks_all_known_writers(self) -> None:
        probe = self.probe_for("go")
        evidence = self.discover_with("go", probe)
        self.assertTrue(evidence["supported"])
        writer_calls = [call for call in probe.calls if len(call) == 2 and call[0] == "-x"]  # type: ignore[attr-defined]
        self.assertEqual(
            {call[1] for call in writer_calls},
            {
                "go",
                "gopls",
                "Code Helper",
                "Cursor Helper",
                "Zed",
                "GoLand",
                "IntelliJ IDEA",
                "Windsurf",
            },
        )

    def test_pgrep_accepts_only_the_root_owned_immutable_system_path(self) -> None:
        metadata = os.stat(self.pgrep)
        root_metadata = mock.Mock(
            st_mode=stat.S_IFREG | 0o755,
            st_uid=0,
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino,
            st_nlink=metadata.st_nlink,
            st_gid=metadata.st_gid,
            st_size=metadata.st_size,
            st_blocks=metadata.st_blocks,
            st_blksize=metadata.st_blksize,
            st_rdev=metadata.st_rdev,
            st_mtime_ns=metadata.st_mtime_ns,
            st_ctime_ns=metadata.st_ctime_ns,
            st_birthtime_ns=getattr(metadata, "st_birthtime_ns", 0),
            st_flags=getattr(metadata, "st_flags", 0),
        )
        with (
            mock.patch.object(cache_adapters.shutil, "which", return_value="/usr/bin/pgrep"),
            mock.patch.object(cache_adapters.os.path, "realpath", return_value="/usr/bin/pgrep"),
            mock.patch.object(cache_adapters.os, "stat", return_value=root_metadata),
        ):
            self.assertEqual(cache_adapters._pgrep_path(), "/usr/bin/pgrep")

        root_metadata.st_mode = stat.S_IFREG | 0o775
        with (
            mock.patch.object(cache_adapters.shutil, "which", return_value="/usr/bin/pgrep"),
            mock.patch.object(cache_adapters.os.path, "realpath", return_value="/usr/bin/pgrep"),
            mock.patch.object(cache_adapters.os, "stat", return_value=root_metadata),
        ):
            self.assertIsNone(cache_adapters._pgrep_path())

    def test_active_or_multiple_writer_blocks_without_leaking_pids(self) -> None:
        for writer_state, expected in (("active", "active"), ("multiple", "unknown")):
            with self.subTest(writer_state=writer_state):
                probe = self.probe_for("go", writers={"Code Helper": writer_state})
                evidence = self.discover_with("go", probe)
                self.assertFalse(evidence["supported"])
                self.assertEqual(evidence["owner_activity"], expected)
                self.assertTrue(all(pid not in str(evidence) for pid in ("1234", "5678")))

    def test_unknown_version_and_missing_feature_are_review_only(self) -> None:
        probe = self.probe_for("uv")

        def unknown_version(argv: list[str], *, env=None):
            if argv[1:] == ["--version"]:
                return self.result(stdout="uv future-build\n")
            return probe(argv, env=env)

        evidence = self.discover_with("uv", unknown_version)
        self.assertFalse(evidence["supported"])
        self.assertEqual(evidence["owner_activity"], "unknown")
        self.assertTrue(evidence["reasons"])

        def no_prune_help(argv: list[str], *, env=None):
            if argv[1:] == ["cache", "prune", "--help"]:
                return self.result(stdout="usage: uv cache\n")
            return probe(argv, env=env)

        evidence = self.discover_with("uv", no_prune_help)
        self.assertFalse(evidence["supported"])
        self.assertTrue(any("cache prune" in reason for reason in evidence["reasons"]))

    def test_uv_version_policy_rejects_old_future_and_prerelease_versions(self) -> None:
        for version in ("uv 0.9.3\n", "uv 0.12.13\n", "uv 0.12.12-rc.1\n"):
            with self.subTest(version=version):
                probe = self.probe_for("uv")

                def version_probe(argv: list[str], *, env=None, value=version):
                    if argv[1:] == ["--version"]:
                        return self.result(stdout=value)
                    return probe(argv, env=env)

                evidence = self.discover_with("uv", version_probe)
                self.assertFalse(evidence["supported"])
                self.assertTrue(
                    any("stable version policy" in reason for reason in evidence["reasons"])
                )

    def test_go_version_policy_rejects_old_future_and_prerelease_versions(self) -> None:
        for version in (
            "go version go1.19.13 darwin/arm64\n",
            "go version go1.27.2 darwin/arm64\n",
            "go version go1.27.1-rc.1 darwin/arm64\n",
        ):
            with self.subTest(version=version):
                probe = self.probe_for("go")

                def version_probe(argv: list[str], *, env=None, value=version):
                    if argv[1:] == ["version"]:
                        return self.result(stdout=value)
                    return probe(argv, env=env)

                evidence = self.discover_with("go", version_probe)
                self.assertFalse(evidence["supported"])
                self.assertTrue(
                    any("stable version policy" in reason for reason in evidence["reasons"])
                )

    def test_uv_unverified_link_mode_is_not_treated_as_default(self) -> None:
        with mock.patch.dict(os.environ, {"UV_LINK_MODE": ""}, clear=False):
            evidence = self.discover_with("uv", self.probe_for("uv"))
        self.assertFalse(evidence["supported"])
        self.assertEqual(evidence["link_mode"], "unverified")
        self.assertIn("unset", " ".join(evidence["reasons"]))

    def test_uv_symlink_and_centralized_environment_are_disclosed_and_blocked(self) -> None:
        with mock.patch.dict(os.environ, {"UV_LINK_MODE": "symlink"}, clear=False):
            evidence = self.discover_with("uv", self.probe_for("uv"))
        self.assertFalse(evidence["supported"])
        self.assertIn("symlink", " ".join(evidence["reasons"]))
        self.assertIn("symlink", " ".join(evidence["warnings"]))

        with mock.patch.dict(
            os.environ,
            {"UV_PROJECT_ENVIRONMENT": str(self.root / "central-env")},
            clear=False,
        ):
            evidence = self.discover_with("uv", self.probe_for("uv"))
        self.assertFalse(evidence["supported"])
        self.assertIn("centralized", " ".join(evidence["reasons"]))
        self.assertNotIn(str(self.root / "central-env"), str(evidence))

    def test_npm_and_homebrew_are_review_only_not_plan_capable(self) -> None:
        npm = self.discover_with("npm", self.probe_for("npm"))
        self.assertEqual(npm["status"], "REVIEW-ONLY")
        self.assertFalse(npm["supported"])
        self.assertEqual(npm["target"], str(self.cache))

        brew = self.discover_with("brew", self.probe_for("brew"))
        self.assertEqual(brew["status"], "REVIEW-ONLY")
        self.assertFalse(brew["supported"])
        self.assertEqual(brew["target"], str(self.cache))

    def actual_plan(self, adapter: str, version: str):
        return cleanup_contract.make_plan(
            adapter,
            str(self.cache),
            str(self.owner),
            version,
            home=str(self.home),
        )

    def test_prepare_calls_actual_contract_make_plan_only_after_ready_evidence(self) -> None:
        probe = self.probe_for("uv")
        with mock.patch.object(
            cache_adapters.shutil, "which", side_effect=self.which_for
        ) as which:
            plan = cache_adapters.prepare("uv", probe=probe)
        self.assertEqual(plan["adapter"], "uv")
        self.assertEqual(plan["action"], "uv-cache-prune")
        self.assertEqual(plan["target"], str(self.cache))
        self.assertNotIn("actionable", plan)
        self.assertTrue(which.called)

    def test_prepare_raises_when_discovery_is_not_supported(self) -> None:
        probe = self.probe_for("uv", writers={"uv": "active"})
        with mock.patch.object(cache_adapters.shutil, "which", side_effect=self.which_for):
            with self.assertRaises(cache_adapters.AdapterError):
                cache_adapters.prepare("uv", probe=probe)

    def test_prepare_make_plan_failure_is_sanitized_adapter_error(self) -> None:
        probe = self.probe_for("uv")
        with mock.patch.object(
            cache_adapters.shutil, "which", side_effect=self.which_for
        ), mock.patch.object(
            cleanup_contract,
            "make_plan",
            side_effect=ValueError("private path and command details"),
        ):
            with self.assertRaises(cache_adapters.AdapterError) as raised:
                cache_adapters.prepare("uv", probe=probe)
        self.assertEqual(str(raised.exception), "cleanup plan could not be prepared")
        self.assertNotIn("private", str(raised.exception))

    def test_preflight_actual_plan_rechecks_target_and_executable_identity(self) -> None:
        probe = self.probe_for("go")
        with mock.patch.object(cache_adapters.shutil, "which", side_effect=self.which_for):
            plan = cache_adapters.prepare("go", probe=probe)
            result = cache_adapters.preflight(plan, probe=probe)
        self.assertTrue(result["ok"])
        self.assertTrue(result["evidence"]["dynamic_scope"])
        self.assertIn("authoritative", result["evidence"]["lock_semantics"])

        # Replace the executable at the same path.  A path-only comparison is
        # insufficient; cleanup_contract.target_unchanged must reject it.
        self.owner.unlink()
        self.owner.write_text("#!/bin/sh\n# replaced\nexit 0\n", encoding="utf-8")
        self.owner.chmod(0o700)
        with mock.patch.object(cache_adapters.shutil, "which", side_effect=self.which_for):
            blocked = cache_adapters.preflight(plan, probe=probe)
        self.assertFalse(blocked["ok"])
        self.assertTrue(any("identity" in reason for reason in blocked["reasons"]))

        active_probe = self.probe_for("go", writers={"gopls": "active"})
        with mock.patch.object(cache_adapters.shutil, "which", side_effect=self.which_for):
            blocked = cache_adapters.preflight(plan, probe=active_probe)
        self.assertFalse(blocked["ok"])
        self.assertTrue(any("activity" in reason for reason in blocked["reasons"]))

    def test_build_command_is_fixed_allowlist_with_full_safe_environment(self) -> None:
        uv_plan = self.actual_plan("uv", "uv 0.12.12")
        go_plan = self.actual_plan("go", "go version go1.24.0 darwin/arm64")
        with mock.patch.dict(os.environ, {"API_TOKEN": "must-not-pass", "GOFLAGS": "-mod=mod"}):
            uv_argv, uv_env = cache_adapters.build_command(uv_plan)
            go_argv, go_env = cache_adapters.build_command(go_plan)

        self.assertEqual(
            uv_argv,
            [uv_plan["executable"], "--cache-dir", str(self.cache), "cache", "prune"],
        )
        self.assertEqual(uv_env["HOME"], str(self.home))
        self.assertIn("PATH", uv_env)
        self.assertEqual(uv_env["UV_NO_CONFIG"], "1")
        self.assertEqual(uv_env["UV_NO_CACHE"], "0")
        self.assertEqual(uv_env["UV_CACHE_DIR"], str(self.cache))
        self.assertEqual(uv_env["UV_LOCK_TIMEOUT"], "30")
        self.assertEqual(uv_env["UV_LINK_MODE"], "copy")
        self.assertNotIn("API_TOKEN", uv_env)
        self.assertNotIn("--force", uv_argv)
        self.assertEqual(go_argv, [go_plan["executable"], "clean", "-cache"])
        self.assertEqual(go_env["HOME"], str(self.home))
        self.assertEqual(go_env["GOCACHE"], str(self.cache))
        self.assertEqual(go_env["GOFLAGS"], "")
        self.assertEqual(go_env["GOCACHEPROG"], "")
        self.assertEqual(go_env["GOTOOLCHAIN"], "local")
        self.assertEqual(go_env["GOENV"], "off")
        self.assertEqual(go_env["GOWORK"], "off")
        self.assertNotIn("API_TOKEN", go_env)
        self.assertNotIn("-modcache", go_argv)
        self.assertNotIn("-fuzzcache", go_argv)

    def test_guard_is_established_before_discovery_filesystem_access(self) -> None:
        with mock.patch.object(
            cache_adapters.audit_runtime,
            "enable_no_hydration",
            side_effect=cache_adapters.audit_runtime.GuardUnavailable(),
        ), mock.patch.object(cache_adapters.shutil, "which") as which:
            evidence = cache_adapters.discover("uv", probe=self.probe_for("uv"))
        which.assert_not_called()
        self.assertEqual(evidence["guard"], "unavailable")
        self.assertEqual(evidence["owner_activity"], "unknown")
        self.assertFalse(evidence["supported"])

    def test_guard_loss_stops_all_subsequent_probes(self) -> None:
        calls: list[tuple[str, ...]] = []

        def guard_lost(argv: list[str], *, env=None):
            calls.append(tuple(argv[1:]))
            return self.result(outcome="guard-unavailable")

        with mock.patch.object(cache_adapters.shutil, "which", side_effect=self.which_for):
            evidence = cache_adapters.discover("uv", probe=guard_lost)
        self.assertEqual(calls, [("--version",)])
        self.assertEqual(evidence["guard"], "unavailable")
        self.assertFalse(evidence["supported"])

    def test_cancellation_from_probe_outcome_propagates_without_more_probes(self) -> None:
        calls: list[tuple[str, ...]] = []

        def cancel(argv: list[str], *, env=None):
            calls.append(tuple(argv[1:]))
            return self.result(outcome="cancelled")

        with mock.patch.object(cache_adapters.shutil, "which", side_effect=self.which_for):
            with self.assertRaises(cache_adapters.AdapterCancelled):
                cache_adapters.discover("uv", probe=cancel)
        self.assertEqual(calls, [("--version",)])

    def test_default_probe_checks_guard_before_temp_directory(self) -> None:
        with mock.patch.object(
            cache_adapters.audit_runtime,
            "enable_no_hydration",
            side_effect=cache_adapters.audit_runtime.GuardUnavailable(),
        ), mock.patch.object(cache_adapters.tempfile, "TemporaryDirectory") as temporary:
            with self.assertRaises(cache_adapters.audit_runtime.GuardUnavailable):
                cache_adapters._default_probe([str(self.owner), "--version"])
        temporary.assert_not_called()

    def test_default_probe_uses_guarded_runtime_and_removes_private_outputs(self) -> None:
        observed: dict[str, object] = {}

        class Result:
            returncode = 0
            outcome = "finished"

        def fake_run(argv, **kwargs):
            observed["argv"] = argv
            observed.update(kwargs)
            observed["parent_exists_during_call"] = Path(kwargs["stdout_path"]).parent.exists()
            Path(kwargs["stdout_path"]).write_text("ok\n", encoding="utf-8")
            Path(kwargs["stderr_path"]).write_text("private\n", encoding="utf-8")
            return Result()

        with mock.patch.object(cache_adapters.audit_runtime, "run_command", side_effect=fake_run):
            result = cache_adapters._default_probe([str(self.owner), "--version"])
        self.assertEqual(result["stdout"], "ok\n")
        self.assertEqual(result["stderr"], "private\n")
        self.assertTrue(observed["guarded"])
        self.assertTrue(observed["parent_exists_during_call"])
        self.assertFalse(Path(observed["stdout_path"]).exists())
        self.assertFalse(Path(observed["stderr_path"]).exists())


if __name__ == "__main__":
    unittest.main()
