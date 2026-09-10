---
name: macos-cleanup
description: >-
  Audit and clean macOS storage safely and portably. Use whenever the user asks to clean a Mac, free disk space, find what uses storage, analyze the filesystem, remove junk or caches, inspect local Time Machine/APFS snapshots, review offline cloud files in iCloud Drive, Google Drive, Dropbox, OneDrive, Yandex Disk, or another File Provider, audit models/VMs/SDKs/Docker/app data, or uninstall named applications. Always discover capabilities and paths on the current Mac instead of assuming a username, disk identifier, Homebrew prefix, cloud account, application, or tool is present.
compatibility: Requires macOS, Python 3.10+ and native Darwin dataless policy support. No Python dependencies are installed. Optional provider and owner tools are discovered at runtime.
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

For `cleanup`, include filesystem, cache, GUI-app, Homebrew/package, and cloud-offline audits. Start the cloud module in the same audit pass whenever any provider root is discovered; do not silently defer it. For `filesystem-audit`, do not force the expensive full removal audit unless the user asks what apps/packages to remove.

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

For broad `cleanup`, run `scripts/audit-all.sh` from the active project, using its resolved absolute skill path. It delegates to the Python orchestrator, validates JSON records, supervises guarded process groups, preserves valid partial results and writes private reports. Use `--modules <comma-separated-list>` for a fresh subset run; this is not a complete cleanup audit. Read `references/inventory-schema.md` before consuming records.

The default and maximum module timeout is 3600 seconds. `--timeout` or `MACOS_CLEANUP_MODULE_TIMEOUT` can explicitly select a lower limit. This is per module, not the total audit duration: for a default four-module run, set the calling tool's outer timeout to at least 14520000 milliseconds (four hours plus two minutes for overhead), rather than relying on its 120000-millisecond default. For subsets or explicit overrides, budget the module count times the selected timeout plus overhead. If the tool cannot wait that long, use its supported supervised background execution and poll the same run; do not launch duplicate audits merely because the foreground wait expired. Cloud traversal hard limits remain unchanged.

The existing shell modules are collectors, not safe standalone entry points. For a narrow filesystem collector, launch it through `python3 scripts/audit_runtime.py --guard-exec /bin/sh <collector> <arguments>` using resolved paths. No content searches, hashes, previews, or broad recursive `$HOME` scans are part of ordinary audit. Do not silently fall back to an unguarded collector if Python or the Darwin policy is unavailable.

Audit scripts do not invoke cleanup commands. Owner CLI discovery can have incidental metadata/cache effects; do not describe this as an OS-wide read-only sandbox. If a module fails or times out, retain valid records and mark the scope `PARTIAL`. Do not replace a blocked module with an untracked, unguarded scan. `proposed_action` strings in older records are advice, never executable input or approval.

## Implemented Scope

| Area | Current implementation | Remaining limitation |
|---|---|---|
| Runtime | Process-scoped native no-hydration guard; supervised command groups | Not a security sandbox for hostile processes or an OS-wide download switch |
| Orchestration | JSON validation, private fresh reports, module outcomes and coverage | Full cleanup includes categories not yet implemented |
| Capacity/snapshots | Target-volume discovery and snapshot inventory | Snapshot presence gives no reclaimable-byte estimate |
| Paths | Narrow allocated/apparent measurements | No automatic full-home content scan |
| Caches | Measurement plus guarded version/writer review; plan adapters for uv and Go | npm/pnpm/Homebrew are review-only; unsupported versions, symlinks and unknown activity block plans |
| Cloud | Root discovery automatically invokes bounded metadata inventory per root | Sync/pin/conflict/eviction remain unverified; no cloud mutation is implemented |
| Execution | Separate digest-bound uv/Go executor with TTL, fresh preflight and single-use journal | No generic deletion, automatic retry, or automatic reconciliation; audit records never authorize actions |
| Apps/packages/Docker/VM/SDK/models/duplicates | Requirements below describe the target workflow | Report missing coverage; do not claim these adapters already ran |

Unit/fixture tests are not a live File Provider integration test or an agent benchmark. State which verification actually ran.

## Reviewed Action CLI

Use `python3 <skill>/scripts/cleanup_actions.py review --adapter <owner>` for fresh owner evidence. For a supported idle uv/Go target, `prepare --adapter <owner> --output <new-private-plan>` captures identity, scope, risk and a digest; `show --plan <plan>` displays the plan without executing it. These commands do not grant approval.

Only after the user selects that exact plan through `question`, call `execute --plan <plan> --confirm-digest <displayed-digest> --journal-dir <private-journal>`. A newly prepared or changed plan requires a new choice. The executor rechecks expiry immediately before dispatch and blocks replay of a previously attempted digest, including interrupted attempts. A digest is a workflow binding, not proof that a human agreed; enforce the selection step.

Review `references/cleanup-action-workflow.md` and `references/cache-adapter-sources.md` before use. The initial adapters have explicit version limits. uv requires an explicitly supported link mode; do not set environment variables merely to bypass a blocked review. Go clears inherited flags and external cache-program settings before its fixed build-cache-only command. Historical link relationships or shared environments cannot be inferred solely from the current environment.

## No-Hydration Policy

The runtime sets and verifies Darwin `IOPOL_MATERIALIZE_DATALESS_FILES_OFF` before launching collectors. The process-scoped setting prevents those processes from triggering dataless content downloads; it does not cancel existing transfers or affect unrelated applications. Failure to establish the policy blocks the scan.

Metadata enumeration and payload reads are different operations. Apple's `du` source includes its own dataless safeguard; do not claim that `du` inherently downloads cloud files. Directory enumeration may require remote metadata, while content search such as `rg`, hashing and previews can request payload. An inaccessible dataless item is incomplete coverage, not permission to retry without protection. See `references/safety-runtime.md` for boundaries and tests.

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
2. Record allocated and apparent size separately when available through guarded metadata inspection. Keep `estimated_reclaimable_bytes` unknown unless an owner-aware estimate exists. Allocated size is not exclusive APFS allocation or guaranteed recovery.
3. Never sum a parent and its child, shared APFS capacity, Docker shared layers, clones, sparse files, or cloud placeholders.
4. Treat sizes as estimates, not promised recovery. Keep `observed_available_delta_bytes` separate from owner-reported reclaim. It includes concurrent activity. Execute destructive groups sequentially to preserve per-group checkpoints; never sum overlapping/shared allocations.
5. If available capacity changes by at least 1 GiB or 2% of the preflight available space without a workflow action, record concurrent activity. During read-only audit, continue and flag it. Immediately before a destructive or hard-to-reverse action, pause that target once, re-preflight, and require a new decision for that target if material drift persists. Continue processing unrelated targets.
6. For snapshot questions, use `tmutil listlocalsnapshots <mount-point>` and `diskutil apfs listSnapshots <resolved-device-or-volume>`. Snapshots are system-managed recovery assets, not routine junk. Consider deletion only as a separately approved advanced action under actual space pressure, without promising a retained size.

## Filesystem Audit

Build a high-level topology before drilling down. Discover the target volume and home location without recursively traversing the entire home. Start with narrow local roots and expand only material categories under the guard. Do not assume `--target` for capacity authorizes content inspection at that path.

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

Provider discovery automatically invokes `cloud_metadata_collect.py` for each root under the guard: up to 1000 entries, depth 3 and a 10-second cooperative traversal budget per root, within the outer module timeout. Dataless directories are skipped rather than hydrated. The explicit `cloud-metadata --root <root>` CLI supports bounded follow-up, with hard limits of 10000 entries, depth 64 and 60 seconds. A single blocking kernel metadata call may outlast a cooperative budget; the outer audit runtime provides process-level timeout/cancellation.

Public item IDs use per-run HMAC, not predictable filename hashes; do not treat them as stable cross-run identities or executable paths. Missing measurements remain unknown, with known lower bounds separately labelled. Complete enumeration is not complete cloud verification: sync, pins, conflicts and remote-preserving eviction still need provider-specific evidence. Do not ask the user to request an already authorized safe metadata audit again.

For every discovered provider in `cloud-offline-audit` and `cleanup`:

1. Enumerate item-level offline copies instead of stopping at provider-root totals. Record the provider/domain, exact target, allocated bytes, logical bytes, sync state, local state, and evidence source.
2. Research the provider-specific eviction mechanism available on the current Mac. Prefer documented Finder/provider Remove Download or online-only semantics with verified API access. File Provider extension APIs are not universal administrative access to every domain. `brctl evict` is private/version-dependent; missing help does not prove absence, but neither is it a stable public contract. Do not use a real mutation merely as a capability probe.
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
2. Use the specifically reviewed and approved eviction method. A private `brctl` command requires explicit version/capability review; Foundation or Finder also requires verified scope and sync evidence.
3. If an operation fails or times out, reconcile partial/asynchronous state before trying another method. Do not stack fallback actions or restart `bird` as routine cleanup. Stop a known content-reading source first when handling accidental hydration; the no-hydration guard does not cancel an existing transfer.
4. Verify allocated bytes fall, logical bytes and namespace remain, and provider metadata still reports the item uploaded and not trashed. Folder-level `isDownloaded=1` alone does not prove descendants remain materialized.

If item-level status or supported eviction semantics cannot be verified, classify the target `AMBIGUOUS`, include it in the report, and provide exact provider UI steps or the narrow next audit needed. Do not silently omit it or guess a filesystem deletion command.

The cloud module is `PARTIAL` when a cloud root is discovered but item-level offline candidates were not enumerated, their sync/local state was not investigated, or a remote-preserving eviction mechanism was not researched.

## Cleanup Workflow

Follow this order: audit all in-scope areas, present the consolidated results, ask which exact targets to clean, then handle each selected target's preflight and execution. Active Go/editor processes or another execution blocker do not prevent storage measurement or the remaining audit. Record known blockers in the results without asking the user to close applications or resolve them during the audit.

Do not substitute the cache adapter reviews for the full audit. Finish all available guarded checks across the required categories, including supplementary owner inventories for material areas the bundled runner does not implement. Explicitly report any remaining coverage gaps; unsupported cleanup automation does not mean an area has been audited or has no candidates.

1. Discover capabilities and establish capacity checkpoints.
2. Run the unified audit and inspect its `audit_summary`, every `orchestrator_module` record, and the generated report before proposing any action. Build additional owner-specific inventories only for material areas not yet covered.
3. Classify each target as `PROTECTED/IN-USE`, `REVIEW-CANDIDATE`, `AMBIGUOUS`, `STALE-ARTIFACT`, `SAFE-GARBAGE`, `APPROVAL-REQUIRED`, `REVIEW-ONLY`, or `EXCLUDED`.
4. If a cloud root is discovered, complete the item-level cloud audit before presenting cleanup choices. If provider tooling cannot enumerate exact offline candidates, mark the cloud module `PARTIAL` and state that limitation explicitly; do not present the cloud root as an actionable cleanup target.
5. Extend the generated report with ownership, activity/lock evidence, installed-version action scope, recovery cost, risk and verification. Collector records remain REVIEW-ONLY. Do not flip `actionable`, copy a command from old JSON, or bypass a blocked adapter with raw deletion.
6. Present concise findings in chat before calling `question`. The findings must say which cloud providers were found, whether item-level verification completed, and whether cloud actions are included or excluded.
7. If cloud verification is incomplete, finish all available guarded checks and name the exact unresolved requirement. Ask only for a concrete missing permission, account decision or scope choice; do not ask whether to perform an already requested safe audit. Keep unverified cloud data untouched while reporting other categories.
8. Use separate confirmation groups for caches, cloud-local eviction, personal files, models, apps, app data, Docker, VMs/SDKs, packages, snapshots, and administrator handoffs.
9. Each option must map to one concrete owner or exact target set. Include every path and material risk. Never use a broad option such as `delete all personal data`, `all app data`, `all models`, or `all volumes`.
   Selecting a review-only target requests preparation, not permission to delete it. After target selection, prepare and show a separate plan for each supported cache owner and obtain exact digest-bound approval before execution. Disclose known limitations in the selection options; never label a blocked or unsupported target ready to execute.
10. Execute only independently verified plans selected through `question`, not collector suggestions. Re-run exact target identity/scope, owner activity and capacity preflight immediately before every selected group. Execute groups sequentially and independently: a blocked, unavailable, declined, or failed target must not prevent review, approval, or execution of unrelated targets. A question instead of a selection is not approval; changed targets or continued material drift require a new decision.
11. Verify owner/service health, exact target state, Trash destination where applicable, and `df -k` after each group.
12. Preserve executor outcomes `BLOCKED`, `PARTIAL`, `EXECUTED` and `UNCHANGED` per target with their reasons; do not collapse independent targets into one fail-fast result or turn an interrupted action into success. Other owner workflows may report `MOVED-TO-TRASH`, `BLOCKED-PRIVILEGE`, `BLOCKED-IN-USE`, `SKIPPED` or `NOT-FOUND`. If private raw output could not be removed, report retention and required reconciliation without displaying it.

For several independent low-risk caches, a multi-select question may include an `All listed low-risk caches` option and `None`. Expand the selection into separate plans and outcomes; if one cache is blocked, continue with every other approved cache. It must not include personal data, models, apps, Docker volumes, cloud deletion, snapshots, VMs, databases, or SDK state.

### Selected-target failures

Only after the user selects a target, if its preparation, preflight or cleanup is blocked, explain that target's reason and offer `Retry this target` or `Skip this target and continue`. Include the exact owner/path in the question. Never replace this with `Close your IDE or stop the cleanup`, `Continue the audit?`, or another all-or-nothing choice. Skipping declines only that target; unrelated checks and selected actions continue. Offer retry only when there is a concrete resolvable blocker; unsupported actions remain review-only, not a reason to repeat the same failed check.

A retry requests a fresh check, not forced deletion or approval of a replacement plan. Reconcile any partial execution first; a new or changed plan requires fresh exact approval. Keep unselected targets untouched and do not ask the user to resolve their blockers. Report all per-target outcomes even when none could be executed; do not describe this as user cancellation unless the user explicitly cancelled the whole process.

## Owner-Aware Procedures

- Prefer version-supported selective pruning and warm-cache budgets over full reset. Check owner locks, action scope and regeneration cost; do not bypass busy locks. State whether recovery is local regeneration, public download, private registry, or unverified.
- Check owner processes plus known writers/dependents (for example gopls/editors for Go). Missing visibility means unknown, not idle. Do not expose full command-line arguments. A cache's measurable size does not prove owner inactivity.
- Preserve cache roots and metadata when directly clearing exact cache contents. Enumerate dotfiles safely and do not follow symlinks or cross mount points.
- Use Docker commands for Docker resources. Never delete Docker Desktop's container directory directly. Keep active containers, compose stacks, databases, and volumes separate.
- Use Xcode, `simctl`, Android Studio/`avdmanager`, UTM, or another owning tool for SDK/device/VM state. Never delete raw simulator or VM directories as generic cleanup.
- Use model-manager or repository-aware commands for model data. Inventory exact models and shared blobs before offering deletion; disclose shared-layer and redownload effects.
- Move ordinary local files to a unique `~/.Trash` destination when practical. A Trash move is reversible relocation, not reclaimed capacity until Trash is emptied through a separately approved action.
- Never request or collect an administrator password. For an approved privileged action, provide one exact narrowly scoped administrator command and describe prerequisites and untouched data.

## Application and Package Audit

For broad `cleanup`, audit GUI apps and package managers. For named `uninstall`, restrict scope to the named apps.

For GUI apps, collect bundle path, allocated size, install/update evidence, Spotlight last-used metadata, exact running processes, cask ownership, helpers, launch jobs, extensions, and app-data candidates. Null Spotlight metadata is missing evidence, not proof of non-use.

For Homebrew, set `HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_NO_AUTOREMOVE=1` to separate incidental maintenance. Verify installed-version semantics and use the same configuration for preview and action. Treat explicit autoremove as a separate package decision. Prefer bulk installed JSON, guarded Cellar/Caskroom measurements, leaves, dependency graphs, pins and services over slow per-formula calls.

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

Write each run in a fresh private directory under `macos-cleanup/` relative to the active project, with a unique timestamp/suffix, `report.md`, `inventory.jsonl` and validated per-module output. Never reuse or overwrite an existing output directory. Do not copy run-specific facts into this skill directory.

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

Stop only the affected target before action when its approval is missing; the exact target changed; the owner became active; cloud sync is incomplete; it may contain unsynced data, credentials, active databases, shared model blobs, VM state, or service-linked volumes; its required tooling is unavailable; or its material capacity drift remains after one fresh preflight. Record the outcome and continue with unrelated targets.

Only explicit cancellation or loss of the no-hydration guard stops the overall cleanup process. Do not turn uncertainty into deletion. Keep the affected candidate in the report, explain the missing evidence, offer the narrow next audit or supported UI/tool needed to resolve it, and continue independent checks and actions.
