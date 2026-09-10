#!/usr/bin/env python3
"""Approval-bound dispatch for the two trusted cache actions.

This module intentionally has no cleanup registry.  The adapter is the only
source of command and environment semantics; this module owns consent
binding, replay protection, capacity checkpoints, and conservative outcome
classification.
"""

from __future__ import annotations

import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import sys
import threading
import time
from typing import Any, Mapping


# Dependencies are imported when available, but keeping the names defined lets
# the executor be imported while the parallel contract/adapter work is still
# being assembled. Tests replace these names with small fakes.
try:
    from cleanup_contract import capture_target, plan_digest, target_unchanged, validate_plan  # type: ignore
except ImportError:  # pragma: no cover - dependency may be built in parallel
    capture_target = None  # type: ignore[assignment]
    plan_digest = None  # type: ignore[assignment]
    target_unchanged = None  # type: ignore[assignment]
    validate_plan = None  # type: ignore[assignment]

try:
    from cache_adapters import build_command, preflight  # type: ignore
except ImportError:  # pragma: no cover - dependency may be built in parallel
    build_command = None  # type: ignore[assignment]
    preflight = None  # type: ignore[assignment]

try:
    from audit_runtime import enable_no_hydration, run_command  # type: ignore
except ImportError:  # pragma: no cover - dependency may be built in parallel
    enable_no_hydration = None  # type: ignore[assignment]
    run_command = None  # type: ignore[assignment]


_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_SUPPORTED = {
    ("uv", "uv-cache-prune"),
    ("go", "go-build-cache-clean"),
}
_PLAN_KEYS = frozenset(
    {
        "schema_version",
        "adapter",
        "action",
        "target",
        "home",
        "target_state",
        "executable",
        "executable_state",
        "version",
        "created_at",
        "expires_at",
        "scope",
        "risk",
        "estimated_reclaimable_bytes",
    }
)
_COMMAND_TIMEOUT = 120.0
_CAPACITY_PAUSE = 0.01
_DRIFT_BYTES = 1 << 30
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_DANGEROUS_COMMAND_WORDS = {"rm", "sudo", "kill", "killall", "pkill"}


class _Blocked(Exception):
    """Internal exception carrying only a fixed public reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _install_cancel_handlers() -> list[tuple[int, Any]]:
    if threading.current_thread() is not threading.main_thread():
        return []

    def cancel(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    installed: list[tuple[int, Any]] = []
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        try:
            previous = signal.getsignal(signal_number)
            signal.signal(signal_number, cancel)
            installed.append((signal_number, previous))
        except (OSError, ValueError):
            for old_signal, previous in reversed(installed):
                try:
                    signal.signal(old_signal, previous)
                except (OSError, ValueError):
                    pass
            return []
    return installed


def _restore_cancel_handlers(installed: list[tuple[int, Any]]) -> None:
    for signal_number, previous in reversed(installed):
        try:
            signal.signal(signal_number, previous)
        except (OSError, ValueError):
            pass


def _load_dependencies() -> None:
    """Resolve dependencies lazily for the parallel-build/test seam."""

    global capture_target, plan_digest, target_unchanged, validate_plan
    global build_command, preflight, enable_no_hydration, run_command
    if any(value is None for value in (capture_target, plan_digest, target_unchanged, validate_plan)):
        from cleanup_contract import capture_target as contract_capture_target  # type: ignore
        from cleanup_contract import plan_digest as contract_plan_digest  # type: ignore
        from cleanup_contract import target_unchanged as contract_target_unchanged  # type: ignore
        from cleanup_contract import validate_plan as contract_validate_plan  # type: ignore

        capture_target = capture_target or contract_capture_target
        plan_digest = plan_digest or contract_plan_digest
        target_unchanged = target_unchanged or contract_target_unchanged
        validate_plan = validate_plan or contract_validate_plan
    if build_command is None or preflight is None:
        from cache_adapters import build_command as adapter_build_command  # type: ignore
        from cache_adapters import preflight as adapter_preflight  # type: ignore

        build_command = build_command or adapter_build_command
        preflight = preflight or adapter_preflight
    if enable_no_hydration is None or run_command is None:
        from audit_runtime import enable_no_hydration as runtime_enable_no_hydration  # type: ignore
        from audit_runtime import run_command as runtime_run_command  # type: ignore

        enable_no_hydration = enable_no_hydration or runtime_enable_no_hydration
        run_command = run_command or runtime_run_command


def _blocked(reason: str, digest: str | None = None, **fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "BLOCKED", "reason": reason}
    if digest is not None:
        result["digest"] = digest
    result.update(fields)
    return result


def _terminal(
    status: str,
    digest: str,
    reason: str,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    available_delta: int | None = None,
    capacity_drift: bool = False,
    version_before: str | None = None,
    version_after: str | None = None,
    command_outcome: str | None = None,
    output_retained: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "reason": reason,
        "digest": digest,
        "before": before,
        "after": after,
        "observed_available_delta_bytes": available_delta,
        "concurrent_capacity_drift": capacity_drift,
        "version_before": version_before,
        "version_after": version_after,
        "private_output_retained": output_retained,
    }
    if command_outcome is not None:
        result["command_outcome"] = command_outcome
    return result


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _safe_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and os.path.isabs(value)
        and "\x00" not in value
        and _CONTROL_RE.search(value) is None
        and os.path.normpath(value) == value
        and not value.startswith("//")
    )


def _execution_time(now: float | None, started_monotonic: float) -> float:
    if now is None:
        return time.time()
    elapsed = max(0.0, time.monotonic() - started_monotonic)
    return float(now) + elapsed


def _recheck_lifetime(
    plan: Mapping[str, Any], now: float | None, started_monotonic: float
) -> None:
    current = _execution_time(now, started_monotonic)
    created = plan.get("created_at")
    expires = plan.get("expires_at")
    if not _finite_number(created) or not _finite_number(expires):
        raise _Blocked("plan lifetime is invalid before dispatch")
    if float(created) > current:
        raise _Blocked("plan is from the future before dispatch")
    if float(expires) <= current:
        raise _Blocked("plan expired before dispatch")


def _validate_memory_plan(
    plan: object, confirmed_digest: object, now: float | None
) -> tuple[dict[str, Any], str]:
    if not isinstance(confirmed_digest, str) or _DIGEST_RE.fullmatch(confirmed_digest) is None:
        raise _Blocked("confirmation digest format is invalid")
    if now is not None and not _finite_number(now):
        raise _Blocked("execution time is invalid")

    _load_dependencies()
    try:
        validated = validate_plan(plan, now=now)  # type: ignore[misc]
    except Exception:
        raise _Blocked("plan validation failed") from None
    if not isinstance(validated, dict):
        raise _Blocked("plan validation failed")
    if frozenset(validated) != _PLAN_KEYS:
        raise _Blocked("plan contains unsupported fields")

    adapter = validated.get("adapter")
    action = validated.get("action")
    if (adapter, action) not in _SUPPORTED:
        raise _Blocked("action is not supported by the executor")

    current = time.time() if now is None else float(now)
    created = validated.get("created_at")
    expires = validated.get("expires_at")
    if not _finite_number(created) or not _finite_number(expires):
        raise _Blocked("plan lifetime is invalid")
    if float(expires) <= float(created) or float(expires) - float(created) > 900:
        raise _Blocked("plan lifetime is invalid")
    if float(created) > current:
        raise _Blocked("plan is from the future")
    if float(expires) <= current:
        raise _Blocked("plan has expired")

    for key in ("target", "home", "executable"):
        if not _safe_path(validated.get(key)):
            raise _Blocked("plan path is invalid")
    target = validated["target"]
    home = validated["home"]
    if target == os.sep or target == home:
        raise _Blocked("target is a protected path")

    try:
        computed = plan_digest(validated)  # type: ignore[misc]
    except Exception:
        raise _Blocked("plan digest could not be computed") from None
    if not isinstance(computed, str) or _DIGEST_RE.fullmatch(computed) is None:
        raise _Blocked("plan digest is invalid")
    if not hmac.compare_digest(computed, confirmed_digest):
        raise _Blocked("confirmation digest does not match the plan")
    return validated, computed


def _system_alias_path(path: str) -> str:
    """Canonicalize only Apple's known /var and /tmp aliases."""

    if sys.platform != "darwin":
        return path
    for alias, physical in (("/var", "/private/var"), ("/tmp", "/private/tmp")):
        if path == alias or path.startswith(alias + os.sep):
            try:
                if os.path.islink(alias) and os.path.realpath(alias) == physical:
                    return physical + path[len(alias) :]
            except OSError:
                raise _Blocked("private journal ancestry is unavailable") from None
    return path


def _allowed_sticky_temp(path: str, mode: int, owner: int) -> bool:
    if owner != 0 or not (mode & stat.S_ISVTX):
        return False
    canonical = _system_alias_path(path)
    return canonical == "/private/tmp" or canonical.startswith("/private/tmp" + os.sep)


def _check_directory(path: str, *, create_missing: bool) -> str:
    if not _safe_path(path) or path == os.sep:
        raise _Blocked("private journal path is invalid")
    path = _system_alias_path(path)
    parts = Path(path).parts
    current = parts[0]
    for component in parts[1:]:
        current = os.path.join(current, component)
        try:
            value = os.lstat(current)
        except FileNotFoundError:
            if not create_missing:
                raise _Blocked("private journal ancestry is unavailable") from None
            try:
                os.mkdir(current, 0o700)
            except FileExistsError:
                pass
            except OSError:
                raise _Blocked("private journal ancestry is unavailable") from None
            try:
                value = os.lstat(current)
            except OSError:
                raise _Blocked("private journal ancestry is unavailable") from None
        except OSError:
            raise _Blocked("private journal ancestry is unavailable") from None

        if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
            raise _Blocked("private journal ancestry contains a symlink or non-directory")
        if not (stat.S_IMODE(value.st_mode) & 0o111):
            raise _Blocked("private journal ancestry is not searchable")
        mode = stat.S_IMODE(value.st_mode)
        if current == path:
            if value.st_uid != os.geteuid() or mode != 0o700:
                raise _Blocked("private journal directory is not private")
        else:
            if value.st_uid not in {0, os.geteuid()}:
                raise _Blocked("private journal ancestry has an untrusted owner")
            if mode & 0o022 and not _allowed_sticky_temp(current, mode, value.st_uid):
                raise _Blocked("private journal ancestry is writable by another user")
    return path


def _safe_journal_file(fd: int) -> bool:
    try:
        value = os.fstat(fd)
    except OSError:
        return False
    return (
        stat.S_ISREG(value.st_mode)
        and value.st_uid == os.geteuid()
        and value.st_nlink == 1
        and stat.S_IMODE(value.st_mode) == 0o600
    )


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except OSError:
            raise _Blocked("private journal could not be written") from None
        if written <= 0:
            raise _Blocked("private journal could not be written")
        view = view[written:]


def _journal_record(
    digest: str,
    stage: str,
    status: str,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    available_delta: int | None = None,
    capacity_drift: bool = False,
    version_before: str | None = None,
    version_after: str | None = None,
    command_outcome: str | None = None,
    output_retained: bool = False,
) -> bytes:
    record: dict[str, Any] = {
        "schema_version": 1,
        "digest": digest,
        "stage": stage,
        "status": status,
        "before": before,
        "after": after,
        "observed_available_delta_bytes": available_delta,
        "concurrent_capacity_drift": capacity_drift,
        "version_before": version_before,
        "version_after": version_after,
        "private_output_retained": output_retained,
    }
    if command_outcome is not None:
        record["command_outcome"] = command_outcome
    try:
        return (
            json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode()
    except (TypeError, ValueError, OverflowError):
        raise _Blocked("private journal record is invalid") from None


def _unlink_our_journal_leaf(path: str, fd: int | None) -> None:
    if fd is None:
        return
    try:
        file_value = os.fstat(fd)
        path_value = os.lstat(path)
        if (
            stat.S_ISREG(path_value.st_mode)
            and file_value.st_dev == path_value.st_dev
            and file_value.st_ino == path_value.st_ino
        ):
            os.unlink(path)
    except OSError:
        pass


def _reserve_journal(journal_dir: str, digest: str) -> tuple[int, str]:
    if not isinstance(journal_dir, str) or not _safe_path(journal_dir):
        raise _Blocked("private journal path is invalid")
    directory = _check_directory(journal_dir, create_missing=True)
    digest_leaf = os.path.join(directory, digest + ".jsonl")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise _Blocked("private journal safe creation is unavailable")
    fd: int | None = None
    try:
        fd = os.open(os.fspath(digest_leaf), os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
        if not _safe_journal_file(fd):
            raise _Blocked("private journal file is unsafe")
        _write_all(fd, _journal_record(digest, "started", "PARTIAL"))
        os.fsync(fd)
        return fd, digest_leaf
    except FileExistsError:
        raise _Blocked("plan digest has already been attempted; reconcile its journal") from None
    except _Blocked:
        if fd is not None:
            _unlink_our_journal_leaf(digest_leaf, fd)
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    except OSError:
        if fd is not None:
            _unlink_our_journal_leaf(digest_leaf, fd)
            try:
                os.close(fd)
            except OSError:
                pass
        raise _Blocked("private journal could not be reserved") from None
    except BaseException:
        if fd is not None:
            _unlink_our_journal_leaf(digest_leaf, fd)
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _append_journal(fd: int, data: bytes) -> bool:
    if not _safe_journal_file(fd):
        return False
    try:
        os.lseek(fd, 0, os.SEEK_END)
        _write_all(fd, data)
        os.fsync(fd)
        return True
    except (_Blocked, OSError):
        return False


def _capacity_sample(target: str) -> dict[str, int]:
    try:
        target_stat = os.stat(target, follow_symlinks=False)
        if not (stat.S_ISREG(target_stat.st_mode) or stat.S_ISDIR(target_stat.st_mode)):
            raise OSError
        volume = os.statvfs(target)
        block_size = int(getattr(volume, "f_frsize", 0) or getattr(volume, "f_bsize", 0))
        available = int(volume.f_bavail) * block_size
        total = int(volume.f_blocks) * block_size
        if block_size <= 0 or available < 0 or total < 0:
            raise OSError
        return {
            "device": int(target_stat.st_dev),
            "available_bytes": available,
            "total_bytes": total,
        }
    except (AttributeError, OSError, TypeError, ValueError, OverflowError):
        raise _Blocked("target capacity metadata is unavailable") from None


def _material_capacity_drift(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    if first.get("device") != second.get("device"):
        return True
    available_first = first.get("available_bytes")
    available_second = second.get("available_bytes")
    if not isinstance(available_first, int) or not isinstance(available_second, int):
        return True
    delta = abs(available_second - available_first)
    return delta >= _DRIFT_BYTES or delta >= int(abs(available_first) * 0.02)


def _capacity_pair(target: str) -> tuple[dict[str, int], dict[str, int], bool]:
    first = _capacity_sample(target)
    time.sleep(_CAPACITY_PAUSE)
    second = _capacity_sample(target)
    return first, second, _material_capacity_drift(first, second)


def _target_snapshot(plan: Mapping[str, Any]) -> dict[str, Any]:
    try:
        value = capture_target(plan["target"], home=plan["home"])  # type: ignore[misc]
    except Exception:
        raise _Blocked("target metadata could not be captured") from None
    if not isinstance(value, dict):
        raise _Blocked("target metadata could not be captured")
    return value


def _target_state_matches_bound(plan: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    expected = plan.get("target_state")
    if not isinstance(expected, Mapping) or not expected:
        return True
    expected_fingerprint = expected.get("fingerprint")
    current_fingerprint = current.get("fingerprint")
    if isinstance(expected_fingerprint, str) and isinstance(current_fingerprint, str):
        if not hmac.compare_digest(expected_fingerprint, current_fingerprint):
            return False
        expected_root = _root_identity(expected)
        current_root = _root_identity(current)
        return expected_root is None or current_root is None or expected_root == current_root
    return dict(expected) == dict(current)


def _root_identity(value: Mapping[str, Any]) -> object:
    for key in ("root", "root_identity", "filesystem_root"):
        item = value.get(key)
        if isinstance(item, Mapping):
            for dev_key, ino_key in (("dev", "ino"), ("st_dev", "st_ino")):
                if dev_key in item and ino_key in item:
                    return (item[dev_key], item[ino_key])
        elif item is not None:
            return item
    if "root_dev" in value or "root_ino" in value:
        return (value.get("root_dev"), value.get("root_ino"))
    return None


def _snapshot_effect_signature(value: Mapping[str, Any]) -> tuple[object, ...]:
    # Ancestor/home/filesystem-root metadata is a safety observation, not an
    # owner-action effect. The contract's fingerprint and target-root identity
    # are the authoritative target changes.
    return (
        value.get("fingerprint"),
        _root_identity(value),
        value.get("entry_count"),
        value.get("allocated_bytes"),
        value.get("logical_bytes"),
    )


def _snapshot_changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    return _snapshot_effect_signature(before) != _snapshot_effect_signature(after)


def _measurement(
    snapshot: Mapping[str, Any] | None, capacity: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    if snapshot is None and capacity is None:
        return None
    result: dict[str, Any] = {
        "allocated_bytes": None,
        "logical_bytes": None,
        "available_bytes": None,
    }
    if isinstance(snapshot, Mapping):
        for source in ("allocated_bytes", "logical_bytes"):
            value = snapshot.get(source)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[source] = value
    if isinstance(capacity, Mapping):
        value = capacity.get("available_bytes")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result["available_bytes"] = value
    return result


def _version(value: object) -> str | None:
    if not isinstance(value, str) or not value or _CONTROL_RE.search(value):
        return None
    # Version evidence is useful in the private journal, but arbitrary owner
    # output is not. Keep only the release-shaped strings used by the frozen
    # uv/go adapter contract.
    if re.fullmatch(r"(?:uv\s+)?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?(?:\s.*)?", value):
        return value[:128]
    if re.fullmatch(r"go version go\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?\s+.+", value):
        return value[:128]
    return None


def _preflight_ok(plan: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    try:
        result = preflight(plan)  # type: ignore[misc]
    except Exception:
        raise _Blocked("fresh owner preflight failed") from None
    if not isinstance(result, Mapping) or result.get("ok") is not True:
        raise _Blocked("fresh owner preflight is not safe")
    evidence = result.get("evidence")
    if evidence is not None and not isinstance(evidence, Mapping):
        raise _Blocked("fresh owner evidence is malformed")
    combined: Mapping[str, Any] = evidence if isinstance(evidence, Mapping) else result
    activity = combined.get("owner_activity")
    if activity is not None and activity != "idle":
        raise _Blocked("owner activity is not idle")
    if result.get("owner_activity") is not None and result.get("owner_activity") != "idle":
        raise _Blocked("owner activity is not idle")
    for evidence_value in (result, combined):
        for key in (
            "supported",
            "fresh",
            "tool_identity",
            "executable_identity",
            "config_unchanged",
            "version_match",
        ):
            if key in evidence_value and evidence_value.get(key) is False:
                raise _Blocked("fresh owner evidence is incomplete")
    for key, expected in (
        ("target", plan.get("target")),
        ("executable", plan.get("executable")),
        ("version", plan.get("version")),
    ):
        if key in combined and combined.get(key) != expected:
            raise _Blocked("fresh owner configuration changed")
    version = _version(combined.get("version"))
    return dict(result), version


def _executable_identity(path: str) -> dict[str, Any]:
    try:
        resolved = os.path.realpath(path)
        value = os.stat(path, follow_symlinks=True)
    except OSError:
        raise _Blocked("owner executable identity is unavailable") from None
    if not os.path.isabs(resolved) or not stat.S_ISREG(value.st_mode) or not (value.st_mode & 0o111):
        raise _Blocked("owner executable identity is unsafe")
    return {
        "resolved": resolved,
        "dev": int(value.st_dev),
        "ino": int(value.st_ino),
        "mode": int(value.st_mode),
        "nlink": int(value.st_nlink),
        "uid": int(value.st_uid),
        "gid": int(value.st_gid),
        "size": int(value.st_size),
        "blocks": int(getattr(value, "st_blocks", 0)),
        "blksize": int(getattr(value, "st_blksize", 0)),
        "rdev": int(value.st_rdev),
        "mtime_ns": int(value.st_mtime_ns),
        "ctime_ns": int(value.st_ctime_ns),
        "birthtime_ns": int(getattr(value, "st_birthtime_ns", 0)),
        "flags": int(getattr(value, "st_flags", 0)),
    }


def _bound_executable_matches(plan: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    expected_state = plan.get("executable_state")
    if not isinstance(expected_state, Mapping) or not expected_state:
        return True
    expected = expected_state.get("identity", expected_state)
    if not isinstance(expected, Mapping):
        return False
    aliases = {
        "st_dev": "dev",
        "st_ino": "ino",
        "st_mode": "mode",
        "st_nlink": "nlink",
        "st_uid": "uid",
        "st_size": "size",
        "st_mtime_ns": "mtime_ns",
    }
    recognized = False
    for key, value in expected.items():
        normalized = aliases.get(key, key)
        if normalized in current:
            recognized = True
            if current[normalized] != value:
                return False
    return recognized


def _safe_command(
    plan: Mapping[str, Any], command: object, environment: object
) -> tuple[list[str], dict[str, str]]:
    if not isinstance(command, (list, tuple)) or not command:
        raise _Blocked("trusted owner command is malformed")
    argv = list(command)
    if any(not isinstance(item, str) or not item or "\x00" in item for item in argv):
        raise _Blocked("trusted owner command is malformed")
    if not os.path.isabs(argv[0]) or os.path.abspath(argv[0]) != os.path.abspath(plan["executable"]):
        raise _Blocked("trusted owner executable changed")
    expected: list[str]
    if plan.get("adapter") == "go" and plan.get("action") == "go-build-cache-clean":
        expected = [plan["executable"], "clean", "-cache"]
    elif plan.get("adapter") == "uv" and plan.get("action") == "uv-cache-prune":
        expected = [plan["executable"], "--cache-dir", plan["target"], "cache", "prune"]
    else:
        raise _Blocked("action is not supported by the executor")
    if argv != expected:
        raise _Blocked("trusted owner command is outside the action allowlist")
    for item in argv:
        if item == "--force" or os.path.basename(item) in _DANGEROUS_COMMAND_WORDS:
            raise _Blocked("trusted owner command is outside the action allowlist")
    if not isinstance(environment, dict) or any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or "\x00" in key
        or "\x00" in value
        for key, value in environment.items()
    ):
        raise _Blocked("trusted owner environment is malformed")
    return argv, dict(environment)


def _command_value(result: object, key: str) -> object:
    if isinstance(result, Mapping):
        return result.get(key)
    return getattr(result, key, None)


def _command_class(result: object) -> tuple[str | None, int | None]:
    outcome = _command_value(result, "outcome")
    returncode = _command_value(result, "returncode")
    allowed_outcomes = {
        "finished",
        "timeout",
        "cancelled",
        "spawn-error",
        "guard-unavailable",
        "output-limit",
    }
    if not isinstance(outcome, str) or outcome not in allowed_outcomes:
        outcome = None
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        returncode = None
    return outcome, returncode


def _cleanup_output(path: Path) -> bool:
    try:
        os.unlink(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def execute(
    plan: dict,
    *,
    confirmed_digest: str,
    journal_dir: str,
    now: float | None = None,
) -> dict:
    """Execute one freshly preflighted, exact-digest owner action."""

    # This is deliberately the first operation. In particular, do not inspect
    # a target, journal path, or executable before this guard.
    cancel_handlers = _install_cancel_handlers()
    try:
        _load_dependencies()
        if not callable(enable_no_hydration):
            _restore_cancel_handlers(cancel_handlers)
            return _blocked("no-hydration guard is unavailable")
        enable_no_hydration()  # type: ignore[misc]
    except KeyboardInterrupt:
        _restore_cancel_handlers(cancel_handlers)
        return _blocked("execution was cancelled before dispatch")
    except BaseException:
        _restore_cancel_handlers(cancel_handlers)
        return _blocked("no-hydration guard is unavailable")

    digest: str | None = None
    try:
        started_monotonic = time.monotonic()
    except BaseException:
        _restore_cancel_handlers(cancel_handlers)
        return _blocked("execution was cancelled before dispatch")
    journal_fd: int | None = None
    journal_leaf: str | None = None
    mutation_started = False
    before_snapshot: dict[str, Any] | None = None
    before_capacity: dict[str, int] | None = None
    capacity_drift = False
    version_before: str | None = None
    output_retained = False
    try:
        validated, digest = _validate_memory_plan(
            plan, confirmed_digest, _execution_time(now, started_monotonic)
        )
        # A journal inside the target could be removed by the owner command.
        try:
            target_path = _system_alias_path(validated["target"])
            journal_path = _system_alias_path(os.path.abspath(journal_dir))
            if os.path.commonpath((target_path, journal_path)) == target_path:
                raise _Blocked("private journal must not be inside the cleanup target")
        except _Blocked:
            raise
        except (TypeError, ValueError, OSError):
            raise _Blocked("private journal path is invalid") from None

        journal_fd, journal_leaf = _reserve_journal(journal_dir, digest)

        _preflight, version_before = _preflight_ok(validated)
        if not callable(target_unchanged):
            raise _Blocked("target identity check is unavailable")
        try:
            if not bool(target_unchanged(validated)):  # type: ignore[misc]
                raise _Blocked("target identity changed")
        except _Blocked:
            raise
        except Exception:
            raise _Blocked("target identity could not be checked") from None

        before_snapshot = _target_snapshot(validated)
        if not _target_state_matches_bound(validated, before_snapshot):
            raise _Blocked("target identity changed")

        _before_capacity_a, before_capacity_b, drifted = _capacity_pair(validated["target"])
        capacity_drift = drifted
        if drifted:
            # One fresh preflight and one fresh pair are allowed. Persistent
            # material drift means this approval is no longer current.
            _preflight_ok(validated)
            if not bool(target_unchanged(validated)):  # type: ignore[misc]
                raise _Blocked("target identity changed during capacity check")
            _before_capacity_a, before_capacity_b, drifted = _capacity_pair(validated["target"])
            if drifted:
                raise _Blocked("capacity is changing; fresh approval is required")
        before_capacity = before_capacity_b

        # Recheck both target and owner executable after capacity sampling and
        # immediately before handing control to the trusted adapter command.
        if not bool(target_unchanged(validated)):  # type: ignore[misc]
            raise _Blocked("target identity changed before dispatch")
        before_snapshot = _target_snapshot(validated)
        if not _target_state_matches_bound(validated, before_snapshot):
            raise _Blocked("target identity changed before dispatch")
        executable_before = _executable_identity(validated["executable"])
        if (
            _system_alias_path(executable_before["resolved"])
            != _system_alias_path(validated["executable"])
            or not _bound_executable_matches(validated, executable_before)
        ):
            raise _Blocked("owner executable identity changed")

        try:
            built = build_command(validated)  # type: ignore[misc]
        except Exception:
            raise _Blocked("trusted owner command could not be built") from None
        if not isinstance(built, tuple) or len(built) != 2:
            raise _Blocked("trusted owner command is malformed")
        argv, environment = _safe_command(validated, built[0], built[1])
        executable_after_build = _executable_identity(validated["executable"])
        if (
            _system_alias_path(executable_after_build["resolved"])
            != _system_alias_path(validated["executable"])
            or executable_after_build != executable_before
            or not _bound_executable_matches(validated, executable_after_build)
        ):
            raise _Blocked("owner executable identity changed before dispatch")
        _check_directory(journal_dir, create_missing=False)
        if not _safe_journal_file(journal_fd):
            raise _Blocked("private journal file is unsafe before dispatch")

        # The first validation may have happened before a long preflight or
        # build. Recheck at the last safe point; an injected test clock advances
        # from its monotonic start rather than freezing the plan forever.
        _recheck_lifetime(validated, now, started_monotonic)

        token = secrets.token_hex(16)
        stdout_path = Path(journal_leaf).parent / (".executor-" + token + ".stdout")  # type: ignore[union-attr]
        stderr_path = Path(journal_leaf).parent / (".executor-" + token + ".stderr")  # type: ignore[union-attr]
        mutation_started = True
        try:
            result = run_command(  # type: ignore[misc]
                argv,
                timeout=_COMMAND_TIMEOUT,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                env=environment,
                cwd=os.sep,
                guarded=True,
            )
        except BaseException:
            result = None
        finally:
            output_retained = True
            try:
                stdout_cleaned = _cleanup_output(stdout_path)
                stderr_cleaned = _cleanup_output(stderr_path)
                output_retained = not stdout_cleaned or not stderr_cleaned
            except BaseException:
                # An interrupt during cleanup must not hide possible retained
                # private output from reconciliation.
                output_retained = True
                raise

        command_outcome, returncode = _command_class(result)
        command_success = command_outcome == "finished" and returncode == 0

        after_snapshot: dict[str, Any] | None = None
        after_capacity: dict[str, int] | None = None
        postcheck_ok = True
        try:
            after_snapshot = _target_snapshot(validated)
            before_root = _root_identity(before_snapshot)
            after_root = _root_identity(after_snapshot)
            if before_root is not None and after_root is not None and before_root != after_root:
                postcheck_ok = False
            if not isinstance(after_snapshot, Mapping):
                postcheck_ok = False
        except _Blocked:
            postcheck_ok = False
        try:
            after_capacity = _capacity_sample(validated["target"])
            if before_capacity is not None and after_capacity.get("device") != before_capacity.get("device"):
                postcheck_ok = False
        except _Blocked:
            postcheck_ok = False

        before_measurement = _measurement(before_snapshot, before_capacity)
        after_measurement = _measurement(after_snapshot, after_capacity)
        available_delta: int | None = None
        if before_capacity is not None and after_capacity is not None:
            available_delta = after_capacity["available_bytes"] - before_capacity["available_bytes"]

        if output_retained:
            status = "PARTIAL"
            reason = "private owner output was retained; reconcile before any retry"
        elif not postcheck_ok:
            status = "PARTIAL"
            reason = "postcheck could not verify the target"
        elif not command_success:
            status = "PARTIAL"
            if command_outcome == "timeout":
                reason = "owner command timed out; reconcile before any retry"
            elif command_outcome == "cancelled":
                reason = "owner command was cancelled; reconcile before any retry"
            elif command_outcome is None:
                reason = "owner command outcome was unknown; reconcile before any retry"
            else:
                reason = "owner command did not complete successfully; reconcile before any retry"
        elif not _snapshot_changed(before_snapshot, after_snapshot):  # type: ignore[arg-type]
            status = "UNCHANGED"
            reason = "owner command completed but target metadata was unchanged"
        else:
            status = "EXECUTED"
            reason = "owner command completed and target metadata changed"

        journal_ok = _append_journal(
            journal_fd,
            _journal_record(
                digest,
                "terminal",
                status,
                before=before_measurement,
                after=after_measurement,
                available_delta=available_delta,
                capacity_drift=capacity_drift,
                version_before=version_before,
                version_after=None,
                command_outcome=command_outcome,
                output_retained=output_retained,
            ),
        )
        if not journal_ok:
            status = "PARTIAL"
            reason = (
                "private owner output was retained and terminal journal could not be persisted; "
                "reconcile before any retry"
                if output_retained
                else "terminal journal could not be persisted; reconcile before any retry"
            )
        return _terminal(
            status,
            digest,
            reason,
            before=before_measurement,
            after=after_measurement,
            available_delta=available_delta,
            capacity_drift=capacity_drift,
            version_before=version_before,
            command_outcome=command_outcome,
            output_retained=output_retained,
        )
    except _Blocked as error:
        if digest is None:
            return _blocked(error.reason)
        if journal_fd is not None:
            journal_status = "PARTIAL" if mutation_started else "BLOCKED"
            _append_journal(
                journal_fd,
                _journal_record(
                    digest,
                    "terminal",
                    journal_status,
                    before=_measurement(before_snapshot, before_capacity),
                    after=None,
                    capacity_drift=capacity_drift,
                    version_before=version_before,
                    output_retained=output_retained,
                ),
            )
        if mutation_started:
            return _terminal(
                "PARTIAL",
                digest,
                "private owner output was retained; reconcile before any retry"
                if output_retained
                else error.reason,
                before=_measurement(before_snapshot, before_capacity),
                capacity_drift=capacity_drift,
                version_before=version_before,
                output_retained=output_retained,
            )
        return _blocked(
            "private owner output was retained; reconcile before retry"
            if output_retained
            else error.reason,
            digest,
            private_output_retained=output_retained,
        )
    except BaseException:
        # No exception text or owner output is part of the public result. At
        # this point a dispatch may have happened, so never call it a clean
        # block.
        if digest is None:
            return _blocked("execution failed before dispatch")
        if journal_fd is not None:
            _append_journal(
                journal_fd,
                _journal_record(
                    digest,
                    "terminal",
                    "PARTIAL" if mutation_started else "BLOCKED",
                    before=_measurement(before_snapshot, before_capacity),
                    after=None,
                    capacity_drift=capacity_drift,
                    version_before=version_before,
                    output_retained=output_retained,
                ),
            )
        return _terminal(
            "PARTIAL" if mutation_started else "BLOCKED",
            digest,
            "private owner output was retained; reconcile before any retry"
            if output_retained
            else ("execution failed after dispatch; reconcile journal" if mutation_started else "execution failed before dispatch"),
            before=_measurement(before_snapshot, before_capacity),
            capacity_drift=capacity_drift,
            version_before=version_before,
            output_retained=output_retained,
        )
    finally:
        if journal_fd is not None:
            try:
                os.close(journal_fd)
            except OSError:
                pass
        _restore_cancel_handlers(cancel_handlers)


__all__ = ["execute"]
