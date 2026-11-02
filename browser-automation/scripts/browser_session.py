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
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator


DEFAULT_STATE_DIR = Path.home() / "Library" / "Application Support" / "browser-automation"


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
    "auth_lease": "Project authentication lease blocks all automation until explicit resume.",
    "tab_gone": "Owned target is missing; do not choose another tab.",
    "ownership_unverified": "Owned target selection could not be verified; no automatic retargeting.",
    "command_denied": "Command is outside the bounded page-command allowlist.",
    "confirmation_required": "Explicit capture/completion/account declaration is required.",
    "reconciliation_required": "An unresolved operation requires manual reconciliation before human input.",
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
        dotgit = candidate / ".git"
        if dotgit.is_dir() and dotgit.resolve() == common:
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
    env["AGENT_BROWSER_NAMESPACE"] = "ba-" + project_key
    return env


def validate_run_command(args: list[str]) -> list[str]:
    """Small exact grammar: no arbitrary flags, tab controls, JS, or exports."""
    if not args or len(args) > 16 or any("\x00" in arg for arg in args):
        fail("command_denied")
    command, tail = args[0], args[1:]
    ref = lambda value: bool(re.fullmatch(r"@e[0-9]+", value))
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
    if command == "press" and len(tail) == 1 and tail[0] in {"Enter", "Tab", "Escape", "Space", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Backspace"}:
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

    def guard(self, task: dict[str, Any] | None = None) -> None:
        if self.state["auth_task"] is not None:
            fail("auth_lease")
        if task and task["status"] == "human":
            fail("human_lease")

    def task(self, task_id: str) -> dict[str, Any]:
        validate_task(task_id)
        task = self.state["tasks"].get(task_id)
        if task is None:
            fail("task_missing")
        return task

    def agent(self, browser: dict[str, Any], task: dict[str, Any], args: list[str]) -> Any:
        executable = shutil.which("agent-browser")
        if not executable:
            fail("agent_browser_missing")
        result = checked_process([executable, "--config", str(self.store.empty_config()),
                                  "--session", task["session"], "--cdp", str(browser["port"]),
                                  "--pin-tab", "--json", *args], cwd=self.store.directory,
                                 env=child_environment(self.project.key), timeout=45)
        try:
            value = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            fail("invalid_json")
        if not isinstance(value, (dict, list)) or not value or (isinstance(value, dict) and value.get("success") is False):
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
        return {key: task.get(key) for key in ("status", "session", "targetId", "origin")}

    def inspect(self) -> dict[str, Any]:
        browser = self.browser(required=False)
        if browser is None and self.state["browser"] is not None:
            fail("stale_browser")
        return {"status": "verified" if browser else "not_running", "projectKey": self.project.key,
                "headed": browser["headed"] if browser else None,
                "authLease": self.state["auth_task"] is not None,
                "tasks": [self.result(task) for task in self.state["tasks"].values()]}

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
        self.guard()
        if task_id in self.state["tasks"]:
            fail("task_exists")
        browser = self.browser()
        before = cdp_target_ids(browser)
        task = {"session": session_name(self.project.key, task_id), "status": "attaching",
                "targetId": None, "origin": origin(url), "auth": False}
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
            self.store.save(self.state)
            return self.result(task)
        except BaseException:
            task["status"] = "uncertain"
            self.store.save(self.state)
            raise

    def run(self, task_id: str, args: list[str]) -> Any:
        validate_run_command(args)
        task = self.task(task_id)
        self.guard(task)
        if task["status"] != "active":
            fail("task_paused")
        browser = self.browser()
        self.owned(browser, task)
        task["status"] = "uncertain"
        self.store.save(self.state)
        # If interrupted/timed out, the durable uncertainty prevents silent replay.
        result = self.agent(browser, task, args)
        self.browser()
        tab = self.owned(browser, task)
        task.update(status="active", origin=tab["origin"])
        self.store.save(self.state)
        return result

    def handoff(self, task_id: str, auth: bool, captures_stopped: bool) -> dict[str, Any]:
        if not captures_stopped:
            fail("confirmation_required")
        task = self.task(task_id)
        self.guard(task)
        unresolved = {"attaching", "uncertain", "releasing"}
        if task["status"] in unresolved or (auth and any(
                other["status"] in unresolved for other in self.state["tasks"].values())):
            fail("reconciliation_required")
        if task["status"] not in {"active", "paused"} or not task.get("targetId"):
            fail("task_paused")
        browser = self.browser()
        tab = self.owned(browser, task)
        task.update(status="human", auth=auth, origin=tab["origin"])
        if auth:
            self.state["auth_task"] = task_id
        self.store.save(self.state)
        return {**self.result(task), "authLease": auth,
                "viewerStatus": "viewer_required", "namespace": "ba-" + self.project.key,
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
                "viewerStatus": "viewer_required", "namespace": "ba-" + self.project.key,
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
        self.owned(browser, task)
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
