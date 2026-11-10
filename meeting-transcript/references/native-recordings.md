# Native recording formats and evidence

Authority: native formats, conversion and transcript readiness. For source renames, also read [native-release.md](native-release.md); readiness is not release.

## Verified local Amanu corpus

Read-only inspection after the user's explicit download covered **32 top-level packages / 5,879 segments**, without copying speech, participant names or meeting titles into fixtures/audit output. The earlier `compressed,dataless` / `Resource deadlock avoided` blocker no longer applies to those samples.

| Input | Structure |
| --- | --- |
| `meta.json` | Object: aware UTC `started` / `ended`, numeric non-boolean `duration_seconds`. Capture fields (`app`, `mic_capture`, `recovered`, stop-hook information) vary and are optional. |
| `transcript.json` | Object: `created_at`, `engine`, `model`, nonempty ordered `segments`. Each segment has integer `start_ms` / `end_ms`, string `speaker` / `text`. |
| `speakers.json` (optional) | Object: `created_at`, `speakers` map with string values or objects containing `name` / `source`. |
| `transcript.md` | Timestamped utterances (`**[minutes:seconds] Speaker:** text`), not canonical meeting Markdown. |

All samples had valid aware timestamps, numeric durations within one second of the recording interval, valid segment types and start ordering. **Ten packages overshot metadata duration at the final segment end**; this alone does not invalidate speech or authorize truncation/reordering.

Observed segment speaker labels matched neither map keys nor string values. Preserve unresolved labels; resolve identities only through adapter-supported exact mappings, never fuzzy matches. Derive participants from labels actually used in turns, not every map entry. An absent mapping does not invalidate speech.

## Transcript conversion

1. Convert validated, internally consistent structured JSON to canonical Markdown **in memory**, preserving text, labels, order and recording times. Join consecutive same-speaker chunks with newlines per canonical convention.
2. Without a true source meeting title, use the explicitly matched calendar title—not an app/helper-process name. Unsupported/unrepresentable labels, malformed JSON, missing recording end or incomplete data remain pending with an explanation; do not repair silently.
3. Keep the fixed template's recording-level Started/Ended metadata. Preserve segment millisecond bounds in the original JSON under its raw-input hash; do not add timestamps/synthetic IDs to speech or invent a timed template.
4. Bind preview to **raw-input hashes and the actual event-title-specific derived-output hash**; revalidate both at apply. Source edits invalidate approval. Leave original native Markdown/JSON unchanged and persist no adapter JSON.

## Readiness is not rename safety

All 32 logs had one completion record, `<UTC ISO timestamp> done — <integer> segments`, followed by archive/cleanup records: **completion need not be the last line**. Counts matched JSON segments; transcript `created_at` was close to second-precision completion time.

Validate completion against the current snapshot: matching segment count, completion no earlier than `created_at` at whole-second precision, and no later transcription restart. Missing, stale or mismatched evidence remains pending; an arbitrary old `done` line is insufficient.

Metadata `ended`, stop reasons (`call-ended`, `system-sleep`) and `on_stop_hook = fired` corroborate transcript production, not released handles. No audited package had `.transcribing.json`. The marker alone proves neither active processing nor idle state.

An approved, unchanged supported snapshot can be imported while its package stays untouched. No universal producer-owned idle/release contract was established: requested renames require the separate [release protocol](native-release.md), never completion alone or fabricated normalized `processing = idle` metadata.

## Native OBS timing evidence

Read-only `ffprobe` checks of three timestamp-named MOVs found aware UTC container `creation_time` and finite duration. Creation time plausibly represented recording start and differed from the filename clock by three hours; neither interpretation is universal.

Use container timing only as labeled candidate evidence. Require an explicit match when interval overlap is the only evidence. Never hard-code that offset, substitute filesystem mtime or infer OBS stopped writing. Missing/invalid probe data stays unknown. Release additionally requires known readable container structure and positive finite duration, per [native-release.md](native-release.md#evidence-bundle).

Probe metadata only: no frame/audio decoding or report writes. Interface reference: [official ffprobe documentation](https://ffmpeg.org/ffprobe.html) (`-show_entries`, `format_tags`, JSON writer).
