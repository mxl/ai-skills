---
name: youtube-summary
description: Use this skill whenever the user asks to summarize a YouTube video, extract or download YouTube subtitles, get an original transcript with yt-dlp, summarize captions, or retry transcript extraction from YouTube. This skill runs the bundled script to choose original subtitles, download them with yt-dlp, and clean VTT captions into transcript.txt; then the agent must read transcript.txt and produce the final LLM summary.
license: MIT
compatibility: opencode
metadata:
  audience: agents
  domain: youtube
---

# YouTube Subtitle Summary

This skill handles YouTube transcript extraction through a bundled script, then uses the agent's LLM reasoning to summarize the cleaned transcript. The script is the source of truth for subtitle selection, downloading, and VTT cleaning. The final summary must be written by the agent after reading `transcript.txt`, saved as `summary.md` next to the transcript, and also returned to the user.

## When To Use

Use this skill for requests like:

- "резюмируй видео <youtube-url>"
- "вытащи транскрипт через yt-dlp"
- "скачай оригинальные субтитры"
- "суммаризируй субтитры"
- "сделай скачивание субтитров скриптом"

## Requirements

- `node` available in PATH.
- `yt-dlp` available in PATH.
- Internet access to YouTube.

If `yt-dlp` is missing, tell the user that the script cannot run until `yt-dlp` is installed. Do not silently switch to browser scraping or LLM-only summarization.

## Script Workflow

Run the bundled script from this skill directory:

```bash
node youtube-summary/scripts/youtube-summary.mjs "<youtube-url>"
```

Useful options:

```bash
node youtube-summary/scripts/youtube-summary.mjs "<youtube-url>" --out-dir "/tmp/opencode/youtube-summary"
node youtube-summary/scripts/youtube-summary.mjs "<youtube-url>" --language ru
node youtube-summary/scripts/youtube-summary.mjs --input-vtt "/path/to/subtitles.vtt"
```

The script performs these deterministic steps:

1. Calls `yt-dlp --skip-download --list-subs <url>`.
2. Parses available automatic captions and manual subtitles.
3. Prefers original subtitle tracks like `ru-orig`, then other `*-orig` tracks, then requested language, then `ru`, then `en`, then the first available track.
4. Downloads one subtitle track as VTT with `yt-dlp --skip-download --write-auto-subs --write-subs --sub-langs <lang> --sub-format vtt`.
5. Cleans VTT timing, inline cue tags, duplicate progressive-caption lines, and music markers.
6. Writes `transcript.txt`, `metadata.json`, and the downloaded `.vtt` path.
7. Prints JSON with artifact paths and subtitle-selection metadata.

## Output Rules

After the script finishes:

1. Read `transcript.txt` from the path printed by the script.
2. Summarize the transcript with LLM reasoning. Do not use script output as the final summary.
3. Write the summary in the user's language unless they request another language.
4. Save the LLM-written summary to `summary.md` in the same directory as `transcript.txt`.
5. Return the summary to the user and include the saved `summary.md` path.

Return:

- selected subtitle language and whether it was original;
- path to downloaded VTT;
- path to cleaned transcript;
- path to saved `summary.md`;
- concise LLM summary;
- key points;
- practical takeaways when useful;
- any warnings from the script.

If the script exits non-zero, report the failure clearly and include the exact failing step from stdout/stderr. Do not fabricate a summary.

## LLM Summary Quality

The script output is raw transcript data. The agent is responsible for turning it into a useful summary by interpreting noisy auto-captions, restoring likely meaning when ASR errors are obvious, and clearly separating confirmed transcript content from inference.

Prefer this structure for video summaries:

- Short summary
- Key points
- Practical takeaways
- Caveats about transcript quality, if relevant

Save the same structure to `summary.md`. The file should be Markdown, without frontmatter, unless the user explicitly asks to create an Obsidian note.

## Privacy

Treat downloaded transcripts as user workspace data. Save outputs under the requested `--out-dir` or the system temp directory by default. Do not publish or externally share transcript content without explicit approval.
