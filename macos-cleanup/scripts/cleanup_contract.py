#!/usr/bin/env python3
"""Strict, metadata-only preparation contract for cache cleanup plans.

This module never executes an owner command.  It binds a bounded filesystem
observation and an executable identity to a small plan which a later executor
may validate again immediately before dispatch.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import sys
import time
from typing import Any

from audit_runtime import GuardUnavailable, enable_no_hydration as _runtime_enable_no_hydration


class PlanError(ValueError):
    """A sanitized plan or filesystem-contract failure."""


# Darwin's sys/stat.h defines SF_DATALESS as 0x40000000.  Keep the value local
# so importing this module does not require an SDK or a Python dependency.
SF_DATALESS = 0x40000000

_MAX_TTL_SECONDS = 900
_MAX_ENTRIES = 10_000
_MAX_ANCESTORS = 256
_DEFAULT_CAPTURE_TIMEOUT = 10.0
_MAX_CAPTURE_TIMEOUT = 30.0
_SCHEMA_VERSION = 1

_ACTIONS = {
    "uv": "uv-cache-prune",
    "go": "go-build-cache-clean",
}
_SCOPE = "owner-managed-dynamic-cache"
_RISK = (
    "Owner-managed cache maintenance may remove shared cache entries; "
    "re-download/rebuild required. Scope is dynamic, not a fixed file "
    "deletion list."
)

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

_META_KEYS = (
    "dev",
    "ino",
    "mode",
    "nlink",
    "uid",
    "gid",
    "size",
    "blocks",
    "blksize",
    "rdev",
    "mtime_ns",
    "ctime_ns",
    "birthtime_ns",
    "flags",
)
_META_KEY_SET = frozenset(_META_KEYS)

_TARGET_STATE_KEYS = frozenset(
    {
        "version",
        "max_entries",
        "entry_count",
        "allocated_bytes",
        "logical_bytes",
        "fingerprint",
        "root",
        "home",
        "filesystem_root",
        "ancestors",
    }
)
_EXECUTABLE_STATE_KEYS = frozenset({"version", "identity"})


class _DuplicateKey(Exception):
    pass


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey()
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def enable_no_hydration() -> None:
    """Use the shared Darwin guard and expose only a sanitized public error."""

    try:
        _runtime_enable_no_hydration()
    except GuardUnavailable:
        raise PlanError("no-hydration guard unavailable") from None
    except Exception:
        raise PlanError("no-hydration guard unavailable") from None


def _fail(message: str) -> None:
    # Never include paths, errno text, owner output or command details.
    raise PlanError(message)


def _number(value: object, *, integer: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("invalid numeric value")
    if integer and not isinstance(value, int):
        _fail("invalid integer value")
    try:
        if not math.isfinite(float(value)):
            _fail("nonfinite numeric value")
    except (OverflowError, TypeError, ValueError):
        _fail("invalid numeric value")
    return value


def _text(value: object, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        _fail("invalid text value")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        _fail("control character in value")
    return value


def _path(value: object, *, allow_root: bool = False) -> str:
    value = _text(value)
    if not os.path.isabs(value) or value.startswith("//"):
        _fail("path must be absolute")
    normalized = os.path.normpath(value)
    if normalized != value or (value != os.sep and value.endswith(os.sep)):
        _fail("path is not canonical")
    if not allow_root and value == os.sep:
        _fail("root path is not allowed")
    return value


def _path_is_ancestor(ancestor: str, child: str) -> bool:
    try:
        return os.path.commonpath((ancestor, child)) == ancestor
    except ValueError:
        return False


def _reject_cloud_cache_scope(target: str, home: str) -> None:
    def canonical(path: str) -> str:
        if path.startswith("/System/Volumes/Data/"):
            path = path[len("/System/Volumes/Data"):]
        if path == "/var" or path.startswith("/var/"):
            path = "/private" + path
        return path

    target, home = canonical(target), canonical(home)
    for relative in ("Library/CloudStorage", "Library/Mobile Documents", "Dropbox",
                     "OneDrive", "Google Drive", "Yandex.Disk", "Yandex.Disk.localized"):
        if _path_is_ancestor(os.path.join(home, relative), target):
            _fail("cloud namespace is not an executable cache scope")


def _identity_key(metadata: dict[str, int]) -> tuple[int, int]:
    return metadata["dev"], metadata["ino"]


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        _fail("capture timeout exceeded")


def _metadata(value: os.stat_result) -> dict[str, int]:
    result = {
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
    if any(item < 0 for item in result.values()):
        _fail("invalid filesystem metadata")
    if result["flags"] & SF_DATALESS:
        _fail("dataless identity")
    return result


def _validate_directory_identity(value: object) -> dict[str, int]:
    metadata = _validate_metadata(value)
    if not stat.S_ISDIR(metadata["mode"]):
        _fail("invalid directory identity")
    if not (stat.S_IMODE(metadata["mode"]) & 0o111):
        _fail("unsearchable directory identity")
    return metadata


def _allow_darwin_var_alias(
    path: str, value: os.stat_result, deadline: float | None = None
) -> os.stat_result | None:
    if sys.platform != "darwin" or path != "/var" or not stat.S_ISLNK(value.st_mode):
        return None
    try:
        _check_deadline(deadline)
        actual = os.stat(path)
        _check_deadline(deadline)
        private_var = os.stat("/private/var")
        _check_deadline(deadline)
    except OSError:
        _fail("filesystem metadata unavailable")
    if (
        not stat.S_ISDIR(actual.st_mode)
        or actual.st_dev != private_var.st_dev
        or actual.st_ino != private_var.st_ino
        or value.st_uid != 0
    ):
        _fail("unsafe filesystem alias")
    return actual


def _check_dataless(value: os.stat_result) -> None:
    if sys.platform == "darwin":
        if not hasattr(value, "st_flags"):
            _fail("dataless metadata unavailable")
        try:
            sdk_value = int(getattr(stat, "SF_DATALESS", SF_DATALESS))
        except (TypeError, ValueError, OverflowError):
            _fail("dataless metadata unavailable")
        if sdk_value != SF_DATALESS:
            _fail("dataless metadata unavailable")
    try:
        flags = int(getattr(value, "st_flags", 0))
    except (TypeError, ValueError):
        _fail("invalid dataless metadata")
    if flags & SF_DATALESS:
        _fail("dataless entry")


def _checked_lstat(
    path: str, *, directory: bool = False, deadline: float | None = None
) -> os.stat_result:
    _check_deadline(deadline)
    try:
        value = os.lstat(path)
    except OSError:
        _fail("filesystem metadata unavailable")
    _check_deadline(deadline)
    alias = _allow_darwin_var_alias(path, value, deadline)
    if alias is not None:
        value = alias
    elif stat.S_ISLNK(value.st_mode):
        _fail("symlink is not allowed")
    if directory and not stat.S_ISDIR(value.st_mode):
        _fail("directory required")
    if directory and not (stat.S_IMODE(value.st_mode) & 0o111):
        _fail("directory is not searchable")
    _check_dataless(value)
    return value


def _ancestor_metadata(
    path: str, deadline: float | None = None
) -> list[dict[str, int]]:
    current = path
    reversed_values: list[dict[str, int]] = []
    while True:
        _check_deadline(deadline)
        if len(reversed_values) >= _MAX_ANCESTORS:
            _fail("ancestry is too deep")
        value = _checked_lstat(current, directory=True, deadline=deadline)
        reversed_values.append(_metadata(value))
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    reversed_values.reverse()
    return reversed_values


def _validate_max_entries(value: object) -> int:
    value = _number(value, integer=True)
    if value <= 0 or value > _MAX_ENTRIES:
        _fail("invalid entry bound")
    return int(value)


def _validate_ttl(value: object) -> int:
    value = _number(value, integer=True)
    if value <= 0 or value > _MAX_TTL_SECONDS:
        _fail("plan lifetime exceeds maximum")
    return int(value)


def _validate_capture_timeout(value: object) -> float:
    value = _number(value)
    if float(value) <= 0 or float(value) > _MAX_CAPTURE_TIMEOUT:
        _fail("invalid capture timeout")
    return float(value)


def _snapshot_tree(
    root: str, max_entries: int, deadline: float | None = None
) -> dict[str, Any]:
    _check_deadline(deadline)
    root_value = _checked_lstat(root, directory=True, deadline=deadline)
    root_metadata = _metadata(root_value)
    device = root_metadata["dev"]
    records: list[tuple[str, tuple[int, ...]]] = []
    stack: list[tuple[str, str]] = [(root, "")]
    seen: set[tuple[int, int]] = {_identity_key(root_metadata)}
    logical = root_metadata["size"]
    allocated = root_metadata["blocks"] * 512

    while stack:
        _check_deadline(deadline)
        directory, relative = stack.pop()
        try:
            _check_deadline(deadline)
            with os.scandir(directory) as iterator:
                _check_deadline(deadline)
                names: list[str] = []
                for entry in iterator:
                    _check_deadline(deadline)
                    if 1 + len(records) + len(names) >= max_entries:
                        _fail("entry bound exceeded")
                    names.append(entry.name)
            _check_deadline(deadline)
        except PlanError:
            raise
        except OSError:
            _fail("directory enumeration incomplete")

        names.sort()
        child_directories: list[tuple[str, str]] = []
        for name in names:
            _check_deadline(deadline)
            child_path = os.path.join(directory, name)
            child_value = _checked_lstat(child_path, deadline=deadline)
            child_metadata = _metadata(child_value)
            if child_metadata["dev"] != device:
                _fail("crossed filesystem mount")
            if not (stat.S_ISREG(child_value.st_mode) or stat.S_ISDIR(child_value.st_mode)):
                _fail("unsupported filesystem entry")

            child_relative = name if not relative else relative + "/" + name
            records.append(
                (
                    child_relative,
                    tuple(child_metadata[key] for key in _META_KEYS),
                )
            )
            identity = _identity_key(child_metadata)
            if identity not in seen:
                seen.add(identity)
                logical += child_metadata["size"]
                allocated += child_metadata["blocks"] * 512
            if stat.S_ISDIR(child_value.st_mode):
                if not (stat.S_IMODE(child_value.st_mode) & 0o111):
                    _fail("directory is not searchable")
                child_directories.append((child_path, child_relative))

        # The stack is deterministic; records are also sorted before hashing.
        stack.extend(reversed(child_directories))

    records.sort(key=lambda item: item[0])
    return {
        "root": root_metadata,
        "records": records,
        "entry_count": 1 + len(records),
        "logical_bytes": logical,
        "allocated_bytes": allocated,
    }


def _capture_target_unchecked(
    target: str,
    home: str,
    max_entries: int,
    deadline: float | None = None,
) -> dict[str, Any]:
    _check_deadline(deadline)
    target = _path(target)
    home = _path(home)
    _reject_cloud_cache_scope(target, home)
    if target == home or _path_is_ancestor(target, home):
        _fail("target is a home alias or broad ancestor")

    home_before = _metadata(_checked_lstat(home, directory=True, deadline=deadline))
    filesystem_root_before = _metadata(
        _checked_lstat(os.sep, directory=True, deadline=deadline)
    )
    if _identity_key(home_before) == _identity_key(filesystem_root_before):
        _fail("home aliases a protected directory")
    target_before = _metadata(_checked_lstat(target, directory=True, deadline=deadline))
    if _identity_key(target_before) in {
        _identity_key(home_before),
        _identity_key(filesystem_root_before),
    }:
        _fail("target aliases a protected directory")
    ancestors_before = _ancestor_metadata(target, deadline)
    before = _snapshot_tree(target, max_entries, deadline)

    home_after = _metadata(_checked_lstat(home, directory=True, deadline=deadline))
    filesystem_root_after = _metadata(
        _checked_lstat(os.sep, directory=True, deadline=deadline)
    )
    ancestors_after = _ancestor_metadata(target, deadline)
    after = _snapshot_tree(target, max_entries, deadline)

    if (
        before["root"] != after["root"]
        or before["records"] != after["records"]
        or ancestors_before != ancestors_after
        or home_before != home_after
        or filesystem_root_before != filesystem_root_after
    ):
        _fail("target changed during capture")

    root_metadata = before["root"]
    home_identity = _identity_key(home_before)
    filesystem_root_identity = _identity_key(filesystem_root_before)
    if _identity_key(root_metadata) in {home_identity, filesystem_root_identity}:
        _fail("target aliases a protected directory")

    fingerprint_payload = {
        "root_before": before["root"],
        "root_after": after["root"],
        "children_before": [
            [relative, list(metadata)] for relative, metadata in before["records"]
        ],
        "children_after": [
            [relative, list(metadata)] for relative, metadata in after["records"]
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()

    return {
        "version": 1,
        "max_entries": max_entries,
        "entry_count": before["entry_count"],
        "allocated_bytes": before["allocated_bytes"],
        "logical_bytes": before["logical_bytes"],
        "fingerprint": fingerprint,
        "root": root_metadata,
        "home": home_before,
        "filesystem_root": filesystem_root_before,
        "ancestors": ancestors_before,
    }


def capture_target(
    path: str,
    *,
    home: str | None = None,
    max_entries: int = _MAX_ENTRIES,
    timeout: float = _DEFAULT_CAPTURE_TIMEOUT,
) -> dict[str, Any]:
    """Capture a bounded, metadata-only target fingerprint."""

    enable_no_hydration()
    timeout = _validate_capture_timeout(timeout)
    deadline = time.monotonic() + timeout
    if home is None:
        home = os.environ.get("HOME")
    if home is None:
        _fail("home is unavailable")
    return _capture_target_unchecked(
        _path(path),
        _path(home),
        _validate_max_entries(max_entries),
        deadline,
    )


def _safe_resolved_executable(executable: str) -> tuple[str, dict[str, int]]:
    executable = _path(executable)
    try:
        resolved = os.path.realpath(executable)
    except (OSError, ValueError):
        _fail("executable resolution failed")
    resolved = _path(resolved)

    # Inspect ancestors for searchability, but do not persist directory state
    # as executable identity.  The bound identity is only the resolved file.
    parent = os.path.dirname(resolved)
    while True:
        _checked_lstat(parent, directory=True)
        if parent == os.sep:
            break
        next_parent = os.path.dirname(parent)
        if next_parent == parent:
            _fail("executable ancestry is invalid")
        parent = next_parent

    value = _checked_lstat(resolved)
    if not stat.S_ISREG(value.st_mode) or not (value.st_mode & 0o111):
        _fail("executable is not a regular executable")
    if value.st_uid != os.geteuid():
        _fail("executable ownership is not trusted")
    return resolved, _metadata(value)


def _capture_executable_state(executable: str) -> tuple[str, dict[str, Any]]:
    resolved, identity = _safe_resolved_executable(executable)
    return resolved, {"version": 1, "identity": identity}


def _validate_metadata(value: object) -> dict[str, int]:
    if not isinstance(value, dict) or frozenset(value) != _META_KEY_SET:
        _fail("invalid identity shape")
    result: dict[str, int] = {}
    for key in _META_KEYS:
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            _fail("invalid identity value")
        result[key] = item
    if result["ino"] == 0 or result["nlink"] == 0:
        _fail("invalid filesystem identity")
    return result


def _validate_target_state(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != _TARGET_STATE_KEYS:
        _fail("invalid target state shape")
    version = value["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        _fail("invalid target state version")
    max_entries = _validate_max_entries(value["max_entries"])
    entry_count = _number(value["entry_count"], integer=True)
    if entry_count < 1 or entry_count > max_entries:
        _fail("invalid target entry count")
    allocated = _number(value["allocated_bytes"], integer=True)
    logical = _number(value["logical_bytes"], integer=True)
    if allocated < 0 or logical < 0:
        _fail("invalid target size")
    fingerprint = value["fingerprint"]
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        _fail("invalid target fingerprint")
    root = _validate_directory_identity(value["root"])
    home = _validate_directory_identity(value["home"])
    filesystem_root = _validate_directory_identity(value["filesystem_root"])
    if filesystem_root["uid"] != 0:
        _fail("invalid filesystem root ownership")
    if home["uid"] != os.geteuid() or root["uid"] != os.geteuid():
        _fail("invalid target ownership")
    ancestors = value["ancestors"]
    if not isinstance(ancestors, list) or not (1 <= len(ancestors) <= _MAX_ANCESTORS):
        _fail("invalid target ancestry")
    validated_ancestors = [_validate_directory_identity(item) for item in ancestors]
    ancestor_ids = [_identity_key(item) for item in validated_ancestors]
    if len(set(ancestor_ids)) != len(ancestor_ids):
        _fail("invalid target ancestry identity")
    if ancestor_ids[0] != _identity_key(filesystem_root):
        _fail("invalid target ancestry root")
    if ancestor_ids[-1] != _identity_key(root):
        _fail("invalid target ancestry target")
    if _identity_key(home) == _identity_key(filesystem_root):
        _fail("target state aliases a protected identity")
    if _identity_key(root) in {
        _identity_key(home),
        _identity_key(filesystem_root),
    }:
        _fail("target state aliases a protected identity")
    return {
        "version": 1,
        "max_entries": max_entries,
        "entry_count": int(entry_count),
        "allocated_bytes": int(allocated),
        "logical_bytes": int(logical),
        "fingerprint": fingerprint,
        "root": root,
        "home": home,
        "filesystem_root": filesystem_root,
        "ancestors": validated_ancestors,
    }


def _validate_executable_state(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != _EXECUTABLE_STATE_KEYS:
        _fail("invalid executable state shape")
    version = value["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        _fail("invalid executable state version")
    identity = _validate_metadata(value["identity"])
    if not stat.S_ISREG(identity["mode"]) or not (identity["mode"] & 0o111):
        _fail("invalid executable identity")
    if identity["uid"] != os.geteuid():
        _fail("invalid executable ownership")
    return {"version": 1, "identity": identity}


def _validate_plan_shape(plan: object) -> dict[str, Any]:
    if not isinstance(plan, dict) or frozenset(plan) != _PLAN_KEYS:
        _fail("invalid plan shape")

    schema_version = plan["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != _SCHEMA_VERSION
    ):
        _fail("unsupported schema version")

    adapter = _text(plan["adapter"])
    action = _text(plan["action"])
    if _ACTIONS.get(adapter) != action:
        _fail("unsupported action")

    target = _path(plan["target"])
    home = _path(plan["home"])
    executable = _path(plan["executable"])
    _reject_cloud_cache_scope(target, home)
    if target == home or _path_is_ancestor(target, home):
        _fail("target is a protected home scope")

    target_state = _validate_target_state(plan["target_state"])
    executable_state = _validate_executable_state(plan["executable_state"])
    version = _text(plan["version"])
    scope = _text(plan["scope"])
    risk = _text(plan["risk"])
    if scope != _SCOPE or risk != _RISK:
        _fail("fixed plan disclosure changed")
    if plan["estimated_reclaimable_bytes"] is not None:
        _fail("reclaim estimate must be unknown")

    created_at = _number(plan["created_at"])
    expires_at = _number(plan["expires_at"])
    if created_at < 0 or expires_at <= created_at:
        _fail("invalid plan lifetime")
    if float(expires_at) - float(created_at) > _MAX_TTL_SECONDS:
        _fail("plan lifetime exceeds maximum")

    return {
        "schema_version": 1,
        "adapter": adapter,
        "action": action,
        "target": target,
        "home": home,
        "target_state": target_state,
        "executable": executable,
        "executable_state": executable_state,
        "version": version,
        "created_at": created_at,
        "expires_at": expires_at,
        "scope": scope,
        "risk": risk,
        "estimated_reclaimable_bytes": None,
    }


def _validate_current_lifetime(plan: dict[str, Any], now: object) -> None:
    now = _number(now)
    if plan["created_at"] > now:
        _fail("plan is from the future")
    if plan["expires_at"] <= now:
        _fail("plan has expired")


def make_plan(
    adapter: str,
    target: str,
    executable: str,
    version: str,
    *,
    home: str | None = None,
    now: float | None = None,
    ttl_seconds: int = _MAX_TTL_SECONDS,
) -> dict[str, Any]:
    """Capture a supported owner-cache plan without approval or execution."""

    adapter = _text(adapter)
    if adapter not in _ACTIONS:
        _fail("unsupported adapter")
    target = _path(target)
    executable = _path(executable)
    version = _text(version)
    if home is None:
        home = os.environ.get("HOME")
    if home is None:
        _fail("home is unavailable")
    home = _path(home)
    ttl_seconds = _validate_ttl(ttl_seconds)
    if now is None:
        now = time.time()
    now = _number(now)

    # capture_target establishes the guard before its first filesystem access.
    target_state = capture_target(target, home=home)
    resolved_executable, executable_state = _capture_executable_state(executable)
    plan = {
        "schema_version": 1,
        "adapter": adapter,
        "action": _ACTIONS[adapter],
        "target": target,
        "home": home,
        "target_state": target_state,
        "executable": resolved_executable,
        "executable_state": executable_state,
        "version": version,
        "created_at": float(now),
        "expires_at": float(now) + ttl_seconds,
        "scope": _SCOPE,
        "risk": _RISK,
        "estimated_reclaimable_bytes": None,
    }
    return _validate_plan_shape(plan)


def validate_plan(plan: object, *, now: float | None = None) -> dict[str, Any]:
    """Strictly validate schema, disclosures, identities and current TTL."""

    validated = _validate_plan_shape(plan)
    if now is None:
        now = time.time()
    _validate_current_lifetime(validated, now)
    return validated


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        _fail("value is not canonical JSON")


def plan_digest(plan: dict) -> str:
    """Return a stable digest without making current time part of validation."""

    validated = _validate_plan_shape(plan)
    return hashlib.sha256(_canonical_json(validated)).hexdigest()


_STABLE_ANCESTOR_KEYS = ("dev", "ino", "mode", "uid", "gid", "flags")


def _stable_ancestor_fingerprint(state: dict[str, Any]) -> tuple[tuple[int, ...], ...]:
    return tuple(
        tuple(metadata[key] for key in _STABLE_ANCESTOR_KEYS)
        for metadata in state["ancestors"]
    )


def _target_state_unchanged(expected: dict[str, Any], current: dict[str, Any]) -> bool:
    for key in (
        "version",
        "max_entries",
        "entry_count",
        "allocated_bytes",
        "logical_bytes",
        "fingerprint",
        "root",
        "home",
        "filesystem_root",
    ):
        if expected[key] != current[key]:
            return False
    return _stable_ancestor_fingerprint(expected) == _stable_ancestor_fingerprint(current)


def target_unchanged(plan: dict) -> bool:
    """Recheck guard, protected identities, target fingerprint and executable."""

    try:
        validated = validate_plan(plan)
        enable_no_hydration()
        current_target = _capture_target_unchecked(
            validated["target"],
            validated["home"],
            validated["target_state"]["max_entries"],
            time.monotonic() + _DEFAULT_CAPTURE_TIMEOUT,
        )
        if not _target_state_unchanged(validated["target_state"], current_target):
            return False
        current_executable, current_state = _capture_executable_state(
            validated["executable"]
        )
        if current_executable != validated["executable"]:
            return False
        return current_state == validated["executable_state"]
    except Exception:
        return False


def _private_ancestor_check(path: str, *, allow_var_alias: bool = True) -> None:
    parent = os.path.dirname(path)
    if not parent:
        _fail("private output parent is invalid")
    current = parent
    chain: list[str] = []
    while True:
        chain.append(current)
        if current == os.sep:
            break
        next_parent = os.path.dirname(current)
        if next_parent == current:
            _fail("private output ancestry is invalid")
        current = next_parent

    for directory in chain:
        try:
            value = os.lstat(directory)
        except OSError:
            _fail("private output ancestry is unavailable")
        if stat.S_ISLNK(value.st_mode):
            allowed_var = (
                allow_var_alias and sys.platform == "darwin" and directory == "/var"
            )
            if not allowed_var:
                _fail("private output ancestry contains symlink")
            try:
                var_value = os.stat(directory)
                private_var = os.stat("/private/var")
            except OSError:
                _fail("private output ancestry is unavailable")
            if (
                not stat.S_ISDIR(var_value.st_mode)
                or var_value.st_dev != private_var.st_dev
                or var_value.st_ino != private_var.st_ino
                or value.st_uid != 0
            ):
                _fail("private output ancestry contains unsafe alias")
            continue
        if not stat.S_ISDIR(value.st_mode) or not (stat.S_IMODE(value.st_mode) & 0o111):
            _fail("private output ancestry is not searchable")
        mode = stat.S_IMODE(value.st_mode)
        writable_by_other = bool(mode & 0o022)
        sticky = bool(mode & stat.S_ISVTX)
        if writable_by_other and not sticky:
            _fail("private output ancestry is writable by another user")


def _private_file_metadata(file_descriptor: int) -> os.stat_result:
    try:
        value = os.fstat(file_descriptor)
    except OSError:
        _fail("private file metadata unavailable")
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.geteuid()
        or value.st_nlink != 1
        or stat.S_IMODE(value.st_mode) != 0o600
    ):
        _fail("private file is unsafe")
    return value


def _cleanup_private_file(path: str, identity: tuple[int, int] | None) -> None:
    """Remove only a file created by this call and still at the same inode."""

    if identity is None:
        return
    try:
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) != identity:
            return
        os.unlink(path)
    except OSError:
        return


def write_private_json(path: str, value: dict) -> None:
    """Create one new 0600 JSON file, never replacing an existing path."""

    enable_no_hydration()
    path = _path(path)
    if not isinstance(value, dict):
        _fail("private JSON value must be an object")
    data = _canonical_json(value)
    _private_ancestor_check(path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        _fail("safe file creation unavailable")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
    file_descriptor: int | None = None
    created_identity: tuple[int, int] | None = None
    try:
        file_descriptor = os.open(path, flags, 0o600)
        created = os.fstat(file_descriptor)
        created_identity = (created.st_dev, created.st_ino)
        os.fchmod(file_descriptor, 0o600)
        _private_file_metadata(file_descriptor)
        view = memoryview(data)
        while view:
            written = os.write(file_descriptor, view)
            if not isinstance(written, int) or written <= 0:
                _fail("private JSON write incomplete")
            view = view[written:]
        os.fsync(file_descriptor)
    except PlanError:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
            file_descriptor = None
        _cleanup_private_file(path, created_identity)
        raise
    except FileExistsError:
        raise PlanError("private output already exists") from None
    except OSError:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
            file_descriptor = None
        _cleanup_private_file(path, created_identity)
        raise PlanError("private JSON write failed") from None
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass


def load_plan(path: str) -> dict[str, Any]:
    """Read and validate one private plan file using duplicate-key rejection."""

    enable_no_hydration()
    path = _path(path)
    _private_ancestor_check(path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        _fail("safe file opening unavailable")
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | nofollow)
        before = _private_file_metadata(descriptor)
        if before.st_size > 4 * 1024 * 1024:
            _fail("private plan is too large")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not isinstance(chunk, bytes):
                _fail("private plan read failed")
            if not chunk:
                break
            total += len(chunk)
            if total > 4 * 1024 * 1024:
                _fail("private plan is too large")
            chunks.append(chunk)
        after = _private_file_metadata(descriptor)
        if _metadata(before) != _metadata(after):
            _fail("private plan changed during read")
        try:
            raw = b"".join(chunks).decode("utf-8")
            parsed = json.loads(
                raw,
                object_pairs_hook=_duplicate_rejecting_object,
                parse_constant=_reject_json_constant,
            )
        except (
            _DuplicateKey,
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
        ):
            _fail("invalid private plan JSON")
        return validate_plan(parsed)
    except PlanError:
        raise
    except FileNotFoundError:
        raise PlanError("private plan is unavailable") from None
    except OSError:
        raise PlanError("private plan is unavailable") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


__all__ = [
    "PlanError",
    "capture_target",
    "make_plan",
    "validate_plan",
    "plan_digest",
    "target_unchanged",
    "load_plan",
    "write_private_json",
    "enable_no_hydration",
    "SF_DATALESS",
]
