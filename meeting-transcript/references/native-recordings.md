# Native recording formats and evidence

## Verified local Amanu corpus

After the user explicitly downloaded the source corpus, read-only structural inspection covered 32 top-level Amanu packages and 5,879 transcript segments. No speech, participant names, or meeting titles were copied into fixtures or audit output. The earlier `compressed,dataless` / `Resource deadlock avoided` blocker no longer applies to those downloaded samples.

Observed files:

- `meta.json`: object with timezone-aware UTC `started` and `ended`, numeric non-boolean `duration_seconds`, plus optional technical fields such as `app`, `mic_capture`, `recovered`, and stop-hook information. Optional capture fields vary and must not become unconditional requirements.
- `transcript.json`: object with `created_at`, `engine`, `model`, and nonempty ordered `segments`.
- Each segment: integer `start_ms` and `end_ms`, string `speaker`, string `text`.
- `speakers.json`: object with `created_at` and a `speakers` map. The observed map contains both string values and objects with `name` / `source`.
- `transcript.md`: timestamped utterances such as `**[minutes:seconds] Speaker:** text`; this is not the canonical meeting-transcript template.

All audited packages had valid aware recording timestamps and numeric durations consistent with the interval to within one second. Segment start ordering and field types were valid throughout. Ten packages had a final segment end beyond the metadata recording duration: do not truncate source text, reorder segments, or declare invalid solely from that observed chunk-timing overshoot.

Observed segment speaker strings did not match speaker-map keys or string values. They are labels in their own right, not grounds for guessing a person. Preserve the segment label unless an explicit exact mapping is supported by the adapter; never fuzzy-match speaker identities or treat an absent mapping as proof that speech is invalid. Use labels actually present in turns rather than assuming every map entry attended.

## Transcript conversion

Prefer structured native JSON over parsing displayed minute/second timestamps from Markdown. Convert only a validated, internally consistent snapshot into canonical meeting Markdown, in memory. Preserve source text, speaker labels, order, and source recording times. Joining consecutive chunks from the same speaker with newlines follows the existing canonical convention.

Use the explicitly matched calendar title when no actual source meeting title is available. An application name or helper-process name is not the meeting title. Unsupported/unrepresentable labels, malformed JSON, missing recording end, or incomplete transcript data must remain pending with an explanation, not be silently repaired.

The existing canonical template carries recording-level Started/Ended metadata, not per-segment timestamp annotations. Segment millisecond bounds remain intact in the original JSON and are protected by its raw-input hash; do not insert timestamps or synthetic IDs into speech or change the fixed template to invent a new timed transcript format.

Preview must bind the source inputs and actual event-title-specific converted output to hashes. Revalidate at apply; source edits invalidate approval. Do not persist an extra adapter JSON or overwrite the original native Markdown/JSON.

## Readiness is not rename safety

All 32 audited logs contain one completion record with the grammar `<UTC ISO timestamp> done — <integer> segments`; it is not the last line, because archive/cleanup records follow. All 32 counts match their JSON segment counts, and transcript `created_at` values are close to the log's second-precision completion time. Validate that completion evidence belongs to the current transcript snapshot rather than accepting an arbitrary old `done` line. Missing, stale, or mismatched completion evidence keeps the native transcript pending.

Sample metadata also contains `ended`, stop reasons such as `call-ended` or `system-sleep`, and `on_stop_hook = fired`. These corroborate production of transcription output, not release of every recorder/transcriber file handle.

The audit found no `.transcribing.json` markers; absence does not prove idle. Presence alone would not prove active processing either. Native transcript snapshot eligibility and source rename eligibility must remain separate decisions. A supported snapshot can be imported after approval and unchanged-input checks while the original package remains untouched.

No universal producer-owned idle/release contract was established. Keep native source renames blocked without positive verified idle evidence and explicit operator attestation. Do not write normalized `processing = idle` metadata into real packages to bypass this protection.

## Native OBS timing evidence

Read-only `ffprobe` checks of three timestamp-named MOV files found timezone-aware UTC container `creation_time` and finite format duration. In those samples, creation time matches the filename clock with a three-hour difference and is consistent with recording start rather than end. That sample observation is not a universal timezone or start-time guarantee.

Use container timestamps only as labeled evidence for candidate selection. Require an explicit match when interval overlap is the only evidence; never hard-code the observed three-hour difference, substitute filesystem modification time, or infer that OBS has stopped writing. Missing or invalid probe data remains unknown. Probe only metadata, without decoding frames or audio or writing reports.

The official ffprobe documentation defines the `-show_entries`, `format_tags`, and JSON writer interfaces: https://ffmpeg.org/ffprobe.html . Context7 documentation lookup was performed before implementing this integration.
