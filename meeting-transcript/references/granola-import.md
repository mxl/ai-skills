# Granola acquisition and unified import

Read for the generic three-source handoff, a Granola refresh within synchronization, or an acquisition failure. An explicitly selected local transcript needs no refresh; standalone `$granola fetch` follows its own range workflow without calendar-import setup.

## 1. Resolve scope and export root

Load `granola` and use its existing fetcher, not a replacement API client. It owns app-modification disclosures, patch/bulk confirmations, drift re-verification and cleanup. `--yes` requires approval of the whole operation; the configured default does not waive future confirmations. Development/tests use mocked acquisition, not the live app.

For synchronization, first resolve the project's calendar, timezone, destination and explicit initial start or saved successful boundary. Include unresolved older event intervals and suppress declines. Capture one aware end and reuse it as every preview's `--until`.

Export-root precedence:

1. Explicit user path.
2. Selected configured Granola source path.
3. `GRANOLA_EXPORT_DIR`.
4. `<project>/.granola/`.

Resolve it to an absolute path and use it for both fetch `--out` and preview `--granola-dir`. The additive preview flag preserves OBS/Amanu roots, including older two-source project configurations; a selected catalog source with `type = "granola"` also works. Clarify conflicting roots rather than choosing extra directories, writing configuration or inventing calendar IDs.

## 2. Fetch a covering date range

Granola filters the unconverted date prefix of document `created_at`, not meeting timestamps. For timezone-boundary coverage:

- Convert the earliest needed instant—including older pending work—and fixed end to UTC.
- Set FROM to the earliest UTC date minus one day.
- Set exclusive TO to the end's UTC date plus two days.

Padding covers adjacent timezone dates only: document creation can be far from meeting start. A complete document query is not proof of exhaustive meeting-time coverage. If an expected meeting is absent, inspect scope and ask for an approved wider fetch. Use actual calendar evidence and exact synchronization scope afterward; extra exports are not automatic imports. Avoid API title filters that could hide project meetings.

Run from the active project with resolved dates:

```sh
python3 <granola-skill-dir>/scripts/granola_fetch.py \
  --from FROM_DATE --to EXCLUSIVE_TO_DATE \
  --out /absolute/export-root --strict-completion
```

The fetcher creates the export root and writes canonical Markdown. Keep `--strict-completion`: legacy success may conceal individual request failures. Empty/missing transcript content means not ready, not a failed API request; leave corresponding work pending.

## 3. Gate on acquisition status

| Result | Next action |
|---|---|
| Strict acquisition and cleanup complete | Preview with the same export root and fixed cutoff. |
| Missing app, authentication/API error, cancelled confirmation, drift, saturated listing or failed restoration | Stop with the reason, or produce a diagnostic preview with `--source-acquisition-incomplete granola`. |
| Valid empty query / missing transcript | Report it distinctly; review calendar work still lacking a ready transcript. |

An incomplete plan cannot apply or advance the boundary. Re-fetch successfully and regenerate the full preview before approval. Stale local exports, force flags or a separately approved direct-file import cannot turn failed three-source acquisition into a complete synchronization.

## 4. Hand off to unified review

Return to [calendar-sync.md](calendar-sync.md#3-discover-and-review) and use [calendar-cli.md](calendar-cli.md) for preview/apply. Validate canonical metadata/times/content and preserve frontmatter, heuristic speaker provenance and original Granola filenames/content. Document IDs and filenames are not calendar IDs.

The shared workflow owns event matching, competing-transcript choices, single-import/all-source associations, pending retries and exact-plan approval. Input/output hashes and the discovery inventory are rechecked at apply; new exports require a fresh preview. Granola renames remain blocked even with idle attestation; only OBS/Amanu use the recording-rename protocol.

Report acquisition status, range/root and any incomplete or not-ready work with the shared import results. Offer summarization separately.
