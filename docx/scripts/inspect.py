#!/usr/bin/env python3
"""
inspect.py — Inspect a .docx/.doc file and report format, safety, metadata,
relationships, and content flags as JSON.

Exit codes:
  0  success (flags are data, not errors)
  1  file unreadable or corrupt
  2  usage error
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path
from typing import Any

# Remove skill-tree path entries that would shadow 'docx' (python-docx package).
import os as _os
_scripts_dir = str(Path(__file__).parent.resolve())
_skill_dir   = str(Path(__file__).parent.parent.resolve())
sys.path = [p for p in sys.path if _os.path.realpath(p or ".") not in (_scripts_dir, _skill_dir)]

# Load _common by absolute path so sys.path manipulation doesn't shadow
# third-party packages (python-docx) that share the 'docx' package name.
import importlib.util as _ilu
_common_path = Path(__file__).parent / '_common.py'
_spec = _ilu.spec_from_file_location('_common', _common_path)
_common_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_common_mod)
NAMESPACES = _common_mod.NAMESPACES
detect_format = _common_mod.detect_format
emit_json = _common_mod.emit_json
fail = _common_mod.fail
zip_safety_report = _common_mod.zip_safety_report

try:
    import defusedxml.ElementTree as ET
except ImportError:
    import xml.etree.ElementTree as ET  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Namespace helpers
# ---------------------------------------------------------------------------

W  = NAMESPACES["w"]
R  = NAMESPACES["r"]
REL = NAMESPACES["rel"]
CP = NAMESPACES["cp"]
DC = NAMESPACES["dc"]
DCTERMS = NAMESPACES["dcterms"]
APP = NAMESPACES["app"]


def _tag(ns: str, local: str) -> str:
    return f"{{{ns}}}{local}"


# ---------------------------------------------------------------------------
# XML reading helpers (read from zip bytes, no disk extraction)
# ---------------------------------------------------------------------------

def _read_xml(zf: zipfile.ZipFile, name: str):
    """Parse an XML entry from an open ZipFile. Returns root Element or None."""
    try:
        data = zf.read(name)
        return ET.fromstring(data)
    except Exception:
        return None


def _read_xml_safe(zf: zipfile.ZipFile, name: str):
    """Return (root, error_str). error_str is None on success."""
    try:
        data = zf.read(name)
        root = ET.fromstring(data)
        return root, None
    except KeyError:
        return None, f"not found: {name}"
    except Exception as exc:
        return None, str(exc)


# ---------------------------------------------------------------------------
# Core properties
# ---------------------------------------------------------------------------

def _read_core_props(zf: zipfile.ZipFile) -> dict[str, Any]:
    root = _read_xml(zf, "docProps/core.xml")
    if root is None:
        return {}
    fields = {
        "creator":          f"{{{DC}}}creator",
        "lastModifiedBy":   f"{{{CP}}}lastModifiedBy",
        "created":          f"{{{DCTERMS}}}created",
        "modified":         f"{{{DCTERMS}}}modified",
        "revision":         f"{{{CP}}}revision",
        "description":      f"{{{DC}}}description",
        "subject":          f"{{{DC}}}subject",
        "title":            f"{{{DC}}}title",
        "keywords":         f"{{{CP}}}keywords",
        "category":         f"{{{CP}}}category",
        "contentStatus":    f"{{{CP}}}contentStatus",
        "language":         f"{{{DC}}}language",
    }
    result: dict[str, Any] = {}
    for key, tag in fields.items():
        el = root.find(tag)
        if el is not None and el.text:
            result[key] = el.text.strip()
    return result


def _read_app_props(zf: zipfile.ZipFile) -> dict[str, Any]:
    root = _read_xml(zf, "docProps/app.xml")
    if root is None:
        return {}
    fields = {
        "application":   f"{{{APP}}}Application",
        "appVersion":    f"{{{APP}}}AppVersion",
        "company":       f"{{{APP}}}Company",
        "manager":       f"{{{APP}}}Manager",
        "template":      f"{{{APP}}}Template",
        "pages":         f"{{{APP}}}Pages",
        "words":         f"{{{APP}}}Words",
        "characters":    f"{{{APP}}}Characters",
    }
    result: dict[str, Any] = {}
    for key, tag in fields.items():
        el = root.find(tag)
        if el is not None and el.text:
            result[key] = el.text.strip()
    return result


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------

REL_NS = NAMESPACES["rel"]
REL_TAG = f"{{{REL_NS}}}Relationship"


def _read_rels(zf: zipfile.ZipFile, rels_path: str) -> list[dict[str, str]]:
    root = _read_xml(zf, rels_path)
    if root is None:
        return []
    result = []
    for el in root.iter(REL_TAG):
        entry: dict[str, str] = {
            "id":     el.get("Id", ""),
            "type":   el.get("Type", "").split("/")[-1],  # short name
            "type_full": el.get("Type", ""),
            "target": el.get("Target", ""),
        }
        tm = el.get("TargetMode")
        if tm:
            entry["targetMode"] = tm
        result.append(entry)
    return result


def _all_rels(zf: zipfile.ZipFile) -> list[dict[str, str]]:
    names = zf.namelist()
    rels: list[dict[str, str]] = []
    for name in names:
        if name.endswith(".rels"):
            for r in _read_rels(zf, name):
                r["source"] = name
                rels.append(r)
    return rels


# ---------------------------------------------------------------------------
# Content flags (scan document.xml without full extraction)
# ---------------------------------------------------------------------------

def _scan_document_flags(zf: zipfile.ZipFile) -> dict[str, bool]:
    """
    Scan document.xml for presence of tracked changes, hidden text, comments,
    external hyperlinks. Streaming approach to avoid large memory use.
    """
    flags = {
        "has_tracked_changes": False,
        "has_hidden_text": False,
        "has_comment_refs": False,
    }
    try:
        data = zf.read("word/document.xml")
    except KeyError:
        return flags

    # Simple string checks first (fast path)
    if b"<w:ins " in data or b"<w:del " in data:
        flags["has_tracked_changes"] = True
    if b"<w:vanish" in data:
        flags["has_hidden_text"] = True
    if b"w:commentReference" in data or b"w:commentRangeStart" in data:
        flags["has_comment_refs"] = True
    return flags


# ---------------------------------------------------------------------------
# Parts listing
# ---------------------------------------------------------------------------

def _list_parts(zf: zipfile.ZipFile) -> list[dict[str, Any]]:
    parts = []
    for info in zf.infolist():
        parts.append({
            "name": info.filename,
            "compressed_size": info.compress_size,
            "size": info.file_size,
        })
    return parts


# ---------------------------------------------------------------------------
# Main inspection logic
# ---------------------------------------------------------------------------

def inspect_docx(path: Path) -> dict[str, Any]:
    fmt = detect_format(path)
    result: dict[str, Any] = {
        "file": str(path),
        "format": fmt,
    }

    if fmt == "doc":
        result["zip_safety"] = {"ok": False, "issues": ["OLE .doc format — not a ZIP"]}
        result["flags"] = {
            "unsupported_for_inspection": True,
        }
        result["note"] = (
            "Legacy .doc (OLE) format. Convert to .docx first: "
            "scripts/convert.py input.doc -o output.docx"
        )
        return result

    if fmt == "unknown":
        result["zip_safety"] = {"ok": False, "issues": ["unknown format"]}
        result["flags"] = {}
        return result

    # ZIP-based (docx / docm)
    safety = zip_safety_report(path)
    result["zip_safety"] = safety

    if not safety["ok"]:
        result["flags"] = {"zip_unsafe": True}
        result["note"] = "ZIP safety check failed; use --force with safe-unpack.py to override"
        return result

    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = set(zf.namelist())

            # Metadata
            core = _read_core_props(zf)
            app  = _read_app_props(zf)
            result["metadata"] = {**core, **app}

            # Parts
            result["parts"] = _list_parts(zf)
            result["media_count"] = sum(
                1 for n in names if n.startswith("word/media/")
            )

            # Relationships
            result["relationships"] = _all_rels(zf)

            # Document-level flags
            doc_flags = _scan_document_flags(zf)

            result["flags"] = {
                "has_macros": (
                    "vbaProject.bin" in names
                    or any(n.endswith("vbaProject.bin") for n in names)
                ),
                "has_external_links": any(
                    r.get("targetMode") == "External"
                    for r in result["relationships"]
                ),
                "has_comments": "word/comments.xml" in names,
                "has_tracked_changes": doc_flags["has_tracked_changes"],
                "has_hidden_text": doc_flags["has_hidden_text"],
                "has_comment_refs": doc_flags["has_comment_refs"],
                "has_embedded_objects": any(
                    n.startswith("word/embeddings/") for n in names
                ),
                "has_custom_xml": any(
                    n.startswith("customXml/") for n in names
                ),
            }

    except zipfile.BadZipFile as exc:
        result["flags"] = {"zip_corrupt": True}
        result["error"] = str(exc)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="inspect.py",
        description="Inspect a .docx/.doc file and output a JSON report.",
    )
    parser.add_argument("file", help="Path to .docx or .doc file")
    parser.add_argument(
        "--json", action="store_true", default=True,
        help="Output JSON (default; flag kept for explicitness)",
    )
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        fail(2, f"file not found: {path}")
    if not path.is_file():
        fail(2, f"not a file: {path}")

    try:
        report = inspect_docx(path)
    except Exception as exc:
        fail(1, f"inspection failed: {exc}")

    emit_json(report)


if __name__ == "__main__":
    main()
