# AI Skills

Reusable [Agent Skills](https://agentskills.io) for OpenCode and compatible coding assistants. This repository packages task-specific instructions, references, evals, fixtures, and deterministic helper scripts for workflows that benefit from repeatable handling instead of ad-hoc prompting.

The current collection focuses on document automation, OCR, PDFs, YouTube transcript summaries, and meeting transcript storage.

## Skills

| Skill | Use it for | Notable helpers |
| --- | --- | --- |
| [`docx`](docx/) | Create, read, edit, inspect, sanitize, validate, convert, and extract Microsoft Word `.docx` and legacy `.doc` files. | Safe OOXML unpack/pack, validation, extraction, conversion, template filling, metadata sanitization |
| [`ocr`](ocr/) | Extract text from scanned PDFs, screenshots, photos, forms, receipts, and image-only documents. | OCR probing, page preprocessing, quality reports, optional searchable PDF generation |
| [`pdf`](pdf/) | Read, extract, create, merge, split, render, inspect, and verify PDF files. | Tool-routing guidance and visual verification workflow |
| [`youtube-summary`](youtube-summary/) | Download YouTube subtitle tracks, clean VTT captions, and summarize transcripts. | `yt-dlp` subtitle downloader and VTT cleaner |
| [`meeting-transcript`](meeting-transcript/) | Save meeting transcripts and verified summaries into an Obsidian-style vault. | Storage rules, summary verification, action-item extraction guidance |

## Installation

### OpenCode

Clone the repository and add it to your OpenCode skill paths:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "skills": {
    "paths": ["/path/to/ai-skills"]
  }
}
```

Restart OpenCode after changing the config. OpenCode scans configured paths recursively for `SKILL.md` files.

### Claude Code

Claude Code loads skills from `.claude/skills` or `~/.claude/skills`. Symlink the skills you want:

```sh
mkdir -p ~/.claude/skills
ln -s /path/to/ai-skills/docx ~/.claude/skills/docx
ln -s /path/to/ai-skills/ocr ~/.claude/skills/ocr
ln -s /path/to/ai-skills/pdf ~/.claude/skills/pdf
ln -s /path/to/ai-skills/youtube-summary ~/.claude/skills/youtube-summary
ln -s /path/to/ai-skills/meeting-transcript ~/.claude/skills/meeting-transcript
```

For project-local installation, create the same symlinks under that project's `.claude/skills/` directory.

## Requirements

Install only the dependencies required by the skills you use.

| Dependency | Used by | Example macOS install |
| --- | --- | --- |
| Python 3.9+ | `docx`, `ocr`, `pdf` | `brew install python` |
| Node.js | `youtube-summary` | `brew install node` |
| `defusedxml` | DOCX validation and XML parsing | `python3 -m pip install defusedxml` |
| `python-docx` | DOCX extraction and simple document generation | `python3 -m pip install python-docx` |
| `docxtpl` | DOCX template filling | `python3 -m pip install docxtpl` |
| `pandoc` | Higher-fidelity DOCX/Markdown conversion | `brew install pandoc` |
| LibreOffice | `.doc` conversion and DOCX/PDF rendering | `brew install --cask libreoffice` |
| `poppler` | PDF rendering, text extraction, and OCR preprocessing | `brew install poppler` |
| `tesseract` | OCR | `brew install tesseract` |
| `yt-dlp` | YouTube subtitle download | `python3 -m pip install yt-dlp` |

Some workflows have optional fallback tools. See each skill's `SKILL.md` for task-specific requirements.

## Repository Layout

```text
ai-skills/
├── README.md
├── LICENSE
├── .docs/
│   └── docx-research.md
├── .github/workflows/
│   └── docx-validation.yml
├── docx/
│   ├── SKILL.md
│   ├── scripts/
│   ├── references/
│   ├── evals/
│   └── tests/
├── ocr/
│   ├── SKILL.md
│   ├── scripts/
│   ├── references/
│   └── evals/
├── pdf/
│   └── SKILL.md
├── youtube-summary/
│   ├── SKILL.md
│   └── scripts/
├── meeting-transcript/
│   ├── SKILL.md
│   └── templates/
├── generated/              # ignored output directory for skill development artifacts
└── tests/                  # shared test utilities
```

The `generated/` directory is git-ignored and used for transient outputs during skill development.

## Skill Anatomy

A skill directory usually contains:

```text
skill-name/
├── SKILL.md          # required routing metadata and agent instructions
├── scripts/          # optional deterministic helpers
├── references/       # optional deep technical notes
├── evals/            # optional trigger or workflow evals
└── tests/            # optional regression tests
```

`SKILL.md` starts with YAML frontmatter. At minimum it must define `name` and `description`. The `description` is used for routing, so it should be explicit about when the skill should and should not be used.

## Common Workflows

Validate DOCX tooling and generated Word files:

```sh
python3 -m pytest docx/tests
python3 docx/scripts/validate-generated-docx.py .
```

Run DOCX fixture evals:

```sh
python3 docx/evals/run-evals.py
```

Probe and OCR a scanned document:

```sh
bash ocr/scripts/probe.sh path/to/file.pdf
python3 ocr/scripts/ocr.py path/to/file.pdf --format all
```

Try the YouTube transcript helper:

```sh
node youtube-summary/scripts/youtube-summary.mjs "https://www.youtube.com/watch?v=..."
```

Validate a skill directory when OpenCode skill tooling is available:

```sh
opencode skill validate docx
```

## Development Notes

- Run the smallest checks that cover the skill you changed.
- For script changes, run the script manually against a representative fixture and inspect generated artifacts.
- Document skills should preserve original files and write outputs to new paths.
- Keep reusable fixtures, references, tests, and scripts inside the relevant skill directory.
- Keep transient outputs in ignored locations such as `generated/` or a clearly named scratch directory.
- Do not commit secrets, credentials, downloaded dependencies, or machine-local paths.

## Contributing

See the development notes above for conventions and review expectations. The process for adding or modifying skills:
1. Create a new skill directory with `SKILL.md`, `scripts/`, `references/`, `evals/`, `tests/` as needed
2. Write deterministic helper scripts with clear CLI interfaces
3. Add synthetic fixtures and mechanical assertions in `evals/`
4. Document OOXML/security/routing specifics in `references/`
5. Validate with `opencode skill validate <skill-name>`
6. Run evals: `skill_eval` for trigger accuracy, mechanical scripts for functional correctness

## License

This project is licensed under the [MIT License](LICENSE).