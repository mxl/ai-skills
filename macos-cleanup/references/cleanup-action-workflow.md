# Reviewed Cache Actions

Read before preparing or executing cache/owner-CLI actions. The parent [skill](../SKILL.md) owns audit ordering, target selection, exact approval, independent outcomes and retry/skip behavior; apply those gates here.

The contract, initial uv/Go adapters, executor and CLI have been integrated and tested on synthetic local fixtures. No real owner-cache deletion or live cloud-provider eviction was tested. Implementation status is tracked in the parent project's DAG; no audit candidate automatically becomes an approval.

## Digest-bound adapter

Resolve `<skill>` to the installed skill directory and run from the user's active project. Use `python3 <skill>/scripts/cleanup_actions.py --help` for installed syntax and read [adapter sources](cache-adapter-sources.md) for version limits.

| Command | Purpose | Can invoke cleanup? |
|---|---|---|
| `review --adapter <owner>` | Fresh version, configured cache, feature and writer evidence | No |
| `prepare --adapter <owner> --output <new-private-plan>` | Capture identity, scope, risk, expiry and digest | No |
| `show --plan <private-plan>` | Display the reviewed plan and digest | No |
| `execute --plan <private-plan> --confirm-digest <exact-digest> --journal-dir <private-journal>` | Verify and dispatch the selected supported action | Only after exact approval through `question` |
| `cloud-metadata --root <exact-provider-root>` | Bounded metadata inventory, not eviction | No |

The bundled executable action types are narrowly scoped uv cache pruning and Go build-cache cleaning. Busy or unknown writers, incomplete visibility, ambiguous scope or centralized environments remain blockers. uv requires an explicitly supported link mode; do not change environment variables to bypass review. Go clears inherited flags and external cache-program settings before its fixed build-cache-only command. Current environment settings alone cannot establish historical link relationships or shared environments.

After approval of the displayed plan, pass its exact digest to the executor. Freshness is rechecked immediately before dispatch; journal reservation blocks replay of every attempted digest, including interrupted attempts. A new or changed plan needs fresh exact approval. A saved plan or a digest is not evidence of human consent.

## Owner-command fallback

Prefer a supported adapter. If missing, review-only or outside version policy, an installed owner CLI may handle a narrow rebuildable target after this preflight:

1. Resolve the executable and configured target on this Mac; discover paths and package-manager prefixes.
2. Inspect installed `--help` or equivalent local documentation. Use only supported options; examples below are not executable recipes.
3. Check the exact owner and known writers/dependents, including editors/gopls for Go. Missing visibility means unknown, not idle. An active writer blocks this target only; force flags cannot bypass it.
4. Prefer owner dry-run, status, verify or inventory evidence. Show the exact command, target/scope, options, estimated benefit, recovery cost, exclusions and verification. Owner commands manage dynamic cache scope, not an immutable file list; do not promise the entire allocated size will be freed.
5. Obtain separate explicit approval for that exact owner command through `question`; candidate selection or another group's approval is insufficient.
6. Re-preflight identity, activity and capacity, then execute one approved command at a time, without shell wildcards or raw recursive deletion. Capture `df -k` and owner state before and after, using the parent skill's drift rules.
7. Reconcile completed, partial, failed or unchanged state. After timeout/interruption, inspect current owner state before any newly approved attempt; never automatically retry or silently substitute another method.

Possible owners/actions include `brew cleanup`, `npm cache clean`, `pnpm store prune`, `go clean -cache`, `uv cache prune`, `docker builder prune` and `docker image prune` only when installed help confirms the required scope. Docker additionally requires active container/compose preflight.

This fallback excludes Docker volumes, cloud namespace deletion or eviction, models with shared blobs, VMs, simulators, SDKs, databases, snapshots, applications, application data, credentials, personal files and privileged system maintenance. Use the dedicated workflow and [action policy](action-policy.md) for those classes. Missing help, ambiguous scope, active writers and excluded classes stay blocked; raw deletion is not a fallback.

## Verification and limitations

- The executor checks freshness, tool/target identity, owner evidence and capacity drift immediately before dispatch and uses only the trusted built-in command builder.
- Command success alone is insufficient: postcheck target metadata and owner/service health, and report failed/incomplete verification.
- Record allocated/logical measurements, unknown estimated reclaim and observed available-space delta separately. Concurrent writes affect the last metric.
- Process-scoped no-hydration protection remains enabled; it is not a sandbox for a malicious same-user process. Metadata fingerprints reduce stale-target risk but neither replace locks nor eliminate concurrency races.
- A bounded capture may refuse large or symlink-containing caches. Keep that conservative limitation; do not rerun an unbounded scan or delete directly.
- Known cloud namespaces are excluded from cache plans even when local files exist. Custom sync locations need separate review; local availability never authorizes remote deletion.
- Cloud metadata alone cannot prove sync/pin/conflict state or safe eviction. Items remain non-actionable until provider-specific evidence supplies those checks; follow the [cloud workflow](cloud-offline.md).

## Safe development

Use fake owner executables/probes and temporary local targets only. CLI forwarding tests do not prove executor validation: test the actual plan roundtrip, changed-target rejection and invalid-digest path separately. Native guard tests do not substitute for an instrumented File Provider integration test.
