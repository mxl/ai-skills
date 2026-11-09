# Issue tracker: Local Markdown

Issues and specs for this repository live as Markdown files in `.scratch/`. Publishing to the issue tracker means saving a local file; it does not authorize external publication.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`.
- The specification is `.scratch/<feature-slug>/spec.md`.
- Implementation issues are separate files in `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01`.
- Record state as a `Status:` line near the top of each specification or issue.
- Specifications published by `to-spec` use `Status: ready-for-agent`.
- Append discussion under a `## Comments` heading, preserving earlier history.

## Publish and fetch

When a skill says to publish, create the corresponding local Markdown file and its parent directory if needed. Preserve existing documents; update them deliberately rather than silently replacing unrelated work.

When a skill says to fetch a ticket, read the supplied path. Resolve an issue number within the named feature; ask for the feature if the number is ambiguous.

## Wayfinding operations

- Map: `.scratch/<effort>/map.md`, containing Notes, Decisions-so-far, and Fog.
- Child ticket: `.scratch/<effort>/issues/NN-<slug>.md` with the question in its body.
- Record ticket type in `Type:` (`research`, `prototype`, `grilling`, or `task`).
- Record dependencies in `Blocked by: NN, NN`. A ticket is unblocked when all listed tickets are `resolved`.
- The frontier consists of open, unblocked, unclaimed tickets; choose the lowest number first.
- Claim a ticket by saving `Status: claimed` before working on it.
- Resolve it by appending an `## Answer`, setting `Status: resolved`, and adding a summary and link to the map's Decisions-so-far section.
