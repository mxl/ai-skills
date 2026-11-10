# Calendar workflow CLI and adapter contracts

Read [calendar-sync.md](calendar-sync.md) first for authority, setup and synchronization scope. Commands require Python 3.11+ on macOS/Linux; apply is unsupported on Windows. Tests ran on macOS: validate Linux exclusive-rename behavior on the target host.

## Project setup

After resolving the project for its first import, run this idempotent setup before acquisition:

```sh
python3 <skill-dir>/scripts/meeting_transcript.py calendar setup \
  --project /absolute/project
```

Setup automatically adds `/.granola/` and `/.meeting-transcript/meeting-transcript.lock` to the project's `.gitignore`, preserving existing bytes and skipping already-effective rules. It needs neither calendar credentials nor configuration and creates no exports, transcripts, state or lock file. Unsafe `.gitignore` symlinks/nonregular files are refused. It does not untrack files already in Git or ignore the entire `.meeting-transcript/` directory: state and reviewed plans remain versionable. A custom in-project Granola export path needs its own separately scoped ignore rule.

An import request includes this setup step; an explicitly read-only preview or a development task does not authorize modifying the user's `.gitignore`. `preview` itself remains read-only. Approved `apply` also ensures these rules before journal/import writes as a fallback for direct CLI callers.

## Preview

```sh
python3 <skill-dir>/scripts/meeting_transcript.py calendar preview \
  --project /absolute/project \
  --global-config /absolute/global.toml \
  --calendar /provider-supplied/normalized-calendar.json \
  --since START_WITH_OFFSET --until FIXED_END_WITH_OFFSET \
  > /review-location/plan.md
```

Replace timestamp placeholders with resolved aware values. First use requires `--since`; later runs use state. Reuse the acquisition cutoff with `--until`; otherwise preview captures UTC now before discovery. Future cutoffs and naive timestamps are rejected.

Preview writes no files. The caller may save stdout as Markdown outside recording roots. Keep the exact reviewed document; regenerate preview to change choices, rather than editing its machine-readable block.

`--global-config` defaults to `$XDG_CONFIG_HOME/meeting-transcript/config.toml`, falling back to `~/.config/meeting-transcript/config.toml`. Other one-run overrides:

| Option | Meaning |
|---|---|
| `--provider`, `--timezone`, `--destination` | Override configured values; do not persist. |
| `--calendar-id ID` | Repeatable; replaces calendar selection. |
| `--source TYPE=/absolute/root` | Repeatable; replaces discovery roots. Types: `obs`, `amanu`, `granola`. |
| `--granola-dir /absolute/root` | Adds Granola while retaining recording roots. Use the root resolved by [granola-import.md](granola-import.md). |
| `--source-acquisition-incomplete TYPE` | Repeatable supported source type; diagnostic preview only. Apply/boundary advancement are blocked. |

A missing Granola root is diagnostic only when explicitly marked incomplete; preview creates no directory. Re-fetch successfully and regenerate the plan. Omitting the marker is not proof that the caller fetched Granola or every calendar page. An explicit canonical file uses direct `import SOURCE --out DIRECTORY`, not these directory overrides.

### Review choices

Use IDs from preview; options are repeatable:

```text
--match ARTIFACT_ID=EVENT_KEY
--transcript EVENT_KEY=TRANSCRIPT_ID
--select EVENT_KEY
--decline EVENT_KEY
--rename ARTIFACT_ID
--idle-source ARTIFACT_ID
--accept-update EVENT_KEY
```

Without `--select`, all visible meetings are selected: approval covers that full batch. Unselected work remains pending; declines persist. Generate the final preview with the user's choices and present its exact bytes for approval.

`--rename` plus independent user-backed `--idle-source` requests native release observation. The attestation covers the selected source's recorder/transcriber being idle and not restarting until completion; timestamps, marker absence or import intent cannot supply it. Read [native-release.md](native-release.md) before using this path. Normalized sources require validated idle metadata; native sources require calibrated observations. `release_proof` reports method/scope, timing, inventory fingerprint and aggregate results; only eligible proof clears `rename_blocked`. Apply repeats verification. Missing `lsof` or OBS `ffprobe` means unknown, not a force-safe route; create no synthetic sidecars.

`--accept-update` accepts displayed calendar changes for a known event. It neither authorizes renaming nor rewrites canonical transcript content.

## Apply

After explicit human approval, calculate SHA-256 of the exact plan bytes:

```sh
python3 <skill-dir>/scripts/meeting_transcript.py calendar apply \
  --plan /absolute/plan.md --approve EXACT_SHA256_OF_PLAN_BYTES \
  --calendar /provider-supplied/fresh-normalized-calendar.json
```

Add `--allow-source-renames` only after separate permission for eligible planned renames. Flags/hashes bind operations; they do not establish human consent.

Apply rechecks calendar, configuration, state, source fingerprints, derived output and discovery inventory. Changed inputs require a fresh preview/approval. Destination conflicts and rename collisions refuse rather than overwrite or invent suffixes. Report blocked renames explicitly.

Canonical destinations use stable event-key directories under the configured destination; source names use reviewed calendar values. Event keys are identities, not transcript segment IDs.

## Normalized calendar input

Calendar integration owns authorization and acquisition. Normalize its read-only response into this external handoff, not a persisted transcript/cache artifact; do not archive temporary calendar JSON in the project.

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

Recurring events also need `"recurring": true` and a stable nonempty provider instance ID in `recurrence_id`. Identity comprises provider/calendar/event/instance, not title/time. Duplicate identities, invalid/naive times and reversed intervals are rejected.

Both preview and apply need all pages for the new period and fresh known non-declined events, including newly added events and older unresolved work. Missing/deleted/cancelled known events without refreshable data require retrieval or manual reconciliation; cancellation-specific normalization is not implemented. The script cannot attest caller pagination, and an empty envelope does not prove successful acquisition.

## Native recording sources

Read [native-recordings.md](native-recordings.md) for Amanu JSON conversion and OBS timing/structure evidence. Conversion stays in memory, preserves source content and revalidates raw/derived hashes. Native Markdown is not assumed canonical. Interval-only evidence needs an explicit match; completion does not prove source release. Rename eligibility follows [native-release.md](native-release.md), not guessed metadata or a user download.

## Granola canonical exports

A `granola` root contains top-level canonical Markdown from the Granola skill. Validate template metadata and aware source times; uncertain metadata/associations remain pending. Bind original input, canonical output and root discovery inventory: added, removed or replaced exports invalidate reviewed choices. Document IDs/filenames are not calendar IDs.

Calendar import keeps Granola files unchanged and blocks their rename requests. Different ready canonical candidates require selection; identical bytes deduplicate. One event produces one destination with all confirmed source associations.

## Normalized artifact contract

Supported external sidecars are `<recording-filename>.meeting.toml` beside OBS videos and `meeting-import.toml` inside Amanu packages:

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

The optional `transcript` is Amanu-only, package-relative and an existing canonical Markdown file. Native JSON has a separate adapter; unknown native formats/guessed Markdown parsers remain unsupported.

`[event]` supports exact identity matching. Without it, automatic association needs exact case-insensitive metadata title plus intersecting aware recording/event intervals. Filename titles and time-only evidence require explicit matching; no nearest-event score or undocumented tolerance. Unknown/unreadable metadata remains pending. Sidecars are external inputs, not files to manufacture for idle proof. OBS sidecars stay at their original paths when videos are renamed; state retains those paths.

## Recovery

| Project document | Role |
|---|---|
| `.meeting-transcript/state.md` | Successful boundary and per-meeting progress. |
| `.meeting-transcript/journal.md` | Durable incomplete-transaction intents; removed after success. |
| `.meeting-transcript/meeting-transcript.lock` | Kernel-backed writer exclusion; existence alone is not a held lock. |

Upgrade note: older versions used `.meeting-transcript/workflow.lock`. Stop all old import processes before upgrading; mixed old/new writers use different locks and are unsupported. The CLI never deletes or migrates the old file automatically. A leftover old lock is not an active-lock signal; do not remove or rename a held lock.

State/journal use versioned fenced TOML in Markdown. After interruption, replay the same approved plan with fresh calendar input or generate a new read-only preview reconciling journaled outcomes; a new plan needs approval. Prove outputs through recorded hashes/identities. Unverifiable outcomes require manual reconciliation, not journal deletion or state edits to bypass errors.

The old boundary remains until completion, even when individual operations already succeeded and are recognized on retry. Tests cover process interruption/injected failures, not power loss or filesystem corruption. Project locking neither establishes recorder idle state nor coordinates different projects mutating shared sources.
