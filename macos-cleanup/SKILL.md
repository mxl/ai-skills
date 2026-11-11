---
name: macos-cleanup
description: >-
  Audit and clean macOS storage. Use for disk-usage analysis, freeing space or removing caches, offline cloud copies, Time Machine/APFS snapshots, app/package/model/VM/SDK/Docker storage, and named application uninstalls.
compatibility: Requires macOS, Python 3.10+ and native Darwin dataless policy support. No Python dependencies are installed. Optional provider and owner tools are discovered at runtime.
---

# macOS Cleanup

Audit first → present findings → select targets → approve exact actions → execute sequentially → verify. Audit evidence is never permission to delete.

## 1. Select scope and load the branch

Choose the narrowest mode. Combined requests may use several modes; keep their findings and approvals separate.

| Mode | Request | Required reading and result |
|---|---|---|
| `filesystem-audit` | Disk usage, large files, filesystem analysis | [Audit guide](references/audit-guide.md): read-only topology and prioritized candidates; no cleanup confirmation unless cleanup was requested. Full app/package removal review is optional unless requested. |
| `cloud-offline-audit` | Offline cloud copies or provider disk usage | [Audit guide](references/audit-guide.md) and [cloud workflow](references/cloud-offline.md): provider-aware local footprint, distinct from cloud deletion. |
| `cleanup` | Clean the Mac, free space, remove junk/caches | [Audit guide](references/audit-guide.md): filesystem, caches, GUI apps, packages and cloud-offline audit, followed by exact approvals and verification. Load the [cloud workflow](references/cloud-offline.md) in the same pass whenever a provider root is discovered. |
| `uninstall` | Named app or exact bundle path | [Uninstall workflow](references/uninstall.md) and the application/package section of the [audit guide](references/audit-guide.md): only named apps and their reviewed associated data. |

Whenever snapshots appear in an inventory, report or proposed action—including broad cleanup—or the user asks about their sizes, read [snapshots](references/snapshots.md). Before proposing any mutation, read the [action policy](references/action-policy.md); before cache or owner-CLI preparation/execution, also read [reviewed actions](references/cleanup-action-workflow.md) and [adapter sources](references/cache-adapter-sources.md). These references are required when their branch applies, not optional background.

## 2. Establish safe measurements

Discover home, user, target volume, devices, tool paths, configured caches and provider roots on the current Mac; use the audit guide's portability contract. Machine-specific facts belong in private run reports or test fixtures, not this skill.

### No hydration

Collectors must run under the verified Darwin `IOPOL_MATERIALIZE_DATALESS_FILES_OFF` policy. Failure to establish it blocks scanning; never replace a blocked collector with an unguarded scan. The policy is process-scoped: it neither cancels existing downloads nor controls unrelated applications. Owner CLI discovery may have incidental metadata/cache effects; this is not an OS-wide read-only sandbox.

Ordinary audit uses metadata and owner inventories, not content searches, hashes, previews or a broad recursive home scan. Directory enumeration can require remote metadata; Apple's `du` has its own dataless safeguard, so do not claim it inherently downloads payloads. Inaccessible dataless items mean incomplete coverage. Read [runtime safety](references/safety-runtime.md) when investigating guard behavior or its boundaries.

### Privacy

- Report aggregate categories, counts, sizes, owner, age bucket and risk. Show personal filenames from Photos, Documents, Downloads, Mail, Messages, cloud trees, medical records or private projects only when the user requests exact names.
- Report credential-like names and sensitive extensions as counts and total size. Keep contents, tokens, raw cloud status tokens, account identifiers, full process arguments and secrets out of output.
- Redact email-like provider accounts unless disambiguation requires them; prefer labels such as `Google Drive account 1`.
- Inspect shell history only with permission: parse timestamped history in memory, retain only mapped tool/timestamp from the first executable token, and persist no raw history.

### Capacity

1. Capture `df -h` for display and `df -k` for exact target-volume checkpoints.
2. Keep allocated and apparent sizes separate. Leave `estimated_reclaimable_bytes` unknown without an owner-aware estimate; allocated blocks are not exclusive APFS allocation or guaranteed recovery.
3. Avoid double-counting parents/children, shared APFS capacity, Docker layers, clones, sparse files and cloud placeholders. Keep `observed_available_delta_bytes` separate from owner-reported reclaim; concurrent activity affects it.
4. A capacity change without a workflow action of at least 1 GiB or 2% of preflight available space is material drift. During audit, record it and continue. Before destructive/hard-to-reverse action, pause only that target once and re-preflight; persistent drift requires a new target-specific decision.

## 3. Complete the audit before asking to clean

For broad cleanup, run the guarded unified audit as specified in the audit guide. Read [inventory schema](references/inventory-schema.md) before consuming records. Inspect `audit_summary`, every `orchestrator_module` record and the generated report. Collector records remain `REVIEW-ONLY`; neither changing `actionable` nor copying an old `proposed_action` authorizes execution.

Complete available guarded checks across every required material category, supplementing missing coverage with owner-specific inventories. Cache-adapter reviews are not a full audit. Unsupported automation, missing tools, Full Disk Access or missing metadata mean `UNAVAILABLE`, `PERMISSION-DENIED` or `PARTIAL`, not an empty inventory. Retain valid partial records.

Classify each target as `PROTECTED/IN-USE`, `REVIEW-CANDIDATE`, `AMBIGUOUS`, `STALE-ARTIFACT`, `SAFE-GARBAGE`, `APPROVAL-REQUIRED`, `REVIEW-ONLY` or `EXCLUDED`. Record ownership, activity/lock evidence, installed-version action scope, recovery cost, risk and verification needs. Active owners block execution of their targets, not measurement or the remaining audit; record blockers without asking the user to close applications during audit.

**Audit gate:** every required material category has findings or an explicit coverage gap. For every discovered cloud root, finish available item-level checks and research remote-preserving eviction before offering cleanup choices. If unavailable, mark cloud coverage `PARTIAL`, name the missing evidence and keep the root non-actionable. Ask only for a concrete missing permission, account decision or scope choice, not whether to repeat an already authorized safe audit.

Present consolidated findings in chat before `question`: size-ordered candidates, blockers and gaps; discovered cloud providers; whether item-level verification completed; and whether cloud actions are included or excluded. An unexplained absence of material candidates is not a completed analysis.

## 4. Select targets, then approve actions

Use separate confirmation groups for caches, cloud-local eviction, personal files, models, apps, app data, Docker, VMs/SDKs, packages, snapshots and administrator handoffs. Each option identifies one concrete owner or exact target set, every path and material risk. Keep personal names private until their disclosure is authorized. Broad categories such as all personal data, all models or all volumes are not exact target sets.

Selecting a review-only target authorizes preparation, not deletion. For each selected target, prepare a supported digest-bound plan or a dedicated owner workflow allowed by the action policy. For narrow rebuildable targets without a supported adapter, use the reviewed owner-command fallback. Show scope, exact command/plan, risks, exclusions, regeneration/redownload cost and verification; obtain exact approval through `question`. Disclose limitations; active, ambiguous or unverified targets are not ready to execute.

**Approval gate:** the user selected the exact executable plan or command, not merely a candidate or a request for explanation. Changed commands, targets, plans or persistent material drift require a new decision. A digest binds a workflow; it is not proof of human consent. Approval never extends to siblings or related data.

For independent low-risk caches only, a multi-select may offer `All listed low-risk caches` and `None`. Expand that choice into separate plans and outcomes. It must exclude personal data, models, apps, Docker volumes, cloud deletion, snapshots, VMs, databases and SDK state.

## 5. Execute and reconcile each target independently

Immediately before each selected group, recheck exact identity/scope, owner and known writer activity, capacity and branch-specific evidence. Execute groups sequentially through the reviewed owner workflow; no collector suggestion or raw recursive deletion is executable authorization. Verify owner/service health, exact target state, Trash destination where applicable, and `df -k` after each group.

Stop only the affected target when approval is missing, identity changes, the owner becomes active, cloud sync is incomplete, required tools/evidence are unavailable, material drift persists, or the scope may include unsynced data, credentials, active databases, shared model blobs, VM state or service-linked volumes. Keep it in the report and continue unrelated checks and approved actions. Only explicit cancellation or loss of the no-hydration guard stops the overall process.

For a selected blocked target, explain its exact owner/path and offer `Skip this target and continue`; offer `Retry this target` only for a concrete resolvable blocker. Retry means fresh checks, not forced deletion, replay or approval of a replacement action. Reconcile interrupted/partial execution before any new attempt; do not retry automatically. Unselected targets remain untouched and require no blocker-resolution questions. Do not turn a single-target failure into an all-or-nothing cleanup decision.

Preserve per-target outcomes and reasons: executor `BLOCKED`, `PARTIAL`, `EXECUTED`, `UNCHANGED`; dedicated workflows may use `MOVED-TO-TRASH`, `BLOCKED-PRIVILEGE`, `BLOCKED-IN-USE`, `SKIPPED`, `NOT-FOUND`. Interrupted action is not success. Report all outcomes even if none executed; call it user cancellation only if the user cancelled the whole process.

## 6. Finish the private report

Each run gets a fresh private directory under `macos-cleanup/` in the active project, with a unique timestamp/suffix, `report.md`, `inventory.jsonl` and validated per-module output. Never reuse a run directory or copy run-specific facts into this skill.

Include mode/scope, target volume, capabilities and module status; capacity checkpoints and drift; size-ordered candidates without overlap; cloud allocated/apparent sizes and sync/local-state evidence; separate app/package tables when required; redacted sensitive findings; approvals and executed groups; per-target before/action/after measurements and observed capacity delta; protected/ambiguous/unavailable/partial areas; verification and reconciliation. Use the inventory schema's common fields. If private raw output could not be removed, report retention and required reconciliation without displaying it.

**Completion gate:** every selected target has a verified outcome or an explicit unresolved condition, and every coverage gap remains visible. State which verification actually ran: unit/fixture tests are neither a live File Provider integration test nor an agent benchmark.

## Maintaining this skill

Keep examples hypothetical. After edits, search production files for absolute user-home prefixes, fixed disk identifiers, email addresses, UUIDs, PIDs and copied run measurements. Use fake owner tools and temporary local targets for tests; never validate documentation changes by cleaning the host Mac.
