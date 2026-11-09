# Calendar-linked meeting import: implementation status

Status: partial; native Amanu verification blocked

## Delivered behavior

- Public calendar preview/apply workflow extends meeting-transcript; explicit canonical import and summary preparation remain available.
- Approved nested global/project TOML settings, with one-run overrides and validation.
- Read-only discovery, explicit matching/transcript choices, safe review-bound apply, grouped normalized sources, pending/declined tracking, incremental boundaries, update proposals, project locking, and journal-based interruption recovery.
- Normalized calendar input is supplied by an authenticated provider integration; the workflow neither selects a project calendar nor handles credentials itself.
- Global source configuration has been created in the user's standard meeting-transcript configuration directory with the approved OBS/Amanu locations. Project calendar settings have not been guessed or created.

## Scope qualification

The tested source adapter uses explicitly normalized metadata plus an existing canonical transcript. It is NOT a verified native Amanu adapter. Native metadata and transcript conversion remain incomplete under tickets 02 and 03. Real source metadata has not been synthesized or written to bypass this limitation.

No actual meeting imports, source renames, source deletion, transcription, summarization, or calendar mutations were performed during development.

## External blocker

Reading the actual sample Amanu metadata and transcripts returns Resource deadlock avoided. Filesystem stat reports compressed,dataless. The user must make one complete representative export locally readable, for example by downloading the folder in Finder. Then verify the native schema, timezone semantics, transcript structure, and processing status read-only before implementing the native adapter and claiming compatibility.

Actual project calendar identifiers, provider, timezone, and initial period must be explicitly selected at project setup; examples are not chosen values.

## Verification record

Behavioral tests use the public subprocess CLI with synthetic calendar responses and temporary recording packages, not personal meeting contents. Review is measured against the pre-implementation base 743b73a4a57fe4ef204519f214d518d6a61c22be and the local specification.

Final verification on the development macOS host:

```text
python3 -m unittest discover -s meeting-transcript/tests -v
Ran 50 tests in 11.458s
OK
```

The suite comprises 41 public calendar-workflow tests and 9 existing canonical import/summary tests. Python compilation of both scripts and both test modules passed. Evaluation JSON and documentation links were validated; this is not a claim that agent eval scenarios were executed.

The 128 MiB sparse-recording behavioral test verifies bounded process memory with streamed hashing. Race tests cover fresh calendar additions, conflicting destination files, replacement of approved plan paths, changing processing metadata, symlink locks, and destination-directory moves before/after publication. Process interruption and concurrent-writer tests pass. Power-loss durability and Linux execution have not been verified.

Ticket outcomes: 01 and 04–07 are resolved for the supported normalized source contract. 02 awaits a locally readable native Amanu export; 03 retains the remaining native conversion work. These remaining items prevent claiming full production readiness for the actual Amanu corpus.

## Review axes

### Standards

Initial review exposed stale readiness checks, exact-plan-byte parsing, malformed-input handling, and symlink lock issues. Follow-up review found a publication-directory replacement race. All were fixed with behavioral regressions. The final independent Standards re-review found no remaining P1/P2 actionable findings and independently confirmed the directory-replacement reproduction now fails safely (exit 2, empty external directory, no state advancement, staging cleaned).

### Spec

Initial review exposed mismatched settings schema, lost newly added calendar events, and canonical destination publication races. The Spec follow-up confirmed all five original implementation findings fixed with dedicated regressions and no new critical regression identified. Native Amanu remains an explicitly incomplete external dependency rather than a test-backed completion claim.
