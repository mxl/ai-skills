# Import review and user decisions

Read after discovery and before requesting approval of a recording import plan. This gate covers ambiguous OBS recordings, Amanu packages and Granola exports, missing transcripts, and original-artifact rename offers. A pending status is a tracking result, not a substitute for asking the user.

## 1. Make ambiguous sources recognizable

List every unresolved artifact in the conversation, using manageable batches for long lists; a linked plan may supplement, not replace, the questions. For each recording/package/export show:

- Original basename or path, source type, recording date/start/end/duration and timezone when known.
- A one-sentence meeting description and its evidence: calendar title/description/participants, source metadata, or a short inspection of an existing transcript. Label calendar-only descriptions as candidate descriptions, not identified recording content. This is an identification aid, not a summary cycle; create no `summary.md` and include no raw speech in workflow plans.
- Candidate calendar events with their dates/times and the evidence for/against each association. Name overlapping alternatives even when one looks more likely; time proximity alone is a proposal requiring a user choice.

If no content or informative metadata is available, say “topic unknown; no transcript” and show available timing/candidates. Do not invent a topic from the recorder/app name or silently run speech recognition to obtain a description.

Ask what to do with each item: choose a named candidate, supply/correct the meeting identity, explicitly defer for retry, or explicitly decline future import. Offer 2–4 concrete choices plus custom input where practical. For competing ready transcripts, describe the candidate sources and ask which one to import; preserve all confirmed source associations without merging different speech.

**Completion:** every ambiguous item has a user answer, an explicit decision to leave it unresolved, or is still awaiting an answer. If the user approves only ready work, keep unanswered items pending and visibly list them; do not describe them as reviewed or declined. A generic “import meetings” request is not permission to choose the nearest event.

## 2. Offer missing-transcript transcription

Proactively ask whether to transcribe recordings whose meeting has no usable transcript. Show the exact eligible recordings/packages and why a transcript is absent, unreadable, incomplete or near-empty. Distinguish technical inability to read a source from proof that it has no transcript.

First group confirmed sources by meeting: if Granola or Amanu already supplies a usable transcript, explain that its OBS video needs no extra transcription for this import. Offer replacement/additional transcription only when relevant or requested, rather than reprocessing every duplicate video.

Give scoped choices: transcribe selected listed recordings now, defer transcription and import ready meetings, or decline it. If timing/identity is ambiguous, transcription may be offered as a way to identify the meeting; the resulting content still needs association review. Obtain permission for the files, execution route and any external upload/cost before running it. Use the applicable transcription workflow (load `media-import` for audio/video acquisition and transcription); if no route is available, report the blocker and ask how to proceed.

After approved transcription, validate canonical output and regenerate the preview before exact-plan approval. Keep original recordings/packages unchanged; do not retrofit producer metadata or sidecars. A refusal or deferral leaves ready work available, and unresolved items retain explicit reasons. When no recording needs transcription, state that rather than inventing an offer.

**Completion:** the user has received the scoped offer and chosen its disposition, or the offer is visibly awaiting an answer. Reporting “OBS has no transcripts” only after import does not satisfy this step. Import approval alone does not authorize transcription.

## 3. Offer original-artifact renames

During import review, proactively offer to rename original OBS files and Amanu package folders using the calendar workflow's naming convention. Show a table of current path → proposed path for each applicable artifact, separately from the canonical transcript destination. If event identity or naming configuration is unresolved, show that blocker instead of inventing a final path; resolve it before generating the exact rename plan. Preserve OBS extensions and Amanu package internals. Granola export names/content stay unchanged.

Offer rename versus keep originals unchanged; honor a user's existing explicit no-rename scope without re-asking. Explain existing-name no-ops, unresolved matches, collisions and unavailable/busy sources instead of omitting them. Include artifacts with and without usable transcripts, but verify each against the supported release protocol: an offer is not a claim of eligibility. Where no original OBS/Amanu artifact is in scope, mark renaming not applicable.

Before requesting native rename verification, read [native-release.md](native-release.md). Ask separately for scoped recorder/transcriber idle/no-restart attestation and rename permission. Show verified eligibility and visibility limitations in the exact resulting plan; keep busy/unknown artifacts unchanged. A generic approval to import transcripts supplies neither rename permission nor idle attestation.

**Completion:** each proposed original rename is explicitly approved with all safety gates, explicitly declined, or awaiting a decision. Execution follows the calendar workflow's exact-plan approval and fresh apply checks, not an ad-hoc filesystem rename.

## 4. Close the review gate

Consolidate these questions into one review exchange where practical, with separate decisions for associations, transcription and original renames. If a choice changes sources or planned mutations, regenerate the CLI preview before approval. Ask for approval of the exact plan only after the user's choices are explicit; persist the reviewed Markdown outside source roots at a stable project location so retries can find it.

Report imported/unchanged meetings, unresolved items and their chosen disposition, transcription performed/deferred/declined, and original renames performed/kept/blocked. Do not claim “all meetings imported” while unmatched or transcriptless sources remain unresolved. Offer summarization separately.
