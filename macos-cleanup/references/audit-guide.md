# Audit Guide

Read before storage audits. Use the Implemented Scope table below to distinguish bundled coverage from required supplementary checks; an inventory is not proof of removal readiness. Apply the parent [skill](../SKILL.md)'s guard, privacy and measurement gates. When snapshots are found, read [snapshots](snapshots.md) before reporting their sizes or proposing actions.

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

## Capability Discovery

For broad `cleanup`, run `scripts/audit-all.sh` from the active project, using its resolved absolute skill path. It delegates to the Python orchestrator, validates JSON records, supervises guarded process groups, preserves valid partial results and writes private reports. Use `--modules <comma-separated-list>` for a fresh subset run; this is not a complete cleanup audit. Read `references/inventory-schema.md` before consuming records.

The default and maximum module timeout is 3600 seconds. `--timeout` or `MACOS_CLEANUP_MODULE_TIMEOUT` can explicitly select a lower limit. This is per module, not the total audit duration: for a default four-module run, set the calling tool's outer timeout to at least 14520000 milliseconds (four hours plus two minutes for overhead), rather than relying on its 120000-millisecond default. For subsets or explicit overrides, budget the module count times the selected timeout plus overhead. If the tool cannot wait that long, use its supported supervised background execution and poll the same run; do not launch duplicate audits merely because the foreground wait expired. Cloud traversal hard limits remain unchanged.

The existing shell modules are collectors, not safe standalone entry points. For a narrow filesystem collector, launch it through `python3 scripts/audit_runtime.py --guard-exec /bin/sh <collector> <arguments>` using resolved paths. No content searches, hashes, previews, or broad recursive `$HOME` scans are part of ordinary audit. Do not silently fall back to an unguarded collector if Python or the Darwin policy is unavailable.

Audit scripts do not invoke cleanup commands. Owner CLI discovery can have incidental metadata/cache effects; do not describe this as an OS-wide read-only sandbox. If a module fails or times out, retain valid records and mark the scope `PARTIAL`. Do not replace a blocked module with an untracked, unguarded scan. `proposed_action` strings in older records are advice, never executable input or approval.

## Implemented Scope

| Area | Current implementation | Remaining limitation |
|---|---|---|
| Runtime | Process-scoped native no-hydration guard; supervised command groups | Not a security sandbox for hostile processes or an OS-wide download switch |
| Orchestration | JSON validation, private fresh reports, module outcomes and honest full/partial coverage | Databases, projects and personal-file analysis are not yet implemented |
| Capacity/snapshots | Target-volume discovery, snapshot inventory, and explicit size-status reporting | Supported CLI/API inventory does not expose Disk Utility's per-snapshot Private Size or Cumulative Size |
| Paths | Narrow allocated/apparent measurements | No automatic full-home content scan |
| Caches | Measurement plus guarded version/writer review; plan adapters for uv and Go; reviewed owner-command fallback | Missing or blocked adapters require command-specific preflight, exact approval and verification |
| Cloud | Root discovery automatically invokes bounded metadata inventory per root | Sync/pin/conflict/eviction remain unverified; no cloud mutation is implemented |
| Execution | Digest-bound uv/Go executor plus approved owner CLI fallback for narrow, rebuildable targets | No raw recursive deletion, automatic retry, or automatic reconciliation; audit records never authorize actions |
| Apps | Guarded bundle inventory with bounded allocation measurement | App data, helpers, activity and uninstall readiness remain separate |
| Packages | Guarded Homebrew/npm/pnpm inventory and cache/install-root measurement | Cleanup may use installed owner CLI after dry-run/help review; package removal remains separate |
| Docker | Owner-reported images, containers, build cache and volume inventory; owner CLI fallback for build cache and unused images | Other contexts and Docker Desktop VM allocation are not covered; volumes remain dedicated high risk |
| Models | Hugging Face cache inventory with allocated/logical separation | Shared-blob reclaim and other model managers remain unverified |
| VM/SDK | Xcode/simctl, Android and detected VM-owner inventory | Exact removal readiness and complete owner coverage remain unverified |

Unit/fixture tests are not a live File Provider integration test or an agent benchmark. State which verification actually ran.

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

## Application and Package Audit

For broad `cleanup`, audit GUI apps and package managers. For named `uninstall`, restrict scope to the named apps.

For GUI apps, collect bundle path, allocated size, install/update evidence, Spotlight last-used metadata, exact running processes, cask ownership, helpers, launch jobs, extensions, and app-data candidates. Null Spotlight metadata is missing evidence, not proof of non-use.

For Homebrew, set `HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_NO_AUTOREMOVE=1` to separate incidental maintenance. Verify installed-version semantics and use the same configuration for preview and action. Treat explicit autoremove as a separate package decision. Prefer bulk installed JSON, guarded Cellar/Caskroom measurements, leaves, dependency graphs, pins and services over slow per-formula calls.

Separate install age from usage evidence. Protect dependencies, active dependents, pinned packages, running services, active project toolchains, OCR/model tooling, databases, and provider clients. Missing history produces `AMBIGUOUS` or `REVIEW-CANDIDATE`, never `unused`.
