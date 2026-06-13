#!/usr/bin/env python3
"""
safe-unpack.py — Safely unpack a .docx file, pretty-printing XML and
optionally merging adjacent runs.

Exit codes:
  0  success
  1  safety check failed (use --force to override)
  2  usage error
  3  unsupported format
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import zipfile
from io import BytesIO
from pathlib import Path

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
fail = _common_mod.fail
sha256_file = _common_mod.sha256_file
zip_safety_report = _common_mod.zip_safety_report

try:
    import defusedxml.minidom as minidom
    import defusedxml.ElementTree as ET
    _DEFUSEDXML = True
except ImportError:
    import xml.dom.minidom as minidom  # type: ignore[no-redef]
    import xml.etree.ElementTree as ET  # type: ignore[no-redef]
    _DEFUSEDXML = False


# ---------------------------------------------------------------------------
# XML pretty-printing
# ---------------------------------------------------------------------------

def _pretty_print_xml(data: bytes) -> bytes:
    """Pretty-print XML bytes with 2-space indent. Returns original on parse error."""
    try:
        if _DEFUSEDXML:
            dom = minidom.parseString(data)
        else:
            dom = minidom.parseString(data)
        pretty = dom.toprettyxml(indent="  ", encoding="UTF-8")
        # toprettyxml adds <?xml ...?> header — keep only if original had one
        lines = pretty.decode("utf-8").splitlines()
        # Remove the extra blank lines minidom introduces
        cleaned = "\n".join(line for line in lines if line.strip())
        return cleaned.encode("utf-8")
    except Exception:
        return data


# ---------------------------------------------------------------------------
# Run merging
# ---------------------------------------------------------------------------

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _clark(local: str) -> str:
    return f"{{{W_NS}}}{local}"


_TAG_P   = _clark("p")
_TAG_R   = _clark("r")
_TAG_RPR = _clark("rPr")
_TAG_T   = _clark("t")
_TAG_INS = _clark("ins")
_TAG_DEL = _clark("del")

# These child tags on a run mean it is NOT a plain text run; don't merge.
_COMPLEX_RUN_CHILDREN = {
    _clark("drawing"),
    _clark("br"),
    _clark("tab"),
    _clark("lastRenderedPageBreak"),
    _clark("ptab"),
    _clark("pgNum"),
    _clark("cr"),
    _clark("noBreakHyphen"),
    _clark("softHyphen"),
    _clark("sym"),
    _clark("fldChar"),
    _clark("instrText"),
    _clark("endnoteReference"),
    _clark("footnoteReference"),
    _clark("commentReference"),
    _clark("commentRangeStart"),
    _clark("commentRangeEnd"),
    _clark("delText"),
    _clark("delInstrText"),
    _clark("rPrChange"),
}


def _rpr_key(run_el) -> str:
    """Canonical string key for a run's rPr (for merge comparison)."""
    rpr = run_el.find(_TAG_RPR)
    if rpr is None:
        return ""
    try:
        return ET.tostring(rpr, encoding="unicode")
    except Exception:
        return ""


def _is_plain_run(run_el) -> bool:
    """True if run contains only rPr + w:t children (safe to merge)."""
    for child in run_el:
        if child.tag == _TAG_RPR:
            continue
        if child.tag == _TAG_T:
            continue
        if child.tag in _COMPLEX_RUN_CHILDREN:
            return False
        # Unknown children — be conservative
        return False
    return True


def _merge_runs_in_paragraph(p_el) -> None:
    """Merge adjacent runs with identical rPr within a paragraph element."""
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
            # Merge: append nxt's w:t text into curr's w:t
            curr_t = curr.find(_TAG_T)
            nxt_t  = nxt.find(_TAG_T)

            if curr_t is None:
                curr_t = ET.SubElement(curr, _TAG_T)
                curr_t.text = ""
            if nxt_t is None:
                nxt_t_text = ""
            else:
                nxt_t_text = nxt_t.text or ""

            combined = (curr_t.text or "") + nxt_t_text

            # Preserve xml:space="preserve" if either had leading/trailing space
            if combined != combined.strip():
                curr_t.set(
                    "{http://www.w3.org/XML/1998/namespace}space", "preserve"
                )
            curr_t.text = combined

            # Remove nxt from parent
            p_el.remove(nxt)
            children.pop(i + 1)
            # Don't advance i — try merging curr with the new next
        else:
            i += 1


def _merge_runs_in_xml_bytes(data: bytes) -> bytes:
    """Parse XML, merge adjacent runs, re-serialise."""
    try:
        root = ET.fromstring(data)
    except Exception:
        return data

    # Merge within all w:p elements (including nested in tables etc.)
    for p in root.iter(_TAG_P):
        _merge_runs_in_paragraph(p)
        # Also merge inside w:ins / w:del blocks within the paragraph
        for revision_el in p:
            if revision_el.tag in (_TAG_INS, _TAG_DEL):
                _merge_runs_in_paragraph(revision_el)

    try:
        # Preserve XML declaration
        return ET.tostring(root, encoding="unicode").encode("utf-8")
    except Exception:
        return data


# ---------------------------------------------------------------------------
# Safe extraction
# ---------------------------------------------------------------------------

def _safe_member_path(outdir: Path, member_name: str) -> Path:
    """
    Resolve member path inside outdir; raise ValueError on path traversal.
    """
    target = (outdir / member_name).resolve()
    if not str(target).startswith(str(outdir.resolve())):
        raise ValueError(f"path traversal attempt: {member_name!r}")
    return target


_XML_EXTENSIONS = {".xml", ".rels"}


def unpack(
    src: Path,
    outdir: Path,
    merge_runs: bool = True,
    force: bool = False,
) -> dict:
    """
    Unpack src into outdir. Returns a meta dict written to .docx-meta.json.
    """
    safety = zip_safety_report(src)
    if not safety["ok"] and not force:
        print(
            f"error: ZIP safety check failed:\n"
            + "\n".join(f"  - {i}" for i in safety["issues"]),
            file=sys.stderr,
        )
        sys.exit(1)
    elif not safety["ok"] and force:
        print(
            "warning: ZIP safety issues (--force active):\n"
            + "\n".join(f"  - {i}" for i in safety["issues"]),
            file=sys.stderr,
        )

    if outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True)

    with zipfile.ZipFile(src, "r") as zf:
        for info in zf.infolist():
            member_path = _safe_member_path(outdir, info.filename)

            if info.filename.endswith("/"):
                member_path.mkdir(parents=True, exist_ok=True)
                continue

            member_path.parent.mkdir(parents=True, exist_ok=True)
            data = zf.read(info.filename)

            suffix = Path(info.filename).suffix.lower()
            if suffix in _XML_EXTENSIONS:
                if merge_runs and info.filename in (
                    "word/document.xml",
                    "word/header1.xml", "word/header2.xml", "word/header3.xml",
                    "word/footer1.xml", "word/footer2.xml", "word/footer3.xml",
                    "word/comments.xml",
                ):
                    data = _merge_runs_in_xml_bytes(data)
                data = _pretty_print_xml(data)

            member_path.write_bytes(data)

    meta = {
        "source": str(src.resolve()),
        "sha256": sha256_file(src),
        "merge_runs": merge_runs,
        "format": detect_format(src),
    }
    (outdir / ".docx-meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    return meta


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="safe-unpack.py",
        description="Safely unpack a .docx file for XML editing.",
    )
    parser.add_argument("file", help="Source .docx file")
    parser.add_argument("outdir", help="Output directory (will be created/replaced)")
    parser.add_argument(
        "--merge-runs", dest="merge_runs", action="store_true", default=True,
        help="Merge adjacent runs with identical formatting (default: on)",
    )
    parser.add_argument(
        "--no-merge-runs", dest="merge_runs", action="store_false",
        help="Skip run merging",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Proceed despite ZIP safety warnings",
    )
    args = parser.parse_args()

    src = Path(args.file)
    if not src.exists():
        fail(2, f"file not found: {src}")

    fmt = detect_format(src)
    if fmt not in ("docx", "docm"):
        fail(3, f"unsupported format {fmt!r}; only .docx/.docm can be unpacked")

    outdir = Path(args.outdir)
    meta = unpack(src, outdir, merge_runs=args.merge_runs, force=args.force)

    print(
        f"unpacked {src} -> {outdir} "
        f"({'merge_runs' if meta['merge_runs'] else 'no merge_runs'})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
