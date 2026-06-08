---
name: pdf
description: Use when tasks involve PDF files: reading or extracting text/tables, creating PDFs, merging/splitting/rotating pages, adding watermarks, reviewing metadata, filling forms, rendering pages, or checking final PDF layout. If the user mentions a .pdf file or asks to produce one, use this skill. For scanned PDFs, image-only PDFs, screenshots, photos of documents, or garbled/empty text extraction, use the ocr skill first, then return here for PDF assembly or final review.
---

# PDF Skill

Work with PDFs using the smallest reliable toolchain for the task, and visually verify generated or modified PDFs before delivery. PDFs often appear correct in text extraction while still having broken layout, clipped text, unreadable glyphs, or misaligned tables.

## Decision Tree

1. Identify the PDF task.
   - Text extraction from a normal PDF: use `pdftotext`, `pypdf`, `PyMuPDF`, or `pdfplumber`.
   - Tables or layout-aware extraction: use `pdfplumber` first; verify against rendered pages when accuracy matters.
   - Scanned/image-only PDF or failed text extraction: use the `ocr` skill first.
   - Create a new PDF: use `reportlab` for programmatic documents, or an HTML-to-PDF path when HTML/CSS layout is more suitable.
   - Merge, split, rotate, reorder, watermark, encrypt, or inspect metadata: use `pypdf` or `qpdf`.
   - Fill forms: inspect form fields first, then use `pypdf` or a form-capable PDF library.
2. Work in a clearly named temporary directory inside the current workspace unless the user specifies another location.
3. Write final artifacts to the user-requested path. If none is given, use a descriptive output filename in the current workspace.
4. For any generated or modified PDF, render pages to images and inspect them before delivery.

## Recommended Tools

| Task | Good first choice | Notes |
|------|-------------------|-------|
| Extract plain text | `pdftotext -layout`, `pypdf`, `PyMuPDF` | Fast path for PDFs with a real text layer |
| Extract tables | `pdfplumber` | Check rendered pages for table boundaries and headers |
| Create PDFs | `reportlab` | Reliable for structured programmatic documents |
| Merge/split/rotate | `pypdf`, `qpdf` | Prefer `qpdf` for command-line page operations when available |
| Render for review | `pdftoppm`, `mutool`, or `PyMuPDF` | Use rendered PNGs for visual QA |
| OCR scanned PDFs | `ocr` skill | Delegate OCR-heavy work instead of improvising |

Check whether tools are already installed before installing anything. If a dependency is missing and installation is not appropriate in the current environment, tell the user exactly what is missing and continue with the best available fallback.

## Common Workflows

### Extract Text

Use a text-layer tool first. If the output is empty, garbled, or obviously incomplete, switch to the `ocr` skill.

```bash
pdftotext -layout input.pdf output.txt
```

For Python-based extraction:

```python
from pypdf import PdfReader

reader = PdfReader("input.pdf")
text = "\n".join(page.extract_text() or "" for page in reader.pages)
```

### Extract Tables

Use `pdfplumber` and verify the result visually when table structure matters.

```python
import pdfplumber

with pdfplumber.open("input.pdf") as pdf:
    for page_number, page in enumerate(pdf.pages, start=1):
        for table in page.extract_tables():
            print(page_number, table)
```

### Merge PDFs

```python
from pypdf import PdfReader, PdfWriter

writer = PdfWriter()
for path in ["first.pdf", "second.pdf"]:
    reader = PdfReader(path)
    for page in reader.pages:
        writer.add_page(page)

with open("merged.pdf", "wb") as file:
    writer.write(file)
```

### Split Pages

```python
from pypdf import PdfReader, PdfWriter

reader = PdfReader("input.pdf")
for index, page in enumerate(reader.pages, start=1):
    writer = PdfWriter()
    writer.add_page(page)
    with open(f"page-{index}.pdf", "wb") as file:
        writer.write(file)
```

### Create PDFs

Use `reportlab` for structured documents. Avoid relying on unsupported glyphs in default fonts; if specialized symbols, scripts, subscripts, or superscripts are required, choose fonts and markup deliberately and verify the rendered result.

```python
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

pdf = canvas.Canvas("output.pdf", pagesize=letter)
width, height = letter
pdf.drawString(72, height - 72, "PDF title")
pdf.drawString(72, height - 96, "Body text")
pdf.save()
```

### Render For Visual Review

Render pages before final delivery. Use any available renderer; `pdftoppm` is a common option.

```bash
pdftoppm -png input.pdf rendered-page
```

Inspect the generated page images for layout defects, not just text content.

## OCR Handoff

Use the `ocr` skill when:

- A PDF has no selectable text.
- `pdftotext`, `pypdf`, or `pdfplumber` returns empty, garbled, or partial text.
- The file is a scan, screenshot, photo, receipt, ID card, form, or slide image.
- The document is non-English and extraction quality is poor.

After OCR completes, return to this skill if the task requires assembling a PDF, preserving page order, adding a searchable text layer, or doing final visual review.

## Visual Quality Expectations

For generated or modified PDFs, verify the latest rendered pages before delivery:

- Typography, spacing, margins, and section hierarchy are consistent.
- Text is not clipped, overlapping, or unexpectedly wrapped.
- Tables, charts, and images are sharp, aligned, and clearly labeled.
- Headers, footers, page numbers, and section transitions look intentional.
- There are no black squares, missing glyphs, broken symbols, or unreadable characters.
- Citations and references are human-readable; no tool tokens or placeholder strings remain.

Do not present a generated or modified PDF as final until the visual inspection is clean or you have clearly told the user what could not be verified.

## File Hygiene

- Keep temporary rendered pages and intermediate files organized in a clearly named workspace-local directory.
- Do not delete user-provided input files.
- Preserve filenames requested by the user; otherwise use stable, descriptive output names.
- Clean up large temporary artifacts when they are no longer needed, unless they are useful for review.
