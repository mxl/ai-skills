#!/usr/bin/env python3
"""
inspect.py — Inspect a .pptx/.ppt file and report format, safety, metadata,
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
# Namespace shortcuts
# ---------------------------------------------------------------------------

P  = NAMESPACES["p"]
A  = NAMESPACES["a"]
R  = NAMESPACES["r"]
CP = NAMESPACES["cp"]
DC = NAMESPACES["dc"]
DCTERMS = NAMESPACES["dcterms"]
APP = NAMESPACES["app"]
REL_NS = NAMESPACES["rel"]
REL_TAG = f"{{{REL_NS}}}Relationship"


def _tag(ns: str, local: str) -> str:
    return f"{{{ns}}}{local}"


# ---------------------------------------------------------------------------
# XML reading helpers
# ---------------------------------------------------------------------------

def _read_xml(zf: zipfile.ZipFile, name: str):
    try:
        data = zf.read(name)
        return ET.fromstring(data)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core + app properties
# ---------------------------------------------------------------------------

def _read_core_props(zf: zipfile.ZipFile) -> dict[str, Any]:
    root = _read_xml(zf, "docProps/core.xml")
    if root is None:
        return {}
    fields = {
        "creator":        f"{{{DC}}}creator",
        "lastModifiedBy": f"{{{CP}}}lastModifiedBy",
        "created":        f"{{{DCTERMS}}}created",
        "modified":       f"{{{DCTERMS}}}modified",
        "revision":       f"{{{CP}}}revision",
        "description":    f"{{{DC}}}description",
        "subject":        f"{{{DC}}}subject",
        "title":          f"{{{DC}}}title",
        "keywords":       f"{{{CP}}}keywords",
        "category":       f"{{{CP}}}category",
        "language":       f"{{{DC}}}language",
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
        "application":  f"{{{APP}}}Application",
        "appVersion":   f"{{{APP}}}AppVersion",
        "company":      f"{{{APP}}}Company",
        "presentationFormat": f"{{{APP}}}PresentationFormat",
        "slides":       f"{{{APP}}}Slides",
        "notes":        f"{{{APP}}}Notes",
        "hiddenSlides": f"{{{APP}}}HiddenSlides",
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

def _read_rels(zf: zipfile.ZipFile, rels_path: str) -> list[dict[str, str]]:
    root = _read_xml(zf, rels_path)
    if root is None:
        return []
    result = []
    for el in root.iter(REL_TAG):
        entry: dict[str, str] = {
            "id":        el.get("Id", ""),
            "type":      el.get("Type", "").split("/")[-1],
            "type_full": el.get("Type", ""),
            "target":    el.get("Target", ""),
        }
        tm = el.get("TargetMode")
        if tm:
            entry["targetMode"] = tm
        result.append(entry)
    return result


def _all_rels(zf: zipfile.ZipFile) -> list[dict[str, str]]:
    rels: list[dict[str, str]] = []
    for name in zf.namelist():
        if name.endswith(".rels"):
            for r in _read_rels(zf, name):
                r["source"] = name
                rels.append(r)
    return rels


# ---------------------------------------------------------------------------
# Slide count
# ---------------------------------------------------------------------------

def _slide_count(zf: zipfile.ZipFile) -> int:
    root = _read_xml(zf, "ppt/presentation.xml")
    if root is None:
        return 0
    sld_id_lst_tag = f"{{{P}}}sldIdLst"
    sld_id_tag     = f"{{{P}}}sldId"
    sld_id_lst = root.find(f".//{sld_id_lst_tag}")
    if sld_id_lst is None:
        return 0
    return sum(1 for el in sld_id_lst if el.tag == sld_id_tag)


# ---------------------------------------------------------------------------
# Content flags
# ---------------------------------------------------------------------------

def _scan_flags(zf: zipfile.ZipFile, names: set[str]) -> dict[str, Any]:
    """Scan for content flags without full extraction."""
    flags: dict[str, Any] = {
        "has_macros": False,
        "has_external_links": False,
        "has_speaker_notes": False,
        "has_comments": False,
        "has_embedded_objects": False,
        "has_charts": False,
        "has_media": False,
        "slide_count": 0,
    }

    # Macros
    flags["has_macros"] = (
        "vbaProject.bin" in names
        or any(n.endswith("vbaProject.bin") for n in names)
    )

    # External links (any rel with TargetMode=External)
    for name in names:
        if name.endswith(".rels"):
            root = _read_xml(zf, name)
            if root is None:
                continue
            for el in root.iter(REL_TAG):
                if el.get("TargetMode") == "External":
                    flags["has_external_links"] = True
                    break

    # Speaker notes
    flags["has_speaker_notes"] = any(
        n.startswith("ppt/notesSlides/") and n.endswith(".xml") for n in names
    )

    # Comments (ppt/comments/ directory)
    flags["has_comments"] = any(
        n.startswith("ppt/comments/") for n in names
    )

    # Embedded objects
    flags["has_embedded_objects"] = any(
        n.startswith("ppt/embeddings/") for n in names
    )

    # Charts
    flags["has_charts"] = any(
        n.startswith("ppt/charts/") for n in names
    )

    # Media
    flags["has_media"] = any(
        n.startswith("ppt/media/") for n in names
    )

    # Slide count
    try:
        flags["slide_count"] = _slide_count(zf)
    except Exception:
        pass

    return flags


# ---------------------------------------------------------------------------
# Parts listing
# ---------------------------------------------------------------------------

def _list_parts(zf: zipfile.ZipFile) -> list[dict[str, Any]]:
    return [
        {"name": info.filename, "compressed_size": info.compress_size, "size": info.file_size}
        for info in zf.infolist()
    ]


# ---------------------------------------------------------------------------
# Main inspection
# ---------------------------------------------------------------------------

def inspect_pptx(path: Path) -> dict[str, Any]:
    fmt = detect_format(path)
    result: dict[str, Any] = {
        "file": str(path),
        "format": fmt,
    }

    if fmt == "ppt":
        result["zip_safety"] = {"ok": False, "issues": ["OLE .ppt format — not a ZIP"]}
        result["flags"] = {"unsupported_for_inspection": True}
        result["note"] = (
            "Legacy .ppt (OLE) format. Convert to .pptx first: "
            "scripts/convert.py input.ppt -o output.pptx"
        )
        return result

    if fmt == "unknown":
        result["zip_safety"] = {"ok": False, "issues": ["unknown format"]}
        result["flags"] = {}
        return result

    safety = zip_safety_report(path)
    result["zip_safety"] = safety

    if not safety["ok"]:
        result["flags"] = {"zip_unsafe": True}
        result["note"] = "ZIP safety check failed; use --force with safe-unpack.py to override"
        return result

    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = set(zf.namelist())

            result["metadata"] = {**_read_core_props(zf), **_read_app_props(zf)}
            result["parts"] = _list_parts(zf)
            result["media_count"] = sum(1 for n in names if n.startswith("ppt/media/"))
            result["relationships"] = _all_rels(zf)
            result["flags"] = _scan_flags(zf, names)

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
        description="Inspect a .pptx/.ppt file and output a JSON report.",
    )
    parser.add_argument("file", help="Path to .pptx or .ppt file")
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
        report = inspect_pptx(path)
    except Exception as exc:
        fail(1, f"inspection failed: {exc}")

    emit_json(report)


if __name__ == "__main__":
    main()
