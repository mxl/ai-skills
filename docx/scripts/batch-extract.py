#!/usr/bin/env python3
"""
batch-extract.py — Convert .doc files to .docx and extract full document
structure (headings, tables, comments, footnotes, headers, footers) into JSON
for indexing.

Usage:
  # Convert + extract a single .doc file
  python batch-extract.py input.doc -o output/

  # Batch-process a directory of .doc files
  python batch-extract.py ./legacy_docs/ -o ./extracted/

  # Extract from already-converted .docx files (skip conversion)
  python batch-extract.py ./docs/*.docx -o ./extracted/ --no-convert

  # Write a combined index file
  python batch-extract.py ./legacy_docs/ -o ./extracted/ --index all_docs.json

Exit codes:
  0  success
  1  extraction error
  2  usage error
  3  missing dependency / engine
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

# Fix sys.path so 'import docx' resolves to python-docx, not the skill dir.
_scripts_dir = str(Path(__file__).parent.resolve())
_skill_dir = str(Path(__file__).parent.parent.resolve())
sys.path = [p for p in sys.path if os.path.realpath(p or ".") not in (_scripts_dir, _skill_dir)]

try:
    from docx import Document
    from docx.oxml.ns import qn
    _HAS_DOCX = True
except ImportError:
    _HAS_DOCX = False

# Load _common by absolute path
import importlib.util as _ilu
_common_path = Path(__file__).parent / "_common.py"
_spec = _ilu.spec_from_file_location("_common", _common_path)
_common_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_common_mod)
detect_format = _common_mod.detect_format
fail = _common_mod.fail


# ---------------------------------------------------------------------------
# Heading detection
# ---------------------------------------------------------------------------

_HEADING_MAP = {
    "Heading 1": 1, "Heading 2": 2, "Heading 3": 3,
    "Heading 4": 4, "Heading 5": 5, "Heading 6": 6,
    "Title": 1,
}


def _heading_level(para) -> int | None:
    if para.style and para.style.name in _HEADING_MAP:
        return _HEADING_MAP[para.style.name]
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


def _iter_block_items(doc) -> list[Any]:
    """Yield document paragraphs and tables in body order."""
    body = doc.element.body
    paragraphs_by_element = {p._p: p for p in doc.paragraphs}
    tables_by_element = {t._tbl: t for t in doc.tables}
    blocks: list[Any] = []
    for child in body.iterchildren():
        if child in paragraphs_by_element:
            blocks.append(paragraphs_by_element[child])
        elif child in tables_by_element:
            blocks.append(tables_by_element[child])
    return blocks


# ---------------------------------------------------------------------------
# Comment extraction
# ---------------------------------------------------------------------------

def _extract_comments(doc_path: Path) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    try:
        doc = Document(str(doc_path))
        for c in doc.comments:
            entry: dict[str, Any] = {
                "id": str(getattr(c, "id", "")),
                "author": str(getattr(c, "author", "")),
                "date": str(getattr(c, "date", "")),
                "text": "\n".join(
                    p.text for p in getattr(c, "paragraphs", [])
                ).strip(),
            }
            comments.append(entry)
    except AttributeError:
        comments = _extract_comments_xml(doc_path)
    except Exception:
        comments = _extract_comments_xml(doc_path)
    return comments


def _extract_comments_xml(doc_path: Path) -> list[dict[str, Any]]:
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
            cid = comment.get(f"{{{W_NS}}}id", "")
            author = comment.get(f"{{{W_NS}}}author", "")
            date = comment.get(f"{{{W_NS}}}date", "")
            texts = []
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
# Footnote extraction
# ---------------------------------------------------------------------------

def _extract_footnotes(doc_path: Path) -> list[dict[str, Any]]:
    """Extract footnotes from word/footnotes.xml."""
    footnotes: list[dict[str, Any]] = []
    try:
        import defusedxml.ElementTree as ET
    except ImportError:
        import xml.etree.ElementTree as ET  # type: ignore[no-redef]

    W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    try:
        with zipfile.ZipFile(doc_path, "r") as zf:
            if "word/footnotes.xml" not in zf.namelist():
                return []
            data = zf.read("word/footnotes.xml")
        root = ET.fromstring(data)
        for note in root:
            nid = note.get(f"{{{W_NS}}}id", "")
            note_type = note.get(f"{{{W_NS}}}type", "normal")
            # Skip separator/continuationSeparator footnotes
            if note_type in ("separator", "continuationSeparator"):
                continue
            texts = []
            for t_elem in note.iter(f"{{{W_NS}}}t"):
                if t_elem.text:
                    texts.append(t_elem.text)
            text = "".join(texts).strip()
            if text:
                footnotes.append({
                    "id": nid,
                    "type": note_type,
                    "text": text,
                })
    except Exception:
        pass
    return footnotes


# ---------------------------------------------------------------------------
# Endnote extraction
# ---------------------------------------------------------------------------

def _extract_endnotes(doc_path: Path) -> list[dict[str, Any]]:
    """Extract endnotes from word/endnotes.xml."""
    endnotes: list[dict[str, Any]] = []
    try:
        import defusedxml.ElementTree as ET
    except ImportError:
        import xml.etree.ElementTree as ET  # type: ignore[no-redef]

    W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    try:
        with zipfile.ZipFile(doc_path, "r") as zf:
            if "word/endnotes.xml" not in zf.namelist():
                return []
            data = zf.read("word/endnotes.xml")
        root = ET.fromstring(data)
        for note in root:
            nid = note.get(f"{{{W_NS}}}id", "")
            note_type = note.get(f"{{{W_NS}}}type", "normal")
            if note_type in ("separator", "continuationSeparator"):
                continue
            texts = []
            for t_elem in note.iter(f"{{{W_NS}}}t"):
                if t_elem.text:
                    texts.append(t_elem.text)
            text = "".join(texts).strip()
            if text:
                endnotes.append({
                    "id": nid,
                    "type": note_type,
                    "text": text,
                })
    except Exception:
        pass
    return endnotes


# ---------------------------------------------------------------------------
# Full structure extraction
# ---------------------------------------------------------------------------

def extract_structure(doc_path: Path, source_path: Path | None = None) -> dict[str, Any]:
    """Extract full document structure for indexing."""
    doc = Document(str(doc_path))

    # Metadata
    cp = doc.core_properties
    metadata: dict[str, Any] = {
        "author": cp.author,
        "lastModifiedBy": cp.last_modified_by,
        "created": str(cp.created) if cp.created else None,
        "modified": str(cp.modified) if cp.modified else None,
        "title": cp.title,
        "subject": cp.subject,
        "keywords": cp.keywords,
        "revision": cp.revision,
    }

    # Headings (extracted separately for easy indexing)
    headings: list[dict[str, Any]] = []
    paragraphs: list[dict[str, Any]] = []
    for index, p in enumerate(doc.paragraphs):
        lvl = _heading_level(p)
        paragraph: dict[str, Any] = {
            "index": index,
            "style": p.style.name if p.style else "Normal",
            "text": p.text,
        }
        if lvl:
            paragraph["heading_level"] = lvl
        paragraphs.append(paragraph)
        if lvl and p.text.strip():
            headings.append({
                "level": lvl,
                "text": p.text.strip(),
                "paragraph_index": index,
            })

    # Tables
    tables: list[dict[str, Any]] = []
    for index, table in enumerate(doc.tables):
        rows = []
        for row in table.rows:
            rows.append([cell.text.strip() for cell in row.cells])
        tables.append({
            "index": index,
            "rows": rows,
            "row_count": len(rows),
            "column_count": max((len(row) for row in rows), default=0),
        })

    body_blocks: list[dict[str, Any]] = []
    paragraph_index = 0
    table_index = 0
    for block in _iter_block_items(doc):
        if hasattr(block, "_p"):
            entry = dict(paragraphs[paragraph_index])
            entry["type"] = "paragraph"
            body_blocks.append(entry)
            paragraph_index += 1
        elif hasattr(block, "_tbl"):
            entry = dict(tables[table_index])
            entry["type"] = "table"
            body_blocks.append(entry)
            table_index += 1

    # Headers / footers
    headers: list[str] = []
    footers: list[str] = []
    for section in doc.sections:
        for p in section.header.paragraphs:
            if p.text.strip():
                headers.append(p.text.strip())
        for p in section.footer.paragraphs:
            if p.text.strip():
                footers.append(p.text.strip())

    # Comments
    comments = _extract_comments(doc_path)

    # Footnotes
    footnotes = _extract_footnotes(doc_path)

    # Endnotes
    endnotes = _extract_endnotes(doc_path)

    # Full body text (for full-text indexing)
    body_text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())

    return {
        "source_file": str(source_path or doc_path),
        "docx_file": str(doc_path),
        "metadata": metadata,
        "paragraphs": paragraphs,
        "body_blocks": body_blocks,
        "headings": headings,
        "tables": tables,
        "headers": headers,
        "footers": footers,
        "comments": comments,
        "footnotes": footnotes,
        "endnotes": endnotes,
        "body_text": body_text,
    }


# ---------------------------------------------------------------------------
# .doc → .docx conversion
# ---------------------------------------------------------------------------

def convert_doc_to_docx(doc_path: Path, output_dir: Path) -> Path:
    """Convert a .doc file to .docx using the skill's convert.py."""
    script_dir = Path(__file__).parent
    convert_script = script_dir / "convert.py"
    docx_path = output_dir / (doc_path.stem + ".docx")

    result = subprocess.run(
        [sys.executable, str(convert_script), str(doc_path),
         "-o", str(docx_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"convert.py failed for {doc_path.name}: {result.stderr.strip()}"
        )
    if not docx_path.exists():
        raise RuntimeError(f"convert.py did not produce expected output: {docx_path}")
    return docx_path


def _unique_path(path: Path) -> Path:
    """Return a non-existing path by appending a numeric suffix if needed."""
    if not path.exists():
        return path
    for i in range(1, 10000):
        candidate = path.with_name(f"{path.stem}-{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find available output path for {path}")


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------

def collect_input_files(paths: list[str]) -> list[Path]:
    """Resolve input paths to a flat list of files."""
    files: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            files.extend(
                sorted(
                    f for f in path.rglob("*")
                    if f.is_file()
                    and f.suffix.lower() in (".doc", ".docx", ".docm")
                    and not f.name.startswith("~$")
                )
            )
        elif path.is_file():
            if path.suffix.lower() in (".doc", ".docx", ".docm") and not path.name.startswith("~$"):
                files.append(path)
        else:
            # Try as a glob pattern
            expanded = list(Path(".").glob(p))
            if expanded:
                files.extend(sorted(expanded))
            else:
                print(f"warning: skipping unresolved path: {p}", file=sys.stderr)
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[Path] = []
    for f in files:
        resolved = str(f.resolve())
        if resolved not in seen:
            seen.add(resolved)
            unique.append(f)
    return unique


def process_batch(
    input_files: list[Path],
    output_dir: Path,
    skip_convert: bool = False,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Process a batch of files: convert (if needed) then extract."""
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for src in input_files:
        fmt = detect_format(src)
        docx_path: Path

        try:
            if fmt == "doc" and not skip_convert:
                print(f"converting: {src.name} → .docx", file=sys.stderr)
                target_path = _unique_path(output_dir / src.with_suffix(".docx").name)
                with tempfile.TemporaryDirectory(prefix="docx-batch-") as tmpdir:
                    converted_path = convert_doc_to_docx(src, Path(tmpdir))
                    shutil.move(str(converted_path), target_path)
                docx_path = target_path
            elif fmt in ("docx", "docm"):
                if src.parent.resolve() != output_dir.resolve():
                    # Copy to output dir so we don't modify the original
                    docx_path = _unique_path(output_dir / src.name)
                    shutil.copy2(src, docx_path)
                else:
                    docx_path = src
            elif fmt == "doc" and skip_convert:
                print(f"warning: skipping .doc file because --no-convert was used: {src.name}",
                      file=sys.stderr)
                errors.append({"file": src.name, "error": "skipped .doc file"})
                continue
            else:
                print(f"warning: unsupported format ({fmt}): {src.name}", file=sys.stderr)
                errors.append({"file": src.name, "error": f"unsupported format: {fmt}"})
                continue

            print(f"extracting: {docx_path.name}", file=sys.stderr)
            structure = extract_structure(docx_path, src)

            # Write individual JSON
            json_path = _unique_path(output_dir / (Path(docx_path).stem + ".json"))
            json_path.write_text(
                json.dumps(structure, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            print(f"  → {json_path}", file=sys.stderr)

            results.append(structure)

        except Exception as exc:
            print(f"error processing {src.name}: {exc}", file=sys.stderr)
            errors.append({"file": src.name, "error": str(exc)})

    # Write combined index if requested
    index_data: dict[str, Any] = {
        "total_files": len(results),
        "total_errors": len(errors),
        "documents": results,
    }
    if errors:
        index_data["errors"] = errors

    if index_path:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            json.dumps(index_data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        print(f"\nindex written → {index_path}", file=sys.stderr)

    return index_data


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="batch-extract.py",
        description=(
            "Convert .doc files to .docx and extract full document structure "
            "(headings, tables, comments, footnotes) into JSON for indexing."
        ),
    )
    parser.add_argument(
        "inputs", nargs="+",
        help="Input file(s), directory, or glob patterns",
    )
    parser.add_argument(
        "-o", "--output", required=True,
        help="Output directory for .docx and .json files",
    )
    parser.add_argument(
        "--index", metavar="FILE",
        help="Write a combined JSON index of all extracted documents",
    )
    parser.add_argument(
        "--no-convert", action="store_true",
        help="Skip .doc → .docx conversion (only extract from .docx files)",
    )
    args = parser.parse_args()

    if not _HAS_DOCX:
        fail(3, "python-docx not installed; run: pip install 'python-docx>=1.2.0'")

    input_files = collect_input_files(args.inputs)
    if not input_files:
        fail(2, "no input files found")

    output_dir = Path(args.output)
    index_path = Path(args.index) if args.index else None

    print(f"processing {len(input_files)} file(s) → {output_dir}", file=sys.stderr)
    process_batch(input_files, output_dir, args.no_convert, index_path)
    print("done.", file=sys.stderr)


if __name__ == "__main__":
    main()
