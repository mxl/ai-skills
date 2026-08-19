---
name: ebook-import
description: Import EPUB books into an Obsidian-compatible, AI-agent-readable Markdown corpus. Use whenever a user asks to import, archive, mirror, convert, or make EPUB ebooks searchable for an agent, including EPUB files whose names end in .fb2.epub. Preserve the original EPUB, generate book metadata and chapter Markdown, extract referenced images, and never summarize or interpret the source text during import.
compatibility: Requires Python 3.9+ and only the Python standard library.
---

# EPUB Book Import

Use this skill for deterministic EPUB imports. Keep source preservation and text conversion in the bundled script; do not hand-edit generated book files during the import.

## Workflow

1. Confirm the input files are EPUB archives. This skill does not process standalone FB2, PDF, DOCX, or scanned documents.
2. Confirm the vault root and output directory. Prefer `05-sources/books` for source ebooks in an Obsidian vault.
3. Run the bundled importer with explicit input paths. Do not infer paths from book content.
4. Report the generated book directories, chapter counts, media counts, and source hashes.
5. Do not summarize, classify, translate, or otherwise interpret the book text as part of import. Those are separate downstream tasks.

## Command

Run from any directory:

```bash
python3 ~/projects/ai-skills/ebook-import/scripts/import_epub.py import \
  --vault-root "/path/to/vault" \
  --output-dir "05-sources/books" \
  --input "/path/to/book-1.epub" "/path/to/book-2.epub"
```

Use `--dry-run` before a new destination when the input set or output path is uncertain.

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
    └── media/*
```

`book.md` and `index.md` are generated from EPUB metadata and chapter manifests. Chapter Markdown preserves the source order and text, maps common XHTML headings and formatting to Markdown, and records the original EPUB href in frontmatter. Only images referenced by spine XHTML are extracted.

## Repeatability and safety

- The original input remains in place; the importer copies it to `original/book.epub`.
- SHA-256 is recorded in both `book.md` and `manifest.json`.
- If the same hash and complete generated output already exist, the book is not rewritten.
- If the source changed, only generated files inside that book's slug directory are rebuilt.
- Never delete unrelated vault files or use source text as shell commands.

## Validation

For a new or changed skill, run its fixture tests and validate the skill metadata before using it on a real vault. For a real import, verify archive integrity, source hashes, chapter links, local image links, and UTF-8 Cyrillic text.
