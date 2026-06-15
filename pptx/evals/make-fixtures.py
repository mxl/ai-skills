#!/usr/bin/env python3
"""
make-fixtures.py — Generate synthetic .pptx fixture files for pptx skill evals.

All fixtures are generated locally with python-pptx and stdlib;
no external content or network access required.

Usage:
    python evals/make-fixtures.py [--outdir evals/fixtures]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import zipfile
from pathlib import Path

# Add repo root to sys.path for common.ooxml access
_here = Path(__file__).resolve().parent
_repo = _here.parent.parent
sys.path.insert(0, str(_repo))

try:
    from pptx import Presentation
    from pptx.util import Inches, Pt, Emu
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    _PPTX = True
except ImportError:
    print("error: python-pptx required. pip install python-pptx", file=sys.stderr)
    sys.exit(3)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _save(prs: Presentation, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(path))
    print(f"  created: {path.name}")


def _new_prs(wide: bool = True) -> Presentation:
    prs = Presentation()
    if wide:
        prs.slide_width  = Emu(9144000)   # 10 in
        prs.slide_height = Emu(5143500)   # 5.625 in (16:9)
    return prs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_simple(out: Path) -> None:
    """simple.pptx — two slides with title and body text."""
    prs = _new_prs()
    for i, (title, body) in enumerate([
        ("Introduction", "This is slide one.\nIt has two lines."),
        ("Conclusion", "This is slide two.\nEnd of presentation."),
    ], start=1):
        layout = prs.slide_layouts[1]
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = title
        slide.placeholders[1].text = body
    _save(prs, out)


def make_multi_slide(out: Path) -> None:
    """multi-slide.pptx — five slides with varied content."""
    prs = _new_prs()
    titles = ["Overview", "Problem", "Solution", "Results", "Next Steps"]
    for title in titles:
        layout = prs.slide_layouts[1]
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = title
        slide.placeholders[1].text = f"Content for {title} slide."
    _save(prs, out)


def make_tables(out: Path) -> None:
    """tables.pptx — slide with a data table."""
    prs = _new_prs()
    layout = prs.slide_layouts[5]  # title only
    slide = prs.slides.add_slide(layout)
    slide.shapes.title.text = "Sales Data"

    rows, cols = 4, 3
    left = top = Inches(1.0)
    width  = Inches(8.0)
    height = Inches(3.0)
    table = slide.shapes.add_table(rows, cols, left, top, width, height).table

    headers = ["Region", "Q3 Revenue", "Growth"]
    for col_idx, header in enumerate(headers):
        table.cell(0, col_idx).text = header

    data = [
        ("North", "$1.2M", "+12%"),
        ("South", "$0.9M", "+5%"),
        ("West",  "$1.5M", "+18%"),
    ]
    for row_idx, row_data in enumerate(data, start=1):
        for col_idx, val in enumerate(row_data):
            table.cell(row_idx, col_idx).text = val

    _save(prs, out)


def make_images_alt(out: Path) -> None:
    """images-alt.pptx — slide with a placeholder image and alt text."""
    import struct, zlib

    prs = _new_prs()
    layout = prs.slide_layouts[6]  # blank
    slide = prs.slides.add_slide(layout)

    # Create a minimal 10x10 white PNG in memory
    def _minimal_png() -> bytes:
        w, h = 10, 10
        raw = b"\x00" + b"\xff\xff\xff" * w
        raw_all = raw * h
        compressed = zlib.compress(raw_all)
        def _chunk(name: bytes, data: bytes) -> bytes:
            crc = zlib.crc32(name + data) & 0xFFFFFFFF
            return struct.pack(">I", len(data)) + name + data + struct.pack(">I", crc)
        return (
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", compressed)
            + _chunk(b"IEND", b"")
        )

    img_bytes = _minimal_png()
    img_stream = io.BytesIO(img_bytes)
    pic = slide.shapes.add_picture(img_stream, Inches(1), Inches(1), Inches(2), Inches(2))
    pic.name = "Sample Image"

    # Add a text box to describe the image
    txb = slide.shapes.add_textbox(Inches(3.5), Inches(1), Inches(5), Inches(1))
    txb.text_frame.text = "Image with alt text example"

    _save(prs, out)


def make_speaker_notes(out: Path) -> None:
    """speaker-notes.pptx — slides with speaker notes."""
    prs = _new_prs()
    notes_data = [
        ("Key Metrics", "Emphasise the 15% revenue growth. Mention Q3 beat expectations."),
        ("Roadmap",     "Focus on H1 2027 deliverables. Do not discuss acquisition rumours."),
        ("Q&A",         "Common questions: timeline, budget, headcount. Keep answers brief."),
    ]
    for title, notes in notes_data:
        layout = prs.slide_layouts[1]
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = title
        slide.placeholders[1].text = f"Slide: {title}"
        notes_slide = slide.notes_slide
        notes_slide.notes_text_frame.text = notes
    _save(prs, out)


def make_external_rels(out: Path) -> None:
    """external-rels.pptx — slide with an external hyperlink relationship."""
    prs = _new_prs()
    layout = prs.slide_layouts[1]
    slide = prs.slides.add_slide(layout)
    slide.shapes.title.text = "External Links Slide"
    slide.placeholders[1].text = "See references below."

    # Add a text box with a hyperlink
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    txb = slide.shapes.add_textbox(Inches(1), Inches(3), Inches(7), Inches(1))
    tf = txb.text_frame
    p = tf.paragraphs[0]
    run = p.add_run()
    run.text = "Visit example.com"
    hlink = run.hyperlink
    hlink.address = "https://example.com"

    _save(prs, out)


def make_corrupt(out: Path) -> None:
    """corrupt.pptx — truncated ZIP (invalid file)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"PK\x03\x04" + b"\x00" * 50)
    print(f"  created: {out.name}")


def make_zipbomb(out: Path) -> None:
    """
    zipbomb.pptx — high compression ratio entry (safe size, triggers ratio check).
    Uses a small repeated-byte payload that compresses extremely well.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    # Write a minimal valid PPTX structure but include one highly compressible entry
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Minimal content types
        zf.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
</Types>""")
        zf.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>""")
        # High-ratio entry: 100 KB of repeated bytes → compresses to ~100 bytes
        zf.writestr("ppt/bomb.bin", b"\x00" * 102400)
    out.write_bytes(buf.getvalue())
    print(f"  created: {out.name}")


def make_macro_stub(out: Path) -> None:
    """
    macro-stub.pptm — .pptx ZIP with a stub vbaProject.bin to simulate a macro file.
    Not an actual working macro; just triggers the has_macros flag.
    """
    # Start with a valid simple pptx, add vbaProject.bin
    prs = _new_prs()
    layout = prs.slide_layouts[1]
    slide = prs.slides.add_slide(layout)
    slide.shapes.title.text = "Macro Stub"
    slide.placeholders[1].text = "This file contains a stub vbaProject.bin."

    # Save to buffer then repack with vbaProject.bin added
    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)

    out.parent.mkdir(parents=True, exist_ok=True)
    out_buf = io.BytesIO()
    with zipfile.ZipFile(buf, "r") as zin:
        with zipfile.ZipFile(out_buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
            # Add stub vbaProject.bin (OLE magic bytes, not a real macro)
            zout.writestr("ppt/vbaProject.bin", b"\xd0\xcf\x11\xe0" + b"\x00" * 508)
    out.write_bytes(out_buf.getvalue())
    print(f"  created: {out.name}")


def make_scanned_image_slide(out: Path) -> None:
    """scanned-image-slide.pptx — slide with an embedded PNG (simulates a scanned image)."""
    make_images_alt(out)  # reuse same pattern — contains an embedded PNG


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

FIXTURES = {
    "simple.pptx":               make_simple,
    "multi-slide.pptx":          make_multi_slide,
    "tables.pptx":               make_tables,
    "images-alt.pptx":           make_images_alt,
    "speaker-notes.pptx":        make_speaker_notes,
    "external-rels.pptx":        make_external_rels,
    "corrupt.pptx":              make_corrupt,
    "zipbomb.pptx":              make_zipbomb,
    "macro-stub.pptm":           make_macro_stub,
    "scanned-image-slide.pptx":  make_scanned_image_slide,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="make-fixtures.py",
        description="Generate synthetic .pptx fixture files for pptx skill evals.",
    )
    parser.add_argument(
        "--outdir", default=str(Path(__file__).parent / "fixtures"),
        help="Output directory (default: evals/fixtures/)",
    )
    parser.add_argument(
        "--only", nargs="*", metavar="NAME",
        help="Only generate specific fixture(s) by filename",
    )
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    targets = args.only or list(FIXTURES.keys())
    print(f"Generating {len(targets)} fixture(s) in {outdir}/")

    for name in targets:
        if name not in FIXTURES:
            print(f"  unknown fixture: {name}", file=sys.stderr)
            continue
        try:
            FIXTURES[name](outdir / name)
        except Exception as exc:
            print(f"  ERROR generating {name}: {exc}", file=sys.stderr)

    print("Done.")


if __name__ == "__main__":
    main()
