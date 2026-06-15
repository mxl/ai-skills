#!/usr/bin/env python3
"""
validate.py — Validate a .pptx file: ZIP integrity, required parts,
content types, relationships, well-formed XML, slide/layout consistency.

Exit codes:
  0  all checks passed
  1  one or more checks failed
  2  usage error
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import importlib.util as _ilu
_common_path = Path(__file__).parent / '_common.py'
_spec = _ilu.spec_from_file_location('_common', _common_path)
_common_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_common_mod)

emit_json = _common_mod.emit_json
fail = _common_mod.fail
PPT_PROFILE = _common_mod.PPT_PROFILE

from common.ooxml.engine import validate as _engine_validate


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="validate.py",
        description="Validate a .pptx file (ZIP, parts, rels, XML, slides).",
    )
    parser.add_argument("file", help="Path to .pptx file")
    parser.add_argument(
        "--json", action="store_true", default=True,
        help="Output JSON report (default)",
    )
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        fail(2, f"file not found: {path}")

    results = _engine_validate(path, PPT_PROFILE)
    overall = all(r.ok for r in results)

    report = {
        "file": str(path),
        "ok": overall,
        "checks": [r.to_dict() for r in results],
    }
    emit_json(report)
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    main()
