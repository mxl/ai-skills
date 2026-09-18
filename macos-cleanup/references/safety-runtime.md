# Guarded Audit Runtime

## Entry points

Use `scripts/audit-all.sh` for the existing CLI. It delegates to Python 3 without installing dependencies. Child collectors run under `audit_runtime.py`, which installs native Darwin dataless protection before exec.

`python3 scripts/audit_runtime.py --check-guard` checks native policy support without reading cloud content. For a narrow collector, `--guard-exec` installs the policy then executes the supplied argument vector. This low-level entry point is not a cleanup authorization mechanism and does not supply the orchestrator's timeout and output management by itself.

The orchestrator establishes the guard before checking user-selected module/output paths. Initial failure returns fixed diagnostic JSON and exit code 78 without creating a run directory. Cancellation after a run starts preserves a partial sanitized report, marks remaining modules NOT-RUN, and returns 130. SIGTERM/SIGINT handling covers output processing as well as child execution; SIGKILL or power loss cannot guarantee temporary-file cleanup.

The orchestrator is the normal supervised entry point. Individual shell collectors must not be used as an unguarded fallback if protection cannot be installed.

## Guarantees and boundaries

- Native `setiopolicy_np` type `IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES`, scope `IOPOL_SCOPE_PROCESS`, policy `IOPOL_MATERIALIZE_DATALESS_FILES_OFF`; verify using `getiopolicy_np`.
- No global iCloud switch, no background-download cancellation and no changes to another application's policy.
- Reads that require dataless materialization fail instead of silently downloading payload. Metadata enumeration is distinct and can require provider metadata; incomplete enumeration must remain visible.
- This is not a sandbox for untrusted programs. A deliberately hostile process can change policy, escape a process group or invoke unrelated services. The runtime supervises trusted bundled collectors and known owner tools, not arbitrary downloaded scripts.
- Process groups control ordinary descendants on timeout/cancel, including children that outlive their immediate parent. Do not claim containment of intentionally detached sessions.
- Output is streamed through bounded pipes into private files; the combined saved stdout/stderr never exceeds the configured limit. The orchestrator removes temporary raw streams after validation and records sanitized failure diagnostics.
- Disabling materialization also prevents a normal helper from accidentally opening cloud-only payload through an unexpected path. It does not bound remote metadata enumeration by itself; scope and runtime limits remain necessary.
- Structured output is evidence, not a command channel. No arbitrary argv from an inventory is executed.
- The package discovery CLIs may maintain metadata themselves. The workflow avoids cleanup commands but is not an OS-enforced read-only filesystem sandbox.

## Why the guard exists

Apple File Provider fetches the payload of a dataless file when content is read. Content search, hashing and previews are therefore outside metadata-only audit. Apple's own `du` already attempts to suppress materialization; the incident involving a content-reading `rg` process does not justify claiming that all metadata tools inherently download cloud files.

Sources:

- https://developer.apple.com/documentation/technotes/tn3150-getting-ready-for-data-less-files
- https://github.com/apple-oss-distributions/file_cmds/blob/file_cmds-430.100.5/du/du.c
- https://github.com/dundee/gdu/blob/master/pkg/fs/dataless_darwin.go
- https://developer.apple.com/videos/play/wwdc2021/10182/

## Verification levels

Run `sh tests/run-tests.sh` for the bundled regression suite. It uses synthetic owner commands and temporary local files, not actual cleanup commands or personal cloud data. The native policy check does not read dataless content. No third-party packages are installed by this runner.

1. Unit tests: unsupported policy, errors, validation, private output and synthetic subprocess trees.
2. Native Darwin test: set/get policy and inheritance through exec, without opening user cloud files.
3. File Provider integration test: disposable test provider/account with fetch instrumentation and placeholders. This is separate, not implied by level 1 or 2.
4. Agent behavior evaluation: simulated prompts with no destructive tools or real user paths. A passed unit suite is not a passed agent benchmark.

## Deliberately unavailable

Audit cache records remain measurements with `actionable=false`. Separate version-aware uv/Go plan adapters and a digest/TTL-bound executor are available through `cleanup_actions.py`; this is the only bundled mutation code path. The agent may also use a reviewed owner-command fallback for narrow rebuildable targets when it verifies the installed CLI help, exact scope, owner activity, dry-run/status evidence and separate approval. npm/pnpm/Homebrew remain review-only in the adapter API, but their installed owner CLIs may qualify for this fallback. Cloud metadata is implemented and invoked automatically on root discovery, but provider-specific sync verification and cloud eviction remain unavailable. Do not trust old actionable=true records or bypass a blocked probe with broad searches or raw deletion.

Generic cached model directories, database files, Docker volumes and local snapshots are not low-risk caches, regardless of their name or apparent rebuildability. No measured collector record authorizes raw recursive deletion of these targets.
