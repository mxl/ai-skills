#!/usr/bin/env python3
"""Prepare private, reviewable Caddy/launchd templates; never deploy services.

Python 3.10+. Import performs no runtime/profile inspection. Approval flags are
accident guards, not consent. The host must approve dashboard starts (including
standing LaunchAgent grants) and separately approve every deployment/trust step.
Integrity hashes detect accidents, not a malicious owner or a consent boundary.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit


MESSAGES = {
    "invalid_arguments": "Invalid command arguments; values suppressed.",
    "invalid_manifest": "Manifest is invalid or contains unsupported fields.",
    "invalid_project": "Project roots could not be resolved uniquely.",
    "unsafe_path": "Unsafe path, ownership, permissions, or file type.",
    "output_in_git": "Output bundles must be outside Git repositories.",
    "invalid_bundle": "Bundle integrity or configuration checks failed.",
    "approval_required": "Dashboard start requires host approval and --allow-start.",
    "command_failed": "Command failed; raw output suppressed. Review locally before retrying.",
    "command_timeout": "Command timed out; outcome uncertain. Review locally before retrying.",
    "invalid_response": "Dashboard response was not recognized; bootstrap preserved.",
    "unsupported_platform": "Dashboard runtime commands require macOS.",
}
ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", re.ASCII)
TOKEN = re.compile(r"dashboard-access-token=[0-9a-fA-F]{64}", re.ASCII)
RANGES = tuple(ipaddress.ip_network(v) for v in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7"))
MAX_JSON = 2 * 1024 * 1024
_SIBLING = None


class RemoteAccessError(Exception):
    """Stable public error; never embeds subprocess or input text."""
    def __init__(self, code: str):
        self.code = code if code in MESSAGES else "command_failed"
        super().__init__(MESSAGES[self.code])


def fail(code: str) -> None:
    raise RemoteAccessError(code)


def sibling():
    """Lazy import only; no StateStore, profiles, or dashboard.config reads."""
    global _SIBLING
    if _SIBLING is None:
        source = Path(__file__).resolve().with_name("browser_session.py")
        spec = importlib.util.spec_from_file_location("_remote_browser_session", source)
        if spec is None or spec.loader is None:
            fail("invalid_project")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclasses resolves its module here.
        try:
            spec.loader.exec_module(module)
        except Exception:
            fail("invalid_project")
        _SIBLING = module
    return _SIBLING


def clean_string(value: Any) -> str:
    if (not isinstance(value, str) or not value
            or any(ord(c) < 32 or ord(c) == 127 or c in "${}" for c in value)):
        fail("invalid_manifest")
    return value


def fields(value: Any, required: set[str], optional: set[str] | None = None) -> dict:
    if (not isinstance(value, dict) or not required <= value.keys()
            or value.keys() - required - (optional or set())):
        fail("invalid_manifest")
    return value


def strict_json(raw: str | bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail("invalid_manifest")
            result[key] = value
        return result

    def constant(_):
        fail("invalid_manifest")

    try:
        if len(raw) > MAX_JSON:
            fail("invalid_manifest")
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        fail("invalid_manifest")


def origin(value: Any, allow_ip: bool = False) -> str:
    text = clean_string(value)
    if not text.isascii():
        fail("invalid_manifest")
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port if parts.port is not None else 443
        if (parts.scheme != "https" or not host or parts.username is not None
                or parts.password is not None or parts.path not in ("", "/")
                or parts.query or parts.fragment or "?" in text or "#" in text
                or parts.netloc.endswith(":") or "\\" in text):
            fail("invalid_manifest")
        # IP literals are opt-in and restricted to the same private ranges as
        # bindAddress. Port differences never create cookie isolation.
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None:
            if not allow_ip:
                fail("invalid_manifest")
            host = allowed_address(host)
            host = f"[{host}]" if address.version == 6 else host
        elif (len(host) > 253 or "." not in host or host.endswith(".")
              or host == "localhost" or host.endswith(".localhost")
              or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                     for label in host.split("."))):
            fail("invalid_manifest")
        if port != 443 and not 1024 <= port <= 65535:
            fail("invalid_manifest")
        return "https://" + host + (f":{port}" if port != 443 else "")
    except ValueError:
        fail("invalid_manifest")


def high_port(value: Any) -> int:
    if type(value) is not int or not 1024 <= value <= 65535:
        fail("invalid_manifest")
    return value


def allowed_address(value: Any, *, network: bool = False) -> str:
    text = clean_string(value)
    try:
        if network:
            if "/" not in text:
                fail("invalid_manifest")
            addr = ipaddress.ip_network(text, strict=True)
            if not any(addr.version == r.version and addr.subnet_of(r)
                       and addr.prefixlen > r.prefixlen for r in RANGES):
                fail("invalid_manifest")
        else:
            addr = ipaddress.ip_address(text)
            if not any(addr.version == r.version and addr in r for r in RANGES):
                fail("invalid_manifest")
        if "%" in text:
            fail("invalid_manifest")
        return str(addr)
    except ValueError:
        fail("invalid_manifest")


def absolute_path(value: Any) -> Path:
    path = Path(clean_string(value))
    if not path.is_absolute() or ".." in path.parts:
        fail("invalid_manifest")
    return path


def validate_manifest(value: Any) -> dict:
    """Validate/normalize nonsecret input; resolve canonical roots, never profiles."""
    fields(value, {"version", "hubOrigin", "bindAddress", "allowedPeers", "tls", "projects"},
           {"caddyAdminPort", "mode"})
    if type(value["version"]) is not int or value["version"] != 1:
        fail("invalid_manifest")
    mode = value.get("mode", "project-hostnames")
    if mode not in ("project-hostnames", "shared-ip"):
        fail("invalid_manifest")
    shared = mode == "shared-ip"
    hub = origin(value["hubOrigin"], allow_ip=shared)
    bind = allowed_address(value["bindAddress"])
    if shared and urlsplit(hub).hostname != bind:
        fail("invalid_manifest")
    peers = value["allowedPeers"]
    if not isinstance(peers, list) or not peers:
        fail("invalid_manifest")
    peers = [allowed_address(peer, network=True) for peer in peers]
    if len(set(peers)) != len(peers):
        fail("invalid_manifest")
    tls = value["tls"]
    if not isinstance(tls, dict):
        fail("invalid_manifest")
    if tls.get("mode") == "internal":
        fields(tls, {"mode"})
        tls = {"mode": "internal"}
    elif tls.get("mode") == "existing":
        fields(tls, {"mode", "certificate", "privateKey"})
        tls = {"mode": "existing", "certificate": str(absolute_path(tls["certificate"])),
               "privateKey": str(absolute_path(tls["privateKey"]))}
    else:
        fail("invalid_manifest")
    admin = high_port(value.get("caddyAdminPort", 2029))
    entries = value["projects"]
    if not isinstance(entries, list) or not entries:
        fail("invalid_manifest")
    projects, ids, roots = [], set(), set()
    hosts = {urlsplit(hub).hostname}
    public_ports = {urlsplit(hub).port or 443}
    dashboard_ports = set()
    for entry in entries:
        fields(entry, {"id", "root", "origin", "dashboardPort"})
        project_id = clean_string(entry["id"])
        if len(project_id) > 63 or not ID.fullmatch(project_id):
            fail("invalid_manifest")
        project_origin = origin(entry["origin"], allow_ip=shared)
        host = urlsplit(project_origin).hostname
        port = high_port(entry["dashboardPort"])
        root = absolute_path(entry["root"])
        try:
            resolved = sibling().resolve_project(root)
        except Exception:
            fail("invalid_project")
        canonical = clean_string(str(resolved.root))
        if project_id in ids or canonical in roots:
            fail("invalid_manifest")
        if shared:
            if project_origin != hub or (dashboard_ports and port not in dashboard_ports):
                fail("invalid_manifest")
        elif host in hosts or port in dashboard_ports:
            fail("invalid_manifest")
        ids.add(project_id)
        roots.add(canonical)
        hosts.add(host)
        dashboard_ports.add(port)
        public_ports.add(urlsplit(project_origin).port or 443)
        projects.append({"id": project_id, "root": canonical,
                         "origin": project_origin, "dashboardPort": port})
    if admin in public_ports or admin in dashboard_ports or public_ports & dashboard_ports:
        fail("invalid_manifest")
    result = {"version": 1, "hubOrigin": hub, "bindAddress": bind,
              "allowedPeers": peers, "tls": tls, "projects": projects, "caddyAdminPort": admin}
    # Preserve exact normalized output for legacy manifests/bundle hashes.
    if "mode" in value:
        result["mode"] = mode
    return result


def read_manifest(path: str | Path) -> dict:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        fail("invalid_manifest")
    return validate_manifest(strict_json(raw))


def no_symlinks(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        fail("unsafe_path")
    for item in (path, *path.parents):
        if item.is_symlink():
            fail("unsafe_path")


def private_info(path: Path, *, directory: bool = False) -> None:
    no_symlinks(path)
    try:
        info = path.lstat()
    except OSError:
        fail("unsafe_path")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or (not directory and info.st_nlink != 1)):
        fail("unsafe_path")


def private_read(path: Path) -> bytes:
    private_info(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                fail("unsafe_path")
            data = stream.read(MAX_JSON + 1)
        if len(data) > MAX_JSON:
            fail("invalid_bundle")
        return data
    except OSError:
        fail("unsafe_path")


def private_directory(path: Path) -> None:
    no_symlinks(path)
    if not path.exists():
        private_info(path.parent, directory=True)
        path.mkdir(mode=0o700)
    private_info(path, directory=True)


def private_write(path: Path, data: str | bytes, *, replace: bool = False) -> None:
    """Same-directory atomic publish; never truncate an existing destination."""
    private_info(path.parent, directory=True)
    no_symlinks(path)
    if path.exists():
        if not replace:
            fail("unsafe_path")
        private_info(path)
    temp = None
    try:
        fd, temp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data.encode("utf-8") if isinstance(data, str) else data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp, path)
        else:
            os.link(temp, path, follow_symlinks=False)
            os.unlink(temp)
        temp = None
    except OSError:
        fail("unsafe_path")
    finally:
        if temp is not None:
            os.unlink(temp)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"


def clean_environment() -> dict[str, str]:
    # No proxy, API, manager, namespace, NODE_OPTIONS, or config variables.
    result = {"HOME": str(Path.home()), "PATH": os.defpath, "LANG": "en_US.UTF-8"}
    for key in ("PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES"):
        value = os.environ.get(key)
        if value and not any(ord(c) < 32 or ord(c) == 127 for c in value):
            result[key] = value
    return result


def run(args: list[str], *, cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, cwd=cwd, env=clean_environment(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        fail("command_timeout")
    except (OSError, UnicodeError):
        fail("command_failed")
    if result.returncode:
        fail("command_failed")
    return result


def outside_git(path: Path) -> None:
    ancestor = path
    while not ancestor.exists():
        ancestor = ancestor.parent
    no_symlinks(ancestor)
    git = shutil.which("git", path=clean_environment()["PATH"])
    if not git:
        fail("output_in_git")
    # A nonzero Git probe is expected outside repositories; bare repos also fail closed.
    try:
        probe = subprocess.run([git, "-C", str(ancestor), "rev-parse", "--git-dir"],
                               cwd=ancestor, env=clean_environment(), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        fail("output_in_git")
    if probe.returncode != 128:
        fail("output_in_git")


def approved_executables(value: dict | None = None) -> dict:
    if value is None:
        value = {"agentBrowser": shutil.which("agent-browser", path=clean_environment()["PATH"]),
                 "node": shutil.which("node", path=clean_environment()["PATH"])}
    fields(value, {"agentBrowser", "node"})
    return {key: str(absolute_path(path)) if path is not None else None
            for key, path in value.items()}


def make_plan(manifest: dict, bundle: Path, executables: dict | None = None) -> dict:
    projects = []
    for entry in manifest["projects"]:
        try:
            project = sibling().resolve_project(entry["root"])
            socket = absolute_path(str(sibling().child_environment(project.key)["AGENT_BROWSER_SOCKET_DIR"]))
        except Exception:
            fail("invalid_project")
        if project.root == bundle or bundle in project.root.parents:
            fail("invalid_manifest")
        if manifest.get("mode", "project-hostnames") == "shared-ip":
            no_symlinks(socket)
        projects.append({**entry, "key": project.key, "socketDir": str(socket)})
    if (manifest.get("mode", "project-hostnames") == "shared-ip"
            and len({p["socketDir"] for p in projects}) != 1):
        # Enrollment belongs to the sibling runtime policy. Never synthesize a
        # shared directory or silently migrate existing per-project runtimes.
        fail("invalid_project")
    return {"version": 1, "bundle": str(bundle), "projects": projects,
            "executables": approved_executables(executables), "qualificationRequired": True}


def render_caddy(manifest: dict, bundle: str | Path, *, standalone: bool = False) -> str:
    """Render additive sites by default, or the review-only standalone config."""
    q = json.dumps
    lines = []
    if standalone:
        lines += ["{", "\tskip_install_trust", "\tauto_https disable_redirects",
                  f"\tadmin 127.0.0.1:{manifest['caddyAdminPort']}",
                  "\tservers {", "\t\tprotocols h1 h2", "\t}", "}", ""]
    if manifest.get("mode", "project-hostnames") == "shared-ip":
        sites = [(manifest["hubOrigin"], manifest["projects"][0]["dashboardPort"])]
    else:
        sites = [(manifest["hubOrigin"], None)] + [
            (p["origin"], p["dashboardPort"]) for p in manifest["projects"]]
    for site, port in sites:
        lines += [q(site) + " {", "\tbind " + q(manifest["bindAddress"])]
        tls = manifest["tls"]
        lines.append("\ttls internal" if tls["mode"] == "internal" else
                     "\ttls " + q(tls["certificate"]) + " " + q(tls["privateKey"]))
        lines += ["\t@allowed remote_ip " + " ".join(q(p) for p in manifest["allowedPeers"]),
                  "\thandle @allowed {"]
        if port is None:
            lines += ["\t\troot * " + q(str(Path(bundle) / "hub")), "\t\tfile_server"]
        else:
            lines += [f"\t\treverse_proxy 127.0.0.1:{port}"]
        lines += ["\t}", "\thandle {", '\t\trespond "Forbidden" 403', "\t}", "}", ""]
    return "\n".join(lines)


def render_hub(manifest: dict) -> str:
    links = "\n".join('  <li><a href="' + html.escape(p["origin"], quote=True)
                       + '">' + html.escape(p["id"]) + "</a></li>"
                       for p in manifest["projects"])
    return ("<!doctype html>\n<html lang=\"en\"><meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<title>Project viewports</title>\n<h1>Project viewports</h1>\n<ul>\n"
            + links + "\n</ul>\n<p>Bootstrap credentials are supplied separately.</p>\n</html>\n")


def reviewed_runtime_path(executables: dict | None = None) -> str:
    """Use only approved tool directories and fixed system directories."""
    tools = approved_executables(executables)
    directories = [str(Path(tools[key]).parent) for key in ("node", "agentBrowser")
                   if tools[key] is not None]
    directories.extend(("/usr/bin", "/bin", "/usr/sbin", "/sbin"))
    return os.pathsep.join(dict.fromkeys(directories))


def render_launchd(project: dict, bundle: str | Path, executables: dict | None = None) -> bytes:
    bundle = Path(bundle)
    # A template only: loading it grants periodic dashboard-only starts. No Chrome.
    name = "local.browser-viewport." + project["id"]
    return plistlib.dumps({
        "Label": name, "ProgramArguments": [str(Path(sys.executable).resolve()),
            str(Path(__file__).resolve()), "dashboard-start", "--bundle", str(bundle),
            "--project", project["id"], "--allow-start"],
        "RunAtLoad": True, "StartInterval": 60, "WorkingDirectory": str(bundle),
        "EnvironmentVariables": {"HOME": str(Path.home()), "PATH": reviewed_runtime_path(executables),
                                 "LANG": "en_US.UTF-8"},
        "StandardOutPath": str(bundle / "logs" / (project["id"] + ".out.log")),
        "StandardErrorPath": str(bundle / "logs" / (project["id"] + ".err.log")),
        "Umask": 0o077,
    }, sort_keys=True)


REQUIREMENTS = """# Manual deployment requirements — not an installer

Caddyfile.sites is additive. Do not replace an existing daemon's configuration.
Review and manually integrate these globals with its owner: skip_install_trust,
auto_https disable_redirects, loopback-only admin on the manifest port, and
servers { protocols h1 h2 }. Resolve existing globals/listener conflicts first.
If those changes would disrupt existing sites or policy, stop and propose an
explicitly approved isolated instance with free ports and separate private
storage; do not silently change existing global redirect/trust/admin settings.
Caddyfile.standalone is a parser-review input, not authorization to deploy it.
No log/log_credentials directive, Host/Origin rewrite, public ACME fallback,
HTTP redirect listener, raw CDP proxy, or separate stream proxy is supplied.

Internal TLS may create a local CA when provisioned. Trust installation is never
automatic here. Approve provisioning and remote-client CA trust separately.
Existing certificate/key paths are metadata only: prepare never reads the key.
Verify certificate hostname coverage, private-key access, and Caddy storage
permissions manually. Port 443 requires a separately approved privilege/capability
plan; other public ports must exceed 1023. No listener is opened by prepare/adapt.

Before deployment, verify the bind address exists on this host, VPN/LAN routes,
firewall/peer restrictions, DNS, certificate trust, and no unintended listeners.
remote_ip is the direct peer address: do not place an unreviewed proxy in front.
A successful adapt parses configuration; it does NOT provision modules, verify
TLS, qualify live networking, or approve activation. Existing Caddy global
access logging/policies must be reviewed independently for credential exposure.

Each dashboard has its own hostname/origin (cookies are not port-isolated).
Hub links contain no token. bootstrap/<id>.txt contains a reusable bearer URL;
transfer it privately to the trusted human, never into chat/logs/hub/config.
The CLI does not promise token expiry or one-shot use. Browser history and the
remote dashboard remain sensitive. There is no remote untrusted-user boundary.

launchd/*.plist are templates ONLY. Host approval is required before loading;
loading grants standing dashboard-only starts every 60 seconds and at load.
They never launch Chrome and do not use KeepAlive. Preserve the existing local
Chrome profile/authentication and ~/.ab/<project-key-prefix> socket directory.
The wrapper never stops or reconfigures a mismatched existing dashboard.
A start response still requires independent listener/PID/loopback and end-to-end
qualification. The wrapper reports a separate bounded loopback-listener readiness
check against the returned PID; that is not remote/TLS/auth qualification. No remote
activation is done.

All bundle metadata is private. Integrity hashes detect accidental changes, not
host consent or malicious same-user edits. Regenerate/review after source or
configuration changes. Keep launchd output logs private. Review commands and
obtain host approval separately for deployment, service changes, and trust.
"""


def generated_files(manifest: dict, plan: dict, bundle: Path) -> dict[str, bytes]:
    shared = manifest.get("mode", "project-hostnames") == "shared-ip"
    requirements = REQUIREMENTS
    if shared:
        requirements = requirements.replace(
            "firewall/peer restrictions, DNS, certificate trust, and no unintended listeners.",
            "firewall/peer restrictions, certificate IP SAN/trust, and no unintended listeners.")
        requirements = requirements.replace(
            "Each dashboard has its own hostname/origin (cookies are not port-isolated).\n"
            "Hub links contain no token. bootstrap/<id>.txt contains a reusable bearer URL;",
            "Shared-IP mode exposes ONE direct dashboard origin, not a hub/DNS route.\n"
            "Every enrolled project uses the same policy-selected socket directory,\n"
            "dashboard process and instance bearer token. Project browser profiles and\n"
            "leases remain separate, but the trusted operator has full control across\n"
            "projects: no per-project ACL or cookie/port isolation is promised.\n"
            "The generated hub is NOT served and its links contain no token.\n"
            "bootstrap/shared.txt contains the single reusable instance bearer URL;")
        requirements = requirements.replace(
            "Chrome profile/authentication and ~/.ab/<project-key-prefix> socket directory.",
            "Chrome profiles/authentication and the policy-selected shared socket directory.\n"
            "launchd/shared.plist designates the first project; any enrolled member may\n"
            "start/reuse that same instance. Configure enrollment separately before prepare.")
    result = {
        "manifest.json": json_text(manifest).encode(),
        "plan.json": json_text(plan).encode(),
        "Caddyfile.sites": render_caddy(manifest, bundle).encode(),
        "Caddyfile.standalone": render_caddy(manifest, bundle, standalone=True).encode(),
        "GLOBAL-REQUIREMENTS.md": requirements.encode(),
        "hub/index.html": render_hub(manifest).encode(),
        "agent-browser-empty.json": b"{}\n",
    }
    for project in manifest["projects"][:1] if shared else manifest["projects"]:
        name = "shared" if shared else project["id"]
        result["launchd/" + name + ".plist"] = render_launchd(project, bundle, plan["executables"])
    return result


def prepare_bundle(manifest: dict | str | Path, output_dir: str | Path) -> dict:
    """Write a new/empty private bundle. No runtime probes, installs, or service changes."""
    manifest = validate_manifest(manifest) if isinstance(manifest, dict) else read_manifest(manifest)
    bundle = absolute_path(str(output_dir))
    no_symlinks(bundle)
    outside_git(bundle)
    plan = make_plan(manifest, bundle)
    # Permit owned non-writable ancestors and system-owned structural ancestors;
    # reject writable shared directories unless sticky (e.g. resolved temp roots).
    for ancestor in bundle.parents:
        if ancestor.exists():
            info = ancestor.stat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                    or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
                fail("unsafe_path")
    missing, ancestor = [], bundle
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    if bundle.exists():
        private_info(bundle, directory=True)
        if any(bundle.iterdir()):
            fail("unsafe_path")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    private_info(bundle, directory=True)
    for subdir in ("hub", "launchd", "logs", "bootstrap"):
        private_directory(bundle / subdir)
    files = generated_files(manifest, plan, bundle)
    for name, content in files.items():
        private_write(bundle / name, content)
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    private_write(bundle / "integrity.json", json_text({"version": 1, "sha256": hashes}))
    return {"status": "prepared", "bundle": str(bundle), "qualificationRequired": True}


def load_bundle(bundle: str | Path) -> dict:
    """Revalidate roots/socket mapping and exact generated files before operations."""
    bundle = absolute_path(str(bundle))
    private_info(bundle, directory=True)
    for name in ("hub", "launchd", "logs", "bootstrap"):
        private_info(bundle / name, directory=True)
    try:
        manifest = validate_manifest(strict_json(private_read(bundle / "manifest.json")))
        saved_plan = strict_json(private_read(bundle / "plan.json"))
        if not isinstance(saved_plan, dict) or not isinstance(saved_plan.get("executables"), dict):
            fail("invalid_bundle")
        plan = make_plan(manifest, bundle, approved_executables(saved_plan["executables"]))
        if saved_plan != plan:
            fail("invalid_bundle")
        files = generated_files(manifest, plan, bundle)
        integrity = strict_json(private_read(bundle / "integrity.json"))
        expected = {"version": 1, "sha256": {
            name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
        if integrity != expected:
            fail("invalid_bundle")
        for name, data in files.items():
            if private_read(bundle / name) != data:
                fail("invalid_bundle")
    except RemoteAccessError:
        fail("invalid_bundle")
    return {"bundle": bundle, "manifest": manifest, "plan": plan}


def adapt_bundle(bundle: str | Path, caddy: str | Path | None = None) -> dict:
    loaded = load_bundle(bundle)
    executable = str(caddy) if caddy is not None else shutil.which("caddy", path=clean_environment()["PATH"])
    if not executable:
        fail("command_failed")
    result = run([executable, "adapt", "--config", str(loaded["bundle"] / "Caddyfile.standalone"),
                  "--adapter", "caddyfile"], cwd=loaded["bundle"])
    try:
        parsed = strict_json(result.stdout)
    except RemoteAccessError:
        fail("command_failed")
    if not isinstance(parsed, dict):
        fail("command_failed")
    # Caddy parser warnings appear on stderr; never echo them or adapted config.
    warnings = sum(1 for line in result.stderr.splitlines() if "warn" in line.lower())
    return {"status": "parsed", "parserWarnings": warnings, "qualificationRequired": True}


def parse_dashboard_response(raw: str, expected_origin: str, expected_port: int) -> dict:
    """Exact upstream start/reuse envelope; never expose unvalidated output.

    Verified upstream cli/src/main.rs L1000-1008, L1124-1128: success follows
    spawn/state write, not readiness. access_urls contains strings, never objects.
    """
    try:
        response = strict_json(raw)
        fields(response, {"success", "data"})
        if response["success"] is not True:
            fail("invalid_response")
        data = fields(response["data"], {"port", "pid", "access_urls"}, {"already_running"})
        if (type(data["port"]) is not int or data["port"] != expected_port
                or type(data["pid"]) is not int or data["pid"] <= 0
                or ("already_running" in data and data["already_running"] is not True)):
            fail("invalid_response")
        urls = data["access_urls"]
        if not isinstance(urls, list) or len(urls) != 1:
            fail("invalid_response")
        text = urls[0]
        if not isinstance(text, str) or any(ord(c) < 33 or ord(c) == 127 for c in text):
            fail("invalid_response")
        parts = urlsplit(text)
        if (parts.username is not None or parts.password is not None
                or parts.path not in ("", "/") or parts.query or "?" in text
                or not TOKEN.fullmatch(parts.fragment)):
            fail("invalid_response")
        # Only an already-approved private-IP expected origin enables literal
        # parsing here; the public origin() default remains DNS-only.
        expected = origin(expected_origin, allow_ip=True)
        if expected != expected_origin:
            fail("invalid_response")
        try:
            ipaddress.ip_address(urlsplit(expected).hostname)
            allow_ip = True
        except ValueError:
            allow_ip = False
        if origin(text.split("#", 1)[0], allow_ip=allow_ip) != expected:
            fail("invalid_response")
        return {"url": text, "pid": data["pid"], "port": data["port"]}
    except (RemoteAccessError, ValueError):
        fail("invalid_response")


def listener_ready(pid: int, port: int) -> bool:
    """Bounded local-only readiness; does not prove remote routing, TLS, or auth."""
    for delay in (0, 0.2, 0.5):
        if delay:
            time.sleep(delay)
        try:
            sibling().verify_listener(pid, port)
            return True
        except Exception:
            pass
    return False


def start_dashboard(bundle: str | Path, project_id: str, *, allow_start: bool = False) -> dict:
    if not allow_start:
        fail("approval_required")
    if sys.platform != "darwin":
        fail("unsupported_platform")
    loaded = load_bundle(bundle)
    selected = [p for p in loaded["plan"]["projects"] if p["id"] == project_id]
    if len(selected) != 1:
        fail("invalid_arguments")
    project = selected[0]
    directory = loaded["bundle"]
    executable = loaded["plan"]["executables"]["agentBrowser"]
    if not executable:
        fail("command_failed")
    # Preserve the sibling policy's exact socket mapping (shared only when
    # explicitly enrolled there); never namespace, migrate or reconfigure it.
    socket = Path(project["socketDir"])
    no_symlinks(socket)
    if socket.parent.exists():
        parent_info = socket.parent.stat()
        if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid()
                or parent_info.st_mode & 0o022):
            fail("unsafe_path")
    else:
        private_directory(socket.parent)
    private_directory(socket)
    environment = clean_environment()
    environment["PATH"] = reviewed_runtime_path(loaded["plan"]["executables"])
    environment["AGENT_BROWSER_SOCKET_DIR"] = str(socket)
    args = [executable, "--config", str(directory / "agent-browser-empty.json"), "--json",
            "dashboard", "start", "--port", str(project["dashboardPort"]),
            "--allowed-origins", project["origin"]]
    try:
        result = subprocess.run(args, cwd=directory, env=environment, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=45, check=False)
    except subprocess.TimeoutExpired:
        fail("command_timeout")
    except (OSError, UnicodeError):
        fail("command_failed")
    if result.returncode:
        fail("command_failed")
    response = parse_dashboard_response(result.stdout, project["origin"], project["dashboardPort"])
    ready = listener_ready(response["pid"], response["port"])
    bootstrap_id = ("shared" if loaded["manifest"].get("mode") == "shared-ip" else project_id)
    target = directory / "bootstrap" / (bootstrap_id + ".txt")
    private_write(target, response["url"] + "\n", replace=True)
    return {"status": "bootstrap-saved", "bootstrapFile": str(target), "ready": ready,
            "origin": project["origin"], "projectId": project_id, "qualificationRequired": True}


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        fail("invalid_arguments")


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--manifest", required=True)
    prepare.add_argument("--output-dir", required=True)
    adapt = sub.add_parser("adapt")
    adapt.add_argument("--bundle", required=True)
    adapt.add_argument("--caddy")
    start = sub.add_parser("dashboard-start")
    start.add_argument("--bundle", required=True)
    start.add_argument("--project", required=True)
    start.add_argument("--allow-start", action="store_true")
    try:
        args = parser.parse_args(argv)
        if args.command == "prepare":
            result = prepare_bundle(args.manifest, args.output_dir)
        elif args.command == "adapt":
            result = adapt_bundle(args.bundle, args.caddy)
        else:
            result = start_dashboard(args.bundle, args.project, allow_start=args.allow_start)
        print(json.dumps(result, sort_keys=True))
        return 0
    except RemoteAccessError as exc:
        print(json.dumps({"status": "error", "code": exc.code, "message": str(exc)}))
        return 1
    except Exception:
        print(json.dumps({"status": "error", "code": "command_failed",
                          "message": MESSAGES["command_failed"]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
