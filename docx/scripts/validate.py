#!/usr/bin/env python3
"""
validate.py — Validate a .docx file: ZIP integrity, required parts,
content types, relationships, well-formed XML, tracked-changes nesting,
and comments consistency.

Exit codes:
  0  all checks passed
  1  one or more checks failed
  2  usage error
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

# Allow running as a standalone script from any working directory
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

W_NS    = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS    = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS  = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS   = "http://schemas.openxmlformats.org/package/2006/content-types"
OFFICE_DOCUMENT_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
DOCUMENT_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"

RELATIONSHIP_CONTENT_TYPES = {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument": {
        DOCUMENT_CONTENT_TYPE,
    },
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml",
    },
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml",
    },
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/webSettings": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.webSettings+xml",
    },
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/fontTable": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml",
    },
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml",
    },
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml",
    },
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml",
    },
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
    },
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
    },
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml",
    },
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme": {
        "application/vnd.openxmlformats-officedocument.theme+xml",
    },
}

_TAG_INS               = f"{{{W_NS}}}ins"
_TAG_DEL               = f"{{{W_NS}}}del"
_TAG_T                 = f"{{{W_NS}}}t"
_TAG_DEL_TEXT          = f"{{{W_NS}}}delText"
_TAG_COMMENT_REF       = f"{{{W_NS}}}commentReference"
_TAG_COMMENT_START     = f"{{{W_NS}}}commentRangeStart"
_TAG_COMMENT_END       = f"{{{W_NS}}}commentRangeEnd"
_TAG_CT_DEFAULT        = f"{{{CT_NS}}}Default"
_TAG_CT_OVERRIDE       = f"{{{CT_NS}}}Override"
_TAG_REL               = f"{{{REL_NS}}}Relationship"
_TAG_CT_TYPES          = f"{{{CT_NS}}}Types"


def _check(name: str, ok: bool, details: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "details": details}


def _content_type_maps(zf: zipfile.ZipFile) -> tuple[dict[str, str], dict[str, str]]:
    root = ET.fromstring(zf.read("[Content_Types].xml"))
    default_types: dict[str, str] = {}
    override_types: dict[str, str] = {}
    for el in root:
        if el.tag == _TAG_CT_DEFAULT:
            ext = el.get("Extension", "").lower()
            content_type = el.get("ContentType", "")
            if ext and content_type:
                default_types[ext] = content_type
        elif el.tag == _TAG_CT_OVERRIDE:
            part_name = el.get("PartName", "").lstrip("/")
            content_type = el.get("ContentType", "")
            if part_name and content_type:
                override_types[part_name] = content_type
    return default_types, override_types


def _part_content_type(name: str, default_types: dict[str, str], override_types: dict[str, str]) -> str | None:
    if name in override_types:
        return override_types[name]
    ext = Path(name).suffix.lstrip(".").lower()
    if ext:
        return default_types.get(ext)
    return None


def _resolve_relationship_target(rels_name: str, target: str) -> tuple[str | None, bool]:
    rels_dir = PurePosixPath(rels_name).parent
    base_dir = rels_dir.parent
    unresolved = PurePosixPath(target.lstrip("/")) if target.startswith("/") else base_dir / target
    stack: list[str] = []
    for part in unresolved.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                return None, True
            stack.pop()
        else:
            stack.append(part)
    return "/".join(stack), False


# ---------------------------------------------------------------------------
# Check 1: ZIP integrity
# ---------------------------------------------------------------------------

def check_zip_integrity(path: Path) -> dict[str, Any]:
    try:
        safety = zip_safety_report(path)
        if not safety["ok"]:
            return _check("zip_integrity", False, "; ".join(safety["issues"][:5]))
        with zipfile.ZipFile(path, "r") as zf:
            bad = zf.testzip()
            if bad:
                return _check("zip_integrity", False, f"corrupt entry: {bad}")
            return _check("zip_integrity", True)
    except zipfile.BadZipFile as exc:
        return _check("zip_integrity", False, str(exc))
    except Exception as exc:
        return _check("zip_integrity", False, str(exc))


# ---------------------------------------------------------------------------
# Check 2: Required parts
# ---------------------------------------------------------------------------

REQUIRED_PARTS = [
    "[Content_Types].xml",
    "_rels/.rels",
    "word/document.xml",
]


def check_required_parts(zf: zipfile.ZipFile) -> dict[str, Any]:
    names = set(zf.namelist())
    missing = [p for p in REQUIRED_PARTS if p not in names]
    if missing:
        return _check("required_parts", False, f"missing: {', '.join(missing)}")

    try:
        root = ET.fromstring(zf.read("_rels/.rels"))
    except Exception as exc:
        return _check("required_parts", False, f"cannot parse _rels/.rels: {exc}")

    office_targets: list[str] = []
    for rel in root.iter(_TAG_REL):
        if rel.get("Type") == OFFICE_DOCUMENT_REL:
            target = rel.get("Target", "")
            if target:
                office_targets.append(target.lstrip("/"))

    if "word/document.xml" not in office_targets:
        targets = ", ".join(office_targets) if office_targets else "none"
        return _check(
            "required_parts", False,
            f"_rels/.rels must target word/document.xml as officeDocument (found: {targets})",
        )

    return _check("required_parts", True)


# ---------------------------------------------------------------------------
# Check 3: Content types coverage
# ---------------------------------------------------------------------------

def check_content_types(zf: zipfile.ZipFile) -> dict[str, Any]:
    try:
        ct_data = zf.read("[Content_Types].xml")
        root = ET.fromstring(ct_data)
    except Exception as exc:
        return _check("content_types", False, f"cannot parse [Content_Types].xml: {exc}")

    if root.tag != _TAG_CT_TYPES:
        return _check("content_types", False, "[Content_Types].xml root is not ct:Types")

    # Collect extensions covered by Default and specific paths by Override
    default_exts: set[str] = set()
    override_paths: set[str] = set()
    override_types: dict[str, str] = {}
    for el in root:
        if el.tag == _TAG_CT_DEFAULT:
            ext = el.get("Extension", "").lower()
            if ext:
                default_exts.add(ext)
        elif el.tag == _TAG_CT_OVERRIDE:
            pn = el.get("PartName", "").lstrip("/")
            if pn:
                override_paths.add(pn)
                override_types[pn] = el.get("ContentType", "")

    doc_content_type = override_types.get("word/document.xml")
    if doc_content_type != DOCUMENT_CONTENT_TYPE:
        return _check(
            "content_types", False,
            "word/document.xml must have WordprocessingML document content type",
        )

    names = set(zf.namelist())
    uncovered = []
    for name in names:
        if name.endswith("/"):
            continue
        if name == "[Content_Types].xml":
            continue
        # .rels files are part of the packaging infrastructure; they are not
        # required to have a content-type entry in the spec.
        if name.endswith(".rels"):
            continue
        ext = Path(name).suffix.lstrip(".").lower()
        # Parts without an extension (e.g. _rels/.rels handled above) skip
        if not ext:
            continue
        if name not in override_paths and ext not in default_exts:
            uncovered.append(name)

    if uncovered:
        sample = uncovered[:5]
        return _check(
            "content_types", False,
            f"uncovered parts (first {len(sample)}): {', '.join(sample)}"
        )
    return _check("content_types", True)


# ---------------------------------------------------------------------------
# Check 4: Relationships resolve
# ---------------------------------------------------------------------------

def check_relationships(zf: zipfile.ZipFile) -> dict[str, Any]:
    names = set(zf.namelist())
    broken: list[str] = []

    for name in names:
        if not name.endswith(".rels"):
            continue
        try:
            data = zf.read(name)
            root = ET.fromstring(data)
        except Exception as exc:
            broken.append(f"{name}: parse error: {exc}")
            continue

        # Base path for relative targets
        rels_dir = Path(name).parent  # e.g. word/_rels
        base_dir = rels_dir.parent    # e.g. word

        for rel in root.iter(_TAG_REL):
            target_mode = rel.get("TargetMode", "Internal")
            if target_mode == "External":
                continue
            target = rel.get("Target", "")
            if not target:
                continue
            # Resolve target relative to base_dir, then normalise without ever
            # allowing internal relationships to escape the ZIP package root.
            unresolved = PurePosixPath(target.lstrip("/")) if target.startswith("/") else PurePosixPath(base_dir.as_posix()) / target
            stack: list[str] = []
            escaped_root = False
            for part in unresolved.parts:
                if part in ("", "."):
                    continue
                if part == "..":
                    if stack:
                        stack.pop()
                    else:
                        escaped_root = True
                        break
                else:
                    stack.append(part)
            if escaped_root:
                broken.append(f"{name}: target escapes package root {target!r}")
                continue
            resolved = "/".join(stack)

            if resolved not in names:
                broken.append(f"{name}: unresolved target {target!r} -> {resolved!r}")

    if broken:
        sample = broken[:5]
        return _check("relationships", False, "; ".join(sample))
    return _check("relationships", True)


# ---------------------------------------------------------------------------
# Check 5: Well-formed XML
# ---------------------------------------------------------------------------

def check_wellformed_xml(zf: zipfile.ZipFile) -> dict[str, Any]:
    bad: list[str] = []
    for name in zf.namelist():
        suffix = Path(name).suffix.lower()
        if suffix not in (".xml", ".rels"):
            continue
        try:
            data = zf.read(name)
            ET.fromstring(data)
        except Exception as exc:
            bad.append(f"{name}: {exc}")

    if bad:
        sample = bad[:5]
        return _check("wellformed_xml", False, "; ".join(sample))
    return _check("wellformed_xml", True)


# ---------------------------------------------------------------------------
# Check 6: Tracked-changes nesting
# ---------------------------------------------------------------------------

def check_tracked_changes(zf: zipfile.ZipFile) -> dict[str, Any]:
    issues: list[str] = []

    try:
        data = zf.read("word/document.xml")
        root = ET.fromstring(data)
    except Exception as exc:
        return _check("tracked_changes", False, f"cannot parse document.xml: {exc}")

    # Build parent map for ancestry checks
    parent_map: dict = {}
    for parent in root.iter():
        for child in parent:
            parent_map[child] = parent

    def _is_inside(el, tag: str) -> bool:
        current = parent_map.get(el)
        while current is not None:
            if current.tag == tag:
                return True
            current = parent_map.get(current)
        return False

    # w:delText not inside w:del
    for el in root.iter(_TAG_DEL_TEXT):
        if not _is_inside(el, _TAG_DEL):
            issues.append("<w:delText> found outside <w:del>")
            break

    # w:t inside w:del (should be w:delText instead)
    for del_el in root.iter(_TAG_DEL):
        for t_el in del_el.iter(_TAG_T):
            issues.append("<w:t> found inside <w:del> (should be <w:delText>)")
            break

    # w:ins / w:del must not be nested in another tracked insertion/deletion,
    # EXCEPT w:del inside w:ins (valid rejection pattern per OOXML spec).
    for el in root.iter():
        if el.tag not in (_TAG_INS, _TAG_DEL):
            continue
        current = parent_map.get(el)
        while current is not None:
            if current.tag in (_TAG_INS, _TAG_DEL):
                outer = current.tag.split("}")[-1]
                inner = el.tag.split("}")[-1]
                # w:del inside w:ins is the correct rejection pattern
                if inner == "del" and outer == "ins":
                    break
                issues.append(f"nested tracked change <w:{inner}> inside <w:{outer}>")
                break
            current = parent_map.get(current)

    # w:t should not be directly inside w:del (should be w:delText instead),
    # and w:delText should not appear directly inside an insertion
    # (but w:delText inside w:del-inside-w:ins is valid for rejection).
    for el in root.iter(_TAG_DEL_TEXT):
        if _is_inside(el, _TAG_INS) and not _is_inside(el, _TAG_DEL):
            issues.append("<w:delText> found directly inside <w:ins> (not via <w:del>)")
            break

    # w:ins / w:del must have w:id, w:author, w:date.
    seen_ids: set[str] = set()
    duplicate_ids: list[str] = []
    for el in root.iter():
        if el.tag in (_TAG_INS, _TAG_DEL):
            tag_name = el.tag.split("}")[-1]
            wid    = el.get(f"{{{W_NS}}}id")
            author = el.get(f"{{{W_NS}}}author")
            date   = el.get(f"{{{W_NS}}}date")
            if not wid:
                issues.append(f"<w:{tag_name}> missing w:id")
            if not author:
                issues.append(f"<w:{tag_name}> missing w:author")
            if not date:
                issues.append(f"<w:{tag_name}> missing w:date")
            if wid:
                if wid in seen_ids:
                    duplicate_ids.append(wid)
                else:
                    seen_ids.add(wid)

    if duplicate_ids:
        issues.append(f"duplicate w:id values in tracked changes: {duplicate_ids[:5]}")

    if issues:
        return _check("tracked_changes", False, "; ".join(issues[:5]))
    return _check("tracked_changes", True)


# ---------------------------------------------------------------------------
# Check 7: Comments consistency
# ---------------------------------------------------------------------------

def check_comments(zf: zipfile.ZipFile) -> dict[str, Any]:
    names = set(zf.namelist())
    if "word/comments.xml" not in names:
        return _check("comments", True, "no comments.xml (skipped)")

    issues: list[str] = []

    # Load comment ids from comments.xml
    try:
        data = zf.read("word/comments.xml")
        root = ET.fromstring(data)
        defined_ids = {
            el.get(f"{{{W_NS}}}id")
            for el in root
            if el.get(f"{{{W_NS}}}id") is not None
        }
    except Exception as exc:
        return _check("comments", False, f"cannot parse comments.xml: {exc}")

    # Load document.xml and check for matching range markers
    try:
        doc_data = zf.read("word/document.xml")
        doc_root = ET.fromstring(doc_data)
    except Exception as exc:
        return _check("comments", False, f"cannot parse document.xml: {exc}")

    starts = {
        el.get(f"{{{W_NS}}}id")
        for el in doc_root.iter(_TAG_COMMENT_START)
    }
    ends = {
        el.get(f"{{{W_NS}}}id")
        for el in doc_root.iter(_TAG_COMMENT_END)
    }
    refs = {
        el.get(f"{{{W_NS}}}id")
        for el in doc_root.iter(_TAG_COMMENT_REF)
    }

    for cid in refs:
        if cid not in defined_ids:
            issues.append(f"commentReference w:id={cid!r} not in comments.xml")
        if cid not in starts:
            issues.append(f"commentReference w:id={cid!r} missing commentRangeStart")
        if cid not in ends:
            issues.append(f"commentReference w:id={cid!r} missing commentRangeEnd")

    if issues:
        return _check("comments", False, "; ".join(issues[:5]))
    return _check("comments", True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def validate(path: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    # Check 1 — must pass before we open as ZipFile
    c1 = check_zip_integrity(path)
    checks.append(c1)
    if not c1["ok"]:
        return {"file": str(path), "ok": False, "checks": checks}

    with zipfile.ZipFile(path, "r") as zf:
        checks.append(check_required_parts(zf))
        checks.append(check_content_types(zf))
        checks.append(check_relationships(zf))
        checks.append(check_wellformed_xml(zf))
        checks.append(check_tracked_changes(zf))
        checks.append(check_comments(zf))

    overall = all(c["ok"] for c in checks)
    return {"file": str(path), "ok": overall, "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="validate.py",
        description="Validate a .docx file (ZIP, parts, rels, XML, tracked changes, comments).",
    )
    parser.add_argument("file", help="Path to .docx file")
    parser.add_argument(
        "--json", action="store_true", default=True,
        help="Output JSON report (default)",
    )
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        fail(2, f"file not found: {path}")

    report = validate(path)
    emit_json(report)
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
