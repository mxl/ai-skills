#!/usr/bin/env python3
"""
convert.py — Convert between .pptx and other formats.

Supported conversions:
  .ppt  → .pptx   (soffice)
  .pptx → .pdf    (soffice)
  .pptx → .png    (soffice → pdftoppm)
  .pptx → .md     (pandoc, fallback: extract.py)
  .md   → .pptx   (pandoc)

Exit codes:
  0  success
  1  conversion failed
  2  usage error
  3  required engine missing (prints install command)
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
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


# ---------------------------------------------------------------------------
# Engine detection
# ---------------------------------------------------------------------------

def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        p = _which(name)
        if p:
            return p
    return None


def _pandoc() -> str | None:
    return _which("pandoc")


def _pdftoppm() -> str | None:
    return _which("pdftoppm")


# ---------------------------------------------------------------------------
# soffice wrapper
# ---------------------------------------------------------------------------

def _run_soffice(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """Run soffice with an isolated user profile and timeout."""
    soffice = _soffice()
    if not soffice:
        fail(3, "LibreOffice not found. Install with: brew install --cask libreoffice")

    with tempfile.TemporaryDirectory(prefix="pptx-soffice-") as profile_dir:
        cmd = [
            soffice,
            f"-env:UserInstallation=file://{profile_dir}",
            "--headless",
            "--norestore",
            "--nologo",
        ] + args
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------------------
# Conversion routes
# ---------------------------------------------------------------------------

def _ppt_to_pptx(src: Path, output: Path) -> dict:
    """Convert legacy .ppt to .pptx via LibreOffice."""
    out_dir = output.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    result = _run_soffice([
        "--convert-to", "pptx",
        "--outdir", str(out_dir),
        str(src),
    ])
    # soffice writes <srcname>.pptx in outdir
    soffice_out = out_dir / (src.stem + ".pptx")
    if result.returncode != 0 or not soffice_out.exists():
        fail(1, f"soffice conversion failed: {result.stderr[:500]}")
    if soffice_out != output:
        soffice_out.rename(output)
    return {"engine": "soffice", "output": str(output)}


def _pptx_to_pdf(src: Path, output: Path) -> dict:
    """Convert .pptx to PDF via LibreOffice."""
    out_dir = output.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    result = _run_soffice([
        "--convert-to", "pdf",
        "--outdir", str(out_dir),
        str(src),
    ])
    soffice_out = out_dir / (src.stem + ".pdf")
    if result.returncode != 0 or not soffice_out.exists():
        fail(1, f"soffice conversion failed: {result.stderr[:500]}")
    if soffice_out != output:
        soffice_out.rename(output)
    return {"engine": "soffice", "output": str(output)}


def _pptx_to_png(src: Path, output_prefix: Path) -> dict:
    """Convert .pptx to PNG slide images via soffice → pdftoppm."""
    pdftoppm = _pdftoppm()
    if not pdftoppm:
        fail(3, "pdftoppm not found. Install with: brew install poppler")

    with tempfile.TemporaryDirectory(prefix="pptx-png-") as tmp:
        pdf_path = Path(tmp) / (src.stem + ".pdf")
        _pptx_to_pdf(src, pdf_path)

        output_prefix.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [pdftoppm, "-png", "-r", "150", str(pdf_path), str(output_prefix)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            fail(1, f"pdftoppm failed: {result.stderr[:500]}")

    output_files = sorted(output_prefix.parent.glob(f"{output_prefix.name}*.png"))
    return {
        "engine": "soffice+pdftoppm",
        "output_prefix": str(output_prefix),
        "slides": [str(f) for f in output_files],
    }


def _pptx_to_md(src: Path, output: Path) -> dict:
    """Convert .pptx to Markdown via pandoc, fallback to extract.py."""
    pandoc = _pandoc()
    if pandoc:
        result = subprocess.run(
            [pandoc, str(src), "-t", "gfm", "--wrap=none", "-o", str(output)],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return {"engine": "pandoc", "output": str(output)}
        print(f"warning: pandoc failed ({result.stderr[:200]}); falling back to extract.py",
              file=sys.stderr)

    # Fallback: extract.py
    extract_script = Path(__file__).parent / "extract.py"
    result = subprocess.run(
        [sys.executable, str(extract_script), str(src), "--format", "md", "-o", str(output)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        fail(1, f"extract.py fallback failed: {result.stderr[:500]}")
    warnings = ["pandoc not available; extracted via python-pptx (lower fidelity)"] if not pandoc else []
    return {"engine": "extract.py", "output": str(output), "warnings": warnings}


def _md_to_pptx(src: Path, output: Path) -> dict:
    """Convert Markdown to .pptx via pandoc."""
    pandoc = _pandoc()
    if not pandoc:
        fail(3, "pandoc not found. Install with: brew install pandoc")
    result = subprocess.run(
        [pandoc, str(src), "-t", "pptx", "-o", str(output)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        fail(1, f"pandoc failed: {result.stderr[:500]}")
    return {"engine": "pandoc", "output": str(output)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="convert.py",
        description="Convert between .pptx and other formats.",
    )
    parser.add_argument("input", help="Input file path")
    parser.add_argument("-o", "--output", required=True, help="Output file or prefix")
    parser.add_argument(
        "--to", choices=["pptx", "pdf", "png", "md"],
        help="Force output format (default: inferred from output extension)",
    )
    args = parser.parse_args()

    src = Path(args.input)
    if not src.exists():
        fail(2, f"file not found: {src}")

    output = Path(args.output)

    # Determine target format
    to_fmt = args.to or output.suffix.lstrip(".").lower()
    if to_fmt == "markdown":
        to_fmt = "md"

    src_fmt = detect_format(src) or src.suffix.lstrip(".").lower()

    if src_fmt in ("ppt",) and to_fmt == "pptx":
        result = _ppt_to_pptx(src, output)
    elif src_fmt in ("pptx", "pptm") and to_fmt == "pdf":
        result = _pptx_to_pdf(src, output)
    elif src_fmt in ("pptx", "pptm") and to_fmt == "png":
        result = _pptx_to_png(src, output)
    elif src_fmt in ("pptx", "pptm") and to_fmt == "md":
        result = _pptx_to_md(src, output)
    elif src.suffix.lower() == ".md" and to_fmt == "pptx":
        result = _md_to_pptx(src, output)
    else:
        fail(2, f"unsupported conversion: {src_fmt!r} → {to_fmt!r}")

    emit_json(result)


if __name__ == "__main__":
    main()
