# Native source release verification

## Meaning of an eligible result

Native rename eligibility is an operational observation, not an unconditional producer lock:

> The selected recording has validated completion evidence, its rename unit remained unchanged during a bounded observation window, and the operating-system handle inspector detected no other open handles in its observable scope. The operator separately attests that the owning recorder/transcriber is idle and will not restart until the operation finishes.

This permits successful native OBS/Amanu renames instead of hard-coding them as unsafe. It does not prove that an invisible process or future writer can never modify the artifact. Show the evidence and its visibility limitation before asking for approval.

An import request, downloaded files, a positive-duration container, a missing transcription marker, or operator attestation alone is insufficient.

## Evidence bundle

For a requested native rename, require all of the following:

1. **Native completion evidence.** Amanu has an ended recording, valid transcript snapshot and a matching completion record, as described in `native-recordings.md`. OBS has validated readable container structure and plausible completed-recording timing; do not describe a duration tag as a producer-owned stopped signal. A live MKV/fragmented container may remain readable while being written.
2. **Stable rename unit.** Observe the exact file or complete package inventory more than once over at least two seconds of monotonic time. Bind filesystem identities and relevant metadata/content fingerprints. Additions, removals, replacements, unreadable descendants, symlinks or unsupported traversal make the result unknown or changed, not eligible.
3. **Calibrated handle inspection.** Keep one precisely identified read-only control descriptor open on the existing recording or a package input. The same `lsof` invocation and selection scope must report that live PID/descriptor/device/inode. Exclude only that descriptor; another descriptor in the same agent process is still a conflicting handle. Never narrow the scan to the agent PID or current user just to obtain a control match.
4. **No other observed handles.** Any visible source/package handle blocks rename, even if opened read-only. This conservative rule avoids guessing which readers may turn into writers. A timeout, missing inspector, missing control, warning, traversal problem, unsupported status or malformed output means unknown. Empty output or exit status 1 is not a safe negative result.
5. **Independent attestation and exact approval.** The operator's idle attestation applies to selected artifacts and owning producers, not every recording in a directory. The reviewed plan and its rename permission must also be approved.
6. **Fresh mutation-boundary check.** Repeat completion, stability and handle checks after import/hash readers close, immediately before rename. Recheck source and parent identities and destination absence, then use exclusive no-clobber rename. Verify the renamed destination's identity and absence of the old source name. A changed or unknown result invalidates the mutation rather than silently promoting it to safe.

## Read-only inspection

The verification itself must not create sidecars, markers, caches or control files inside recordings. The positive control uses an existing file opened read-only. Preview remains read-only; the caller may explicitly save the printed plan outside source directories.

Retain aggregate evidence, timing, source fingerprints and the inspector's method/scope in the plan. Do not retain process names, command lines, observed PIDs, raw `lsof` listings, or transcript content as proof artifacts. Close the control descriptor on every path before mutation.

The current `lsof` command interface was checked against its official documentation through Context7 and the installed macOS 4.91 help/man page. NUL field mode still uses newline boundaries for process/file sets. Machine output must be parsed as structured records rather than relying on human-readable columns; incomplete process-only records or missing/misplaced boundaries mean unknown.

On the tested macOS 4.91 build, recursive `+D` may return status 1 while correctly reporting the held descendant control FD when the directory itself has no handle. Status 0 or 1 is accepted only together with empty diagnostics, complete strictly valid records, and the exact same-scope positive control. Neither code is independently proof of release. Other return codes, empty output or missing control are unknown.

## Result categories

- **Eligible:** the full observation bundle succeeded and independent operator attestation is present. Apply still repeats verification.
- **Busy:** another handle is observed or active native processing is identified. Refuse rename and report the reason without disclosing process details.
- **Unknown/changed:** inspection cannot establish the bundle, metadata changed, the source was replaced, or the tool cannot observe reliably. Refuse rename; never interpret it as idle.
- **Renamed and verified:** exclusive rename succeeded and postconditions match the approved artifact.

Failure after a previously eligible preview must not advance the synchronization boundary as if the rename succeeded. Preserve recovery information and existing source data. Source collisions and ambiguous rollback outcomes require explicit reconciliation; never overwrite to recover.

## Practical limitations

Repeated scans and stable snapshots are observations rather than an atomic global filesystem snapshot. The inspector can only report handles visible to it; a positive control establishes that the selected invocation works for its control, not universal coverage of every process. Permissions, runtime warnings and traversal failures must fail closed.

A producer that deliberately ignores the operator's attestation or starts after the final check cannot be excluded without a producer-owned interlock. This limitation is disclosed rather than used to make all native renames permanently unavailable. Do not claim that `lsof` proves the producer is stopped.

Use temporary native-format fixtures and real inspector processes for successful/active-handle regression tests. Unknown-tool and warning cases can exercise the external-tool boundary. Development and passing tests do not authorize renaming actual user recordings.
