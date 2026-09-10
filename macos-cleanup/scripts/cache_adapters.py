#!/usr/bin/env python3
"""Trusted, read-only cache-owner discovery and command construction.

This module intentionally has a small allowlist.  Discovery may ask a known
owner for its version, configured cache directory, and help text, but it never
runs a cache mutation.  A plan is created only after the owner is known, idle,
and its relevant command features have been observed.

``cleanup_contract`` is imported lazily.  That keeps this module usable while
that dependency is being developed and prevents an import cycle between the
plan/identity implementation and the adapters.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any, Callable, Mapping

try:  # The tests import scripts as a plain directory, not as a package.
    import audit_runtime
except ImportError:  # pragma: no cover - useful when imported as a package.
    from . import audit_runtime  # type: ignore


Probe = Callable[..., Mapping[str, Any]]

PROBE_TIMEOUT_SECONDS = 10.0
PROBE_OUTPUT_BYTES = 64 * 1024
UV_LOCK_TIMEOUT_SECONDS = 30

UV_ACTION = "uv-cache-prune"
GO_ACTION = "go-build-cache-clean"

# The uv documentation says UV_LOCK_TIMEOUT was added in 0.9.4.  The upper
# bound is the latest stable release reviewed for this adapter on 2026-09-10.
# Stable versions outside this window, prereleases, and future versions remain
# review-only until their lock semantics are reviewed again.
_UV_MIN_SUPPORTED = (0, 9, 4)
_UV_MAX_SUPPORTED = (0, 12, 12)
_GO_MIN_SUPPORTED = (1, 20, 0)
_GO_MAX_SUPPORTED = (1, 27, 1)
_UV_EXECUTION_LINK_MODE = "copy"

_SAFE_ENV_KEYS = (
    "PATH",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
)

_OWNER_COMMANDS = {
    "uv": "uv",
    "go": "go",
    "npm": "npm",
    "pnpm": "pnpm",
    "brew": "brew",
    "homebrew": "brew",
}
_SUPPORTED_ACTIONS = {"uv": UV_ACTION, "go": GO_ACTION}
_REVIEW_ONLY = {"npm", "pnpm", "brew", "homebrew"}

_WRITERS = {
    "uv": ("uv", "uvx"),
    # Exact process names only.  The editor controllers are conservative
    # stop-signals; no command-line matching or process termination is used.
    "go": (
        "go",
        "gopls",
        "Code Helper",
        "Cursor Helper",
        "Zed",
        "GoLand",
        "IntelliJ IDEA",
        "Windsurf",
    ),
}

# These are release formats, rather than a claim that every future release
# has the same behavior.  Discovery also verifies the relevant help feature.
_UV_VERSION_RE = re.compile(
    r"^(?:uv\s+)?(?P<version>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)(?:\s.*)?$"
)
_GO_VERSION_RE = re.compile(
    r"^go version (?P<version>go\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?)\s+.+$"
)
_BREW_VERSION_RE = re.compile(
    r"^Homebrew\s+\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?(?:\s.*)?$"
)
_GENERIC_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# macOS has root-owned aliases for these locations.  They are safe aliases,
# unlike an arbitrary symlink in a user-configured cache path.
_ALIAS_PREFIXES = (
    ("/var", "/private/var"),
    ("/tmp", "/private/tmp"),
    ("/etc", "/private/etc"),
)

_RISKS = {
    "uv": (
        "Owner-managed dynamic cache scope may remove shared cache entries; "
        "re-download or rebuild may be required."
    ),
    "go": (
        "Only the Go build cache is in scope; build artifacts are regenerated, "
        "while module and fuzz caches are not targeted."
    ),
    "npm": (
        "npm cache ownership and cleanup semantics are review-only in this "
        "adapter; registry access may be required to recover entries."
    ),
    "pnpm": (
        "pnpm uses a shared store; ownership and project activity require a "
        "separate review."
    ),
    "brew": (
        "Homebrew cache cleanup is review-only here; package and cask state "
        "must not be inferred from a cache directory."
    ),
    "homebrew": (
        "Homebrew cache cleanup is review-only here; package and cask state "
        "must not be inferred from a cache directory."
    ),
}


class AdapterError(ValueError):
    """A plan or adapter input cannot be safely handled by this module."""


class AdapterCancelled(KeyboardInterrupt):
    """A guarded owner probe was cancelled and must reach CLI exit handling."""


class _GuardLost(RuntimeError):
    """The native no-hydration guard was lost during discovery."""


class _ProbeState:
    __slots__ = ("returncode", "stdout", "stderr", "outcome")

    def __init__(
        self,
        returncode: int | None,
        stdout: str,
        stderr: str,
        outcome: str,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.outcome = outcome

    @property
    def finished(self) -> bool:
        return self.outcome == "finished" and self.returncode is not None


def _safe_text(value: object, *, limit: int = 256) -> str:
    """Return bounded diagnostic text without preserving command output."""

    if not isinstance(value, str):
        return "unavailable"
    value = _CONTROL_RE.sub(" ", value).strip()
    if len(value) > limit:
        value = value[:limit]
    return value or "unavailable"


def _generic_probe_error(operation: str, state: _ProbeState) -> str:
    operation = _safe_text(operation, limit=64)
    if state.outcome == "cancelled":
        return f"{operation} probe was cancelled"
    if state.outcome == "guard-unavailable":
        return f"{operation} probe was blocked because the no-hydration guard was unavailable"
    if state.outcome == "timeout":
        return f"{operation} probe timed out"
    if state.outcome == "output-limit":
        return f"{operation} probe exceeded its output bound"
    if state.outcome != "finished":
        return f"{operation} probe did not finish"
    return f"{operation} probe returned a non-zero status"


def _decode_output(value: object) -> str:
    if isinstance(value, bytes):
        return value[:PROBE_OUTPUT_BYTES].decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value[:PROBE_OUTPUT_BYTES]
    return ""


def _normalise_probe_result(value: object) -> _ProbeState:
    if not isinstance(value, Mapping):
        return _ProbeState(None, "", "", "spawn-error")

    returncode = value.get("returncode")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        returncode = None
    outcome = value.get("outcome")
    if not isinstance(outcome, str):
        outcome = "spawn-error"
    return _ProbeState(
        returncode,
        _decode_output(value.get("stdout")),
        _decode_output(value.get("stderr")),
        _safe_text(outcome, limit=64),
    )


def _default_probe(argv: list[str], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run one bounded read-only probe through the native runtime guard.

    The guard is checked before creating the temporary directory. The runtime
    owns the child lifecycle and writes output to private files. Those files
    are read only long enough to parse the answer and are then removed. In
    particular, stderr is never placed in adapter evidence.
    """

    audit_runtime.enable_no_hydration()
    with tempfile.TemporaryDirectory(prefix="cache-adapter-probe-") as directory:
        root = Path(directory)
        stdout_path = root / "stdout"
        stderr_path = root / "stderr"
        result = audit_runtime.run_command(
            list(argv),
            timeout=PROBE_TIMEOUT_SECONDS,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            env=env,
            max_output_bytes=PROBE_OUTPUT_BYTES,
            guarded=True,
        )
        try:
            stdout = stdout_path.read_bytes()[:PROBE_OUTPUT_BYTES]
        except OSError:
            stdout = b""
        try:
            stderr = stderr_path.read_bytes()[:PROBE_OUTPUT_BYTES]
        except OSError:
            stderr = b""

    # stderr is transient and bounded.  The caller receives it only so it can
    # make an internal success/error decision; no public result copies it.
    return {
        "returncode": result.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "outcome": result.outcome,
    }


def _call_probe(probe: Probe | None, argv: list[str], *, env: dict[str, str] | None) -> _ProbeState:
    callback = _default_probe if probe is None else probe
    try:
        result = callback(argv, env=env)
    except audit_runtime.GuardUnavailable as exc:
        raise _GuardLost() from exc
    except Exception:
        # A malformed/injected probe is evidence failure, not owner idleness.
        # Do not catch BaseException: KeyboardInterrupt and cancellation must
        # propagate to the caller instead of being masked as review evidence.
        return _ProbeState(None, "", "", "spawn-error")
    state = _normalise_probe_result(result)
    if state.outcome == "cancelled":
        raise AdapterCancelled()
    if state.outcome == "guard-unavailable":
        raise _GuardLost()
    return state


def _probe_env(adapter: str) -> dict[str, str] | None:
    """Preserve owner configuration while disabling incidental maintenance."""

    if adapter not in {"brew", "homebrew", "go"}:
        return None
    environment = dict(os.environ)
    if adapter in {"brew", "homebrew"}:
        environment.update(
            {
                "HOMEBREW_NO_AUTO_UPDATE": "1",
                "HOMEBREW_NO_INSTALL_CLEANUP": "1",
                "HOMEBREW_NO_AUTOREMOVE": "1",
            }
        )
    else:
        # Avoid an automatic toolchain download while asking Go for metadata.
        environment["GOTOOLCHAIN"] = "local"
    return environment


def _canonical_alias(path: str) -> str:
    path = os.path.normpath(path)
    for alias, physical in _ALIAS_PREFIXES:
        if path == alias:
            return physical
        if path.startswith(alias + os.sep):
            return physical + path[len(alias) :]
    return path


def _has_control_or_dot_component(path: str) -> bool:
    if "\x00" in path or _CONTROL_RE.search(path) is not None:
        return True
    return any(component in {".", "..", ""} for component in path.split(os.sep)[1:])


def _metadata_state(value: os.stat_result) -> dict[str, int]:
    """Match cleanup_contract's executable identity shape without importing it."""

    return {
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


def _capture_executable(
    found: str | None, *, trusted_root_paths: frozenset[str] = frozenset()
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    if not isinstance(found, str) or not found:
        return None, None, "owner executable is unavailable"
    if not os.path.isabs(found) or _has_control_or_dot_component(found):
        return None, None, "owner executable discovery returned an unsafe path"

    resolved = os.path.realpath(found)
    if not os.path.isabs(resolved) or _has_control_or_dot_component(resolved):
        return None, None, "owner executable could not be resolved safely"
    try:
        metadata = os.stat(resolved, follow_symlinks=False)
    except OSError:
        return None, None, "owner executable metadata is unavailable"
    if not stat.S_ISREG(metadata.st_mode) or not (metadata.st_mode & 0o111):
        return None, None, "owner executable is not a regular executable file"
    trusted_owner = metadata.st_uid == os.geteuid()
    trusted_system_owner = (
        metadata.st_uid == 0
        and resolved in trusted_root_paths
        and not (stat.S_IMODE(metadata.st_mode) & 0o022)
    )
    if not trusted_owner and not trusted_system_owner:
        return None, None, "owner executable ownership is not trusted"

    return resolved, {"version": 1, "identity": _metadata_state(metadata)}, None


def _capture_cache_root(path: str | None) -> tuple[str | None, dict[str, int] | None, str | None]:
    if not isinstance(path, str) or not path:
        return None, None, "configured cache path is unavailable"
    if not os.path.isabs(path) or _has_control_or_dot_component(path):
        return None, None, "configured cache path is unsafe"
    normalized = os.path.normpath(path)
    if normalized == "/":
        return None, None, "configured cache path is an unsafe root"

    resolved = os.path.realpath(normalized)
    if _canonical_alias(normalized) != _canonical_alias(resolved):
        return None, None, "configured cache path contains an unapproved symlink"
    if resolved == "/":
        return None, None, "configured cache path resolves to the filesystem root"

    try:
        metadata = os.stat(normalized, follow_symlinks=False)
    except OSError:
        return None, None, "configured cache directory is absent or inaccessible"
    if not stat.S_ISDIR(metadata.st_mode) or not os.access(normalized, os.R_OK | os.X_OK):
        return None, None, "configured cache directory is absent or inaccessible"

    home = os.environ.get("HOME")
    if isinstance(home, str) and home and os.path.isabs(home):
        try:
            if os.path.samefile(normalized, home):
                return None, None, "configured cache path is the home directory"
        except OSError:
            # An unavailable comparison is not evidence of safety.
            return None, None, "home directory identity could not be checked"

    state = {
        "st_dev": int(metadata.st_dev),
        "st_ino": int(metadata.st_ino),
        "st_mode": int(stat.S_IMODE(metadata.st_mode)),
        "st_uid": int(metadata.st_uid),
        "st_size": int(metadata.st_size),
        "st_mtime_ns": int(metadata.st_mtime_ns),
    }
    return normalized, state, None


def _single_line(value: str) -> str | None:
    # Owner CLIs normally terminate with one newline.  Strip only line
    # terminators so spaces in a configured path are not changed.
    value = value.rstrip("\r\n")
    if not value or "\n" in value or "\r" in value:
        return None
    if _CONTROL_RE.search(value) is not None:
        return None
    return value


_NUMERIC_RELEASE_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?$"
)


def _numeric_release(value: str) -> tuple[int, int, int] | None:
    match = _NUMERIC_RELEASE_RE.fullmatch(value)
    if match is None:
        return None
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch") or "0"),
    )


def _uv_release(version: object) -> tuple[int, int, int] | None:
    if not isinstance(version, str):
        return None
    match = _UV_VERSION_RE.fullmatch(version)
    if match is None:
        # The output may contain harmless build text after the release.
        match = _UV_VERSION_RE.match(version)
    if match is None:
        return None
    release = match.group("version")
    # The lock-timeout policy is for stable releases only.  Do not infer
    # prerelease/build semantics from a help page.
    if "-" in release or "+" in release:
        return None
    return _numeric_release(release)


def _go_release(version: object) -> tuple[int, int, int] | None:
    if not isinstance(version, str):
        return None
    match = _GO_VERSION_RE.fullmatch(version)
    if match is None:
        match = _GO_VERSION_RE.match(version)
    if match is None:
        return None
    release = match.group("version")
    if not release.startswith("go") or "-" in release or "+" in release:
        return None
    return _numeric_release(release[2:])


def _parse_version(adapter: str, stdout: str) -> str | None:
    value = _single_line(stdout)
    if value is None:
        return None
    if adapter == "uv":
        return value if _UV_VERSION_RE.match(value) else None
    if adapter == "go":
        return value if _GO_VERSION_RE.match(value) else None
    if adapter in {"brew", "homebrew"}:
        return value if _BREW_VERSION_RE.match(value) else None
    return value if _GENERIC_VERSION_RE.match(value) else None


def _version_is_known(adapter: str, version: object) -> bool:
    if adapter == "uv":
        release = _uv_release(version)
        return release is not None and _UV_MIN_SUPPORTED <= release <= _UV_MAX_SUPPORTED
    if adapter == "go":
        release = _go_release(version)
        return release is not None and _GO_MIN_SUPPORTED <= release <= _GO_MAX_SUPPORTED
    return isinstance(version, str) and _GENERIC_VERSION_RE.match(version) is not None


def _parse_configured_path(stdout: str) -> str | None:
    return _single_line(stdout)


def _pgrep_path() -> str | None:
    found = shutil.which("pgrep")
    resolved, _state, _reason = _capture_executable(
        found, trusted_root_paths=frozenset({"/usr/bin/pgrep"})
    )
    return resolved


def _writer_activity(
    adapter: str, *, probe: Probe | None, env: dict[str, str] | None
) -> tuple[str, list[str]]:
    pgrep = _pgrep_path()
    if pgrep is None:
        return "unknown", ["known writer probe is unavailable"]

    saw_unknown = False
    saw_active = False
    reasons: list[str] = []
    for writer in _WRITERS[adapter]:
        state = _call_probe(probe, [pgrep, "-x", writer], env=env)
        if state.outcome == "cancelled":
            return "unknown", ["known writer probe was cancelled"]
        if state.outcome != "finished" or state.returncode is None:
            saw_unknown = True
            reasons.append(f"known writer probe for {writer} did not complete")
            continue
        if state.returncode == 1:
            continue
        if state.returncode != 0:
            saw_unknown = True
            reasons.append(f"known writer probe for {writer} returned an unexpected status")
            continue

        lines = [line for line in state.stdout.splitlines() if line]
        if not lines or any(not line.isdigit() for line in lines):
            saw_unknown = True
            reasons.append(f"known writer probe for {writer} returned unrecognised output")
        elif len(lines) == 1:
            saw_active = True
            reasons.append(f"known writer {writer} is active")
        else:
            # Multiple exact-name writers are deliberately not treated as a
            # safe idle result; PID values are never retained.
            saw_unknown = True
            reasons.append(f"multiple known writers named {writer} are active")

    if saw_unknown:
        return "unknown", reasons
    if saw_active:
        return "active", reasons
    return "idle", reasons


def _base_evidence(adapter: str) -> dict[str, Any]:
    return {
        "adapter": adapter,
        "status": "REVIEW-ONLY",
        "executable": None,
        "version": None,
        "target": None,
        "owner_activity": "unknown",
        "supported": False,
        "reasons": [],
        "risk": _RISKS.get(adapter, "Untrusted or unsupported cache owner; review-only."),
        "estimated_reclaimable_bytes": None,
        "actionable": False,
    }


def _add_reason(result: dict[str, Any], reason: str) -> None:
    result["reasons"].append(_safe_text(reason))


def _run_version_and_path(
    adapter: str,
    executable: str,
    *,
    probe: Probe | None,
    env: dict[str, str] | None,
) -> tuple[str | None, str | None, list[str]]:
    version_argv = [executable, "version"] if adapter == "go" else [executable, "--version"]
    version_state = _call_probe(probe, version_argv, env=env)
    if not version_state.finished or version_state.returncode != 0:
        return None, None, [_generic_probe_error("version", version_state)]
    version = _parse_version(adapter, version_state.stdout)
    if version is None:
        return None, None, ["installed version output is not a known release format"]

    if adapter == "uv":
        path_argv = [executable, "cache", "dir"]
    elif adapter == "go":
        path_argv = [executable, "env", "GOCACHE"]
    elif adapter == "npm":
        path_argv = [executable, "config", "get", "cache"]
    elif adapter == "pnpm":
        path_argv = [executable, "store", "path"]
    else:
        path_argv = [executable, "--cache"]

    path_state = _call_probe(probe, path_argv, env=env)
    if not path_state.finished or path_state.returncode != 0:
        return version, None, [_generic_probe_error("cache configuration", path_state)]
    configured = _parse_configured_path(path_state.stdout)
    if configured is None:
        return version, None, ["cache configuration returned an unsafe or ambiguous path"]
    return version, configured, []


def _verify_features(
    adapter: str,
    executable: str,
    *,
    probe: Probe | None,
    env: dict[str, str] | None,
) -> tuple[bool, list[str], list[str]]:
    if adapter == "uv":
        state = _call_probe(
            probe,
            [executable, "cache", "prune", "--help"],
            env=env,
        )
        if not state.finished or state.returncode != 0:
            return False, [_generic_probe_error("uv cache prune help", state)], []
        help_text = (state.stdout + "\n" + state.stderr).lower()
        if (
            "prune" not in help_text
            or "cache" not in help_text
            or "--cache-dir" not in help_text
        ):
            return False, ["installed uv does not document cache prune with --cache-dir"], []
        return True, [], ["uv cache dir, cache prune, and --cache-dir help were verified"]

    if adapter == "go":
        state = _call_probe(probe, [executable, "help", "clean"], env=env)
        if not state.finished or state.returncode != 0:
            return False, [_generic_probe_error("go help clean", state)], []
        help_text = (state.stdout + "\n" + state.stderr).lower()
        if "-cache" not in help_text or "clean" not in help_text:
            return False, ["installed Go help does not document go clean -cache"], []
        return True, [], ["go env GOCACHE and go help clean were verified"]

    return False, [], []


def _uv_environment_review() -> tuple[str, bool, list[str]]:
    raw_mode = os.environ.get("UV_LINK_MODE")
    supplied_mode = raw_mode.strip().lower() if isinstance(raw_mode, str) else ""
    documented_modes = {"copy", "hardlink", "clone", "symlink"}
    warnings: list[str] = [
        "uv configuration files and cache environment references were not content-inspected"
    ]
    blocked = False
    if not supplied_mode:
        # An unset process variable is not evidence that persisted config is
        # absent.  The bound execution environment therefore requires the one
        # explicitly reviewed mode instead of assuming uv's default globally.
        link_mode = "unverified"
        blocked = True
        warnings.append("uv link mode is unverified because the process variable is unset")
    elif supplied_mode not in documented_modes:
        link_mode = "unknown"
        blocked = True
        warnings.append("uv link mode is not a documented supported mode")
    elif supplied_mode != _UV_EXECUTION_LINK_MODE:
        link_mode = supplied_mode
        blocked = True
        warnings.append(
            f"uv link mode {supplied_mode} is documented but not supported by this bound action"
        )
    else:
        link_mode = supplied_mode

    centralized = os.environ.get("UV_PROJECT_ENVIRONMENT")
    if isinstance(centralized, str) and centralized:
        blocked = True
        # Do not expose the configured private path.
        warnings.append(
            "UV_PROJECT_ENVIRONMENT is configured; centralized environments need separate review"
        )

    no_cache = os.environ.get("UV_NO_CACHE")
    if isinstance(no_cache, str) and no_cache.strip().lower() not in {
        "",
        "0",
        "false",
        "no",
        "off",
    }:
        blocked = True
        warnings.append("UV_NO_CACHE is enabled or unverified")
    return link_mode, blocked, warnings


def _normalise_adapter(value: object) -> str:
    if not isinstance(value, str) or _CONTROL_RE.search(value) is not None:
        return "unknown"
    return value.lower()


def _guard_failure_evidence(adapter: str, reason: str) -> dict[str, Any]:
    result = _base_evidence(adapter)
    result["status"] = "UNAVAILABLE"
    result["guard"] = "unavailable"
    _add_reason(result, reason)
    return result


def discover(adapter: str, *, probe: Probe | None = None) -> dict[str, Any]:
    """Return bounded review evidence for one built-in cache owner.

    The native no-hydration guard is established before ``which``, stat, or
    temporary-directory access.  A guard loss stops discovery immediately;
    cancellation propagates to the caller so the CLI can return code 130.
    """

    adapter = _normalise_adapter(adapter)
    try:
        audit_runtime.enable_no_hydration()
    except audit_runtime.GuardUnavailable:
        return _guard_failure_evidence(adapter, "no-hydration guard unavailable")
    except Exception:
        return _guard_failure_evidence(adapter, "no-hydration guard unavailable")

    result = _base_evidence(adapter)
    result["guard"] = "available"
    command_name = _OWNER_COMMANDS.get(adapter)
    if command_name is None:
        _add_reason(result, "adapter is not a trusted built-in owner")
        return result

    try:
        executable, executable_state, executable_reason = _capture_executable(
            shutil.which(command_name)
        )
        if executable is None:
            result["status"] = "UNAVAILABLE"
            _add_reason(result, executable_reason or "owner executable is unavailable")
            return result
        result["executable"] = executable
        result["executable_state"] = executable_state

        env = _probe_env(adapter)
        version, configured_path, reasons = _run_version_and_path(
            adapter, executable, probe=probe, env=env
        )
        if version is not None:
            result["version"] = version
        for reason in reasons:
            _add_reason(result, reason)
        if version is None or configured_path is None:
            return result

        target, target_state, target_reason = _capture_cache_root(configured_path)
        if target is None:
            _add_reason(result, target_reason or "configured cache directory is unavailable")
            return result
        result["target"] = target
        result["target_state"] = target_state

        if adapter in _REVIEW_ONLY:
            result["status"] = "REVIEW-ONLY"
            _add_reason(result, f"{command_name} ownership adapter is review-only")
            return result

        if not _version_is_known(adapter, version):
            _add_reason(result, "installed version is outside the reviewed stable version policy")
            return result

        features_ok, feature_reasons, feature_warnings = _verify_features(
            adapter, executable, probe=probe, env=env
        )
        for reason in feature_reasons:
            _add_reason(result, reason)
        result["warnings"] = [_safe_text(item) for item in feature_warnings]
        if not features_ok:
            return result

        if adapter == "uv":
            link_mode, link_blocked, link_reasons = _uv_environment_review()
            result["link_mode"] = link_mode
            result["centralized_environment"] = bool(os.environ.get("UV_PROJECT_ENVIRONMENT"))
            result["warnings"].extend(_safe_text(item) for item in link_reasons)
            if link_blocked:
                for reason in link_reasons:
                    _add_reason(result, reason)
                return result

        activity, activity_reasons = _writer_activity(adapter, probe=probe, env=env)
        result["owner_activity"] = activity
        for reason in activity_reasons:
            # Active/unknown writer names are fixed allowlist values, not
            # command output.  Keep them as bounded diagnostics.
            _add_reason(result, reason)
        if activity != "idle":
            return result

        result["supported"] = True
        result["status"] = "READY"
        result["features_verified"] = [
            "configured cache root",
            "reviewed stable release version",
            *(feature_warnings or []),
        ]
        # A successful discovery still cannot be an approval or an action.
        result["actionable"] = False
        return result
    except _GuardLost:
        result["status"] = "UNAVAILABLE"
        result["guard"] = "unavailable"
        result["supported"] = False
        result["owner_activity"] = "unknown"
        _add_reason(result, "no-hydration guard became unavailable during discovery")
        return result
    except OSError:
        result["status"] = "REVIEW-ONLY"
        result["supported"] = False
        _add_reason(result, "owner discovery filesystem metadata was unavailable")
        return result
    except Exception:
        result["status"] = "REVIEW-ONLY"
        result["supported"] = False
        _add_reason(result, "owner discovery failed closed")
        return result


def _load_contract() -> Any:
    try:
        return importlib.import_module("cleanup_contract")
    except Exception as exc:
        # Contract import failures are never allowed to escape with dependency
        # paths or implementation details.
        raise AdapterError("cleanup_contract dependency is not available") from exc


def prepare(adapter: str, *, probe: Probe | None = None) -> dict[str, Any]:
    """Prepare an approval-bound plan, or raise a sanitized ``AdapterError``."""

    adapter = _normalise_adapter(adapter)
    evidence = discover(adapter, probe=probe)
    if evidence.get("guard") == "unavailable":
        raise AdapterError("no-hydration guard unavailable")
    if not evidence.get("supported") or evidence.get("owner_activity") != "idle":
        raise AdapterError("owner evidence is not ready for plan preparation")
    if adapter not in _SUPPORTED_ACTIONS:
        raise AdapterError("owner has no supported action in this adapter")

    try:
        contract = _load_contract()
    except AdapterError:
        raise AdapterError("cleanup contract is unavailable") from None

    make_plan = getattr(contract, "make_plan", None)
    validate_plan = getattr(contract, "validate_plan", None)
    if not callable(make_plan) or not callable(validate_plan):
        raise AdapterError("cleanup contract plan API is unavailable")

    kwargs: dict[str, Any] = {}
    home = os.environ.get("HOME")
    if isinstance(home, str) and home:
        kwargs["home"] = home
    try:
        plan = make_plan(
            adapter,
            evidence["target"],
            evidence["executable"],
            evidence["version"],
            **kwargs,
        )
        validated = validate_plan(plan)
    except Exception:
        # Do not leak a dependency's exception, path, or command output.
        # Cancellation is a BaseException and propagates.
        raise AdapterError("cleanup plan could not be prepared") from None
    if not isinstance(validated, dict):
        raise AdapterError("cleanup contract returned an invalid plan")
    return validated


def _validate_plan(plan: object) -> tuple[Any, dict[str, Any]]:
    contract = _load_contract()
    validate = getattr(contract, "validate_plan", None)
    if not callable(validate):
        raise AdapterError("cleanup_contract.validate_plan is unavailable")
    try:
        validated = validate(plan)
    except Exception as exc:
        raise AdapterError("cleanup plan validation failed") from exc
    if not isinstance(validated, dict):
        raise AdapterError("cleanup plan validation returned an invalid plan")
    return contract, validated


def preflight(plan: dict[str, Any], *, probe: Probe | None = None) -> dict[str, Any]:
    """Freshly rediscover plan inputs without running a mutation."""

    try:
        contract, validated = _validate_plan(plan)
    except AdapterError as exc:
        return {"ok": False, "reasons": [_safe_text(str(exc))], "evidence": {}}

    adapter = validated.get("adapter")
    if adapter not in _SUPPORTED_ACTIONS:
        return {
            "ok": False,
            "reasons": ["plan adapter has no executable cache action"],
            "evidence": {},
        }

    evidence = discover(adapter, probe=probe)
    evidence_out = {
        "adapter": adapter,
        "executable": evidence.get("executable"),
        "version": evidence.get("version"),
        "target": evidence.get("target"),
        "owner_activity": evidence.get("owner_activity"),
        "lock_semantics": "owner CLI is authoritative; adapter does not replace native owner locks",
        "dynamic_scope": True,
        "disclosures": [],
    }
    if evidence.get("guard") == "unavailable":
        return {
            "ok": False,
            "reasons": ["no-hydration guard unavailable"],
            "evidence": evidence_out,
        }

    reasons: list[str] = []
    if evidence.get("status") != "READY" or not evidence.get("supported"):
        reasons.append("fresh owner capability evidence is not ready")
    if evidence.get("owner_activity") != "idle":
        reasons.append("known owner activity is not idle")
    if evidence.get("executable") != validated.get("executable"):
        reasons.append("resolved owner executable changed")
    if evidence.get("version") != validated.get("version"):
        reasons.append("installed owner version changed")
    if evidence.get("target") != validated.get("target"):
        reasons.append("configured cache root changed")

    target_unchanged = getattr(contract, "target_unchanged", None)
    if not callable(target_unchanged):
        reasons.append("cleanup_contract.target_unchanged is unavailable")
    else:
        try:
            if not bool(target_unchanged(validated)):
                reasons.append("configured cache target identity changed")
        except Exception:
            reasons.append("configured cache target identity could not be recaptured")

    warnings = evidence.get("warnings", [])
    if isinstance(warnings, list):
        disclosures = [_safe_text(item) for item in warnings if isinstance(item, str)]
    else:
        disclosures = []
    evidence_out = {
        "adapter": adapter,
        "executable": evidence.get("executable"),
        "version": evidence.get("version"),
        "target": evidence.get("target"),
        "owner_activity": evidence.get("owner_activity"),
        "lock_semantics": "owner CLI is authoritative; adapter does not replace native owner locks",
        "dynamic_scope": True,
        "disclosures": disclosures,
    }
    return {"ok": not reasons, "reasons": reasons, "evidence": evidence_out}


def _local_plan_fields(plan: Mapping[str, Any], adapter: str, action: str) -> tuple[str, str, str]:
    if plan.get("adapter") != adapter or plan.get("action") != action:
        raise AdapterError("plan action is not in the trusted cache allowlist")
    executable = plan.get("executable")
    target = plan.get("target")
    version = plan.get("version")
    if not isinstance(executable, str) or not os.path.isabs(executable):
        raise AdapterError("plan executable is not absolute")
    if _has_control_or_dot_component(executable):
        raise AdapterError("plan executable is unsafe")
    if not isinstance(target, str) or not os.path.isabs(target):
        raise AdapterError("plan cache root is not absolute")
    if _has_control_or_dot_component(target):
        raise AdapterError("plan cache root is unsafe")
    if not _version_is_known(adapter, version):
        raise AdapterError("plan uses an unknown owner version")
    return executable, target, version


def _predictable_environment(home: object) -> dict[str, str]:
    """Build a complete, non-secret environment for the owner subprocess."""

    if not isinstance(home, str) or not os.path.isabs(home):
        raise AdapterError("plan home is not absolute")
    if _has_control_or_dot_component(home):
        raise AdapterError("plan home is unsafe")

    path = os.environ.get("PATH", os.defpath)
    if not isinstance(path, str) or not path or "\x00" in path or _CONTROL_RE.search(path):
        path = os.defpath
    environment = {"HOME": home, "PATH": path}
    for key in _SAFE_ENV_KEYS:
        if key in {"PATH"}:
            continue
        value = os.environ.get(key)
        if isinstance(value, str) and value and "\x00" not in value and _CONTROL_RE.search(value) is None:
            environment[key] = value
    return environment


def build_command(plan: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """Build one fixed owner command and a complete predictable environment."""

    _contract, validated = _validate_plan(plan)
    adapter = validated.get("adapter")
    if adapter == "uv":
        executable, target, version = _local_plan_fields(
            validated, "uv", UV_ACTION
        )
        # Preparation binds the reviewed link/configuration assumptions.  The
        # command builder stays pure and uses a fixed safe mode rather than
        # rereading mutable process environment at dispatch time.
        environment = _predictable_environment(validated.get("home"))
        # All variables below are documented uv configuration controls.  The
        # explicit root wins over discarded configuration-file references;
        # setting UV_NO_CACHE=0 prevents an inherited no-cache setting from
        # silently changing the meaning of prune.
        environment.update(
            {
                "UV_NO_CONFIG": "1",
                "UV_NO_CACHE": "0",
                "UV_CACHE_DIR": target,
                "UV_LOCK_TIMEOUT": str(UV_LOCK_TIMEOUT_SECONDS),
                "UV_LINK_MODE": _UV_EXECUTION_LINK_MODE,
            }
        )
        if _uv_release(version) is not None and _uv_release(version) >= (0, 11, 8):
            # Avoid project discovery where this documented option exists.
            environment["UV_NO_PROJECT"] = "1"
        return [executable, "--cache-dir", target, "cache", "prune"], environment
    if adapter == "go":
        executable, target, version = _local_plan_fields(
            validated, "go", GO_ACTION
        )
        if _go_release(version) is None:
            raise AdapterError("plan uses an unknown Go version")
        environment = _predictable_environment(validated.get("home"))
        # Empty values explicitly clear inherited widening controls.  GOENV
        # and GOWORK are set to their documented disabling values.
        environment.update(
            {
                "GOFLAGS": "",
                "GOCACHEPROG": "",
                "GOTOOLCHAIN": "local",
                "GOENV": "off",
                "GOWORK": "off",
                "GOCACHE": target,
            }
        )
        return [executable, "clean", "-cache"], environment
    raise AdapterError("plan action is not in the trusted cache allowlist")


__all__ = [
    "AdapterError",
    "AdapterCancelled",
    "discover",
    "prepare",
    "preflight",
    "build_command",
]
