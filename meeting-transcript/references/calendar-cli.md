# Calendar workflow CLI and adapter contracts

Read [calendar-sync.md](calendar-sync.md) first for authority, setup, source safety, and pending work. Calendar commands require Python 3.11+ on macOS or Linux; calendar apply is not supported on Windows. Linux exclusive-rename behavior must be validated on the target host; implementation tests were run on macOS.

## Preview

```sh
python3 <skill-dir>/scripts/meeting_transcript.py calendar preview \
  --project /absolute/project \
  --global-config /absolute/global.toml \
  --calendar /provider-supplied/normalized-calendar.json \
  --since 2026-08-01T00:00:00+03:00 \
  > /review-location/plan.md
```

The script's preview writes no files. The caller may explicitly save stdout as a Markdown plan outside recording source directories. Keep that exact reviewed document for apply; do not edit its machine-readable block to change behavior.

- `--global-config` defaults to `$XDG_CONFIG_HOME/meeting-transcript/config.toml`, or `~/.config/meeting-transcript/config.toml`.
- `--since` is required for the first period; subsequent periods use project state.
- `--until` optionally supplies a historical cutoff. Otherwise UTC now is captured before discovery. Future cutoffs are rejected.
- Timestamps require explicit timezone offsets.

One-run overrides do not persist into configuration:

```text
--provider PROVIDER
--calendar-id ID              repeatable, replaces configured calendar selection
--timezone IANA_ZONE
--destination RELATIVE_PATH
--source TYPE=/absolute/root repeatable, replaces configured discovery roots
```

Source types are `obs` or `amanu`. These workflow overrides refer to source directories; an explicit canonical transcript file continues to use the existing direct `import SOURCE --out DIRECTORY` command.

Choices use IDs printed in preview and may be repeated:

```text
--match ARTIFACT_ID=EVENT_KEY
--transcript EVENT_KEY=TRANSCRIPT_ID
--select EVENT_KEY
--decline EVENT_KEY
--rename ARTIFACT_ID
--idle-source ARTIFACT_ID
--accept-update EVENT_KEY
```

Generate a new preview with the user's choices, then present that exact resulting plan for approval.

Without `--select`, all visible meetings are selected; applying that plan therefore requires approval of the displayed full batch. Unselected meetings remain pending rather than disappearing behind the watermark. Explicit declines are persisted and suppressed later.

`--idle-source` is an operator attestation. Ask the user to establish that the recorder/transcriber is idle; never manufacture attestation from file timestamps, marker absence, or an import request. Validated idle metadata and this independent attestation are both required for source renames. A pending or blocked rename remains unchanged and must be reported.

`--accept-update` accepts the displayed changed calendar values for a known event. It does not by itself authorize renaming; ordinary rename selection and safety requirements still apply. Canonical transcript content is not rewritten to follow a calendar edit.

## Apply

Only after explicit human approval of the exact plan, calculate its SHA-256 and pass it to apply:

```sh
python3 <skill-dir>/scripts/meeting_transcript.py calendar apply \
  --plan /absolute/plan.md \
  --approve EXACT_SHA256_OF_PLAN_BYTES \
  --calendar /provider-supplied/fresh-normalized-calendar.json
```

If the plan requests eligible source renames, also pass `--allow-source-renames` after obtaining rename permission. A digest or flag is a technical binding, not proof of human consent. Do not approve a plan on the user's behalf.

Apply verifies fresh calendar data, configuration, state, and source fingerprints. Changed data requires a fresh preview and approval. Canonical transcript destination conflicts and source rename collisions fail closed instead of overwriting existing artifacts. Rename collisions are blocked rather than silently assigned suffixes.

Canonical imported transcripts use stable event-key directories under the configured destination. Source names remain human-readable calendar-derived names; the event key is an identity, not a transcript segment ID.

## Normalized calendar input

Calendar authorization and acquisition remain with the selected provider integration, not this script. The input is an external provider response normalized into this envelope; it is not a new persisted transcript source, adapter cache, or workflow JSON artifact. Use the provider's existing response handoff; do not archive temporary calendar JSON in the project.

```json
{
  "version": 1,
  "provider": "selected-provider",
  "events": [
    {
      "calendar_id": "explicit-calendar-id",
      "event_id": "stable-provider-event-id",
      "recurrence_id": "",
      "title": "Planning",
      "start": "2026-08-01T09:00:00Z",
      "end": "2026-08-01T10:00:00Z"
    }
  ]
}
```

For recurring events include `"recurring": true` and a stable nonempty provider instance identifier as `recurrence_id`. Identity includes provider, calendar, event, and recurrence instance, never title or scheduled time. Duplicate identities and invalid, naive, or reversed intervals are rejected.

Every run must include fresh data for all known non-declined events as well as the requested new period, with all provider pages retrieved. The apply handoff must cover the same scope and reflect new events too. Missing known events require retrieval or manual reconciliation. Deleted/cancelled known events without a refreshable record currently require manual reconciliation; cancellation-specific provider normalization is not implemented.

An incomplete fetch must not be presented as a complete envelope. The script cannot attest that the provider's caller fetched all pages, and an empty envelope is not evidence that authentication or acquisition succeeded.

## Tested normalized artifact contract — not a native Amanu contract

The current tested adapter recognizes explicitly normalized metadata:

- OBS: a sibling named `<recording-filename>.meeting.toml`.
- Amanu: `meeting-import.toml` inside the top-level package.

```toml
schema = "meeting-transcript-artifact-v1"
title = "Planning"
started_at = "2026-08-01T09:01:00Z"
ended_at = "2026-08-01T09:59:00Z"
processing = "idle"
transcript = "transcript.md"

[event]
calendar_id = "explicit-calendar-id"
event_id = "stable-provider-event-id"
recurrence_id = ""
```

The optional transcript field is supported only inside Amanu packages. Its path must be package-relative and refer to an existing **canonical** meeting-transcript Markdown document. The script validates that document and uses the existing importer. It does not convert unverified native Amanu JSON/plain-Markdown formats.

The optional event table supports exact event identity matching. Otherwise automatic association requires exact case-insensitive metadata title plus intersecting timezone-aware recording/event intervals. Filename-title evidence alone requires an explicit match. No nearest-event scoring or undocumented time tolerance is used. Identical transcript bytes are deduplicated; different ready transcripts require selection.

Unknown, malformed, unreadable, or native-only metadata remains unverified/pending. Do not create normalized metadata in real source directories to bypass verification or manufacture idle evidence. The tested normalized schema does not demonstrate that real Amanu exports conform to it.

OBS normalized sidecars are not renamed with videos. The project state preserves their original metadata paths; these files are an external adapter input, not a source file the workflow creates automatically.

## Recovery

Project operational documents are machine-readable Markdown with versioned fenced TOML:

- `.meeting-transcript/state.md`: successful boundary and per-meeting progress.
- `.meeting-transcript/journal.md`: durable incomplete-transaction intents, removed on successful completion.
- `.meeting-transcript/workflow.lock`: kernel-backed project writer exclusion. File existence alone does not mean a process holds the lock.

After interruption, either replay the same approved plan with fresh calendar input or generate a new read-only preview that reconciles journaled outcomes. A different new plan requires approval. Recovery proves outputs through recorded hashes and source identities; unverifiable outcomes require manual reconciliation, not blind replay. Do not delete the journal or edit state just to bypass an error.

Recovery tests cover process interruption and injected operation failures, not sudden power loss or filesystem corruption. An interrupted run leaves the old boundary intact until completion. Some individual operations may already have succeeded and are recognized on retry. The project lock does not establish that recording producers are idle or make independent projects safe to mutate shared sources simultaneously.
