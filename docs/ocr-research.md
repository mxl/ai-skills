# OCR Research: Tesseract Tables and Benchmark Images

Date: 2026-06-19

## Context

While filling a local LLM benchmark table from Hugging Face model cards, several benchmark tables were available only as images. The OCR skill was available through `skills.paths` and was loaded successfully. We tested the baseline Tesseract path on downloaded benchmark images, then compared it with the skill's vision-tier workflow.

Test images:

- `qwen3vl30_text.jpg` from `Qwen/Qwen3-VL-30B-A3B-Instruct`
- `qwen3vl4_text.jpg` from `Qwen/Qwen3-VL-4B-Instruct`
- `qwen3coder30_main.jpg` from `Qwen/Qwen3-Coder-30B-A3B-Instruct`
- `hermes70_a.png` from `NousResearch/Hermes-4-70B`
- `hermes70_b.png` from `NousResearch/Hermes-4-70B`

## Commands Tested

Baseline OCR:

```bash
python3 ~/projects/ai-skills/ocr/scripts/ocr.py <images...> --format all --out <out-dir> --verbose
```

Single-block retry for difficult tables/plots:

```bash
python3 ~/projects/ai-skills/ocr/scripts/ocr.py qwen3coder30_main.jpg hermes70_b.png --format all --out <out-dir> --psm 6 --verbose
```

## Review-Vision Flag Logic

The script marks a page as `review-vision` when either condition is true:

```python
if conf < args.min_conf or looks_tabular(words):
    flag = "review-vision"
```

The default confidence threshold is:

```python
DEFAULT_MIN_CONF = 60.0
```

`looks_tabular(words)` uses a bounding-box heuristic:

- group words by y-bucket of 10 px;
- compute x-position buckets across the page width;
- a row is considered table-like if it has at least 4 words across at least 3 x-column buckets;
- the page is considered table-like if at least 3 rows match this pattern.

This means high-confidence table OCR can still be flagged. The flag does not mean Tesseract failed; it means table structure should be reviewed with vision because cell-to-column association matters.

## Results

| Image | Tesseract result | Vision-tier result |
| --- | --- | --- |
| `qwen3vl30_text.jpg` | Mean confidence `92.9`; text and numbers mostly correct; flagged because table-like. Minor OCR errors: `LCBv6` -> `LCBvé`, `MultiIF` -> `MultilF`, `22.2` -> `22,2`. | Preserves table structure and column/value mapping reliably. |
| `qwen3vl4_text.jpg` | Mean confidence `93.0`; text mostly correct; flagged because table-like. Dangerous errors: `35.2` -> `354`, `53.5` -> `§3.5`. | Preserves column mapping between `4B Instruct`, `8B Instruct`, `Qwen3-4B Instruct-2507`, and `Qwen3-8B Non-Thinking`. |
| `qwen3coder30_main.jpg` | Baseline confidence `82.8`; table structure poor. `--psm 6` improves recall but still has errors: `33.3` -> `S353`, `22.3` -> `2253`, `13.0` -> `13730`, `53.3` -> `53-3`, `31.5` -> `3125`. | Correctly preserves the `Qwen3-Coder 30B-A3B-Instruct` column values. |
| `hermes70_a.png` | Mean confidence `72.8`; main table usable but headings/parentheses are noisy: `Metric` -> `Me`, `Qwen3` -> `Owen3`, parenthesized non-reasoning scores can be confused. | Correctly distinguishes reasoning scores from non-reasoning scores in parentheses. |
| `hermes70_b.png` | Baseline confidence `47.7`; `--psm 6` confidence `55.3`; graph labels and percentages badly distorted. | Needed for reliable bar-chart reading. |

## Main Observation

Tesseract is useful as a sanity check and raw text extractor. It is not enough as the final source for benchmark table extraction because it loses or corrupts 2D structure:

- row/column association is not guaranteed;
- OCR can confuse `5/S/§`, decimal points, commas, and hyphens;
- plots and stylized tables degrade sharply;
- high confidence can coexist with incorrect table reconstruction.

## Recommended Pipeline for Table Markdown

To improve Tesseract-based table extraction, do not use plain text output. Use TSV/HOCR with bounding boxes and reconstruct the table.

Suggested command:

```bash
tesseract image.png stdout --oem 3 --psm 6 -c preserve_interword_spaces=1 tsv
```

Or Python:

```python
pytesseract.image_to_data(img, config="--oem 3 --psm 6", output_type=Output.DATAFRAME)
```

Recommended reconstruction steps:

1. Preprocess image: crop decorative margins, upscale 2x/3x, grayscale, sharpen, threshold, deskew.
2. Run multiple PSM modes: `3`, `4`, `6`, `11`, optionally `12`.
3. Extract TSV/word bounding boxes, not plain text.
4. Filter low-confidence words.
5. Cluster words into rows by y-position/baseline.
6. Cluster x-positions into columns.
7. Assign words to nearest row/column cell.
8. Join words inside each cell.
9. Validate numeric cells with benchmark-specific regexes.
10. Apply benchmark-specific corrections using dictionaries of known metric names.
11. Emit Markdown table.
12. Use vision-tier only for flagged/ambiguous cells or complex chart/table layouts.

## Suggested OCR Skill Enhancements

Add a table output mode:

```text
--format table-md
--table-mode auto|bbox|lines
--psm-sweep 3,4,6,11
```

Implementation notes:

- reuse existing `words` with bounding boxes already produced by the script;
- when `looks_tabular(words)` is true, attempt table reconstruction before flagging only;
- add `tables: [...]` to JSON output;
- optionally use OpenCV morphology for tables with visible horizontal/vertical lines;
- for line-less benchmark tables, use row/column clustering from word coordinates;
- keep `review-vision` for low-confidence or ambiguous table cells.

## Practical Rule

For benchmark images:

1. Run Tesseract first for text extraction and sanity check.
2. Use bbox-based reconstruction when available.
3. Use vision-tier to verify final metric-to-model mappings.
4. Only write values into notes when source image and value mapping are unambiguous.
