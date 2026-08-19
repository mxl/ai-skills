# Artifact Workflows

Use this reference for every therapy-history read or artifact write.

## Root Resolution

`THERAPY_DIR` is required for file-backed behavior.

- Absolute value: use it directly.
- Relative value: resolve it from the active workspace root.
- Normalize `.` and `..` segments before use.
- Require the root directory to exist.
- Do not guess a fallback path.
- Do not read therapy history or write artifacts outside the resolved root.
- If configuration is invalid, report the issue and do not mutate files.

Derived paths:

```text
${THERAPY_DIR}/session-context.md
${THERAPY_DIR}/safety-plan.md
${THERAPY_DIR}/prompts/
${THERAPY_DIR}/sessions/
${THERAPY_DIR}/transcripts/
${THERAPY_DIR}/summaries/
${THERAPY_DIR}/patterns/
```

Create a missing `sessions/`, `transcripts/`, `summaries/`, or `patterns/` directory only for an explicit save operation and only after validating the root.

## Authoritative Templates

Use these workspace templates when present:

```text
Templates/therapy-transcript.md
Templates/therapy-session.md
Templates/therapy-summary.md
Templates/therapy-pattern.md
```

If a template is unavailable, preserve the same frontmatter and heading schema described below. Do not modify templates merely to complete one save operation.

## General Markdown Rules

- Preserve valid Obsidian Markdown, YAML frontmatter, aliases, tags, headings, callouts, embeds, wikilinks, block IDs, and Markdown links.
- Preserve an existing `created:` value. Refresh `updated:` after a material edit.
- Use ISO dates as `YYYY-MM-DD`.
- Keep `confidentiality: private`.
- Do not invent ratings, quotations, session numbers, dates, events, diagnoses, or outcomes.
- Keep generated interpretations visibly tentative.
- Use wikilinks without exposing absolute filesystem paths in note content.
- Avoid rewriting unrelated sections when updating an existing note.

## Read-Only Integrity

For `start`, unsaved `prepare`, unsaved `debrief`, unsaved `review`, unsaved `pattern`, `privacy`, and `help`, do not invoke file-editing tools for therapy artifacts.

For file-backed read-only routes, capture the relevant therapy-tree file list and content hashes before the operation when practical, then compare them afterward. If a file was added, removed, or changed unexpectedly, do not claim the route was read-only. Report the unexpected change and stop. Do not revert a concurrent user change.

## Close A Session

Choose the next available three-digit session number for the date by inspecting filenames, not note content.

```text
sessions/YYYY-MM-DD-session-NNN.md
transcripts/YYYY-MM-DD-session-NNN-transcript.md
```

Transcript frontmatter:

```yaml
type: therapy-transcript
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags: [therapy, transcript]
aliases: []
confidentiality: private
session: "[[YYYY-MM-DD-session-NNN]]"
```

Record the dialogue faithfully. Repeat `### User` and `### AI` headings for each turn. Do not improve, summarize, or reinterpret the raw transcript.

Session frontmatter:

```yaml
type: therapy-session
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags: [therapy, ai-session]
aliases: []
confidentiality: private
session_number: "NNN"
transcript: "[[YYYY-MM-DD-session-NNN-transcript]]"
mood_before:
mood_after:
```

Populate mood fields only from explicit user ratings. Use these headings:

```markdown
## Intention
## Summary
## Themes
## Emotional Signals
## Cognitive Patterns
## Working Hypotheses
## Helpful Reframes
## Actions / Experiments
## Follow-Up Questions
## Links
```

In `Links`, include the transcript wikilink. Do not create a pattern note during close unless explicitly requested.

Before reporting success, validate:

- the chosen date-scoped session number is collision-free;
- exactly one transcript and one matching session note were created or updated;
- required frontmatter and headings are present;
- mood fields contain only explicit user ratings;
- transcript and session links are reciprocal;
- no unrelated therapy file changed.

## Prepare And Debrief Saves

With `--save`, create a session-style note under `sessions/` with a collision-safe filename:

```text
YYYY-MM-DD-therapy-prepare-<short-slug>.md
YYYY-MM-DD-therapy-debrief-<short-slug>.md
```

Use only headings that fit the content, while preserving private frontmatter. Do not fabricate a professional's statements or conclusions.

## Review And Synthesis

Resolve the requested date range from filenames or frontmatter. If no period is supplied, use the last 30 days.

Without `--save`, return:

```markdown
## Recent Themes
## Open Loops
## Experiment Outcomes
## Possible Next Focus
## Notes To Revisit
```

With `--save`, use the summary template and record every reviewed session as a wikilink. Prefer names such as:

```text
summaries/YYYY-Www-weekly-summary.md
summaries/YYYY-MM-monthly-summary.md
summaries/YYYY-qN-therapy-summary.md
summaries/YYYY-MM-DD-ad-hoc-summary.md
```

Update a matching period note rather than creating a duplicate. Preserve `created:` and refresh `updated:`.

Because the existing summary template has no provenance heading, append:

```markdown
## Provenance

- Sources: structured session notes and summaries reviewed for [period].
- Raw transcripts: not reviewed, or list the explicitly reviewed transcript.
- Limitations: note missing sessions, uncertain dates, or other material gaps.
```

Keep this section factual and concise. Never claim to have reviewed a source that was not opened.

Before reporting success, validate the period, expected filename, summary schema, source links, preserved `created:`, refreshed `updated:`, and absence of unrelated mutations.

## Pattern Notes

Use a short descriptive slug:

```text
patterns/<pattern-slug>.md
```

Use the pattern template headings. Evidence must cite related session or summary wikilinks. Include counterexamples. A durable pattern requires multiple observations or explicit user confirmation. If evidence is thin, keep it as a chat draft or mark it as an early hypothesis.

Before reporting a saved pattern, validate the filename, schema, evidence and counterexample sections, related links, review date, and absence of unrelated mutations.

## Privacy Review

Privacy review is read-only unless the user separately requests a specific edit. Check:

- identifying people, employers, clinicians, addresses, dates, or locations;
- raw detail that is unnecessary for the intended recipient;
- credentials or secrets, which must not remain in the vault;
- whether quotations and claims preserve provenance;
- whether Git history may retain removed content;
- whether the destination and recipient are trusted;
- whether a summary or redacted excerpt would meet the purpose with less exposure.

Never export, send, publish, commit, or push as an implicit next step.
