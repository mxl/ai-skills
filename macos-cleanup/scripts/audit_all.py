#!/usr/bin/env python3
"""Safe, structured read-only orchestrator for the macOS cleanup modules.

The orchestrator deliberately treats module output as untrusted input. Child
processes are run through ``audit_runtime`` (which owns the no-hydration
guard), their output is parsed line by line, and only validated/redacted JSON
is retained in the run directory.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import secrets
import signal
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

# The audit entrypoint must not leave import bytecode in the skill directory.
sys.dont_write_bytecode = True
from audit_runtime import GuardUnavailable, enable_no_hydration, run_command


SCRIPT_DIR = Path(__file__).resolve().parent
KNOWN_MODULES = ("capabilities", "paths", "caches", "cloud")
DEFAULT_MODULES = KNOWN_MODULES
COVERAGE_CATEGORIES = (
    "apps",
    "packages",
    "models",
    "docker",
    "vms",
    "databases",
    "projects",
    "personal",
)

KNOWN_RECORD_TYPES = {
    "capability",
    "capacity",
    "path",
    "cleanup_candidate",
    "cloud_root",
    "cloud_container_inventory",
    "snapshot",
    "module_status",
    "orchestrator_module",
    "audit_summary",
}
RESERVED_CHILD_RECORD_TYPES = {"orchestrator_module", "audit_summary"}
KNOWN_STATUSES = {
    "COMPLETE",
    "PARTIAL",
    "FAILED",
    "UNAVAILABLE",
    "AVAILABLE",
    "PERMISSION-DENIED",
    "MEASURED",
    "DISCOVERED",
    "NOT-FOUND",
    "NOT-IMPLEMENTED",
    "NOT-RUN",
    "UNSUPPORTED",
    "REVIEW-ONLY",
    "APPROVAL-REQUIRED",
    "AMBIGUOUS",
    "PROTECTED",
    "IN-USE",
    "EXCLUDED",
    "SAFE-GARBAGE",
    "STALE-ARTIFACT",
    "APPROVAL_REQUIRED",
}
NUMERIC_FIELDS = {
    "total_bytes",
    "used_bytes",
    "available_bytes",
    "allocated_bytes",
    "logical_bytes",
    "container_count",
    "exit_code",
}
BOOLEAN_FIELDS = {"sensitive", "actionable", "timed_out", "final_status_seen"}
MODULE_STATUS_STATUSES = {"COMPLETE", "PARTIAL", "FAILED", "UNAVAILABLE", "PERMISSION-DENIED"}
STRING_OR_NULL_FIELDS = {
    "path",
    "owner",
    "tool",
    "volume_device",
    "classification",
    "proposed_action",
    "review_action",
    "owner_activity",
    "scope_state",
    "verification_state",
    "recovery",
    "risk",
    "confidence",
    "provider_kind",
    "domain_label",
    "sync_state",
    "local_state",
    "size_relation",
    "eviction_method",
    "snapshot_name",
    "purgeable",
    "child_module",
    "observed_status",
    "outcome",
    "output_path",
    "error_path",
    "inventory_path",
    "report_path",
}
DANGEROUS_COMMAND_KEYS = {
    "command",
    "commands",
    "cmd",
    "argv",
    "args",
    "shell",
    "exec",
    "executable",
    "script",
    "scripts",
    "command_line",
    "delete_command",
    "cleanup_command",
    "action_command",
    "action_argv",
}
EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
HOME_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:Users|home)/[^/\s]+")
SECRET_ASSIGNMENT_RE = re.compile(r"(?i)\b(token|secret|password|api[_-]?key|credential)\b\s*[:=]\s*[^\s,;]+")
BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_STRING_LENGTH = 4096
MAX_TIMEOUT = 3600.0
MIN_TIMEOUT = 0.01
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
GUARD_EXIT_CODE = 78
CANCEL_EXIT_CODE = 130
INITIAL_GUARD_DIAGNOSTIC = "audit_all: no-hydration guard unavailable"
SUCCESS_OUTCOME = "finished"
GUARD_LOSS_OUTCOME = "guard-unavailable"
CANCEL_OUTCOME = "cancelled"
KNOWN_RUNTIME_OUTCOMES = {
    SUCCESS_OUTCOME,
    "timeout",
    CANCEL_OUTCOME,
    GUARD_LOSS_OUTCOME,
    "spawn-error",
    "output-limit",
    "running",
}


class ConfigurationError(ValueError):
    """A user/configuration error that must happen before a run is created."""


class OutputPathError(RuntimeError):
    """The requested run path is not a fresh, safe directory."""


@dataclass
class RunConfig:
    target: str
    output: Path | None
    timeout: float
    requested_modules: tuple[str, ...]
    module_dir: Path
    home: str


@dataclass
class ParsedModule:
    records: list[dict[str, Any]] = field(default_factory=list)
    malformed_line_count: int = 0
    invalid_record_count: int = 0
    unknown_record_count: int = 0
    observed_status: str | None = None
    final_status_seen: bool = False


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _ensure_finite_json(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    if isinstance(value, dict):
        for nested in value.values():
            _ensure_finite_json(nested)
    elif isinstance(value, list):
        for nested in value:
            _ensure_finite_json(nested)


def _parse_json_line(line: str) -> Any:
    value = json.loads(
        line,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonfinite,
    )
    _ensure_finite_json(value)
    return value


def _is_nonnegative_number(value: Any) -> bool:
    return value is None or (
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
    )


def _validate_record(record: Any, expected_module: str) -> str:
    """Return ``valid``, ``unknown``, or ``invalid`` without trusting nesting."""

    if not isinstance(record, dict):
        return "invalid"
    if record.get("schema_version") != "1":
        return "invalid"
    record_type = record.get("record_type")
    if not isinstance(record_type, str):
        return "invalid"
    if record_type not in KNOWN_RECORD_TYPES:
        return "unknown"
    if record_type in RESERVED_CHILD_RECORD_TYPES:
        return "invalid"
    if record.get("module") != expected_module:
        return "invalid"
    if not isinstance(record.get("status"), str) or record["status"] not in KNOWN_STATUSES:
        return "invalid"
    if record_type == "module_status" and record["status"] not in MODULE_STATUS_STATUSES:
        return "invalid"

    for key in NUMERIC_FIELDS:
        if key in record and not _is_nonnegative_number(record[key]):
            return "invalid"
    for key, value in record.items():
        if key.endswith("_bytes") or key.endswith("_count"):
            if not _is_nonnegative_number(value):
                return "invalid"
        if key in BOOLEAN_FIELDS and not isinstance(value, bool):
            return "invalid"

    for key in STRING_OR_NULL_FIELDS:
        if key in record and record[key] is not None and not isinstance(record[key], str):
            return "invalid"
    if "evidence" in record and record["evidence"] is not None and not isinstance(record["evidence"], (str, list)):
        return "invalid"
    if "sensitive" in record and not isinstance(record["sensitive"], bool):
        return "invalid"

    path_bearing = record_type in {
        "capability",
        "capacity",
        "path",
        "cleanup_candidate",
        "cloud_root",
        "cloud_container_inventory",
    }
    if path_bearing and "sensitive" not in record:
        return "invalid"

    required: dict[str, tuple[str, ...]] = {
        "capability": ("tool",),
        "capacity": ("path", "total_bytes", "used_bytes", "available_bytes"),
        "path": ("path", "allocated_bytes", "logical_bytes"),
        "cleanup_candidate": (
            "path",
            "owner",
            "allocated_bytes",
            "logical_bytes",
            "classification",
            "proposed_action",
            "recovery",
            "risk",
            "confidence",
            "actionable",
        ),
        "cloud_root": (
            "path",
            "owner",
            "provider_kind",
            "domain_label",
            "allocated_bytes",
            "logical_bytes",
            "sync_state",
            "local_state",
            "size_relation",
            "eviction_method",
        ),
        "cloud_container_inventory": ("container_count",),
        "snapshot": ("snapshot_name", "purgeable"),
    }
    for key in required.get(record_type, ()):
        if key not in record:
            return "invalid"

    if record_type == "cleanup_candidate":
        if not isinstance(record.get("actionable"), bool):
            return "invalid"
        for key in ("owner", "recovery", "risk", "confidence"):
            if not isinstance(record.get(key), str):
                return "invalid"
        if record.get("proposed_action") is not None and not isinstance(record.get("proposed_action"), str):
            return "invalid"
    if record_type == "cloud_root":
        for key in (
            "provider_kind",
            "domain_label",
            "sync_state",
            "local_state",
            "size_relation",
            "eviction_method",
        ):
            if not isinstance(record.get(key), str):
                return "invalid"
    return "valid"


def _redact_string(value: str, home: str, key: str = "") -> str:
    value = value[:MAX_STRING_LENGTH]
    if home:
        value = re.sub(rf"{re.escape(home)}(?=$|/)", "<home>", value)
    value = HOME_PATH_RE.sub("<home>", value)
    value = EMAIL_RE.sub("<redacted-account>", value)
    value = BEARER_RE.sub("Bearer <redacted>", value)
    value = SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", value)
    if any(token in key.lower() for token in ("token", "secret", "password", "credential", "api_key")):
        value = "<redacted>"
    return CONTROL_RE.sub("", value)


def _sanitize_value(value: Any, home: str, key: str = "") -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                continue
            if raw_key.lower() in DANGEROUS_COMMAND_KEYS:
                continue
            cleaned[raw_key] = _sanitize_value(raw_value, home, raw_key)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_value(item, home, key) for item in value]
    if isinstance(value, str):
        return _redact_string(value, home, key)
    return value


def _sanitize_record(record: dict[str, Any], home: str) -> dict[str, Any]:
    cleaned = _sanitize_value(record, home)
    if record.get("record_type") == "cleanup_candidate":
        # No current cleanup engine verifies candidates. A child cannot
        # authorize a destructive action by setting this field to true.
        cleaned["actionable"] = False
        cleaned["classification"] = "REVIEW-ONLY"
        cleaned["actionability_reason"] = "measured candidate requires dedicated review and verification"
    elif record.get("record_type") == "cloud_root":
        # Cloud roots are never item-level eviction approvals.
        cleaned["actionable"] = False
    return cleaned


def _decode_output(path: Path) -> tuple[str, bool]:
    try:
        raw = path.read_bytes()
    except OSError:
        return "", True
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), True


def _parse_module_output(
    data: str, module: str, home: str, *, invalid_utf8: bool = False
) -> ParsedModule:
    parsed = ParsedModule()
    last_nonempty_was_footer = False
    saw_nonempty_line = False
    for raw_line in data.split("\n"):
        if not raw_line.strip():
            continue
        saw_nonempty_line = True
        last_nonempty_was_footer = False
        if "\ufffd" in raw_line:
            parsed.malformed_line_count += 1
            continue
        try:
            value = _parse_json_line(raw_line)
            classification = _validate_record(value, module)
            if classification == "unknown":
                parsed.unknown_record_count += 1
                continue
            if classification != "valid":
                parsed.invalid_record_count += 1
                continue
            sanitized = _sanitize_record(value, home)
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
            parsed.malformed_line_count += 1
            continue
        parsed.records.append(sanitized)
        if sanitized.get("record_type") == "module_status":
            parsed.observed_status = sanitized.get("status")
            last_nonempty_was_footer = True
    if invalid_utf8 and not saw_nonempty_line:
        parsed.malformed_line_count += 1
    parsed.final_status_seen = bool(parsed.records) and last_nonempty_was_footer
    return parsed


def _allowed_system_alias(path: str) -> bool:
    # macOS commonly exposes /var as a symlink to /private/var. This is the
    # only symlinked ancestry accepted; user-created aliases remain rejected.
    return path == "/var" and os.path.realpath(path) == "/private/var"


def _check_ancestry(path: Path) -> None:
    """Reject symlinked output ancestry without resolving user paths."""

    current = os.path.abspath(os.fspath(path))
    missing: list[str] = []
    while not os.path.lexists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        missing.append(current)
        current = parent
    while True:
        if os.path.islink(current) and not _allowed_system_alias(current):
            raise OutputPathError("output path ancestry contains a symlink")
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for missing_path in missing:
        if os.path.lexists(missing_path) and os.path.islink(missing_path):
            raise OutputPathError("output path ancestry contains a symlink")


def _absolute_user_path(value: str) -> str:
    expanded = os.path.expanduser(value)
    if not os.path.isabs(expanded):
        expanded = os.path.join(os.getcwd(), expanded)
    return os.path.normpath(expanded)


def _safe_create_directory(path: Path, *, leaf_exclusive: bool) -> None:
    path_string = os.path.abspath(os.fspath(path))
    _check_ancestry(path)
    if leaf_exclusive and os.path.lexists(path_string):
        raise OutputPathError("output directory already exists")
    missing: list[str] = []
    current = path_string
    while not os.path.lexists(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        is_leaf = directory == path_string
        created = False
        try:
            os.mkdir(directory, 0o700)
            created = True
        except FileExistsError:
            # The explicit final run directory is exclusive. Never accept,
            # chmod, or write into a directory created by a racing caller.
            if is_leaf and leaf_exclusive:
                raise OutputPathError("output directory creation raced")
        if os.path.islink(directory) or not os.path.isdir(directory):
            raise OutputPathError("output path ancestry changed")
        if is_leaf and created:
            os.chmod(directory, 0o700)


def _create_fresh_output(explicit: Path | None) -> Path:
    if explicit is not None:
        output = Path(os.path.abspath(os.fspath(explicit)))
        _check_ancestry(output)
        if os.path.lexists(output):
            raise OutputPathError("output directory already exists")
        _safe_create_directory(output, leaf_exclusive=True)
        return output

    date_part = datetime.now().strftime("%Y-%m-%d")
    parent = Path.cwd() / "macos-cleanup" / date_part
    _check_ancestry(parent)
    _safe_create_directory(parent, leaf_exclusive=False)
    for _ in range(20):
        suffix = secrets.token_hex(4)
        candidate = parent / f"run-{datetime.now().strftime('%H%M%S')}-{suffix}"
        try:
            os.mkdir(candidate, 0o700)
        except FileExistsError:
            continue
        if os.path.islink(candidate):
            raise OutputPathError("output path changed to a symlink")
        os.chmod(candidate, 0o700)
        return candidate
    raise OutputPathError("could not allocate a unique output directory")


def _atomic_write(path: Path, content: str) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _private_output_file(output: Path, module: str, kind: str) -> Path:
    fd, name = tempfile.mkstemp(prefix=f".{module}.{kind}.", dir=output)
    os.close(fd)
    path = Path(name)
    os.chmod(path, 0o600)
    return path


def _module_argv(module: str, module_dir: Path, target: str, home: str) -> list[str]:
    script = module_dir / f"audit-{module}.sh"
    if module == "capabilities":
        return [os.fspath(script), target]
    if module == "paths":
        # These are deliberately the only default filesystem paths. The
        # target is used for capacity, not as a recursive HOME scan.
        return [
            os.fspath(script),
            "--exact",
            "/Applications",
            os.path.join(home, "Library", "Caches"),
        ]
    return [os.fspath(script)]


def _validate_config(namespace: argparse.Namespace) -> RunConfig:
    home = os.path.abspath(os.environ.get("HOME") or str(Path.home()))
    target_raw = namespace.target
    if not isinstance(target_raw, str) or not target_raw or "\x00" in target_raw:
        raise ConfigurationError("target must be a non-empty path")
    target = _absolute_user_path(target_raw)

    timeout_raw = namespace.timeout
    try:
        timeout = float(timeout_raw)
    except (TypeError, ValueError):
        raise ConfigurationError("timeout must be finite and within the supported range")
    if not (MIN_TIMEOUT <= timeout <= MAX_TIMEOUT) or timeout != timeout or timeout in (float("inf"), float("-inf")):
        raise ConfigurationError("timeout must be finite and within the supported range")

    raw_modules = namespace.modules
    if not isinstance(raw_modules, str) or not raw_modules:
        raise ConfigurationError("modules must be a non-empty comma-separated list")
    modules = tuple(item.strip() for item in raw_modules.split(","))
    if any(not item for item in modules):
        raise ConfigurationError("modules must not contain empty names")
    if len(set(modules)) != len(modules):
        raise ConfigurationError("modules must be unique")
    unknown = [item for item in modules if item not in KNOWN_MODULES]
    if unknown:
        raise ConfigurationError("modules contains an unknown module")

    module_dir_raw = os.environ.get("MACOS_CLEANUP_MODULE_DIR") or os.fspath(SCRIPT_DIR)
    module_dir = Path(_absolute_user_path(module_dir_raw))
    for module in modules:
        script = module_dir / f"audit-{module}.sh"
        if not script.is_file() or not os.access(script, os.X_OK):
            raise ConfigurationError("requested audit module is unavailable")

    output_raw = namespace.output
    if output_raw is not None and (not output_raw or "\x00" in output_raw):
        raise ConfigurationError("output must be a non-empty path")
    output = None if output_raw is None else Path(_absolute_user_path(output_raw))
    # Validate ancestry before any output parent or file is created.
    try:
        if output is not None:
            _check_ancestry(output)
        else:
            _check_ancestry(Path.cwd() / "macos-cleanup" / datetime.now().strftime("%Y-%m-%d"))
    except OSError:
        raise OutputPathError("output path is unavailable or unsafe")
    return RunConfig(target, output, timeout, modules, module_dir, home)


def _arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audit-all.sh",
        description="Run the read-only macOS cleanup audit modules safely.",
        allow_abbrev=False,
    )
    # Avoid Path.home() here: no user-controlled filesystem/account lookup is
    # allowed before the no-hydration guard is established in main().
    parser.add_argument("--target", default=os.environ.get("HOME") or "/")
    parser.add_argument("--output")
    parser.add_argument(
        "--timeout",
        default=os.environ.get("MACOS_CLEANUP_MODULE_TIMEOUT", str(MAX_TIMEOUT)),
        help=f"Per-module timeout in seconds (default and maximum: {MAX_TIMEOUT:g}); "
             "overridable by MACOS_CLEANUP_MODULE_TIMEOUT",
    )
    parser.add_argument("--modules", default=",".join(DEFAULT_MODULES))
    return parser


def _fixed_error_file(path: Path, message: str) -> None:
    _atomic_write(path, message + "\n")


@contextmanager
def _orchestration_signal_scope(cancelled: threading.Event) -> Iterator[None]:
    """Keep cancellation cooperative across parsing, reporting, and cleanup.

    ``audit_runtime`` temporarily owns SIGTERM while a child is running and
    restores this handler before returning its cancelled result. Outside that
    window this handler only records the event, so every temporary file still
    passes through the normal cleanup ``finally`` blocks.
    """

    previous: dict[int, Any] = {}
    installed: list[int] = []

    def handle(signum: int, _frame: Any) -> None:
        cancelled.set()
        if signum == signal.SIGINT:
            # The runtime listens for SIGTERM while it owns a child. This
            # preserves Ctrl-C cleanup there without making the outer handler
            # raise through parsing or report generation.
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            except OSError:
                pass

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handle)
            installed.append(signum)
        yield
    finally:
        for signum in reversed(installed):
            try:
                signal.signal(signum, previous[signum])
            except (OSError, ValueError):
                pass


def _emit_guard_failure_status() -> int:
    print(
        json.dumps(
            {
                "schema_version": "1",
                "record_type": "audit_summary",
                "module": "orchestrator",
                "status": "UNAVAILABLE",
                "outcome": "guard-unavailable",
                "coverage_status": "NOT-RUN",
                "cloud_targets_actionable": False,
                "sensitive": False,
            },
            separators=(",", ":"),
        )
    )
    print(INITIAL_GUARD_DIAGNOSTIC, file=sys.stderr)
    return GUARD_EXIT_CODE


def _emit_pre_run_cancel_status() -> int:
    print(
        json.dumps(
            {
                "schema_version": "1",
                "record_type": "audit_summary",
                "module": "orchestrator",
                "status": "PARTIAL",
                "outcome": CANCEL_OUTCOME,
                "coverage_status": "NOT-RUN",
                "cloud_targets_actionable": False,
                "sensitive": False,
            },
            separators=(",", ":"),
        )
    )
    return CANCEL_EXIT_CODE


def _result_field(result: Any, name: str, default: Any) -> Any:
    if isinstance(result, Mapping):
        return result.get(name, default)
    return getattr(result, name, default)


def _not_run_record(
    output: Path,
    module: str,
    *,
    status: str = "NOT-RUN",
    outcome: str = "not-run",
    reason: str = "module not run",
) -> tuple[dict[str, Any], ParsedModule]:
    module_path = output / "modules" / f"{module}.jsonl"
    error_path = output / "modules" / f"{module}.stderr"
    _atomic_write(module_path, "")
    _fixed_error_file(error_path, reason)
    return (
        {
            "schema_version": "1",
            "record_type": "orchestrator_module",
            "module": "orchestrator",
            "status": status,
            "child_module": module,
            "observed_status": status,
            "exit_code": None,
            "timed_out": False,
            "duration_seconds": None,
            "final_status_seen": False,
            "outcome": outcome,
            "output_path": f"modules/{module}.jsonl",
            "error_path": f"modules/{module}.stderr",
            "valid_record_count": 0,
            "malformed_line_count": 0,
            "invalid_record_count": 0,
            "unknown_record_count": 0,
            "sensitive": False,
        },
        ParsedModule(),
    )


def _run_one(
    config: RunConfig, output: Path, module: str, guard_ready: bool
) -> tuple[dict[str, Any], ParsedModule, str | None]:
    if not guard_ready:
        record, parsed = _not_run_record(
            output,
            module,
            status="UNAVAILABLE",
            outcome=GUARD_LOSS_OUTCOME,
            reason="module not run: no-hydration guard unavailable",
        )
        return record, parsed, GUARD_LOSS_OUTCOME

    module_path = output / "modules" / f"{module}.jsonl"
    error_path = output / "modules" / f"{module}.stderr"
    stdout_temp = _private_output_file(output, module, "stdout")
    stderr_temp = _private_output_file(output, module, "stderr")
    parsed = ParsedModule()
    returncode: int | None = None
    returncode_valid = False
    duration_seconds: int | float | None = None
    timed_out = False
    outcome = "runner-error"
    status = "PARTIAL"
    terminal_reason: str | None = None
    try:
        try:
            result = run_command(
                _module_argv(module, config.module_dir, config.target, config.home),
                timeout=config.timeout,
                stdout_path=stdout_temp,
                stderr_path=stderr_temp,
                env=None,
                cwd=None,
                max_output_bytes=MAX_OUTPUT_BYTES,
                guarded=True,
            )
            raw_returncode = _result_field(result, "returncode", None)
            if isinstance(raw_returncode, int) and not isinstance(raw_returncode, bool):
                returncode = raw_returncode
                returncode_valid = True
            raw_duration = _result_field(result, "duration_seconds", None)
            if (
                isinstance(raw_duration, (int, float))
                and not isinstance(raw_duration, bool)
                and math.isfinite(raw_duration)
                and raw_duration >= 0
            ):
                duration_seconds = raw_duration
            raw_timed_out = _result_field(result, "timed_out", False)
            timed_out = raw_timed_out if isinstance(raw_timed_out, bool) else False
            raw_outcome = _result_field(result, "outcome", None)
            runtime_outcome = raw_outcome if isinstance(raw_outcome, str) else None
            decoded_output, invalid_utf8 = _decode_output(stdout_temp)
            parsed = _parse_module_output(
                decoded_output, module, config.home, invalid_utf8=invalid_utf8
            )

            if timed_out:
                outcome = "timeout"
            elif runtime_outcome == CANCEL_OUTCOME:
                outcome = CANCEL_OUTCOME
                terminal_reason = CANCEL_OUTCOME
            elif runtime_outcome == GUARD_LOSS_OUTCOME:
                outcome = GUARD_LOSS_OUTCOME
                terminal_reason = GUARD_LOSS_OUTCOME
            elif runtime_outcome in KNOWN_RUNTIME_OUTCOMES - {
                SUCCESS_OUTCOME,
                CANCEL_OUTCOME,
                GUARD_LOSS_OUTCOME,
            }:
                outcome = runtime_outcome
            elif runtime_outcome != SUCCESS_OUTCOME:
                outcome = "invalid-outcome"
            elif not returncode_valid:
                outcome = "invalid-return-code"
            elif returncode != 0:
                outcome = "nonzero-exit"
            elif not parsed.final_status_seen:
                outcome = "missing-final-status"
            elif parsed.malformed_line_count or parsed.invalid_record_count or parsed.unknown_record_count:
                outcome = "invalid-or-unknown-records"
            else:
                outcome = SUCCESS_OUTCOME

            if outcome == GUARD_LOSS_OUTCOME:
                status = "UNAVAILABLE"
            elif outcome in {
                SUCCESS_OUTCOME,
            }:
                observed = parsed.observed_status or "FAILED"
                status = observed if observed in KNOWN_STATUSES else "PARTIAL"
            else:
                status = "PARTIAL"
            _atomic_write(
                module_path,
                "".join(_safe_json_line(record) for record in parsed.records),
            )
            if status == "COMPLETE":
                _fixed_error_file(error_path, "")
            else:
                _fixed_error_file(error_path, f"module status: {status}; stderr redacted")
        except GuardUnavailable:
            status = "UNAVAILABLE"
            outcome = GUARD_LOSS_OUTCOME
            terminal_reason = GUARD_LOSS_OUTCOME
            _atomic_write(module_path, "")
            _fixed_error_file(error_path, "module not run: no-hydration guard unavailable")
        except (RecursionError, OSError, ValueError, TypeError):
            # Runtime exceptions are not allowed to turn stderr into a
            # secret-bearing report. The following fixed status is enough
            # for callers to repeat the narrow module audit.
            status = "PARTIAL"
            outcome = "runner-error"
            _atomic_write(module_path, "")
            _fixed_error_file(error_path, "module runner failed; stderr redacted")
        except Exception:
            status = "PARTIAL"
            outcome = "runner-error"
            _atomic_write(module_path, "")
            _fixed_error_file(error_path, "module runner failed; stderr redacted")
    finally:
        for temporary in (stdout_temp, stderr_temp):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    orchestrator_record = {
        "schema_version": "1",
        "record_type": "orchestrator_module",
        "module": "orchestrator",
        "status": status,
        "child_module": module,
        "observed_status": parsed.observed_status or ("UNAVAILABLE" if status == "UNAVAILABLE" else "FAILED"),
        "exit_code": returncode,
        "timed_out": timed_out,
        "duration_seconds": duration_seconds,
        "final_status_seen": parsed.final_status_seen,
        "outcome": outcome,
        "output_path": f"modules/{module}.jsonl",
        "error_path": f"modules/{module}.stderr",
        "valid_record_count": len(parsed.records),
        "malformed_line_count": parsed.malformed_line_count,
        "invalid_record_count": parsed.invalid_record_count,
        "unknown_record_count": parsed.unknown_record_count,
        "sensitive": False,
    }
    return orchestrator_record, parsed, terminal_reason


def _safe_json_line(record: Mapping[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"


def _capacity_drift(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    samples = [
        record
        for record in records
        if record.get("record_type") == "capacity"
        and isinstance(record.get("available_bytes"), int)
        and not isinstance(record.get("available_bytes"), bool)
    ]
    if len(samples) < 2:
        return {"status": "NOT-MEASURED", "sample_count": len(samples)}

    identities = [record.get("volume_device") for record in samples]
    if (
        any(not isinstance(identity, str) or not identity for identity in identities)
        or len(set(identities)) != 1
    ):
        return {
            "status": "NOT-MEASURED",
            "sample_count": len(samples),
            "reason": "capacity samples do not share one target-volume identity",
        }
    values = [record["available_bytes"] for record in samples]
    return {
        "status": "MEASURED",
        "sample_count": len(values),
        "available_delta_bytes": values[-1] - values[0],
    }


def _markdown_code(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    text = CONTROL_RE.sub("", text)
    text = html.escape(text, quote=True)
    return text.replace("`", "&#96;")


def _markdown_list(values: Sequence[Any]) -> str:
    if not values:
        return "none"
    return ", ".join(f"`{_markdown_code(value)}`" for value in values)


def _render_report(
    summary: Mapping[str, Any],
    orchestrator_records: Sequence[Mapping[str, Any]],
    child_records: Sequence[Mapping[str, Any]],
) -> str:
    candidates = [
        record for record in child_records if record.get("record_type") == "cleanup_candidate"
    ]
    cloud_roots = [record for record in child_records if record.get("record_type") == "cloud_root"]
    coverage_text = [f"{name}={summary['coverage'][name]}" for name in summary["coverage"]]
    lines = [
        "# macOS Cleanup Audit",
        "",
        "No cleanup commands invoked; collectors launched with no-hydration policy. Owner tools may update their metadata; not OS-wide read-only sandbox.",
        "Modules were run sequentially; this report makes no claim of parallel execution.",
        "",
        "## Completion",
        "",
        f"- Module run status: `{_markdown_code(summary['status'])}`",
        f"- Broad coverage status: `{_markdown_code(summary['coverage_status'])}`",
        f"- Schema-valid review-only candidates: {summary['review_only_candidate_count']}",
        "- Actionable candidates: 0 (this audit never authorizes execution)",
        f"- Discovered cloud roots: {summary['cloud_root_count']}",
        f"- Incomplete modules: {summary['incomplete_module_count']}",
        "",
        "## Requested and covered scope",
        "",
        f"- Requested modules: {_markdown_list(summary['requested_modules'])}",
        f"- Attempted modules: {_markdown_list(summary['attempted_modules'])}",
        f"- Executed modules: {_markdown_list(summary['executed_modules'])}",
        f"- Omitted supported modules: {_markdown_list(summary['omitted_modules'])}",
        f"- Unsupported broad categories: {_markdown_list(summary['unsupported_modules'])}",
        f"- Actual supported module set: {_markdown_list(summary['actual_modules'])}",
        f"- Full coverage statuses: {_markdown_list(coverage_text)}",
        "",
        "## Modules",
        "",
    ]
    for record in orchestrator_records:
        lines.append(
            f"- `{_markdown_code(record['child_module'])}`: "
            f"`{_markdown_code(record['status'])}` "
            f"({_markdown_code(record['outcome'])}); "
            f"valid records {record['valid_record_count']}, "
            f"malformed {record['malformed_line_count']}, "
            f"invalid {record['invalid_record_count']}, "
            f"unknown {record['unknown_record_count']}"
        )
    lines.extend(["", "## Review-Only Candidates", ""])
    if not candidates:
        lines.append("No schema-valid cleanup candidates were produced.")
    else:
        for candidate in candidates:
            size = candidate.get("allocated_bytes")
            size_text = "unknown" if size is None else f"{size} bytes (measured estimate; not reclaimable total)"
            guidance = (
                candidate.get("review_action")
                or candidate.get("proposed_action")
                or "Dedicated review required: verify owner activity, scope and supported cleanup semantics"
            )
            lines.append(
                f"- {size_text} | owner `{_markdown_code(candidate.get('owner'))}` | "
                f"path `{_markdown_code(candidate.get('path'))}` | "
                f"review guidance `{_markdown_code(guidance)}` | `REVIEW-ONLY`"
            )
    metadata_records = [record for record in child_records
                        if record.get("inventory_scope") == "bounded-item-metadata"]
    if metadata_records:
        lines.extend(["", "## Cloud Metadata", ""])
        for metadata in metadata_records:
            counts = metadata.get("counts", {})
            totals = metadata.get("totals", {})
            counts = counts if isinstance(counts, dict) else {}
            totals = totals if isinstance(totals, dict) else {}
            allocated = metadata.get("allocated_bytes")
            lower_bound = totals.get("allocated_lower_bound_bytes")
            if not isinstance(lower_bound, int) or isinstance(lower_bound, bool) or lower_bound < 0:
                lower_bound = None
            size_text = (f"{allocated} measured allocated bytes" if allocated is not None
                         else (f"unknown total; known lower bound {lower_bound} bytes"
                               if lower_bound is not None else "allocation unavailable"))
            lines.append(
                f"- `{_markdown_code(metadata.get('domain_label'))}`: "
                f"`{_markdown_code(metadata.get('enumeration_status'))}`, "
                f"{_markdown_code(counts.get('items', 0))} inspected items; {size_text}."
            )
        lines.append("Metadata was attempted automatically with per-root budgets. "
                     "No content reads, sync proof or eviction authorization is implied.")
    lines.extend([
        "",
        "## Safety gate",
        "",
        "All measured candidates remain non-actionable and REVIEW-ONLY pending dedicated owner-aware review and verification.",
        "Candidate sizes are individual estimates; no overlapping or reclaimable total is reported.",
    ])
    if cloud_roots:
        lines.append(
            "Cloud roots were discovered, but no cloud item adapter authorizes eviction. "
            "Root measurements are not cleanup approval."
        )
    else:
        lines.append(
            "Cloud discovery produced no validated root records in this run; absence is not proof that no provider exists."
        )
    drift = summary["capacity_drift"]
    if drift["status"] == "MEASURED":
        lines.append(f"Capacity drift between samples: {drift['available_delta_bytes']} bytes.")
    else:
        lines.append("Capacity drift: not measured (fewer than two capacity samples).")
    lines.extend([
        "",
        "## Validation",
        "",
        f"- Malformed/truncated lines discarded: {summary['malformed_line_count']}",
        f"- Schema-invalid records discarded: {summary['invalid_record_count']}",
        f"- Unknown record types ignored: {summary['unknown_record_count']}",
        "- Raw stdout and stderr were not retained; failure output is represented by fixed sanitized status text.",
        "",
        "Machine-readable inventory: `inventory.jsonl`",
        "",
    ])
    return "\n".join(lines)


def _execute(
    config: RunConfig, cancellation: threading.Event
) -> tuple[Path, Path, int]:
    """Execute after the caller has established the no-hydration guard.

    The cancellation event is owned by the orchestration-lifetime signal
    handler. It is checked between modules and after bounded parse/write work;
    it never causes an asynchronous exception through a temporary-file scope.
    """

    if cancellation.is_set():
        raise KeyboardInterrupt
    output = _create_fresh_output(config.output)
    modules_dir = output / "modules"
    os.mkdir(modules_dir, 0o700)
    os.chmod(modules_dir, 0o700)

    guard_ready = True
    orchestrator_records: list[dict[str, Any]] = []
    child_records: list[dict[str, Any]] = []
    parsed_modules: list[ParsedModule] = []
    attempted_modules: list[str] = []
    executed_modules: list[str] = []
    termination_reason: str | None = None

    for module in config.requested_modules:
        if cancellation.is_set() and termination_reason is None:
            termination_reason = CANCEL_OUTCOME
        if termination_reason is not None:
            orchestrator_record, parsed = _not_run_record(
                output,
                module,
                status="NOT-RUN",
                outcome=termination_reason,
                reason=f"module not run: prior {termination_reason} stopped the audit",
            )
        elif not guard_ready:
            # The first record explains why the guard blocked the run; all
            # following modules are explicitly NOT-RUN rather than attempted.
            first_guard_failure = not orchestrator_records
            orchestrator_record, parsed = _not_run_record(
                output,
                module,
                status="UNAVAILABLE" if first_guard_failure else "NOT-RUN",
                outcome=GUARD_LOSS_OUTCOME,
                reason="module not run: no-hydration guard unavailable",
            )
            termination_reason = GUARD_LOSS_OUTCOME
        else:
            attempted_modules.append(module)
            orchestrator_record, parsed, module_terminal_reason = _run_one(
                config, output, module, True
            )
            # A process that produced a result other than a guard/spawn
            # failure did execute, even if the audit result is partial.
            if orchestrator_record["outcome"] not in {
                GUARD_LOSS_OUTCOME,
                "spawn-error",
                "runner-error",
                "invalid-outcome",
                "invalid-return-code",
            }:
                executed_modules.append(module)
            if cancellation.is_set() and module_terminal_reason is None:
                # The signal may have arrived while parsing or sanitizing the
                # bounded child output rather than while the child ran.
                module_terminal_reason = CANCEL_OUTCOME
                orchestrator_record["status"] = "PARTIAL"
                orchestrator_record["outcome"] = CANCEL_OUTCOME
            if module_terminal_reason is not None:
                termination_reason = module_terminal_reason
                if module_terminal_reason == GUARD_LOSS_OUTCOME:
                    guard_ready = False
        orchestrator_records.append(orchestrator_record)
        child_records.extend(parsed.records)
        parsed_modules.append(parsed)

    run_status = (
        "COMPLETE"
        if termination_reason is None and all(record["status"] == "COMPLETE" for record in orchestrator_records)
        else "PARTIAL"
    )
    malformed_count = sum(item.malformed_line_count for item in parsed_modules)
    invalid_count = sum(item.invalid_record_count for item in parsed_modules)
    unknown_count = sum(item.unknown_record_count for item in parsed_modules)
    candidates = [record for record in child_records if record.get("record_type") == "cleanup_candidate"]
    actionable_count = sum(1 for record in candidates if record.get("actionable") is True)
    cloud_root_count = sum(1 for record in child_records if record.get("record_type") == "cloud_root")
    incomplete_count = sum(1 for record in orchestrator_records if record.get("status") != "COMPLETE")
    omitted_modules = [module for module in KNOWN_MODULES if module not in config.requested_modules]
    not_run_modules = [
        record["child_module"]
        for record in orchestrator_records
        if record.get("status") == "NOT-RUN"
    ]
    unsupported = list(COVERAGE_CATEGORIES)
    drift = _capacity_drift(child_records)
    summary: dict[str, Any] = {
        "schema_version": "1",
        "record_type": "audit_summary",
        "module": "orchestrator",
        "status": run_status,
        "expected_modules": " ".join(config.requested_modules),
        "requested_modules": list(config.requested_modules),
        "attempted_modules": attempted_modules,
        "executed_modules": executed_modules,
        "not_run_modules": not_run_modules,
        "termination_reason": termination_reason,
        "omitted_modules": omitted_modules,
        "omitted_module_statuses": {module: "NOT-RUN" for module in omitted_modules},
        "unsupported_modules": unsupported,
        "actual_modules": list(KNOWN_MODULES),
        "module_lists": {
            "requested": list(config.requested_modules),
            "attempted": attempted_modules,
            "executed": executed_modules,
            "not_run": not_run_modules,
            "omitted": omitted_modules,
            "unsupported": unsupported,
            "actual": list(KNOWN_MODULES),
        },
        "coverage_status": "PARTIAL",
        "coverage": {category: "NOT-IMPLEMENTED" for category in COVERAGE_CATEGORIES},
        "candidate_count": len(candidates),
        "review_only_candidate_count": len(candidates),
        "actionable_candidate_count": actionable_count,
        "cloud_root_count": cloud_root_count,
        "incomplete_module_count": incomplete_count,
        "malformed_line_count": malformed_count,
        "invalid_record_count": invalid_count,
        "unknown_record_count": unknown_count,
        "capacity_drift": drift,
        "cloud_targets_actionable": False,
        "inventory_path": "inventory.jsonl",
        "report_path": "report.md",
        "sensitive": False,
    }

    def update_for_late_cancellation() -> bool:
        nonlocal termination_reason
        if cancellation.is_set() and termination_reason != CANCEL_OUTCOME:
            termination_reason = CANCEL_OUTCOME
            summary["status"] = "PARTIAL"
            summary["termination_reason"] = CANCEL_OUTCOME
            return True
        return False

    def write_inventory() -> None:
        inventory_content = "".join(_safe_json_line(record) for record in child_records)
        inventory_content += "".join(_safe_json_line(record) for record in orchestrator_records)
        inventory_content += _safe_json_line(summary)
        _atomic_write(output / "inventory.jsonl", inventory_content)

    update_for_late_cancellation()
    write_inventory()
    if update_for_late_cancellation():
        write_inventory()

    report_content = _render_report(summary, orchestrator_records, child_records)
    if update_for_late_cancellation():
        write_inventory()
        report_content = _render_report(summary, orchestrator_records, child_records)
    _atomic_write(output / "report.md", report_content + "\n")
    if update_for_late_cancellation():
        write_inventory()
        _atomic_write(
            output / "report.md",
            _render_report(summary, orchestrator_records, child_records) + "\n",
        )
    exit_code = CANCEL_EXIT_CODE if termination_reason == CANCEL_OUTCOME else 0
    return output / "report.md", output / "inventory.jsonl", exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = _arg_parser()
    try:
        namespace = parser.parse_args(argv)
    except KeyboardInterrupt:
        return _emit_pre_run_cancel_status()

    # This must precede _validate_config: that routine stats user-selected
    # module/output paths, and _execute creates the run directory. On initial
    # guard failure, emit only fixed machine/diagnostic status and code 78;
    # never touch a potentially dataless user-controlled parent.
    try:
        enable_no_hydration()
    except Exception:
        return _emit_guard_failure_status()

    cancellation = threading.Event()
    with _orchestration_signal_scope(cancellation):
        if cancellation.is_set():
            return _emit_pre_run_cancel_status()
        try:
            config = _validate_config(namespace)
        except ConfigurationError as error:
            parser.error(str(error))
            return 2
        except OutputPathError as error:
            print(str(error), file=sys.stderr)
            return 2
        if cancellation.is_set():
            return _emit_pre_run_cancel_status()

        try:
            report_path, inventory_path, code = _execute(config, cancellation)
        except KeyboardInterrupt:
            cancellation.set()
            return _emit_pre_run_cancel_status()
        except OutputPathError as error:
            print(str(error), file=sys.stderr)
            return 2
        except OSError:
            print("could not create the audit run", file=sys.stderr)
            return 2
        # Return paths relative to the current working directory so auto-created
        # runs remain discoverable without exposing an absolute home path.
        print(_redact_string(os.path.relpath(report_path, Path.cwd()), config.home))
        print(_redact_string(os.path.relpath(inventory_path, Path.cwd()), config.home))
        return code


if __name__ == "__main__":
    raise SystemExit(main())
