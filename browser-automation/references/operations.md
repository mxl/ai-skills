# Ownership, onboarding, and recovery

## Resource model

| Resource | Owner | Policy |
| --- | --- | --- |
| Canonical `.chrome-user-data/` | Repository | Shared by linked worktrees; never shared with independent clones |
| Chrome process and dynamic local CDP listener | Project supervisor | Reused across tasks; lifecycle requires user approval |
| Named pinned session and known target | Task | No default session, arbitrary tab adoption, or auto-connect |
| Human-control lease | Task | Blocks task automation; persists until explicit completion |
| Authentication lease | Project | Blocks affected helper automation during shared auth change |
| Dashboard | Coordinating owner | Local-only; not stopped by a finishing task |
| Application/server/test-data lifecycle | Project tooling | Outside this skill |

The helper state directory is outside the project, defaults to `~/Library/Application Support/browser-automation`, and uses owner-only directories/files, atomic state replacement, and a per-project OS file lock. State is coordination metadata, not a credential store. The CLI has no runtime-location override: all cooperating tasks for the same canonical project must see the same locks and leases. Unit tests substitute isolated stores without changing this production contract.

Locking serializes helper operations within a project, not arbitrary raw CDP/CLI access. Different projects have different locks. Cooperating agents must use the wrapper and respect human ownership. This is not hard multi-user authorization.

## Onboarding

Resolve a linked worktree to its canonical main checkout; don't use branch names, remote URL, or current directory basename as project identity. Ask if an explicit non-Git root is ambiguous. Independent clones are intentionally separate.

Before starting a new profile, verify that it is untracked and ignored by Git. With permission, add `/.chrome-user-data/` to the canonical root `.gitignore`. Preserve user edits, existing ignore rules, browser policy, and applicable instructions. Never rewrite AGENTS.md automatically.

A legacy `.agent-browser-profile/` is a migration question. Stop and coordinate shutdown before moving browser data. A fresh empty directory is not a sign-in-preserving replacement. If old and new data both exist, explain the conflict and ask; do not merge profile databases.

Profile symlinks and unclear ownership require explicit resolution. Do not bypass Chrome singleton locks, delete lock files, copy cookie databases, or copy Keychain/encryption material.

## Process and endpoint verification

Before attachment or meaningful task operations:

1. Find the live main Chrome process with the exact canonical user-data directory. Child renderer processes are not separate profile owners. Conflicting/ambiguous main owners block reuse.
2. Discover the actual port/browser target from the exact profile's `DevToolsActivePort` file.
3. Verify the listener belongs to the expected process and is loopback-only.
4. Verify local `/json/version` identifies that browser target without following redirects or using proxies.
5. Compare the browser's recorded identity with task state. A restarted browser invalidates old target bindings even when it uses the same profile.

No endpoint is trusted solely because it answers on a familiar port. Public output need not contain debugging WebSocket URLs or page URL query/fragment data.

## Approval boundary

Launch switches and caller declaration flags are accident guards, not attestation of human consent. Use the host question tool before launch/restart/migration/auth changes. The helper cannot independently prove that a human approved a CLI flag.

No routine task command may stop shared Chrome, change profiles, install/update dependencies, or invoke broad `close --all`/process-kill operations.

## Queued headed fallback

The helper does not automatically orchestrate a live mode transition. A blocked task reports `headed_required` or equivalent workflow status and queues a human decision:

1. Try the existing task's local headless viewport first.
2. Explain the native-UI limitation, affected tasks, and in-memory state that may be lost.
3. Obtain explicit lifecycle approval through the question tool.
4. Ask all task owners to reach safe checkpoints; active human leases cannot be implicitly cleared. If any task cannot checkpoint, keep the transition pending.
5. The coordinating owner closes or preserves task state deliberately and shuts down only the verified project Chrome. Profile shutdown must finish before reuse. Do not kill unrelated processes or start a second Chrome against the profile.
6. Start headed with the same canonical profile using the approved lifecycle operation. Complete native interaction with task-control/auth coordination.
7. Before returning to headless, repeat coordination and approval. Do not infer that the first approval grants every future restart.
8. Re-verify browser identity, reattach tasks deliberately, take fresh safe snapshots, and verify account/tenant. Reopening a URL does not restore unsaved forms, JavaScript state, or a pending OAuth transaction.

If exact continuity is required and cannot survive restart, report the blocker. Separate bootstrap browsers and auth transfer are not an automatic fallback in v1.

## Failure recovery

| Condition | Response |
| --- | --- |
| Missing browser | Report; ask before launch |
| Profile mismatch / unknown CDP | Stop; never substitute another browser |
| Missing pinned target | `tab_gone`; do not choose a neighbor |
| Target owned by another task | Stop; do not take over |
| Browser restarted | Old bindings are stale; coordinate explicit task recovery |
| Attach/command outcome uncertain | Preserve metadata; require explicit reconciliation before handoff or cleanup; a client timeout does not prove daemon work finished |
| Viewer disconnect / user idle | Keep human lease; ask resume/cancel |
| Account differs | Keep paused; coordinate project authentication |
| Cleanup fails | Preserve state, report blocker; no broad close |
| External controller bypasses helper | Stop affected tasks and reassess ownership; no hard isolation claim |

Failure preservation does not authorize later cleanup of a human-held task. The coordinator must explicitly resolve ownership first.

## Scope exclusions

No credential extraction, password-manager integration, session export/import, cookie syncing, cloud browser, remote dashboard exposure, live profile cloning, VM provisioning, browser installation, test-data resets, app-server startup, commits, or pushes is included. These require separate reviewed workflows and approval.
