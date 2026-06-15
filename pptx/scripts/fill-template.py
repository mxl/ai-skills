#!/usr/bin/env python3
"""
fill-template.py — Fill placeholder text in a .pptx template from a JSON data file.

Replaces {{key}} tokens in all text frames across all slides.

Exit codes:
  0  success
  1  fill failed
  2  usage error
  3  missing dependency
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import importlib.util as _ilu
_common_path = Path(__file__).parent / '_common.py'
_spec = _ilu.spec_from_file_location('_common', _common_path)
_common_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_common_mod)

detect_format = _common_mod.detect_format
emit_json = _common_mod.emit_json
fail = _common_mod.fail

try:
    from pptx import Presentation
    _PPTX_AVAILABLE = True
except ImportError:
    _PPTX_AVAILABLE = False


_TOKEN_RE = re.compile(r"\{\{([^}]+)\}\}")


def _fill_text_frame(tf, data: dict) -> list[str]:
    """Replace {{key}} tokens in a text frame. Returns list of missing keys."""
    missing: list[str] = []
    for para in tf.paragraphs:
        for run in para.runs:
            def _replace(m: re.Match) -> str:
                key = m.group(1).strip()
                if key not in data:
                    missing.append(key)
                    return m.group(0)
                return str(data[key])
            run.text = _TOKEN_RE.sub(_replace, run.text)
    return missing


def fill_template(template_path: Path, data: dict, output_path: Path) -> dict:
    """Fill all {{key}} placeholders in template_path and save to output_path."""
    prs = Presentation(str(template_path))
    all_missing: list[str] = []

    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                missing = _fill_text_frame(shape.text_frame, data)
                all_missing.extend(missing)
            # Notes
            try:
                notes_tf = slide.notes_slide.notes_text_frame
                if notes_tf:
                    missing = _fill_text_frame(notes_tf, data)
                    all_missing.extend(missing)
            except Exception:
                pass

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(output_path))

    unique_missing = sorted(set(all_missing))
    return {
        "template": str(template_path),
        "output": str(output_path),
        "missing_keys": unique_missing,
        "ok": len(unique_missing) == 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fill-template.py",
        description="Fill {{key}} placeholders in a .pptx template from JSON data.",
    )
    parser.add_argument("template", help="Path to .pptx template file")
    parser.add_argument("data", help="Path to JSON data file (must be a JSON object)")
    parser.add_argument("-o", "--output", required=True, help="Output .pptx path")
    args = parser.parse_args()

    if not _PPTX_AVAILABLE:
        fail(3, "python-pptx is required. Install with: pip install python-pptx")

    template = Path(args.template)
    if not template.exists():
        fail(2, f"template not found: {template}")

    data_path = Path(args.data)
    if not data_path.exists():
        fail(2, f"data file not found: {data_path}")

    try:
        data = json.loads(data_path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(2, f"failed to parse JSON data: {exc}")

    if not isinstance(data, dict):
        fail(2, "data file must contain a JSON object (not an array or scalar)")

    fmt = detect_format(template)
    if fmt not in ("pptx", "pptm"):
        fail(3, f"unsupported format {fmt!r}; template must be a .pptx file")

    try:
        result = fill_template(template, data, Path(args.output))
    except Exception as exc:
        fail(1, f"fill failed: {exc}")

    emit_json(result)
    if result["missing_keys"]:
        print(f"warning: {len(result['missing_keys'])} undefined key(s): "
              f"{result['missing_keys'][:5]}", file=sys.stderr)
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
