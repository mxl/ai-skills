#!/usr/bin/env python3
"""Approved disposable migration qualification, not dashboard/network qualification.

Review before running. --allow-browser-launch is only an accident guard: the host
must separately approve a new HEADLESS fixture Chrome, loopback HTTP fixture,
fixture controller processes, private ephemeral metadata-query directories, and
(only after complete success/cleanup) one real HOME qualification receipt. Each
version query uses its own private HOME/cwd and explicit empty config, then removes
that metadata sandbox. No real project, Chrome profile, or policy is used.
--diagnose-bootstrap additionally separates the first pinned blank bootstrap from
fixture navigation. It requires the same launch approval and NEVER writes a
qualification receipt, including on complete success.
--observe-daemon-response additionally replaces the first fixture open CLI with
one verified fixture Unix-socket request and a bounded 70-second response read.
It requires both flags above; no daemon operation timeout or runtime is changed.
Every fixture Chrome launch uses basic password storage and a mock keychain only
for its disposable profile; this does not establish the cause of a stalled open.
Fixture scratch bytes are deliberately retained for audit, including on failure.
Imports cannot write source bytecode caches.
"""
from __future__ import annotations

import argparse
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time


class QualificationFailure(Exception):
    pass


def require(condition):
    if not condition:
        raise QualificationFailure()


def load_migration():
    # Called ONLY after replacing HOME and the complete inherited environment.
    name = "_disposable_controller_migration"
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("migrate_controller.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def clean_environment(home, original):
    directories = []
    # Discover installed executables before discarding PATH, not their config.
    for name in ("agent-browser", "node", "git"):
        executable = shutil.which(name, path=original.get("PATH", os.defpath))
        require(executable is not None)
        # Keep the discovered basename reachable when an installed command is a
        # symlink to a differently named native executable. Only these explicit
        # tool directories are retained, never the caller's complete PATH.
        installed = Path(os.path.abspath(executable))
        directories.append(str(installed.parent))
        directories.append(str(installed.resolve(strict=True).parent))
    directories.extend(["/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    temporary = home / "tmp"
    temporary.mkdir(mode=0o700)
    return {"HOME": str(home), "PATH": os.pathsep.join(dict.fromkeys(directories)),
            "TMPDIR": str(temporary), "LANG": "C", "LC_ALL": "C",
            "PYTHONDONTWRITEBYTECODE": "1"}


class FixtureHTTPServer(ThreadingHTTPServer):
    # Browsers pre-open idle sockets. A synchronous HTTPServer can block the
    # real navigation behind one of them. Join bounded handlers on cleanup.
    daemon_threads = False
    block_on_close = True
    request_queue_size = 16


class SyntheticPage(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def do_GET(self):
        body = b'<!doctype html><title>Disposable qualification</title><input id="fixture" type="text"><script>window.fixtureNonce="initial";</script>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # Never print request paths, addresses, or browser output.


def eval_result(payload):
    require(isinstance(payload, dict) and payload.get("success") is True)
    data = payload.get("data")
    require(isinstance(data, dict) and "result" in data)
    value = data["result"]
    # Known source returns the JS value under data.result; JSON.stringify is
    # intentional so the synthetic probe has one bounded, explicit schema.
    require(isinstance(value, str) and len(value) < 4096)
    value = json.loads(value)
    require(isinstance(value, dict) and set(value) == {"form", "nonce"})
    require(all(isinstance(item, str) for item in value.values()))
    return value


def process_start(core, pid):
    result = core.checked_process(["/bin/ps", "-p", str(pid), "-o", "lstart="],
                                  accepted=(0, 1), timeout=5)
    return result.stdout.strip()


def daemon_identity(core, home, root, session):
    require(root.is_relative_to(home))
    pidfile, sock = root / (session + ".pid"), root / (session + ".sock")
    if not pidfile.exists() and not sock.exists():
        return None
    for path, kind in ((pidfile, stat.S_ISREG), (sock, stat.S_ISSOCK)):
        info = path.lstat()
        require(not path.is_symlink() and kind(info.st_mode)
                and info.st_uid == os.getuid() and not info.st_mode & 0o077
                and info.st_nlink == 1)
    text = pidfile.read_text().strip()
    require(text.isascii() and text.isdigit() and 1 < int(text) < 2**31)
    pid = int(text)
    started = process_start(core, pid)
    require(bool(started))
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    result = core.checked_process([lsof, "-nP", "-a", "-p", str(pid), "-U", "-Fn"],
                                  accepted=(0, 1), timeout=5)
    require("n" + str(sock) in result.stdout.splitlines())
    return pid, started


def terminate_verified(core, pid, started):
    current = process_start(core, pid)
    if not current:
        return True
    require(current == started)
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        current = process_start(core, pid)
        if not current:
            return True
        require(current == started)
        time.sleep(0.1)
    return False  # No SIGKILL, no broad process matching.


def fixture_cli_diagnostics(core, home, browser, session, executable, *,
                            staged_bootstrap=False, fixture_url=None,
                            observe_daemon_response=False):
    """Capture verified fixture responses; optionally observe one native request."""
    require(not observe_daemon_response or (staged_bootstrap and fixture_url is not None))
    original = core.checked_process
    counter = 0
    first_open_seen = False
    observation_stopped = False

    def validate(args, cwd, env):
        require(isinstance(env, dict) and env.get("HOME") == str(home))
        require(cwd is not None and Path(cwd).resolve().is_relative_to(home))
        require("--session" in args and args[args.index("--session") + 1] == session)
        require("--cdp" in args and args[args.index("--cdp") + 1] == str(browser["port"]))
        require(Path(env.get("AGENT_BROWSER_SOCKET_DIR", "")).resolve().is_relative_to(home / ".ab"))
        if staged_bootstrap:
            require(len(args) >= 10 and args[1] == "--config")
            config = Path(args[2])
            require(config.is_absolute() and not config.is_symlink()
                    and config.resolve().is_relative_to(home)
                    and json.loads(config.read_text(encoding="utf-8")) == {})
            require(args[3:9] == ["--session", session, "--cdp", str(browser["port"]),
                                  "--pin-tab", "--json"])

    def execute(args, cwd, env, timeout, accepted, stage):
        nonlocal counter
        validate(args, cwd, env)
        directory = home / "diagnostics"
        directory.mkdir(mode=0o700, exist_ok=True)
        counter += 1
        record = {"argv": args, "fixtureOnly": True, "stage": stage}
        started = time.monotonic()
        try:
            result = subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, timeout=timeout, check=False)
            record.update(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
        except subprocess.TimeoutExpired as error:
            record.update(timeout=True,
                          stdout=(error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else error.stdout),
                          stderr=(error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr))
            result = None
        record["elapsedSeconds"] = time.monotonic() - started
        path = directory / ("agent-" + str(counter).zfill(3) + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
        if result is None:
            core.fail("command_timeout")
        # Preserve the native uncertainty code even if its exit status was not
        # accepted. Never replay a navigation or substitute an opaque failure.
        try:
            value = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            value = None
        if isinstance(value, dict) and value.get("code") == "outcome_unknown":
            core.fail("command_outcome_unknown")
        if result.returncode not in accepted:
            core.fail("command_failed")
        return result

    def observe(args, cwd, env, accepted):
        nonlocal counter, observation_stopped
        validate(args, cwd, env)
        require(checked.bootstrapPreflightDone and args[9:] == ["open", fixture_url])
        root = Path(env["AGENT_BROWSER_SOCKET_DIR"])
        require(root.is_absolute() and root.resolve(strict=True) == root
                and root.is_relative_to(home / ".ab")
                and re.fullmatch(r"[A-Za-z0-9_-]+", session) is not None)
        proof = daemon_identity(core, home, root, session)
        if proof is None:
            core.fail("ownership_unverified")
        # No CLI connection flags: NativeState is already connected and pinned.
        request_id = "fixture-observe-" + secrets.token_hex(16)
        request = {"id": request_id, "action": "navigate", "url": fixture_url, "pinTab": True}
        wire = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
        directory = home / "diagnostics"
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and not directory.is_symlink()
                and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700)
        counter += 1
        path = directory / ("agent-" + str(counter).zfill(3) + ".json")
        # Reserve private evidence before any write to the daemon.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        record = {"fixtureOnly": True, "stage": "daemon_observation",
                  "requestId": request_id, "observationSeconds": 70}
        raw = bytearray()
        started = time.monotonic()
        deadline = started + 70
        failure = None
        sent = False
        payload = None
        observation_stopped = True  # Only a valid response can permit further CLI work.
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(max(0.001, deadline - time.monotonic()))
                client.connect(str(root / (session + ".sock")))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout()
                client.settimeout(remaining)
                sent = True  # A sendall error can follow a partial write; never retry.
                client.sendall(wire)
                while b"\n" not in raw:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise socket.timeout()
                    client.settimeout(remaining)
                    chunk = client.recv(min(65536, 2 * 1024 * 1024 + 1 - len(raw)))
                    if not chunk:
                        failure = "command_outcome_unknown"
                        break
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        failure = "invalid_json"
                        break
                if failure is None:
                    require(raw.endswith(b"\n") and raw.count(b"\n") == 1)
                    payload = json.loads(raw.decode("utf-8"))
                    require(isinstance(payload, dict) and type(payload.get("success")) is bool
                            and "type" not in payload
                            and (payload.get("error") is None or isinstance(payload["error"], str))
                            and (payload.get("code") is None or isinstance(payload["code"], str))
                            and ("id" not in payload or payload["id"] == request_id))
                    checked.daemonResponseReceived = True
                    if payload.get("code") == "outcome_unknown":
                        failure = "command_outcome_unknown"
        except (socket.timeout, TimeoutError):
            record["timeout"] = True
            failure = "command_outcome_unknown"
        except OSError:
            failure = "command_outcome_unknown" if sent else "command_failed"
        except (ValueError, UnicodeError, QualificationFailure):
            failure = "invalid_json"
        finally:
            record.update(elapsedSeconds=time.monotonic() - started,
                          rawDaemonResponse=raw.decode("utf-8", errors="replace"),
                          daemonResponseReceived=checked.daemonResponseReceived)
            if failure is not None:
                record["failureCode"] = failure
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream)
        if failure is not None:
            core.fail(failure)
        result = subprocess.CompletedProcess(args, 0 if payload["success"] else 1,
                                             raw.decode("utf-8"), "")
        if result.returncode not in accepted:
            core.fail("command_failed")
        observation_stopped = not payload["success"]
        return result

    def checked(args, *, cwd=None, env=None, timeout=15, accepted=(0,)):
        nonlocal first_open_seen
        if not args or Path(args[0]).resolve() != Path(executable):
            return original(args, cwd=cwd, env=env, timeout=timeout, accepted=accepted)
        if observe_daemon_response and observation_stopped:
            core.fail("command_outcome_unknown")
        validate(args, cwd, env)
        if observe_daemon_response and first_open_seen and args[9:10] == ["open"]:
            core.fail("command_outcome_unknown")
        if (staged_bootstrap and first_open_seen and not checked.bootstrapPreflightDone
                and args[9:10] == ["open"]):
            core.fail("ownership_unverified")
        if staged_bootstrap and not first_open_seen and args[9:10] == ["open"]:
            require(len(args) == 11 and isinstance(args[10], str)
                    and re.fullmatch(r"http://127\.0\.0\.1:[0-9]+/", args[10]) is not None
                    and 0 < int(args[10].split(":")[-1][:-1]) < 65536
                    and (fixture_url is None or args[10] == fixture_url))
            first_open_seen = True  # Failure must never permit another preflight/open.
            before = core.cdp_target_ids(browser)
            result = execute(args[:9] + ["tab", "list"], cwd, env, 75, (0,), "bootstrap")
            try:
                payload = json.loads(result.stdout)
            except (ValueError, UnicodeError):
                core.fail("invalid_json")
            if (not isinstance(payload, dict) or payload.get("success") is not True
                    or payload.get("code") is not None or payload.get("error") is not None
                    or "type" in payload):
                core.fail("invalid_json")
            tabs = core.parse_tabs(payload)
            after = core.cdp_target_ids(browser)
            selected = [tab for tab in tabs if tab["active"]]
            new = after - before
            if (not before <= after or len(new) != 1 or len(selected) != 1
                    or selected[0]["targetId"] not in new):
                core.fail("ownership_unverified")
            data = payload.get("data")
            raw_tabs = data.get("tabs") if isinstance(data, dict) else None
            if not isinstance(raw_tabs, list):
                core.fail("invalid_json")
            raw_selected = [tab for tab in raw_tabs
                            if tab.get("targetId") == selected[0]["targetId"]]
            if len(raw_selected) != 1 or raw_selected[0].get("url") != "about:blank":
                core.fail("ownership_unverified")
            checked.bootstrapPreflightDone = True
            if observe_daemon_response:
                return observe(args, cwd, env, accepted)
        return execute(args, cwd, env, timeout, accepted,
                       "open" if args[9:10] == ["open"] else "fixture-cli")

    checked.bootstrapPreflightDone = False
    checked.daemonResponseReceived = False
    return checked


def validate_fixture_chrome(chrome):
    """Accept one installed executable path, never a command or profile selector."""
    require(isinstance(chrome, str) and bool(chrome)
            and not any(ord(character) < 32 or ord(character) == 127 for character in chrome)
            and not any(character in chrome for character in ";&|`$<>\"'\\")
            and "--" not in chrome)
    path = Path(chrome)
    require(path.is_absolute()
            and path.name in {"Google Chrome", "Google Chrome for Testing", "Chromium"}
            and path.is_file() and os.access(path, os.X_OK))
    return chrome


def start_fixture_browser(core, coordinator, home, chrome=None):
    """Scope synthetic-profile crypto flags to one fixture start, then restore Popen.

    This isolation is not evidence that keychain access caused a navigation stall.
    Called before fixture HTTP threads exist; never changes Core.start or config.
    """
    if chrome is not None:
        chrome = validate_fixture_chrome(chrome)
    require(home.is_absolute() and home.resolve(strict=True) == home)
    project = coordinator.project
    profile = project.profile
    require(project.root.is_absolute() and project.root.resolve(strict=True).is_relative_to(home)
            and profile.is_absolute() and profile.resolve().is_relative_to(home)
            and profile.resolve() == profile and profile != home)
    original = core.subprocess.Popen
    launched = False

    def fixture_popen(args, *positional, **kwargs):
        nonlocal launched
        # Do not let a shell/string spelling bypass the profile argument guard.
        require(not isinstance(args, (str, bytes)) or
                ("--user-data-dir" not in args if isinstance(args, str)
                 else b"--user-data-dir" not in args))
        # ps/git and other non-launch processes keep their exact original call.
        if not isinstance(args, (list, tuple)) or not any(
                isinstance(arg, str) and arg.startswith("--user-data-dir") for arg in args):
            return original(args, *positional, **kwargs)
        directories = [arg for arg in args if isinstance(arg, str)
                       and arg.startswith("--user-data-dir")]
        environment = kwargs.get("env")
        require(not launched and directories == ["--user-data-dir=" + str(profile)]
                and Path(args[0]).name in {"Google Chrome", "Google Chrome for Testing", "Chromium"}
                and not positional and not kwargs.get("shell", False)
                and isinstance(environment, dict) and environment.get("HOME") == str(home)
                and profile.resolve() == profile and profile.is_relative_to(home)
                and list(args).count("about:blank") == 1 and args[-1] == "about:blank"
                and not any(arg.startswith("--password-store") and arg != "--password-store=basic"
                            for arg in args)
                and "--use-real-keychain" not in args)
        injected = list(args[:-1])
        for flag in ("--password-store=basic", "--use-mock-keychain"):
            if flag not in injected:
                injected.append(flag)
        result = original(injected + ["about:blank"], **kwargs)
        launched = True  # True only after the matching launch was actually performed.
        return result

    core.subprocess.Popen = fixture_popen
    try:
        result = coordinator.start(allow_launch=True, headed=False, chrome=chrome)
        require(result["status"] == "started" and launched)
        return True
    finally:
        core.subprocess.Popen = original


def exercise(home, checks, diagnose_bootstrap=False, observe_daemon_response=False, chrome=None):
    if chrome is not None:
        chrome = validate_fixture_chrome(chrome)
    migration = load_migration()
    core, runtime = migration._modules()
    server = None
    thread = None
    coordinator = None
    browser = None
    roots = []
    daemons = set()
    task_id = "qualification-" + secrets.token_hex(8)
    session = None
    metadata = None
    complete = False
    original_agent = None
    original_checked = None
    try:
        metadata = migration.qualification_metadata()
        checks["metadataReady"] = True
        root = home / "project"
        root.mkdir(mode=0o700)
        core.checked_process([shutil.which("git"), "init", "--quiet", str(root)], timeout=10)
        (root / ".gitignore").write_text(".chrome-user-data/\n", encoding="utf-8")
        project = core.resolve_project(root)
        require(project.root == root and not project.profile.exists())
        checks["fixtureProjectReady"] = True
        store = core.StateStore(core.DEFAULT_STATE_DIR, project)
        coordinator = core.Coordinator(project, store)
        roots = [runtime.legacy_socket_directory(project.key), home / ".ab" / "shared"]
        session = core.session_name(project.key, task_id)
        require(all(len(os.fsencode(path / (session + ".sock"))) <= 103 for path in roots))
        with store.lock():
            if chrome is None:
                checks["fixtureUsesMockKeychain"] = start_fixture_browser(core, coordinator, home)
            else:
                checks["fixtureUsesMockKeychain"] = start_fixture_browser(core, coordinator, home, chrome=chrome)
        browser = coordinator.browser()
        require(browser["headed"] is False)
        identity = core.browser_identity(browser)
        checks["fixtureBrowser"] = True
        original_checked = core.checked_process
        server = FixtureHTTPServer(("127.0.0.1", 0), SyntheticPage)
        fixture_url = "http://127.0.0.1:" + str(server.server_port) + "/"
        core.checked_process = fixture_cli_diagnostics(
            core, home, browser, session, metadata["agentBrowserPath"],
            staged_bootstrap=diagnose_bootstrap, fixture_url=fixture_url,
            observe_daemon_response=observe_daemon_response)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with store.lock():
            coordinator.attach(task_id, fixture_url)
        task = coordinator.task(task_id)
        daemon = daemon_identity(core, home, roots[0], session)
        require(daemon is not None)
        daemons.add(daemon)
        original_agent = coordinator.agent
        def audited_agent(current_browser, current_task, args):
            result = original_agent(current_browser, current_task, args)
            path = Path(current_task["socketDir"])
            require(path in roots)
            # close may leave stale sidecars after daemon exit; never probe a
            # disconnected backend or cause a second attachment to discover it.
            if args != ["close"]:
                proof = daemon_identity(core, home, path, session)
                require(proof is not None)
                daemons.add(proof)
            return result
        coordinator.agent = audited_agent
        form, nonce = secrets.token_hex(16), secrets.token_hex(16)
        probe = 'JSON.stringify({form:document.querySelector("#fixture").value,nonce:window.fixtureNonce})'
        setup = ('document.querySelector("#fixture").value=' + json.dumps(form)
                 + ';window.fixtureNonce=' + json.dumps(nonce) + ';' + probe)
        baseline = eval_result(coordinator.agent(browser, task, ["eval", setup]))
        require(baseline == {"form": form, "nonce": nonce})
        targets = core.cdp_target_ids(browser)
        target = task["targetId"]
        with store.lock():
            coordinator.handoff(task_id, auth=True, captures_stopped=True)
        checkpoint = copy.deepcopy(coordinator.state)
        checks["syntheticCheckpoint"] = True
        runtime.configure(roots[1], [root], allow_write=True)
        require(runtime.project_socket_directory(project.key) == roots[1]
                and runtime.task_socket_directory(project.key, task) == roots[0]
                and runtime.task_socket_directory(project.key, {}) == roots[0])
        checks["policyRetainsLegacyTask"] = True
        # The sole gate patch is local to this disposable process and restored.
        gate = migration.qualification_verified
        try:
            migration.qualification_verified = lambda: True
            result = migration.migrate(coordinator, task_id, allow_migrate=True,
                                       human_paused=True, captures_stopped=True)
        finally:
            migration.qualification_verified = gate
        require(result["status"] == "migration_complete")
        with store.lock():
            coordinator.state = store.load()
        migrated = coordinator.task(task_id)
        expected = copy.deepcopy(checkpoint)
        expected["tasks"][task_id]["socketDir"] = str(roots[1])
        require(coordinator.state == expected and migrated["status"] == "human"
                and migrated["auth"] is True and coordinator.state["auth_task"] == task_id)
        require(core.browser_identity(coordinator.browser()) == identity
                and migrated["targetId"] == target
                and core.cdp_target_ids(browser) == targets)
        checks["migrationPreservesIdentityAndTargets"] = True
        checks["leaseAndRecordedRootPreserved"] = True
        # Normal automation must fail BEFORE invoking any agent command. The
        # private synthetic eval below is the only deliberate post-handoff probe.
        calls = []
        def forbidden_agent(*args):
            calls.append(True)
            raise QualificationFailure()
        coordinator.agent = forbidden_agent
        try:
            try:
                coordinator.run(task_id, ["get", "title"])
            except core.BrowserSessionError as error:
                require(error.code == "auth_lease")
            else:
                raise QualificationFailure()
            require(not calls)
        finally:
            coordinator.agent = audited_agent
        checks["normalAutomationBlocked"] = True
        require(eval_result(coordinator.agent(browser, migrated, ["eval", probe])) == baseline)
        require(core.browser_identity(coordinator.browser()) == identity
                and core.cdp_target_ids(browser) == targets)
        checks["syntheticFormAndNoncePreserved"] = True
        metadata.update(version=1, passed=True,
                        chromeVersion=core.cdp_json(browser["port"], "/json/version")["Browser"])
        complete = True
    finally:
        cleanup = True
        # Never attach/restart a backend to clean it. Close only sidecars whose
        # live Unix socket and PID ownership are proved under the fixture HOME.
        if coordinator is not None and session is not None:
            for path in roots:
                try:
                    if (path / (session + ".sock")).exists():
                        proof = daemon_identity(core, home, path, session)
                        require(proof is not None)
                        daemons.add(proof)
                        current = core.verify_browser(coordinator.project)
                        require(current is not None)
                        shadow = {"session": session, "socketDir": str(path)}
                        # No marker; close disconnects an external-CDP backend.
                        (original_agent or coordinator.agent)(current, shadow, ["close"])
                except BaseException:
                    cleanup = False
        for pid, started in daemons:
            try:
                cleanup = terminate_verified(core, pid, started) and cleanup
            except BaseException:
                cleanup = False
        if coordinator is not None:
            try:
                current = core.verify_browser(coordinator.project)
                if current:
                    if browser:
                        require(core.browser_identity(current) == core.browser_identity(browser))
                    cleanup = terminate_verified(core, current["pid"], current["started"]) and cleanup
                require(not core.discover_processes(coordinator.project.profile))
            except BaseException:
                cleanup = False
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                require(not thread.is_alive())
            except BaseException:
                cleanup = False
        checks["cleanupComplete"] = cleanup
        if original_checked is not None:
            if diagnose_bootstrap:
                checks["bootstrapPreflightDone"] = core.checked_process.bootstrapPreflightDone
            if observe_daemon_response:
                checks["daemonResponseReceived"] = core.checked_process.daemonResponseReceived
            core.checked_process = original_checked
    require(complete and checks["cleanupComplete"])
    return migration, metadata


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise QualificationFailure()


def main(argv=None):
    checks = dict.fromkeys(("approvedLaunchFlag", "metadataReady", "fixtureProjectReady",
                           "fixtureBrowser", "fixtureUsesMockKeychain", "syntheticCheckpoint",
                           "policyRetainsLegacyTask", "migrationPreservesIdentityAndTargets",
                           "leaseAndRecordedRootPreserved", "normalAutomationBlocked",
                           "syntheticFormAndNoncePreserved", "cleanupComplete", "receiptWritten"), False)
    original = dict(os.environ)
    original_dont_write_bytecode = sys.dont_write_bytecode
    original_home = Path.home().resolve(strict=True)
    home = None
    success = False
    diagnostic_only = False
    failure_code = None
    old_umask = os.umask(0o077)
    sys.dont_write_bytecode = True
    try:
        parser = SafeParser(description=__doc__, allow_abbrev=False)
        parser.add_argument("--allow-browser-launch", action="store_true")
        parser.add_argument("--diagnose-bootstrap", action="store_true",
                            help="Separate fixture bootstrap from navigation; never issue a qualification receipt.")
        parser.add_argument("--observe-daemon-response", action="store_true",
                            help="Observe one fixture daemon response for up to 70 seconds; diagnostic only.")
        parser.add_argument("--chrome", metavar="PATH",
                            help="Absolute installed Chrome executable for the disposable HEADLESS fixture only; "
                                 "does not upgrade an existing profile or change the actual project browser.")
        args = parser.parse_args(argv)
        chrome_options = {} if args.chrome is None else {"chrome": validate_fixture_chrome(args.chrome)}
        require(not args.observe_daemon_response
                or (args.diagnose_bootstrap and args.allow_browser_launch))
        diagnostic_only = args.diagnose_bootstrap
        if diagnostic_only:
            checks["bootstrapPreflightDone"] = False
        require(args.allow_browser_launch and sys.platform == "darwin")
        checks["approvedLaunchFlag"] = True
        home = Path(tempfile.mkdtemp(prefix="b", dir="/private/tmp")).resolve(strict=True)
        require(len(os.fsencode(home)) <= 22 and home.stat().st_uid == os.getuid()
                and stat.S_IMODE(home.stat().st_mode) == 0o700)
        environment = clean_environment(home, original)
        os.environ.clear()
        os.environ.update(environment)
        if args.observe_daemon_response:
            checks["daemonResponseReceived"] = False
            migration, metadata = exercise(home, checks, diagnose_bootstrap=True,
                                           observe_daemon_response=True, **chrome_options)
        elif diagnostic_only:
            migration, metadata = exercise(home, checks, diagnose_bootstrap=True, **chrome_options)
        else:
            migration, metadata = exercise(home, checks, **chrome_options)
        # Diagnostic success means only that this staged fixture completed. It
        # must never authorize migration, even when every check passes.
        if not diagnostic_only:
            # Only this evidence write touches the original HOME. Keep HOME
            # fixture-scoped; the explicit trusted HOME selects the receipt only.
            migration.record_qualification(metadata, home=original_home)
            checks["receiptWritten"] = True
        success = True
    except BaseException as error:
        # Only fixed core codes reach stdout; fixture CLI details remain private.
        code = getattr(error, "code", None)
        allowed = {"command_failed", "command_timeout", "command_outcome_unknown", "invalid_json",
                   "ownership_unverified", "browser_unverified", "tab_gone",
                   "stale_browser", "invalid_state", "task_paused",
                   "reconciliation_required", "auth_lease", "human_lease"}
        failure_code = code if isinstance(code, str) and code in allowed else "check_failed"
    finally:
        os.environ.clear()
        os.environ.update(original)
        sys.dont_write_bytecode = original_dont_write_bytecode
        os.umask(old_umask)
    print(json.dumps({"success": success, "checks": checks, "failureCode": failure_code,
                      "diagnosticOnly": diagnostic_only,
                      "qualificationIssued": checks["receiptWritten"],
                      "scratchPath": str(home) if home else None}, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
