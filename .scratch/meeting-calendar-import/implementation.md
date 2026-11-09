# Calendar-linked meeting import: implementation status

Status: resolved, including successful native OBS/Amanu rename verification

## Delivered behavior

- Public calendar preview/apply workflow extends meeting-transcript; explicit canonical import and summary preparation remain available.
- Approved nested global/project TOML settings, one-run overrides, setup validation, pending/declined tracking, incremental boundaries and calendar-update proposals.
- Review-bound apply, project writer locking, safe canonical publication and journal-based process-interruption recovery.
- Native Amanu JSON conversion in memory with original text, labels, order and recording times preserved. No source adapter files are created.
- Native OBS timing and container/stream evidence inspected through bounded metadata-only ffprobe.
- Native OBS/Amanu rename eligibility is no longer permanently false: completion evidence, two stable observations spanning at least two seconds, calibrated same-scope lsof checks, explicit operator attestation and exact plan approval can permit a real rename.
- Apply repeats native release checks after imports/hashing, preserves the approved inventory fingerprint, pins parent ancestry, performs descriptor-relative exclusive rename, and verifies destination identity/source-name absence. Guarded rollback preserves concurrent occupants on uncertain postconditions.

All seven local tickets are resolved. Passing development checks does not authorize mutation of actual user recordings.

Global source configuration exists in the user's standard meeting-transcript configuration directory with the approved OBS/Amanu locations. Project calendar settings have not been guessed or created. Provider acquisition remains with the selected authenticated integration; credentials are not stored in these settings.

## Native source verification

After the initial dataless-file blocker was removed, read-only inspection covered all 32 Amanu packages and 5,879 segments, including speaker-map variants, timing overshoot and completion records. Independent in-memory conversion validated 32/32 packages without writing output or printing speech. See `native-audit.md` for aggregate evidence.

The canonical template carries recording-level times. Original millisecond segment bounds remain in unchanged source JSON, protected by raw-input hashes. Exact-label consecutive chunks merge with newline; case-distinct labels do not merge. Unrepresentable content remains pending instead of being repaired.

Actual event-title-specific canonical output and raw inputs are hash-bound and regenerated at apply/prepublication. Workflow plans/state/journals do not contain copied speech bodies or source JSON.

## Native release follow-up

The user correctly identified that the previous implementation tested native refusal but left every native rename ineligible. This follow-up adds a genuine successful path and real OS-tool regressions rather than weakening that refusal with a force-safe flag.

The observer opens existing source data read-only and requires lsof to report that exact PID/FD/device/inode as a positive control in the same scan. Only that descriptor is excluded. Other handles are busy; warnings, incomplete records, malformed NUL/LF boundaries, timeouts, missing tools or missing controls are unknown. Raw listings and process details are not persisted.

The evidence is observed quiescence, not an unconditional producer lock. Operator attestation is scoped to selected artifacts and includes no producer restart until completion. A positive-duration container, completion log, marker absence, downloaded file or attestation alone is insufficient. Unknown sources still refuse rename, but supported quiescent native sources can now succeed.

On tested macOS lsof 4.91, recursive +D can return 1 with a valid descendant control when the directory itself has no handle. Status 0/1 is accepted only with complete strict records, empty diagnostics and the exact positive control; neither exit status alone proves release.

## Final verification

```text
python3 -m unittest discover -s meeting-transcript/tests -v
Ran 81 tests in 87.141s
OK
```

All 81 tests ran; none skipped. This includes 72 public calendar-workflow regressions and 9 original canonical import/summary tests.

Successful native tests use the actual system lsof: Amanu package rename preserves internal files, and OBS uses a real ffmpeg-generated MP4 with planned calendar-derived naming. These are temporary fixtures, not user recordings. Coverage also includes external open descriptors, late writers, observation-time additions/content changes, same-size/mtime changes, missing/warning/empty/malformed inspector output, a literal LF in a package name, transcription restart/marker uncertainty, source/parent replacement, late destination collision, post-rename rollback and occupied-name rollback refusal.

Compilation of all four scripts and both test modules passed. Documentation links, 12 eval definitions and whitespace checks passed. Agent eval scenarios were not executed.

## Review axes

### Standards

Earlier adapter/output-binding findings were fixed before this follow-up. Native-release review found one P2 strict-record-framing issue; it was fixed with public regressions based on actual lsof output containing the real control. Final independent Standards review reran all 81 tests without skips and reported no new concrete P1/P2 findings.

### Spec

Native-release review found two P2 issues: acceptance of process-only inspector records and rebasing apply observations onto a changed inventory. Both were fixed. The final independent Spec review reran all 81 tests without skips and confirmed approved-inventory binding, successful native rename coverage, late-collision and guarded rollback cases. No new concrete P1/P2 findings remained.

## Runtime boundaries

- Observations cannot guarantee visibility into every process or prevent an invisible/future writer. These limits are disclosed in the native-release protocol rather than being used to prohibit all native renames.
- Actual provider, calendar identifiers, timezone and initial date still require explicit project setup. Complete provider pagination and known-event refresh are the caller's responsibility.
- Deleted/cancelled known events without refreshable provider data require manual reconciliation.
- Tests cover process interruption, not power loss or filesystem corruption. This verification ran on macOS; Linux behavior has not been exercised here.
- No actual meeting imports, source renames, source deletion, transcription, summarization or calendar mutations were performed during development.
