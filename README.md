# AI Skills

Reusable [Agent Skills](https://agentskills.io) for OpenCode and compatible coding assistants. This repository packages task-specific instructions, references, evals, fixtures, and deterministic helper scripts for workflows that benefit from repeatable handling instead of ad-hoc prompting.

The current collection focuses on document automation, OCR, PDFs, video summaries, meeting transcript storage, and domain availability checks.

The `docx` and `pptx` skills share a common OOXML engine in [`common/ooxml/`](common/ooxml/) that provides ZIP-safety checks, XML utilities, and a generic unpack/pack/validate engine parameterized by format-specific profiles.

## Skills

| Skill | Use it for | Notable helpers |
| --- | --- | --- |
| [`docx`](docx/) | Create, read, edit, inspect, sanitize, validate, convert, and extract Microsoft Word `.docx` and legacy `.doc` files. | Safe OOXML unpack/pack, validation, extraction, conversion, template filling, metadata sanitization |
| [`pptx`](pptx/) | Create, read, edit, inspect, sanitize, validate, convert, and extract Microsoft PowerPoint `.pptx` and legacy `.ppt` files. | PptxGenJS generation, safe OOXML unpack/pack, validation, extraction, slide thumbnails, template filling |
| [`ocr`](ocr/) | Extract text from scanned PDFs, screenshots, photos, forms, receipts, and image-only documents. | OCR probing, page preprocessing, quality reports, optional searchable PDF generation |
| [`pdf`](pdf/) | Read, extract, create, merge, split, render, inspect, and verify PDF files. | Tool-routing guidance and visual verification workflow |
| [`video-summary`](video-summary/) | Summarize YouTube videos/playlists, web videos, local video files, streams, transcripts, audio, and frames. | Configurable presets, `yt-dlp` subtitle helper, `peepshow` orchestration, Fabric prompt routing |
| [`meeting-transcript`](meeting-transcript/) | Save meeting transcripts and verified summaries into an Obsidian-style vault. | Storage rules, summary verification, action-item extraction guidance |
| [`regru`](regru/) | Check exact domain names for availability through REG.RU API 2. | Self-contained REG.RU `domain/check` CLI with optional client SSL auth |
| [`domain-check`](domain-check/) | Check exact domain availability for .ru, .рф, and other TLDs using public registry signals (RDAP/WHOIS). | No-API availability CLI with IDN support |

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
ln -s /path/to/ai-skills/pptx ~/.claude/skills/pptx
ln -s /path/to/ai-skills/ocr ~/.claude/skills/ocr
ln -s /path/to/ai-skills/pdf ~/.claude/skills/pdf
ln -s /path/to/ai-skills/video-summary ~/.claude/skills/video-summary
ln -s /path/to/ai-skills/meeting-transcript ~/.claude/skills/meeting-transcript
ln -s /path/to/ai-skills/regru ~/.claude/skills/regru
ln -s /path/to/ai-skills/domain-check ~/.claude/skills/domain-check
```

For project-local installation, create the same symlinks under that project's `.claude/skills/` directory.

> **Note for `docx` and `pptx`:** these skills use a shared OOXML engine in
> `common/ooxml/`. The engine is located automatically by walking up to the
> repo root. If you install skills via per-skill symlinks in an isolated
> directory, set `AI_SKILLS_ROOT=/path/to/ai-skills` in your environment so
> the bootstrap shim can find `common/ooxml/`.

## Requirements

Install only the dependencies required by the skills you use.

| Dependency | Used by | Example macOS install |
| --- | --- | --- |
| Python 3.9+ | `docx`, `pptx`, `ocr`, `pdf` | `brew install python` |
| Node.js | `pptx` (PptxGenJS), `video-summary`, `regru` | `brew install node` |
| `defusedxml` | `docx`, `pptx` — XML parsing | `python3 -m pip install defusedxml` |
| `python-docx` | `docx` — extraction and simple document generation | `python3 -m pip install python-docx` |
| `python-pptx` | `pptx` — extraction and simple deck generation | `python3 -m pip install python-pptx` |
| `pptxgenjs` | `pptx` — rich deck creation (primary) | `npm install -g pptxgenjs` |
| `docxtpl` | `docx` — template filling | `python3 -m pip install docxtpl` |
| `pandoc` | `docx`, `pptx` — higher-fidelity conversion | `brew install pandoc` |
| LibreOffice | `docx`, `pptx` — `.doc`/`.ppt` conversion and PDF rendering | `brew install --cask libreoffice` |
| `poppler` | `pptx` — slide thumbnails; `pdf`, `ocr` — rendering | `brew install poppler` |
| `tesseract` | `ocr` | `brew install tesseract` |
| `yt-dlp` | `video-summary` | `python3 -m pip install yt-dlp` |
| REG.RU partner API credentials | `regru` | Set `REGRU_USERNAME` and `REGRU_PASSWORD`; optionally `REGRU_SSL_CERT_PATH` and `REGRU_SSL_KEY_PATH`. REG.RU `domain/check` requires partner/reseller access. |
| WhoisXML API key | `domain-check` | Set `WHOISXML_API_KEY` environment variable (only for legacy WhoisXML fallback). |

Some workflows have optional fallback tools. See each skill's `SKILL.md` for task-specific requirements.

## Repository Layout

```text
ai-skills/
├── README.md
├── LICENSE
├── common/
│   └── ooxml/              # shared OOXML engine (docx + pptx)
├── .docs/
│   ├── docx-research.md
│   └── pptx-research.md
├── .github/workflows/
│   └── docx-validation.yml
├── docx/
│   ├── SKILL.md
│   ├── scripts/
│   ├── references/
│   ├── evals/
│   └── tests/
├── pptx/
│   ├── SKILL.md
│   ├── scripts/
│   ├── references/
│   └── evals/
├── ocr/
│   ├── SKILL.md
│   ├── scripts/
│   ├── references/
│   └── evals/
├── pdf/
│   └── SKILL.md
├── video-summary/
│   ├── SKILL.md
│   ├── video-summary-config.json
│   └── scripts/
├── meeting-transcript/
│   ├── SKILL.md
│   └── templates/
│   └── evals/
├── regru/
│   ├── SKILL.md
│   ├── scripts/
│   └── evals/
├── domain-check/
│   ├── SKILL.md
│   ├── scripts/
│   └── evals/
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

Generate PPTX fixtures and run evals:

```sh
python3 pptx/evals/make-fixtures.py
python3 pptx/evals/run-evals.py
```

Probe and OCR a scanned document:

```sh
bash ocr/scripts/probe.sh path/to/file.pdf
python3 ocr/scripts/ocr.py path/to/file.pdf --format all
```

Try the video discovery helper:

```sh
node video-summary/scripts/video-summary.mjs discover "https://www.youtube.com/watch?v=..."
```

Check exact domain availability through REG.RU API 2:

```sh
REGRU_USERNAME=your-login REGRU_PASSWORD=your-api-password \
  node regru/scripts/regru-domain-check.mjs example.ru example.com
```

Check exact domain availability using public registry signals:

```sh
node domain-check/scripts/domain-check.mjs example.ru пример.рф
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
