#!/usr/bin/env python3
"""
convert.py — Convert between .doc, .docx, Markdown, PDF, and PNG (preview).

Engine routing with automatic fallback. Output format is inferred from the
output file extension unless --to is given.

Exit codes:
  0  success
  1  conversion error
  2  usage error
  3  required engine not found
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
detect_format = _common_mod.detect_format
emit_json = _common_mod.emit_json
fail = _common_mod.fail


# ---------------------------------------------------------------------------
# Engine detection
# ---------------------------------------------------------------------------

def _find(cmd: str) -> str | None:
    return shutil.which(cmd)


def _soffice_cmd() -> str | None:
    for candidate in ["soffice", "libreoffice"]:
        p = _find(candidate)
        if p:
            return p
    return None


# ---------------------------------------------------------------------------
# soffice runner (isolated profile, timeout)
# ---------------------------------------------------------------------------

def _run_soffice(args_list: list[str], timeout: int = 120) -> tuple[int, str]:
    """Run soffice with an isolated user profile. Returns (returncode, stderr)."""
    soffice = _soffice_cmd()
    if not soffice:
        return -1, "soffice/libreoffice not found"

    with tempfile.TemporaryDirectory(prefix="docx-soffice-") as tmpdir:
        user_installation = f"file://{tmpdir}/profile"
        cmd = [
            soffice,
            f"-env:UserInstallation={user_installation}",
        ] + args_list
        try:
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=timeout,
            )
            return result.returncode, result.stderr
        except subprocess.TimeoutExpired:
            return -1, f"soffice timed out after {timeout}s"
        except Exception as exc:
            return -1, str(exc)


# ---------------------------------------------------------------------------
# Conversion routes
# ---------------------------------------------------------------------------

def _convert_doc_to_docx(src: Path, dst: Path, engine: str) -> dict[str, Any]:
    """Convert legacy .doc to .docx."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    if engine in ("auto", "soffice"):
        soffice = _soffice_cmd()
        if soffice:
            with tempfile.TemporaryDirectory(prefix="docx-convert-") as tmpdir:
                code, stderr = _run_soffice([
                    "--headless", "--convert-to", "docx",
                    "--outdir", tmpdir, str(src),
                ])
                if code == 0:
                    # soffice writes <stem>.docx in outdir
                    out_name = src.stem + ".docx"
                    out_path = Path(tmpdir) / out_name
                    if out_path.exists():
                        shutil.copy2(out_path, dst)
                        return {"engine": "soffice", "output": str(dst), "warnings": warnings}
                warnings.append(f"soffice failed (code={code}): {stderr[:200]}")
        else:
            if engine == "soffice":
                fail(3, "soffice/libreoffice not found; brew install --cask libreoffice")

    if engine in ("auto", "textutil"):
        textutil = _find("textutil")
        if textutil:
            result = subprocess.run(
                ["textutil", "-convert", "docx", "-output", str(dst), str(src)],
                capture_output=True, text=True,
            )
            if result.returncode == 0 and dst.exists():
                warnings.append("used textutil fallback (lower fidelity than soffice)")
                return {"engine": "textutil", "output": str(dst), "warnings": warnings}
            warnings.append(f"textutil failed: {result.stderr[:200]}")
        else:
            if engine == "textutil":
                fail(3, "textutil not found (macOS only)")

    fail(
        3,
        "no engine available for .doc -> .docx conversion.\n"
        "Install LibreOffice: brew install --cask libreoffice\n"
        "Or on macOS textutil is usually available."
    )


def _convert_docx_to_md(src: Path, dst: Path, engine: str) -> dict[str, Any]:
    """Convert .docx to Markdown (GFM)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    if engine in ("auto", "pandoc"):
        pandoc = _find("pandoc")
        if pandoc:
            media_dir = dst.parent / (dst.stem + "-media")
            cmd = [
                pandoc, str(src),
                "-t", "gfm",
                "--track-changes=all",
                "--wrap=none",
                f"--extract-media={media_dir}",
                "-o", str(dst),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                return {"engine": "pandoc", "output": str(dst), "warnings": warnings}
            warnings.append(f"pandoc failed: {result.stderr[:200]}")
        elif engine == "pandoc":
            fail(3, "pandoc not found; brew install pandoc")

    # Fallback: use extract.py
    if engine in ("auto", "extract"):
        script_dir = Path(__file__).parent
        extract_script = script_dir / "extract.py"
        if extract_script.exists():
            result = subprocess.run(
                [sys.executable, str(extract_script), str(src), "--format", "md", "-o", str(dst)],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                warnings.append(
                    "used extract.py fallback (pandoc recommended for full fidelity, "
                    "tracked-changes support, and media extraction)"
                )
                return {"engine": "extract.py", "output": str(dst), "warnings": warnings}

    fail(
        3,
        "no engine available for .docx -> Markdown.\n"
        "Install pandoc: brew install pandoc"
    )


def _convert_md_to_docx(src: Path, dst: Path, engine: str) -> dict[str, Any]:
    """Convert Markdown to .docx via pandoc."""
    dst.parent.mkdir(parents=True, exist_ok=True)

    if engine in ("auto", "pandoc"):
        pandoc = _find("pandoc")
        if pandoc:
            result = subprocess.run(
                [pandoc, str(src), "-o", str(dst)],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                return {"engine": "pandoc", "output": str(dst), "warnings": []}
            fail(1, f"pandoc failed: {result.stderr[:400]}")
        elif engine == "pandoc":
            fail(3, "pandoc not found; brew install pandoc")

    fail(3, "Markdown -> .docx requires pandoc; brew install pandoc")


def _convert_docx_to_pdf(src: Path, dst: Path, engine: str) -> dict[str, Any]:
    """Convert .docx to PDF via soffice."""
    dst.parent.mkdir(parents=True, exist_ok=True)

    if engine in ("auto", "soffice"):
        soffice = _soffice_cmd()
        if soffice:
            with tempfile.TemporaryDirectory(prefix="docx-pdf-") as tmpdir:
                code, stderr = _run_soffice([
                    "--headless", "--convert-to", "pdf",
                    "--outdir", tmpdir, str(src),
                ])
                if code == 0:
                    out_name = src.stem + ".pdf"
                    out_path = Path(tmpdir) / out_name
                    if out_path.exists():
                        shutil.copy2(out_path, dst)
                        return {"engine": "soffice", "output": str(dst), "warnings": []}
            fail(1, f"soffice PDF conversion failed: {stderr[:400]}")
        elif engine == "soffice":
            fail(3, "soffice not found; brew install --cask libreoffice")

    fail(3, ".docx -> PDF requires LibreOffice; brew install --cask libreoffice")


def _convert_docx_to_png(src: Path, dst_prefix: Path, engine: str) -> dict[str, Any]:
    """Convert .docx to PNG previews (one per page)."""
    dst_prefix.parent.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    # Step 1: docx -> pdf
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_pdf = Path(tmp.name)
    try:
        _convert_docx_to_pdf(src, tmp_pdf, engine)

        # Step 2: pdf -> png via pdftoppm
        pdftoppm = _find("pdftoppm")
        if not pdftoppm:
            fail(3, "pdftoppm not found; brew install poppler")
        result = subprocess.run(
            [pdftoppm, "-png", "-r", "150", str(tmp_pdf), str(dst_prefix)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            fail(1, f"pdftoppm failed: {result.stderr[:400]}")
    finally:
        tmp_pdf.unlink(missing_ok=True)

    output_files = sorted(dst_prefix.parent.glob(f"{dst_prefix.name}*.png"))
    return {
        "engine": "soffice+pdftoppm",
        "output": [str(f) for f in output_files],
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Format inference
# ---------------------------------------------------------------------------

_EXT_TO_FMT = {
    ".docx": "docx", ".doc": "doc", ".docm": "docm",
    ".md": "md", ".markdown": "md", ".txt": "txt",
    ".pdf": "pdf", ".png": "png",
}


def _infer_format(path: Path) -> str | None:
    return _EXT_TO_FMT.get(path.suffix.lower())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="convert.py",
        description="Convert documents between .doc, .docx, Markdown, PDF, and PNG.",
    )
    parser.add_argument("input", help="Input file path")
    parser.add_argument("-o", "--output", required=True, help="Output file path")
    parser.add_argument(
        "--to", choices=["docx", "md", "txt", "pdf", "png"],
        help="Output format (inferred from output extension if omitted)",
    )
    parser.add_argument(
        "--engine", default="auto",
        choices=["auto", "pandoc", "soffice", "textutil", "extract"],
        help="Conversion engine (default: auto)",
    )
    args = parser.parse_args()

    src = Path(args.input)
    if not src.exists():
        fail(2, f"file not found: {src}")

    dst = Path(args.output)
    to_fmt = args.to or _infer_format(dst)
    if not to_fmt:
        fail(2, f"cannot infer output format from {dst.name}; use --to")

    src_fmt = detect_format(src)
    # Accept markdown/txt by extension for source
    if src_fmt == "unknown":
        src_ext = src.suffix.lower()
        if src_ext in (".md", ".markdown", ".txt"):
            src_fmt = "md"

    report: dict[str, Any]

    if src_fmt == "doc" and to_fmt == "docx":
        report = _convert_doc_to_docx(src, dst, args.engine)
    elif src_fmt in ("docx", "docm") and to_fmt == "md":
        report = _convert_docx_to_md(src, dst, args.engine)
    elif src_fmt == "md" and to_fmt == "docx":
        report = _convert_md_to_docx(src, dst, args.engine)
    elif src_fmt in ("docx", "docm") and to_fmt == "pdf":
        report = _convert_docx_to_pdf(src, dst, args.engine)
    elif src_fmt in ("docx", "docm") and to_fmt == "png":
        report = _convert_docx_to_png(src, Path(dst.with_suffix("")), args.engine)
    elif src_fmt in ("docx", "docm") and to_fmt == "txt":
        # Reuse extract.py
        script_dir = Path(__file__).parent
        result = subprocess.run(
            [sys.executable, str(script_dir / "extract.py"), str(src),
             "--format", "txt", "-o", str(dst)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            fail(1, result.stderr)
        report = {"engine": "extract.py", "output": str(dst), "warnings": []}
    else:
        fail(2, f"unsupported conversion: {src_fmt} -> {to_fmt}")

    emit_json(report)


if __name__ == "__main__":
    main()
