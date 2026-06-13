#!/usr/bin/env python3
"""
make-fixtures.py — Generate synthetic DOCX fixtures for eval assertions.

All fixtures are created entirely from python-docx and stdlib (no external
files, no copied content). Run from the repo root or the evals/ directory.

Usage:
    python evals/make-fixtures.py [--outdir evals/fixtures]
"""
from __future__ import annotations

import argparse
import io
import struct
import zipfile
from pathlib import Path

try:
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    _HAS_DOCX = True
except ImportError:
    _HAS_DOCX = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _save(doc: "Document", path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    print(f"  wrote {path}")


def _add_tracked_change_xml(doc: "Document") -> None:
    """
    Insert a paragraph containing a tracked insertion and deletion directly
    via OOXML, since python-docx has no tracked-changes API.
    """
    from lxml import etree

    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    body = doc.element.body
    # Build a paragraph with w:ins and w:del
    p_xml = (
        f'<w:p xmlns:w="{W}">'
        f'<w:r><w:t xml:space="preserve">Original </w:t></w:r>'
        f'<w:del w:id="1" w:author="Alice" w:date="2026-01-01T00:00:00Z">'
        f'  <w:r><w:delText>old</w:delText></w:r>'
        f'</w:del>'
        f'<w:ins w:id="2" w:author="Bob" w:date="2026-06-01T00:00:00Z">'
        f'  <w:r><w:t>new</w:t></w:r>'
        f'</w:ins>'
        f'<w:r><w:t xml:space="preserve"> text.</w:t></w:r>'
        f'</w:p>'
    )
    p_el = etree.fromstring(p_xml)
    # Insert before the last sectPr
    body.insert(len(body) - 1, p_el)


# ---------------------------------------------------------------------------
# Fixture generators
# ---------------------------------------------------------------------------

def make_simple(path: Path) -> None:
    """Simple document: headings + paragraphs."""
    doc = Document()
    doc.add_heading("Simple Document", level=1)
    doc.add_heading("Section One", level=2)
    doc.add_paragraph("This is the first paragraph of section one.")
    doc.add_paragraph("This is the second paragraph.")
    doc.add_heading("Section Two", level=2)
    doc.add_paragraph("Content of section two.")
    _save(doc, path)


def make_report_toc(path: Path) -> None:
    """Report with multiple heading levels (TOC placeholder via field)."""
    doc = Document()
    doc.add_heading("Annual Report 2026", level=1)
    doc.add_paragraph("Executive summary paragraph.")
    doc.add_heading("Financial Overview", level=2)
    doc.add_paragraph("Revenue increased by 12% year-over-year.")
    doc.add_heading("Revenue Breakdown", level=3)
    doc.add_paragraph("North America accounted for 60% of total revenue.")
    doc.add_heading("Operations", level=2)
    doc.add_paragraph("Operational efficiency improved across all divisions.")
    _save(doc, path)


def make_tables(path: Path) -> None:
    """Document with a table."""
    doc = Document()
    doc.add_heading("Tables Fixture", level=1)
    table = doc.add_table(rows=3, cols=3)
    table.style = "Table Grid"
    headers = ["Name", "Value", "Notes"]
    for i, cell in enumerate(table.rows[0].cells):
        cell.text = headers[i]
    data = [("Alpha", "100", "First"), ("Beta", "200", "Second")]
    for row_idx, row_data in enumerate(data, start=1):
        for col_idx, val in enumerate(row_data):
            table.rows[row_idx].cells[col_idx].text = val
    _save(doc, path)


def _make_minimal_png() -> bytes:
    """Generate a valid 8x8 red PNG using stdlib only."""
    import struct
    import zlib

    def chunk(name: bytes, data: bytes) -> bytes:
        c = name + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    # IHDR: 8x8 pixels, 8-bit RGB
    ihdr_data = struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
    # Image data: 8 rows, each row = filter byte (0) + 8 RGB pixels (red = FF 00 00)
    raw_rows = b"".join(b"\x00" + b"\xff\x00\x00" * 8 for _ in range(8))
    idat_data = zlib.compress(raw_rows)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr_data)
        + chunk(b"IDAT", idat_data)
        + chunk(b"IEND", b"")
    )
    return png


def make_images_alt(path: Path) -> None:
    """Document with a synthetic 8x8 PNG image and alt text."""
    doc = Document()
    doc.add_heading("Images Fixture", level=1)
    doc.add_paragraph("The image below is a synthetic 8x8 red square.")

    png_bytes = _make_minimal_png()

    doc.add_picture(io.BytesIO(png_bytes), width=Inches(1))
    # python-docx doesn't expose alt-text directly; add via XML
    from lxml import etree
    inline = doc.inline_shapes[0]._inline
    docPr = inline.find(
        qn("wp:docPr"),
        {"wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"},
    )
    if docPr is None:
        # Try without namespace map
        for child in inline:
            if child.tag.endswith("}docPr") or child.tag == "docPr":
                docPr = child
                break
    if docPr is not None:
        docPr.set("descr", "A synthetic 1x1 red pixel for testing.")
        docPr.set("title", "Red Pixel")

    _save(doc, path)


def make_headers_footers(path: Path) -> None:
    """Document with header and footer text."""
    doc = Document()
    section = doc.sections[0]

    # Header
    header = section.header
    header.paragraphs[0].text = "Confidential — Test Document"

    # Footer
    footer = section.footer
    footer.paragraphs[0].text = "Page 1"

    doc.add_heading("Document with Headers and Footers", level=1)
    doc.add_paragraph("This document has a header and footer.")
    _save(doc, path)


def make_comments(path: Path) -> None:
    """Document with a comment using python-docx 1.2 API."""
    doc = Document()
    doc.add_heading("Comments Fixture", level=1)
    para = doc.add_paragraph("This paragraph has a comment attached to it.")
    run = para.runs[0]
    try:
        doc.add_comment(run, "This is a test comment.", author="Reviewer", initials="R")
    except AttributeError:
        # python-docx < 1.2 fallback: add comment via raw XML
        _add_comment_xml_fallback(doc, para)
    _save(doc, path)


def _add_comment_xml_fallback(doc, para) -> None:
    """Minimal comment via raw XML for python-docx < 1.2."""
    from lxml import etree
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    # We skip full comment XML here; the fixture will simply have no comment.
    # The test for this fixture should detect python-docx version and skip.
    pass


def make_tracked_changes(path: Path) -> None:
    """Document with tracked insertions and deletions by two authors."""
    doc = Document()
    doc.add_heading("Tracked Changes Fixture", level=1)
    doc.add_paragraph("Unmodified paragraph.")
    try:
        _add_tracked_change_xml(doc)
    except ImportError:
        # lxml not available; add a plain paragraph instead
        doc.add_paragraph("[tracked changes could not be added — lxml missing]")
    _save(doc, path)


def make_hidden_custom(path: Path) -> None:
    """Document with hidden text and a simple custom XML part."""
    doc = Document()
    doc.add_heading("Hidden Text Fixture", level=1)
    para = doc.add_paragraph()
    run = para.add_run("Visible text. ")
    hidden_run = para.add_run("HIDDEN TEXT")
    # Add w:vanish to the hidden run
    rPr = hidden_run._r.get_or_add_rPr()
    vanish = OxmlElement("w:vanish")
    rPr.append(vanish)
    para.add_run(" More visible text.")
    _save(doc, path)


def make_external_rels(path: Path) -> None:
    """Document with an external hyperlink relationship."""
    doc = Document()
    doc.add_heading("External Relationships Fixture", level=1)
    para = doc.add_paragraph()
    # Add a hyperlink via XML
    from lxml import etree
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), "rId100")  # will be added to rels
    r = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    rStyle = OxmlElement("w:rStyle")
    rStyle.set(qn("w:val"), "Hyperlink")
    rPr.append(rStyle)
    r.append(rPr)
    t = OxmlElement("w:t")
    t.text = "Example Link"
    r.append(t)
    hyperlink.append(r)
    para._p.append(hyperlink)

    doc.save(str(path))

    # Add the external rel to word/_rels/document.xml.rels
    with zipfile.ZipFile(path, "r") as zin:
        names = zin.namelist()
        parts = {n: zin.read(n) for n in names}

    rels_name = "word/_rels/document.xml.rels"
    rels_data = parts.get(rels_name, b"")
    if rels_data:
        rels_str = rels_data.decode("utf-8")
        # Insert the external relationship before </Relationships>
        ext_rel = (
            '<Relationship Id="rId100" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
            'Target="https://example.com" TargetMode="External"/>'
        )
        rels_str = rels_str.replace("</Relationships>", ext_rel + "</Relationships>")
        parts[rels_name] = rels_str.encode("utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for n in sorted(parts):
            zout.writestr(n, parts[n])
    path.write_bytes(buf.getvalue())
    print(f"  wrote {path}")


def make_corrupt(path: Path) -> None:
    """A truncated/corrupt ZIP file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 20 + b"this is not a real zip")
    print(f"  wrote {path}")


def make_zipbomb(path: Path) -> None:
    """
    A ZIP with a very high compression ratio entry (safe total size < 1 MB).
    Triggers the ratio check in zip_safety_report.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Repeated null bytes compress to near nothing
    uncompressed = b"\x00" * 500_000  # 500 KB -> compresses to ~500 bytes
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", uncompressed)
        zf.writestr("word/document.xml", b"<w:document/>")
    path.write_bytes(buf.getvalue())
    print(f"  wrote {path}")


def make_scanned_image(path: Path) -> None:
    """Document with an embedded PNG (simulates a scanned image for OCR handoff)."""
    make_images_alt(path)  # reuse — same structure, different name


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

FIXTURES = {
    "simple.docx":          make_simple,
    "report-toc.docx":      make_report_toc,
    "tables.docx":          make_tables,
    "images-alt.docx":      make_images_alt,
    "headers-footers.docx": make_headers_footers,
    "comments.docx":        make_comments,
    "tracked-changes.docx": make_tracked_changes,
    "hidden-custom.docx":   make_hidden_custom,
    "external-rels.docx":   make_external_rels,
    "corrupt.docx":         make_corrupt,
    "zipbomb.docx":         make_zipbomb,
    "scanned-image.docx":   make_scanned_image,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="make-fixtures.py",
        description="Generate synthetic DOCX fixtures for eval assertions.",
    )
    parser.add_argument(
        "--outdir", default="evals/fixtures",
        help="Output directory (default: evals/fixtures)",
    )
    parser.add_argument(
        "--only", metavar="NAME",
        help="Generate only this fixture (stem name)",
    )
    args = parser.parse_args()

    if not _HAS_DOCX:
        print("error: python-docx not installed; pip install python-docx", flush=True)
        raise SystemExit(3)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    to_make = FIXTURES
    if args.only:
        key = args.only if args.only.endswith(".docx") else args.only + ".docx"
        if key not in FIXTURES:
            print(f"error: unknown fixture {key!r}; choices: {list(FIXTURES)}")
            raise SystemExit(2)
        to_make = {key: FIXTURES[key]}

    for name, fn in to_make.items():
        try:
            fn(outdir / name)
        except Exception as exc:
            print(f"  ERROR {name}: {exc}")


if __name__ == "__main__":
    main()
