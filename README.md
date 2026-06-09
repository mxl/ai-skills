# AI Skills

Repository for storing and versioning AI assistant skills.

## Skills

### [ocr](ocr/)

Extract text from scanned PDFs and images (PNG/JPG/TIFF/HEIC) using OCR. Use when a PDF's text cannot be selected or copied, the document is a scan or photo, or the file is a receipt, screenshot, fax, ID card, form, or presentation slide. Handles language auto-detection, deskew/denoise for messy scans, tables and charts via vision escalation, and produces Markdown plus plain-text output.

### [pdf](pdf/)

Work with PDF files — extract text, metadata, and structure. Hands off to the `ocr` skill when a page has no selectable text layer.
