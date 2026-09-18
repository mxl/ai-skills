# Reviewed Cache Actions

The contract, initial uv/Go adapters, executor and CLI have been integrated and tested on synthetic local fixtures. No real owner-cache deletion or live cloud-provider eviction was tested. Implementation status is tracked in the parent project's DAG; no audit candidate automatically becomes an approval.

## Separation of commands

Use `python3 <skill>/scripts/cleanup_actions.py --help` for syntax. Resolve `<skill>` to the actual installed skill directory; run from the user's active project.

| Command | Purpose | Can invoke cleanup? |
|---|---|---|
| `review --adapter uv` | Version, configured cache, feature and writer evidence | No |
| `prepare --adapter uv --output <new-private-plan>` | Capture a fresh scoped plan and digest | No |
| `show --plan <private-plan>` | Show the reviewed scope, risks, expiry and digest | No |
| `execute --plan <private-plan> --confirm-digest <exact-digest> --journal-dir <private-journal>` | Verify and dispatch the selected supported action | Yes, only after explicit user choice |
| `cloud-metadata --root <exact-provider-root>` | Bounded metadata inventory, not eviction | No |

The bundled digest-bound executable action types are narrowly scoped uv cache pruning and Go build-cache cleaning, subject to the installed-version adapter's checks. When an adapter is unavailable, review-only, or outside its version policy, the agent may instead prepare an owner-command fallback for a narrow rebuildable target: inspect the installed CLI help, verify exact scope and owner activity, prefer dry-run/status evidence, disclose risks and exclusions, and obtain separate exact approval before execution. Models, Docker volumes, snapshots, apps, app data, SDKs, personal files and cloud eviction are not eligible for this fallback.

## Approval procedure

For broad cleanup, complete the available in-scope audit and present consolidated results before asking which targets to clean. Owner activity is audit evidence, not a prerequisite for finishing the audit. Do not ask the user to close an IDE or choose whether to stop the whole cleanup because one cache is busy. Selection of a review-only target requests preparation; it is not executable-plan approval.

1. Review owner evidence. Busy or unknown writers, incomplete visibility, ambiguous scope or centralized environments remain blockers; do not replace these checks with raw deletion.
2. Prefer a digest-bound adapter plan when supported. Otherwise inspect the installed owner CLI's local help and prepare an exact owner-command fallback with its target, options, dry-run/status evidence, risk, exclusions and verification steps.
3. Show the plan or fallback command. Explain that the owner command manages a dynamic cache scope, not an immutable list of file deletions. Do not promise the entire allocated size will be freed.
4. Ask for this exact plan or fallback command through the question tool. A question, empty response, decline or request for more information is not selection.
5. After selecting an adapter plan, pass the exact displayed digest to the executor. After selecting a fallback, run only the exact approved owner command. Do not silently replace its target or options; any change needs a new decision.
6. Reconcile each target independently. A blocked preflight performs no cleanup for that target, but must not block review or execution of unrelated approved plans. An interrupted or partially completed action must not be retried automatically. Inspect its journal or current owner state before any newly approved attempt, then continue with other independent targets.

If preparation or execution of a selected target is blocked, ask only whether to retry that exact target after resolving its blocker or skip it and continue. Do not ask about blockers on unselected targets. A retry cannot bypass safety checks, replay an attempted digest, or authorize a changed fallback command. Missing installed-help evidence, active writers, ambiguous targets and excluded high-risk classes remain blocked. Keep unrelated selected targets in the queue.

A digest is an integrity and workflow binding, not a signature proving that a human clicked a UI. The assistant must still enforce the explicit selection step. Neither a saved plan nor its presence in a report authorizes execution.

## Verification and limitations

- The executor checks plan freshness, tool/target identity, owner evidence and capacity drift immediately before dispatch and uses only the trusted built-in command builder.
- A journal reservation precedes mutation, preventing replay of the same plan even if the previous attempt was interrupted.
- An owner command's success is not enough: postcheck must determine whether target metadata changed and report failed/incomplete verification.
- Record allocated/logical measurements, unknown estimated reclaim and observed available-space delta separately. Concurrent writes can affect the last metric.
- Process-scoped no-hydration protection remains enabled; it is not a sandbox for a malicious process running as the same user. Metadata fingerprints reduce stale-target risk but do not replace owner locks or eliminate all concurrency races.
- A bounded capture may refuse large or symlink-containing caches. This is an explicit conservative limitation, not permission to rerun an unbounded scan or delete directly.
- Known cloud namespaces are excluded from cache plans even when local files exist. Custom third-party sync locations require separate review; local availability alone does not authorize remote namespace deletion.
- The cloud metadata command cannot prove sync/pin/conflict state or safe eviction by allocated bytes alone. All such items remain non-actionable until a provider-specific adapter supplies the missing evidence.

## Safe development

Use only fake owner executables/probes and temporary local targets in tests. A test of CLI forwarding alone does not prove executor validation; test the actual plan roundtrip, changed-target rejection and invalid-digest path separately. Native guard tests do not substitute for an instrumented File Provider integration test.
