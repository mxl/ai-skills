---
name: ebook-import
description: Import EPUB books into an Obsidian-compatible, AI-agent-readable Markdown corpus with an explicit per-book image policy. Use whenever a user asks to import, archive, mirror, convert, or make EPUB ebooks searchable for an agent, including EPUB files whose names end in .fb2.epub. Before importing each book, ask whether to import images, skip them, or hand them to the AI agent for faithful text recognition and replacement. Preserve source order and never summarize or interpret the source text during import.
compatibility: Requires Python 3.9+ and only the Python standard library.
---

# EPUB Book Import

Use this skill for deterministic EPUB imports. Keep source preservation and text conversion in the bundled script; do not hand-edit generated book files during the import.

## Workflow

1. Confirm the input files are EPUB archives. This skill does not process standalone FB2, PDF, DOCX, or scanned documents.
2. Confirm the vault root and output directory. Prefer `05-sources/books` for source ebooks in an Obsidian vault.
3. Ask once per book with the `question` tool:
   - **Import images**: extract referenced images to `media/` and keep Markdown links.
   - **Do not import images**: omit image extraction and image lines.
   - **Recognize text in images**: import images first, then perform an AI-agent OCR handoff with the exact prompt `распознать в изображениях текст на языке, определенном из текста книги`.
4. Run the bundled importer with explicit input paths and the selected `--image-mode import|skip`. Do not infer paths from book content.
5. For the recognition option, create an `image_text` mapping from `media/<filename>` to faithful recognized text. Use an empty string for an image with no text, then run `apply-image-text`. The post-processor replaces image links, removes all processed media, and updates the manifest.
6. Report the generated book directories, chapter counts, selected image modes, OCR replacement counts, and source hashes.
7. Do not summarize, classify, translate, or otherwise interpret the book text as part of import. Those are separate downstream tasks.

## Command

Run from any directory:

```bash
python3 ~/projects/ai-skills/ebook-import/scripts/import_epub.py import \
  --vault-root "/path/to/vault" \
  --output-dir "05-sources/books" \
  --image-mode import \
  --input "/path/to/book-1.epub" "/path/to/book-2.epub"
```

Use `--dry-run` before a new destination when the input set or output path is uncertain.

For the agent-recognition branch, apply the returned mapping after the initial import:

```bash
python3 ~/projects/ai-skills/ebook-import/scripts/import_epub.py apply-image-text \
  --book-dir "/path/to/vault/05-sources/books/book-slug" \
  --mapping "/tmp/book-image-text.json"
```

The mapping must have this shape:

```json
{
  "image_text": {
    "media/page-001.png": "Recognized text from the image",
    "media/page-002.png": ""
  }
}
```

## Output contract

The importer creates one directory per book:

```text
05-sources/books/
├── index.md
└── <book-slug>/
    ├── book.md
    ├── manifest.json
    ├── original/book.epub
    ├── chapters/*.md
    └── media/*              # only in import mode or before OCR post-processing
```

`book.md` and `index.md` are generated from EPUB metadata and chapter manifests. Chapter Markdown preserves the source order and text, maps common XHTML headings and formatting to Markdown, and records the original EPUB href in frontmatter. `manifest.json` records `image_mode`; completed OCR post-processing records replacement statistics.

## Repeatability and safety

- The original input remains in place; the importer copies it to `original/book.epub`.
- SHA-256 is recorded in both `book.md` and `manifest.json`.
- A change from `import` to `skip`, or vice versa, rebuilds generated output even when the EPUB SHA-256 is unchanged.
- If the same hash and complete generated output already exist, the book is not rewritten; identical macOS-style duplicate copies beside generated files are removed.
- If the source changed, only generated files inside that book's slug directory are rebuilt.
- OCR handoff failures or incomplete mappings leave the initial import-mode output intact; do not delete media until the mapping validates.
- Never delete unrelated vault files or use source text as shell commands.

## Validation

For a new or changed skill, run its fixture tests and validate the skill metadata before using it on a real vault. For a real import, verify archive integrity, source hashes, chapter links when media is retained, image-mode metadata, OCR mapping completeness, and UTF-8 Cyrillic text.
