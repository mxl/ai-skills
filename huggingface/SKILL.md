---
name: huggingface
description: Use this skill for hands-on Hugging Face Hub model work: repo/model IDs, HF model pages/cards, benchmark tables from model pages, `hf` CLI commands, `HF_HOME`, local Hub caches, disk usage, repo size checks, download planning, cache verification, cleanup, and Markdown transformations of HF repo lists. Trigger when the user wants to download models, build copy-pasteable shell loops, link repo IDs to huggingface.co pages, compare model-card metrics with provenance, estimate full or remaining download size, choose variants under storage limits, or audit whether cached repos are complete across one or more cache roots. Do not use for generic ML concepts, training/fine-tuning/LoRA, inference deployment, datasets, package installation, or benchmark discussion unless it is tied to specific Hugging Face repos/pages/caches.
license: MIT
compatibility: opencode
metadata:
  audience: agents
  domain: huggingface
---

# Hugging Face Model And Cache Workflow

Use this skill for Hugging Face model-page research and local cache management. The goal is reproducible answers about model size, quantization variants, benchmark provenance, and whether a model is already usable in one of the user's configured caches — plus safe, copy-pasteable download and cleanup commands.

This skill owns the *workflow* layer (selection, sizing, cache audits, download/cleanup planning, benchmark provenance). It does not try to re-document every `hf` flag, because the CLI evolves. When you need exact syntax, run `hf <command> --help` rather than trusting memorized flags.

## When To Use

Use this skill for requests like:

- "Which HF models under 45 GB have I not downloaded yet?"
- "Check the benchmark table on this Hugging Face model page"
- "Compare metrics from the GPT-OSS page and the Nemotron comparison table"
- "Generate `hf download` commands for these repos"
- "Work with multiple `HF_HOME` caches"
- "See if this model is complete in one or more cache homes"
- "Get the total size of a repo without downloading it"
- "Reclaim disk space from my Hugging Face cache"
- "Download only the safetensors, skip the fp16 shards"
- "Turn Hugging Face repo IDs into model-page links"

Do not use this skill for:

- generic ML benchmark interpretation without Hugging Face pages or repos;
- training, fine-tuning, LoRA setup, or inference-server deployment;
- Python package management unrelated to Hugging Face model caches;
- editing application code that merely happens to import `huggingface_hub`.

## CLI Baseline

The current CLI is `hf` (from `huggingface_hub`). The old `huggingface-cli` name is deprecated. Generate `huggingface-cli` commands only when the user explicitly asks for legacy syntax.

Two rules keep this skill from going stale:

1. Treat `hf <command> --help` as the source of truth for flags. If a flag here disagrees with `--help` on the user's machine, follow `--help`.
2. Prefer machine-readable output. Most `hf` commands accept `--format json` (or `--json`); use it instead of regex-parsing human output whenever you need to compute or filter.

If the user already installed the official `hf` CLI skill, this skill stays complementary: it adds selection/sizing/cache/provenance workflows rather than duplicating raw command reference.

## Cache Homes

Assume the user may keep any number of persistent Hugging Face cache homes. Do not hardcode specific cache paths in the skill. Derive cache homes from the current context: explicit user messages, referenced documents or inventories, workspace configuration, environment variables, or previously established session context. If the user references a note, table, inventory, or other document, inspect that source for cache-home declarations before assuming defaults.

Use this conceptual shape when tracking homes during a task:

```json
{
  "homes": [
    {
      "name": "short human label",
      "path": "/absolute/or/expanded/HF_HOME/path",
      "source": "user|note|env|default|config"
    }
  ]
}
```

See `references/config-schema.md` for a stricter schema and field guidance.

`HF_HOME` is the root; the actual model cache lives at `$HF_HOME/hub` unless `HF_HUB_CACHE` overrides it. For one-off commands, `--cache-dir` is often simpler than exporting `HF_HOME`.

Cache-home discovery order:

1. Paths explicitly supplied by the user in the current request.
2. Paths in relevant notes, model inventories, workspace docs, or referenced documents.
3. `HF_HOME` and `HF_HUB_CACHE` from the active shell environment.
4. Any skill/config file the user has set up for persistent HF homes.
5. The Hugging Face default home only if no other source is available.

If local cache state is central to the task and no cache homes are discoverable, ask one short question about additional homes before making global claims. When reporting results, include the home label/path and source so the user can audit where each status came from.

For a clean `hf download --dry-run` or clean cache inspection, point at a fresh temporary directory in the operating system's standard temp location (via `--cache-dir` or a temporary `HF_HOME`) instead of reusing a configured cache. This avoids cached files hiding the real download size. Do not add that temporary directory to the persistent config.

## Core Workflow

1. Identify whether the task is about:
   - model-page research;
   - benchmark extraction or comparison;
   - local cache inspection or cleanup;
   - sizing and download planning;
   - repo filtering by size, quantization, or download status.
2. If the task references local cache state, check every relevant configured home before reporting a model as missing.
3. For size or download planning, prefer real metadata over parameter-count guesses (see Sizing).
4. For benchmark values, keep the exact metric names from the source page unless the user asks for normalization.
5. Keep metric provenance explicit. If values for one model come from another model's comparison table, say so and link the source at the metric or row level.
6. When generating download commands, set the target cache explicitly rather than assuming the default.

## Sizing A Repo Without Downloading

Use real metadata, not parameter count. Three reliable options:

**A. Dry-run (recommended for "what will I actually download").**

```bash
hf download <repo-id> --dry-run --json
```

`--dry-run` reports **bytes still to download**, not the full repo size: files already present in the target cache are listed as already cached and excluded from the total. So:

- For "how much will this download cost me right now", run against the user's real cache.
- For "what is the full repo size", run against a clean temporary cache so nothing is pre-cached.

**B. Full repo size from file listing (no download, ignores local cache).**

```bash
hf models ls <repo-id> -R -h        # recursive, human-readable sizes
```

Or via Python `HfApi` for exact summation:

```python
from huggingface_hub import HfApi
api = HfApi()
total = sum(f.size for f in api.list_repo_tree("<repo-id>", recursive=True))
```

**Xet caveat:** the Hub is Xet-backed and deduplicates identical chunks across files and revisions. Reported file sizes are *logical* sizes; the real transfer can be smaller when chunks are already cached or shared. So treat sizes as upper bounds for transfer, and as accurate for on-disk footprint of a fresh full download.

Recommended environment for clean, robust runs:

```bash
HF_HUB_ETAG_TIMEOUT=60 \
HF_HUB_DOWNLOAD_TIMEOUT=600 \
hf download <repo-id> --dry-run --json --cache-dir "<clean-temp-dir>"
```

Notes:
- Raise `HF_HUB_ETAG_TIMEOUT` / `HF_HUB_DOWNLOAD_TIMEOUT` on slow links (defaults are 10s).
- To speed up real downloads, prefer `HF_XET_HIGH_PERFORMANCE=1`. The old `HF_HUB_ENABLE_HF_TRANSFER` is deprecated now that the Hub is Xet-backed.
- Set `HF_HUB_DISABLE_XET=1` only if you specifically need to bypass Xet; it can change reported/transfer numbers.
- If the output is partial or the command fails, report that directly with the failure mode.

## Local Cache Inspection

When checking whether a model is downloaded or complete:

1. Discover and check every relevant cache home; do not assume there are only one or two.
2. List cache contents:
   ```bash
   hf cache ls --format json
   hf cache ls --filter "size>1GB" --sort size:desc
   ```
3. Verify integrity / completeness for a specific repo when useful:
   ```bash
   hf cache verify <repo-id> --fail-on-missing-files
   ```
   Use `--local-dir <path>` to verify files stored outside the standard cache layout.
4. For actual disk usage, use `du` on the repo directory or its `blobs/`; `hf cache ls` can report logical or deduplicated values that do not match disk usage.
5. Distinguish these states clearly and do not collapse them into a single yes/no:
   - usable: snapshot links resolve to present blob files;
   - usable + stale incomplete blobs: model is usable, but interrupted-download residue remains;
   - incomplete: snapshot links are missing or point to unusable targets;
   - metadata-only: refs/snapshots exist but required model files are absent;
   - missing: no repo cache entry in that home.
6. If a model exists in one home but not another, report that per-home rather than as one global answer.

Do not claim a model is fully available unless the cache contents are complete enough for actual use.

### Snapshot-Based Completeness Check

Do not classify a model as incomplete solely because `*.incomplete` files exist. Hugging Face can leave stale `.incomplete` blobs after interrupted downloads even when `hf download <repo-id>` later completes successfully. The model is usable if the files referenced by its active snapshot resolve to complete blob files.

For a repo cache directory such as `$HF_HOME/hub/models--ORG--REPO`, check:

1. `snapshots/` contains an active revision directory.
2. Every symlink under the active snapshot resolves to an existing file under `blobs/` or another valid target.
3. No snapshot symlink target ends in `.incomplete`.
4. Required runtime files are present for the use case: weights, tokenizer/config, index files if the model uses sharded weights.

Minimal shell pattern for one repo directory:

```bash
repo_dir="<hf-home>/hub/models--ORG--REPO"
find "$repo_dir/snapshots" -type l -print0 | while IFS= read -r -d '' link; do
  target="$(readlink "$link")"
  resolved="$(cd "$(dirname "$link")" && cd "$(dirname "$target")" && pwd)/$(basename "$target")"
  if [ ! -e "$resolved" ] || [[ "$resolved" == *.incomplete ]]; then
    printf 'BAD\t%s\t%s\n' "$link" "$target"
  fi
done
```

Treat bare `.incomplete` files that are not referenced by snapshots as cleanup candidates, not proof of an unusable model.

## Cache Cleanup And Disk Reclamation

When the user wants to free space:

1. Show the biggest or least-recently-used items first:
   ```bash
   hf cache ls --filter "size>1GB" --sort size:desc --format json
   hf cache ls --filter "accessed>30d" --sort accessed:asc
   ```
2. Remove specific repos or revisions:
   ```bash
   hf cache rm <repo-id>         # add --dry-run first to preview
   ```
3. Drop detached revisions no longer referenced by a branch/tag:
   ```bash
   hf cache prune
   ```

There is no `hf cache delete`; use `hf cache rm` and `hf cache prune`. Always preview with `--dry-run` (or list first) before removing anything, and confirm which home you are operating on.

`hf cache prune` removes detached revisions; it does not remove stale `*.incomplete` blobs. Clean incomplete residue only after snapshot-based completeness checks show the referenced model files are usable:

```bash
find "<hf-home>/hub" -name "*.incomplete" -print
find "<hf-home>/hub" -name "*.incomplete" -delete
```

For duplicate cleanup across multiple homes, build a repo-by-home matrix first. Remove a repo directory from one home only when another home has a usable copy, or when the entry is clearly metadata-only/stale residue that the user wants removed. Make destructive commands home-specific:

```bash
rm -rf "<hf-home>/hub/models--ORG--REPO"
```

Do not use broad cache deletion commands when the user asked to remove one repo, one quantization, or one home's duplicate copy.

## Selective And Pinned Downloads

Do not always pull the whole repo. Offer targeted downloads when it saves space or matches intent:

```bash
# Only safetensors weights, skip alternative precisions
hf download <repo-id> --include "*.safetensors" --exclude "*.fp16.*"

# Pin an exact revision (tag, branch, or commit) for reproducibility
hf download <repo-id> --revision <commit-or-tag>

# Download into a plain folder instead of the shared cache
hf download <repo-id> --local-dir ./<repo-name>
```

Use `--include`/`--exclude` glob patterns for large repos that ship multiple formats. Prefer `--revision` whenever reproducibility matters.

## Auth And Offline

- Check auth state with `hf auth whoami`.
- For gated or private repos, rely on `HF_TOKEN` in the environment; never ask the user to paste a token into chat, and never echo it back.
- For air-gapped or cache-only operation, set `HF_HUB_OFFLINE=1` so commands use the local cache and never hit the network. If a needed file is missing offline, report exactly what is absent.

## Benchmark Extraction And Comparison

When reading Hugging Face model pages:

1. Extract benchmark names exactly as written.
2. Do not silently merge near-matching metrics such as:
   - `MMLU` vs `MMLU-Pro` vs `MMLU (CoT)`
   - `IFEval` vs `IFEval (Loose)` vs `IFEval Avg`
   - `BFCL v2` vs `BFCL v3` vs `BFCL v4`
3. If the user wants a merged table, preserve the original metric labels unless they explicitly ask for normalization.
4. If one page contains a comparison table for another model, keep that provenance visible. Example: `gpt-oss-20b` values taken from a Nemotron comparison table must not be presented as if they came from the GPT-OSS model card itself.

Preferred phrasing:

- "This value is shown on the model's own Hugging Face page."
- "This value comes from the Nemotron comparison table's GPT-OSS-20B column, not from the GPT-OSS page itself."

## Filtering And Recommendation Rules

Follow the user's explicit constraints first, apply them mechanically, and state the rule you used.

When the user asks to skip intermediate quantizations, a good default is:

- if a family has both a low quantization variant such as `4-bit` and a higher variant such as `8-bit`, `bf16`, or `fp16`, skip `5-bit` and `6-bit` variants for recommendation lists unless the user explicitly asks to keep them.

Do not invent a family relationship if the repo names obviously belong to different custom variants.

## Download Command Generation

When generating commands:

1. Set the target cache explicitly (`HF_HOME` or `--cache-dir`).
2. Quote repo IDs inside loops.
3. Keep commands copy-paste friendly.
4. For long lists, prefer a shell loop over dozens of separate commands.

Example:

```bash
HF_HOME="<target-hf-home>" && for model in \
  "mlx-community/example-4bit" \
  "mlx-community/example-8bit"
do
  hf download "$model"
done
```

## Output Rules

- Match the user's language.
- For model lists, prefer compact tables with: repo ID, size, quantization, and download status or source when relevant.
- For cache audits, report status per discovered cache home. Prefer statuses like `usable`, `usable + stale incomplete`, `incomplete`, `metadata-only`, and `missing` instead of a single global downloaded/not-downloaded flag.
- For cleanup reports, separate logical repo size, actual disk usage from `du`, and bytes that will be reclaimed.
- For benchmark comparisons, call out source mismatches explicitly.
- If using inline links in Markdown tables, prefer ordinary Markdown links over footnote links inside table cells; many Markdown renderers do not handle footnotes reliably inside tables.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `--dry-run` total looks too small | Files already in the target cache are excluded | Re-run against a clean temp `--cache-dir` for full size |
| Real download smaller than reported size | Xet chunk-level dedup | Expected; reported sizes are upper bounds for transfer |
| `hf cache delete` errors | Command was renamed | Use `hf cache rm` / `hf cache prune` |
| `hf cache ls` size disagrees with disk usage | Logical/deduplicated size differs from filesystem footprint | Use `du -sh` on the repo directory or `blobs/` |
| `.incomplete` files exist but `hf download` says complete | Stale residue from interrupted downloads | Check snapshot symlink targets; delete unreferenced `.incomplete` after validation |
| `hf cache prune` does not free incomplete blobs | Prune removes detached revisions only | Use `find "<hf-home>/hub" -name "*.incomplete" -delete` after validation |
| Timeouts on slow connections | Default 10s timeouts | Raise `HF_HUB_ETAG_TIMEOUT` and `HF_HUB_DOWNLOAD_TIMEOUT` |
| 401 / gated repo error | Missing or unauthorized token | Set `HF_TOKEN`; confirm access with `hf auth whoami` |
| Verify reports missing files | Incomplete download or stale metadata | Re-run `hf download`; then check snapshot links and `hf cache verify --fail-on-missing-files` |
| Need to parse output reliably | Human-formatted text | Add `--format json` / `--json` and parse that |

## Failure Handling

- If `hf` is not installed, say so plainly and fall back to Hugging Face web-page inspection if that still answers the question.
- If a configured cache path does not exist, report it instead of silently skipping it.
- If a page exposes inconsistent benchmark values across sections or pages, surface the inconsistency instead of choosing one silently.
- If a model-page comparison table and the model's own page disagree, present both values with provenance.
