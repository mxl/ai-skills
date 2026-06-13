#!/usr/bin/env python3
"""
pack.py — Repack an unpacked DOCX directory back into a .docx file.

Steps:
  1. Condense XML (remove pretty-print whitespace between elements).
  2. Auto-repair common issues (xml:space, invalid w:id values).
  3. Write deterministic ZIP (sorted entries, ZIP_DEFLATED).
  4. Validate the result (unless --no-validate).

Exit codes:
  0  success
  1  validation failed
  2  usage error
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
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
fail = _common_mod.fail

try:
    import defusedxml.ElementTree as ET
    _ET_PARSE = ET.fromstring
except ImportError:
    import xml.etree.ElementTree as ET  # type: ignore[no-redef]
    _ET_PARSE = ET.fromstring  # type: ignore[assignment]

import xml.etree.ElementTree as _StdET  # always use stdlib for serialisation


# ---------------------------------------------------------------------------
# XML condensing & auto-repair
# ---------------------------------------------------------------------------

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
_TAG_T  = f"{{{W_NS}}}t"
_TAG_W_ID_ATTRS = {"w:id", "id"}  # checked by local name below


def _condense_xml(data: bytes) -> bytes:
    """
    Re-serialise XML without pretty-print whitespace between elements.
    Preserves text content (including whitespace inside <w:t>).
    Returns original bytes on any parse error.
    """
    try:
        # Parse with stdlib (safe — file is already on disk, not user input
        # that could carry XXE; also defusedxml fromstring works here too)
        root = _ET_PARSE(data)
        return _StdET.tostring(root, encoding="unicode", xml_declaration=False).encode("utf-8")
    except Exception:
        return data


def _autorepair_xml(data: bytes, filename: str) -> tuple[bytes, list[str]]:
    """
    Apply auto-repair rules to XML bytes.
    Returns (repaired_bytes, list_of_repair_messages).
    """
    repairs: list[str] = []

    try:
        root = _ET_PARSE(data)
    except Exception:
        return data, repairs

    # Rule 1: add xml:space="preserve" to <w:t> with leading/trailing whitespace
    xml_space_attr = f"{{{XML_NS}}}space"
    for t_el in root.iter(_TAG_T):
        text = t_el.text or ""
        if text and (text[0] == " " or text[-1] == " "):
            if t_el.get(xml_space_attr) != "preserve":
                t_el.set(xml_space_attr, "preserve")
                repairs.append(
                    f"{filename}: added xml:space='preserve' to <w:t> with "
                    f"leading/trailing space: {text[:40]!r}"
                )

    # Rule 2: w:id attributes must be non-negative integers < 0x7FFFFFFF
    MAX_ID = 0x7FFFFFFF
    used_ids: set[int] = set()
    next_id = 1

    def _next_free_id() -> int:
        nonlocal next_id
        while next_id in used_ids:
            next_id += 1
        used_ids.add(next_id)
        return next_id

    # First pass: collect valid ids
    for el in root.iter():
        val = el.get(f"{{{W_NS}}}id")
        if val is not None:
            try:
                i = int(val)
                if 0 <= i < MAX_ID:
                    used_ids.add(i)
            except ValueError:
                pass

    # Second pass: fix invalid ids
    for el in root.iter():
        val = el.get(f"{{{W_NS}}}id")
        if val is not None:
            try:
                i = int(val)
                if i < 0 or i >= MAX_ID:
                    new_id = _next_free_id()
                    el.set(f"{{{W_NS}}}id", str(new_id))
                    repairs.append(
                        f"{filename}: replaced invalid w:id={val} with {new_id} "
                        f"in <{el.tag.split('}')[-1]}>"
                    )
            except ValueError:
                new_id = _next_free_id()
                el.set(f"{{{W_NS}}}id", str(new_id))
                repairs.append(
                    f"{filename}: replaced non-numeric w:id={val!r} with {new_id} "
                    f"in <{el.tag.split('}')[-1]}>"
                )

    try:
        return _StdET.tostring(root, encoding="unicode").encode("utf-8"), repairs
    except Exception:
        return data, repairs


# ---------------------------------------------------------------------------
# Missing parts from original
# ---------------------------------------------------------------------------

def _copy_missing_from_original(
    outdir: Path,
    original: Path,
    written_names: set[str],
) -> None:
    """Copy parts present in original but absent in outdir into the ZIP buffer."""
    # This is called in _build_zip; we return a dict name->bytes
    pass  # handled inline in _build_zip


# ---------------------------------------------------------------------------
# ZIP building
# ---------------------------------------------------------------------------

_XML_EXTENSIONS = {".xml", ".rels"}


def _build_zip(
    outdir: Path,
    original: Path | None,
    autorepair: bool,
) -> tuple[bytes, list[str]]:
    """
    Build ZIP bytes from outdir contents. Returns (zip_bytes, repair_messages).
    """
    repairs: list[str] = []
    buf = io.BytesIO()

    # Collect all files from outdir
    all_files: dict[str, bytes] = {}
    for path in sorted(outdir.rglob("*")):
        if not path.is_file():
            continue
        name = path.name
        if name == ".docx-meta.json":
            continue
        rel = path.relative_to(outdir).as_posix()
        data = path.read_bytes()

        suffix = Path(rel).suffix.lower()
        if suffix in _XML_EXTENSIONS and autorepair:
            data, r = _autorepair_xml(data, rel)
            repairs.extend(r)
        if suffix in _XML_EXTENSIONS:
            data = _condense_xml(data)

        all_files[rel] = data

    # Merge missing parts from original
    if original is not None and original.exists():
        try:
            with zipfile.ZipFile(original, "r") as ozf:
                for orig_name in ozf.namelist():
                    if orig_name not in all_files and not orig_name.endswith("/"):
                        all_files[orig_name] = ozf.read(orig_name)
        except Exception as exc:
            print(f"warning: could not read original for missing parts: {exc}", file=sys.stderr)

    # [Content_Types].xml must be first
    ordered: list[str] = []
    if "[Content_Types].xml" in all_files:
        ordered.append("[Content_Types].xml")
    for name in sorted(all_files):
        if name != "[Content_Types].xml":
            ordered.append(name)

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in ordered:
            zf.writestr(name, all_files[name])

    return buf.getvalue(), repairs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pack.py",
        description="Repack an unpacked DOCX directory into a .docx file.",
    )
    parser.add_argument("unpacked_dir", help="Directory produced by safe-unpack.py")
    parser.add_argument("output", help="Output .docx path")
    parser.add_argument(
        "--original", metavar="ORIGINAL.docx",
        help="Original .docx to fill in parts absent from unpacked_dir",
    )
    parser.add_argument(
        "--no-validate", action="store_true",
        help="Skip post-pack validation",
    )
    parser.add_argument(
        "--no-autorepair", action="store_true",
        help="Skip auto-repair of trivial XML issues",
    )
    parser.add_argument(
        "--keep-invalid", action="store_true",
        help="Keep output file even if validation fails",
    )
    args = parser.parse_args()

    unpacked = Path(args.unpacked_dir)
    if not unpacked.is_dir():
        fail(2, f"not a directory: {unpacked}")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    original = Path(args.original) if args.original else None
    autorepair = not args.no_autorepair

    zip_bytes, repairs = _build_zip(unpacked, original, autorepair)

    for msg in repairs:
        print(f"repair: {msg}", file=sys.stderr)

    output.write_bytes(zip_bytes)
    print(f"packed -> {output}", file=sys.stderr)

    if not args.no_validate:
        # Run validate.py as a subprocess to get an independent check
        script_dir = Path(__file__).parent
        validate_script = script_dir / "validate.py"
        if validate_script.exists():
            result = subprocess.run(
                [sys.executable, str(validate_script), str(output)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                print(result.stdout, file=sys.stderr)
                print(result.stderr, file=sys.stderr)
                if not args.keep_invalid:
                    output.unlink(missing_ok=True)
                    fail(1, "validation failed; output removed (use --keep-invalid to keep)")
                else:
                    print("warning: validation failed but --keep-invalid set", file=sys.stderr)
                    sys.exit(1)
            else:
                print("validation passed", file=sys.stderr)


if __name__ == "__main__":
    main()
