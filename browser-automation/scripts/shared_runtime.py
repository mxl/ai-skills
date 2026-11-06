#!/usr/bin/env python3
"""Opt-in shared controller transport; never moves controllers or browser state.

configure's --allow-write is an accident guard, not evidence of host approval.
The host must obtain approval before invoking it. plan is strictly read-only.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
try:
    import fcntl
except ImportError:
    fcntl = None


MESSAGES = {
    "invalid_state": "Shared runtime policy is invalid; preserve it for review.",
    "unsafe_path": "Unsafe shared runtime path, ownership, permissions, or type.",
    "invalid_arguments": "Invalid shared runtime arguments; values suppressed.",
    "confirmation_required": "Host approval and --allow-write are required.",
    "shared_root_change_required": "Configured shared root cannot change automatically.",
    "invalid_project": "Specify an existing unambiguous project root.",
}


class SharedRuntimeError(Exception):
    def __init__(self, code: str = "invalid_state"):
        self.code = code if code in MESSAGES else "invalid_state"
        self.message = MESSAGES[self.code]
        super().__init__(self.message)


def policy_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "browser-automation" / "shared-runtime.json"


def _reject_symlinks(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise SharedRuntimeError("unsafe_path")


def _private_directory(path: Path) -> None:
    _reject_symlinks(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise SharedRuntimeError("unsafe_path") from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise SharedRuntimeError("unsafe_path")


def _project_key(key: str) -> str:
    if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
        raise SharedRuntimeError()
    return key


def validate_socket_directory(value: str | Path) -> Path:
    """Validate a short, canonical direct child of ~/.ab without creating it."""
    if not isinstance(value, (str, Path)):
        raise SharedRuntimeError("unsafe_path")
    raw = str(value)
    path = Path(raw)
    base = Path.home() / ".ab"
    if (not path.is_absolute() or raw != str(path) or path.parent != base
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", path.name)
            or len(os.fsencode(path)) > 52):
        raise SharedRuntimeError("unsafe_path")
    _reject_symlinks(path)
    # ~/.ab may already be used by a live legacy dashboard. Do not tighten its
    # mode or require a new mode; only the selected socket root is private.
    if base.exists():
        info = base.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise SharedRuntimeError("unsafe_path")
    _private_directory(path)
    return path


def legacy_socket_directory(project_key: str) -> Path:
    return validate_socket_directory(Path.home() / ".ab" / _project_key(project_key)[:24])


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise SharedRuntimeError()
        value[key] = item
    return value


def _validate_policy(value: object) -> dict:
    if (not isinstance(value, dict) or set(value) != {"version", "socketDirectory", "projects"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["socketDirectory"], str)
            or not isinstance(value["projects"], list)):
        raise SharedRuntimeError()
    keys = [_project_key(key) for key in value["projects"]]
    if len(set(keys)) != len(keys):
        raise SharedRuntimeError()
    validate_socket_directory(value["socketDirectory"])
    return value


def _open_private(path: Path, flags: int) -> int:
    _reject_symlinks(path)
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            os.close(fd)
            raise SharedRuntimeError("unsafe_path")
        return fd
    except OSError:
        raise SharedRuntimeError("unsafe_path") from None


def read_policy() -> dict | None:
    path = policy_path()
    _reject_symlinks(path)
    _private_directory(path.parent)
    # Only a genuinely absent file is the legacy default, never a broken link.
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise SharedRuntimeError("unsafe_path") from None
    fd = _open_private(path, os.O_RDONLY)
    try:
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(1_000_001)
        if len(data) > 1_000_000:
            raise SharedRuntimeError()
        return _validate_policy(json.loads(data, object_pairs_hook=_unique_object))
    except (ValueError, UnicodeError, RecursionError):
        raise SharedRuntimeError() from None
    except OSError:
        raise SharedRuntimeError("unsafe_path") from None


def project_socket_directory(project_key: str) -> Path:
    _project_key(project_key)
    policy = read_policy()
    if policy is not None and project_key in policy["projects"]:
        return validate_socket_directory(policy["socketDirectory"])
    return legacy_socket_directory(project_key)


def task_socket_directory(project_key: str, task: dict) -> Path:
    """Missing socketDir is permanently legacy, including after enrollment."""
    legacy = legacy_socket_directory(project_key)
    if not isinstance(task, dict):
        raise SharedRuntimeError()
    if "socketDir" not in task:
        return legacy
    if not isinstance(task["socketDir"], str):
        raise SharedRuntimeError()
    recorded = validate_socket_directory(task["socketDir"])
    if recorded == legacy:
        return recorded
    policy = read_policy()
    if (policy is not None and project_key in policy["projects"]
            and str(recorded) == policy["socketDirectory"]):
        return recorded
    raise SharedRuntimeError()


def validate_controller_migration(project_key: str, value: object) -> None:
    """Validate the journal without authorizing execution or clearing a lease."""
    if (not isinstance(value, dict) or not value
            or set(value) - {"status", "fromSocketDir", "toSocketDir", "phase",
                             "targetId", "temporaryTargetId"}
            or not isinstance(value.get("status"), str)
            or value["status"] not in {"in_progress", "completed"}):
        raise SharedRuntimeError()
    for field in ("fromSocketDir", "toSocketDir"):
        if field in value:
            task_socket_directory(project_key, {"socketDir": value[field]})
    if "phase" in value and (not isinstance(value["phase"], str) or value["phase"] not in {
            "disconnect_source", "attach_destination", "select_original",
            "close_temporary_blank", "uncertain"}):
        raise SharedRuntimeError()
    for field in ("targetId", "temporaryTargetId"):
        if field in value and (not isinstance(value[field], str)
                or re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value[field]) is None):
            raise SharedRuntimeError()


def _load_core():
    # File-based imports work even when the scripts directory is not on sys.path.
    name = "_browser_automation_shared_runtime_core"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("browser_session.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


def _resolve_keys(projects: list[str | Path]) -> list[str]:
    if not projects:
        raise SharedRuntimeError("invalid_arguments")
    core = _load_core()
    try:
        return sorted({_project_key(core.resolve_project(root).key) for root in projects})
    except core.BrowserSessionError:
        raise SharedRuntimeError("invalid_project") from None


def _candidate(socket_dir: str | Path, keys: list[str]) -> dict:
    root = validate_socket_directory(socket_dir)
    previous = read_policy()
    if previous is not None and previous["socketDirectory"] != str(root):
        raise SharedRuntimeError("shared_root_change_required")
    return _validate_policy({"version": 1, "socketDirectory": str(root),
                             "projects": sorted(set(keys) | set(previous["projects"] if previous else []))})


def plan(socket_dir: str | Path, projects: list[str | Path]) -> dict:
    return {"status": "planned", "policy": _candidate(socket_dir, _resolve_keys(projects)),
            "controllersMoved": False}


def configure(socket_dir: str | Path, projects: list[str | Path], *, allow_write: bool = False) -> dict:
    if not allow_write:
        raise SharedRuntimeError("confirmation_required")
    if fcntl is None:
        raise SharedRuntimeError("invalid_state")
    keys = _resolve_keys(projects)
    _candidate(socket_dir, keys)  # Complete read-only preflight before any writes.
    parent = policy_path().parent
    # Existing ancestors need not be private; only the policy directory does.
    if not parent.exists():
        missing = []
        ancestor = parent
        while not ancestor.exists():
            missing.append(ancestor)
            ancestor = ancestor.parent
        _reject_symlinks(parent)
        for directory in reversed(missing):
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            _private_directory(directory)
    _private_directory(parent)
    fd = _open_private(parent / "shared-runtime.lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        candidate = _candidate(socket_dir, keys)  # Merge again under global lock.
        tmp_fd, tmp = tempfile.mkstemp(prefix=".shared-runtime-", dir=parent)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as stream:
                json.dump(candidate, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            _reject_symlinks(policy_path())
            os.replace(tmp, policy_path())
            directory_fd = os.open(parent, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError:
        raise SharedRuntimeError("unsafe_path") from None
    finally:
        os.close(fd)
    return {"status": "configured", "policy": candidate, "controllersMoved": False}


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise SharedRuntimeError("invalid_arguments")


def build_parser() -> argparse.ArgumentParser:
    parser = SafeParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "configure"):
        sub = commands.add_parser(command)
        sub.add_argument("--socket-dir", required=True)
        sub.add_argument("--project", action="append", required=True)
        if command == "configure":
            sub.add_argument("--allow-write", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.command == "plan":
            result = plan(args.socket_dir, args.project)
        else:
            result = configure(args.socket_dir, args.project, allow_write=args.allow_write)
        print(json.dumps({"success": True, "data": result}, ensure_ascii=True))
        return 0
    except SharedRuntimeError as error:
        code = error.code
    except (OSError, ValueError, TypeError, RuntimeError, UnicodeError):
        code = "invalid_state"
    print(json.dumps({"success": False, "error": {"code": code, "message": MESSAGES[code]}}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
