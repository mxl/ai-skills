# Tool Routing Reference

## Decision Tree

| Task | Primary | Fallback 1 | Fallback 2 | Missing dep action |
|------|---------|-----------|-----------|-------------------|
| Read `.docx` → Markdown | `pandoc --track-changes=all -t gfm` | `scripts/extract.py` | — | Warn: tracked changes may be missing |
| Extract structured JSON | `scripts/extract.py --format json` | `docx2python` | — | python-docx required |
| Extract headers/footers/footnotes | `docx2python` | `scripts/extract.py` (sections API) | direct XML parse | pip install docx2python |
| Extract comments | `python-docx` 1.2 `.comments` | `scripts/extract.py` (XML fallback) | — | — |
| Extract tracked changes | `pandoc --track-changes=all` | direct XML parse (no library hides them) | — | brew install pandoc |
| Create new `.docx` | `npm docx` (docx-js) | `python-docx` | `pandoc` from Markdown | See docx-js.md |
| Fill Word template | `docxtpl` via `scripts/fill-template.py` | direct OOXML placeholder replace | — | pip install docxtpl |
| Edit existing `.docx` | unpack → Edit tool → pack | `python-docx` (simple edits) | — | — |
| Add tracked changes | direct OOXML (unpack → edit → pack) | — | — | No library does this reliably |
| Add comments | `python-docx` 1.2 `add_comment` | direct OOXML | — | pip install python-docx>=1.2 |
| `.doc` → `.docx` | `soffice --headless --convert-to docx` | `textutil -convert docx` (macOS) | — | brew install --cask libreoffice |
| `.docx` → PDF | `soffice --headless --convert-to pdf` | — | — | brew install --cask libreoffice |
| `.docx` → PNG preview | soffice → PDF → `pdftoppm -png` | — | — | brew install poppler |
| Markdown → `.docx` | `pandoc input.md -o output.docx` | — | — | brew install pandoc |
| Inspect / audit | `scripts/inspect.py` | — | — | — |
| Sanitize / privacy | `scripts/sanitize.py` | — | — | — |
| Validate | `scripts/validate.py` | — | — | — |
| OCR embedded images | extract `word/media/` → `ocr` skill | — | — | See ocr/ skill |

---

## Environment-aware routing

`scripts/convert.py` detects available engines at runtime and degrades
gracefully. Exit code 3 means a required engine is missing; the error message
includes the install command.

Check what is available on the current machine:

```bash
command -v pandoc   && echo "pandoc ok"   || echo "pandoc MISSING — brew install pandoc"
command -v soffice  && echo "soffice ok"  || echo "soffice MISSING — brew install --cask libreoffice"
command -v pdftoppm && echo "pdftoppm ok" || echo "pdftoppm MISSING — brew install poppler"
command -v node     && echo "node ok"     || echo "node MISSING"
python3 -c "import docx;    print('python-docx ok')"  2>/dev/null || echo "python-docx MISSING"
python3 -c "import docxtpl; print('docxtpl ok')"      2>/dev/null || echo "docxtpl MISSING"
python3 -c "import docx2python; print('docx2python ok')" 2>/dev/null || echo "docx2python MISSING"
npm ls -g docx 2>/dev/null | grep docx || echo "npm docx MISSING — npm install -g docx"
```

---

## Dependency Tiers

### Tier 1 — MVP (works on this machine today)

| Tool | Version | Install |
|------|---------|---------|
| `python3` | 3.9.6 | system |
| `python-docx` | 1.2.0 | already installed |
| `defusedxml` | 0.7.1 | already installed |
| `textutil` | — | macOS built-in |
| `pdftoppm` | — | already installed |
| `tesseract` | 5.5.2 | already installed |

Capabilities without any extra installs: create/edit/validate/inspect/sanitize
`.docx`, extract Markdown/JSON/text, fill templates (after `pip install docxtpl`),
OCR handoff, macOS `.doc` → `.docx` via textutil (low fidelity).

### Tier 2 — Recommended (full pipeline)

| Tool | Install |
|------|---------|
| `pandoc` | `brew install pandoc` |
| LibreOffice | `brew install --cask libreoffice` |
| npm `docx` 9.x | `npm install -g docx` |
| `docxtpl` | `pip install docxtpl` or `uv pip install docxtpl` |
| `docx2python` | `pip install docx2python` |

Adds: high-fidelity `.docx` → Markdown (with tracked changes), `.doc` → `.docx`
(high fidelity), `.docx` → PDF/PNG, Markdown → `.docx`, rich JS document
generation, structured extraction with footnotes/endnotes.

### Tier 3 — Optional / heavy

| Tool | Use case | Install |
|------|---------|---------|
| `markitdown` | LLM-oriented Markdown extraction | `pip install markitdown` |
| `docling` | AI-native document model, RAG | `pip install docling` |
| `unstructured` | AI chunking pipeline | `pip install unstructured` |
| Apache Tika | Multi-format extraction including `.doc` | Java, run as server |
| `mammoth` | Clean semantic HTML from `.docx` | `pip install mammoth` |

---

## Tool Capability Matrix

| Tool | Create | Edit | Read→MD | Tracked Δ | Comments | `.doc` | PDF out | Templates |
|------|--------|------|---------|-----------|----------|-------|---------|-----------|
| python-docx 1.2 | ✓ | ✓ | partial | read-only¹ | ✓ native | ✗ | ✗ | ✗ |
| npm docx 9.x | ✓ | patch | ✗ | ✗ | ✓ | ✗ | ✗ | ✗ |
| pandoc | via MD | ✗ | ✓✓ | ✓✓ | ✓ | ✗ | ✓ | ✗ |
| docxtpl | ✗ | template | ✗ | ✗ | ✗ | ✗ | ✗ | ✓✓ |
| docx2python | ✗ | ✗ | partial | ✗ | ✓ | ✗ | ✗ | ✗ |
| soffice | convert | ✗ | ✗ | accept | ✗ | ✓✓ | ✓✓ | ✗ |
| textutil | ✗ | ✗ | text | ✗ | ✗ | ✓ low | ✗ | ✗ |
| direct OOXML | ✓ | ✓✓ | ✗ | ✓✓ | ✓✓ | ✗ | ✗ | ✗ |

¹ python-docx `.paragraphs` / `.tables` skip content inside `w:ins` / `w:del`.
