#!/usr/bin/env python3
"""
sanitize.py — Produce a sanitized copy of a .docx file.

Categories that can be removed:
  metadata         Core and app properties (author, company, revision, etc.)
  comments         word/comments.xml and all comment range markers
  revisions        Tracked changes (w:ins / w:del); use --accept-revisions or
                   --reject-revisions to control which side survives
  hidden-text      Runs with w:vanish
  custom-xml       customXml/ parts
  external-rels    Relationships with TargetMode="External" (hyperlink text kept)
  macros           vbaProject.bin + content-type downgrade to .docx
  embedded-objects word/embeddings/ parts and OLE references

Use --remove all  to apply every category.

Exit codes:
  0  success
  1  error
  2  usage error
"""
from __future__ import annotations

import argparse
import io
import json
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
detect_format = _common_mod.detect_format
emit_json = _common_mod.emit_json
fail = _common_mod.fail
zip_safety_report = _common_mod.zip_safety_report

try:
    import defusedxml.ElementTree as ET
    _PARSE = ET.fromstring
except ImportError:
    import xml.etree.ElementTree as ET  # type: ignore[no-redef]
    _PARSE = ET.fromstring  # type: ignore[assignment]

import xml.etree.ElementTree as _StdET

# ---------------------------------------------------------------------------
# Namespace helpers
# ---------------------------------------------------------------------------

W_NS    = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS    = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS  = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS   = "http://schemas.openxmlformats.org/package/2006/content-types"
CP_NS   = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC_NS   = "http://purl.org/dc/elements/1.1/"
DCTERMS_NS = "http://purl.org/dc/terms/"
APP_NS  = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"

ALL_CATEGORIES = [
    "metadata", "comments", "revisions", "hidden-text",
    "custom-xml", "external-rels", "macros", "embedded-objects",
]


def _w(local: str) -> str:
    return f"{{{W_NS}}}{local}"


def _rel(local: str) -> str:
    return f"{{{REL_NS}}}{local}"


# ---------------------------------------------------------------------------
# In-memory package representation
# ---------------------------------------------------------------------------

class DocxPackage:
    """In-memory DOCX package: dict of part_name -> bytes."""

    def __init__(self, path: Path) -> None:
        self.parts: dict[str, bytes] = {}
        with zipfile.ZipFile(path, "r") as zf:
            for name in zf.namelist():
                if not name.endswith("/"):
                    self.parts[name] = zf.read(name)

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        ordered = ["[Content_Types].xml"] + sorted(
            k for k in self.parts if k != "[Content_Types].xml"
        )
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name in ordered:
                if name in self.parts:
                    zf.writestr(name, self.parts[name])
        return buf.getvalue()

    def parse_xml(self, name: str):
        """Parse a part as XML. Returns (root, None) or (None, error_str)."""
        if name not in self.parts:
            return None, f"missing part: {name}"
        try:
            return _PARSE(self.parts[name]), None
        except Exception as exc:
            return None, str(exc)

    def set_xml(self, name: str, root) -> None:
        self.parts[name] = _StdET.tostring(
            root, encoding="unicode", xml_declaration=False
        ).encode("utf-8")

    def remove_part(self, name: str) -> bool:
        if name in self.parts:
            del self.parts[name]
            return True
        return False

    def remove_parts_prefix(self, prefix: str) -> list[str]:
        removed = [k for k in list(self.parts) if k.startswith(prefix)]
        for k in removed:
            del self.parts[k]
        return removed


# ---------------------------------------------------------------------------
# Removal operations
# ---------------------------------------------------------------------------

def _remove_metadata(pkg: DocxPackage) -> dict[str, Any]:
    removed_fields: list[str] = []
    retained_fields: list[str] = []

    # core.xml: blank sensitive fields, keep file valid
    CORE_SENSITIVE = [
        f"{{{CP_NS}}}lastModifiedBy",
        f"{{{DC_NS}}}creator",
        f"{{{DC_NS}}}description",
        f"{{{CP_NS}}}keywords",
        f"{{{CP_NS}}}category",
        f"{{{CP_NS}}}contentStatus",
        f"{{{CP_NS}}}revision",
    ]
    CORE_KEEP = [
        f"{{{DC_NS}}}title",
        f"{{{DC_NS}}}subject",
        f"{{{DC_NS}}}language",
        f"{{{DCTERMS_NS}}}created",
        f"{{{DCTERMS_NS}}}modified",
    ]

    root, err = pkg.parse_xml("docProps/core.xml")
    if root is not None:
        for child in list(root):
            if child.tag in CORE_SENSITIVE:
                child.text = ""
                removed_fields.append(child.tag.split("}")[-1])
            else:
                if child.text:
                    retained_fields.append(child.tag.split("}")[-1])
        pkg.set_xml("docProps/core.xml", root)

    # app.xml: remove Company, Manager; keep others
    APP_SENSITIVE = [
        f"{{{APP_NS}}}Company",
        f"{{{APP_NS}}}Manager",
        f"{{{APP_NS}}}HyperlinkBase",
    ]
    root, err = pkg.parse_xml("docProps/app.xml")
    if root is not None:
        for child in list(root):
            if child.tag in APP_SENSITIVE:
                child.text = ""
                removed_fields.append(child.tag.split("}")[-1])
        pkg.set_xml("docProps/app.xml", root)

    return {"removed": removed_fields, "retained": retained_fields}


def _remove_comments(pkg: DocxPackage) -> dict[str, Any]:
    removed_parts: list[str] = []
    comment_parts = [
        "word/comments.xml",
        "word/commentsExtended.xml",
        "word/commentsExtensible.xml",
        "word/commentsIds.xml",
        "word/people.xml",
    ]
    for p in comment_parts:
        if pkg.remove_part(p):
            removed_parts.append(p)

    # Remove from document.xml: commentRangeStart, commentRangeEnd, commentReference
    _strip_comment_markers(pkg, "word/document.xml")
    # Also headers and footers
    for name in list(pkg.parts):
        if (name.startswith("word/header") or name.startswith("word/footer")) and name.endswith(".xml"):
            _strip_comment_markers(pkg, name)

    # Remove rels pointing to comment parts
    _remove_rels_by_target_suffix(pkg, tuple(
        p.split("/")[-1] for p in comment_parts
    ))

    # Remove content-type overrides for comment parts
    _remove_ct_overrides(pkg, set(f"/{p}" for p in comment_parts))

    return {"removed_parts": removed_parts}


_COMMENT_MARKER_TAGS = {
    _w("commentRangeStart"),
    _w("commentRangeEnd"),
    _w("commentReference"),
}


def _strip_comment_markers(pkg: DocxPackage, part_name: str) -> None:
    root, err = pkg.parse_xml(part_name)
    if root is None:
        return
    changed = False
    for parent in root.iter():
        to_remove = [c for c in parent if c.tag in _COMMENT_MARKER_TAGS]
        for el in to_remove:
            parent.remove(el)
            changed = True
    if changed:
        pkg.set_xml(part_name, root)


def _remove_revisions(
    pkg: DocxPackage,
    accept: bool = False,
    reject: bool = False,
) -> dict[str, Any]:
    """
    Remove tracked changes from document.xml.
    accept=True: keep inserted text, discard deleted text.
    reject=True: keep deleted text (restore), discard inserted text.
    Neither: strip all revision markup, keeping accepted content.
    """
    if not accept and not reject:
        accept = True  # default: accept all

    root, err = pkg.parse_xml("word/document.xml")
    if root is None:
        return {"error": err}

    count_ins = [0]
    count_del = [0]

    def _process(parent) -> None:
        for child in list(parent):
            if child.tag == _w("ins"):
                count_ins[0] += 1
                if accept:
                    # Unwrap: move children of w:ins to parent in place
                    idx = list(parent).index(child)
                    for i, sub in enumerate(child):
                        parent.insert(idx + i, sub)
                parent.remove(child)
                # Re-process children that were moved in
                _process(parent)
                return  # restart iteration for this parent
            elif child.tag == _w("del"):
                count_del[0] += 1
                if reject:
                    # Restore: convert w:delText -> w:t and unwrap
                    for r in child:
                        for dt in list(r):
                            if dt.tag == _w("delText"):
                                dt.tag = _w("t")
                    idx = list(parent).index(child)
                    for i, sub in enumerate(child):
                        parent.insert(idx + i, sub)
                parent.remove(child)
                _process(parent)
                return
            else:
                _process(child)

    _process(root)

    # Also remove w:rPrChange, w:pPrChange, w:sectPrChange
    for change_tag in (_w("rPrChange"), _w("pPrChange"), _w("sectPrChange"), _w("tblPrChange")):
        for parent in root.iter():
            for child in list(parent):
                if child.tag == change_tag:
                    parent.remove(child)

    pkg.set_xml("word/document.xml", root)
    return {"accepted_insertions": count_ins[0], "removed_deletions": count_del[0]}


def _remove_hidden_text(pkg: DocxPackage) -> dict[str, Any]:
    root, err = pkg.parse_xml("word/document.xml")
    if root is None:
        return {"error": err}
    removed = 0
    for parent in root.iter():
        to_remove = []
        for child in parent:
            if child.tag == _w("r"):
                rPr = child.find(_w("rPr"))
                if rPr is not None and rPr.find(_w("vanish")) is not None:
                    to_remove.append(child)
        for el in to_remove:
            parent.remove(el)
            removed += 1
    pkg.set_xml("word/document.xml", root)
    return {"removed_runs": removed}


def _remove_custom_xml(pkg: DocxPackage) -> dict[str, Any]:
    removed = pkg.remove_parts_prefix("customXml/")
    _remove_rels_by_target_prefix(pkg, "customXml/")
    _remove_ct_overrides(pkg, {f"/{p}" for p in removed})
    return {"removed_parts": removed}


def _remove_external_rels(pkg: DocxPackage) -> dict[str, Any]:
    removed_rels: list[str] = []
    # Find all .rels parts
    for name in list(pkg.parts):
        if not name.endswith(".rels"):
            continue
        root, err = pkg.parse_xml(name)
        if root is None:
            continue
        to_remove = []
        for child in root:
            if child.get("TargetMode") == "External":
                to_remove.append(child)
                removed_rels.append(f"{name}: {child.get('Target', '')}")
        for el in to_remove:
            root.remove(el)
        pkg.set_xml(name, root)

    # In document.xml, unwrap w:hyperlink elements (keep text, remove the link)
    root, err = pkg.parse_xml("word/document.xml")
    if root is not None:
        _unwrap_hyperlinks(root)
        pkg.set_xml("word/document.xml", root)

    return {"removed_external_rels": removed_rels}


def _unwrap_hyperlinks(parent) -> None:
    for child in list(parent):
        if child.tag == _w("hyperlink"):
            idx = list(parent).index(child)
            for i, sub in enumerate(child):
                parent.insert(idx + i, sub)
            parent.remove(child)
            _unwrap_hyperlinks(parent)
            return
        else:
            _unwrap_hyperlinks(child)


def _remove_macros(pkg: DocxPackage) -> dict[str, Any]:
    removed = []
    macro_parts = [k for k in list(pkg.parts) if "vbaProject" in k]
    for p in macro_parts:
        pkg.remove_part(p)
        removed.append(p)
    _remove_rels_by_target_suffix(pkg, ("vbaProject.bin",))

    # Update content type of document part from macro-enabled to plain docx
    root, err = pkg.parse_xml("[Content_Types].xml")
    if root is not None:
        macro_ct = "application/vnd.ms-word.document.macroEnabled.main+xml"
        plain_ct = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
        for child in root:
            if child.get("ContentType") == macro_ct:
                child.set("ContentType", plain_ct)
        pkg.set_xml("[Content_Types].xml", root)

    return {"removed_parts": removed}


def _remove_embedded_objects(pkg: DocxPackage) -> dict[str, Any]:
    removed = pkg.remove_parts_prefix("word/embeddings/")
    _remove_rels_by_target_prefix(pkg, "embeddings/")

    # Remove oleObject references from document.xml
    root, err = pkg.parse_xml("word/document.xml")
    if root is not None:
        OLE_NS = "urn:schemas-microsoft-com:office:office"
        VML_NS = "urn:schemas-microsoft-com:vml"
        OLE_TAG = f"{{{OLE_NS}}}OLEObject"
        for parent in root.iter():
            for child in list(parent):
                if child.tag == OLE_TAG:
                    parent.remove(child)
        pkg.set_xml("word/document.xml", root)

    return {"removed_parts": removed}


# ---------------------------------------------------------------------------
# Helpers for .rels and content-types manipulation
# ---------------------------------------------------------------------------

def _remove_rels_by_target_suffix(pkg: DocxPackage, suffixes: tuple) -> None:
    for name in list(pkg.parts):
        if not name.endswith(".rels"):
            continue
        root, _ = pkg.parse_xml(name)
        if root is None:
            continue
        changed = False
        for child in list(root):
            target = child.get("Target", "")
            if any(target.endswith(s) for s in suffixes):
                root.remove(child)
                changed = True
        if changed:
            pkg.set_xml(name, root)


def _remove_rels_by_target_prefix(pkg: DocxPackage, prefix: str) -> None:
    for name in list(pkg.parts):
        if not name.endswith(".rels"):
            continue
        root, _ = pkg.parse_xml(name)
        if root is None:
            continue
        changed = False
        for child in list(root):
            target = child.get("Target", "")
            if target.startswith(prefix) or target.startswith("../"+prefix):
                root.remove(child)
                changed = True
        if changed:
            pkg.set_xml(name, root)


def _remove_ct_overrides(pkg: DocxPackage, part_names: set[str]) -> None:
    root, _ = pkg.parse_xml("[Content_Types].xml")
    if root is None:
        return
    changed = False
    for child in list(root):
        pn = child.get("PartName", "")
        if pn in part_names:
            root.remove(child)
            changed = True
    if changed:
        pkg.set_xml("[Content_Types].xml", root)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sanitize.py",
        description="Produce a sanitized copy of a .docx file.",
    )
    parser.add_argument("file", help="Source .docx file")
    parser.add_argument("-o", "--output", required=True, help="Output .docx path")
    parser.add_argument(
        "--remove",
        help=(
            "Comma-separated list of categories to remove, or 'all'. "
            f"Categories: {', '.join(ALL_CATEGORIES)}"
        ),
        default="",
    )
    parser.add_argument(
        "--accept-revisions", action="store_true",
        help="When removing revisions, accept insertions and discard deletions",
    )
    parser.add_argument(
        "--reject-revisions", action="store_true",
        help="When removing revisions, restore deletions and discard insertions",
    )
    args = parser.parse_args()

    src = Path(args.file)
    if not src.exists():
        fail(2, f"file not found: {src}")

    fmt = detect_format(src)
    if fmt not in ("docx", "docm"):
        fail(3, f"unsupported format {fmt!r}; only .docx/.docm supported")

    safety = zip_safety_report(src)
    if not safety["ok"]:
        fail(1, f"ZIP safety check failed: {safety['issues']}")

    if args.remove.strip().lower() == "all":
        categories = set(ALL_CATEGORIES)
    else:
        categories = {c.strip() for c in args.remove.split(",") if c.strip()}
        unknown = categories - set(ALL_CATEGORIES)
        if unknown:
            fail(2, f"unknown categories: {', '.join(unknown)}; valid: {', '.join(ALL_CATEGORIES)}")

    if not categories:
        fail(2, "specify --remove <categories> or --remove all")

    pkg = DocxPackage(src)
    report: dict[str, Any] = {"source": str(src), "output": args.output, "removed": {}}

    if "metadata" in categories:
        report["removed"]["metadata"] = _remove_metadata(pkg)

    if "comments" in categories:
        report["removed"]["comments"] = _remove_comments(pkg)

    if "revisions" in categories:
        report["removed"]["revisions"] = _remove_revisions(
            pkg,
            accept=args.accept_revisions,
            reject=args.reject_revisions,
        )

    if "hidden-text" in categories:
        report["removed"]["hidden_text"] = _remove_hidden_text(pkg)

    if "custom-xml" in categories:
        report["removed"]["custom_xml"] = _remove_custom_xml(pkg)

    if "external-rels" in categories:
        report["removed"]["external_rels"] = _remove_external_rels(pkg)

    if "macros" in categories:
        report["removed"]["macros"] = _remove_macros(pkg)

    if "embedded-objects" in categories:
        report["removed"]["embedded_objects"] = _remove_embedded_objects(pkg)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pkg.to_bytes())

    emit_json(report)


if __name__ == "__main__":
    main()
