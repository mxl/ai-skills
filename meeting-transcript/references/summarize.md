# Current-agent summary cycle

Read this before preparing, analyzing, verifying or summarizing a meeting transcript. An explicit `$meeting summarize` authorizes one uninterrupted current-agent cycle, subject to the material-question gate below. Resolve an ambiguous meeting first; importing a transcript alone does not start this cycle.

## 1. Prepare and apply rules

```sh
python3 <skill-dir>/scripts/meeting_transcript.py summarize prepare \
  <meeting-folder>/transcript.md [--bundle DIR]
```

Preparation validates the canonical transcript and prints the prompt, summary template, ordered speaker-labelled transcript, output path, transcript hash and summarization-rules provenance. It writes no files.

Apply any project Markdown rules selected by `MEETING_TRANSCRIPT_SUMMARY_RULES` before transcript analysis. Rules may customize focus, terminology, structure and recurring conventions; they cannot override transcript immutability, evidence requirements or the prohibition on invention. Record their path and SHA-256 in `Summarization Rules`.

## 2. Establish evidence

Read relevant structured people/entity references, terminology, project decisions and directly related documents. Record every reference actually used in `Reference Sources` with absolute path and SHA-256.

Project context can confirm a unique identity, terminology, role or directly comparable attribute; it cannot prove that something was said. A unique structured-reference match may be reference-confirmed with its exact source. Fuzzy/contextual matches require user confirmation. Context cannot override a contradictory transcript attribution, role, date, amount, decision or contract detail without confirmation.

Inspect material recognition issues before drafting. A finding must affect an entity, fact, decision, action, attribution, date, amount, open question or link. Ignore filler, grammar, style and harmless recognition noise. Cite a short verbatim quotation and its speaker, without synthetic segment IDs; retain the original transcript unchanged.

## 3. Material-question gate

Build a findings checklist. Ask a consolidated batch whenever uncertainty or contradiction affects:

- a participant, person, role, entity or contract party;
- a decision, commitment, action, owner, recipient, signer or responsible team;
- a deadline, meeting date, amount, price or other numeric value;
- a link, system, vendor, legal entity or integration.

Include correction, attribution, entity-attribute and action-disposition questions where material. **Wait before writing `summary.md`.** The gate clears only when there are no material questions or the user answers, explicitly declines resolution, or confirms that an ambiguity should remain. Record answers in `User Resolutions`; unresolved findings need matching open questions. Listing `Open Questions` alone does not clear the gate. A direct summarize command does not waive it.

## 4. Draft and reconcile

Create a transcript-only draft in memory, then reconcile separately supplied notes/prior summaries and relevant project context:

| Claim | Treatment |
|---|---|
| Transcript-supported | May enter normal summary sections. |
| Contradicted | Record in Verification and, when actionable, Open Questions; apply the material-question gate. |
| Useful but absent from transcript | Label explicitly unconfirmed. |
| Decision or action | Require transcript support. |
| Unstated owner or deadline | Leave empty or unresolved. |

If reconciliation exposes a new material question, return to the gate before output.

## 5. Write and verify

After the gate clears, write `<meeting-folder>/summary.md` directly in the current agent using the supplied summary template. Include every applicable section, `[transcript.md](./transcript.md)`, reference/rules provenance and user resolutions. Use Markdown directly, without a JSON draft or render/apply phase. Keep the canonical transcript verbatim.

Suggest only durable improvements to project summarization rules. Ask whether to apply all, selected or none before editing the rules file. After an approved edit, recompute its hash and update `Summarization Rules` in `summary.md` directly.

Completion requires transcript-supported decisions/actions, evidence or explicit unresolved status for material findings, and provenance for every reference/rules file used.

## 6. Action-item handoff and report

Show the complete action-item table, then ask: **Create Todoist tasks: all / selected / skip?** Create tasks only with confirmation. Update task status and Todoist IDs/URLs directly in `summary.md`; mark declined items `skipped` and items the user says are complete `resolved`.

Report transcript/summary paths, material findings and unresolved items, contradictions and resolutions, entities and links, the action-item table and Todoist status, and any summarization-rule suggestions.
