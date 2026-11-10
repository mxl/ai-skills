# Granola acquisition and unified import

Read this for a generic meeting import, an explicitly requested Granola refresh, or a Granola acquisition failure. Importing an explicitly selected local transcript does not require a refresh.

## Acquisition authority and setup

A generic “импортируй встречи” request selects the three-source workflow: new Granola acquisition plus OBS/Amanu discovery. Load the `granola` skill before acquisition and use its existing fetch script, not a new API client. Explain that the fetch may temporarily patch/restart Granola and retain its patch, bulk-fetch and version-drift confirmation gates. Use `--yes` only after the user has approved the whole operation; selecting the three-source default is not a blanket permission to suppress future app-modification confirmations. A development request does not authorize a real fetch.

For the calendar-linked handoff, resolve the project's actual calendar binding, timezone, import destination, first start or saved boundary, and fixed period end before fetching. A standalone `$granola fetch` keeps its own date-range workflow and does not require calendar-import project configuration. Include unresolved older events in the acquisition scope; fetching only from the latest successful boundary can otherwise strand a pending Granola transcript forever. Explicit declines remain suppressed.

Resolve one export root in this order:

1. A user-selected export path.
2. An explicitly configured selected Granola source path.
3. `GRANOLA_EXPORT_DIR` when set.
4. `<project>/.granola/`, the Granola skill's documented default.

Pass the same absolute path to fetch `--out` and preview `--granola-dir`. This additive preview option preserves the configured OBS/Amanu roots, including older project configurations that list only those two. A catalog source with `type = "granola"` is also supported. Conflicting roots require clarification instead of scanning arbitrary extra directories. Do not silently write project configuration or invent calendar identifiers.

## Date-range handoff

Capture one timezone-aware synchronization end and reuse it with every preview's `--until`; use the saved successful start or the explicitly selected initial start. Include earlier pending event intervals too.

Granola's `--from`/`--to` interface uses date strings and filters the date prefix of document `created_at`; it does not convert those timestamps into the project's timezone. Request a covering date range rather than pretending it is an exact timestamp filter: convert the earliest needed instant and fixed end to UTC, use the earliest UTC date minus one day as FROM, and the end's UTC date plus two days as exclusive TO. This includes adjacent timezone dates, not arbitrary differences between document creation and meeting time. `created_at` may be far from the meeting's start: a successful date-prefix fetch proves completion of that document query, not exhaustive meeting-time coverage. If an expected meeting is absent, inspect the reported scope and ask for an approved wider acquisition range rather than treating the meeting as nonexistent. Filter the resulting local exports against actual calendar-event evidence and exact synchronization scope during unified preview. Extra fetched documents are not automatic imports. Do not add an API title filter that could hide meetings from one project calendar.

Run from the active project:

```sh
python3 <granola-skill-dir>/scripts/granola_fetch.py \
  --from FROM_DATE --to EXCLUSIVE_TO_DATE \
  --out /absolute/export-root \
  --strict-completion
```

The placeholders are resolved dates, not literal arguments. The fetch script creates its export directory, writes canonical Markdown only, and restores app changes through its existing cleanup flow. Do not call this script during skill tests; mock the acquisition boundary.

## Complete versus incomplete acquisition

Use `--strict-completion`: legacy exit status alone is insufficient, because individual transcript request failures previously still returned success. Strict mode refuses to attest completeness when listing reaches its current unpaginated limit or a document/transcript request fails. Empty/missing transcript content is different from an API failure: report it as not ready and leave corresponding calendar work pending.

A missing app, authentication failure, cancelled confirmation, version drift, API error, saturated listing or failed restoration is incomplete acquisition, not an empty source. Do not patch around a drift error, fall back silently to stale exports, or claim all three sources were checked successfully.

On an incomplete fetch, either stop with an actionable explanation or show a read-only preview marked `--source-acquisition-incomplete granola`. Such a plan is diagnostic only: apply must refuse it and preserve the synchronization boundary. Re-fetch successfully and regenerate the full preview before requesting apply approval. Independently approved explicit local-file imports remain a separate workflow, not a way to label this synchronization complete.

## Unified review

After successful acquisition, use the existing calendar preview command with `--granola-dir /absolute/export-root` and the same fixed `--until`. The source is canonical Markdown: validate template metadata, source times and transcript content. Preserve its existing frontmatter and supported speaker-label provenance. Filenames and document IDs are not calendar event IDs.

Group confirmed Granola, OBS and Amanu artifacts by the actual calendar event. Time proximity alone requires an explicit match; do not assign the nearest event. Deduplicate byte-identical canonical candidates. Ask which transcript to use when Granola and Amanu produce different ready candidates; do not silently rank, concatenate or merge them.

One event produces one canonical destination regardless of how many sources exist. Persist all source associations and retry pending work without duplicating already imported meetings. Source reads and final output are approval/hash-bound and revalidated at apply.

Granola exports are immutable workflow inputs: keep their original names and contents. Source renaming applies only to OBS videos and Amanu packages under their existing safety protocol. A Granola rename request is blocked, even when an idle attestation is present.

## Completion

Report Granola acquisition status, range and export root, OBS/Amanu discovery status, source choices, imported/unchanged/pending/declined meetings and the successful period boundary. Clearly distinguish a valid empty result from an inaccessible or incomplete source. Offer summarization separately; neither fetching nor import starts summarization automatically.
