#!/usr/bin/env python3
"""Fail-closed, local macOS browser task coordinator (Python 3.10+).

Approval flags are accident guards, NOT evidence of human consent. The host must
obtain approval. This is a cooperative command boundary, not an OS sandbox:
raw agent-browser/CDP, another process, pages, and the dashboard can bypass it.
Human leases never expire. Capture cessation/account checks are declarations.
No credentials, page content, full URLs, or browser WebSocket URLs are persisted.
"""
from __future__ import annotations

import argparse
import contextlib
try:
    import fcntl
except ImportError:  # Pure parsing/validation functions remain importable off Unix.
    fcntl = None
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Iterator


DEFAULT_STATE_DIR = Path.home() / "Library" / "Application Support" / "browser-automation"
# Explicitly reviewed site roots, not a guessed public-suffix algorithm. Unknown
# domains remain project-wide blockers until reviewed support is added.
SUPPORTED_SITE_ROOTS = frozenset({
    "psychodemia.ru", "miin.ru", "mosdigitals.ru", "bangbangeducation.ru",
    "maed.ru", "school-xyz.com", "fashionfactoryschool.ru", "fashionfactory.ru",
    "fashionfactoryschool.com", "psychodemia.com", "selectel.ru",
})
# Both Fashion Factory domains belong to the same authentication isolation scope.
SITE_SCOPE_ALIASES = {"fashionfactory.ru": "fashionfactoryschool.ru"}


MESSAGES = {
    "unsupported_platform": "Runtime commands require macOS.",
    "invalid_project": "Specify an existing unambiguous non-bare project.",
    "onboarding_required": "Profile must be untracked and git-ignored; approve onboarding edits separately.",
    "migration_required": "Legacy profile exists; coordinate an approved offline migration.",
    "unsafe_path": "Unsafe file ownership, permissions, type, or symbolic link.",
    "invalid_state": "Runtime state is invalid; preserve it for manual review.",
    "command_failed": "A subprocess failed; raw output suppressed.",
    "command_timeout": "A subprocess timed out; preserve state for review.",
    "browser_unverified": "Browser process, profile, or local endpoint could not be verified.",
    "browser_absent": "No verified project browser; request launch approval first.",
    "browser_locked": "Profile lock or stale endpoint exists; no automatic cleanup or restart.",
    "stale_browser": "Browser identity changed; tasks cannot be rebound automatically.",
    "launch_approval_required": "Launch requires host approval and --allow-launch.",
    "mode_transition_required": "Existing browser/tasks prevent a mode change; coordinate manually.",
    "launch_uncertain": "Launch did not verify; inspect manually without repeating launch.",
    "agent_browser_missing": "Install/configure agent-browser separately; executable not found.",
    "invalid_json": "Browser response was empty or unrecognized; raw output suppressed.",
    "invalid_url": "Use HTTPS or loopback HTTP without userinfo, query, or fragment.",
    "invalid_task": "Task ID must contain 1-128 ASCII letters, digits, dots, underscores, or hyphens.",
    "task_exists": "Task already exists; uncertain/released tasks require manual coordination.",
    "task_missing": "Task does not exist; do not adopt another tab.",
    "task_paused": "Task is paused or uncertain; complete an explicit human handoff/resume.",
    "human_lease": "Human lease blocks this action until explicit resume.",
    "auth_lease": "Authentication lease blocks this action until explicit resume or verified cross-site policy scope.",
    "invalid_policy": "Browser concurrency policy is invalid; preserve it for review.",
    "tab_gone": "Owned target is missing; do not choose another tab.",
    "ownership_unverified": "Owned target selection could not be verified; no automatic retargeting.",
    "command_denied": "Command is outside the bounded page-command allowlist.",
    "confirmation_required": "Explicit capture/completion/account declaration is required.",
    "reconciliation_required": "An unresolved operation requires manual reconciliation before human input.",
    "completion_unproven": "No matching durable daemon completion receipt; keep uncertain and coordinate reviewed recovery.",
    "retirement_recorded": "A target retirement was already journaled; do not repeat it or reconcile the old operation.",
    "daemon_command_error": "Daemon returned a recognized completed command error; task remains uncertain for explicit reconciliation.",
    "command_outcome_unknown": "CLI could not establish daemon completion; preserve uncertainty without replay.",
    "invalid_arguments": "Invalid command arguments; values suppressed.",
}


class BrowserSessionError(Exception):
    """Public error with a stable code and a fixed, non-sensitive message."""
    def __init__(self, code: str):
        self.code = code if code in MESSAGES else "command_failed"
        self.message = MESSAGES[self.code]
        super().__init__(self.message)


def fail(code: str) -> None:
    raise BrowserSessionError(code)


def shared_runtime_call(name: str, *args):
    """Lazily load the sibling policy module, including under file-based tests."""
    import importlib.util
    module_name = "_browser_automation_shared_runtime"
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            module_name, Path(__file__).with_name("shared_runtime.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
    module = sys.modules[module_name]
    try:
        return getattr(module, name)(*args)
    except module.SharedRuntimeError as error:
        fail("unsafe_path" if error.code == "unsafe_path" else "invalid_state")


def controller_migration_guard(task: dict[str, Any]) -> None:
    marker = task.get("controllerMigration")
    if marker is not None and (not isinstance(marker, dict)
                               or marker.get("status") != "completed"):
        fail("reconciliation_required")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def checked_process(args: list[str], *, cwd: Path | None = None,
                    env: dict[str, str] | None = None, timeout: int = 15,
                    accepted: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        fail("command_timeout")
    except (OSError, UnicodeError):
        fail("command_failed")
    if result.returncode not in accepted:
        fail("command_failed")
    return result


@dataclass(frozen=True)
class Project:
    root: Path
    key: str
    is_git: bool

    @property
    def profile(self) -> Path:
        return self.root / ".chrome-user-data"


def resolve_project(project: str | Path | None = None, *, cwd: Path | None = None) -> Project:
    """Resolve worktrees to their main checkout; explicit non-Git roots allowed."""
    explicit = project is not None
    try:
        root = Path(project if explicit else (cwd or Path.cwd())).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        fail("invalid_project")
    if not root.is_dir():
        fail("invalid_project")
    git = shutil.which("git")
    if not git:
        fail("invalid_project")
    probe = checked_process([git, "-C", str(root), "rev-parse", "--is-bare-repository"],
                            accepted=(0, 128))
    if probe.returncode:
        if not explicit:
            fail("invalid_project")
        return Project(root, digest(str(root)), False)
    if probe.stdout.strip() != "false":
        fail("invalid_project")
    common_raw = checked_process([git, "-C", str(root), "rev-parse", "--path-format=absolute",
                                  "--git-common-dir"]).stdout.strip()
    common = Path(common_raw).resolve()
    listing = checked_process([git, "-C", str(root), "worktree", "list", "--porcelain", "-z"]).stdout
    records = [r for r in listing.split("\0\0") if r]
    main = []
    for record in records:
        fields = record.split("\0")
        paths = [f[9:] for f in fields if f.startswith("worktree ")]
        if len(paths) != 1 or "bare" in fields:
            fail("invalid_project")
        candidate = Path(paths[0]).resolve()
        if candidate == common:
            # Git lists the Git directory rather than the checkout when the
            # main repository uses --separate-git-dir. Require an explicit
            # main location, then verify its gitfile and top-level below.
            configured = checked_process(
                [git, "config", "--file", str(common / "config"), "--get", "core.worktree"],
                accepted=(0, 1),
            )
            if configured.returncode or not configured.stdout.strip():
                continue
            location = Path(configured.stdout.strip())
            candidate = (location if location.is_absolute() else common / location).resolve()
        dotgit = candidate / ".git"
        if not (dotgit.is_dir() or dotgit.is_file()):
            continue
        resolved = checked_process(
            [git, "rev-parse", "--resolve-git-dir", str(dotgit)], accepted=(0, 128)
        )
        if resolved.returncode or Path(resolved.stdout.strip()).resolve() != common:
            continue
        # A gitfile may point at a separate main Git directory or at a linked
        # worktree's private directory. Only the former equals the common dir.
        top = checked_process(
            [git, "-C", str(candidate), "rev-parse", "--show-toplevel"], accepted=(0, 128)
        )
        if not top.returncode and Path(top.stdout.strip()).resolve() == candidate:
            main.append(candidate)
    if len(main) != 1:
        fail("invalid_project")
    root = main[0]
    return Project(root, digest(str(root)), True)


def reject_symlinks(path: Path) -> None:
    """Check every existing component, including dangling symlinks."""
    for item in (path, *path.parents):
        if item.is_symlink():
            fail("unsafe_path")


def check_profile(project: Project) -> None:
    profile = project.profile
    reject_symlinks(profile)
    legacy = project.root / ".agent-browser-profile"
    if legacy.is_symlink() or (legacy.exists() and (not legacy.is_dir() or any(legacy.iterdir()))):
        fail("migration_required")
    if profile.exists() and not profile.is_dir():
        fail("unsafe_path")
    if profile.exists() and profile.stat().st_uid != os.getuid():
        fail("unsafe_path")
    if project.is_git:
        git = shutil.which("git") or "git"
        tracked = checked_process([git, "-C", str(project.root), "ls-files", "-z", "--",
                                   ".chrome-user-data"]).stdout
        ignored = checked_process([git, "-C", str(project.root), "check-ignore", "--no-index",
                                   "-q", ".chrome-user-data/"],
                                  accepted=(0, 1))
        if tracked or ignored.returncode:
            fail("onboarding_required")


def secure_directory(path: Path) -> None:
    reject_symlinks(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        fail("unsafe_path")


def secure_open(path: Path, flags: int) -> int:
    reject_symlinks(path)
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
        os.close(fd)
        fail("unsafe_path")
    return fd


class StateStore:
    """Owner-only state; callers must hold lock() across complete operations."""
    def __init__(self, base: Path, project: Project):
        self.directory = base.expanduser().absolute() / project.key
        secure_directory(self.directory.parent)
        secure_directory(self.directory)
        self.path = self.directory / "state.json"
        self.project = project

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        if fcntl is None:
            fail("unsupported_platform")
        fd = secure_open(self.directory / "lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def load(self) -> dict[str, Any]:
        if not self.path.exists() and not self.path.is_symlink():
            return {"version": 1, "project": self.project.key, "browser": None,
                    "auth_task": None, "tasks": {}}
        fd = secure_open(self.path, os.O_RDONLY)
        try:
            with os.fdopen(fd) as stream:
                value = json.load(stream)
        except (ValueError, UnicodeError):
            fail("invalid_state")
        if (not isinstance(value, dict) or value.get("version") != 1
                or value.get("project") != self.project.key or not isinstance(value.get("tasks"), dict)
                or "auth_task" not in value or "browser" not in value):
            fail("invalid_state")
        for key, task in value["tasks"].items():
            if not isinstance(task, dict) or task.get("session") != session_name(self.project.key, key):
                fail("invalid_state")
            if task.get("status") not in {"attaching", "active", "uncertain", "human", "paused", "releasing", "released"}:
                fail("invalid_state")
            if task.get("targetId") is not None and not valid_target(task["targetId"]):
                fail("invalid_state")
            if "socketDir" in task:
                shared_runtime_call("task_socket_directory", self.project.key, task)
            if "controllerMigration" in task:
                shared_runtime_call("validate_controller_migration", self.project.key,
                                    task["controllerMigration"])
        if value["auth_task"] is not None:
            task = value["tasks"].get(value["auth_task"])
            if not task or task.get("status") != "human" or not task.get("auth"):
                fail("invalid_state")
        return value

    def save(self, value: dict[str, Any]) -> None:
        reject_symlinks(self.path)
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream, separators=(",", ":"), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def empty_config(self) -> Path:
        path = self.directory / "agent-browser-empty.json"
        fd = secure_open(path, os.O_CREAT | os.O_RDWR)
        try:
            data = os.read(fd, 4096)
            if not data:
                os.write(fd, b"{}\n")
                os.fsync(fd)
            elif data.strip() != b"{}":
                fail("invalid_state")
        finally:
            os.close(fd)
        return path


def validate_task(task: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", task):
        fail("invalid_task")
    return task


def session_name(project_key: str, task: str) -> str:
    validate_task(task)
    return "ba-" + project_key[:16] + "-" + digest(task)[:24]


def loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def validate_url(url: str) -> str:
    try:
        value = urllib.parse.urlsplit(url)
        port = value.port
        if (not value.hostname or value.username is not None or value.password is not None
                or "?" in url or "#" in url or re.search(r"[\x00-\x20\x7f\\]", url)
                or (value.scheme != "https" and not (value.scheme == "http" and loopback(value.hostname)))):
            fail("invalid_url")
        if port is not None and not 1 <= port <= 65535:
            fail("invalid_url")
    except ValueError:
        fail("invalid_url")
    return url


def origin(url: str) -> str:
    """Redact every URL component except its HTTP(S) origin."""
    try:
        value = urllib.parse.urlsplit(url)
        host = value.hostname
        if value.scheme not in {"http", "https"} or not host or re.search(r"[^A-Za-z0-9.:_-]", host):
            return "opaque"
        host = "[" + host + "]" if ":" in host else host
        port = value.port
        return value.scheme + "://" + host + (":" + str(port) if port else "")
    except (ValueError, TypeError):
        return "opaque"


def concurrency_roots(project: Project) -> frozenset[str]:
    """Read an approved project opt-in; absent policy preserves legacy behavior."""
    path = project.root / "browser-concurrency.json"
    reject_symlinks(path)
    if not path.exists():
        return frozenset()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or info.st_size > 16384):
                fail("invalid_policy")
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        fail("invalid_policy")
                    result[key] = value
                return result
            policy = json.load(stream, object_pairs_hook=unique_object)
    except (OSError, ValueError, UnicodeError):
        fail("invalid_policy")
    if (not isinstance(policy, dict)
            or set(policy) != {"version", "allow_cross_site_handoffs", "site_roots"}
            or type(policy["version"]) is not int or policy["version"] != 1
            or type(policy["allow_cross_site_handoffs"]) is not bool
            or not isinstance(policy["site_roots"], list)
            or not policy["site_roots"]
            or any(not isinstance(root, str) or root not in SUPPORTED_SITE_ROOTS
                   for root in policy["site_roots"])
            or len(set(policy["site_roots"])) != len(policy["site_roots"])):
        fail("invalid_policy")
    return frozenset(policy["site_roots"]) if policy["allow_cross_site_handoffs"] else frozenset()


def site_scope(value: Any, roots: frozenset[str]) -> str | None:
    """Only clean canonical HTTPS origins under explicitly approved site roots."""
    if not isinstance(value, str) or value != origin(value):
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        if parsed.scheme != "https" or not host or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", host):
            return None
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return None
    except ValueError:
        return None
    matches = [root for root in roots if host == root or host.endswith("." + root)]
    return SITE_SCOPE_ALIASES.get(matches[0], matches[0]) if len(matches) == 1 else None


def flag_values(command: str, flag: str) -> list[str]:
    """Parse ps' unquoted argv, whose path values may contain spaces.

    Ambiguous flag-like components in paths deliberately do not match. macOS ps
    does not offer lossless argv; executable is independently checked via comm.
    """
    pattern = r"(?:^|\s)" + re.escape(flag) + r"(?:=|\s+)(.*?)(?=\s+--[A-Za-z]|$)"
    return [match.strip().strip('"\'') for match in re.findall(pattern, command)]


def discover_processes(profile: Path) -> list[dict[str, Any]]:
    rows = checked_process(["/bin/ps", "-axo", "pid=,command=", "-ww"]).stdout.splitlines()
    found = []
    for row in rows:
        parts = row.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, command = int(parts[0]), parts[1]
        if "--user-data-dir" not in command:
            continue
        values = flag_values(command, "--user-data-dir")
        # A malformed command mentioning this exact profile is not safe to ignore.
        if str(profile) not in values:
            if str(profile) in command:
                fail("browser_unverified")
            continue
        if len(values) != 1:
            fail("browser_unverified")
        if re.search(r"(?:^|\s)--type(?:=|\s)", command):
            continue
        executable = checked_process(["/bin/ps", "-p", str(pid), "-o", "comm="]).stdout.strip()
        if (Path(executable).name not in {"Google Chrome", "Google Chrome for Testing", "Chromium"}
                or not command.startswith(executable + " ")):
            fail("browser_unverified")
        started = checked_process(["/bin/ps", "-p", str(pid), "-o", "lstart="]).stdout.strip()
        if not started:
            fail("browser_unverified")
        found.append({"pid": pid, "started": started, "executable": executable,
                      "headed": not bool(re.search(r"(?:^|\s)--headless(?:[=\s]|$)", command)),
                      "features": flag_values(command, "--enable-features")})
    if len(found) > 1:
        fail("browser_unverified")
    return found


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def cdp_json(port: int, path: str) -> Any:
    # Fixed loopback address, no inherited proxy, no redirect or credential output.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open("http://127.0.0.1:" + str(port) + path, timeout=3) as response:
            data = response.read(2_000_001)
        if len(data) > 2_000_000:
            fail("browser_unverified")
        return json.loads(data)
    except (OSError, ValueError):
        fail("browser_unverified")


def verify_listener(pid: int, port: int) -> None:
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    text = checked_process([lsof, "-nP", "-iTCP:" + str(port),
                            "-sTCP:LISTEN", "-Fpn"], accepted=(0, 1)).stdout
    owners, addresses = [], []
    for line in text.splitlines():
        if line.startswith("p"):
            owners.append(line[1:])
        elif line.startswith("n"):
            addresses.append(line[1:])
    if owners != [str(pid)] or not addresses:
        fail("browser_unverified")
    for address in addresses:
        try:
            host, actual_port = address.rsplit(":", 1)
        except ValueError:
            fail("browser_unverified")
        if actual_port != str(port) or not loopback(host.strip("[]")):
            fail("browser_unverified")


def browser_identity(browser: dict[str, Any]) -> dict[str, Any]:
    return {key: browser[key] for key in ("pid", "started", "executable", "port", "endpoint_hash", "headed")}


def verify_browser(project: Project, expected: dict[str, Any] | None = None) -> dict[str, Any] | None:
    processes = discover_processes(project.profile)
    if not processes:
        return None
    process = processes[0]
    endpoint = project.profile / "DevToolsActivePort"
    reject_symlinks(endpoint)
    try:
        if endpoint.stat().st_uid != os.getuid() or endpoint.stat().st_size > 4096:
            fail("browser_unverified")
        lines = endpoint.read_text().splitlines()
        if len(lines) != 2 or not lines[0].isdigit() or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]+", lines[1]):
            fail("browser_unverified")
        port = int(lines[0])
        if not 1 <= port <= 65535:
            fail("browser_unverified")
    except (OSError, UnicodeError):
        fail("browser_unverified")
    verify_listener(process["pid"], port)
    version = cdp_json(port, "/json/version")
    if not isinstance(version, dict):
        fail("browser_unverified")
    try:
        ws = urllib.parse.urlsplit(version.get("webSocketDebuggerUrl", ""))
        if (ws.scheme != "ws" or not loopback(ws.hostname) or ws.port != port
                or ws.path != lines[1] or ws.username is not None or ws.password is not None
                or ws.query or ws.fragment):
            fail("browser_unverified")
    except ValueError:
        fail("browser_unverified")
    if not str(version.get("Browser", "")).startswith(("Chrome/", "HeadlessChrome/")):
        fail("browser_unverified")
    if not any("ForceMacVSyncTimerForDebugging" in value.split(",") for value in process["features"]):
        fail("browser_unverified")
    again = discover_processes(project.profile)
    if not again or again[0] != process:
        fail("stale_browser")
    process.update(port=port, endpoint_hash=digest(lines[1]))
    if expected is not None and browser_identity(process) != expected:
        fail("stale_browser")
    return process


def valid_target(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value))


def parse_tabs(payload: Any) -> list[dict[str, Any]]:
    """Normalize supported agent-browser tab-list JSON; reject unknown schemas."""
    if isinstance(payload, dict) and "success" in payload:
        if payload["success"] is not True:
            fail("invalid_json")
        payload = payload.get("data")
    if isinstance(payload, dict):
        payload = payload.get("tabs")
    if not isinstance(payload, list) or not payload:
        fail("invalid_json")
    tabs, ids = [], set()
    for item in payload:
        if not isinstance(item, dict) or not valid_target(item.get("targetId")) or not isinstance(item.get("url"), str):
            fail("invalid_json")
        target = item["targetId"]
        if target in ids:
            fail("invalid_json")
        ids.add(target)
        selected = [item[key] for key in ("active", "current", "selected") if key in item]
        if not selected or any(type(value) is not bool for value in selected) or len(set(selected)) != 1:
            fail("ownership_unverified")
        tabs.append({"targetId": target, "origin": origin(item["url"]), "active": selected[0]})
    if sum(tab["active"] for tab in tabs) != 1:
        fail("ownership_unverified")
    return tabs


def cdp_target_ids(browser: dict[str, Any]) -> set[str]:
    payload = cdp_json(browser["port"], "/json/list")
    if not isinstance(payload, list):
        fail("browser_unverified")
    targets = set()
    for item in payload:
        if not isinstance(item, dict) or not valid_target(item.get("id")):
            fail("browser_unverified")
        if item.get("type") == "page":
            targets.add(item["id"])
    return targets


def child_environment(project_key: str) -> dict[str, str]:
    # Strip configuration/credential-manager fallbacks; explicit empty config
    # below also prevents user/project config from silently enabling plugins.
    prefixes = ("AGENT_BROWSER_", "BW_", "BWS_", "AAC_", "BROWSER_USE_", "AI_GATEWAY_")
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(prefixes)
           and key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
    # Explicit opt-in shares controller discovery only, never project state or
    # Chrome profiles. Tasks override this default with their recorded binding.
    env["AGENT_BROWSER_SOCKET_DIR"] = str(shared_runtime_call("project_socket_directory", project_key))
    return env


def widget_path(value: str) -> bool:
    parts = value.split(" > ")
    return (len(value) <= 768 and 1 <= len(parts) <= 12 and all(
        re.fullmatch(r"(?:div|span|label|input|button|ul|ol|li|a):nth-child\(([1-9][0-9]{0,2})\)", part)
        and int(re.search(r"\(([0-9]+)\)", part)[1]) <= 256 for part in parts))


def widget_label(value: str) -> bool:
    return (1 <= len(value) <= 256 and bool(value.strip())
            and not value.startswith("-")
            and not re.search(r"[\x00-\x1f\x7f\x85\u2028\u2029]", value))


class WidgetMarkupGuard(HTMLParser):
    """Conservative credential/control screening, not a secrecy guarantee."""
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a" and (values.get("href") or "").strip().lower() not in {
                "", "#", "javascript:void(0)", "javascript:void(0);", "javascript:;"}:
            fail("command_denied")
        sensitive = r"password|passwd|secret|token|csrf|xsrf|nonce|credential|authorization|session|api[_-]?key|access[_-]?key|bearer"
        if (tag in {"script", "style", "iframe", "object", "embed", "form", "textarea"}
                or any(name.startswith("on") or re.search(sensitive, name, re.I)
                       for name, _ in attrs)
                or (tag == "input" and (
                    (values.get("type") or "text").lower() not in {"checkbox", "radio", "hidden"}
                    or any(re.search(sensitive, values.get(name) or "", re.I)
                           for name in ("name", "id", "autocomplete"))))):
            fail("command_denied")


def checked_widget_markup(value: str) -> str:
    if len(value.encode("utf-8")) > 65536:
        fail("command_denied")
    parser = WidgetMarkupGuard(convert_charrefs=True)
    parser.feed(value)
    parser.close()
    return value


def validate_run_command(args: list[str]) -> list[str]:
    """Small exact grammar: no arbitrary flags, tab controls, JS, or exports."""
    if not args or len(args) > 16 or any("\x00" in arg for arg in args):
        fail("command_denied")
    command, tail = args[0], args[1:]
    ref = lambda value: bool(re.fullmatch(r"@e[0-9]+", value))
    if command == "inspect-widget" and len(tail) == 1 and ref(tail[0]):
        return args
    if (command == "click-widget" and len(tail) == 4 and ref(tail[0])
            and widget_path(tail[1]) and tail[2] == "--expected-text"
            and widget_label(tail[3])):
        return args
    # Diagnostics stay bound to the verified task; no caller-controlled exports,
    # arbitrary JS, capture streams, or log clearing are accepted.
    if command in {"console", "errors"} and not tail:
        return args
    if command == "screenshot" and tail in ([], ["--full"]):
        return args
    if (command == "set" and len(tail) == 3 and tail[0] == "viewport"
            and all(re.fullmatch(r"[0-9]{3,4}", value) for value in tail[1:])
            and all(320 <= int(value) <= 3840 for value in tail[1:])):
        return args
    if command == "get" and (tail == ["url"] or (
            len(tail) == 3 and tail[0] == "attr" and ref(tail[1])
            and tail[2] in {"href", "src", "alt", "aria-label"})):
        return args
    if command in {"back", "forward", "reload"} and not tail:
        return args
    if command == "open" and len(tail) == 1:
        validate_url(tail[0])
        return args
    if command == "snapshot" and all(arg in {"-i", "--interactive", "-c", "--compact"} for arg in tail):
        return args
    if command in {"click", "dblclick", "hover", "focus", "check", "uncheck", "scrollintoview"} and len(tail) == 1 and ref(tail[0]):
        return args
    if command in {"fill", "type", "select"} and len(tail) == 2 and ref(tail[0]) and not tail[1].startswith("-"):
        return args
    if (command == "find" and len(tail) == 4 and tail[0] == "text"
            and tail[2:] == ["click", "--exact"]
            and 1 <= len(tail[1]) <= 256 and tail[1].strip()
            and not tail[1].startswith("-")
            and not re.search(r"[\x00-\x1f\x7f\x85\u2028\u2029]", tail[1])):
        return args
    if command == "press" and len(tail) == 1 and tail[0] in {"Enter", "Tab", "Escape", "Space", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Backspace", "Control+a", "Meta+a", "Shift+ArrowUp", "Shift+ArrowDown"}:
        return args
    if command == "scroll" and len(tail) in {1, 2} and tail[0] in {"up", "down", "left", "right"}:
        if len(tail) == 1 or (tail[1].isdigit() and 1 <= int(tail[1]) <= 10000):
            return args
    if command == "wait" and len(tail) == 1 and (ref(tail[0]) or (tail[0].isdigit() and 0 <= int(tail[0]) <= 10000)):
        return args
    if command == "get" and ((tail == ["title"]) or (len(tail) == 2 and tail[0] in {"text", "count", "box"} and (ref(tail[1]) or tail[1] == "body"))):
        return args
    if command == "is" and len(tail) == 2 and tail[0] in {"visible", "enabled", "checked"} and ref(tail[1]):
        return args
    fail("command_denied")


class Coordinator:
    def __init__(self, project: Project, store: StateStore):
        self.project, self.store = project, store
        self.state = store.load()

    def browser(self, required: bool = True) -> dict[str, Any] | None:
        browser = verify_browser(self.project, self.state["browser"])
        if browser is None and required:
            fail("stale_browser" if self.state["browser"] else "browser_absent")
        return browser

    def guard(self, task: dict[str, Any] | None = None, *,
              requested_origin: str | None = None) -> None:
        if task is not None:
            controller_migration_guard(task)
        auth = self.state["auth_task"] is not None
        if task is not None and task.get("status") == "human":
            fail("auth_lease" if auth else "human_lease")
        roots = concurrency_roots(self.project)
        if not roots:
            if auth:
                fail("auth_lease")
            return
        leases = [other for other in self.state["tasks"].values()
                  if other.get("status") == "human" or other.get("auth")]
        if not leases and not auth:
            return
        code = "auth_lease" if auth else "human_lease"
        if auth and self.state["tasks"].get(self.state["auth_task"]) not in leases:
            fail(code)
        if task is not None:
            if (not any(other is task for other in self.state["tasks"].values())
                    or task.get("status") != "active" or not valid_target(task.get("targetId"))
                    or any(other is not task and other.get("targetId") == task["targetId"]
                           for other in self.state["tasks"].values())):
                fail(code)
            candidates = [task.get("origin")]
            if requested_origin is not None:
                candidates.append(requested_origin)
        else:
            # Only attach supplies a destination; start/lifecycle stays blocked.
            candidates = [requested_origin]
        protected = {site_scope(other.get("origin"), roots) for other in leases}
        if None in protected or not protected:
            fail(code)
        for candidate in candidates:
            scope = site_scope(candidate, roots)
            if scope is None or scope in protected:
                fail(code)

    def task(self, task_id: str) -> dict[str, Any]:
        validate_task(task_id)
        task = self.state["tasks"].get(task_id)
        if task is None:
            fail("task_missing")
        return task

    def agent(self, browser: dict[str, Any], task: dict[str, Any], args: list[str]) -> Any:
        controller_migration_guard(task)
        executable = shutil.which("agent-browser")
        if not executable:
            fail("agent_browser_missing")
        environment = child_environment(self.project.key)
        environment["AGENT_BROWSER_SOCKET_DIR"] = str(
            shared_runtime_call("task_socket_directory", self.project.key, task))
        secure_directory(Path(environment["AGENT_BROWSER_SOCKET_DIR"]))
        # Persist every call while a run is unresolved, including ownership
        # postchecks. A completed page action is insufficient if a later IPC
        # request timed out. Never persist argv, response data, or raw errors.
        operation = task.get("operation")
        journal = (task.get("status") == "uncertain" and isinstance(operation, dict)
                   and operation.get("kind") == "run")
        if journal:
            operation["last_call"] = {"completed": False, "outcome": "unknown"}
            self.store.save(self.state)
        result = checked_process([executable, "--config", str(self.store.empty_config()),
                                  "--session", task["session"], "--cdp", str(browser["port"]),
                                  "--pin-tab", "--json", *args], cwd=self.store.directory,
                                 env=environment, timeout=45, accepted=(0, 1))
        try:
            value = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            fail("invalid_json")
        if not isinstance(value, (dict, list)) or not value:
            fail("invalid_json")
        # CLI-generated transport errors also use success:false. Only this
        # reviewed daemon-side code is currently accepted as completed failure;
        # unknown/new codes, untyped errors, and outcome_unknown fail closed.
        completed_error = (isinstance(value, dict) and value.get("success") is False
                           and value.get("code") == "tab_gone" and "type" not in value
                           and isinstance(value.get("error"), str)
                           and result.returncode == 1)
        completed_success = (result.returncode == 0 and (
            isinstance(value, list) or (value.get("success") is True
                                       and value.get("code") is None
                                       and value.get("error") is None
                                       and "type" not in value)))
        if journal and (completed_success or completed_error):
            operation["last_call"] = {"completed": True,
                                      "outcome": "error" if completed_error else "success"}
            self.store.save(self.state)
        if completed_error:
            fail("daemon_command_error")
        if isinstance(value, dict) and value.get("code") == "outcome_unknown":
            fail("command_outcome_unknown")
        if not completed_success:
            fail("invalid_json")
        return value

    def owned(self, browser: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        if task.get("targetId") not in cdp_target_ids(browser):
            fail("tab_gone")
        tabs = parse_tabs(self.agent(browser, task, ["tab", "list"]))
        matched = [tab for tab in tabs if tab["targetId"] == task.get("targetId")]
        if not matched:
            fail("tab_gone")
        if not matched[0]["active"]:
            fail("ownership_unverified")
        return matched[0]

    @staticmethod
    def result(task: dict[str, Any]) -> dict[str, Any]:
        result = {key: task.get(key) for key in ("status", "session", "targetId", "origin")}
        operation = task.get("operation")
        if isinstance(operation, dict):
            result["operationId"] = operation.get("id")
            receipt = operation.get("last_call")
            result["completionRecorded"] = (isinstance(receipt, dict)
                                            and receipt.get("completed") is True)
        retirement = task.get("retirement")
        if isinstance(retirement, dict):
            result["abortOutcome"] = retirement.get("outcome")
            result["closeAttempted"] = retirement.get("closeAttempted")
            result["targetAbsentVerified"] = retirement.get("targetAbsentVerified")
            result["originalOperationCompletionClaimed"] = False
        return result

    def inspect(self) -> dict[str, Any]:
        browser = self.browser(required=False)
        if browser is None and self.state["browser"] is not None:
            fail("stale_browser")
        return {"status": "verified" if browser else "not_running", "projectKey": self.project.key,
                "headed": browser["headed"] if browser else None,
                "authLease": self.state["auth_task"] is not None,
                "configuredSocketDirectory": str(shared_runtime_call(
                    "project_socket_directory", self.project.key)),
                "tasks": [{**self.result(task), "taskId": task_id,
                           "socketDirectory": str(shared_runtime_call(
                               "task_socket_directory", self.project.key, task))}
                          for task_id, task in self.state["tasks"].items()]}

    def start(self, allow_launch: bool, headed: bool, chrome: str | None) -> dict[str, Any]:
        self.guard()
        existing = self.browser(required=False)
        active = any(task["status"] != "released" for task in self.state["tasks"].values())
        if existing:
            if existing["headed"] != headed:
                fail("mode_transition_required")
            return {"status": "already_running", "headed": existing["headed"]}
        if active:
            fail("mode_transition_required")
        if self.state.get("launch_pending"):
            fail("launch_uncertain")
        if not allow_launch:
            fail("launch_approval_required")
        if any((self.project.profile / name).exists() or (self.project.profile / name).is_symlink()
               for name in ("SingletonLock", "SingletonSocket", "SingletonCookie", "DevToolsActivePort")):
            fail("browser_locked")
        executable = Path(chrome or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").expanduser()
        if executable.name not in {"Google Chrome", "Google Chrome for Testing", "Chromium"} or not executable.is_file() or not os.access(executable, os.X_OK):
            fail("browser_unverified")
        # Recheck after all preflight work, while the project lock remains held.
        if discover_processes(self.project.profile):
            fail("browser_unverified")
        self.project.profile.mkdir(mode=0o700, exist_ok=True)
        self.state["launch_pending"] = True
        self.state["browser"] = None
        self.store.save(self.state)
        args = [str(executable), "--user-data-dir=" + str(self.project.profile),
                "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
                "--enable-features=ForceMacVSyncTimerForDebugging", "--no-first-run", "--no-default-browser-check"]
        if not headed:
            args.append("--headless=new")
        args.append("about:blank")
        try:
            subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=child_environment(self.project.key),
                             start_new_session=True, close_fds=True)
        except OSError:
            fail("launch_uncertain")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                browser = verify_browser(self.project)
                if browser:
                    self.state["browser"] = browser_identity(browser)
                    self.state["launch_pending"] = False
                    self.store.save(self.state)
                    return {"status": "started", "headed": browser["headed"]}
            except BrowserSessionError:
                pass
            time.sleep(0.2)
        fail("launch_uncertain")

    def attach(self, task_id: str, url: str) -> dict[str, Any]:
        validate_task(task_id)
        validate_url(url)
        self.guard(requested_origin=origin(url))
        if task_id in self.state["tasks"]:
            fail("task_exists")
        browser = self.browser()
        before = cdp_target_ids(browser)
        task = {"session": session_name(self.project.key, task_id), "status": "attaching",
                "targetId": None, "origin": origin(url), "auth": False,
                "socketDir": str(shared_runtime_call("project_socket_directory", self.project.key))}
        self.state["browser"] = browser_identity(browser)
        self.state["tasks"][task_id] = task
        self.store.save(self.state)  # journal intent before any pinned attachment
        try:
            self.agent(browser, task, ["open", url])
            self.browser()
            after = cdp_target_ids(browser)
            new = after - before
            tabs = parse_tabs(self.agent(browser, task, ["tab", "list"]))
            selected = [tab for tab in tabs if tab["active"]]
            if len(new) != 1 or len(selected) != 1 or selected[0]["targetId"] not in new:
                fail("ownership_unverified")
            target = selected[0]["targetId"]
            if any(other.get("targetId") == target for key, other in self.state["tasks"].items() if key != task_id):
                fail("ownership_unverified")
            task.update(targetId=target, origin=selected[0]["origin"], status="active")
            self.guard(task)
            self.store.save(self.state)
            return self.result(task)
        except BaseException:
            task["status"] = "uncertain"
            self.store.save(self.state)
            raise

    def widget_command(self, browser: dict[str, Any], task: dict[str, Any],
                       args: list[str]) -> Any:
        """Internal bounded expansion; every IPC uses ordinary run journaling."""
        def query(command, field, expected_type):
            response = self.agent(browser, task, command)
            data = response.get("data") if isinstance(response, dict) else None
            value = data.get(field) if isinstance(data, dict) else None
            if (not isinstance(response, dict) or response.get("success") is not True
                    or type(value) is not expected_type):
                fail("invalid_json")
            return value

        ref = args[1]
        role = query(["get", "attr", ref, "role"], "value", str)
        if role not in {"listbox", "tree", "menu"}:
            fail("command_denied")
        widget_id = query(["get", "attr", ref, "id"], "value", str)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,127}", widget_id):
            fail("command_denied")
        root = "#" + widget_id
        # Unique safe element type, not body/dialog/form even if role-spoofed.
        if (query(["get", "count", root], "count", int) != 1
                or query(["get", "count", f"div{root}, ul{root}, ol{root}"], "count", int) != 1
                or query(["get", "attr", root, "role"], "value", str) != role
                or not query(["is", "visible", root], "visible", bool)):
            fail("ownership_unverified")
        markup = checked_widget_markup(query(["get", "html", root], "html", str))
        if args[0] == "inspect-widget":
            return {"widgetId": widget_id, "role": role, "html": markup}
        selector = root + " > " + args[2]
        if (query(["get", "count", selector], "count", int) != 1
                or " ".join(query(["get", "text", selector], "text", str).split())
                   != " ".join(args[4].split())
                or not query(["is", "visible", selector], "visible", bool)
                or query(["get", "attr", ref, "id"], "value", str) != widget_id
                or query(["get", "attr", ref, "role"], "value", str) != role
                or query(["get", "count", selector], "count", int) != 1):
            fail("ownership_unverified")
        return self.agent(browser, task, ["click", selector])

    def run(self, task_id: str, args: list[str]) -> Any:
        validate_run_command(args)
        task = self.task(task_id)
        self.guard(task, requested_origin=origin(args[1]) if args[0] == "open" else None)
        if task["status"] != "active":
            fail("task_paused")
        browser = self.browser()
        tab = self.owned(browser, task)
        self.guard(task, requested_origin=tab["origin"])
        task["status"] = "uncertain"
        task["operation"] = {"id": secrets.token_hex(16), "kind": "run",
                             "browser": browser_identity(browser),
                             "session": task["session"], "targetId": task["targetId"],
                             "last_call": {"completed": False, "outcome": "unknown"}}
        self.store.save(self.state)
        # If interrupted/timed out, the durable uncertainty prevents silent replay.
        result = (self.widget_command(browser, task, args)
                  if args[0] in {"inspect-widget", "click-widget"}
                  else self.agent(browser, task, args))
        self.browser()
        tab = self.owned(browser, task)
        task.update(status="active", origin=tab["origin"])
        try:
            self.guard(task)
        except BrowserSessionError:
            task["status"] = "uncertain"
            self.store.save(self.state)
            raise
        self.store.save(self.state)
        return result

    def reconcile(self, task_id: str, operation_id: str, expected_target: str,
                  human_confirmed: bool, captures_stopped: bool) -> dict[str, Any]:
        """Receipt-backed uncertain -> human only; never resume/replay a command.

        No probe may manufacture completion evidence for a legacy/unknown run.
        Holding the project lock excludes cooperating helper calls, not raw
        controllers: host approval must also establish exclusive task control.
        """
        if not human_confirmed or not captures_stopped:
            fail("confirmation_required")
        task = self.task(task_id)
        if task.get("retirement") is not None:
            fail("retirement_recorded")
        # Conservative recovery boundary: no cross-site lease exception here.
        if self.state["auth_task"] is not None or any(
                other.get("auth") for other in self.state["tasks"].values()):
            fail("auth_lease")
        if any(other.get("status") == "human" for other in self.state["tasks"].values()):
            fail("human_lease")
        if task["status"] != "uncertain":
            fail("reconciliation_required")
        operation = task.get("operation")
        receipt = operation.get("last_call") if isinstance(operation, dict) else None
        if (not isinstance(operation, dict) or operation.get("kind") != "run"
                or not isinstance(operation_id, str)
                or not re.fullmatch(r"[a-f0-9]{32}", operation_id)
                or operation.get("id") != operation_id
                or not isinstance(operation.get("browser"), dict)
                or operation.get("browser") != self.state["browser"]
                or operation.get("session") != task["session"]
                or not valid_target(expected_target)
                or operation.get("targetId") != expected_target
                or task.get("targetId") != expected_target
                or not isinstance(receipt, dict)
                or set(receipt) != {"completed", "outcome"}
                or receipt.get("completed") is not True
                or receipt.get("outcome") not in ("error", "success")):
            fail("completion_unproven")
        if any(other is not task and other.get("targetId") == expected_target
               for other in self.state["tasks"].values()):
            fail("ownership_unverified")
        # Only after durable completion proof do ordinary binding checks run.
        # Their own requests overwrite last_call with pending before sending;
        # a timeout leaves this task uncertain and proof unavailable again.
        browser = self.browser()
        tab = self.owned(browser, task)
        self.browser()
        task.update(status="human", auth=False, origin=tab["origin"])
        self.store.save(self.state)
        return {**self.result(task), "captureDeclarationOnly": True,
                "resumeRequired": True}

    def retire(self, task_id: str, expected_target: str, human_confirmed: bool,
               discard_unsaved: bool) -> dict[str, Any]:
        """Explicitly abandon one uncertain target, not its unknown outcome.

        Host approval must authorize discarding this target's unsaved content.
        This operation neither proves nor retries the original command. A
        journaled attempt is never automatically repeated, even after timeout.
        No new task/tab is opened; a successful tombstone reserves the old ID.
        """
        if not human_confirmed or not discard_unsaved:
            fail("confirmation_required")
        task = self.task(task_id)
        if task.get("retirement") is not None:
            fail("retirement_recorded")
        if self.state["auth_task"] is not None or any(
                other.get("auth") for other in self.state["tasks"].values()):
            fail("auth_lease")
        if any(other.get("status") == "human" for other in self.state["tasks"].values()):
            fail("human_lease")
        if task["status"] != "uncertain":
            fail("reconciliation_required")
        if (not valid_target(expected_target) or task.get("targetId") != expected_target
                or any(other is not task and other.get("targetId") == expected_target
                       for other in self.state["tasks"].values())):
            fail("ownership_unverified")
        self.guard(task)  # Validate project policy; no lease exception above.
        if not isinstance(self.state["browser"], dict):
            fail("browser_unverified")
        browser = self.browser()
        present = expected_target in cdp_target_ids(browser)
        retirement = {"expectedTarget": expected_target, "session": task["session"],
                      "browser": browser_identity(browser),
                      "humanConfirmed": True, "discardUnsaved": True,
                      "targetPresentBefore": present, "closeAttempted": False,
                      "targetAbsentVerified": False,
                      "originalOperationCompletionClaimed": False,
                      "outcome": "pending"}
        # Use existing releasing status so interrupted/older readers also block
        # page commands, handoff, cleanup, and any accidental stable-ID reuse.
        # The original operation/receipt is deliberately left untouched.
        task.update(status="releasing", retirement=retirement)
        self.store.save(self.state)
        try:
            if present:
                tab = self.owned(browser, task)
                if tab["origin"] != task.get("origin"):
                    fail("ownership_unverified")
                self.browser()
                retirement["closeAttempted"] = True
                self.store.save(self.state)  # Intent before the one close call.
                self.agent(browser, task, ["tab", "close", expected_target])
            self.browser()
            if expected_target in cdp_target_ids(browser):
                fail("ownership_unverified")
            retirement.update(targetAbsentVerified=True,
                              outcome="aborted_by_user_no_completion_claim")
            task["status"] = "released"
            self.store.save(self.state)
            return self.result(task)
        except BaseException:
            # Persist bounded uncertainty on all interruptions, then re-raise
            # the original error. Never fall back to raw CDP, another session,
            # daemon shutdown, or a second attempt to close this target.
            task["status"] = "uncertain"
            retirement["outcome"] = "unknown"
            self.store.save(self.state)
            raise

    def handoff(self, task_id: str, auth: bool, captures_stopped: bool) -> dict[str, Any]:
        if not captures_stopped:
            fail("confirmation_required")
        task = self.task(task_id)
        # State v1 has one auth owner; never overwrite another human's lease.
        if auth and self.state["auth_task"] is not None:
            fail("auth_lease")
        self.guard(task)
        unresolved = {"attaching", "uncertain", "releasing"}
        if task["status"] in unresolved or (auth and any(
                other["status"] in unresolved for other in self.state["tasks"].values())):
            fail("reconciliation_required")
        if task["status"] not in {"active", "paused"} or not task.get("targetId"):
            fail("task_paused")
        browser = self.browser()
        tab = self.owned(browser, task)
        self.guard(task, requested_origin=tab["origin"])
        task.update(status="human", auth=auth, origin=tab["origin"])
        if auth:
            self.state["auth_task"] = task_id
        self.store.save(self.state)
        return {**self.result(task), "authLease": auth,
                "viewerStatus": "viewer_required", "socketDirectory": str(shared_runtime_call("task_socket_directory", self.project.key, task)),
                "instructions": "Use an existing local dashboard only after verifying it discovers this namespace/session. Otherwise coordinate a task-owned viewer separately. Keep captures stopped; return for explicit resume. No viewer URL or privacy guarantee is verified.",
                "captureDeclarationOnly": True}

    def viewer(self, task_id: str, allow_start: bool, port: int | None = None) -> dict[str, Any]:
        """Prepare only this leased session's stream; no browser/dashboard launch.

        The normal agent subprocess path supplies the sanitized environment. The
        dashboard itself still needs separately coordinated setup/qualification.
        """
        task = self.task(task_id)
        if task["status"] != "human":
            fail("human_lease")
        if self.state["auth_task"] not in (None, task_id):
            fail("auth_lease")
        browser = self.browser()
        tab = self.owned(browser, task)
        data = {**self.result(task), "origin": tab["origin"],
                "viewerStatus": "viewer_required", "socketDirectory": str(shared_runtime_call("task_socket_directory", self.project.key, task)),
                "serviceStarted": False,
                "instructions": "Coordinate a local dashboard that discovers this namespace/session. Stream preparation does not verify human connection, dashboard privacy, or successful authentication."}
        if not allow_start:
            if port is not None:
                fail("confirmation_required")
            return data
        if type(port) is not int or not 1024 <= port <= 65535 or port == browser["port"]:
            fail("invalid_arguments")
        lsof = shutil.which("lsof") or "/usr/sbin/lsof"
        probe = [lsof, "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN", "-Fpn"]
        if checked_process(probe, accepted=(0, 1)).stdout.strip():
            fail("browser_unverified")
        self.agent(browser, task, ["stream", "enable", "--port", str(port)])
        self.browser()
        self.owned(browser, task)
        listener = checked_process(probe, accepted=(0, 1)).stdout
        owners = [line[1:] for line in listener.splitlines() if line.startswith("p")]
        addresses = [line[1:] for line in listener.splitlines() if line.startswith("n")]
        if len(owners) != 1 or not owners[0].isdigit() or not addresses:
            fail("browser_unverified")
        for address in addresses:
            host, separator, actual_port = address.rpartition(":")
            if not separator or actual_port != str(port) or not loopback(host.strip("[]")):
                fail("browser_unverified")
        data.update(viewerStatus="stream_ready_dashboard_required", serviceStarted=True,
                    streamPort=port, listenerScope="loopback_only")
        return data

    def resume(self, task_id: str, human_confirmed: bool, account_verified: bool) -> dict[str, Any]:
        task = self.task(task_id)
        if task["status"] != "human":
            fail("task_paused")
        if not human_confirmed or (task.get("auth") and not account_verified):
            fail("confirmation_required")
        if self.state["auth_task"] not in (None, task_id):
            fail("auth_lease")
        browser = self.browser()
        tab = self.owned(browser, task)
        task.update(status="active", auth=False, origin=tab["origin"])
        if self.state["auth_task"] == task_id:
            self.state["auth_task"] = None
        self.store.save(self.state)
        return {**self.result(task), "accountDeclarationOnly": bool(account_verified)}

    def release(self, task_id: str, outcome: str) -> dict[str, Any]:
        task = self.task(task_id)
        self.guard(task)
        if outcome == "failure":
            if task["status"] == "released":
                fail("task_paused")
            if task["status"] in {"attaching", "uncertain", "releasing"}:
                fail("reconciliation_required")
            task["status"] = "paused"
            self.store.save(self.state)
            return self.result(task)
        if outcome != "success" or task["status"] != "active":
            fail("task_paused")
        browser = self.browser()
        tab = self.owned(browser, task)
        self.guard(task, requested_origin=tab["origin"])
        task["status"] = "releasing"
        self.store.save(self.state)
        self.agent(browser, task, ["tab", "close", task["targetId"]])
        self.browser()
        if task["targetId"] in cdp_target_ids(browser):
            fail("ownership_unverified")
        task["status"] = "released"  # tombstone prevents stable session reuse/rebinding
        self.store.save(self.state)
        return self.result(task)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        fail("invalid_arguments")


def build_parser() -> argparse.ArgumentParser:
    parser = SafeParser(description=__doc__)
    parser.add_argument("--project", help="Explicit project root; defaults to current Git checkout")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect", aliases=["status"])
    start = commands.add_parser("start")
    start.add_argument("--allow-launch", action="store_true", help="Accident guard, not proof of human consent")
    start.add_argument("--headed", action="store_true")
    start.add_argument("--chrome")
    attach = commands.add_parser("attach")
    attach.add_argument("--task", required=True)
    attach.add_argument("--url", required=True)
    run = commands.add_parser("run")
    run.add_argument("--task", required=True)
    run.add_argument("args", nargs=argparse.REMAINDER)
    reconcile = commands.add_parser("reconcile", help="Receipt-backed uncertain task handoff; never replays")
    reconcile.add_argument("--task", required=True)
    reconcile.add_argument("--operation-id", required=True)
    reconcile.add_argument("--expected-target", required=True)
    reconcile.add_argument("--human-confirmed", action="store_true")
    reconcile.add_argument("--captures-stopped", action="store_true")
    retire = commands.add_parser("retire", help="User-authorized discard of one uncertain target; no completion claim")
    retire.add_argument("--task", required=True)
    retire.add_argument("--expected-target", required=True)
    retire.add_argument("--human-confirmed", action="store_true")
    retire.add_argument("--discard-unsaved", action="store_true")
    handoff = commands.add_parser("handoff")
    handoff.add_argument("--task", required=True)
    handoff.add_argument("--auth", action="store_true")
    handoff.add_argument("--captures-stopped", action="store_true")
    viewer = commands.add_parser("viewer", help="Inspect handoff or prepare only its loopback stream")
    viewer.add_argument("--task", required=True)
    viewer.add_argument("--allow-start", action="store_true", help="Approved stream preparation only, not browser/dashboard launch")
    viewer.add_argument("--port", type=int, help="Explicit approved unused loopback stream port")
    resume = commands.add_parser("resume")
    resume.add_argument("--task", required=True)
    resume.add_argument("--human-confirmed", action="store_true")
    resume.add_argument("--account-verified", action="store_true")
    release = commands.add_parser("release")
    release.add_argument("--task", required=True)
    release.add_argument("--outcome", choices=("success", "failure"), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if sys.platform != "darwin":
            fail("unsupported_platform")
        project = resolve_project(args.project)
        check_profile(project)
        store = StateStore(DEFAULT_STATE_DIR, project)
        with store.lock():
            check_profile(project)
            coordinator = Coordinator(project, store)
            if args.command in {"inspect", "status"}:
                result = coordinator.inspect()
            elif args.command == "start":
                result = coordinator.start(args.allow_launch, args.headed, args.chrome)
            elif args.command == "attach":
                result = coordinator.attach(args.task, args.url)
            elif args.command == "run":
                command = args.args[1:] if args.args[:1] == ["--"] else args.args
                result = coordinator.run(args.task, command)
            elif args.command == "reconcile":
                result = coordinator.reconcile(args.task, args.operation_id, args.expected_target,
                                               args.human_confirmed, args.captures_stopped)
            elif args.command == "retire":
                result = coordinator.retire(args.task, args.expected_target,
                                            args.human_confirmed, args.discard_unsaved)
            elif args.command == "handoff":
                result = coordinator.handoff(args.task, args.auth, args.captures_stopped)
            elif args.command == "viewer":
                result = coordinator.viewer(args.task, args.allow_start, args.port)
            elif args.command == "resume":
                result = coordinator.resume(args.task, args.human_confirmed, args.account_verified)
            else:
                result = coordinator.release(args.task, args.outcome)
        print(json.dumps({"success": True, "data": result}, ensure_ascii=True))
        return 0
    except BrowserSessionError as error:
        print(json.dumps({"success": False, "error": {"code": error.code, "message": error.message}}))
        return 1
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, UnicodeError):
        print(json.dumps({"success": False, "error": {"code": "command_failed", "message": MESSAGES["command_failed"]}}))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"success": False, "error": {"code": "command_timeout", "message": MESSAGES["command_timeout"]}}))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
