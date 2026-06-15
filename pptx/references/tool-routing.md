# Tool Routing Reference

## Decision Tree

| Task | Primary | Fallback | Missing dep action |
|------|---------|----------|--------------------|
| Create new deck (rich) | `pptxgenjs` (npm) | `python-pptx` | `npm install -g pptxgenjs` |
| Create simple deck | `python-pptx` | — | `pip install python-pptx` |
| Fill slide template | `scripts/fill-template.py` | direct placeholder replace | python-pptx required |
| Read / extract text + notes | `scripts/extract.py` (python-pptx) | pandoc / markitdown | python-pptx required |
| Edit existing deck | safe-unpack → Edit XML → pack | python-pptx (simple edits) | — |
| Inspect / audit | `scripts/inspect.py` | — | — |
| Sanitize / privacy | `scripts/sanitize.py` | — | — |
| Validate | `scripts/validate.py` | — | — |
| `.ppt` → `.pptx` | `soffice --convert-to pptx` | — | brew install --cask libreoffice |
| `.pptx` → PDF | `soffice --convert-to pdf` | — | brew install --cask libreoffice |
| `.pptx` → PNG (QA) | `scripts/thumbnails.py` (soffice + pdftoppm) | — | libreoffice + poppler |
| `.pptx` → Markdown | pandoc | `scripts/extract.py` fallback | brew install pandoc |
| Markdown → `.pptx` | pandoc | — | brew install pandoc |
| OCR slide images | extract `ppt/media/` → `ocr` skill | — | see ocr/ skill |

---

## Environment-Aware Routing

`scripts/convert.py` and `scripts/thumbnails.py` detect available engines at
runtime and degrade gracefully. Exit code 3 means a required engine is missing;
the error message includes the install command.

Check what is available:

```bash
command -v soffice   && echo "soffice ok"   || echo "MISSING — brew install --cask libreoffice"
command -v pandoc    && echo "pandoc ok"    || echo "MISSING — brew install pandoc"
command -v pdftoppm  && echo "pdftoppm ok"  || echo "MISSING — brew install poppler"
command -v node      && echo "node ok"      || echo "MISSING — brew install node"
npm ls -g pptxgenjs 2>/dev/null | grep pptxgenjs || echo "pptxgenjs MISSING — npm install -g pptxgenjs"
python3 -c "import pptx; print('python-pptx', pptx.__version__)" 2>/dev/null || echo "python-pptx MISSING"
```

---

## Dependency Tiers

### Tier 1 — Required (zero-install MVP, works today)

| Tool | Version | Notes |
|------|---------|-------|
| `python3` | 3.9+ | system |
| `python-pptx` | 1.0.2 | `pip install python-pptx` |
| `defusedxml` | 0.7.1 | `pip install defusedxml` |

Capabilities without extra installs: create/edit/validate/inspect/sanitize/extract
`.pptx`, fill templates, roundtrip XML editing.

### Tier 2 — Recommended (full pipeline)

| Tool | Install | Adds |
|------|---------|------|
| `pptxgenjs` | `npm install -g pptxgenjs` | Rich deck creation: charts, masters, data-driven |
| LibreOffice | `brew install --cask libreoffice` | `.ppt`→`.pptx`, PDF/PNG export |
| `pandoc` | `brew install pandoc` | High-fidelity `md`↔`pptx`, `.pptx`→`.md` |
| `poppler` (`pdftoppm`) | `brew install poppler` | Thumbnail rendering (required alongside soffice) |

### Tier 3 — Optional / Heavy

| Tool | Use case | Install |
|------|---------|---------|
| `markitdown` | LLM-oriented extraction | `pip install markitdown` |
| `unstructured` | AI chunking pipeline | `pip install unstructured` |
| `docling` | AI-native document model | `pip install docling` |
| Apache Tika | `.ppt` text extraction (Java) | run as isolated server |

---

## Tool Capability Matrix

| Tool | Create | Edit | Read→MD | Notes | `.ppt` | PDF out | Templates |
|------|--------|------|---------|-------|--------|---------|-----------|
| python-pptx 1.0.2 | ✓ | ✓ | partial | ✓ read | ✗ | ✗ | ✗ |
| pptxgenjs 4.x | ✓✓ | ✗ | ✗ | ✓ write | ✗ | ✗ | ✗ |
| pandoc | via MD | ✗ | ✓✓ | ✗ | ✗ | ✓ | ✗ |
| soffice | convert | ✗ | ✗ | ✗ | ✓✓ | ✓✓ | ✗ |
| fill-template.py | ✗ | template | ✗ | ✗ | ✗ | ✗ | ✓ |
| direct OOXML | ✓ | ✓✓ | ✗ | ✓✓ | ✗ | ✗ | ✗ |
