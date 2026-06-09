#!/usr/bin/env python3
"""
ocr.py — layered OCR workhorse for the `ocr` skill.

Baseline: Python stdlib + pdftoppm + pdftotext + tesseract (all assumed present).
Optional tiers: pytesseract, PyMuPDF, opencv-python, numpy, easyocr, openai.
Install optional tiers on-demand: uv run --with <package> python3 ocr.py ...

Usage:  python3 ocr.py INPUT [INPUT ...] [options]
See SKILL.md or --help for full flag reference.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# ── exit codes ────────────────────────────────────────────────────────────────
EXIT_OK = 0
EXIT_BAD_ARGS = 2
EXIT_UNSUPPORTED = 3
EXIT_MISSING_BINARY = 4

# ── constants ─────────────────────────────────────────────────────────────────
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".heic", ".webp", ".bmp", ".gif"}
PDF_EXTENSION = ".pdf"

# OSD script → tesseract language code
SCRIPT_TO_LANG: dict[str, str] = {
    "cyrillic":    "rus",
    "latin":       "eng",
    "han":         "chi_sim",
    "arabic":      "ara",
    "devanagari":  "hin",
    "bengali":     "ben",
    "korean":      "kor",
    "japanese":    "jpn",
    "greek":       "ell",
    "hebrew":      "heb",
    "thai":        "tha",
    "georgian":    "kat",
    "armenian":    "hye",
}

DEFAULT_MIN_CONF = 60.0
DEFAULT_PSM = 3
SMALL_WIDTH_THRESHOLD = 1400  # px — upscale if narrower


# ── capability detection ──────────────────────────────────────────────────────

class Caps:
    """Detect available binaries and Python libraries once at startup."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.has_pytesseract = self._try_import("pytesseract")
        self.has_fitz = self._try_import("fitz")          # PyMuPDF
        self.has_cv2 = self._try_import("cv2")            # opencv-python
        self.has_numpy = self._try_import("numpy")
        self.has_easyocr = self._try_import("easyocr")
        self.has_openai = self._try_import("openai")
        self.has_pil = self._try_import("PIL")             # Pillow

        self.bin_pdftoppm = shutil.which("pdftoppm")
        self.bin_pdftotext = shutil.which("pdftotext")
        self.bin_pdfinfo = shutil.which("pdfinfo")
        self.bin_tesseract = shutil.which("tesseract")
        self.bin_ocrmypdf = shutil.which("ocrmypdf")

        if verbose:
            self._report()

    def _try_import(self, name: str) -> bool:
        try:
            __import__(name)
            return True
        except ImportError:
            return False

    def _report(self):
        lines = ["[caps] Available capabilities:"]
        for attr, val in sorted(self.__dict__.items()):
            if attr.startswith("has_") or attr.startswith("bin_"):
                status = "OK" if val else "MISSING"
                lines.append(f"  {attr:<20} {status}  ({val if val and attr.startswith('bin_') else ''})")
        print("\n".join(lines), file=sys.stderr)

    def require_render(self):
        if not self.bin_pdftoppm and not self.has_fitz:
            _fatal("Cannot render PDF pages: neither pdftoppm nor PyMuPDF (fitz) is available.\n"
                   "Install: brew install poppler  OR  uv run --with pymupdf python3 ocr.py ...",
                   EXIT_MISSING_BINARY)

    def require_ocr(self):
        if not self.bin_tesseract:
            _fatal("tesseract binary not found.\n"
                   "Install: brew install tesseract tesseract-lang  (macOS)\n"
                   "         sudo apt install tesseract-ocr tesseract-ocr-all  (Ubuntu)",
                   EXIT_MISSING_BINARY)

    def require_pdftotext(self):
        if not self.bin_pdftotext and not self.has_fitz:
            _fatal("Cannot extract text layer: neither pdftotext nor PyMuPDF is available.\n"
                   "Install: brew install poppler  OR  uv run --with pymupdf python3 ocr.py ...",
                   EXIT_MISSING_BINARY)


# ── utilities ─────────────────────────────────────────────────────────────────

def _fatal(msg: str, code: int = EXIT_BAD_ARGS) -> None:
    print(f"[ocr] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _log(msg: str, verbose: bool) -> None:
    if verbose:
        print(f"[ocr] {msg}", file=sys.stderr)


def _run(cmd: list[str], capture: bool = True, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=capture,
        check=check,
        text=True,
    )


def _parse_page_range(spec: str, total: int) -> list[int]:
    """Parse '1-3,5,7' into [1, 2, 3, 5, 7] (1-indexed, clamped to total)."""
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            pages.extend(range(int(a), int(b) + 1))
        else:
            pages.append(int(part))
    return [p for p in pages if 1 <= p <= total]


def _sha1_key(path: str, engine: str, dpi: str, preprocess: str, lang: str) -> str:
    stat = os.stat(path)
    raw = f"{os.path.abspath(path)}|{stat.st_mtime}|{stat.st_size}|{engine}|{dpi}|{preprocess}|{lang}"
    return hashlib.sha1(raw.encode()).hexdigest()


# ── input classification ──────────────────────────────────────────────────────

def classify_input(path: str) -> str:
    """Return 'pdf', 'image', or 'unsupported'."""
    ext = Path(path).suffix.lower()
    if ext == PDF_EXTENSION:
        return "pdf"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return "unsupported"


# ── PDF probing ───────────────────────────────────────────────────────────────

def probe_pdf(path: str, caps: Caps, verbose: bool = False) -> dict[str, Any]:
    """
    Quick text-layer probe. Returns dict with needs_ocr, has_text_layer,
    pages, per_page_chars, median_chars, reason.
    Uses PyMuPDF if available (faster), falls back to pdftotext.
    """
    if caps.has_fitz:
        return _probe_fitz(path, verbose)
    caps.require_pdftotext()
    return _probe_pdftotext(path, caps, verbose)


def _probe_fitz(path: str, verbose: bool) -> dict[str, Any]:
    import fitz
    doc = fitz.open(path)
    pages = len(doc)
    per_page_chars = []
    for page in doc:
        text = page.get_text().replace(" ", "").replace("\n", "")
        per_page_chars.append(len(text))
    doc.close()
    return _make_probe_result(pages, per_page_chars, verbose)


def _probe_pdftotext(path: str, caps: Caps, verbose: bool) -> dict[str, Any]:
    # Get page count from pdfinfo
    pages = 1
    if caps.bin_pdfinfo:
        try:
            r = _run([caps.bin_pdfinfo, path])
            for line in r.stdout.splitlines():
                if line.lower().startswith("pages:"):
                    pages = int(line.split(":", 1)[1].strip())
                    break
        except Exception:
            pass

    per_page_chars = []
    for p in range(1, pages + 1):
        try:
            r = _run([caps.bin_pdftotext, "-layout", "-f", str(p), "-l", str(p), path, "-"])
            chars = len(r.stdout.replace(" ", "").replace("\n", ""))
        except Exception:
            chars = 0
        per_page_chars.append(chars)

    return _make_probe_result(pages, per_page_chars, verbose)


def _make_probe_result(pages: int, per_page_chars: list[int], verbose: bool) -> dict[str, Any]:
    if not per_page_chars:
        per_page_chars = [0]
    sorted_counts = sorted(per_page_chars)
    median = sorted_counts[len(sorted_counts) // 2]

    has_text_layer = median >= 100
    needs_ocr = not has_text_layer

    if has_text_layer:
        reason = f"median {median} chars/page — real text layer"
    else:
        reason = f"median {median} non-space chars/page (< 100 threshold) — rasterized text"

    return {
        "pages": pages,
        "per_page_chars": per_page_chars,
        "median_chars": median,
        "has_text_layer": has_text_layer,
        "needs_ocr": needs_ocr,
        "reason": reason,
    }


# ── text-layer extraction ─────────────────────────────────────────────────────

def extract_text_layer(path: str, pages: list[int] | None, caps: Caps) -> list[str]:
    """Extract embedded text from a PDF that has a real text layer."""
    if caps.has_fitz:
        return _extract_fitz(path, pages)
    caps.require_pdftotext()
    return _extract_pdftotext(path, pages, caps)


def _extract_fitz(path: str, pages: list[int] | None) -> list[str]:
    import fitz
    doc = fitz.open(path)
    result = []
    for i, page in enumerate(doc):
        if pages and (i + 1) not in pages:
            continue
        result.append(page.get_text("text"))
    doc.close()
    return result


def _extract_pdftotext(path: str, pages: list[int] | None, caps: Caps) -> list[str]:
    # Extract all pages then split
    r = _run([caps.bin_pdftotext, "-layout", path, "-"])
    # pdftotext uses form-feed (\x0c) as page separator
    all_pages = r.stdout.split("\x0c")
    if pages:
        return [all_pages[p - 1] for p in pages if p <= len(all_pages)]
    return [p for p in all_pages if p.strip()]


# ── page rendering ────────────────────────────────────────────────────────────

def auto_dpi(path: str, caps: Caps) -> int:
    """Choose DPI based on page dimensions."""
    width_pt = 595.0  # default A4
    try:
        if caps.has_fitz:
            import fitz
            doc = fitz.open(path)
            rect = doc[0].rect
            width_pt = rect.width
            doc.close()
        elif caps.bin_pdfinfo:
            r = _run([caps.bin_pdfinfo, path])
            for line in r.stdout.splitlines():
                if "page size" in line.lower():
                    # "Page size: 595.32 x 841.92 pts"
                    nums = re.findall(r"[\d.]+", line)
                    if nums:
                        width_pt = float(nums[0])
                    break
    except Exception:
        pass

    if width_pt > 1000:   # wide slide canvas (1920 pt)
        return 150
    if width_pt > 700:    # large page
        return 200
    return 300             # A4 / Letter


def render_pages(path: str, dpi: int, pages: list[int] | None,
                 tmpdir: str, caps: Caps, verbose: bool = False) -> list[tuple[int, str]]:
    """
    Render PDF pages to PNG files in tmpdir.
    Returns [(page_number, png_path), …].
    Uses PyMuPDF if available, else pdftoppm.
    """
    if caps.has_fitz:
        return _render_fitz(path, dpi, pages, tmpdir, verbose)
    caps.require_render()
    return _render_pdftoppm(path, dpi, pages, tmpdir, caps, verbose)


def _render_fitz(path: str, dpi: int, pages: list[int] | None,
                 tmpdir: str, verbose: bool) -> list[tuple[int, str]]:
    import fitz
    doc = fitz.open(path)
    results = []
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    for i, page in enumerate(doc):
        pnum = i + 1
        if pages and pnum not in pages:
            continue
        pix = page.get_pixmap(matrix=mat)
        out_path = os.path.join(tmpdir, f"page_{pnum:04d}.png")
        pix.save(out_path)
        _log(f"rendered page {pnum} → {pix.width}×{pix.height} px @ {dpi} DPI", verbose)
        results.append((pnum, out_path))
    doc.close()
    return results


def _render_pdftoppm(path: str, dpi: int, pages: list[int] | None,
                     tmpdir: str, caps: Caps, verbose: bool) -> list[tuple[int, str]]:
    prefix = os.path.join(tmpdir, "page")
    cmd = [caps.bin_pdftoppm, "-png", "-r", str(dpi)]
    if pages:
        cmd += ["-f", str(min(pages)), "-l", str(max(pages))]
    cmd += [path, prefix]
    _run(cmd, capture=False)

    # pdftoppm writes page-NNNN.png
    rendered = sorted(Path(tmpdir).glob("page-*.png"))
    results: list[tuple[int, str]] = []
    for png in rendered:
        # extract page number from filename "page-0001.png"
        stem = png.stem  # "page-0001"
        num_str = stem.split("-")[-1]
        pnum = int(num_str)
        if pages and pnum not in pages:
            continue
        _log(f"rendered page {pnum} → {png}", verbose)
        results.append((pnum, str(png)))
    return results


# ── language detection ────────────────────────────────────────────────────────

def detect_lang(img_path: str, caps: Caps, verbose: bool = False) -> str:
    """
    Run tesseract OSD on img_path to detect script, map to language code.
    Always appends +eng. Falls back to 'eng' on any failure.
    """
    if not caps.bin_tesseract:
        return "eng"
    try:
        r = subprocess.run(
            [caps.bin_tesseract, img_path, "stdout", "--psm", "0", "-l", "osd"],
            capture_output=True, text=True, timeout=30,
        )
        output = r.stdout + r.stderr
        script = None
        conf = 0.0
        for line in output.splitlines():
            if "script:" in line.lower() and "confidence" not in line.lower():
                script = line.split(":", 1)[1].strip().lower()
            if "script confidence:" in line.lower():
                try:
                    conf = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass

        _log(f"OSD detected script={script!r} confidence={conf}", verbose)

        if script and conf >= 1.0 and script in SCRIPT_TO_LANG:
            lang_code = SCRIPT_TO_LANG[script]
            if lang_code == "eng":
                return "eng"
            return f"{lang_code}+eng"
    except Exception as e:
        _log(f"OSD failed: {e} — falling back to eng", verbose)

    return "eng"


# ── preprocessing ─────────────────────────────────────────────────────────────

def preprocess(img_path: str, level: str, caps: Caps, tmpdir: str,
               verbose: bool = False) -> str:
    """
    Apply image preprocessing before OCR.
    Returns path to processed image (may be same as input for level=none).
    Requires PIL for basic; opencv-python+numpy for enhanced/full.
    Gracefully falls back to basic if cv2 missing.
    """
    if level == "none":
        return img_path

    out_path = os.path.join(tmpdir, "pp_" + os.path.basename(img_path))

    if level == "basic" or (level in ("enhanced", "full") and not (caps.has_cv2 and caps.has_numpy)):
        if not caps.has_pil:
            _log("Pillow not available — skipping preprocessing", verbose)
            return img_path
        if level in ("enhanced", "full") and not caps.has_cv2:
            _log("opencv-python not available — falling back to basic preprocessing", verbose)
        return _preprocess_basic(img_path, out_path, verbose)

    if level in ("enhanced", "full"):
        return _preprocess_opencv(img_path, out_path, level, verbose)

    return img_path


def _preprocess_basic(img_path: str, out_path: str, verbose: bool) -> str:
    from PIL import Image, ImageEnhance, ImageFilter
    img = Image.open(img_path).convert("RGB")
    w, h = img.size
    if w < SMALL_WIDTH_THRESHOLD:
        img = img.resize((w * 2, h * 2), Image.LANCZOS)
        _log(f"upscaled {w}×{h} → {w*2}×{h*2}", verbose)
    gray = img.convert("L")
    enhanced = ImageEnhance.Contrast(gray).enhance(1.3)
    sharpened = enhanced.filter(ImageFilter.SHARPEN)
    sharpened.save(out_path)
    return out_path


def _preprocess_opencv(img_path: str, out_path: str, level: str, verbose: bool) -> str:
    import cv2
    import numpy as np
    from PIL import Image

    img = Image.open(img_path).convert("RGB")
    w, h = img.size
    if w < SMALL_WIDTH_THRESHOLD:
        img = img.resize((w * 2, h * 2), Image.LANCZOS)
        _log(f"upscaled {w}×{h} → {w*2}×{h*2}", verbose)

    arr = np.array(img)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    # Denoise
    denoised = cv2.fastNlMeansDenoising(gray, h=10, templateWindowSize=7, searchWindowSize=21)

    # Deskew for 'full'
    if level == "full":
        denoised = _deskew(denoised, verbose)

    # Adaptive threshold
    thresh = cv2.adaptiveThreshold(
        denoised, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=31, C=10,
    )
    cv2.imwrite(out_path, thresh)
    return out_path


def _deskew(gray: Any, verbose: bool) -> Any:
    import cv2
    import numpy as np
    inv = cv2.bitwise_not(gray)
    _, binary = cv2.threshold(inv, 50, 255, cv2.THRESH_BINARY)
    coords = np.column_stack(np.where(binary > 0))
    if len(coords) < 100:
        return gray
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
    if abs(angle) < 0.5:
        _log(f"deskew: angle {angle:.2f}° < 0.5° — skipped", verbose)
        return gray
    _log(f"deskew: correcting {angle:.2f}°", verbose)
    h, w = gray.shape
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def resolve_preprocess(level: str, probe: dict | None, caps: Caps,
                       input_type: str = "pdf") -> str:
    """
    Resolve 'auto' preprocessing level.
    - Standalone images: default none (already rasterized at native quality)
    - PDF renders that need OCR: enhanced if cv2 available, else basic
    - PDF with real text layer: basic (shouldn't matter, fast path skips OCR)
    """
    if level != "auto":
        return level
    if input_type == "image":
        return "none"  # don't degrade already-rasterized images
    if probe and probe.get("needs_ocr"):
        return "enhanced" if (caps.has_cv2 and caps.has_numpy) else "basic"
    return "basic"


# ── OCR engines ───────────────────────────────────────────────────────────────

def ocr_tesseract(img_path: str, lang: str, psm: int,
                  caps: Caps, verbose: bool = False) -> tuple[str, float, list[dict]]:
    """
    Run tesseract OCR. Returns (text, mean_conf, words).
    Prefers pytesseract for TSV confidence data; falls back to tesseract CLI.
    """
    caps.require_ocr()

    if caps.has_pytesseract:
        return _ocr_pytesseract(img_path, lang, psm, verbose)
    return _ocr_tesseract_cli(img_path, lang, psm, caps, verbose)


def _ocr_pytesseract(img_path: str, lang: str, psm: int,
                     verbose: bool) -> tuple[str, float, list[dict]]:
    import pytesseract
    config = f"--oem 3 --psm {psm}"
    _log(f"pytesseract lang={lang} config={config}", verbose)

    # Pass img_path directly to avoid PIL round-trip issues with large images
    data = pytesseract.image_to_data(img_path, lang=lang, config=config,
                                     output_type=pytesseract.Output.DICT)
    words = []
    confidences = []
    for i, word in enumerate(data["text"]):
        conf = int(data["conf"][i])
        if conf == -1 or not word.strip():
            continue
        words.append({
            "text": word,
            "conf": conf,
            "bbox": [data["left"][i], data["top"][i],
                     data["width"][i], data["height"][i]],
        })
        confidences.append(conf)

    full_text = pytesseract.image_to_string(img_path, lang=lang, config=config)
    mean_conf = sum(confidences) / len(confidences) if confidences else 0.0
    _log(f"pytesseract: {len(words)} words, mean_conf={mean_conf:.1f}", verbose)

    # Safety retry with rus+eng when lang was auto-detected as eng-only:
    # OSD may have failed (sparse page, logo-heavy), leaving us with bad lang.
    # Always try rus+eng when we used plain "eng" — it's cheap and catches mixed docs.
    if ("rus" not in lang) and lang == "eng":
        _log("retrying with rus+eng (zero words or garbled non-ASCII)", verbose)
        fallback = "rus+eng"
        data2 = pytesseract.image_to_data(img_path, lang=fallback, config=config,
                                          output_type=pytesseract.Output.DICT)
        words2, confs2 = [], []
        for i, w in enumerate(data2["text"]):
            c = int(data2["conf"][i])
            if c == -1 or not w.strip():
                continue
            words2.append({"text": w, "conf": c,
                           "bbox": [data2["left"][i], data2["top"][i],
                                    data2["width"][i], data2["height"][i]]})
            confs2.append(c)
        mean_conf2 = sum(confs2) / len(confs2) if confs2 else 0.0
        # Use rus+eng if it got more words, or similar words with better confidence
        if len(words2) > len(words) or (words2 and mean_conf2 > mean_conf + 5):
            full_text2 = pytesseract.image_to_string(img_path, lang=fallback, config=config)
            _log(f"retry rus+eng: {len(words2)} words, mean_conf={mean_conf2:.1f}", verbose)
            return full_text2, mean_conf2, words2

    return full_text, mean_conf, words


def _ocr_tesseract_cli(img_path: str, lang: str, psm: int,
                       caps: Caps, verbose: bool) -> tuple[str, float, list[dict]]:
    _log(f"tesseract CLI lang={lang} psm={psm}", verbose)
    config = f"--oem 3 --psm {psm}"

    # TSV for confidence
    tsv_result = subprocess.run(
        [caps.bin_tesseract, img_path, "stdout", "-l", lang,
         "--oem", "3", "--psm", str(psm), "tsv"],
        capture_output=True, text=True,
    )
    words: list[dict] = []
    confidences: list[float] = []
    if tsv_result.returncode == 0:
        lines = tsv_result.stdout.splitlines()
        if len(lines) > 1:
            for line in lines[1:]:
                parts = line.split("\t")
                if len(parts) < 12:
                    continue
                word_text = parts[11].strip()
                try:
                    conf = float(parts[10])
                except ValueError:
                    continue
                if conf == -1 or not word_text:
                    continue
                try:
                    bbox = [int(parts[6]), int(parts[7]), int(parts[8]), int(parts[9])]
                except ValueError:
                    bbox = [0, 0, 0, 0]
                words.append({"text": word_text, "conf": int(conf), "bbox": bbox})
                confidences.append(conf)

    # Plain text
    txt_result = subprocess.run(
        [caps.bin_tesseract, img_path, "stdout", "-l", lang,
         "--oem", "3", "--psm", str(psm)],
        capture_output=True, text=True,
    )
    full_text = txt_result.stdout if txt_result.returncode == 0 else ""
    mean_conf = sum(confidences) / len(confidences) if confidences else 0.0
    _log(f"tesseract CLI: {len(words)} words, mean_conf={mean_conf:.1f}", verbose)
    return full_text, mean_conf, words


def ocr_easyocr(img_path: str, caps: Caps, verbose: bool = False) -> tuple[str, float, list[dict]]:
    if not caps.has_easyocr:
        _fatal("easyocr not installed. Run: uv run --with easyocr python3 ocr.py ...\n"
               "Note: first run downloads ~2 GB of models.", EXIT_MISSING_BINARY)
    import easyocr
    import numpy as np
    from PIL import Image
    _log("loading easyocr reader (ru+en)...", verbose)
    reader = easyocr.Reader(["ru", "en"], gpu=False)
    img = Image.open(img_path).convert("RGB")
    arr = np.array(img)
    result = reader.readtext(arr, detail=1, paragraph=False)
    words = []
    texts = []
    confs = []
    for bbox, text, conf in result:
        if text.strip():
            x_coords = [p[0] for p in bbox]
            y_coords = [p[1] for p in bbox]
            words.append({
                "text": text,
                "conf": int(conf * 100),
                "bbox": [int(min(x_coords)), int(min(y_coords)),
                         int(max(x_coords) - min(x_coords)),
                         int(max(y_coords) - min(y_coords))],
            })
            texts.append(text)
            confs.append(conf)
    full_text = "\n".join(texts)
    mean_conf = (sum(confs) / len(confs) * 100) if confs else 0.0
    _log(f"easyocr: {len(words)} words, mean_conf={mean_conf:.1f}", verbose)
    return full_text, mean_conf, words


def vision_handoff(img_paths: list[tuple[int, str]], verbose: bool = False) -> str:
    """
    Print a manifest of rendered PNG paths for agent-driven vision OCR.
    Returns a manifest string; the agent should read each PNG.
    """
    lines = [
        "== Vision OCR: agent read required ==",
        "",
        "The following pages were rendered as PNG for vision-based OCR.",
        "Read each image and reproduce all text. For tables use Markdown",
        "table syntax (| col | col |). For charts describe key values.",
        "",
        "Prompt to use:",
        '  "Read this page image faithfully. Reproduce all visible text in',
        '   reading order. For tables use | Markdown | table | syntax |.',
        '   For charts describe axis labels and key data values.',
        '   No commentary — only the content visible in the image."',
        "",
        "Pages to read:",
    ]
    for pnum, png_path in img_paths:
        lines.append(f"  Page {pnum}: {png_path}")
    manifest = "\n".join(lines)
    print(manifest, file=sys.stderr)
    return manifest


def vision_api(img_paths: list[tuple[int, str]], verbose: bool = False) -> str:
    """Call OpenAI GPT-4o vision API for pages (requires OPENAI_API_KEY)."""
    if not os.environ.get("OPENAI_API_KEY"):
        _fatal("OPENAI_API_KEY not set. Set the env var or use --engine vision (agent-driven).",
               EXIT_BAD_ARGS)
    try:
        from openai import OpenAI
    except ImportError:
        _fatal("openai package not installed. Run: uv run --with openai python3 ocr.py ...",
               EXIT_MISSING_BINARY)

    client = OpenAI()
    parts_by_page: list[str] = []

    for pnum, png_path in img_paths:
        _log(f"vision API: page {pnum}", verbose)
        with open(png_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        content = [
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}},
            {"type": "text",
             "text": ("Read this page image faithfully. Reproduce all visible text in "
                      "reading order. For tables use Markdown table syntax. For charts "
                      "describe axis labels and key data values. No commentary.")},
        ]
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": content}],
            max_tokens=4096,
        )
        page_text = resp.choices[0].message.content or ""
        parts_by_page.append(f"## Page {pnum}\n\n{page_text}")

    return "\n\n".join(parts_by_page)


# ── post-processing ───────────────────────────────────────────────────────────

def general_cleanup(text: str) -> str:
    """
    Light general-purpose cleanup (no domain dictionaries).
    - Collapse runs of spaces/tabs to single space
    - Join hyphenated line-breaks (сло-\nво → слово)
    - Normalize common ligatures (ﬁ→fi, ﬂ→fl, ﬀ→ff, ﬃ→ffi, ﬄ→ffl)
    - Strip control characters (keep newlines and tabs)
    - Collapse 3+ blank lines to 2
    """
    # Ligatures
    for lig, rep in [("ﬁ", "fi"), ("ﬂ", "fl"), ("ﬀ", "ff"), ("ﬃ", "ffi"), ("ﬄ", "ffl"),
                     ("ﬅ", "st"), ("ﬆ", "st")]:
        text = text.replace(lig, rep)
    # Strip control characters (keep \n, \t)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # Join hyphenated line-breaks (word-\n  nextword → wordnextword)
    text = re.sub(r"(\w)-\n[ \t]*(\w)", r"\1\2", text)
    # Collapse runs of spaces/tabs within a line
    text = re.sub(r"[ \t]{2,}", " ", text)
    # Collapse 3+ blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── tabular heuristic ─────────────────────────────────────────────────────────

def looks_tabular(words: list[dict]) -> bool:
    """
    Detect table structure: multiple rows each having words at 3+ distinct
    x-column positions (not just 3 words on the same line — prose does that too).
    Requires the x-positions to span the page width with visible gaps.
    """
    if len(words) < 9:
        return False
    # Group words by y-bucket (10px)
    y_groups: dict[int, list[int]] = {}
    for w in words:
        y = w["bbox"][1] // 10 * 10
        y_groups.setdefault(y, []).append(w["bbox"][0])

    # A table row: ≥ 3 words at x-positions spanning ≥ 3 distinct column buckets
    # Column bucket = x // (page_width / 6)  — divides page into 6 zones
    x_all = [w["bbox"][0] for w in words]
    if not x_all:
        return False
    page_width = max(x_all) - min(x_all) + 1
    col_bucket_size = max(page_width // 6, 50)

    table_rows = 0
    for xs in y_groups.values():
        col_buckets = {x // col_bucket_size for x in xs}
        if len(col_buckets) >= 3 and len(xs) >= 4:
            table_rows += 1

    return table_rows >= 3


# ── output formatters ─────────────────────────────────────────────────────────

def to_markdown(pages_data: list[dict], filename: str) -> str:
    parts = [f"# {filename}", ""]
    for page in pages_data:
        parts.append(f"## Page {page['n']}")
        parts.append("")
        parts.append(page.get("text", "").strip())
        parts.append("")
    return "\n".join(parts)


def to_text(pages_data: list[dict]) -> str:
    parts = []
    for page in pages_data:
        parts.append(f"----- Page {page['n']} -----")
        parts.append(page.get("text", "").strip())
        parts.append("")
    return "\n".join(parts)


def to_json(pages_data: list[dict], meta: dict) -> str:
    low_conf = [p["n"] for p in pages_data if p.get("mean_conf", 100) < meta.get("min_conf", 60)]
    vision_recs = [p["n"] for p in pages_data if p.get("flag") == "review-vision"]
    out = {
        "file": meta.get("file", ""),
        "engine": meta.get("engine", "auto"),
        "lang": meta.get("lang", ""),
        "dpi": meta.get("dpi", 0),
        "preprocess": meta.get("preprocess", "auto"),
        "pages": pages_data,
        "report": {
            "total_pages": len(pages_data),
            "low_conf_pages": low_conf,
            "recommend_vision": vision_recs,
        },
    }
    return json.dumps(out, ensure_ascii=False, indent=2)


# ── caching ───────────────────────────────────────────────────────────────────

class Cache:
    def __init__(self, path: str | None):
        self._path = path
        self._data: dict = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                pass

    def get(self, key: str) -> dict | None:
        return self._data.get(key)

    def set(self, key: str, value: dict) -> None:
        self._data[key] = value
        if self._path:
            try:
                with open(self._path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass


# ── main processing ───────────────────────────────────────────────────────────

def process_file(
    path: str,
    args: argparse.Namespace,
    caps: Caps,
    cache: Cache,
    tmpdir: str,
) -> list[dict]:
    """
    Process one input file. Returns list of page dicts.
    """
    verbose = args.verbose
    input_type = classify_input(path)

    if input_type == "unsupported":
        _fatal(f"Unsupported file type: {path}", EXIT_UNSUPPORTED)

    # Resolve DPI
    dpi = args.dpi
    if dpi == 0:  # 0 = auto
        if input_type == "pdf":
            dpi = auto_dpi(path, caps)
        else:
            dpi = 150  # images already rasterized

    # Probe PDF
    probe: dict | None = None
    if input_type == "pdf":
        probe = probe_pdf(path, caps, verbose)
        _log(f"probe: {probe['reason']}", verbose)

    # Preprocess level
    pp_level = resolve_preprocess(args.preprocess, probe, caps, input_type)

    # Cache key
    cache_key = _sha1_key(path, args.engine, str(dpi), pp_level, args.lang)
    if not args.force and cache.get(cache_key):
        _log(f"cache hit for {path}", verbose)
        cached = cache.get(cache_key)
        return cached.get("pages", [])

    pages_data: list[dict] = []

    # Fast path: real text layer
    if (input_type == "pdf"
            and probe
            and not probe["needs_ocr"]
            and not args.force
            and args.engine == "auto"):
        page_range = _parse_page_range(args.pages, probe["pages"]) if args.pages else None
        texts = extract_text_layer(path, page_range, caps)
        for i, text in enumerate(texts):
            pnum = (page_range[i] if page_range else i + 1)
            cleaned = general_cleanup(text) if not args.no_cleanup else text
            pages_data.append({
                "n": pnum,
                "source": "text_layer",
                "mean_conf": 100.0,
                "flag": None,
                "text": cleaned,
                "words": [],
            })
        cache.set(cache_key, {"pages": pages_data})
        return pages_data

    # Vision-handoff path
    if args.engine == "vision":
        caps.require_render()
        page_range = _parse_page_range(args.pages, probe["pages"] if probe else 1) if args.pages else None
        if input_type == "pdf":
            rendered = render_pages(path, dpi, page_range, tmpdir, caps, verbose)
        else:
            rendered = [(1, path)]
        vision_handoff(rendered, verbose)
        # Return placeholder pages — agent fills the text
        for pnum, png in rendered:
            pages_data.append({
                "n": pnum,
                "source": "vision_pending",
                "mean_conf": None,
                "flag": "vision",
                "text": f"[Vision OCR pending — read {png}]",
                "words": [],
                "png_path": png,
            })
        return pages_data

    # Vision API path
    if args.engine == "vision-api":
        caps.require_render()
        page_range = _parse_page_range(args.pages, probe["pages"] if probe else 1) if args.pages else None
        if input_type == "pdf":
            rendered = render_pages(path, dpi, page_range, tmpdir, caps, verbose)
        else:
            rendered = [(1, path)]
        combined_md = vision_api(rendered, verbose)
        pages_data.append({"n": 0, "source": "vision_api", "mean_conf": None,
                            "flag": None, "text": combined_md, "words": []})
        cache.set(cache_key, {"pages": pages_data})
        return pages_data

    # EasyOCR path
    if args.engine == "easyocr":
        if input_type == "pdf":
            page_range = _parse_page_range(args.pages, probe["pages"]) if args.pages else None
            rendered = render_pages(path, dpi, page_range, tmpdir, caps, verbose)
        else:
            rendered = [(1, path)]
        for pnum, img_path in rendered:
            pp_path = preprocess(img_path, pp_level, caps, tmpdir, verbose)
            text, conf, words = ocr_easyocr(pp_path, caps, verbose)
            cleaned = general_cleanup(text) if not args.no_cleanup else text
            pages_data.append({
                "n": pnum, "source": "easyocr",
                "mean_conf": conf, "flag": None,
                "text": cleaned, "words": words,
            })
        cache.set(cache_key, {"pages": pages_data})
        return pages_data

    # Default: tesseract (auto or explicit)
    if input_type == "pdf":
        page_range = _parse_page_range(args.pages, probe["pages"]) if args.pages else None
        if args.max_pages:
            if page_range:
                page_range = page_range[:args.max_pages]
            else:
                page_range = list(range(1, min(probe["pages"], args.max_pages) + 1))
        rendered = render_pages(path, dpi, page_range, tmpdir, caps, verbose)
    else:
        rendered = [(1, path)]

    # Auto-detect language from first page
    lang = args.lang
    if lang == "auto" and rendered:
        lang = detect_lang(rendered[0][1], caps, verbose)
        _log(f"language detected: {lang}", verbose)

    for pnum, img_path in rendered:
        t0 = time.time()
        pp_path = preprocess(img_path, pp_level, caps, tmpdir, verbose)
        text, conf, words = ocr_tesseract(pp_path, lang, args.psm, caps, verbose)
        elapsed = time.time() - t0

        cleaned = general_cleanup(text) if not args.no_cleanup else text

        # Determine flag
        flag = None
        if conf < args.min_conf or looks_tabular(words):
            flag = "review-vision"

        pages_data.append({
            "n": pnum, "source": "tesseract",
            "mean_conf": round(conf, 1), "flag": flag,
            "text": cleaned, "words": words,
            "elapsed_s": round(elapsed, 2),
        })
        _log(f"page {pnum}: {len(words)} words, conf={conf:.1f}, {elapsed:.1f}s"
             + (f" [FLAGGED: {flag}]" if flag else ""), verbose)

    cache.set(cache_key, {"pages": pages_data})
    return pages_data


# ── output writing ────────────────────────────────────────────────────────────

def write_outputs(
    pages_data: list[dict],
    input_path: str,
    args: argparse.Namespace,
    lang: str,
    dpi: int,
) -> None:
    filename = os.path.basename(input_path)
    meta = {
        "file": filename,
        "engine": args.engine,
        "lang": lang,
        "dpi": dpi,
        "preprocess": args.preprocess,
        "min_conf": args.min_conf,
    }

    formats = args.format.split(",") if "," in args.format else [args.format]
    if "all" in formats:
        formats = ["md", "txt", "json"]

    for fmt in formats:
        if fmt == "md":
            content = to_markdown(pages_data, filename)
        elif fmt == "txt":
            content = to_text(pages_data)
        elif fmt == "json":
            content = to_json(pages_data, meta)
        else:
            continue

        if args.out:
            out_path = args.out
            if os.path.isdir(out_path) or "all" in [args.format] or len(formats) > 1:
                os.makedirs(out_path, exist_ok=True)
                stem = Path(input_path).stem
                out_file = os.path.join(out_path, f"{stem}.{fmt}")
            else:
                out_file = out_path
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"[ocr] wrote {fmt} → {out_file}", file=sys.stderr)
        else:
            if len(formats) > 1:
                print(f"\n{'='*60}\n[{fmt.upper()}]\n{'='*60}", flush=True)
            print(content, flush=True)

    # Optional JSON report
    if args.json_report:
        report_content = to_json(pages_data, meta)
        with open(args.json_report, "w", encoding="utf-8") as f:
            f.write(report_content)
        print(f"[ocr] quality report → {args.json_report}", file=sys.stderr)

    # Searchable PDF
    if args.searchable_pdf:
        if not caps_global.bin_ocrmypdf:
            print("[ocr] WARNING: ocrmypdf not found. Install: brew install ocrmypdf", file=sys.stderr)
        else:
            cmd = [caps_global.bin_ocrmypdf, "-l", lang,
                   "--rotate-pages", "--deskew", "--force-ocr",
                   input_path, args.searchable_pdf]
            try:
                subprocess.run(cmd, check=True)
                print(f"[ocr] searchable PDF → {args.searchable_pdf}", file=sys.stderr)
            except subprocess.CalledProcessError as e:
                print(f"[ocr] ocrmypdf failed: {e}", file=sys.stderr)

    # Print quality report summary
    flagged = [p for p in pages_data if p.get("flag")]
    if flagged:
        print(f"\n[ocr] Quality report: {len(flagged)} page(s) flagged for review", file=sys.stderr)
        for p in flagged:
            print(f"  Page {p['n']}: conf={p.get('mean_conf', '?')}, flag={p['flag']}", file=sys.stderr)
        print("[ocr] Suggestion: re-run with --engine vision --pages "
              + ",".join(str(p["n"]) for p in flagged), file=sys.stderr)


# ── argparse ──────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ocr.py",
        description="Extract text from scanned PDFs and images using layered OCR.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 ocr.py scan.pdf --format all
  python3 ocr.py photo.jpg --format md
  python3 ocr.py doc.pdf --lang rus+eng --preprocess full --format all
  python3 ocr.py slides.pdf --engine vision --pages 9,12
  python3 ocr.py *.pdf --cache cache.json --format txt
        """,
    )
    p.add_argument("inputs", nargs="+", metavar="INPUT", help="PDF or image file(s)")
    p.add_argument("--engine", default="auto",
                   choices=["auto", "tesseract", "easyocr", "vision", "vision-api"],
                   help="OCR engine (default: auto)")
    p.add_argument("--lang", default="auto",
                   help="Tesseract language code(s), e.g. rus+eng (default: auto via OSD)")
    p.add_argument("--format", default="md",
                   help="Output format: md|txt|json|all (default: md)")
    p.add_argument("--out", default="",
                   help="Output file or directory (default: stdout)")
    p.add_argument("--dpi", type=int, default=0,
                   help="Rendering DPI (default: auto — 300 A4, 150 wide canvas)")
    p.add_argument("--preprocess", default="auto",
                   choices=["none", "basic", "enhanced", "full", "auto"],
                   help="Image preprocessing level (default: auto)")
    p.add_argument("--pages", default="",
                   help="Page range to process, e.g. 1-3,5 (default: all)")
    p.add_argument("--max-pages", type=int, default=0,
                   help="Maximum pages per file (default: all)")
    p.add_argument("--psm", type=int, default=DEFAULT_PSM,
                   help=f"Tesseract PSM (default: {DEFAULT_PSM}; 6 for single-block)")
    p.add_argument("--min-conf", type=float, default=DEFAULT_MIN_CONF,
                   help=f"Confidence threshold for flagging pages (default: {DEFAULT_MIN_CONF})")
    p.add_argument("--cache", default="",
                   help="Cache file path (JSON) for skipping already-processed files")
    p.add_argument("--force", action="store_true",
                   help="Ignore cache and re-process all files")
    p.add_argument("--skip-ocr", action="store_true",
                   help="Only process files with a real text layer; skip OCR")
    p.add_argument("--no-cleanup", action="store_true",
                   help="Skip whitespace / ligature cleanup of OCR output")
    p.add_argument("--searchable-pdf", default="",
                   help="Path for searchable PDF output (requires ocrmypdf)")
    p.add_argument("--json-report", default="",
                   help="Path to write JSON quality report")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Verbose logging to stderr")
    return p


# ── entry point ───────────────────────────────────────────────────────────────

caps_global: Caps  # set in main()


def main() -> None:
    global caps_global
    parser = build_parser()
    args = parser.parse_args()

    caps = Caps(verbose=args.verbose)
    caps_global = caps

    # Validate binary requirements for the chosen engine
    if args.engine in ("auto", "tesseract"):
        caps.require_ocr()

    cache = Cache(args.cache if args.cache else None)

    # Vision handoff must persist rendered PNGs after this process exits so the
    # calling agent (Claude, GPT, or any multimodal model) can read them.
    # Other engines use a throwaway temp dir.
    if args.engine == "vision":
        tmpdir = tempfile.mkdtemp(prefix="ocr_skill_vision_")
        print(f"[ocr] vision PNGs will persist in: {tmpdir}", file=sys.stderr)
        cleanup_tmp = False
    else:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="ocr_skill_")
        tmpdir = tmp_ctx.name
        cleanup_tmp = True

    try:
        for input_path in args.inputs:
            if not os.path.exists(input_path):
                print(f"[ocr] WARNING: file not found: {input_path}", file=sys.stderr)
                continue

            _log(f"processing: {input_path}", args.verbose)
            t_start = time.time()

            pages_data = process_file(input_path, args, caps, cache, tmpdir)

            # Resolve effective lang for output meta (might have been auto-detected)
            effective_lang = args.lang
            if effective_lang == "auto" and pages_data:
                # Best we can do without re-running OSD
                effective_lang = "auto-detected"

            effective_dpi = args.dpi or 0

            write_outputs(pages_data, input_path, args, effective_lang, effective_dpi)

            elapsed = time.time() - t_start
            total_chars = sum(len(p.get("text", "")) for p in pages_data)
            _log(f"done: {len(pages_data)} pages, {total_chars} chars, {elapsed:.1f}s total",
                 args.verbose)
    finally:
        if cleanup_tmp:
            tmp_ctx.cleanup()


if __name__ == "__main__":
    main()
