# Helper command contract

Run `python3 <skill-dir>/scripts/browser_session.py --help` for the installed interface. Global options precede the command:

```text
browser_session.py [--project PATH] COMMAND ...
```

Python 3.10+ standard library only. Live operations target macOS; pure functions and mock tests may be exercised without Chrome. Production coordination always uses the invariant per-user runtime directory; there is no CLI state-directory override that could split one project's locks or authentication leases. Unit tests patch the module constant or construct isolated stores directly.

## Commands

| Command | Purpose | Approval / guard |
| --- | --- | --- |
| `inspect` / `status` | Resolve canonical root and check browser/onboarding state | No browser launch |
| `start --allow-launch [--headed] [--chrome PATH]` | Start only when no conflicting project browser/tasks exist | Explicit host approval first; flag is an accident guard |
| `attach --task ID --url URL` | Create a pinned task tab in the existing verified project browser | Never auto-launch; clean URL, no userinfo/query/fragment |
| `run --task ID -- COMMAND ARGS` | Run a supported ordinary page command with forced session/CDP/pin/namespace | Reject during task human lease or project auth lease |
| `handoff --task ID --captures-stopped [--auth]` | Persist task human ownership, optionally project auth ownership | Ask human first; stop capture outside helper too |
| `viewer --task ID [--allow-start --port PORT]` | Inspect handoff; optionally enable only its task-scoped loopback stream through the sanitized helper environment | Approve stream preparation first; no Chrome/dashboard launch or verified human connection |
| `resume --task ID --human-confirmed [--account-verified]` | Explicitly return task control after completed input | Auth handoff requires account verification declaration |
| `release --task ID --outcome success` | Close only the recorded owned target and release task | Human/auth leases must be resolved first |
| `release --task ID --outcome failure` | Preserve task/tab for recovery | Never shuts down shared browser |

The helper emits JSON. Control operations return bounded metadata/status rather than raw page/CLI output. Errors are fixed codes/messages, not forwarded subprocess stderr. `run` returns normal supported page-command results; those can contain private application content and are not globally redacted.

## Safe command surface

Use the helper's allowlist for ordinary navigation/DOM interaction. It is intentionally narrower than agent-browser's complete API. Check the actual parser before relying on an operation. The following are not a supported escape hatch:

- Caller-provided session/CDP/profile/namespace/provider/restore/config/launch flags.
- Default sessions, auto-connect, raw endpoint selection, global lifecycle commands.
- Arbitrary eval, credential tools, cookies/storage exports, network capture, HAR, tracing/recording, plugin execution, tab switching, or broad close operations.
- Password/OTP entry via fill/type/press, stdin payloads, shell expansion, or question answers.

The helper cannot determine that every arbitrary string supplied to a normal `fill` is non-secret. This remains a host policy plus human-input boundary, not automatic semantic secret detection.

Examples use non-secret text only:

```bash
python3 <skill-dir>/scripts/browser_session.py --project <root> attach --task issue-42 --url https://example.com
python3 <skill-dir>/scripts/browser_session.py --project <root> run --task issue-42 -- snapshot -i
python3 <skill-dir>/scripts/browser_session.py --project <root> run --task issue-42 -- click @e3
python3 <skill-dir>/scripts/browser_session.py --project <root> run --task issue-42 -- fill @e4 "non-secret search text"
```

## Handoff flags do not prove safety

`--captures-stopped`, `--human-confirmed`, and `--account-verified` record explicit caller declarations. They do not authenticate the human, inspect every recording pipeline, or prove that the destination website accepted a login.

A human lease has no automatic expiry-to-resume. Viewer disconnect or a stale task does not authorize a different task to take over. Account switches still affect shared project state even when task tabs are pinned.

## Known recovery boundary

No automatic restart, mode switch, process kill, profile migration, update, global installation, password-manager integration, or auth transfer is provided. Follow [operations.md](operations.md) for approval and coordination. Missing targets and uncertain attachment outcomes fail closed rather than selecting a new tab automatically.

## Dependency/version handling

Load `agent-browser skills get core` to use documentation bundled with the installed CLI. The source research covered upstream 0.41.1; the development host reported 0.41.0. Neither number alone qualifies live behavior. Do not upgrade the tool or restart a running browser to resolve a mismatch without approval.
