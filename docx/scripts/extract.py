#!/usr/bin/env python3
"""
extract.py — Extract content from a .docx file as Markdown, plain text, or JSON.

Uses python-docx for structured extraction. Warns when tracked changes are
present (python-docx skips content inside w:ins/w:del) and suggests pandoc.

Exit codes:
  0  success
  1  extraction error
  2  usage error
  3  unsupported format / missing dependency
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

# When Python runs this file as a script it inserts the script's directory
# (docx/scripts/) into sys.path[0].  That causes 'import docx' to resolve to
# the docx/ skill directory instead of the python-docx package.
# Remove any relative path entries that point inside the skill tree before
# importing python-docx.
import os as _os
_scripts_dir = str(Path(__file__).parent.resolve())
_skill_dir   = str(Path(__file__).parent.parent.resolve())
sys.path = [p for p in sys.path if _os.path.realpath(p or ".") not in (_scripts_dir, _skill_dir)]

try:
    import docx
    from docx import Document
    from docx.oxml.ns import qn
    _HAS_DOCX = True
except ImportError:
    _HAS_DOCX = False

# Load _common by absolute path so sys.path manipulation doesn't shadow
# third-party packages (python-docx) that share the 'docx' package name.
import importlib.util as _ilu
_common_path = Path(__file__).parent / '_common.py'
_spec = _ilu.spec_from_file_location('_common', _common_path)
_common_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_common_mod)
detect_format = _common_mod.detect_format
emit_json = _common_mod.emit_json
fail = _common_mod.fail


# ---------------------------------------------------------------------------
# Heading level from style name
# ---------------------------------------------------------------------------

_HEADING_MAP = {
    "Heading 1": 1, "Heading 2": 2, "Heading 3": 3,
    "Heading 4": 4, "Heading 5": 5, "Heading 6": 6,
    "Title": 1,
}


def _heading_level(para) -> int | None:
    if para.style and para.style.name in _HEADING_MAP:
        return _HEADING_MAP[para.style.name]
    # outline level from pPr
    try:
        pPr = para._p.find(qn("w:pPr"))
        if pPr is not None:
            outline = pPr.find(qn("w:outlineLvl"))
            if outline is not None:
                lvl = int(outline.get(qn("w:val"), "9"))
                if lvl < 6:
                    return lvl + 1
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# List detection
# ---------------------------------------------------------------------------

def _list_prefix(para) -> str | None:
    """Return '- ' for bullets or '1. ' for numbered; None if not a list."""
    try:
        pPr = para._p.find(qn("w:pPr"))
        if pPr is None:
            return None
        numPr = pPr.find(qn("w:numPr"))
        if numPr is None:
            return None
        # Detect bullet vs decimal from numFmt — simplified: use style name
        style_name = para.style.name if para.style else ""
        if "List Bullet" in style_name:
            return "- "
        if "List Number" in style_name:
            return "1. "
        # Default to bullet
        return "- "
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Comment extraction
# ---------------------------------------------------------------------------

def _extract_comments(doc_path: Path) -> list[dict[str, Any]]:
    """Extract comments using python-docx 1.2+ API."""
    comments: list[dict[str, Any]] = []
    try:
        doc = Document(str(doc_path))
        for c in doc.comments:
            entry: dict[str, Any] = {
                "id":     str(getattr(c, "id", "")),
                "author": str(getattr(c, "author", "")),
                "date":   str(getattr(c, "date", "")),
                "text":   "\n".join(
                    p.text for p in getattr(c, "paragraphs", [])
                ).strip(),
            }
            comments.append(entry)
    except AttributeError:
        # python-docx < 1.2 — fall back to XML parse
        comments = _extract_comments_xml(doc_path)
    except Exception:
        comments = _extract_comments_xml(doc_path)
    return comments


def _extract_comments_xml(doc_path: Path) -> list[dict[str, Any]]:
    """Fallback: extract comments directly from comments.xml."""
    comments: list[dict[str, Any]] = []
    try:
        import defusedxml.ElementTree as ET
    except ImportError:
        import xml.etree.ElementTree as ET  # type: ignore[no-redef]

    W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    try:
        with zipfile.ZipFile(doc_path, "r") as zf:
            if "word/comments.xml" not in zf.namelist():
                return []
            data = zf.read("word/comments.xml")
        root = ET.fromstring(data)
        for comment in root:
            cid     = comment.get(f"{{{W_NS}}}id", "")
            author  = comment.get(f"{{{W_NS}}}author", "")
            date    = comment.get(f"{{{W_NS}}}date", "")
            texts   = []
            for p in comment.iter(f"{{{W_NS}}}t"):
                if p.text:
                    texts.append(p.text)
            comments.append({
                "id": cid, "author": author, "date": date,
                "text": "".join(texts).strip(),
            })
    except Exception:
        pass
    return comments


# ---------------------------------------------------------------------------
# Tracked changes detection
# ---------------------------------------------------------------------------

def _has_tracked_changes(doc_path: Path) -> bool:
    try:
        with zipfile.ZipFile(doc_path, "r") as zf:
            data = zf.read("word/document.xml")
        return b"<w:ins " in data or b"<w:del " in data
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Table → GFM Markdown
# ---------------------------------------------------------------------------

def _table_to_md(table) -> str:
    rows = []
    for i, row in enumerate(table.rows):
        cells = [cell.text.replace("\n", " ").replace("|", "\\|") for cell in row.cells]
        rows.append("| " + " | ".join(cells) + " |")
        if i == 0:
            rows.append("| " + " | ".join("---" for _ in cells) + " |")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Main extraction logic
# ---------------------------------------------------------------------------

def extract_json(doc_path: Path) -> dict[str, Any]:
    doc = Document(str(doc_path))

    # Core properties
    cp = doc.core_properties
    metadata = {
        "author":          cp.author,
        "lastModifiedBy":  cp.last_modified_by,
        "created":         str(cp.created) if cp.created else None,
        "modified":        str(cp.modified) if cp.modified else None,
        "title":           cp.title,
        "subject":         cp.subject,
        "keywords":        cp.keywords,
        "revision":        cp.revision,
    }

    # Paragraphs
    paragraphs = []
    for p in doc.paragraphs:
        entry: dict[str, Any] = {
            "style": p.style.name if p.style else "Normal",
            "text":  p.text,
        }
        lvl = _heading_level(p)
        if lvl:
            entry["heading_level"] = lvl
        paragraphs.append(entry)

    # Tables
    tables = []
    for table in doc.tables:
        rows = []
        for row in table.rows:
            rows.append([cell.text for cell in row.cells])
        tables.append(rows)

    # Headers / footers
    headers: list[str] = []
    footers: list[str] = []
    for section in doc.sections:
        for p in section.header.paragraphs:
            if p.text.strip():
                headers.append(p.text)
        for p in section.footer.paragraphs:
            if p.text.strip():
                footers.append(p.text)

    # Comments
    comments = _extract_comments(doc_path)

    return {
        "metadata":   metadata,
        "paragraphs": paragraphs,
        "tables":     tables,
        "headers":    headers,
        "footers":    footers,
        "comments":   comments,
    }


def extract_markdown(doc_path: Path) -> str:
    doc = Document(str(doc_path))
    lines: list[str] = []

    # Iterate paragraphs and tables in document order
    from docx.oxml.ns import qn as _qn
    body = doc.element.body

    table_set = {t._tbl for t in doc.tables}

    for child in body:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag

        if tag == "p":
            # It's a paragraph
            try:
                from docx.text.paragraph import Paragraph as _Para
                p = _Para(child, doc)
            except Exception:
                continue

            text = p.text
            if not text.strip():
                lines.append("")
                continue

            lvl = _heading_level(p)
            if lvl:
                lines.append(f"{'#' * lvl} {text}")
            else:
                prefix = _list_prefix(p)
                if prefix:
                    lines.append(f"{prefix}{text}")
                else:
                    lines.append(text)

        elif tag == "tbl":
            # It's a table
            try:
                from docx.table import Table as _Table
                t = _Table(child, doc)
                lines.append("")
                lines.append(_table_to_md(t))
                lines.append("")
            except Exception:
                pass

    # Append comments as a block at the end
    comments = _extract_comments(doc_path)
    if comments:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## Comments")
        lines.append("")
        for c in comments:
            author = c.get("author", "")
            date   = c.get("date", "")[:10] if c.get("date") else ""
            text   = c.get("text", "")
            lines.append(f"> **Comment ({author}, {date}):** {text}")
            lines.append("")

    return "\n".join(lines)


def extract_text(doc_path: Path) -> str:
    doc = Document(str(doc_path))
    return "\n".join(p.text for p in doc.paragraphs)


# ---------------------------------------------------------------------------
# .doc fallback via textutil (macOS)
# ---------------------------------------------------------------------------

def extract_doc_textutil(doc_path: Path, fmt: str) -> str:
    import shutil
    if not shutil.which("textutil"):
        fail(3, "textutil not found (macOS only); install LibreOffice for .doc support")
    result = subprocess.run(
        ["textutil", "-convert", "txt", "-stdout", str(doc_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        fail(1, f"textutil failed: {result.stderr.strip()}")
    return result.stdout


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="extract.py",
        description="Extract content from a .docx file as Markdown, text, or JSON.",
    )
    parser.add_argument("file", help="Path to .docx file")
    parser.add_argument(
        "--format", "-f", choices=["md", "txt", "json"], default="md",
        help="Output format (default: md)",
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

    # .doc fallback
    if fmt == "doc":
        text = extract_doc_textutil(path, args.format)
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
        else:
            print(text)
        return

    if fmt not in ("docx", "docm"):
        fail(3, f"unsupported format: {fmt}")

    if not _HAS_DOCX:
        fail(3, "python-docx not installed; run: pip install python-docx")

    # Warn about tracked changes
    if _has_tracked_changes(path):
        print(
            "warning: document contains tracked changes; python-docx skips content "
            "inside w:ins/w:del — install pandoc for full fidelity extraction:\n"
            "  pandoc --track-changes=all input.docx -t gfm -o output.md",
            file=sys.stderr,
        )

    try:
        if args.format == "json":
            data = extract_json(path)
            output_text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        elif args.format == "txt":
            output_text = extract_text(path)
        else:
            output_text = extract_markdown(path)
    except Exception as exc:
        fail(1, f"extraction failed: {exc}")

    if args.output:
        Path(args.output).write_text(output_text, encoding="utf-8")
        print(f"extracted -> {args.output}", file=sys.stderr)
    else:
        print(output_text)


if __name__ == "__main__":
    main()
