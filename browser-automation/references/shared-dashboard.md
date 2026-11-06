# One dashboard for multiple projects

## What is shared

For a single trusted operator using one HTTPS IP endpoint, a shared dashboard can discover task controllers in one private socket directory. Chrome processes and `.chrome-user-data/` directories remain separate per canonical repository. Task names include project identity, and project state/human/authentication leases remain separate.

This is not multi-user or per-project authorization. The operator receives full control of the sessions discovered in the shared runtime. Browser-profile separation does not isolate remote server-side account mutations if projects use the same external account.

## Enrollment is not migration

The owner-only policy is stored outside Git at:

```text
~/Library/Application Support/browser-automation/shared-runtime.json
```

It selects one private short socket directory and explicitly enrolled project keys. Projects not enrolled retain their isolated runtime. Existing tasks without a recorded `socketDir` retain their legacy project directory after enrollment. New tasks record their chosen directory before their first controller interaction.

```bash
python3 <skill-dir>/scripts/shared_runtime.py plan --socket-dir <approved-shared-root> --project <root-a> --project <root-b>
python3 <skill-dir>/scripts/shared_runtime.py configure --socket-dir <approved-shared-root> --project <root-a> --project <root-b> --allow-write
```

The second command requires explicit host approval. It merges membership for the same root; it does not move controllers, clear leases, change browser profiles, or restart Chrome. Changing an established shared root is not an automatic operation.

The socket root must be an owned private direct child of `~/.ab` and fit macOS's Unix-socket path budget. An already-running dashboard's approved directory may be reused. Do not copy socket, PID, stream, or target-binding sidecars manually. Normal runtime enrollment accepts an owned, non-writable-by-others `~/.ab` parent at `0755`, but the guarded migration's stricter preflight requires that metadata base to be `0700` as well. The selected socket root must remain `0700`. Legacy native PID/socket files can also have broader default modes; inspect ownership/type first and obtain explicit approval before tightening only the necessary owned metadata nodes to `0600`. Do not relax the migration validator, change profile permissions, or silently chmod unrelated paths.

## Guarded migration of an existing controller

A controller migration is a special lifecycle operation, not a normal page command. It must first pass installed-tool source review and a disposable real-browser qualification. Qualification is evidence, not proof of human consent or a security boundary against a malicious local user.

After reviewing its source and obtaining explicit fixture-launch approval, run:

```bash
python3 <skill-dir>/scripts/qualify_shared_runtime.py --allow-browser-launch --chrome "<verified-project-browser-executable>"
```

Select the exact executable used by the existing project, not the default installed app. The selector affects only a new disposable profile; it does not upgrade or restart the real browser. Qualification records its actual Chrome version, which must match the live project's version before migration. The fixture uses a mock keychain/basic password store for synthetic data only; never apply that storage-backend change to an existing real profile.

This uses a short private temporary HOME because of the macOS Unix-socket path limit, a disposable Git project/profile, one headless Chrome, and a synthetic loopback HTTP page. It checks form/JavaScript state, target/process identity, lease preservation, and fixture cleanup. It does not access a real project profile or deploy the dashboard/proxy. Only a completely successful run may write an owner-readable qualification receipt outside Git in the original HOME. The migration gate checks the installed executable's path, hash, and version against that receipt; metadata probes must use an explicit empty config and isolated HOME/cwd. Qualification does not imply remote-network or Brave validation.

Before migrating:

1. Ask the human to stop typing and reach an explicitly safe checkpoint. Do not infer this from inactivity.
2. Stop captures and pause every active controller in the affected project. Reject unresolved/timed-out operations; a terminated CLI client does not prove its daemon operation completed.
3. Verify exact project browser PID/start identity, CDP endpoint, current task target, and source controller directory.
4. Ensure the destination has no existing sidecars for this exact unique task session. A collision is a blocker, not permission to replace it.
5. Preserve human/paused status, `auth_task`, and account verification requirements. Migration never resumes human-held work.

Plan first:

```bash
python3 <skill-dir>/scripts/migrate_controller.py --project <root> --task <task-id> plan
```

Only after qualification and explicit checkpoint/operation approval:

```bash
python3 <skill-dir>/scripts/migrate_controller.py --project <root> --task <task-id> migrate --allow-migrate --human-paused --captures-stopped
```

The transaction journals its intent, disconnects only the externally attached source controller, and verifies Chrome and all pre-existing page targets survive. A new pinned controller intentionally creates one fresh blank page; it selects the verified original target, closes only that temporary blank, verifies ownership again, records the new transport, and removes the migration journal. It neither copies a profile nor sends a Chrome-wide close.

A failure retains an inspectable in-progress/uncertain journal and the existing human/authentication lease. Ordinary commands against that task stay blocked. Do not blindly retry, restore an old state file over uncertain browser effects, delete singleton locks, adopt another tab, or clear a lease on timeout. Report the journal for explicit recovery.

## IP-only remote setup

Use `mode: "shared-ip"` with `scripts/remote_access.py`. Copy [the shared-IP example](../assets/shared-ip-manifest.example.json) to a private location and replace all placeholders.

- `hubOrigin` names the direct dashboard endpoint in this mode; there is no hub route or DNS requirement.
- Its literal private IP must match `bindAddress`.
- Every listed project uses that same origin and loopback dashboard port.
- Every listed project's configured default must resolve to the same approved controller directory.
- Caddy emits one IP site and one reverse proxy. Only one dashboard LaunchAgent and one `bootstrap/shared.txt` are generated.
- The bootstrap token grants full instance access and is shared by the projects. Separate IP ports would not isolate its cookie, so multiple independent dashboards on one IP are not this mode.

The older hostname mode remains available for isolated project dashboards. Do not silently mix the two modes or move controller roots during configuration rendering.

## Deployment and human handoff

Follow [remote-access.md](remote-access.md) for private binding, explicit peers, existing Caddy ownership, TLS trust, secret link delivery, and separate activation approvals. Reusing a dashboard does not authorize changing its origins/port, stopping it, or rotating its token without coordination.

A shared dashboard does not override the task's human lease. The agent stops issuing page commands during manual input. Independent project profiles and project-level leases remain intact, including the existing site-scoped concurrency opt-in within a project.

After migration and proxy activation, verify the expected project/task session appears remotely and keep the task paused until the human explicitly finishes. No screenshots or credential readback are part of this verification.
