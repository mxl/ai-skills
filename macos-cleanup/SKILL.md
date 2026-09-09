---
name: macos-cleanup
description: >-
  Audit and clean macOS storage safely and portably. Use whenever the user asks to clean a Mac, free disk space, find what uses storage, analyze the filesystem, remove junk or caches, inspect local Time Machine/APFS snapshots, review offline cloud files in iCloud Drive, Google Drive, Dropbox, OneDrive, Yandex Disk, or another File Provider, audit models/VMs/SDKs/Docker/app data, or uninstall named applications. Always discover capabilities and paths on the current Mac instead of assuming a username, disk identifier, Homebrew prefix, cloud account, application, or tool is present.
compatibility: Requires macOS shell tools. Optional provider, package-manager, Docker, Xcode, Android, VM, and model-management tools are discovered at runtime.
---

# macOS Cleanup

Treat cleanup as a storage-audit problem first. Discover what exists, measure it without exposing private content, classify possible actions, request exact approval, execute through the owning tool when possible, and verify the real capacity change.

## Modes

Choose the narrowest mode that satisfies the request. A combined request may run more than one mode, but keep their findings and approvals separate.

| Mode | Trigger examples | Required result |
|---|---|---|
| `filesystem-audit` | what uses disk, analyze filesystem, large files | Read-only storage topology and prioritized ideas; no confirmation question unless the user also asks to clean |
| `cloud-offline-audit` | offline cloud files, iCloud local copies, Google Drive disk use | Provider-aware local-footprint analysis; distinguish local eviction from cloud deletion |
| `cleanup` | clean my Mac, free space, remove junk | Full audit, dry run, exact confirmation groups, execution, verification, report |
| `uninstall` | `uninstall <app-name-or-path> ...` | Target only the named apps; do not expand to unrelated cleanup |

For `cleanup`, include filesystem, cache, GUI-app, and Homebrew/package audits. For `filesystem-audit`, do not force the expensive full removal audit unless the user asks what apps/packages to remove.

## Portability Contract

- Resolve the user home from `$HOME` and the current user from `id -un`.
- Select the measured filesystem from the target path. Prefer `/System/Volumes/Data` only when it exists and contains the target; otherwise measure the target's own mounted volume.
- Resolve APFS volume/device identifiers with `diskutil info`; never assume a fixed device identifier.
- Resolve Homebrew with `command -v brew`, `brew --prefix`, and `brew --cache`; never assume `/opt/homebrew` or `/usr/local`.
- Resolve package caches from tool configuration, such as `npm config get cache`, `pnpm store path`, `go env GOCACHE`, and `uv cache dir`.
- Discover cloud domains under `~/Library/CloudStorage`, iCloud containers under `~/Library/Mobile Documents`, and existing legacy provider roots. Do not assume any provider or account exists.
- Discover applications, VMs, models, SDKs, devices, containers, services, and volumes from current inventories. Never embed names, sizes, dates, PIDs, UUIDs, accounts, projects, or findings from another Mac.
- Treat missing tools, Full Disk Access, or metadata as `UNAVAILABLE`, `PERMISSION-DENIED`, or `PARTIAL`, never as an empty inventory.
- Keep examples hypothetical. Machine-specific facts belong only in a run report or test fixture.

Before finishing a skill change, search production files for absolute user-home prefixes, fixed disk identifiers, email addresses, UUIDs, PIDs, and copied run measurements.

## Capability Discovery

Run `scripts/audit-capabilities.sh` first when available. It emits JSONL records for capacity, APFS/Time Machine snapshots, and optional tool availability. Use `scripts/audit-paths.sh` for exact high-level paths and `scripts/audit-cloud.sh` for discovered cloud roots. Read `references/inventory-schema.md` before merging their records.

Scripts are read-only. A script result is evidence, not deletion approval. If a script fails or times out, retain completed records, mark the module `PARTIAL`, and state what was not measured.

## Action Matrix

Include every material storage class in analysis. Whether it may be proposed or executed depends on ownership, recoverability, sync state, and activity.

| Class | Analyze | Propose | Execute after approval |
|---|---|---|---|
| Rebuildable cache | Yes | Exact owner/path | Yes, through owner command or exact cache contents |
| Cloud offline copy | Yes | Exact provider/folder | Only provider-aware `Remove Download` or online-only action after sync verification |
| Personal file/media | Aggregate first | Exact item or reviewed set | Move to a unique Trash destination; cloud-synced deletion is a separate remote-delete decision |
| Application bundle | Yes | One exact app | Yes through cask/vendor uninstaller or exact bundle-to-Trash workflow |
| Application data | Yes | One exact owner/path set | Separate approval from app bundle; use app-aware cleanup when available |
| VM/emulator/simulator | Yes | One named VM/device/runtime | Yes through owning tool; disclose loss of guest/device-local state |
| Docker image/build cache | Yes | Separate Docker groups | Yes after active container/compose preflight |
| Docker volume/database | Yes | One exact volume/database | Dedicated high-risk approval; never include in generic Docker cleanup |
| Model/AI data | Yes | One exact model/repository/version | Dedicated model-aware workflow after explicit approval; never generic recursive deletion |
| Time Machine local snapshot | Yes | One exact purgeable snapshot | Dedicated system-level approval using supported `tmutil`; never promise its logical size |
| System/APFS sealed data | Yes | Supported maintenance only | Never raw-delete; use documented OS tooling or administrator handoff |
| Credentials/secrets | Count and aggregate | Exact path only on explicit request | Dedicated confirmation; never expose contents or mix with temporary-file cleanup |

An analysis without material candidates is incomplete. A proposal is not approval. An exact approval does not authorize sibling paths or related data.

## Privacy and Redaction

- Default reports to aggregate categories, counts, sizes, owner, age bucket, and risk.
- Do not print personal filenames from Photos, Documents, Downloads, Mail, Messages, cloud trees, medical records, or private projects unless the user asks for exact names.
- Detect credential-like names and sensitive extensions, but report them as counts and total size. Never print file contents, tokens, account identifiers, raw cloud status tokens, shell arguments, or secrets.
- Redact email-like cloud account identifiers in chat and reports unless account disambiguation is required. Prefer stable labels such as `Google Drive account 1`.
- Do not persist raw shell history. With permission, parse timestamped history in memory, inspect only the first executable token, and retain only the mapped tool and timestamp.
- Do not recursively inspect file contents to decide cleanup. Use paths, metadata, ownership, sync evidence, and tool inventories.

## Measurement Rules

1. Record `df -h` for display and `df -k` for exact capacity checkpoints on the target volume.
2. Record allocated size and, for cloud/file-provider trees, apparent size. On macOS, `du -sh` estimates allocated blocks and `du -A -sh` estimates apparent size.
3. Never sum a parent and its child, shared APFS capacity, Docker shared layers, clones, sparse files, or cloud placeholders.
4. Treat sizes as estimates, not promised recovery. `df -k` is the observed net capacity change during an action interval.
5. If available capacity changes by at least 1 GiB or 2% of the preflight available space without a workflow action, record concurrent activity. During read-only audit, continue and flag it. Immediately before a destructive or hard-to-reverse action, pause once, re-preflight, and require a new decision if material drift persists.
6. For snapshot questions, use `tmutil listlocalsnapshots <mount-point>` for Time Machine snapshots and `diskutil apfs listSnapshots <resolved-device-or-volume>` for the exact APFS volume. Snapshot presence does not prove retained size.

## Filesystem Audit

Build a high-level topology before drilling down. Start with the target volume, `$HOME`, `/Applications`, and discovered high-level children. Then inspect only material roots or roots the user requested.

Cover these classes when present:

- personal media and documents;
- cloud-local and placeholder data;
- projects and source repositories;
- models and AI runtimes;
- application support, containers, and group containers;
- Docker, VMs, emulators, simulators, device support, and SDKs;
- package managers, language toolchains, browser runtimes, and caches;
- Trash, temporary areas, downloads, installers, archives, logs, and crash reports;
- databases and sparse disk images;
- local Time Machine and APFS snapshots.

For large databases, inspect owner activity and engine-specific reclaimability before recommending compaction. For SQLite, compare page count, page size, freelist pages, WAL state, and open holders. A large database with zero freelist pages will not materially shrink through `VACUUM` alone.

Do not use access time as proof of use. Prefer owner metadata, current processes, application inventories, project references, and explicit user confirmation.

## Cloud Offline Audit

Discover providers dynamically. Measure each domain separately and record both allocated and apparent bytes. A large apparent/allocated gap suggests placeholders but does not prove each item's state.

For every discovered provider in `cloud-offline-audit` and `cleanup`:

1. Enumerate item-level offline copies instead of stopping at provider-root totals. Record the provider/domain, exact target, allocated bytes, logical bytes, sync state, local state, and evidence source.
2. Research the provider-specific eviction mechanism available on the current Mac. For iCloud Drive, prefer an exact `brctl evict "<path>"` call after approval and preflight, even when `brctl help` does not list `evict` or `brctl evict --help` exits with a usage error; those observations do not prove the subcommand is unavailable. If the real exact-target call is unavailable or fails without changing state, fall back to Foundation `FileManager.evictUbiquitousItem(at:)`, then Finder `Remove Download`. For other providers, prefer a supported `Free up space` or `Make online-only` CLI, API, documented client action, or Finder action.
3. Verify that the action removes only the local copy while preserving the object in the synchronized namespace. Never substitute `rm`, Trash, or namespace deletion for local eviction.
4. Aggregate candidates by provider and reviewed folder without double-counting parent and child targets. Show the candidate list even when eviction cannot yet be automated.
5. Offer verified targets in a separate `cloud-local eviction` confirmation group. Each option must identify one provider and exact target set, estimated allocated benefit, offline-access loss, recovery method, and verification plan.
6. After an approved eviction, verify that the remote object remains present, the local item becomes a placeholder or online-only object, and allocated local bytes decrease. Reconcile failures or unchanged targets explicitly.

Before proposing local eviction for a folder or file:

1. Identify the provider/domain and exact target.
2. Verify there are no upload or sync errors using provider/Finder metadata or supported provider tooling.
3. Confirm the operation is `Remove Download`, `Free up space`, or `Make online-only`, not deletion from the synchronized namespace.
4. Estimate local benefit from allocated blocks, not logical size.
5. Explain offline-access loss and remote recovery requirements.

For an approved iCloud eviction:

1. Re-check exact-target allocated and logical sizes, `isUploaded`, `isUploading`, `isDownloading`, conflicts, keep-downloaded state, and filesystem capacity immediately before action.
2. Run `brctl evict "<exact-path>"` first. Do not use `--help` probing as the availability test because this private subcommand may be omitted from help and reject help flags while still accepting a real path.
3. If `brctl evict` fails, verify that allocated size and provider state are unchanged before attempting Foundation `FileManager.evictUbiquitousItem(at:)`. Do not stack fallback actions when the first action may still be asynchronous.
4. Verify allocated bytes fall, logical bytes and namespace remain, and provider metadata still reports the item uploaded and not trashed. Folder-level `isDownloaded=1` alone does not prove descendants remain materialized.

If item-level status or supported eviction semantics cannot be verified, classify the target `AMBIGUOUS`, include it in the report, and provide exact provider UI steps or the narrow next audit needed. Do not silently omit it or guess a filesystem deletion command.

The cloud module is `PARTIAL` when a cloud root is discovered but item-level offline candidates were not enumerated, their sync/local state was not investigated, or a remote-preserving eviction mechanism was not researched.

## Cleanup Workflow

1. Discover capabilities and establish capacity checkpoints.
2. Build the filesystem/storage topology and complete the cache, GUI-app, and Homebrew/package audits.
3. Classify each target as `PROTECTED/IN-USE`, `REVIEW-CANDIDATE`, `AMBIGUOUS`, `STALE-ARTIFACT`, `SAFE-GARBAGE`, `APPROVAL-REQUIRED`, `REVIEW-ONLY`, or `EXCLUDED`.
4. Write a dry-run report containing exact path/owner, allocated and logical size where relevant, evidence, classification, action, recovery path, risk, confidence, and verification.
5. Present concise findings in chat before calling `question`.
6. Use separate confirmation groups for caches, cloud-local eviction, personal files, models, apps, app data, Docker, VMs/SDKs, packages, snapshots, and administrator handoffs.
7. Each option must map to one concrete owner or exact target set. Include every path and material risk. Never use a broad option such as `delete all personal data`, `all app data`, `all models`, or `all volumes`.
8. Execute only options selected through `question`. Re-run exact target and capacity preflight immediately before every selected group.
9. Verify owner/service health, exact target state, Trash destination where applicable, and `df -k` after each group.
10. Reconcile every selected target as `EXECUTED`, `MOVED-TO-TRASH`, `BLOCKED-PRIVILEGE`, `BLOCKED-IN-USE`, `SKIPPED`, `NOT-FOUND`, or `UNCHANGED`.

For several independent low-risk caches, a multi-select question may include an `All listed low-risk caches` option and `None`. It must not include personal data, models, apps, Docker volumes, cloud deletion, snapshots, VMs, databases, or SDK state.

## Owner-Aware Procedures

- Prefer package-manager cleanup commands over direct deletion. State whether recovery is local regeneration, public download, private registry, or unverified.
- Verify exact owner processes with `pgrep -x` or exact executable paths. Never use full-command-line substring matching for closure checks.
- Preserve cache roots and metadata when directly clearing exact cache contents. Enumerate dotfiles safely and do not follow symlinks or cross mount points.
- Use Docker commands for Docker resources. Never delete Docker Desktop's container directory directly. Keep active containers, compose stacks, databases, and volumes separate.
- Use Xcode, `simctl`, Android Studio/`avdmanager`, UTM, or another owning tool for SDK/device/VM state. Never delete raw simulator or VM directories as generic cleanup.
- Use model-manager or repository-aware commands for model data. Inventory exact models and shared blobs before offering deletion; disclose shared-layer and redownload effects.
- Move ordinary local files to a unique `~/.Trash` destination when practical. A Trash move is reversible relocation, not reclaimed capacity until Trash is emptied through a separately approved action.
- Never request or collect an administrator password. For an approved privileged action, provide one exact narrowly scoped administrator command and describe prerequisites and untouched data.

## Application and Package Audit

For broad `cleanup`, audit GUI apps and package managers. For named `uninstall`, restrict scope to the named apps.

For GUI apps, collect bundle path, allocated size, install/update evidence, Spotlight last-used metadata, exact running processes, cask ownership, helpers, launch jobs, extensions, and app-data candidates. Null Spotlight metadata is missing evidence, not proof of non-use.

For Homebrew, prefix audit commands with `HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1`. Prefer one bulk installed JSON query, direct Cellar/Caskroom size passes, `brew leaves --installed-on-request`, dependency/use graphs, pins, services JSON, and `brew autoremove --dry-run`. Avoid slow per-formula calls and zsh special variable names such as `path` and `status`.

Separate install age from usage evidence. Protect dependencies, active dependents, pinned packages, running services, active project toolchains, OCR/model tooling, databases, and provider clients. Missing history produces `AMBIGUOUS` or `REVIEW-CANDIDATE`, never `unused`.

## Named Application Uninstall

Recognize `uninstall <app-name-or-exact-path> ...`. Resolve each target to exactly one bundle in `/Applications`, `~/Applications`, or an explicit path. If resolution is missing or ambiguous, ask for clarification.

1. Audit only named bundles, cask/vendor ownership, processes, helpers, services, extensions, and associated data.
2. Present one bundle option per app. Homebrew cask uninstall may run app-specific scripts; disclose recorded uninstall effects. Never use `--zap --force`.
3. After bundle selection, ask a separate multi-select question for eligible app data, one option per app plus `None`.
4. Keep synced, credential, browser-profile, database, VM, project, personal, and model data in dedicated decisions rather than generic app-data approval.
5. Use vendor uninstallers when needed for helpers or extensions. Otherwise move only the exact user-owned app bundle to Trash after exact closure verification.
6. Approval for one app never authorizes another app, helper, package, cache, or data path.

## Reports

Write `macos-cleanup/macos-cleanup-YYYY-MM-DD-HHMM.md` relative to the active project. Do not copy run-specific facts into this skill directory.

Include:

- mode, scope, target volume, tool capabilities, and module completion status;
- capacity checkpoints and unexplained drift;
- size-ordered candidates without double-counting parents/children;
- cloud allocated/apparent sizes and verified sync/local-state evidence;
- separate app and package tables when required;
- redacted sensitive findings;
- approved and executed groups;
- per-target before/action/after measurements and observed capacity delta;
- protected, ambiguous, unavailable, and partial areas;
- verification and final reconciliation.

Use the common inventory fields in `references/inventory-schema.md` so reports can be generated from JSONL records.

## Stop Conditions

Stop before action when approval is missing; the exact target changed; the owner became active; cloud sync is incomplete; a target may contain unsynced data, credentials, active databases, shared model blobs, VM state, or service-linked volumes; required tooling is unavailable; or material capacity drift remains after one fresh preflight.

Do not turn uncertainty into deletion. Keep the candidate in the report, explain the missing evidence, and offer the narrow next audit or supported UI/tool needed to resolve it.
