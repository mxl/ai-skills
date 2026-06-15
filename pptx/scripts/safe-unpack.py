#!/usr/bin/env python3
"""
safe-unpack.py — Safely unpack a .pptx file for XML editing.

Exit codes:
  0  success
  1  safety check failed (use --force to override)
  2  usage error
  3  unsupported format
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

detect_format = _common_mod.detect_format
fail = _common_mod.fail
PPT_PROFILE = _common_mod.PPT_PROFILE

from common.ooxml.engine import unpack


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="safe-unpack.py",
        description="Safely unpack a .pptx file for XML editing.",
    )
    parser.add_argument("file", help="Source .pptx file")
    parser.add_argument("outdir", help="Output directory (will be created/replaced)")
    parser.add_argument(
        "--force", action="store_true",
        help="Proceed despite ZIP safety warnings",
    )
    args = parser.parse_args()

    src = Path(args.file)
    if not src.exists():
        fail(2, f"file not found: {src}")

    fmt = detect_format(src)
    if fmt not in ("pptx", "pptm"):
        fail(3, f"unsupported format {fmt!r}; only .pptx/.pptm can be unpacked")

    outdir = Path(args.outdir)
    meta = unpack(src, outdir, PPT_PROFILE, force=args.force)
    print(f"unpacked {src} -> {outdir}", file=sys.stderr)


if __name__ == "__main__":
    main()
