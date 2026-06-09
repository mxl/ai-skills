# AI Skills

Repository for storing and versioning AI assistant skills.

## Installation

Skills in this repo follow the [Agent Skills](https://agentskills.io) open standard and work with both **Claude Code** and **OpenCode**. Both agents discover skills placed in `~/.claude/skills/`.

### 1. Clone the repo

```sh
git clone https://github.com/mxl/ai-skills.git ~/ai-skills
```

### 2. Symlink a skill (recommended)

```sh
mkdir -p ~/.claude/skills
ln -s ~/ai-skills/ocr ~/.claude/skills/ocr
```

Symlinks stay up to date automatically — just run `git pull` in `~/ai-skills` to get updates.

### Alternative: copy instead of symlink

```sh
cp -r ~/ai-skills/ocr ~/.claude/skills/ocr
```

Re-copy after `git pull` to pick up updates.

### OpenCode native path

OpenCode also discovers skills in its own config directory:

```sh
ln -s ~/ai-skills/ocr ~/.config/opencode/skills/ocr
```

### Project-local install

To scope a skill to a single project:

```sh
mkdir -p .claude/skills
ln -s ~/ai-skills/ocr .claude/skills/ocr
```

## Skills

### [ocr](ocr/)

Extract text from scanned PDFs and images (PNG/JPG/TIFF/HEIC) using OCR. Use when a PDF's text cannot be selected or copied, the document is a scan or photo, or the file is a receipt, screenshot, fax, ID card, form, or presentation slide. Handles language auto-detection, deskew/denoise for messy scans, tables and charts via vision escalation, and produces Markdown plus plain-text output.

**Requires:** `tesseract`, `poppler`

### [pdf](pdf/)

Work with PDF files — extract text, metadata, and structure. Hands off to the `ocr` skill when a page has no selectable text layer.

**Requires:** `poppler`
