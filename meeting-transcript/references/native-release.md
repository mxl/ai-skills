# Native source release verification

Authority: observed-release protocol and rename safety. Read [native-recordings.md](native-recordings.md) for native formats, conversion and transcript readiness.

## Meaning of an eligible result

Eligibility is a bounded observation, **not a producer lock**: the completed recording's rename unit stayed unchanged, the calibrated OS inspector found no other visible handles, and the operator independently attests its recorder/transcriber is idle and will not restart until the operation finishes.

Show evidence and [visibility limitations](#practical-limitations) before exact approval. Download, import intent, duration, marker absence or attestation alone cannot establish eligibility.

## Evidence bundle

For each requested native rename, require every gate:

1. **Completion.** Amanu: ended recording, valid transcript and snapshot-bound completion record per [readiness checks](native-recordings.md#readiness-is-not-rename-safety). OBS: known readable container structure, positive finite duration and plausible completed-recording timing. A duration tag is not a producer-owned stopped signal; live MKV/fragmented containers can remain readable while being written.
2. **Stability.** Compare complete inventories of the exact file/package across a fixed **at least two-second monotonic window**, with two calibrated handle scans. Bind filesystem identities, relevant metadata/content fingerprints and parent identity. Additions, removals, replacements, unreadable descendants, symlinks or unsupported traversal yield unknown/changed.
3. **Positive control.** Hold one existing recording/package input open read-only. The **same `lsof` invocation and selection scope** must report its exact live PID/FD/device/inode. Exclude **only this FD**; every other source/package handle blocks, including read-only handles and the agent's other FDs. Never narrow inspection to the agent PID/current user to obtain a control match. Apply the strict [inspector acceptance rules](#read-only-inspection).
4. **Independent attestation and exact approval.** Scope idle/no-restart attestation to selected artifacts and owning producers, not the whole directory. Separately obtain approval of the reviewed exact plan and rename permission.
5. **Fresh apply checks.** After import/hash readers close, repeat completion, full-inventory stability and calibrated handle checks immediately before rename. Compare against the **approved inventory**, without rebasing approval onto changed state. Changed/unknown evidence blocks mutation.
6. **Anchored mutation.** Hold a verified parent directory FD through checks and descriptor-relative exclusive no-clobber rename. Recheck source/parent identities and destination absence. Verify destination identity and old-source absence afterward. On failed postconditions, attempt reversal only for the exact artifact and without overwriting; preserve unknown-outcome journal/recovery information for manual reconciliation. Errors never advance the synchronization boundary as success.

## Read-only inspection

- Verification/preview are read-only: no sidecars, markers, caches or control files inside recordings; control uses an existing file. The caller may explicitly save the printed plan outside sources. Close the control FD on every path before mutation.
- Retain aggregate evidence, timing, fingerprints and inspector method/scope. Keep process names, command lines, observed PIDs, raw `lsof` listings and transcript content out of proof artifacts.
- Parse machine output strictly: NUL-delimited fields retain LF boundaries between process/file sets (literal LF inside a name is not a boundary). Require complete valid records; process-only records, missing/misplaced boundaries or malformed output mean unknown.
- On tested **macOS `lsof` 4.91**, recursive `+D` can return 1 with a valid descendant control when the directory itself has no handle. Accept **0 or 1 only with empty stderr, complete valid records and the exact same-scope positive control**. Neither status alone proves release; empty output alone is unknown. Timeout, missing inspector/control, diagnostics, traversal failure or unsupported status also mean unknown.

## Result categories

| Result | Required action |
| --- | --- |
| **Eligible** | Full bundle plus independent attestation; apply must repeat verification. |
| **Busy** | Other handle or active native processing: refuse rename, report reason without process details. |
| **Unknown/changed** | Incomplete/unreliable inspection, changed metadata or replaced source: refuse rename, never infer idle. |
| **Renamed and verified** | Exclusive rename and approved-artifact postconditions succeeded. |

After an eligible preview, failure still preserves source data/recovery information and blocks boundary advancement. Collisions or ambiguous rollback require explicit reconciliation, never overwrite.

## Practical limitations

Inventories/scans are not an atomic global filesystem snapshot. A positive control calibrates only its invocation/scope, not visibility of all processes; permissions, warnings and traversal failures fail closed. Invisible processes and writers starting after the final check cannot be excluded without a producer interlock. Disclose this limit rather than permanently blocking all native renames or claiming `lsof` proves the producer stopped.

Test successful/active-handle paths with temporary native-format fixtures and real inspector processes; test unknown-tool/warning paths at the external-tool boundary. Passing tests/development do not authorize touching actual user recordings.
