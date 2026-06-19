---
name: huggingface
description: Use this skill whenever the user asks to inspect, compare, filter, size, validate, or plan downloads for Hugging Face models, repos, model cards, benchmark tables, or local Hugging Face caches. Use it for tasks involving `hf download --dry-run`, Hugging Face model sizes, `HF_HOME`, multiple Hugging Face cache directories, model availability in cache, benchmark provenance from model pages, and download command generation. Also use it when the user wants to reconcile metrics from different Hugging Face pages or track which values came from which model card or comparison table. Do not use it for general ML theory, model fine-tuning, dataset labeling, or non-Hugging-Face package management.
license: MIT
compatibility: opencode
metadata:
  audience: agents
  domain: huggingface
---

# Hugging Face Model And Cache Workflow

Use this skill for Hugging Face model-page research and local cache management. The main goal is to give the user reproducible answers about model size, quantization variants, benchmark provenance, and whether a model is already available in one of the user's configured caches.

## When To Use

Use this skill for requests like:

- "Which HF models under 45 GB have I not downloaded yet?"
- "Check the benchmark table on this Hugging Face model page"
- "Compare metrics from the GPT-OSS page and the Nemotron comparison table"
- "Generate `hf download` commands for these repos"
- "Work with multiple `HF_HOME` caches"
- "See if this model is complete in main or storage cache"
- "Get the total size from `hf download --dry-run`"
- "Turn Hugging Face repo IDs into model-page links"

Do not use this skill for:

- generic ML benchmark interpretation without Hugging Face pages or repos;
- training, fine-tuning, LoRA setup, or inference-server deployment;
- Python package management unrelated to Hugging Face model caches;
- editing application code that merely happens to import `huggingface_hub`.

## Cache Configuration

Assume the user may keep more than one persistent Hugging Face cache. Store only persistent homes in a small JSON config. Do not persist temporary dry-run homes.

Minimal config shape:

```json
{
  "homes": [
    {
      "name": "main",
      "path": "~/.cache/huggingface"
    },
    {
      "name": "storage",
      "path": "/Volumes/Storage/hf"
    }
  ]
}
```

See `references/config-schema.md` for a stricter schema and field guidance.

If the task needs a clean `hf download --dry-run` or a clean cache inspection, create a fresh temporary `HF_HOME` in the operating system's standard temp directory instead of reusing a configured cache. This avoids false positives from cached metadata and keeps the persistent config small.

## Core Workflow

1. Identify whether the task is about:
   - model-page research;
   - benchmark extraction or comparison;
   - local cache inspection;
   - download planning;
   - repo filtering by size, quantization, or download status.
2. If the task references local cache state, determine which configured `HF_HOME` values matter. Prefer checking all configured homes before reporting a model as missing.
3. If the task needs total model size without downloading, use `hf download <repo> --dry-run` and parse the reported total size from the output.
4. If the task involves benchmark values, keep the exact metric names from the source page unless the user explicitly asks for normalization.
5. Always keep metric provenance explicit. If values for one model come from another model's comparison table, say so and preserve a source link at the metric or row level.
6. When generating batch download commands, set `HF_HOME` explicitly rather than assuming the default cache.

## Local Cache Inspection

When checking whether a model is downloaded or complete:

1. Check all configured persistent homes that are relevant to the task.
2. Use Hugging Face cache commands when available, especially:
   - `hf cache list`
   - `hf cache verify --fail-on-missing-files`
3. Distinguish these states clearly:
   - fully downloaded;
   - missing;
   - metadata-only;
   - incomplete or missing shards/tokenizer/index.
4. If a model exists in one home but not another, report that directly instead of flattening it into a single yes/no answer.

Do not claim a model is fully available unless the cache contents are complete enough for actual use.

## Dry-Run Sizing

For size estimation, use `hf download --dry-run` rather than guessing from parameter count.

Recommended pattern:

```bash
HF_HOME="<clean-temp-home>" \
HF_HUB_DISABLE_XET=1 \
HF_HUB_ETAG_TIMEOUT=60 \
HF_HUB_DOWNLOAD_TIMEOUT=600 \
hf download <repo-id> --dry-run
```

Interpretation rules:

- Use the total reported by the command output, not the repo's approximate parameter count.
- Treat the dry-run total as the source of truth for download planning.
- If the dry-run output is partial or fails, report that directly and include the failure mode.

## Benchmark Extraction And Comparison

When reading Hugging Face model pages:

1. Extract benchmark names exactly as written.
2. Do not silently merge near-matching metrics such as:
   - `MMLU` vs `MMLU-Pro` vs `MMLU (CoT)`
   - `IFEval` vs `IFEval (Loose)` vs `IFEval Avg`
   - `BFCL v2` vs `BFCL v3` vs `BFCL v4`
3. If the user wants a merged table, preserve the original metric labels unless they explicitly ask for normalization.
4. If one page contains a comparison table for another model, keep that provenance visible. Example: `gpt-oss-20b` values taken from a Nemotron comparison table should not be presented as if they came from the GPT-OSS model card itself.

Preferred phrasing:

- "This value is shown on the model's own Hugging Face page."
- "This value comes from the Nemotron comparison table's GPT-OSS-20B column, not from the GPT-OSS page itself."

## Filtering And Recommendation Rules

For lists of candidate repos, follow the user's explicit constraints first. If the user wants model filtering by size or quantization, apply the constraints mechanically and report the rule you used.

When the user asks to skip intermediate quantizations, a good default is:

- if a family has both a low quantization variant such as `4-bit` and a higher variant such as `8-bit`, `bf16`, or `fp16`, skip `5-bit` and `6-bit` variants for recommendation lists unless the user explicitly asks to keep them.

Do not invent a family relationship if the repo names obviously belong to different custom variants.

## Download Command Generation

When generating commands:

1. Set `HF_HOME` explicitly.
2. Quote repo IDs inside loops.
3. Keep commands copy-paste friendly.
4. If the list is long, a shell loop is preferred over dozens of separate commands.

Example:

```bash
HF_HOME="/Volumes/Storage/hf" && for model in \
  "mlx-community/example-4bit" \
  "mlx-community/example-8bit"
do
  hf download "$model"
done
```

## Output Rules

- Match the user's language.
- For model lists, prefer compact tables with:
  - repo ID;
  - size;
  - quantization;
  - download status or source when relevant.
- For benchmark comparisons, call out source mismatches explicitly.
- If using inline links in Markdown tables for Obsidian, prefer ordinary Markdown links over footnote links inside table cells.

## Failure Handling

- If `hf` is not installed, say so plainly and continue with Hugging Face web-page inspection if that still answers the question.
- If a configured cache path does not exist, report that instead of silently skipping it.
- If a page exposes inconsistent benchmark values across sections or pages, surface the inconsistency instead of choosing one silently.
- If a model-page comparison table and the model's own page disagree, present both values with provenance.
