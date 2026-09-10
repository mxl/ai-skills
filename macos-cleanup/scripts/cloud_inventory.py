#!/usr/bin/env python3
"""Guarded, metadata-only inventory for an explicitly supplied cloud root.

This module intentionally has no provider client and no eviction path. It only
walks the local namespace after the native no-hydration guard has been
installed. The public result never contains the supplied path or a child name;
item identifiers are per-run HMACs of relative names.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import math
import os
import secrets
import stat
import sys
import time
import unicodedata
from collections import deque
from typing import Any, Iterable

try:
    from audit_runtime import GuardUnavailable, enable_no_hydration
except ImportError:  # pragma: no cover - only relevant to unusual embedding
    class GuardUnavailable(RuntimeError):
        """Fallback type used when the guarded runtime is not importable."""

    def enable_no_hydration() -> None:
        raise GuardUnavailable("no-hydration guard unavailable")


SCHEMA_VERSION = "1"
MODULE = "cloud"
RECORD_TYPE = "cloud_container_inventory"
PUBLIC_ROOT_LABEL = "redacted-cloud-root"
PUBLIC_DOMAIN_LABEL = "unknown-provider"

# SF_DATALESS is exposed by some Darwin Python builds, but not all Python/stat
# combinations. The fallback is the Darwin value and is useful for tests.
SF_DATALESS = int(getattr(stat, "SF_DATALESS", 0x40000000))

MAX_ENTRIES = 10_000
MAX_DEPTH = 64
MAX_TIMEOUT_SECONDS = 60.0

# These are the only root-owned macOS namespace aliases admitted by this
# adapter. Their physical endpoints are validated before following them once.
_SYSTEM_ALIAS_TARGETS = {
    "/var": "/private/var",
    "/tmp": "/private/tmp",
    "/etc": "/private/etc",
}


def _base_result(*, status: str, enumeration_status: str) -> dict[str, Any]:
    """Create a result without consulting the filesystem."""

    counts = {
        "entries": 0,
        "entries_seen": 0,
        "items": 0,
        "files": 0,
        "directories": 0,
        "denied": 0,
        "skipped": 0,
        "dataless": 0,
        "symlink": 0,
        "cross_mount": 0,
        "depth_limited": 0,
        "hardlinks": 0,
    }
    totals = {
        "allocated_bytes": None,
        "logical_bytes": None,
        "allocated_lower_bound_bytes": None,
        "logical_lower_bound_bytes": None,
        "allocated_measurement": "unknown",
        "logical_measurement": "unknown",
        "measurement_status": "unknown",
        "file_allocated_bytes": None,
        "file_logical_bytes": None,
        "directory_allocated_bytes": None,
        "directory_logical_bytes": None,
        "exclusive_reclaimable_bytes": None,
        "hardlink_deduplicated": "unknown",
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": RECORD_TYPE,
        "module": MODULE,
        # Readiness for a later, separately approved eviction is deliberately
        # never COMPLETE. enumeration_status reports only enumeration.
        "status": status,
        "enumeration_status": enumeration_status,
        "root_label": PUBLIC_ROOT_LABEL,
        "domain_label": PUBLIC_DOMAIN_LABEL,
        "provider_kind": "unknown-file-provider",
        "items": [],
        "counts": counts,
        "totals": totals,
        "allocated_bytes": None,
        "logical_bytes": None,
        "allocated_lower_bound_bytes": None,
        "logical_lower_bound_bytes": None,
        "allocated_measurement": "unknown",
        "logical_measurement": "unknown",
        "measurement_status": "unknown",
        "sync_state": "unknown",
        "pinned": "unknown",
        "eviction": "unverified",
        "eviction_method": "unverified",
        "actionable": False,
        "reasons": [],
        "evidence": ["guarded local metadata enumeration"],
        "path": None,
        "sensitive": True,
    }


def _reason(result: dict[str, Any], text: str) -> None:
    reasons = result["reasons"]
    if text not in reasons:
        reasons.append(text)


def _invalid_result(text: str) -> dict[str, Any]:
    result = _base_result(status="PARTIAL", enumeration_status="NOT-RUN")
    _reason(result, text)
    return result


def _valid_limits(
    max_entries: object, max_depth: object, timeout: object
) -> tuple[int, int, float] | None:
    """Validate finite traversal limits against explicit safety caps."""

    if isinstance(max_entries, bool) or not isinstance(max_entries, int):
        return None
    if isinstance(max_depth, bool) or not isinstance(max_depth, int):
        return None
    if not 0 <= max_entries <= MAX_ENTRIES:
        return None
    if not 0 <= max_depth <= MAX_DEPTH:
        return None
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return None
    try:
        timeout_value = float(timeout)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(timeout_value) or not 0 < timeout_value <= MAX_TIMEOUT_SECONDS:
        return None
    return max_entries, max_depth, timeout_value


def _normal_absolute_root(root: object) -> str | None:
    """Validate a lexical path before canonicalizing a harmless final slash."""

    if not isinstance(root, str) or not root or "\x00" in root:
        return None
    if not os.path.isabs(root) or root.startswith(os.sep * 2):
        return None
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in root):
        return None

    trailing_separators = len(root) - len(root.rstrip(os.sep))
    if trailing_separators > 1:
        return None
    candidate = root.rstrip(os.sep)
    if not candidate or candidate == os.path.sep:
        return None
    # Do not let normpath silently alter the requested scope. Repeated
    # separators are ambiguous except for the harmless trailing separator.
    if os.sep * 2 in candidate:
        return None
    components = candidate.split(os.sep)
    if any(component in {".", ".."} for component in components):
        return None

    normalized = os.path.normpath(candidate)
    return normalized if normalized == candidate else None


def _identity(metadata: Any) -> tuple[int, int] | None:
    try:
        device = int(metadata.st_dev)
        inode = int(metadata.st_ino)
    except (AttributeError, OSError, TypeError, ValueError, OverflowError):
        return None
    if device < 0 or inode < 0:
        return None
    return device, inode


def _mode(metadata: Any) -> int | None:
    try:
        return int(metadata.st_mode)
    except (AttributeError, OSError, TypeError, ValueError, OverflowError):
        return None


def _blocks(metadata: Any) -> int | None:
    try:
        value = int(metadata.st_blocks)
    except (AttributeError, OSError, TypeError, ValueError, OverflowError):
        return None
    return value * 512 if value >= 0 else None


def _logical_size(metadata: Any) -> int | None:
    try:
        value = int(metadata.st_size)
    except (AttributeError, OSError, TypeError, ValueError, OverflowError):
        return None
    return value if value >= 0 else None


def _flags(metadata: Any) -> int | None:
    try:
        value = int(getattr(metadata, "st_flags"))
    except (AttributeError, OSError, TypeError, ValueError, OverflowError):
        return None
    return value if value >= 0 else None


def _is_dataless(metadata: Any) -> bool | None:
    flags = _flags(metadata)
    if flags is None:
        return None
    return bool(flags & SF_DATALESS)


def _is_system_alias(path: str, metadata: Any) -> bool:
    """Allow only validated root-owned /var, /tmp, or /etc aliases."""

    physical = _SYSTEM_ALIAS_TARGETS.get(path)
    if physical is None:
        return False
    try:
        if int(metadata.st_uid) != 0 or not stat.S_ISLNK(_mode(metadata) or 0):
            return False
        target_metadata = os.stat(path)
        physical_metadata = os.lstat(physical)
        if stat.S_ISLNK(_mode(physical_metadata) or 0):
            physical_metadata = os.stat(physical)
        return (
            int(physical_metadata.st_uid) == 0
            and _identity(target_metadata) is not None
            and _identity(target_metadata) == _identity(physical_metadata)
        )
    except (AttributeError, OSError, TypeError, ValueError, OverflowError):
        return False


def _absolute_components(path: str) -> Iterable[str]:
    # ``path`` is normalized and absolute. This is lexical path construction;
    # it does not resolve links or consult the filesystem.
    current = os.path.sep
    yield current
    for component in path.split(os.sep):
        if not component:
            continue
        current = os.path.join(current, component)
        yield current


def _is_permission_error(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "errno", None) in {
        errno.EACCES,
        errno.EPERM,
    }


def _capture_ancestry(
    root: str,
) -> tuple[dict[str, tuple[int, int] | None], str | None, int | None, Any | None]:
    """Capture identities and validate every lexical path component."""

    identities: dict[str, tuple[int, int] | None] = {}
    root_metadata: Any | None = None
    root_device: int | None = None

    for component in _absolute_components(root):
        try:
            metadata = os.lstat(component)
        except OSError as exc:
            kind = "denied" if _is_permission_error(exc) else "skipped"
            return identities, kind, None, None

        identities[component] = _identity(metadata)
        mode = _mode(metadata)
        if mode is None:
            return identities, "skipped", None, None
        if stat.S_ISLNK(mode) and not _is_system_alias(component, metadata):
            return identities, "symlink", None, None

    try:
        root_lstat = os.lstat(root)
    except OSError as exc:
        kind = "denied" if _is_permission_error(exc) else "skipped"
        return identities, kind, None, None

    root_mode = _mode(root_lstat)
    if root_mode is None:
        return identities, "skipped", None, None
    if stat.S_ISLNK(root_mode):
        # Known system aliases may be followed once for root inspection. Child
        # links are still lstat'ed and never followed.
        if not _is_system_alias(root, root_lstat):
            return identities, "symlink", None, None
        try:
            root_metadata = os.stat(root)
        except OSError as exc:
            kind = "denied" if _is_permission_error(exc) else "skipped"
            return identities, kind, None, None
    else:
        root_metadata = root_lstat

    try:
        root_device = int(root_metadata.st_dev)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return identities, "skipped", None, None
    return identities, None, root_device, root_metadata


def _record_id(secret: bytes, relative_parts: tuple[str, ...]) -> str:
    # The relative name is never returned. A per-run secret prevents dictionary
    # attacks and cross-run/name correlations; the secret is never published.
    relative = "/".join(relative_parts).encode("utf-8", "surrogatepass")
    message = b"cloud-item-v2\0" + relative
    digest = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return "item-" + digest[:32]


def _local_state(
    metadata: Any, kind: str, allocated: int | None
) -> tuple[str, list[str]]:
    dataless = _is_dataless(metadata)
    if dataless is True:
        return "placeholder", ["st_flags indicates dataless"]
    if dataless is None:
        return "unknown", ["dataless flag metadata unavailable"]
    if kind != "file":
        return "unknown", ["provider local state unavailable"]
    # Blocks prove that some local allocation exists, not that the file is
    # fully downloaded or synchronized. Zero blocks can also be sparse or
    # placeholder-like, so that case remains unknown.
    if allocated is not None and allocated > 0:
        return "local-allocation-present", [
            "local blocks present; provider state unavailable"
        ]
    if allocated == 0:
        return "unknown", ["no allocated blocks; sparse/placeholder ambiguous"]
    return "unknown", ["allocated size unavailable"]


def _item(
    secret: bytes, relative_parts: tuple[str, ...], metadata: Any, kind: str
) -> dict[str, Any]:
    allocated = _blocks(metadata)
    logical = _logical_size(metadata)
    local_state, state_evidence = _local_state(metadata, kind, allocated)
    return {
        "item_id": _record_id(secret, relative_parts),
        "kind": kind,
        "allocated_bytes": allocated,
        "logical_bytes": logical,
        "local_state": local_state,
        "sync_state": "unknown",
        "pinned": "unknown",
        "eviction": "unverified",
        "eviction_method": "unverified",
        "actionable": False,
        "shared_inode": False,
        "hardlink_state": "not-observed",
        "clone_state": "unknown",
        "exclusive_reclaim": "unknown",
        "exclusive_reclaimable_bytes": None,
        "evidence": ["lstat", "st_blocks", "st_size", *state_evidence],
        "sensitive": True,
    }


def _bump_error(result: dict[str, Any], kind: str) -> None:
    counts = result["counts"]
    if kind == "denied":
        counts["denied"] += 1
    else:
        counts["skipped"] += 1


def _mark_partial(result: dict[str, Any], reason: str) -> None:
    result["enumeration_status"] = "PARTIAL"
    _reason(result, reason)


def _time_exceeded(deadline: float) -> bool:
    try:
        return time.monotonic() >= deadline
    except (OverflowError, ValueError):
        return True


def _same_identity(left: Any, right: Any) -> bool:
    left_id = _identity(left)
    right_id = _identity(right)
    return left_id is not None and left_id == right_id


def _root_is_alias_of(root_metadata: Any, candidate: str) -> tuple[bool, str | None]:
    """Compare identity with a sensitive alias without exposing its path."""

    try:
        candidate_metadata = os.lstat(candidate)
    except OSError as exc:
        return False, "denied" if _is_permission_error(exc) else "skipped"
    candidate_mode = _mode(candidate_metadata)
    if candidate_mode is None:
        return False, "skipped"
    if stat.S_ISLNK(candidate_mode):
        try:
            candidate_metadata = os.stat(candidate)
        except OSError as exc:
            return False, "denied" if _is_permission_error(exc) else "skipped"
    return _same_identity(root_metadata, candidate_metadata), None


def _final_root_metadata(root: str) -> Any | None:
    try:
        metadata = os.lstat(root)
    except OSError:
        return None
    mode = _mode(metadata)
    if mode is not None and stat.S_ISLNK(mode):
        if not _is_system_alias(root, metadata):
            return None
        try:
            return os.stat(root)
        except OSError:
            return None
    return metadata


def _final_ancestry_matches(
    root: str,
    initial: dict[str, tuple[int, int] | None],
    initial_root_metadata: Any,
) -> tuple[bool, str | None]:
    final, failure, _, _ = _capture_ancestry(root)
    if failure is not None:
        return False, failure
    if final != initial:
        return False, "changed"
    final_root = _final_root_metadata(root)
    if final_root is None:
        return False, "skipped"
    if not _same_identity(initial_root_metadata, final_root):
        return False, "changed"
    return True, None


def _update_totals(result: dict[str, Any]) -> None:
    items = result["items"]
    # An empty result after an interrupted/unsafe walk is unknown, not a
    # measured empty directory. Keep the base None values in that case.
    if not items and result["enumeration_status"] != "COMPLETE":
        return

    file_groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    unknown_identity_files: list[dict[str, Any]] = []
    directories: list[tuple[dict[str, Any], tuple[int, int] | None]] = []
    for item in items:
        identity = item.pop("_identity", None)
        if item["kind"] == "file":
            if identity is None:
                unknown_identity_files.append(item)
            else:
                file_groups.setdefault(identity, []).append(item)
        else:
            directories.append((item, identity))

    file_allocated = 0
    file_logical = 0
    directory_allocated = 0
    directory_logical = 0
    allocated_known = 0
    logical_known = 0
    allocated_metadata_complete = not unknown_identity_files
    logical_metadata_complete = not unknown_identity_files

    def known_value(group: list[dict[str, Any]], field: str) -> int | None:
        for grouped_item in group:
            value = grouped_item[field]
            if value is not None:
                return value
        return None

    # One representative per inode prevents hardlinks from inflating totals.
    # A missing inode identity is not safely countable because it could alias
    # another file, so its bytes are not presented as a lower bound.
    for group in file_groups.values():
        allocated = known_value(group, "allocated_bytes")
        logical = known_value(group, "logical_bytes")
        if allocated is None:
            allocated_metadata_complete = False
        else:
            file_allocated += allocated
            allocated_known += 1
        if logical is None:
            logical_metadata_complete = False
        else:
            file_logical += logical
            logical_known += 1

    for item, identity in directories:
        allocated = item["allocated_bytes"]
        logical = item["logical_bytes"]
        if identity is None:
            allocated_metadata_complete = False
            logical_metadata_complete = False
            continue
        if allocated is None:
            allocated_metadata_complete = False
        else:
            directory_allocated += allocated
            allocated_known += 1
        if logical is None:
            logical_metadata_complete = False
        else:
            directory_logical += logical
            logical_known += 1

    enumeration_complete = result["enumeration_status"] == "COMPLETE"

    def measurement_state(metadata_complete: bool, known_count: int) -> str:
        if enumeration_complete and metadata_complete:
            return "complete"
        return "lower-bound" if known_count else "unknown"

    allocated_state = measurement_state(allocated_metadata_complete, allocated_known)
    logical_state = measurement_state(logical_metadata_complete, logical_known)
    overall_state = (
        "complete"
        if allocated_state == logical_state == "complete"
        else "lower-bound"
        if "lower-bound" in {allocated_state, logical_state}
        else "unknown"
    )

    totals = result["totals"]
    totals["allocated_bytes"] = (
        file_allocated + directory_allocated
        if allocated_state == "complete"
        else None
    )
    totals["logical_bytes"] = (
        file_logical + directory_logical if logical_state == "complete" else None
    )
    totals["allocated_lower_bound_bytes"] = (
        file_allocated + directory_allocated
        if allocated_known or allocated_state == "complete"
        else None
    )
    totals["logical_lower_bound_bytes"] = (
        file_logical + directory_logical
        if logical_known or logical_state == "complete"
        else None
    )
    totals["allocated_measurement"] = allocated_state
    totals["logical_measurement"] = logical_state
    totals["measurement_status"] = overall_state
    totals["file_allocated_bytes"] = file_allocated if allocated_known else None
    totals["file_logical_bytes"] = file_logical if logical_known else None
    totals["directory_allocated_bytes"] = (
        directory_allocated if allocated_known else None
    )
    totals["directory_logical_bytes"] = directory_logical if logical_known else None
    totals["hardlink_deduplicated"] = (
        True if not unknown_identity_files else "unknown"
    )

    result["allocated_bytes"] = totals["allocated_bytes"]
    result["logical_bytes"] = totals["logical_bytes"]
    result["allocated_lower_bound_bytes"] = totals["allocated_lower_bound_bytes"]
    result["logical_lower_bound_bytes"] = totals["logical_lower_bound_bytes"]
    result["allocated_measurement"] = allocated_state
    result["logical_measurement"] = logical_state
    result["measurement_status"] = overall_state


def inventory(
    root: str,
    *,
    max_entries: int = 1000,
    max_depth: int = 3,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Return a bounded guarded metadata inventory for ``root``.

    The function never runs provider commands, opens files, reads xattrs, or
    follows child symlinks. A normal guarded inventory is still returned with
    ``status == "PARTIAL"`` because no sync/eviction evidence or approval is
    established by this metadata-only operation.
    """

    # This must remain the first operation that could lead to filesystem I/O.
    # Do not validate HOME aliases or capture ancestry before the guard succeeds.
    try:
        enable_no_hydration()
    except Exception:
        result = _base_result(status="UNAVAILABLE", enumeration_status="UNAVAILABLE")
        _reason(result, "no-hydration guard unavailable")
        return result

    limits = _valid_limits(max_entries, max_depth, timeout)
    if limits is None:
        return _invalid_result("invalid finite traversal limits")
    max_entries_value, max_depth_value, timeout_value = limits

    normalized_root = _normal_absolute_root(root)
    if normalized_root is None:
        return _invalid_result("root must be an absolute non-root path")

    result = _base_result(status="PARTIAL", enumeration_status="COMPLETE")
    home = os.environ.get("HOME")
    normalized_home = None
    if (
        isinstance(home, str)
        and home
        and "\x00" not in home
        and os.path.isabs(home)
    ):
        normalized_home = os.path.normpath(home)
    if normalized_home and normalized_root == normalized_home:
        return _invalid_result("root aliases the current home directory")

    try:
        item_secret = secrets.token_bytes(32)
    except Exception:
        result["status"] = "UNAVAILABLE"
        result["enumeration_status"] = "UNAVAILABLE"
        _reason(result, "per-run item identifier secret unavailable")
        return result

    try:
        deadline = time.monotonic() + timeout_value
    except (OverflowError, ValueError):
        return _invalid_result("invalid finite traversal limits")

    ancestry, failure, root_device, root_metadata = _capture_ancestry(normalized_root)
    if failure is not None or root_metadata is None or root_device is None:
        if failure in {"denied", "skipped"}:
            _bump_error(result, failure)
        elif failure == "symlink":
            result["counts"]["symlink"] += 1
            result["counts"]["skipped"] += 1
        else:
            _bump_error(result, "skipped")
        _mark_partial(result, "root or an ancestor was unavailable or unsafe")
        return result

    root_alias, alias_error = _root_is_alias_of(root_metadata, os.sep)
    if alias_error is not None:
        _bump_error(result, alias_error)
        _mark_partial(result, "filesystem-root alias could not be checked")
        return result
    if root_alias:
        _mark_partial(result, "root aliases the filesystem root")
        result["counts"]["skipped"] += 1
        return result
    if normalized_home:
        home_alias, alias_error = _root_is_alias_of(root_metadata, normalized_home)
        if alias_error is not None:
            _bump_error(result, alias_error)
            _mark_partial(result, "home alias could not be checked")
            return result
        if home_alias:
            _mark_partial(result, "root aliases the current home directory")
            result["counts"]["skipped"] += 1
            return result

    root_mode = _mode(root_metadata)
    if root_mode is None or not stat.S_ISDIR(root_mode):
        _mark_partial(result, "root is not an accessible directory")
        result["counts"]["skipped"] += 1
        return result

    root_dataless = _is_dataless(root_metadata)
    if root_dataless is True:
        result["counts"]["dataless"] += 1
        result["counts"]["skipped"] += 1
        _mark_partial(result, "dataless root directory was not enumerated")
        return result
    if root_dataless is None:
        result["counts"]["skipped"] += 1
        _mark_partial(result, "root dataless flag metadata unavailable")
        return result

    if max_depth_value == 0:
        result["counts"]["depth_limited"] += 1
        result["counts"]["skipped"] += 1
        _mark_partial(result, "maximum traversal depth reached")
        return result

    root_identity = _identity(root_metadata)
    if root_identity is None:
        result["counts"]["skipped"] += 1
        _mark_partial(result, "root identity unavailable")
        return result
    if max_entries_value == 0:
        result["counts"]["skipped"] += 1
        _mark_partial(result, "maximum entry limit reached")
        return result

    # Entries are (directory path, relative path components, depth, identity).
    # The traversal is iterative so a deep tree cannot consume the call stack.
    pending: deque[tuple[str, tuple[str, ...], int, tuple[int, int]]] = deque(
        [(normalized_root, tuple(), 0, root_identity)]
    )
    entries_seen = 0
    timed_out = False
    limit_reached = False

    while pending:
        if _time_exceeded(deadline):
            timed_out = True
            _mark_partial(result, "enumeration time limit reached")
            break
        directory, parent_parts, depth, recorded_identity = pending.popleft()
        try:
            scan_metadata = os.lstat(directory)
        except OSError as exc:
            _bump_error(result, "denied" if _is_permission_error(exc) else "skipped")
            _mark_partial(result, "a queued directory changed or was unavailable")
            continue

        scan_mode = _mode(scan_metadata)
        if scan_mode is None:
            _bump_error(result, "skipped")
            _mark_partial(result, "a queued directory had unusable metadata")
            continue
        if stat.S_ISLNK(scan_mode):
            if not _is_system_alias(directory, scan_metadata):
                result["counts"]["symlink"] += 1
                result["counts"]["skipped"] += 1
                _mark_partial(result, "queued directory became symlinked")
                continue
            try:
                scan_metadata = os.stat(directory)
            except OSError as exc:
                _bump_error(result, "denied" if _is_permission_error(exc) else "skipped")
                _mark_partial(result, "a system alias could not be inspected")
                continue
            scan_mode = _mode(scan_metadata)
        if scan_mode is None or not stat.S_ISDIR(scan_mode):
            _bump_error(result, "skipped")
            _mark_partial(result, "a queued directory changed type")
            continue
        scan_identity = _identity(scan_metadata)
        if scan_identity is None:
            _bump_error(result, "skipped")
            _mark_partial(result, "a queued directory identity unavailable")
            continue
        if scan_identity != recorded_identity:
            result["counts"]["skipped"] += 1
            _mark_partial(result, "queued directory changed before scan")
            continue
        try:
            scan_device = int(scan_metadata.st_dev)
        except (AttributeError, TypeError, ValueError, OverflowError):
            _bump_error(result, "skipped")
            _mark_partial(result, "a queued directory had unusable device metadata")
            continue
        if scan_device != root_device:
            result["counts"]["cross_mount"] += 1
            result["counts"]["skipped"] += 1
            _mark_partial(result, "queued directory crossed a mount")
            continue
        scan_dataless = _is_dataless(scan_metadata)
        if scan_dataless is True:
            result["counts"]["dataless"] += 1
            result["counts"]["skipped"] += 1
            _mark_partial(result, "queued dataless directory was not enumerated")
            continue
        if scan_dataless is None:
            result["counts"]["skipped"] += 1
            _mark_partial(result, "queued directory dataless flag metadata unavailable")
            continue

        try:
            iterator = os.scandir(directory)
        except OSError as exc:
            _bump_error(result, "denied" if _is_permission_error(exc) else "skipped")
            _mark_partial(result, "a directory could not be enumerated")
            continue

        try:
            while True:
                if _time_exceeded(deadline):
                    timed_out = True
                    _mark_partial(result, "enumeration time limit reached")
                    break
                if entries_seen >= max_entries_value:
                    limit_reached = True
                    _mark_partial(result, "maximum entry limit reached")
                    break
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                except OSError as exc:
                    _bump_error(result, "denied" if _is_permission_error(exc) else "skipped")
                    _mark_partial(result, "directory enumeration was interrupted")
                    break

                entries_seen += 1
                try:
                    name = entry.name
                    child_path = os.path.join(directory, name)
                    metadata = os.lstat(child_path)
                except OSError as exc:
                    _bump_error(result, "denied" if _is_permission_error(exc) else "skipped")
                    _mark_partial(result, "an entry changed or could not be inspected")
                    continue

                if _time_exceeded(deadline):
                    timed_out = True
                    _mark_partial(result, "enumeration time limit reached")
                    break

                mode = _mode(metadata)
                if mode is None:
                    _bump_error(result, "skipped")
                    _mark_partial(result, "an entry had unusable metadata")
                    continue

                dataless = _is_dataless(metadata)
                if dataless is True:
                    result["counts"]["dataless"] += 1
                elif dataless is None:
                    _mark_partial(result, "entry dataless flag metadata unavailable")

                if stat.S_ISLNK(mode):
                    result["counts"]["symlink"] += 1
                    result["counts"]["skipped"] += 1
                    _mark_partial(result, "symlink child was excluded")
                    continue

                try:
                    child_device = int(metadata.st_dev)
                except (AttributeError, TypeError, ValueError, OverflowError):
                    _bump_error(result, "skipped")
                    _mark_partial(result, "an entry had unusable device metadata")
                    continue
                if child_device != root_device:
                    result["counts"]["cross_mount"] += 1
                    result["counts"]["skipped"] += 1
                    _mark_partial(result, "cross-mount child was excluded")
                    continue

                child_parts = parent_parts + (name,)
                if stat.S_ISDIR(mode) and dataless is True:
                    result["counts"]["skipped"] += 1
                    _mark_partial(result, "dataless directory was not enumerated")
                    continue
                if stat.S_ISDIR(mode) and dataless is None:
                    result["counts"]["skipped"] += 1
                    _mark_partial(result, "dataless directory flag metadata unavailable")
                    continue

                if stat.S_ISREG(mode):
                    item = _item(item_secret, child_parts, metadata, "file")
                    item_identity = _identity(metadata)
                    item["_identity"] = item_identity
                    result["items"].append(item)
                    result["counts"]["files"] += 1
                    if (
                        item_identity is None
                        or item["allocated_bytes"] is None
                        or item["logical_bytes"] is None
                    ):
                        _mark_partial(result, "file metadata was incomplete")
                    continue

                if stat.S_ISDIR(mode):
                    item = _item(item_secret, child_parts, metadata, "directory")
                    item_identity = _identity(metadata)
                    item["_identity"] = item_identity
                    result["items"].append(item)
                    result["counts"]["directories"] += 1
                    if (
                        item_identity is None
                        or item["allocated_bytes"] is None
                        or item["logical_bytes"] is None
                    ):
                        _mark_partial(result, "directory metadata was incomplete")
                    directory_identity = item_identity
                    if depth + 1 < max_depth_value and directory_identity is not None:
                        pending.append((child_path, child_parts, depth + 1, directory_identity))
                    elif depth + 1 < max_depth_value:
                        result["counts"]["skipped"] += 1
                        _mark_partial(result, "queued directory identity unavailable")
                    else:
                        result["counts"]["depth_limited"] += 1
                        _mark_partial(result, "maximum traversal depth reached")
                    continue

                # Devices, sockets and other non-content entries are not useful
                # cloud-file records and are not followed.
                result["counts"]["skipped"] += 1
                _mark_partial(result, "unsupported entry type was excluded")
        finally:
            close_iterator = getattr(iterator, "close", None)
            if close_iterator is not None:
                try:
                    close_iterator()
                except OSError as exc:
                    _bump_error(result, "denied" if _is_permission_error(exc) else "skipped")
                    _mark_partial(result, "directory enumeration could not be closed")

        if timed_out or limit_reached:
            break

    result["counts"]["entries"] = entries_seen
    result["counts"]["entries_seen"] = entries_seen
    result["counts"]["items"] = len(result["items"])

    # Determine shared inodes only after all reachable entries are known. The
    # identity is removed from each public item by _update_totals below.
    inode_to_items: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for item in result["items"]:
        if item["kind"] != "file":
            continue
        identity = item.get("_identity")
        if identity is not None:
            inode_to_items.setdefault(identity, []).append(item)
    for shared_items in inode_to_items.values():
        if len(shared_items) > 1:
            result["counts"]["hardlinks"] += len(shared_items)
            for item in shared_items:
                item["shared_inode"] = True
                item["hardlink_state"] = "shared-inode"

    ancestry_matches, final_failure = _final_ancestry_matches(
        normalized_root, ancestry, root_metadata
    )
    if not ancestry_matches:
        if final_failure in {"denied", "skipped"}:
            _bump_error(result, final_failure)
            _mark_partial(result, "final target or ancestry check was unavailable")
        elif final_failure == "symlink":
            result["counts"]["symlink"] += 1
            result["counts"]["skipped"] += 1
            _mark_partial(result, "final target or ancestry became symlinked")
        else:
            _mark_partial(result, "target or an ancestor changed during enumeration")

    _update_totals(result)
    # All normal guarded inventories remain PARTIAL for eviction readiness,
    # including one whose enumeration_status is COMPLETE.
    result["status"] = "PARTIAL"
    result["actionable"] = False
    return result


if __name__ == "__main__":  # pragma: no cover - API is the supported entrypoint
    sys.stderr.write("cloud_inventory: use inventory(root, ...)\n")
    raise SystemExit(2)
