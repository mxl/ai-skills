#!/usr/bin/env python3
"""
sanitize.py — Remove sensitive content from a .pptx file before sharing.

Categories:
  metadata         core/app properties (author, company, etc.)
  notes            speaker notes (notesSlides parts)
  comments         comments parts
  macros           vbaProject.bin and related parts
  embedded-objects embedded OLE objects
  external-rels    relationships with TargetMode="External"
  custom-xml       customXml parts

Use --remove all to remove everything above.

Exit codes:
  0  success
  1  sanitize failed
  2  usage error
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
import tempfile
import zipfile
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
zip_safety_report = _common_mod.zip_safety_report
NAMESPACES = _common_mod.NAMESPACES

try:
    import defusedxml.ElementTree as ET
except ImportError:
    import xml.etree.ElementTree as ET  # type: ignore[no-redef]

import xml.etree.ElementTree as StdET

ALL_CATEGORIES = [
    "metadata",
    "notes",
    "comments",
    "macros",
    "embedded-objects",
    "external-rels",
    "custom-xml",
]

CP_NS     = NAMESPACES["cp"]
DC_NS     = NAMESPACES["dc"]
DCTERMS   = NAMESPACES["dcterms"]
APP_NS    = NAMESPACES["app"]
REL_NS    = NAMESPACES["rel"]
REL_TAG   = f"{{{REL_NS}}}Relationship"


# ---------------------------------------------------------------------------
# Metadata sanitization
# ---------------------------------------------------------------------------

def _clear_core_xml(data: bytes) -> bytes:
    """Clear author-identifying fields from docProps/core.xml."""
    try:
        root = ET.fromstring(data)
    except Exception:
        return data

    fields_to_clear = [
        f"{{{DC_NS}}}creator",
        f"{{{CP_NS}}}lastModifiedBy",
        f"{{{DC_NS}}}description",
        f"{{{DC_NS}}}subject",
        f"{{{DC_NS}}}language",
        f"{{{CP_NS}}}keywords",
        f"{{{CP_NS}}}category",
        f"{{{CP_NS}}}contentStatus",
        f"{{{CP_NS}}}revision",
    ]
    # Keep: title, created, modified (structural)
    for el in root.iter():
        if el.tag in fields_to_clear:
            el.text = None

    try:
        return StdET.tostring(root, encoding="unicode", xml_declaration=True).encode("utf-8")
    except Exception:
        return data


def _clear_app_xml(data: bytes) -> bytes:
    """Clear company / manager from docProps/app.xml."""
    try:
        root = ET.fromstring(data)
    except Exception:
        return data

    fields_to_clear = [
        f"{{{APP_NS}}}Company",
        f"{{{APP_NS}}}Manager",
        f"{{{APP_NS}}}Template",
    ]
    for el in root.iter():
        if el.tag in fields_to_clear:
            el.text = None

    try:
        return StdET.tostring(root, encoding="unicode", xml_declaration=True).encode("utf-8")
    except Exception:
        return data


# ---------------------------------------------------------------------------
# Relationship filtering
# ---------------------------------------------------------------------------

def _strip_external_rels(data: bytes) -> tuple[bytes, list[str]]:
    """Remove External TargetMode relationships. Returns (new_data, removed_ids)."""
    try:
        root = ET.fromstring(data)
    except Exception:
        return data, []

    removed = []
    to_remove = []
    for el in root:
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "Relationship" and el.get("TargetMode") == "External":
            removed.append(el.get("Id", "?"))
            to_remove.append(el)
    for el in to_remove:
        root.remove(el)

    if removed:
        try:
            return StdET.tostring(root, encoding="unicode").encode("utf-8"), removed
        except Exception:
            return data, []
    return data, []


# ---------------------------------------------------------------------------
# Content-type update
# ---------------------------------------------------------------------------

def _remove_content_type(ct_data: bytes, part_name: str) -> bytes:
    """Remove Override for part_name from [Content_Types].xml."""
    try:
        root = ET.fromstring(ct_data)
    except Exception:
        return ct_data

    to_remove = []
    for el in root:
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "Override":
            pn = el.get("PartName", "").lstrip("/")
            if pn == part_name:
                to_remove.append(el)
    for el in to_remove:
        root.remove(el)

    try:
        return StdET.tostring(root, encoding="unicode").encode("utf-8")
    except Exception:
        return ct_data


# ---------------------------------------------------------------------------
# Core sanitize engine
# ---------------------------------------------------------------------------

def sanitize(
    src: Path,
    output: Path,
    categories: set[str],
) -> dict[str, Any]:
    """
    Produce a sanitized copy of src at output.
    Returns a report dict with removed/retained/output.
    """
    safety = zip_safety_report(src)
    if not safety["ok"]:
        fail(1, f"ZIP safety check failed: {'; '.join(safety['issues'][:3])}")

    removed: dict[str, list[str]] = {cat: [] for cat in ALL_CATEGORIES}
    retained: list[str] = []

    with zipfile.ZipFile(src, "r") as zin:
        names = set(zin.namelist())
        all_files: dict[str, bytes] = {n: zin.read(n) for n in names if not n.endswith("/")}

    # --- metadata ---
    if "metadata" in categories:
        if "docProps/core.xml" in all_files:
            all_files["docProps/core.xml"] = _clear_core_xml(all_files["docProps/core.xml"])
            removed["metadata"].append("docProps/core.xml fields (creator, lastModifiedBy, etc.)")
        if "docProps/app.xml" in all_files:
            all_files["docProps/app.xml"] = _clear_app_xml(all_files["docProps/app.xml"])
            removed["metadata"].append("docProps/app.xml fields (Company, Manager, Template)")
    else:
        retained.append("metadata")

    # --- notes ---
    if "notes" in categories:
        notes_parts = [n for n in list(all_files) if n.startswith("ppt/notesSlides/")]
        notes_rels  = [n for n in list(all_files)
                       if n.startswith("ppt/slides/_rels/") and n.endswith(".xml.rels")]
        for np in notes_parts:
            del all_files[np]
            # Remove content-type override
            if "[Content_Types].xml" in all_files:
                all_files["[Content_Types].xml"] = _remove_content_type(
                    all_files["[Content_Types].xml"], np
                )
            removed["notes"].append(np)
        # Strip notesSlide relationships from slide _rels
        _NOTES_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"
        for rels_name in notes_rels:
            if rels_name not in all_files:
                continue
            try:
                root = ET.fromstring(all_files[rels_name])
                to_remove = [el for el in root
                             if el.get("Type", "") == _NOTES_REL_TYPE]
                for el in to_remove:
                    root.remove(el)
                    removed["notes"].append(f"{rels_name}: removed notesSlide rel")
                all_files[rels_name] = StdET.tostring(root, encoding="unicode").encode("utf-8")
            except Exception:
                pass
    else:
        retained.append("notes")

    # --- comments ---
    if "comments" in categories:
        comment_parts = [n for n in list(all_files) if n.startswith("ppt/comments/")]
        for cp in comment_parts:
            del all_files[cp]
            if "[Content_Types].xml" in all_files:
                all_files["[Content_Types].xml"] = _remove_content_type(
                    all_files["[Content_Types].xml"], cp
                )
            removed["comments"].append(cp)
    else:
        retained.append("comments")

    # --- macros ---
    if "macros" in categories:
        macro_parts = [n for n in list(all_files)
                       if "vbaProject" in n]
        for mp in macro_parts:
            del all_files[mp]
            if "[Content_Types].xml" in all_files:
                all_files["[Content_Types].xml"] = _remove_content_type(
                    all_files["[Content_Types].xml"], mp
                )
            removed["macros"].append(mp)
    else:
        retained.append("macros")

    # --- embedded-objects ---
    if "embedded-objects" in categories:
        embed_parts = [n for n in list(all_files) if n.startswith("ppt/embeddings/")]
        for ep in embed_parts:
            del all_files[ep]
            if "[Content_Types].xml" in all_files:
                all_files["[Content_Types].xml"] = _remove_content_type(
                    all_files["[Content_Types].xml"], ep
                )
            removed["embedded-objects"].append(ep)
    else:
        retained.append("embedded-objects")

    # --- external-rels ---
    if "external-rels" in categories:
        for rels_name in [n for n in list(all_files) if n.endswith(".rels")]:
            new_data, stripped_ids = _strip_external_rels(all_files[rels_name])
            if stripped_ids:
                all_files[rels_name] = new_data
                for rid in stripped_ids:
                    removed["external-rels"].append(f"{rels_name}: {rid}")
    else:
        retained.append("external-rels")

    # --- custom-xml ---
    if "custom-xml" in categories:
        custom_parts = [n for n in list(all_files) if n.startswith("customXml/")]
        for xp in custom_parts:
            del all_files[xp]
            if "[Content_Types].xml" in all_files:
                all_files["[Content_Types].xml"] = _remove_content_type(
                    all_files["[Content_Types].xml"], xp
                )
            removed["custom-xml"].append(xp)
    else:
        retained.append("custom-xml")

    # Write output ZIP
    output.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    ordered = []
    if "[Content_Types].xml" in all_files:
        ordered.append("[Content_Types].xml")
    for name in sorted(all_files):
        if name != "[Content_Types].xml":
            ordered.append(name)

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name in ordered:
            zout.writestr(name, all_files[name])
    output.write_bytes(buf.getvalue())

    # Validate output
    validate_script = Path(__file__).parent / "validate.py"
    if validate_script.exists():
        import subprocess
        result = subprocess.run(
            [sys.executable, str(validate_script), str(output)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"warning: validation failed after sanitize: {result.stdout[:300]}",
                  file=sys.stderr)

    return {
        "input": str(src),
        "output": str(output),
        "removed": {k: v for k, v in removed.items() if v},
        "retained": retained,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sanitize.py",
        description="Remove sensitive content from a .pptx file.",
    )
    parser.add_argument("file", help="Input .pptx file")
    parser.add_argument("-o", "--output", required=True, help="Output .pptx path")
    parser.add_argument(
        "--remove",
        default="all",
        help=(
            "Comma-separated categories to remove, or 'all'. "
            f"Categories: {', '.join(ALL_CATEGORIES)}"
        ),
    )
    args = parser.parse_args()

    src = Path(args.file)
    if not src.exists():
        fail(2, f"file not found: {src}")

    fmt = detect_format(src)
    if fmt not in ("pptx", "pptm"):
        fail(2, f"unsupported format {fmt!r}; only .pptx/.pptm supported")

    if args.remove.strip().lower() == "all":
        categories = set(ALL_CATEGORIES)
    else:
        categories = {c.strip() for c in args.remove.split(",")}
        unknown = categories - set(ALL_CATEGORIES)
        if unknown:
            fail(2, f"unknown categories: {sorted(unknown)}. "
                    f"Valid: {ALL_CATEGORIES}")

    try:
        report = sanitize(src, Path(args.output), categories)
    except Exception as exc:
        fail(1, f"sanitize failed: {exc}")

    emit_json(report)


if __name__ == "__main__":
    main()
