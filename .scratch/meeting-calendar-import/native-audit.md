# Native source structural audit

## Scope

User made all OBS and Amanu exports locally available after the initial implementation. Inspection was read-only. No recording frames/audio were decoded, no source artifacts were changed, and no speech, participant labels, or meeting titles were copied into this report or test fixtures.

## Amanu corpus

- 32 top-level packages inspected.
- All 32 have object-shaped metadata, transcript JSON, and speaker-map JSON.
- All recording started/ended values are valid timezone-aware timestamps; observed native timestamps are UTC.
- Numeric non-boolean duration agrees with recording interval to within one second in all 32.
- 5,879 segments total, all with valid integer millisecond bounds, string speaker and text fields, and nondecreasing starts.
- 10 packages contain at least one segment ending after the metadata recording duration. The adapter must preserve the source rather than truncate or reject solely for that observed overshoot.
- 64 speaker-map entries: half object-shaped, half string-valued. Segment labels do not match map keys or string values in this corpus; raw segment labels are independently meaningful and must not be guessed or discarded.
- No markers for transcription were present in the 32 packages. Absence is not idle evidence.
- All 32 logs contain one exact UTC `done — N segments` record with a count matching JSON. All have later archive/cleanup records, so the done record is not the last line. Transcript creation timestamps are close to second-precision completion records.
- None of the observed segment labels contains colon, newline, or edge whitespace. No observed segment text has leading/trailing spaces, trailing newline, literal double-open-brace tokens, or an internal blank-line speaker-delimiter pattern. Synthetic fixtures should still cover preservation edge cases.

## OBS sample

Metadata-only ffprobe inspection of three timestamp-named MOV files found aware UTC creation timestamps and positive finite durations. Container creation clocks agree with filenames under a three-hour difference in these samples and are consistent with recording start. This does not justify a universal timezone rule or automatic event match from time alone.

## Readiness qualification

Matching completion records corroborate produced transcript snapshots. No universal producer-owned lock/release guarantee was discovered. Native transcript readiness and native rename safety remain separate. A subsequent native-release protocol combines completion evidence, stable inventories, calibrated OS handle observations and independent idle attestation to make supported native renames possible. This is observed quiescence, not a guarantee against invisible or future writers. Unknown or busy results still block mutation; neither downloaded files nor approval of import fabricates release evidence.

## Adapter verification

Implementation, public CLI regression tests, and final review results are recorded separately in the implementation report after completion. This audit establishes observed native structures, not permission to import or rename actual meetings.
