#!/usr/bin/env python3
"""One-time paused controller migration; never migrates profiles or resumes work.

All approval flags are accident guards; the host must separately obtain approval.
No raw command output, page URL, credentials, or CDP endpoint is returned. Before
production use, qualify the installed agent-browser source AND disposable tests:
external `close` must only disconnect/unpin (not Browser.close, Target.closeTarget,
or kill); fresh pinned attach must create one blank and support tab <targetId>.
The qualification gate deliberately defaults closed; there is no CLI bypass.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import shutil
import subprocess
import tempfile
import json
import os
from pathlib import Path
import re
import stat
import sys


# Compatibility constant only: never authorizes production migration. Evidence
# from the approved disposable harness is required for the installed binary.
# Tests can patch qualification_verified without touching real runtime state.
SOURCE_TEST_QUALIFIED = False
core = None
runtime = None
MESSAGES = {
    "invalid_arguments": "Invalid migration arguments; values suppressed.",
    "unsupported_platform": "Controller migration requires macOS.",
    "confirmation_required": "Host approval and all migration declarations are required.",
    "qualification_required": "Installed controller source and disposable tests need qualification.",
    "task_missing": "Requested task does not exist.",
    "task_checkpoint_required": "Only human or paused task checkpoints can migrate.",
    "project_unsettled": "Another project task is active or unsettled.",
    "migration_pending": "Existing migration marker requires manual reconciliation; do not retry.",
    "policy_required": "Project must be an approved member of the configured shared root.",
    "unsafe_path": "Controller directories must be short, private, owned, and non-symlinked.",
    "destination_occupied": "Destination already has session sidecars; do not adopt or remove them.",
    "source_unverified": "Legacy controller binding could not be verified.",
    "browser_unverified": "Project profile or external browser identity could not be verified.",
    "targets_changed": "Pre-existing page targets changed; preserve state for manual review.",
    "attachment_unverified": "Fresh pinned blank or original target selection could not be verified.",
    "migration_uncertain": "Migration outcome is uncertain; preserve the checkpoint and do not retry.",
    "state_unverified": "Migration state could not be verified; preserve it for review.",
}


class MigrationError(Exception):
    def __init__(self, code):
        self.code = code if code in MESSAGES else "state_unverified"
        super().__init__(MESSAGES[self.code])


def fail(code):
    raise MigrationError(code)


def _load(name, filename):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


def _modules():
    global core, runtime
    if core is None:
        core = _load("_controller_migration_core", "browser_session.py")
    if runtime is None:
        runtime = _load("_controller_migration_runtime", "shared_runtime.py")
    return core, runtime


def receipt_path(home=None):
    """Fixed standard location; optional HOME is for the trusted fixture harness."""
    base = Path.home() if home is None else Path(home)
    if not base.is_absolute() or base.resolve(strict=True) != base:
        raise ValueError("Unsafe evidence home")
    return base / "Library" / "Application Support" / "browser-automation" / "controller-migration-qualification.json"


def _receipt_schema(value):
    fields = {"version", "passed", "agentBrowserPath", "agentBrowserSha256",
              "agentBrowserVersion", "chromeVersion"}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value["version"]) is not int or value["version"] != 1
            or value["passed"] is not True):
        raise ValueError("Invalid evidence")
    for key in fields - {"version", "passed"}:
        if (not isinstance(value[key], str) or not value[key]
                or len(value[key]) > 4096 or any(ord(ch) < 32 for ch in value[key])):
            raise ValueError("Invalid evidence")
    path = Path(value["agentBrowserPath"])
    if (not path.is_absolute() or str(path.resolve(strict=True)) != str(path)
            or not path.is_file() or not os.access(path, os.X_OK)
            or not re.fullmatch(r"[a-f0-9]{64}", value["agentBrowserSha256"])
            or not value["chromeVersion"].startswith(("Chrome/", "HeadlessChrome/"))):
        raise ValueError("Invalid evidence")
    return value


def _no_symlinks(path):
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Unsafe evidence path")


def qualification_metadata():
    """Isolated version query: ephemeral private files only, never services/pages.

    HOME, cwd and explicit config all point into a new disposable sandbox;
    stdout/stderr remain captured and are never printed.
    """
    executable = shutil.which("agent-browser")
    if not executable:
        raise ValueError("Missing executable")
    path = Path(executable).resolve(strict=True)
    node = shutil.which("node")
    directories = [str(path.parent)]
    if node:
        directories.append(str(Path(node).resolve(strict=True).parent))
    directories.extend(["/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    # agent-browser parses flags/TLS configuration BEFORE handling --version.
    # Isolate HOME, cwd AND explicit config even for this harmless query; never
    # allow user/project config fallbacks, inherited plugins or TLS settings.
    with tempfile.TemporaryDirectory(prefix="browser-qualification-metadata-") as temporary:
        sandbox = Path(temporary).resolve(strict=True)
        os.chmod(sandbox, 0o700)
        config = sandbox / "empty.json"
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("{}\n")
        env = {"HOME": str(sandbox), "PATH": os.pathsep.join(dict.fromkeys(directories)),
               "TMPDIR": str(sandbox), "LANG": "C", "LC_ALL": "C",
               "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([str(path), "--config", str(config), "--version"],
                                cwd=sandbox, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=5, check=True)
        version = result.stdout.strip()
    if (not version or len(version) > 4096 or any(ord(ch) < 32 for ch in version)
            or hashlib.sha256(path.read_bytes()).hexdigest() != before):
        raise ValueError("Invalid version")
    return {"agentBrowserPath": str(path), "agentBrowserSha256": before,
            "agentBrowserVersion": version}


def qualification_verified():
    """Private evidence is an accident guard, not a same-user security boundary."""
    try:
        path = receipt_path()
        _no_symlinks(path)
        # Missing evidence returns before any subprocess or executable lookup.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or info.st_size > 16384):
                return False
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate evidence")
                    result[key] = value
                return result
            value = _receipt_schema(json.loads(stream.read(16385), object_pairs_hook=unique))
        current = qualification_metadata()
        return all(value[key] == current[key] for key in current)
    except (OSError, ValueError, RuntimeError, UnicodeError, RecursionError,
            subprocess.SubprocessError):
        return False


def record_qualification(info, *, home=None):
    """Trusted disposable harness only, after assertions AND verified cleanup.

    No CLI accepts supplied proof. Host approval must include this one real-home
    evidence write; same-user forgery is not prevented by this guard.
    """
    value = _receipt_schema(info)
    current = qualification_metadata()
    if any(value[key] != current[key] for key in current):
        raise ValueError("Executable changed")
    path = receipt_path(home)
    _no_symlinks(path)
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    parent_info = path.parent.lstat()
    if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid()
            or stat.S_IMODE(parent_info.st_mode) != 0o700):
        raise ValueError("Unsafe evidence directory")
    if path.exists():
        existing = path.lstat()
        if (not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid()
                or stat.S_IMODE(existing.st_mode) != 0o600 or existing.st_nlink != 1):
            raise ValueError("Unsafe existing evidence")
    fd, temporary = tempfile.mkstemp(prefix=".qualification-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _no_symlinks(path)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _private_root(value, *, required=False):
    path = Path(value)
    if (not path.is_absolute() or str(path) != str(value)
            or len(os.fsencode(path)) > 52 or path.name in {".", ".."}):
        fail("unsafe_path")
    for component in (path, *path.parents):
        if component.is_symlink():
            fail("unsafe_path")
    # Parents above the controller base may legitimately be shared system paths.
    for component in (path.parent, path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if required or component == path.parent:
                fail("unsafe_path")
            continue
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            fail("unsafe_path")
    return path


def _unoccupied(root, session):
    for suffix in (".pid", ".sock", ".target", ".stream"):
        path = root / (session + suffix)
        if path.exists() or path.is_symlink():
            fail("destination_occupied")


def _source_sidecars(root, session):
    """Never let an ownership probe silently start a fresh source controller."""
    for suffix, kind in ((".pid", stat.S_ISREG), (".sock", stat.S_ISSOCK),
                         (".target", stat.S_ISREG)):
        path = root / (session + suffix)
        try:
            info = path.lstat()
        except OSError:
            fail("source_unverified")
        if (not kind(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            fail("source_unverified")
    # Read only the PID, never copy sidecars or trust them as browser identity.
    fd = os.open(root / (session + ".pid"), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        value = os.read(fd, 32).decode("ascii").strip()
    finally:
        os.close(fd)
    if not re.fullmatch(r"[1-9][0-9]{0,9}", value):
        fail("source_unverified")
    c, _ = _modules()
    process = c.checked_process(["/bin/ps", "-p", value, "-o", "pid="], timeout=5)
    if process.stdout.strip() != value:
        fail("source_unverified")


def _shadow(task, root):
    value = copy.deepcopy(task)
    value.pop("controllerMigration", None)
    value["socketDir"] = str(root)
    return value


def _preflight(coordinator, task_id):
    c, r = _modules()
    c.validate_task(task_id)
    task = coordinator.state["tasks"].get(task_id)
    if not isinstance(task, dict):
        fail("task_missing")
    if "controllerMigration" in task:
        fail("migration_pending")
    if task.get("status") not in {"human", "paused"}:
        fail("task_checkpoint_required")
    if (not c.valid_target(task.get("targetId"))
            or task.get("session") != c.session_name(coordinator.project.key, task_id)):
        fail("source_unverified")
    for key, other in coordinator.state["tasks"].items():
        if key == task_id:
            continue
        if (other.get("status") not in {"released", "paused", "human"}
                or "controllerMigration" in other):
            fail("project_unsettled")
        if other.get("status") != "released" and other.get("targetId") == task["targetId"]:
            fail("source_unverified")
    policy = r.read_policy()
    if not isinstance(policy, dict) or coordinator.project.key not in policy.get("projects", []):
        fail("policy_required")
    new = _private_root(r.project_socket_directory(coordinator.project.key))
    if str(new) != policy.get("socketDirectory"):
        fail("policy_required")
    old = _private_root(r.task_socket_directory(coordinator.project.key, task), required=True)
    legacy = Path(r.legacy_socket_directory(coordinator.project.key))
    if old != new and old != legacy:
        fail("source_unverified")
    if old != new:
        _unoccupied(new, task["session"])
    return task, old, new


def _identity(coordinator, expected=None):
    c, _ = _modules()
    c.check_profile(coordinator.project)
    browser = coordinator.browser()
    identity = c.browser_identity(browser)
    if (coordinator.state.get("browser") != identity
            or (expected is not None and identity != expected)):
        fail("browser_unverified")
    return browser, identity


def _preserved(coordinator, expected, before):
    c, _ = _modules()
    browser, _ = _identity(coordinator, expected)
    targets = c.cdp_target_ids(browser)
    if not before <= targets:
        fail("targets_changed")
    return browser, targets


def _plan_locked(coordinator, task_id):
    c, _ = _modules()
    c.validate_task(task_id)
    output = {"status": "blocked", "projectRoot": str(coordinator.project.root),
              "task": task_id, "session": None, "fromSocketDir": None,
              "toSocketDir": None, "targetId": None, "origin": "opaque",
              "blockers": [], "noops": [], "requiresHostApproval": True,
              "requiresHumanResume": True, "createsServices": False}
    try:
        task, old, new = _preflight(coordinator, task_id)
        output.update(session=task["session"], fromSocketDir=str(old),
                      toSocketDir=str(new), targetId=task["targetId"],
                      origin=c.origin(task.get("origin")))
        if old == new:
            output.update(status="no_change", noops=["already_shared"])
            return output
        if sys.platform != "darwin":
            output["blockers"].append("unsupported_platform")
        if not qualification_verified():
            output["blockers"].append("qualification_required")
        # Plan never invokes agent/owned: a stale daemon probe can attach/create a tab.
        output["status"] = "blocked" if output["blockers"] else "planned"
        output["executionChecks"] = ["profile_browser_identity", "source_ownership",
                                     "page_target_preservation", "fresh_pinned_blank"]
    except MigrationError as error:
        output["blockers"].append(error.code)
    except Exception:
        output["blockers"].append("state_unverified")
    return output


def plan_migration(coordinator, task_id):
    """Read-only plan: no controller/CDP commands, services, or migration journals."""
    try:
        with coordinator.store.lock():
            coordinator.state = coordinator.store.load()
            return _plan_locked(coordinator, task_id)
    except MigrationError:
        raise
    except Exception:
        raise MigrationError("state_unverified") from None


def migrate(coordinator, task_id, *, allow_migrate=False, human_paused=False,
            captures_stopped=False):
    """Hold the complete project lock; never release a human/authentication lease."""
    if not (allow_migrate is True and human_paused is True and captures_stopped is True):
        fail("confirmation_required")
    if sys.platform != "darwin":
        fail("unsupported_platform")
    try:
        with coordinator.store.lock():
            coordinator.state = coordinator.store.load()
            task, old, new = _preflight(coordinator, task_id)
            if old == new:
                return {"status": "no_change", "reason": "already_shared",
                        "requiresHumanResume": True}
            if not qualification_verified():
                fail("qualification_required")
            return _execute_locked(coordinator, task, old, new)
    except MigrationError:
        raise
    except Exception:
        raise MigrationError("state_unverified") from None


def _execute_locked(coordinator, task, old, new):
    c, _ = _modules()
    browser, identity = _identity(coordinator)
    original = copy.deepcopy(task)
    auth_task = coordinator.state.get("auth_task")
    old_shadow, new_shadow = _shadow(task, old), _shadow(task, new)
    before = c.cdp_target_ids(browser)
    if task["targetId"] not in before:
        fail("source_unverified")
    # Ownership proof must precede disconnect; never retarget the old session.
    _source_sidecars(old, task["session"])
    selected = coordinator.owned(browser, old_shadow)
    if selected.get("targetId") != task["targetId"] or selected.get("active") is not True:
        fail("source_unverified")
    _preserved(coordinator, identity, before)
    _unoccupied(new, task["session"])
    marker = {"status": "in_progress", "phase": "disconnect_source",
              "fromSocketDir": str(old), "toSocketDir": str(new),
              "targetId": task["targetId"]}
    task["controllerMigration"] = marker
    try:
        coordinator.store.save(coordinator.state)  # Intent before any disconnect.
        coordinator.agent(browser, old_shadow, ["close"])
        browser, after_close = _preserved(coordinator, identity, before)
        if after_close != before:
            fail("targets_changed")
        marker["phase"] = "attach_destination"
        coordinator.store.save(coordinator.state)
        _unoccupied(new, task["session"])
        tabs = c.parse_tabs(coordinator.agent(browser, new_shadow, ["tab", "list"]))
        browser, after_attach = _preserved(coordinator, identity, before)
        added = after_attach - before
        if len(added) == 1:
            marker["temporaryTargetId"] = next(iter(added))
            coordinator.store.save(coordinator.state)
        active = [tab for tab in tabs if tab["active"]]
        if (len(added) != 1 or len(active) != 1
                or active[0]["targetId"] not in added or active[0]["origin"] != "opaque"):
            fail("attachment_unverified")
        blank = marker["temporaryTargetId"]
        # Require literal about:blank via bounded CDP data; opaque alone is not proof.
        pages = c.cdp_json(browser["port"], "/json/list")
        if not isinstance(pages, list) or sum(
                isinstance(page, dict) and page.get("id") == blank
                and page.get("type") == "page" and page.get("url") == "about:blank"
                for page in pages) != 1:
            fail("attachment_unverified")
        marker["phase"] = "select_original"
        coordinator.store.save(coordinator.state)
        coordinator.agent(browser, new_shadow, ["tab", task["targetId"]])
        coordinator.owned(browser, new_shadow)
        browser, _ = _preserved(coordinator, identity, before)
        marker["phase"] = "close_temporary_blank"
        coordinator.store.save(coordinator.state)
        coordinator.agent(browser, new_shadow, ["tab", "close", blank])
        browser, final_targets = _preserved(coordinator, identity, before)
        if final_targets != before:
            fail("targets_changed")
        coordinator.owned(browser, new_shadow)
        _preserved(coordinator, identity, before)
        # Restore exactly the original checkpoint, including auth and operation flags.
        task.clear()
        task.update(original)
        task["socketDir"] = str(new)
        coordinator.state["auth_task"] = auth_task
        coordinator.store.save(coordinator.state)
        return {"status": "migration_complete", "session": task["session"],
                "targetId": task["targetId"], "origin": c.origin(task.get("origin")),
                "fromSocketDir": str(old), "toSocketDir": str(new),
                "requiresHumanResume": True, "authenticationVerified": False}
    except BaseException:
        # Includes interrupted/failed saves. No rollback, retry, cleanup, or broad close.
        marker.update(status="in_progress", phase="uncertain")
        task.clear()
        task.update(original)
        task["controllerMigration"] = marker
        coordinator.state["auth_task"] = auth_task
        try:
            coordinator.store.save(coordinator.state)
        except BaseException:
            pass  # Earlier durable intent still blocks; never expose raw errors.
        raise MigrationError("migration_uncertain") from None


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        fail("invalid_arguments")


def main(argv=None):
    try:
        parser = SafeParser(description=__doc__)
        parser.add_argument("--project", required=True)
        parser.add_argument("--task", required=True)
        commands = parser.add_subparsers(dest="command", required=True)
        commands.add_parser("plan")
        sub = commands.add_parser("migrate")
        for flag in ("--allow-migrate", "--human-paused", "--captures-stopped"):
            sub.add_argument(flag, action="store_true")
        args = parser.parse_args(argv)
        c, _ = _modules()
        c.validate_task(args.task)
        project = c.resolve_project(args.project)
        store = c.StateStore(c.DEFAULT_STATE_DIR, project)
        # Coordinator construction also reads state, so perform it under the lock.
        with store.lock():
            coordinator = c.Coordinator(project, store)
        if args.command == "plan":
            result = plan_migration(coordinator, args.task)
        else:
            result = migrate(coordinator, args.task, allow_migrate=args.allow_migrate,
                             human_paused=args.human_paused,
                             captures_stopped=args.captures_stopped)
        print(json.dumps({"success": True, "data": result}, ensure_ascii=True))
        return 0
    except MigrationError as error:
        code = error.code
    except Exception:
        code = "state_unverified"
    print(json.dumps({"success": False, "error": {"code": code, "message": MESSAGES[code]}}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
