# AI Skills

Repository for storing and versioning AI assistant skills.

## Installation

Skills in this repo follow the [Agent Skills](https://agentskills.io) open standard and work with both **Claude Code** and **OpenCode**.

### 1. Clone the repo

```sh
git clone https://github.com/mxl/ai-skills.git ~/ai-skills
```

### 2. OpenCode global install

Add the repo root to `skills.paths` in `~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "skills": {
    "paths": ["/Users/michaelledin/projects/ai-skills"]
  }
}
```

OpenCode scans this path recursively for `SKILL.md`, so every skill in this repo is available globally. Restart OpenCode after changing config or adding skills.

### Claude Code install

Claude Code discovers skills placed in `~/.claude/skills/`. Symlink individual skills:

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

### [youtube-summary](youtube-summary/)

Extract YouTube subtitles with `yt-dlp`, clean VTT captions into `transcript.txt`, and produce an LLM-written video summary saved as `summary.md`.

Use it by asking OpenCode to summarize a YouTube URL, download original subtitles, or clean an existing `.vtt` file. The skill runs `youtube-summary/scripts/youtube-summary.mjs`, writes `transcript.txt` and `metadata.json`, then the agent reads the transcript and writes `summary.md`.

```sh
node youtube-summary/scripts/youtube-summary.mjs "https://www.youtube.com/watch?v=..."
node youtube-summary/scripts/youtube-summary.mjs --input-vtt subtitles.vtt
```

**Requires:** `node`, `yt-dlp`
