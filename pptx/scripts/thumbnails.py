#!/usr/bin/env python3
"""
thumbnails.py — Render .pptx slides to PNG images for visual QA.

Requires LibreOffice (soffice) and pdftoppm (poppler).

Exit codes:
  0  success
  1  rendering failed
  2  usage error
  3  missing dependency (soffice or pdftoppm)
"""
from __future__ import annotations

import argparse
import subprocess
import shutil
import sys
import tempfile
from pathlib import Path

import importlib.util as _ilu
_common_path = Path(__file__).parent / '_common.py'
_spec = _ilu.spec_from_file_location('_common', _common_path)
_common_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_common_mod)

detect_format = _common_mod.detect_format
emit_json = _common_mod.emit_json
fail = _common_mod.fail


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        p = _which(name)
        if p:
            return p
    return None


def render_thumbnails(
    src: Path,
    output_dir: Path,
    dpi: int = 150,
) -> list[Path]:
    """
    Render each slide of src to a PNG file in output_dir.

    Returns sorted list of generated PNG paths.
    """
    soffice = _soffice()
    if not soffice:
        fail(3, "LibreOffice not found. Install with: brew install --cask libreoffice")

    pdftoppm = _which("pdftoppm")
    if not pdftoppm:
        fail(3, "pdftoppm not found. Install with: brew install poppler")

    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="pptx-thumb-") as tmp:
        profile_dir = Path(tmp) / "soffice-profile"
        pdf_dir = Path(tmp) / "pdf"
        pdf_dir.mkdir()

        # Step 1: pptx → pdf
        result = subprocess.run(
            [
                soffice,
                f"-env:UserInstallation=file://{profile_dir}",
                "--headless", "--norestore", "--nologo",
                "--convert-to", "pdf",
                "--outdir", str(pdf_dir),
                str(src),
            ],
            capture_output=True, text=True, timeout=120,
        )
        pdf_file = pdf_dir / (src.stem + ".pdf")
        if result.returncode != 0 or not pdf_file.exists():
            fail(1, f"soffice failed to convert to PDF: {result.stderr[:500]}")

        # Step 2: pdf → png pages
        prefix = output_dir / src.stem
        result = subprocess.run(
            [pdftoppm, "-png", "-r", str(dpi), str(pdf_file), str(prefix)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            fail(1, f"pdftoppm failed: {result.stderr[:500]}")

    return sorted(output_dir.glob(f"{src.stem}*.png"))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="thumbnails.py",
        description="Render .pptx slides to PNG images for visual QA.",
    )
    parser.add_argument("file", help="Path to .pptx file")
    parser.add_argument(
        "-o", "--output-dir", default=None,
        help="Output directory for PNG files (default: <file_stem>-thumbnails/)",
    )
    parser.add_argument(
        "--dpi", type=int, default=150,
        help="Rendering resolution in DPI (default: 150)",
    )
    args = parser.parse_args()

    src = Path(args.file)
    if not src.exists():
        fail(2, f"file not found: {src}")

    fmt = detect_format(src)
    if fmt not in ("pptx", "pptm"):
        fail(3, f"unsupported format {fmt!r}; only .pptx/.pptm supported")

    output_dir = Path(args.output_dir) if args.output_dir else src.parent / f"{src.stem}-thumbnails"

    pngs = render_thumbnails(src, output_dir, dpi=args.dpi)

    result = {
        "file": str(src),
        "output_dir": str(output_dir),
        "slides": [str(p) for p in pngs],
        "slide_count": len(pngs),
    }
    emit_json(result)
    print(f"rendered {len(pngs)} slide(s) to {output_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
