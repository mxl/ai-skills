# Private remote viewport setup

Use this branch when the human enters data from another computer. Chrome, profiles, agents, task targets, CDP, and raw streams remain on the automation host. This is an explicit private LAN/VPN setup for one fully trusted operator, not public hosting, view-only access, or multi-user authorization.

## Choose the dashboard layout

For **one IP endpoint serving multiple projects**, follow [shared-dashboard.md](shared-dashboard.md): explicitly enroll projects in one private controller runtime, migrate existing task controllers only at approved safe checkpoints, and use `mode: "shared-ip"`. Chrome profiles and project leases remain separate. This mode has one dashboard origin, one proxy, no hub route, and no DNS/hosts changes.

The **hostname mode** below is the alternative for isolated project dashboards. It remains the default for manifests without an explicit mode. Do not silently switch layouts or migrate controllers during configuration preparation.

## Hostname-mode architecture

```text
Remote human browser
  -> approved LAN/VPN + HTTPS
  -> Caddy bound to the approved private interface
     -> hub hostname: non-secret project links only
     -> project-A hostname: project-A loopback dashboard
     -> project-B hostname: project-B loopback dashboard
        -> each dashboard proxies its own task streams internally
```

In hostname mode, each project dashboard uses exactly the project's existing `AGENT_BROWSER_SOCKET_DIR`. The dashboard discovers sessions only in its effective socket directory. A common directory is permitted only through the separate approved shared-runtime workflow, never as an improvised discovery fix. Do not revive namespace assumptions or expose raw CDP/stream endpoints.

Use distinct hostnames, not just different ports: dashboard cookies are host-bound but not port-bound, and the frontend has root-relative routes. Subpath rewriting is unsupported. The hub contains plain project links, never tokenized bootstrap links, profile paths, or secrets. Hostnames alone are not an authorization boundary; the operator is trusted with full control of all sessions exposed by a dashboard.

## 1. Discover and agree on non-secret deployment settings

Inspect versions, listener occupancy, service ownership, and the project's socket-directory contract without printing credentials, environment dumps, `dashboard.config`, private keys, or existing Caddy secrets. Starting a dashboard is not read-only discovery: it can start a process and print bearer links.

Ask through the question tool for decisions that cannot be discovered safely:

- Intended shared HTTPS IP endpoint, or hub/per-project hostnames for hostname mode, including any explicit port.
- Approved host LAN/VPN interface address and remote peer addresses/CIDRs.
- Existing Caddy service/configuration ownership and intended additive integration.
- TLS choice: existing trusted certificate or explicitly approved internal CA.
- Approval scope for file changes, dashboard startup, proxy activation, persistence, trust installation, and live validation. These are separate from approval to prepare files.

Prefer exact remote peer `/32` or `/128` entries. The generator permits RFC1918, IPv6 ULA, and explicitly selected CGNAT VPN ranges; a syntactically private address is not proof of interface ownership, routing, or authorization. Verify actual interface ownership before activation. If the interface disappears, stop/report; never broaden to wildcard binding.

No VPN installation, router forwarding, Funnel/public sharing, DNS API credentials, public certificate issuance, or firewall changes are implicit.

## 2. Prepare a private review bundle

Input is a non-secret JSON manifest. Keep concrete deployment files outside Git. Copy [the two-project example](../assets/remote-manifest.example.json) to a private location and replace its placeholder roots, origins, addresses, and ports before preparing a bundle. Example values below are placeholders, not discovered addresses:

```json
{
  "version": 1,
  "hubOrigin": "https://viewports.home.arpa:8443",
  "bindAddress": "100.101.102.103",
  "allowedPeers": ["100.101.102.104/32"],
  "tls": {"mode": "internal"},
  "caddyAdminPort": 2029,
  "projects": [
    {
      "id": "shop",
      "root": "/absolute/path/to/project",
      "origin": "https://shop.viewports.home.arpa:8443",
      "dashboardPort": 4848
    }
  ]
}
```

For an existing certificate, replace `tls` with:

```json
{"mode":"existing","certificate":"/approved/fullchain.pem","privateKey":"/approved/private-key.pem"}
```

These are paths, not key contents. Preparation does not read or provision the key. Verify certificate SANs, permissions, expiration, and renewal responsibility before activation.

```bash
python3 <skill-dir>/scripts/remote_access.py prepare --manifest <private-manifest.json> --output-dir <new-private-bundle>
```

Preparation validates strict inputs, resolves canonical project roots/socket directories, and generates owner-only review artifacts outside Git. It does not start services, change runtime enrollment, migrate controllers, or inspect browser profile contents. In shared-IP mode all member roots must already select the same runtime; it generates only one site/proxy, one shared LaunchAgent, and one shared bootstrap file. The bundle includes:

- Normalized non-secret manifest and plan, plus integrity hashes.
- `Caddyfile.sites`: additive hub/project site definitions.
- `Caddyfile.standalone`: a candidate for a separately approved new instance only.
- `GLOBAL-REQUIREMENTS.md`: settings to reconcile with an existing Caddy instance.
- `hub/index.html`: plain project links only.
- Per-project launchd templates: not installed or loaded automatically.
- An empty explicit agent-browser configuration; no inherited auto-connect, credentials, AI gateway, proxy, provider, or namespace configuration.

Hashes detect accidental changes to the prepared bundle; they are not proof that a human approved it. Preserve existing Caddy sites and all concurrent project/browser work. Do not replace the global Caddyfile with the generated standalone candidate.

## 3. Parse, then distinguish provisioning validation

```bash
python3 <skill-dir>/scripts/remote_access.py adapt --bundle <private-bundle>
```

This runs only `caddy adapt` without `--validate`, captures its output privately, and reports bounded status. It checks adaptation, not listener reachability, TLS trust, dashboard authentication, or browser behavior.

`caddy validate` and `caddy adapt --validate` load/provision modules. They may read keys and initialize state; they are not a read-only parser. Obtain separate approval and use deliberately isolated storage for provisioning tests. Do not use `caddy reload` as a validation command: reload applies configuration.

## 4. Review Caddy's exposure and integration

For each site, the generated config:

- Explicitly `bind`s the approved private IP. Merely placing a private hostname/IP in a site address is insufficient.
- Allows only explicit `remote_ip` peer CIDRs. It does not trust arbitrary forwarded client-IP headers, PROXY protocol, or a blanket `private_ranges` shortcut (which excludes common CGNAT VPN addresses).
- Proxies only to the project's dashboard at `127.0.0.1:<dashboard-port>`. CDP and raw stream ports never appear as upstreams.
- Preserves external Host and Origin. Rewriting them to localhost can bypass or break the dashboard's intended authorization checks.
- Relies on Caddy's normal WebSocket support; no manual Upgrade/header hacks.
- Requires explicit TLS; no silent public ACME fallback.
- Leaves request access logging disabled. Do not enable `log_credentials`, request/header/body dumps, or TLS key logs. Audit existing observability separately.

Standalone globals suppress automatic trust installation (`skip_install_trust`) and HTTP redirects (`auto_https disable_redirects`), explicitly bind the admin API to loopback, and use HTTP/1.1 + HTTP/2 rather than adding a UDP HTTP/3 listener. For an existing instance, reconcile these globals and any existing listener/trusted-proxy/logging policy before applying an additive snippet. If doing so would disrupt existing sites, stop and propose a separately approved isolated instance on free ports with its own private storage; do not silently change global redirect/trust/admin policy. A site's `bind` alone is not a complete audit of other global listeners.

Caddy reload may disconnect WebSockets. Pause/coordinate affected human viewers first; keep their leases intact. Neither proxy reload nor dashboard restart requires a Chrome restart.

## 5. Start project dashboards with private bootstrap handling

Only after explicit dashboard-start approval:

```bash
python3 <skill-dir>/scripts/remote_access.py dashboard-start --bundle <private-bundle> --project shop --allow-start
```

The helper starts/reuses only the selected dashboard, using its approved loopback port, exact external HTTPS origin, and configured socket directory. In shared-IP mode any listed member addresses the same instance and writes `bootstrap/shared.txt`; this is not a separate dashboard per member. It does not start Chrome, change profile/leases, activate Caddy, install services, or stop a conflicting dashboard. Approval flags are accident guards; host consent remains authoritative.

Dashboard startup JSON contains a reusable bearer URL. The helper captures it through a private pipe, validates its exact intended origin/shape, and stores only the bootstrap link in an owner-readable file outside Git. Ordinary output contains a status and file path, not the link or token. Do not run raw `dashboard start --json` in an agent-visible terminal to retrieve the URL.

Use the question tool to direct the human to retrieve the file through their own trusted terminal/SSH session or another approved private channel. Do not have the agent `cat` it, place it in a QR/screenshot, copy it into chat, or embed it in the hub.

Startup success alone is not readiness. Verify the dashboard PID/listener and real remote authentication behavior before entering credentials. Unknown installed-version output fails closed. Do not weaken validation or print raw subprocess output for debugging.

### Authentication facts

The reviewed dashboard uses a fragment bootstrap into a `Secure`, host-bound, `SameSite=Strict` cookie. The cookie is JavaScript-set and **not HttpOnly**. The token is reusable and has no server-side TTL; matching dashboard starts reuse it. Restarting the dashboard rotates it. It is not a one-time or short-lived link.

Exact allowed origins are browser-origin policy, not a network ACL. Caddy/interface routing/peer allowlists provide the separate private-network boundary. Never rewrite remote Host/Origin to localhost to bypass failed checks. Treat bootstrap files, dashboard state, cookies, and live page content as sensitive.

## 6. DNS, TLS trust, and persistence require explicit application

Shared-IP mode needs no DNS or hosts-file changes; the certificate must cover the exact private IP. In hostname mode, resolve hub/project names to the approved bind address on the human's computer using an approved existing DNS/hosts mechanism. Do not edit remote hosts files or network settings silently.

For internal TLS, Caddy must provision its CA/certificates after approved activation. Transfer **only** the public `root.crt`, verify its SHA-256 fingerprint through an authenticated channel, and obtain approval before trusting it on the remote computer/browser. Never transfer the CA private key. The server does not need to trust its own CA merely to serve HTTPS. No `-k`, certificate-warning bypass, or automatic `caddy trust` is part of qualification.

For existing certificates, verify the exact hostname/SAN, trust chain, key permissions, and renewal mechanism. Public DNS-01 or existing VPN certificate integrations can be planned separately; no DNS secret, VPN install, or certificate-transparency disclosure is automatic.

Generated launchd plists are templates only. Preparation snapshots the discovered absolute agent-browser and Node paths into the private plan; persistence uses those reviewed tool directories plus fixed system directories, not a login shell's PATH. Missing tools require installation and bundle regeneration under separate approval, not silent runtime rediscovery. Review absolute executable/bundle paths, service labels, port conflicts, private logs, and standing dashboard-only restart permissions. RunAtLoad/periodic ensure must invoke the safe wrapper, not a raw CLI whose token output enters logs. Installing/loading/enabling/disabling a LaunchAgent is a separate approved mutation. KeepAlive spin loops and `caddy start` nested under service supervision are not the default.

Existing Caddy persistence belongs to its coordinating owner. Verify its service identity can read only the files it needs. If it runs as a different user, do not relax permissions on the whole bundle: bootstrap links must remain operator-only. Plan separately approved staging of only the necessary Caddy/hub assets and certificate access. Use additive reviewed configuration and the owner's approved reload procedure. For a new instance, explicitly approve its binary, foreground `caddy run`, config, private storage, admin/listener ports, logs, and service identity. No global Caddyfile overwrite, `--resume`, environment dump, or competing service on occupied ports.

## 7. Validate before real credentials

First perform offline fixture tests. Separately approve any temporary listener, TLS provisioning, dashboard launch, remote-device check, or browser run.

Acceptance must establish:

1. HTTPS works on the actual remote computer without bypassing trust; DNS/SAN match.
2. An unapproved peer is denied; a disappearing interface does not produce wildcard fallback.
3. Missing/wrong bootstrap credentials, foreign Origin, and Host mismatches are rejected by the real installed dashboard's APIs and WebSockets.
4. HTTP and bidirectional WebSocket proxying preserve Host/Origin and stay on the dashboard origin. No raw stream/CDP port is remotely reachable.
5. Each project dashboard discovers only its intended socket directory; no controller migration or session takeover occurs.
6. Human ownership, site-scoped concurrency opt-in, disconnect pause, and explicit resume behavior are preserved.
7. Synthetic credential/token markers do not reach agent output, hub files, Git, routine logs, captures, or gateway traffic.
8. Restart/reload rotates or invalidates links as expected without restarting Chrome or automatically resuming tasks.

A fake backend can qualify proxy plumbing, not the actual dashboard's security. Mark live remote qualification pending until the installed dashboard and remote browser pass these checks.

## Sources and version scope

Public research on 2026-11-05; implementation snapshots are evidence, not installed-version qualification:

- [agent-browser dashboard lifecycle](https://github.com/vercel-labs/agent-browser/blob/1f66e398d8ae31f2601fe142af0c84852b2fa896/cli/src/main.rs#L761-L1170)
- [Dashboard authentication/proxy](https://github.com/vercel-labs/agent-browser/blob/1f66e398d8ae31f2601fe142af0c84852b2fa896/cli/src/native/stream/dashboard.rs#L143-L830)
- [Socket-directory session discovery](https://github.com/vercel-labs/agent-browser/blob/1f66e398d8ae31f2601fe142af0c84852b2fa896/cli/src/native/stream/discovery.rs#L1-L120)
- [Caddy bind](https://caddyserver.com/docs/caddyfile/directives/bind), [matchers](https://caddyserver.com/docs/caddyfile/matchers#remote-ip), [reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
- [Caddy globals](https://caddyserver.com/docs/caddyfile/options), [TLS](https://caddyserver.com/docs/caddyfile/directives/tls), [validation lifecycle](https://caddyserver.com/docs/command-line), [logging](https://caddyserver.com/docs/caddyfile/directives/log)
