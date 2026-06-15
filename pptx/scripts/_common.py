"""
pptx skill — format-specific common definitions.

Provides:
- detect_format()   — identifies pptx / pptm / ppt / unknown
- NAMESPACES        — full namespace map (shared + PresentationML)
- PptProfile        — FormatProfile subclass for the shared OOXML engine
- Re-exports of shared utilities for backward compat
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
# Full namespace map (shared + PresentationML / DrawingML extras)
# ---------------------------------------------------------------------------

NAMESPACES: dict[str, str] = {
    **SHARED_NAMESPACES,
    "p":   "http://schemas.openxmlformats.org/presentationml/2006/main",
    "p14": "http://schemas.microsoft.com/office/powerpoint/2010/main",
    "p15": "http://schemas.microsoft.com/office/powerpoint/2012/main",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "c":   "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
}

P_NS  = NAMESPACES["p"]
A_NS  = NAMESPACES["a"]
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
    Returns: 'pptx', 'pptm', 'ppt', 'unknown'.
    """
    path = Path(path)
    try:
        with open(path, "rb") as fh:
            magic = fh.read(8)
    except OSError:
        return "unknown"

    if magic[:4] == _OLE_MAGIC:
        return "ppt"

    if magic[:4] == _ZIP_MAGIC:
        try:
            with zipfile.ZipFile(path, "r") as zf:
                names = set(zf.namelist())
                if "vbaProject.bin" in names or any(
                    n.endswith("vbaProject.bin") for n in names
                ):
                    return "pptm"
                if "[Content_Types].xml" in names and any(
                    n.startswith("ppt/") for n in names
                ):
                    return "pptx"
        except Exception:
            pass
        return "unknown"

    return "unknown"


# ---------------------------------------------------------------------------
# PptProfile — FormatProfile for the shared engine
# ---------------------------------------------------------------------------

# The text element in PresentationML/DrawingML where xml:space is needed
_TAG_A_T = clark("a", "t")

# Clark tags for slide-id consistency check
_TAG_P_SLD_ID     = clark("p", "sldId")
_TAG_REL          = f"{{{NAMESPACES['rel']}}}Relationship"
_REL_SLIDE_TYPE   = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
_REL_LAYOUT_TYPE  = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
_REL_MASTER_TYPE  = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster"


class PptProfile(FormatProfile):
    """FormatProfile for .pptx/.pptm files."""

    def __init__(self) -> None:
        super().__init__(
            name="pptx",
            required_parts=[
                "[Content_Types].xml",
                "_rels/.rels",
                "ppt/presentation.xml",
            ],
            meta_filename=".pptx-meta.json",
            xml_extensions={".xml", ".rels"},
        )

    def pre_write_transform(self, name: str, data: bytes) -> bytes:
        """No run-merging for PPTX — pass through unchanged."""
        return data

    def autorepair(self, name: str, data: bytes) -> tuple[bytes, list[str]]:
        """
        Auto-repair:
        1. Add xml:space="preserve" to <a:t> with leading/trailing whitespace.
        2. Regenerate invalid p:id / r:id attribute values where possible.
        """
        repairs: list[str] = []
        try:
            root = _ET.fromstring(data)
        except Exception:
            return data, repairs

        # Rule 1: xml:space="preserve" on <a:t>
        ensure_xml_space_preserve(root, _TAG_A_T, repairs, name)

        try:
            return _StdET.tostring(root, encoding="unicode").encode("utf-8"), repairs
        except Exception:
            return data, repairs

    def extra_checks(self, zf: zipfile.ZipFile) -> list[CheckResult]:
        """PPTX-specific: slide/layout/master consistency."""
        results = []
        results.append(_check_slide_consistency(zf))
        results.append(_check_layout_master_chain(zf))
        return results


# ---------------------------------------------------------------------------
# PPTX-specific validation helpers
# ---------------------------------------------------------------------------

def _check_slide_consistency(zf: zipfile.ZipFile) -> CheckResult:
    """
    Every sldId r:id in presentation.xml must resolve to an existing slide part
    via presentation.xml.rels.
    """
    names = set(zf.namelist())
    issues: list[str] = []

    # Parse presentation.xml
    try:
        prs_data = zf.read("ppt/presentation.xml")
        prs_root = _ET.fromstring(prs_data)
    except Exception as exc:
        return CheckResult("slide_consistency", False, f"cannot parse presentation.xml: {exc}")

    # Parse presentation.xml.rels
    rels_path = "ppt/_rels/presentation.xml.rels"
    r_id_to_target: dict[str, str] = {}
    if rels_path in names:
        try:
            rels_data = zf.read(rels_path)
            rels_root = _ET.fromstring(rels_data)
            for el in rels_root:
                rel_id = el.get("Id", "")
                target = el.get("Target", "")
                rel_type = el.get("Type", "")
                if rel_id:
                    r_id_to_target[rel_id] = (target, rel_type)
        except Exception as exc:
            return CheckResult("slide_consistency", False, f"cannot parse presentation.xml.rels: {exc}")

    r_ns = NAMESPACES["r"]
    # Check sldIdLst entries
    for sld_id_el in prs_root.iter(_TAG_P_SLD_ID):
        r_id = sld_id_el.get(f"{{{r_ns}}}id", "")
        if not r_id:
            issues.append(f"<p:sldId> missing r:id")
            continue
        if r_id not in r_id_to_target:
            issues.append(f"sldId r:id={r_id!r} not in presentation.xml.rels")
            continue
        target, rel_type = r_id_to_target[r_id]
        # Resolve relative path
        resolved = f"ppt/{target}" if not target.startswith("/") else target.lstrip("/")
        resolved = resolved.replace("ppt/../", "")  # simple normalise
        if resolved not in names:
            issues.append(f"slide target {target!r} (r:id={r_id!r}) not found in ZIP")

    if issues:
        return CheckResult("slide_consistency", False, "; ".join(issues[:5]))
    return CheckResult("slide_consistency", True)


def _check_layout_master_chain(zf: zipfile.ZipFile) -> CheckResult:
    """
    Each slide must reference a slide layout via its _rels.
    Each slide layout must reference a slide master via its _rels.
    """
    names = set(zf.namelist())
    issues: list[str] = []

    slide_parts = [n for n in names if n.startswith("ppt/slides/slide") and n.endswith(".xml")]

    for slide_part in sorted(slide_parts):
        rels_name = slide_part.replace("ppt/slides/", "ppt/slides/_rels/") + ".rels"
        if rels_name not in names:
            issues.append(f"{slide_part}: missing _rels file {rels_name}")
            continue
        try:
            rels_data = zf.read(rels_name)
            rels_root = _ET.fromstring(rels_data)
        except Exception as exc:
            issues.append(f"{rels_name}: parse error: {exc}")
            continue

        has_layout = False
        for el in rels_root:
            if el.get("Type", "") == _REL_LAYOUT_TYPE:
                has_layout = True
                target = el.get("Target", "")
                # Resolve relative to ppt/slides/
                if target.startswith("../"):
                    resolved = "ppt/" + target[3:]
                else:
                    resolved = f"ppt/slides/{target}"
                if resolved not in names:
                    issues.append(f"{slide_part}: slideLayout target {target!r} not in ZIP")
                break
        if not has_layout:
            issues.append(f"{slide_part}: no slideLayout relationship")

    if len(issues) > 5:
        issues = issues[:5] + [f"... and {len(issues) - 5} more"]

    if issues:
        return CheckResult("layout_master_chain", False, "; ".join(issues))
    return CheckResult("layout_master_chain", True)


# Singleton profile instance for use by pptx scripts
PPT_PROFILE = PptProfile()
