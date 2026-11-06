# Qualification and acceptance tests

## Current evidence categories

Keep these distinct in reports:

1. **Source-reviewed:** public documentation and selected upstream implementation were inspected.
2. **Unit/simulated:** filesystem/process/CLI behavior was tested with disposable fixtures and mocks.
3. **Live-qualified:** the actual installed browser/tool combination passed a separately approved disposable-browser run.
4. **Real-account authorized:** the user separately approved testing a real signed-in profile.

Do not promote one category to another. The initial implementation is not live-qualified until evidence is recorded.

## Approval before live tests

Present the disposable directories, synthetic test pages/credentials, intended browser launches, local services/ports, and cleanup scope through the question tool. Use a new disposable user-data directory under a disposable project, not an existing project profile. Never restart the user's shared Chrome to run tests.

No dependency installation, global skill installation/configuration, real vault use, real account, external upload, commit, or push is implied by approval to build the skill.

## Unit and simulated coverage

Use Python stdlib unittest and temporary directories. Stub process inspection, Chrome startup, CDP HTTP responses, agent-browser responses, and listener discovery. Use real Git init/worktree operations only in disposable test repositories. No Chrome/agent-browser daemon needs to run.

Required checks:

- Main checkout and linked worktree resolve to one project; independent clones differ.
- Non-Git roots need explicit selection.
- Legacy profile, profile symlink, tracked browser data, missing ignore rule, and conflicting profile ownership fail closed.
- Startup is serialized by a project lock; missing explicit launch guard cannot call Popen.
- Dynamic endpoint discovery verifies process identity, loopback listener, browser target, and no proxy/redirect substitution.
- Task names are unique across projects/tasks; default session and caller overrides are rejected.
- Attachment verifies a newly owned target; unknown response shapes and partial outcomes preserve uncertainty instead of retrying blindly.
- A missing/changed pinned target cannot navigate a neighbor.
- A human-held task rejects ordinary run/cleanup commands; project auth lease blocks other tasks too.
- Human leases persist beyond viewer disconnect and elapsed time; resume requires explicit declarations.
- Failure preserves tabs; success closes only the known owned target; no `close --all`, Chrome stop, or unrelated-tab cleanup.
- Child environment excludes inherited agent-browser selectors/launch overrides and credential-manager session variables.
- Secret-canary error strings and URL credentials/query/fragment values never appear in helper metadata/error output.
- State permissions, symlink refusal, atomic replacement, and malformed-state handling are safe.

## Disposable live matrix

After approval, exercise two disposable projects with at least two tasks each. Record versions, macOS, hardware, display scaling, launch mode, and enabled features including `ForceMacVSyncTimerForDebugging`. Do not claim a throughput improvement without measurement.

### Browser continuity

- One project Chrome starts despite simultaneous startup attempts.
- Distinct projects use distinct profiles/PIDs/listeners.
- Viewing a headless target preserves Chrome PID, target IDs, cookies, and unsaved synthetic form contents.
- Session and task isolation survive unrelated task navigation and daemon reconnect where supported.
- Closed target produces safe failure.

### Human handoff

- A task command in flight completes before handoff acquires ownership.
- The human controls the existing session/target, not a fresh competing session.
- Task automation is rejected throughout human ownership.
- Other projects continue; same-project tasks behave according to task versus authentication lease scope.
- Viewer disconnect leaves the task paused; no timer resumes it.
- Resume requires explicit human completion and authentication verification where applicable.
- Merely viewing does not steal desktop focus or retarget another session. Test explicit tab selection separately.

### Synthetic credential privacy

Use generated canary credentials, not real credentials. Inspect normal and failure paths for canaries in tool transcripts, helper output/state, CLI Activity/Console, dashboard persistence, screenshots/videos/traces, HAR/network captures, and optional AI-gateway traffic. Report only pass/fail and paths; do not create a real-secret scanner report containing secret values.

Check that AI Chat is unused, no unexpected capture runs, and viewport input goes only to the approved target/origin. A successful canary run is bounded evidence, not proof that unrestricted CDP cannot read passwords.

### Input and authentication limitations

Exercise mouse/wheel, Tab/Shift-Tab, Enter, Cyrillic/IME, paste, scaling/Retina, popup navigation, OTP, file chooser, browser permissions, and passkeys using test fixtures where feasible. Mark unsupported features clearly. A virtual authenticator proves a test flow, not access to the user's physical authenticator.

### Lifecycle and recovery

Verify owner-scoped cleanup, failed-task preservation, explicit browser restart handling, and dummy sign-in persistence after an approved restart. Do not force a mode transition while another task has non-resumable work. Restart and profile migration require separate scope confirmation.

## Skill behavior evals

`evals/evals.json` contains prompts for paired with-skill/baseline agent runs. Evaluate plans in a no-browser, no-secrets environment first. Check both correct routing and refusal to use unsafe recovery shortcuts.

Use the OpenCode Skill Creator review tools when available. If `skill_serve_review` or related tools are unavailable, record that blocker; do not claim a viewer was launched or substitute an unreviewed custom browser service. Preserve paired outputs and grading artifacts for later review.

## Private remote-access qualification

The separate remote helper's unit tests cover strict manifest validation, private interface/peer ranges, distinct hostnames, port conflicts, generated Caddy routes/headers/globals, hub exclusion of bearer links, owner-only files, bundle integrity, exact project socket directories, secret-output suppression, and launchd templates that are not installed. Parser-only `caddy adapt` may be checked with disposable files; never substitute `validate`, `run`, or `reload` under offline approval.

For live remote access, follow the separately approved [remote acceptance matrix](remote-access.md#7-validate-before-real-credentials): actual remote HTTPS trust, peer denial, API/WS token and Origin checks, socket-directory discovery, synthetic credential privacy, and lease preservation. A generated config or successful adaptation does not establish those properties. No Caddy service, trust store, DNS, or network change is authorized merely by running the unit suite.

## Acceptance report

Report exact test commands and results, installed versions tested, source revisions consulted, remaining gaps, and whether any live browser/account was accessed. Mark browser qualification **pending** until the live matrix actually passes. Global availability through an existing repository skill path is not proof of validation.
