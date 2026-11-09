# Calendar-linked meeting import: implementation status

Status: resolved within documented safety boundaries

## Delivered behavior

- Public calendar preview/apply workflow extends meeting-transcript; explicit canonical import and summary preparation remain available.
- Approved nested global/project TOML settings, one-run overrides, and setup validation.
- Read-only source discovery, explicit matching/transcript choices, review-bound apply, grouped sources, pending/declined tracking, incremental boundaries, calendar update proposals, project writer locking, and journal-based process-interruption recovery.
- Global source configuration exists in the user's standard meeting-transcript configuration directory with the approved OBS/Amanu locations. Project calendar settings have not been guessed or created.
- Normalized calendar input comes from the selected authenticated provider integration; the script does not handle credentials or mutate calendars.
- Native Amanu JSON and completion logs are validated and converted to canonical Markdown in memory. Original JSON/Markdown files and audio remain unchanged.
- Native OBS container timing is inspected using bounded metadata-only ffprobe when available. Interval-only evidence requires explicit matching; neither timestamps nor application names imply a calendar identity.

All seven local tickets are resolved. This does not authorize actual recording mutations or imply that all native sources can safely be renamed.

## Native source verification

The initial compressed,dataless / Resource deadlock avoided blocker was removed when the user downloaded the source files. Read-only inspection covered all 32 Amanu packages, 5,879 segments, speaker-map variants, completion records, and selected OBS metadata. See `native-audit.md` for aggregate evidence without speech or participant values.

Final independent read-only conversion with the implemented adapter:

```text
Native Amanu read-only conversion: 32/32 validated; no files written, no speech printed.
```

The adapter preserves exact text, labels, order and source recording times. Consecutive chunks merge only for exactly identical resolved speaker labels, joined by newline. Case-distinct labels are not merged. Unrepresentable source structures remain pending instead of being silently rewritten.

The fixed canonical template carries recording-level times. Original millisecond segment bounds remain in unchanged source JSON, bound by raw-input hashes; they are not inserted into speech as an unrequested timing representation.

Plans bind actual calendar-title-specific canonical output hashes as well as source-input hashes. Apply and prepublication checks regenerate and enforce the approved result. No canonical speech body or source JSON is embedded in persistent workflow plans, state, or journals.

## Safety boundaries and remaining runtime setup

- Native transcript completion is not source-idle proof. Native OBS/Amanu renames remain blocked where technical idle/release evidence is unknown, even with operator attestation. Validated normalized idle evidence plus explicit attestation and approved rename plan remain required for eligible renames. Do not manufacture sidecars in real sources to bypass this boundary.
- Actual project provider, calendar identifiers, timezone and initial period still require explicit setup before using calendar import in a project.
- Provider event acquisition must retrieve all pages and refresh known events; the script cannot prove that the caller supplied a complete provider response.
- Deleted/cancelled known events without a refreshable response require manual reconciliation.
- Tests cover process interruption, not sudden power loss or filesystem corruption. Linux exclusive-rename behavior has not been exercised on this macOS development host.

No actual meeting imports, source renames, source deletion, transcription, summarization, or calendar mutations were performed during development. Source inspection and conversion checks were read-only.

## Final verification

```text
python3 -m unittest discover -s meeting-transcript/tests -v
Ran 66 tests in 17.733s
OK
```

This includes 57 public calendar-workflow regressions and 9 existing canonical import/summary tests. Compilation of all three scripts and both test modules passed. Documentation links and evaluation JSON validated; agent eval scenarios themselves were not executed.

Coverage includes native schema variants, positive completion evidence, timing overshoot, exact labels/text, same-speaker grouping, source-input edits, actual output-hash equality, template changes before apply and publication, FIFO isolation, both-source grouping, no duplicate imports, pending retries, watermark failures, concurrent writers, source collisions, and destination/plan path replacement races.

## Review axes

### Standards

Initial workflow safety findings were fixed and reviewed before the first commit. Native follow-up against base cee5d60 found two P2 issues: unenforced actual converted-output hash and blocking reads of nonregular native inputs. Both were fixed with public regressions. Final independent Standards review confirmed both fixes and found no new concrete P1/P2 regressions.

### Spec

Native follow-up found one actionable derived-output binding issue, subsequently fixed and confirmed by independent follow-up. A proposed per-segment-timestamp finding was withdrawn because the agreed fixed template preserves recording-level time while the original JSON remains untouched and hashed. No remaining actionable P1/P2 findings were reported.
