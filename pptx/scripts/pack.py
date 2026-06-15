#!/usr/bin/env python3
"""
pack.py — Repack an unpacked PPTX directory back into a .pptx file.

Steps:
  1. Condense XML (remove pretty-print whitespace between elements).
  2. Auto-repair common issues (xml:space on <a:t>).
  3. Write deterministic ZIP ([Content_Types].xml first, sorted).
  4. Validate the result (unless --no-validate).

Exit codes:
  0  success
  1  validation failed
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

fail = _common_mod.fail
PPT_PROFILE = _common_mod.PPT_PROFILE

from common.ooxml.engine import pack


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pack.py",
        description="Repack an unpacked PPTX directory into a .pptx file.",
    )
    parser.add_argument("unpacked_dir", help="Directory produced by safe-unpack.py")
    parser.add_argument("output", help="Output .pptx path")
    parser.add_argument(
        "--original", metavar="ORIGINAL.pptx",
        help="Original .pptx to fill in parts absent from unpacked_dir",
    )
    parser.add_argument("--no-validate", action="store_true", help="Skip post-pack validation")
    parser.add_argument("--no-autorepair", action="store_true", help="Skip auto-repair")
    parser.add_argument("--keep-invalid", action="store_true",
                        help="Keep output even if validation fails")
    args = parser.parse_args()

    unpacked = Path(args.unpacked_dir)
    if not unpacked.is_dir():
        fail(2, f"not a directory: {unpacked}")

    output = Path(args.output)
    original = Path(args.original) if args.original else None
    validate_script = Path(__file__).parent / "validate.py"

    pack(
        unpacked,
        output,
        PPT_PROFILE,
        original=original,
        autorepair=not args.no_autorepair,
        validate=not args.no_validate,
        keep_invalid=args.keep_invalid,
        validate_script=validate_script,
    )


if __name__ == "__main__":
    main()
