#!/usr/bin/env python3
"""
extract.py — Extract text, tables, and speaker notes from a .pptx file.

Output formats:
  md   Markdown (one section per slide, default)
  txt  Plain text
  json Structured JSON

Exit codes:
  0  success
  1  extraction failed
  2  usage error
  3  unsupported format / missing dependency
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import importlib.util as _ilu
_common_path = Path(__file__).parent / '_common.py'
_spec = _ilu.spec_from_file_location('_common', _common_path)
_common_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_common_mod)

detect_format = _common_mod.detect_format
emit_json = _common_mod.emit_json
fail = _common_mod.fail

try:
    from pptx import Presentation
    from pptx.util import Emu
    _PPTX_AVAILABLE = True
except ImportError:
    _PPTX_AVAILABLE = False


# ---------------------------------------------------------------------------
# Extraction logic
# ---------------------------------------------------------------------------

def _shape_text(shape) -> str:
    """Extract all text from a shape's text frame."""
    if not shape.has_text_frame:
        return ""
    lines = []
    for para in shape.text_frame.paragraphs:
        line = "".join(run.text for run in para.runs)
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def _table_to_list(shape) -> list[list[str]]:
    """Extract table data as a list of rows (each row is a list of cell strings)."""
    if not shape.has_table:
        return []
    rows = []
    for row in shape.table.rows:
        rows.append([cell.text_frame.text if cell.text_frame else "" for cell in row.cells])
    return rows


def _table_to_md(rows: list[list[str]]) -> str:
    """Render a table as GFM Markdown."""
    if not rows:
        return ""
    lines = []
    header = rows[0]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] * len(header)) + " |")
    for row in rows[1:]:
        # Pad row if shorter than header
        padded = list(row) + [""] * (len(header) - len(row))
        lines.append("| " + " | ".join(padded[:len(header)]) + " |")
    return "\n".join(lines)


def _notes_text(slide) -> str:
    """Extract speaker notes text from a slide."""
    try:
        notes_slide = slide.notes_slide
        tf = notes_slide.notes_text_frame
        return tf.text.strip() if tf else ""
    except Exception:
        return ""


def extract_slides(path: Path) -> list[dict[str, Any]]:
    """
    Extract all slide data via python-pptx.

    Returns a list of slide dicts:
      {
        slide_number: int,
        title: str,
        texts: [str],
        tables: [[[str]]],
        notes: str,
        has_images: bool,
      }
    """
    prs = Presentation(str(path))
    slides_data = []

    for i, slide in enumerate(prs.slides, start=1):
        title = ""
        texts = []
        tables = []
        has_images = False

        for shape in slide.shapes:
            # Picture shapes
            if shape.shape_type == 13:  # MSO_SHAPE_TYPE.PICTURE
                has_images = True
                continue

            if shape.has_table:
                tables.append(_table_to_list(shape))
                continue

            if not shape.has_text_frame:
                continue

            # Check if placeholder (safe: has_text_frame guard already done)
            try:
                ph = shape.placeholder_format
            except Exception:
                ph = None

            if ph is not None:
                try:
                    from pptx.enum.text import PP_PLACEHOLDER
                    if ph.type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE):
                        title = shape.text_frame.text.strip()
                        continue
                except Exception:
                    pass

            text = _shape_text(shape)
            if text:
                texts.append(text)

        # Try title from slide title placeholder properly
        try:
            if slide.shapes.title and slide.shapes.title.has_text_frame:
                title = slide.shapes.title.text_frame.text.strip()
        except Exception:
            pass

        notes = _notes_text(slide)

        slides_data.append({
            "slide_number": i,
            "title": title,
            "texts": texts,
            "tables": tables,
            "notes": notes,
            "has_images": has_images,
        })

    return slides_data


def _to_markdown(slides: list[dict[str, Any]], path: Path) -> str:
    lines = [f"# {path.name}\n"]
    for slide in slides:
        num = slide["slide_number"]
        title = slide["title"] or f"Slide {num}"
        lines.append(f"## Slide {num}: {title}\n")

        for text in slide["texts"]:
            lines.append(text)
            lines.append("")

        for table in slide["tables"]:
            lines.append(_table_to_md(table))
            lines.append("")

        if slide["notes"]:
            lines.append("> **Speaker notes:**")
            for note_line in slide["notes"].splitlines():
                lines.append(f"> {note_line}")
            lines.append("")

    return "\n".join(lines)


def _to_text(slides: list[dict[str, Any]], path: Path) -> str:
    lines = [path.name, "=" * len(path.name), ""]
    for slide in slides:
        num = slide["slide_number"]
        title = slide["title"] or f"Slide {num}"
        lines.append(f"--- Slide {num}: {title} ---")
        for text in slide["texts"]:
            lines.append(text)
        for table in slide["tables"]:
            for row in table:
                lines.append("\t".join(row))
        if slide["notes"]:
            lines.append(f"[Notes: {slide['notes']}]")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="extract.py",
        description="Extract text, tables, and notes from a .pptx file.",
    )
    parser.add_argument("file", help="Path to .pptx file")
    parser.add_argument(
        "--format", choices=["md", "txt", "json"], default="md",
        help="Output format: md (default), txt, json",
    )
    parser.add_argument(
        "-o", "--output", metavar="OUTPUT",
        help="Output file path (default: stdout)",
    )
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        fail(2, f"file not found: {path}")

    fmt = detect_format(path)
    if fmt == "ppt":
        fail(3,
             "legacy .ppt format not supported directly. "
             "Convert first: scripts/convert.py input.ppt -o output.pptx")

    if not _PPTX_AVAILABLE:
        fail(3,
             "python-pptx is required. Install with: pip install python-pptx")

    try:
        slides = extract_slides(path)
    except Exception as exc:
        fail(1, f"extraction failed: {exc}")

    fmt_arg = args.format
    if fmt_arg == "md":
        output = _to_markdown(slides, path)
    elif fmt_arg == "txt":
        output = _to_text(slides, path)
    else:
        output = json.dumps({"file": str(path), "slides": slides}, indent=2, ensure_ascii=False)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"extracted -> {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
