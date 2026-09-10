# Inventory Schema

Audit scripts emit one JSON object per line. Consumers must tolerate unknown fields and retain records from modules that later return `PARTIAL` or `FAILED`.

## Common fields

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string | Current version is `1` |
| `record_type` | string | `capability`, `capacity`, `path`, `cleanup_candidate`, `cloud_root`, `cloud_container_inventory`, `snapshot`, `module_status`, `orchestrator_module`, `audit_summary`, or owner-specific type |
| `module` | string | Producing audit module |
| `status` | string | `COMPLETE`, `PARTIAL`, `FAILED`, `UNAVAILABLE`, `PERMISSION-DENIED`, or target state |
| `path` | string/null | Exact discovered path when safe to retain |
| `owner` | string/null | Application, provider, package manager, or system owner |
| `allocated_bytes` | integer/null | Allocated local blocks; estimate, not promised recovery |
| `logical_bytes` | integer/null | Apparent/logical size, especially for sparse/cloud trees |
| `estimated_reclaimable_bytes` | integer/null | Owner-aware estimate for the exact action; unknown is null, not the root size |
| `observed_available_delta_bytes` | integer/null | Signed capacity change over an action interval; includes concurrent activity |
| `volume_device` | string/null | Runtime-resolved device identifier |
| `classification` | string/null | Cleanup classification from `SKILL.md` |
| `evidence` | array/string/null | Metadata sources used, without secrets or raw private content |
| `proposed_action` | string/null | Legacy/advisory text, never executable input or approval |
| `recovery` | string/null | Regeneration, redownload, remote-only, backup, or unverified |
| `risk` | string/null | Material consequence of the proposed action |
| `confidence` | string/null | `high`, `medium`, or `low` |
| `sensitive` | boolean | Whether chat/report output needs redaction or aggregation |

All records should include `schema_version`, `record_type`, `module`, and `status`. Path-bearing records should include `sensitive`. Use `tool` rather than `owner` for capability records.

Capacity records add `total_bytes`, `used_bytes`, and `available_bytes`. Snapshot records add `snapshot_name` and `purgeable`. Cloud root records add `size_relation` to describe allocated/apparent evidence without claiming item-level placeholder state.

## Module completion

Every script must emit a final `module_status` record. A timeout or permission failure must not erase earlier records.

```json
{"schema_version":"1","record_type":"module_status","module":"cloud","status":"PARTIAL","evidence":"One provider root could not be measured: permission denied"}
```

`PARTIAL` is not equivalent to no candidates. Reports must name the unmeasured scope.

## Orchestration

`audit-all.sh` delegates to `audit_all.py`. Child commands are supervised by `audit_runtime.py` under a native no-hydration policy. Parse JSON, reject duplicate keys/non-finite numbers and validate field types and module identity. Preserve valid records even if later output is malformed or truncated; do not count malformed records as candidates. Unknown fields do not create permissions. Raw output is not a public report.

The runner creates a fresh private directory and appends an `orchestrator_module` record after every attempted module, including `child_module`, `observed_status`, `exit_code`, `timed_out`, `final_status_seen`, and `outcome`. Use `--modules cloud` for a fresh subset run, not to overwrite an earlier inventory. Relative module paths avoid publishing absolute home paths. Module completion is separate from full cleanup coverage.

The final `audit_summary` distinguishes requested/omitted modules and missing broad categories. All currently collected candidates are review-only. A COMPLETE module or a complete subset does not mean that a full cleanup audit is complete. Root discovery never makes `cloud_targets_actionable` true. Zero discovered roots from a failed module means unknown inventory, not no providers.

`requested_modules`, `attempted_modules`, `executed_modules` and `not_run_modules` distinguish selection from actual execution. `termination_reason` records cancellation or guard loss. `coverage_status` is separate from module-run `status`. Initial guard failure returns code 78 and a fixed diagnostic without an output directory; cancellation of a started run returns 130 with a partial report. Runtime failures, output limits and missing return codes cannot produce COMPLETE even if a child printed a success footer.

## Cleanup candidates

`cleanup_candidate` currently denotes a measured review item, not an authorized action. The runner demotes legacy or forged `actionable=true` to false. Collection alone does not verify ownership, process activity, locks, installed-version semantics, action scope or recovery. Do not manually promote an inventory record to bypass these requirements.

Cache records add `owner_activity=unknown`, `scope_state=unverified`, `verification_state=required`, `review_action` (non-executable guidance) and `estimated_reclaimable_bytes=null`. `confidence` describes measurement, not deletion safety. A separate strict action-plan schema is implemented in `cleanup_contract.py`; it is not an audit JSONL record and cannot be forged by setting actionable=true. See `adapter-contract.md` and `cleanup-action-workflow.md` for the digest/TTL/identity/journal protocol.

## Cloud roots

Cloud records should add:

- `provider_kind`: normalized provider family or `unknown-file-provider`;
- `domain_label`: redacted stable label, not an email address;
- `sync_state`: `caught-up`, `needs-sync`, `unknown`, or `unavailable`;
- `local_state`: `mixed`, `local`, `placeholder`, or `unknown`; root-level size scans normally remain `unknown`;
- `size_relation`: `logical-greater-than-allocated`, `similar`, or `unknown`;
- `eviction_method`: supported provider/Finder action or `unverified`.

Root aggregates remain null. `cloud_container_inventory` records with `inventory_scope=bounded-item-metadata` now carry per-root `items`, `counts`, `totals` and `enumeration_status`. Each item has a per-run HMAC identifier, not a personal filename. `local-allocation-present` is not proof of a fully synced/downloaded object; all sync/pin/eviction states remain unknown or unverified. The legacy `container_count` field on these records is the count of inspected items, not the count of iCloud application containers; distinguish by inventory_scope.

Incomplete or missing measurements produce null totals and separately labelled `allocated_lower_bound_bytes`/`logical_lower_bound_bytes`. Do not add root aggregates, child directory aggregates or shared inodes twice. `status` remains PARTIAL for eviction readiness even when `enumeration_status=COMPLETE`. Apple's du has a dataless safeguard; a failed guarded probe never authorizes an unguarded retry.

## Sensitive data

Do not persist tokens, credential contents, raw shell history, cloud account email addresses, personal filenames, or document contents. Aggregate sensitive candidates by category unless the user explicitly requests exact paths.
