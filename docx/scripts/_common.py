"""
docx skill — format-specific common definitions.

Provides:
- detect_format()   — identifies docx / docm / doc / unknown
- NAMESPACES        — full namespace map (shared + WordprocessingML)
- WordProfile       — FormatProfile subclass for the shared OOXML engine
- Re-exports of shared utilities for backward compat with existing scripts
  that load _common.py directly.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

# Bootstrap: locate common/ooxml and fix sys.path.
import importlib.util as _ilu
_skillpath_path = Path(__file__).parent / '_skillpath.py'
_sp_spec = _ilu.spec_from_file_location('_skillpath', _skillpath_path)
_sp_mod = _ilu.module_from_spec(_sp_spec)
_sp_spec.loader.exec_module(_sp_mod)

from common.ooxml.engine import FormatProfile, CheckResult
from common.ooxml.zipsafe import zip_safety_report, safe_member_path, ZIP_LIMITS
from common.ooxml.xmlutil import (
    pretty_print_xml, condense_xml, parse_xml_bytes,
    register_namespaces as _register_shared,
    SHARED_NAMESPACES, XML_NS,
    ensure_xml_space_preserve,
)
from common.ooxml.io import sha256_file, emit_json, fail

try:
    import defusedxml.ElementTree as _ET
except ImportError:
    import xml.etree.ElementTree as _ET  # type: ignore[no-redef]

import xml.etree.ElementTree as _StdET

# ---------------------------------------------------------------------------
# Full namespace map (shared + WordprocessingML)
# ---------------------------------------------------------------------------

NAMESPACES: dict[str, str] = {
    **SHARED_NAMESPACES,
    "w":   "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp":  "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "w15": "http://schemas.microsoft.com/office/word/2012/wordml",
}

W_NS = NAMESPACES["w"]
XML_SPACE_ATTR = f"{{{XML_NS}}}space"


def register_namespaces() -> None:
    """Register all namespaces with stdlib ElementTree."""
    _register_shared(NAMESPACES)


def clark(prefix: str, local: str) -> str:
    """Return Clark-notation tag: {uri}local."""
    return f"{{{NAMESPACES[prefix]}}}{local}"


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

_OLE_MAGIC = b"\xd0\xcf\x11\xe0"
_ZIP_MAGIC = b"PK\x03\x04"


def detect_format(path: "str | Path") -> str:
    """
    Detect file format by magic bytes and internal structure.
    Returns: 'docx', 'docm', 'doc', 'unknown'.
    """
    path = Path(path)
    try:
        with open(path, "rb") as fh:
            magic = fh.read(8)
    except OSError:
        return "unknown"

    if magic[:4] == _OLE_MAGIC:
        return "doc"

    if magic[:4] == _ZIP_MAGIC:
        try:
            with zipfile.ZipFile(path, "r") as zf:
                names = set(zf.namelist())
                if "vbaProject.bin" in names or any(
                    n.endswith("vbaProject.bin") for n in names
                ):
                    return "docm"
                if "[Content_Types].xml" in names:
                    return "docx"
        except Exception:
            pass
        return "unknown"

    return "unknown"


# ---------------------------------------------------------------------------
# Run-merging (docx-specific: merge adjacent w:r with identical w:rPr)
# ---------------------------------------------------------------------------

_TAG_P   = clark("w", "p")
_TAG_R   = clark("w", "r")
_TAG_RPR = clark("w", "rPr")
_TAG_T   = clark("w", "t")
_TAG_INS = clark("w", "ins")
_TAG_DEL = clark("w", "del")

_COMPLEX_RUN_CHILDREN = {
    clark("w", "drawing"),
    clark("w", "br"),
    clark("w", "tab"),
    clark("w", "lastRenderedPageBreak"),
    clark("w", "ptab"),
    clark("w", "pgNum"),
    clark("w", "cr"),
    clark("w", "noBreakHyphen"),
    clark("w", "softHyphen"),
    clark("w", "sym"),
    clark("w", "fldChar"),
    clark("w", "instrText"),
    clark("w", "endnoteReference"),
    clark("w", "footnoteReference"),
    clark("w", "commentReference"),
    clark("w", "commentRangeStart"),
    clark("w", "commentRangeEnd"),
    clark("w", "delText"),
    clark("w", "delInstrText"),
    clark("w", "rPrChange"),
}

# XML parts in which run-merging is applied
_MERGE_PARTS = {
    "word/document.xml",
    "word/header1.xml", "word/header2.xml", "word/header3.xml",
    "word/footer1.xml", "word/footer2.xml", "word/footer3.xml",
    "word/comments.xml",
}


def _rpr_key(run_el) -> str:
    rpr = run_el.find(_TAG_RPR)
    if rpr is None:
        return ""
    try:
        return _StdET.tostring(rpr, encoding="unicode")
    except Exception:
        return ""


def _is_plain_run(run_el) -> bool:
    for child in run_el:
        if child.tag in (_TAG_RPR, _TAG_T):
            continue
        if child.tag in _COMPLEX_RUN_CHILDREN:
            return False
        return False
    return True


def _merge_runs_in_paragraph(p_el) -> None:
    children = list(p_el)
    if not children:
        return
    i = 0
    while i < len(children) - 1:
        curr = children[i]
        nxt  = children[i + 1]
        if (
            curr.tag == _TAG_R
            and nxt.tag == _TAG_R
            and _is_plain_run(curr)
            and _is_plain_run(nxt)
            and _rpr_key(curr) == _rpr_key(nxt)
        ):
            curr_t = curr.find(_TAG_T)
            nxt_t  = nxt.find(_TAG_T)
            if curr_t is None:
                curr_t = _StdET.SubElement(curr, _TAG_T)
                curr_t.text = ""
            nxt_t_text = nxt_t.text or "" if nxt_t is not None else ""
            combined = (curr_t.text or "") + nxt_t_text
            if combined != combined.strip():
                curr_t.set(XML_SPACE_ATTR, "preserve")
            curr_t.text = combined
            p_el.remove(nxt)
            children.pop(i + 1)
        else:
            i += 1


def _merge_runs_in_xml_bytes(data: bytes) -> bytes:
    try:
        root = _ET.fromstring(data)
    except Exception:
        return data
    for p in root.iter(_TAG_P):
        _merge_runs_in_paragraph(p)
        for rev_el in p:
            if rev_el.tag in (_TAG_INS, _TAG_DEL):
                _merge_runs_in_paragraph(rev_el)
    try:
        return _StdET.tostring(root, encoding="unicode").encode("utf-8")
    except Exception:
        return data


# ---------------------------------------------------------------------------
# WordProfile — FormatProfile for the shared engine
# ---------------------------------------------------------------------------

class WordProfile(FormatProfile):
    """FormatProfile for .docx/.docm files."""

    def __init__(self) -> None:
        super().__init__(
            name="docx",
            required_parts=[
                "[Content_Types].xml",
                "_rels/.rels",
                "word/document.xml",
            ],
            meta_filename=".docx-meta.json",
            xml_extensions={".xml", ".rels"},
        )

    def pre_write_transform(self, name: str, data: bytes) -> bytes:
        """Merge adjacent runs in key Word XML parts before pretty-printing."""
        if name in _MERGE_PARTS:
            return _merge_runs_in_xml_bytes(data)
        return data

    def autorepair(self, name: str, data: bytes) -> tuple[bytes, list[str]]:
        """
        Auto-repair:
        1. Add xml:space="preserve" to <w:t> with leading/trailing whitespace.
        2. Regenerate invalid w:id values.
        """
        repairs: list[str] = []
        try:
            root = _ET.fromstring(data)
        except Exception:
            return data, repairs

        # Rule 1: xml:space="preserve" on <w:t>
        ensure_xml_space_preserve(root, _TAG_T, repairs, name)

        # Rule 2: w:id must be non-negative integers < 0x7FFFFFFF
        MAX_ID = 0x7FFFFFFF
        used_ids: set[int] = set()
        next_id = 1

        def _next_free() -> int:
            nonlocal next_id
            while next_id in used_ids:
                next_id += 1
            used_ids.add(next_id)
            return next_id

        w_id_attr = f"{{{W_NS}}}id"
        for el in root.iter():
            val = el.get(w_id_attr)
            if val is not None:
                try:
                    i = int(val)
                    if 0 <= i < MAX_ID:
                        used_ids.add(i)
                except ValueError:
                    pass

        for el in root.iter():
            val = el.get(w_id_attr)
            if val is not None:
                try:
                    i = int(val)
                    if i < 0 or i >= MAX_ID:
                        new_id = _next_free()
                        el.set(w_id_attr, str(new_id))
                        repairs.append(
                            f"{name}: replaced invalid w:id={val} with {new_id} "
                            f"in <{el.tag.split('}')[-1]}>"
                        )
                except ValueError:
                    new_id = _next_free()
                    el.set(w_id_attr, str(new_id))
                    repairs.append(
                        f"{name}: replaced non-numeric w:id={val!r} with {new_id} "
                        f"in <{el.tag.split('}')[-1]}>"
                    )

        try:
            return _StdET.tostring(root, encoding="unicode").encode("utf-8"), repairs
        except Exception:
            return data, repairs

    def extra_checks(self, zf: zipfile.ZipFile) -> list[CheckResult]:
        """Docx-specific: tracked-changes nesting + comments consistency."""
        results = []
        results.append(_check_tracked_changes_docx(zf))
        results.append(_check_comments_docx(zf))
        return results


# ---------------------------------------------------------------------------
# Docx-specific validation helpers
# ---------------------------------------------------------------------------

_TAG_DEL_TEXT    = clark("w", "delText")
_TAG_COMMENT_REF = clark("w", "commentReference")
_TAG_COMMENT_START = clark("w", "commentRangeStart")
_TAG_COMMENT_END   = clark("w", "commentRangeEnd")


def _check_tracked_changes_docx(zf: zipfile.ZipFile) -> CheckResult:
    issues: list[str] = []
    try:
        data = zf.read("word/document.xml")
        root = _ET.fromstring(data)
    except Exception as exc:
        return CheckResult("tracked_changes", False, f"cannot parse document.xml: {exc}")

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

    for el in root.iter(_TAG_DEL_TEXT):
        if not _is_inside(el, _TAG_DEL):
            issues.append("<w:delText> found outside <w:del>")
            break

    for del_el in root.iter(_TAG_DEL):
        for t_el in del_el.iter(_TAG_T):
            issues.append("<w:t> found inside <w:del> (should be <w:delText>)")
            break

    seen_ids: set[str] = set()
    w_id_attr = f"{{{W_NS}}}id"
    for el in root.iter():
        if el.tag not in (_TAG_INS, _TAG_DEL):
            continue
        tag_name = el.tag.split("}")[-1]
        wid    = el.get(w_id_attr)
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
                issues.append(f"duplicate w:id={wid!r} in tracked changes")
            else:
                seen_ids.add(wid)

    if issues:
        return CheckResult("tracked_changes", False, "; ".join(issues[:5]))
    return CheckResult("tracked_changes", True)


def _check_comments_docx(zf: zipfile.ZipFile) -> CheckResult:
    names = set(zf.namelist())
    if "word/comments.xml" not in names:
        return CheckResult("comments", True, "no comments.xml (skipped)")

    issues: list[str] = []
    w_id_attr = f"{{{W_NS}}}id"
    try:
        data = zf.read("word/comments.xml")
        root = _ET.fromstring(data)
        defined_ids = {
            el.get(w_id_attr)
            for el in root
            if el.get(w_id_attr) is not None
        }
    except Exception as exc:
        return CheckResult("comments", False, f"cannot parse comments.xml: {exc}")

    try:
        doc_data = zf.read("word/document.xml")
        doc_root = _ET.fromstring(doc_data)
    except Exception as exc:
        return CheckResult("comments", False, f"cannot parse document.xml: {exc}")

    starts = {el.get(w_id_attr) for el in doc_root.iter(_TAG_COMMENT_START)}
    ends   = {el.get(w_id_attr) for el in doc_root.iter(_TAG_COMMENT_END)}
    refs   = {el.get(w_id_attr) for el in doc_root.iter(_TAG_COMMENT_REF)}

    for cid in refs:
        if cid not in defined_ids:
            issues.append(f"commentReference w:id={cid!r} not in comments.xml")
        if cid not in starts:
            issues.append(f"commentReference w:id={cid!r} missing commentRangeStart")
        if cid not in ends:
            issues.append(f"commentReference w:id={cid!r} missing commentRangeEnd")

    if issues:
        return CheckResult("comments", False, "; ".join(issues[:5]))
    return CheckResult("comments", True)


# Singleton profile instance for use by docx scripts
WORD_PROFILE = WordProfile()
