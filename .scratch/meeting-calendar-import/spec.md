# Calendar-linked meeting import from OBS and Amanu

Status: ready-for-agent

## Problem Statement

Recordings are stored in shared OBS and Amanu source directories, independently of the projects to which their meetings belong. OBS contains top-level video files; Amanu contains meeting directories with recordings, transcripts, and technical artifacts. Names are inconsistent and may identify recording time rather than the calendar event.

The user needs a request to import meetings in a project to discover relevant recordings in both sources, match them to that project's calendar, propose meaningful names for source artifacts, and import transcripts using the existing meeting-transcript skill. Repeated imports must continue from a project-specific synchronization boundary without losing meetings whose recordings or transcripts arrive late.

## Solution

Extend meeting-transcript with on-demand, calendar-linked discovery. Global configuration defines shared recording sources and naming rules. Project configuration defines calendars, timezone, and import destination. Separate project state records the successfully processed period, meeting identities, source artifacts, imported transcripts, and unresolved work.

The agent presents calendar matches and a rename plan before changing original artifacts. Only approved renames are applied. Existing transcript import remains the canonical import mechanism; summarization and transcription are separate operations. Subsequent runs process the next period plus unresolved meetings from previous periods.

## User Stories

1. As a user, I want to configure shared OBS and Amanu sources globally so every project can discover recordings without repeating their locations.
2. As a user, I want discovery to run only when I request an import so recordings are not scanned or changed in the background.
3. As a user, I want an explicit source in my request to take precedence over configured source discovery so targeted imports remain possible.
4. As a project owner, I want to bind one or more calendars explicitly so meeting relevance is not guessed from a project name.
5. As a project owner, I want missing calendar configuration to trigger a setup question so imports do not use an arbitrary calendar.
6. As a project owner, I want to configure a timezone so artifact names are stable when the computer's timezone changes.
7. As a project owner, I want to configure an import destination relative to the project so transcript placement is predictable.
8. As a user, I want the first synchronization to ask for a starting date so historical scope is deliberate.
9. As a user, I want each run to capture its period end at startup so its scope remains fixed during execution.
10. As a user, I want unfinished calendar meetings deferred so recordings still being made are not treated as completed meetings.
11. As a user, I want subsequent imports to start at the last successfully processed period end so I do not repeatedly choose dates.
12. As a user, I want unresolved older meetings checked on later runs so late recordings or transcripts are not lost behind the synchronization boundary.
13. As a user, I want explicitly declined meetings remembered so they are not repeatedly proposed.
14. As a user, I want matching to consider available timestamps, recording intervals, names, and verified metadata so it is not based solely on the nearest calendar event.
15. As a user, I want ambiguous matches presented for clarification so the wrong meeting is not renamed or imported.
16. As a user, I want OBS and Amanu artifacts for one event grouped as one meeting so duplicate recordings do not cause duplicate imports.
17. As a user, I want a plan showing original paths, selected calendar events, matching evidence, and proposed names so I can review changes before approving them.
18. As a user, I want original OBS files and Amanu meeting directories renamed only after confirmation so source changes remain under my control.
19. As a user, I want artifact names to contain the calendar event's planned date, start time, and title so names reflect the scheduled meeting rather than recording startup.
20. As a user, I want file extensions and Amanu internal names preserved so existing recording packages remain structurally intact.
21. As a user, I want unsafe characters sanitized and destination collisions handled without overwriting existing artifacts so renaming cannot destroy another recording.
22. As a user, I want active or unverifiably safe artifacts left unchanged so import does not interfere with ongoing recording or transcription.
23. As a user, I want an available Amanu transcript to be the primary import candidate so existing transcription work is reused.
24. As a user, I want different ready transcripts for the same event presented for selection so content is not silently merged or discarded.
25. As a user, I want only one canonical transcript imported per selected meeting so repeated or multi-source discovery does not create duplicates.
26. As a user, I want missing transcripts recorded as pending and transcription offered separately so recognition is never launched implicitly.
27. As a user, I want other ready meetings imported even when one transcript is missing so incomplete artifacts do not block the whole batch.
28. As a user, I want transcript content preserved through canonical conversion and import so importing does not summarize or rewrite what was said.
29. As a user, I want summarization offered as a separate step so importing does not implicitly start LLM analysis.
30. As a user, I want failed or interrupted runs to preserve the previous synchronization boundary so unprocessed events remain discoverable.
31. As a user, I want a retry to recognize earlier successful imports and source renames so partial progress does not produce duplicates or broken source references.
32. As a user, I want later calendar title or time changes shown as update proposals so previously processed artifacts are not silently renamed.
33. As a project owner, I want configuration and synchronization state separate so settings remain understandable and progress can be inspected independently.
34. As a project owner, I want credentials excluded from these files so calendar authorization stays in the existing authenticated integration.
35. As a user, I want concurrent imports for the same project blocked so competing agents cannot corrupt synchronization state.

## Implementation Decisions

### Existing import integration

- Extend the existing meeting-transcript skill rather than introducing a separate user-facing importer.
- Preserve the explicit-source canonical import flow and its existing validation, atomic transcript write, and byte-identical no-op behavior.
- Add source discovery, calendar matching, rename planning, and incremental synchronization around that flow.
- Use a documented Amanu adapter to produce the canonical transcript input where necessary. Preserve source text, speaker labels, order, and available supported metadata; adapting structure does not authorize rewriting content.
- Import and summarize remain separate operations. Missing transcripts do not authorize transcription.

### Configuration and precedence

- Store global settings in a versioned TOML configuration under the user's standard configuration directory, namespaced for meeting-transcript.
- Configure two global sources: OBS at `/Users/michaelledin/Documents/Recordings/OBS` and Amanu at `/Users/michaelledin/Documents/Recordings/Amanu`. These are user-approved source locations, not portable defaults to hard-code for every installation.
- Store versioned project TOML configuration in a project-local meeting-transcript settings directory.
- Global settings contain source types, source locations, and common naming rules. Project settings contain calendar provider, explicit calendar identifiers, timezone, destination, and selected configured sources.
- Resolve applicable values in this order: explicit request parameters, project configuration, global configuration. A one-run override does not persist a configuration change without separate approval.
- Calendar provider, actual calendar identifiers, timezone, and first-period start must be selected during project setup. Google Calendar and Europe/Moscow were examples, not approved project values.
- Keep credentials in the calendar integration's existing authorization mechanism, not configuration or synchronization state.

### Synchronization state

- Store project synchronization state in a separate machine-readable Markdown document, consistent with the skill's existing restriction on persisted workflow JSON.
- Record the initial period start, successfully synchronized period end with timezone, stable event identity, source-artifact associations, current and original paths, imported transcript location, and unresolved or declined status.
- Use event identity and persisted artifact associations, not filenames alone, to recognize previously processed meetings. Recurrence and multiple configured calendars must not collapse distinct events into one identity; the precise provider-specific identity representation remains an implementation detail to verify.
- Update state atomically and block concurrent synchronization writers for the same project.
- Preserve successful per-meeting progress sufficiently to recover after partial failure while leaving the global synchronization boundary unchanged on failure or interruption.
- Advance the boundary only after the agreed batch completes successfully. Explicitly tracked unresolved meetings may remain pending without blocking ready meetings or losing their retry eligibility.

### Period selection

- Ask for the initial start date when no prior boundary exists.
- Capture the end of the period at run startup and defer events that have not ended.
- Start subsequent runs at the stored boundary and also retry unresolved earlier meetings. Boundary overlap must be harmless through identity-based deduplication.
- Remember explicitly declined meetings rather than proposing them on every run.

### Discovery and matching

- Perform discovery on demand, not in a background service.
- Inspect top-level OBS recordings and top-level Amanu meeting packages, using safe metadata inspection as needed.
- Treat a calendar event as a candidate, not proof that a corresponding recording exists.
- Consider available recording timestamps and intervals, names, and verified metadata. Reuse calendar event identifiers only if their actual availability and semantics are confirmed.
- Present matching evidence. Time proximity alone is insufficient for an unambiguous association.
- Ask about ambiguous matches; leave affected artifacts unchanged while unresolved.
- Group sources belonging to the same event into one logical meeting.
- Numeric matching tolerances and evidence weighting have not been agreed. Verify representative technical metadata before proposing a concrete implementation; do not silently treat an arbitrary nearest-event rule as accepted.

### Rename approval and safety

- Use the approved name format: calendar date, a space, start time with hours and minutes separated by a hyphen, a space, and event title. Render date as year-month-day and use the configured project timezone.
- Preserve OBS file extensions. Rename only the top-level Amanu meeting directory, preserving its internal filenames and structure.
- Before mutation, show original path, selected event, matching rationale, and proposed new name. Require explicit approval.
- Sanitize invalid filename characters. Preserve existing destination artifacts; collisions must not result in overwrite. The exact collision-resolution naming rule is not yet prescribed.
- Determine whether source artifacts can safely be renamed. If active processing cannot be ruled out, leave the artifact unchanged and explain the reason.
- The existence of a transcription marker alone does not establish that processing is currently active.
- Detect calendar title or time changes for imported meetings and present a separate update proposal rather than renaming automatically.

### Transcript selection and import

- Prefer a ready Amanu transcript as the primary candidate. Preserve OBS as an additional source association.
- If multiple different ready transcripts exist, ask the user to select rather than silently merging or choosing one.
- If no transcript is ready, retain a pending meeting and offer recognition separately. Continue with other approved ready meetings.
- Keep canonical meeting artifacts as Markdown and retain the existing summary evidence and permission boundaries.

## Testing Decisions

The user approved one primary behavioral test boundary: the public meeting-import workflow, exercised with controlled calendar events and temporary OBS/Amanu sources. Assert externally observable plans, approval gates, source changes, canonical imports, and persisted synchronization behavior rather than internal helper structure.

- Reuse existing canonical import, invalid-input no-write, content preservation, and unchanged-import tests as regression coverage.
- Reuse existing skill eval patterns for discovery, confirmation boundaries, and bulk import without automatic summarization.
- Exercise global/project configuration resolution and explicit-source precedence through the public workflow.
- Verify that missing calendar binding, timezone, or first-run start date prompts for the required decision instead of silently guessing.
- Verify the fixed period end, deferral of ongoing events, and harmless handling of events at a repeated boundary.
- Cover unique matches, missing recordings, ambiguous matches, misleading nearest-in-time events, and one event recorded in both sources.
- Verify that previewing a plan changes neither original artifacts nor imported transcripts, and that unapproved renames never execute.
- Verify calendar-derived names, timezone stability, extension preservation, untouched Amanu internals, sanitization, and non-destructive collision handling.
- Verify active or unverifiably safe artifacts are left unchanged, including a stale transcription-marker scenario.
- Cover ready Amanu transcript import, competing transcripts requiring selection, pending recognition without automatic execution, and continued import of other ready meetings.
- Assert exact transcript preservation after canonical adaptation and import.
- Verify that a successful batch advances the boundary and that unresolved older meetings are retried afterward.
- Verify that declined meetings stay suppressed and repeated imports do not duplicate meetings.
- Inject failures and interruptions after partial imports and renames. Verify that the boundary does not advance, previous artifacts remain intact, and retries recover associations and finish without duplicates.
- Verify concurrent imports for one project cannot both mutate state.
- Verify calendar title/time changes produce proposals rather than automatic mutations.
- Use synthetic fixtures rather than personal recordings or real calendars for automated mutation tests. Real-source validation remains read-only until explicit user approval.

## Out of Scope

- A new user-facing skill that replaces meeting-transcript.
- Background scanning, scheduling, or continuous synchronization.
- Automatic calendar creation, event editing, or guessing project calendar binding.
- Automatic recognition of recordings or automatic transcript summarization.
- Automatic Todoist creation or other follow-up actions.
- Changes to internal Amanu artifact names or contents, deletion of recordings, or overwrite of existing source artifacts.
- An unconditional archive-wide first import without an approved starting date.
- Storing calendar credentials in configuration or synchronization state.
- Creating or modifying calendar bindings for the UE project as part of writing this specification.
- External issue publication; this specification uses the repository's local Markdown tracker.

## Further Notes

- Discovery found 113 top-level OBS video files, with descriptive and timestamp-only names. Sampled Amanu packages contain an audio recording, Markdown and JSON transcripts, speaker metadata, processing logs, and metadata files. These inventory observations are not a verified adapter contract.
- Attempts to read Amanu metadata failed with `Resource deadlock avoided`. Timestamp semantics, explicit timezone, event-ID availability, and reliable active-processing detection remain unverified technical prerequisites.
- A transcription marker was observed, but its content and current relevance were not verified. Do not treat it as proof of active processing or as permission to rename a package.
- No project calendar binding was established during requirements gathering.
- The agreed configuration and state storage design is separate from implementation approval for mutating real recordings. Writing this spec authorizes neither source renames nor importing actual meetings.
- Repository engineering-skill setup is complete. This spec is published to the local Markdown issue tracker with status ready-for-agent; it does not itself implement the feature.
