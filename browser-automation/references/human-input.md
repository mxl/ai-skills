# Human control and credential entry

## Supported claim

A local live viewport can send human input to an existing headless Chrome page without restarting Chrome. It can keep typed credentials outside the agent conversation and ordinary CLI command arguments. It is **not** a secrecy boundary against the viewer, daemon, destination page, other trusted local processes, or unrestricted CDP access.

Use the existing task's named, pinned session. A second session must not claim its tab. The helper's persisted lease coordinates cooperating agents; direct raw CLI/CDP commands can bypass it and are outside the supported workflow.

## Handoff procedure

1. Ask through the host question tool: project, task, intended origin, purpose, and whether shared authentication changes. Collect consent, not credentials.
2. Stop the task's command loop and capture pipelines. A helper command holds the project lock until its subprocess finishes, so handoff waits for active helper commands. Coordinate all other controllers of that target.
3. If login/logout, account/organization switching, or cookie/authentication changes are involved, use a project authentication lease (`handoff --auth`). Shared cookies are not isolated by pinned tabs or different localhost ports.
4. Stop screenshots, traces, recordings, screencasts consumed by the model, HAR/network collection, raw console forwarding, and dashboard activity persistence where supported. Confirm this before `--captures-stopped`. The helper does not enumerate or prove absence of every recorder.
5. `handoff` records human ownership before viewer preparation. Check project/session/target/origin. A closed or changed target is a blocker, not permission to retarget keyboard input.
6. Prepare a loopback-only viewer using the installed agent-browser's version-matched core/dashboard/streaming documentation. See the helper's `viewer` output if available. Starting the viewer is within the specifically approved handoff scope, not authority to launch a new Chrome, change its profile, or expose a remote service.
7. The human opens the existing task session's viewport in their own browser. Avoid selecting/switching unrelated tabs. No dashboard AI Chat or external AI gateway is part of credential entry.
8. The human types credentials or OTPs directly. Password values never appear in question answers, helper arguments, here-documents, environment assignments, model-visible stdout, or generated scripts. The agent neither types nor reads the secret.
9. Viewer disconnect, inactivity, or task failure leaves human ownership intact. Ask for explicit completion/cancellation; a timer may notify, but cannot authorize resume.
10. Ask the human to finish submission and leave the page safe for automation, then confirm completion. For authentication, verify the intended account/tenant without reading secret fields. Only then use `resume --human-confirmed` (and `--account-verified` for auth). Resume flags are caller declarations, not verified website success.
11. Obtain a fresh safe snapshot, verify the expected destination/account, and continue. If a credential remains visible or a sensitive login page is still active, keep the task paused instead.

## Viewer preparation and ownership

The upstream CLI documents:

```text
agent-browser dashboard start
agent-browser stream enable --port <available-local-port>
agent-browser stream status
agent-browser stream disable
```

These are capabilities, not permission to issue them against a default session. Use the helper for task stream preparation so it supplies the verified namespace/session/CDP/pinning and sanitized child environment:

```bash
python3 <skill-dir>/scripts/browser_session.py --project <root> viewer --task <task-id>
python3 <skill-dir>/scripts/browser_session.py --project <root> viewer --task <task-id> --allow-start --port <approved-unused-port>
```

The first command is discovery-only. The second requires an existing human lease and explicitly approved unused local port; it enables only that task's stream and checks that its listener is loopback-only. It does not start Chrome, start the dashboard, open a viewer, or prove that a human connected. It does not authenticate arbitrary local processes.

The coordinating owner prepares the dashboard separately, using a trusted clean environment and the installed version's local-only setup. Do not stop/restart an existing shared dashboard or weaken namespace isolation to make discovery work. Verify that it discovers the intended namespaced session and preserves the selected CDP target. Otherwise stop and report the qualification gap. Do not invent a dashboard URL from the stream port, publish access tokens, or expose raw CDP/stream ports remotely.

Stream preparation is the narrowly scoped exception to ordinary page commands while human-leased. It must not navigate, type, evaluate, capture into model artifacts, switch tabs, or change ownership. A successfully enabled stream is not proof that the viewer is privacy-qualified.

The dashboard is shared infrastructure. Do not stop its service as part of one task's cleanup while other viewers use it. Viewer disconnect does not imply Chrome stopped.

## Capture and privacy limits

- Password dots hide pixels, not DOM values, CDP, page JavaScript, or network content.
- Screenshot masking does not sanitize trace snapshots, videos, HAR files, errors, console messages, or cookies.
- Direct viewport keyboard input uses plaintext within trusted local processes and CDP. Loopback binding does not authenticate against arbitrary local processes.
- Disable optional dashboard AI Chat during sensitive use. Do not assume all activity/console feeds are redacted or persistence is disabled by default.
- Browser profiles intentionally persist session material. Treat them as sensitive even though Git ignores them. Do not copy cookies, export auth state, save passwords to an extra vault, or upload artifacts without separate scoped approval.
- This version has no Bitwarden CLI, Agent Access, password-provider, clipboard-secret, or secret-returning MCP integration. Do not add one as an improvised recovery path.

## Unsupported challenges

A page viewport is not a full remote desktop or browser-chrome mirror. Native passkey/biometric/security-key prompts, permission UI, file choosers, IME behavior, popups, and clipboard integration require explicit qualification.

When a native visible browser is necessary, offer a queued, approved mode transition described in [operations.md](operations.md). Do not pretend a virtual WebAuthn test authenticator can use a real existing passkey. Do not disable MFA to avoid a blocker.

## Evidence baseline

Public sources reviewed on 2026-11-02; no real credentials tested:

- [agent-browser dashboard](https://github.com/vercel-labs/agent-browser/blob/5868f7c05020e047c00e68e4abf9253cce864f41/docs/content/docs/dashboard.mdx)
- [agent-browser streaming](https://github.com/vercel-labs/agent-browser/blob/5868f7c05020e047c00e68e4abf9253cce864f41/docs/content/docs/streaming.mdx)
- [Viewport input handler](https://github.com/vercel-labs/agent-browser/blob/5868f7c05020e047c00e68e4abf9253cce864f41/packages/dashboard/src/components/viewport.tsx#L332-L421)
- [CDP input dispatch](https://github.com/vercel-labs/agent-browser/blob/5868f7c05020e047c00e68e4abf9253cce864f41/cli/src/native/stream/websocket.rs#L493-L669)
- [Chrome headless debugging](https://developer.chrome.com/docs/automation-and-testing/debug-headless)
- [OWASP secrets management](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html)
- [OWASP logging exclusions](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html#data-to-exclude)
- [Playwright artifact privacy](https://playwright.dev/docs/ci-intro#properly-handling-secrets)
