---
name: video-summary
description: Use this skill for video work: summarize, watch, inspect, transcribe, extract frames, OCR frames, or analyze video content from YouTube videos/playlists, web pages with embedded video, local video files (.mp4, .mov, .mkv, .webm), direct video URLs, HLS/DASH .m3u8/.mpd streams, RTSP streams, and animated GIF/APNG/WebP. Also use when the user says video-summary, video-summary skill, or video-summary workflow. Trigger for prompts like analyze video, watch this video, transcribe video, summarize video, резюмируй видео, проанализируй видео, посмотри видео, транскрибируй видео, YouTube ролик, playlist, video frames, кадры из видео. Handles video-specific workflow: subtitle discovery tied to video, playlist selection, sidecar transcripts beside video files, peepshow frame/audio processing, OCR/vision analysis, Fabric/user prompt summaries, and video-summary-config.json presets. Do not use for standalone subtitle conversion, static images, PDFs, markdown, domains, or code review.
license: MIT
compatibility: opencode
metadata:
  audience: agents
  domain: video
---

# Video Summary

This skill summarizes video by first finding the cheapest reliable text source, then optionally adding audio transcription and frame analysis. It supports playlists, web pages with video, local files, direct video URLs, streams, sidecar transcripts, `peepshow`, OCR, and Fabric patterns.

## Requirements

- `node` available in PATH.
- `yt-dlp` available in PATH for YouTube, playlists, and many web pages with embedded video.
- `peepshow` and `ffmpeg` available in PATH when transcription or frame extraction is selected.
- Optional: `fabric` for Fabric pattern summaries.
- Optional: `tesseract` for `peepshow --ocr`.

If a required tool for the selected path is missing, explain the missing dependency and do not fabricate results.

## Discovery First

Always run discovery before deciding the workflow:

```bash
node video-summary/scripts/video-summary.mjs discover "<input>" --language "<user-language>"
```

Read the JSON output. It includes merged config, candidate presets, subtitles, sidecars, playlist entries, and warnings.

## Config

The config filename is `video-summary-config.json`.

The helper searches every config file in this priority order and merges all found configs:

1. `VIDEO_SUMMARY_CONFIG` environment variable path.
2. The skill directory: `video-summary/video-summary-config.json`.
3. Project agent config directories: `.opencode/`, `.claude/`, `.agents/`.
4. Current project directory.
5. Global agent config directories: `~/.config/opencode/`, `~/.claude/`, `~/.agents/`.

Merge only these top-level collections:

- `peepshowPresets`
- `ocrPresets`
- `prompts`
- `presets`

More-priority files replace entries with the same key from less-priority files. Replacement is per named entry, not deep merge inside an entry.

Use this to inspect merged config without leaking raw API keys:

```bash
node video-summary/scripts/video-summary.mjs config
```

## Preset Selection

After discovery, choose the most suitable preset from `presetRecommendations` but ask the user via `question` before using it.

Question: `Какой video-summary preset использовать?`

Options:

- Put the best match first and suffix the label with `(Recommended)`.
- Then list the other configured presets.
- Put `Указать все настройки вручную` last.

The preset should decide defaults for subtitle preference, transcription fallback, frame analysis, `peepshowPreset`, `ocrPreset`, and `prompt`. User selection wins over the recommendation.

## Playlist Selection

If discovery reports a playlist, ask the user which videos to process using `question` with `multiple: true`.

The first checkbox option must be:

- `Все видео`

Then list each video title with its index. If the user selects `Все видео`, process all entries; otherwise process the selected entries only.

## Information Source Selection

After preset selection, ask a multiple-choice `question` with the available information sources. Preselect mentally from the preset, but still ask.

Use these options when applicable:

- `Использовать найденные субтитры/транскрипт`
- `Транскрибировать аудиодорожку`
- `Также анализировать кадры видео`

Warnings to include in option descriptions:

- If transcribing, audio must be downloaded or read from the source. For YouTube, `yt-dlp` can download audio-only with `-f ba/bestaudio` or `-x --audio-format ...`; a full video download is not necessary for audio transcription.
- If analyzing frames, the video stream must be downloaded/read; audio-only is not enough.

Prefer subtitle language matching the user's language when several subtitle tracks are available. Prefer source/original subtitle tracks over generated translations when quality is otherwise similar.

## Transcripts

Use transcript sources in this order unless the user chooses otherwise:

1. YouTube/web subtitles from `yt-dlp`.
2. Embedded text subtitles extracted by `peepshow` without Whisper.
3. Local sidecar files next to local video: `.vtt`, `.srt`, `.txt`, including language variants such as `.ru.vtt` or `.en.srt`.
4. Remote sidecar candidates for HTTP(S) video URLs.
5. Audio transcription through `peepshow --transcribe`.

Download and clean web subtitles with:

```bash
node video-summary/scripts/video-summary.mjs subtitles "<input>" --language "<lang>" --out-dir "<out-dir>"
```

Clean an existing VTT/SRT/TXT sidecar with:

```bash
node video-summary/scripts/video-summary.mjs subtitles --input-subtitle "<path>" --out-dir "<out-dir>"
```

Read `transcript.txt` after the helper returns.

## Peepshow

Use `peepshow` when the user selected transcription and/or frame analysis.

Build command arguments from the selected `peepshowPreset`:

```bash
node video-summary/scripts/video-summary.mjs peepshow-args --preset "<peepshowPreset>"
```

Then run:

```bash
peepshow "<input>" <args-from-helper> --emit json --output "<out-dir>/peepshow"
```

Important `peepshow` behaviors:

- Without `--force-whisper`, `peepshow` prefers embedded text subtitles when available.
- `--transcribe off|whisper-cpp|openai|groq|deepgram|assemblyai|custom` transcribes audio.
- `--ocr --ocr-lang <code>` runs Tesseract OCR over frames. `--ocr-lang` defaults to `eng` and supports `+` lists such as `eng+rus`.
- `--describe openai|anthropic|groq` asks a vision LLM to caption each frame.
- Parse `frames[].path`, `frames[].ocr`, frame descriptions if present, and `audio.transcript.text` / `audio.transcript.segments[]` from JSON.

## Frame Analysis Choice

If frame analysis is selected, ask a single-choice `question`.

Options:

- Selected preset's `ocrPreset`, if present, first with `(Recommended)`.
- Other configured `ocrPresets`.
- `peepshow --ocr`.
- `Текущая LLM пользователя`.
- `Указать вручную` last.

Valid `ocrPresets`:

```json
{ "type": "skill", "skillName": "ocr" }
```

```json
{ "type": "llm", "llmId": "model-id" }
```

```json
{
  "type": "llm",
  "llmBaseUrl": "https://example.com/v1",
  "llmApiKey": "env:MY_VISION_API_KEY",
  "llmId": "model-id"
}
```

If `llmApiKey` starts with `env:`, resolve it from the environment. Do not print resolved API keys. Warn if a raw key appears in a project-level config. Never include secrets in saved artifacts.

If using the `ocr` skill, load it only when actually analyzing extracted frame images with OCR.

## Prompt Selection

Resolve the selected preset's `prompt` from config:

- `type: "text"`: use `value`.
- `type: "file"`: read `path` relative to the config file when possible, otherwise relative to the workspace.
- `type: "fabric"`: use `pattern`.

If the selected preset has no usable prompt, ask a single-choice `question`:

- `Ввести свой промпт`
- `Сгенерировать подходящий промпт для типа видео`
- `Выбрать шаблон Fabric`

If the user chooses Fabric, ask a second single-choice `question` with a curated shortlist selected from context. Put the best match first with `(Recommended)` and put `Ввести название Fabric pattern вручную` last.

Good curated Fabric patterns:

- `extract_wisdom` for lectures, podcasts, talks, interviews, webinars.
- `summarize` for general summaries and tutorials.
- `extract_main_idea` for short clips and focused explanations.
- `analyze_claims` for news, opinions, debates, reviews, or persuasive content.

Run Fabric like this:

```bash
cat "<combined-context.md>" | fabric --pattern "<pattern>"
```

If Fabric is unavailable, tell the user and offer to summarize with the current LLM instead.

## Combined Context

Before summarizing, assemble `combined-context.md` in the run output directory:

```markdown
# Video Metadata

# Transcript

# Frame OCR

# Frame Descriptions

# Caveats
```

Use the transcript as the main source for narration-heavy videos. Use frame OCR/descriptions to add visual facts, UI details, slide text, scene changes, and caveats. Clearly separate confirmed source content from inference.

## Output

Save artifacts under the selected `--out-dir` or a temp output directory:

- `discovery.json`
- `transcript.txt` when available
- `combined-context.md`
- `summary.md`
- `metadata.json`
- `peepshow/manifest.json` and frame files when `peepshow` runs
- `playlist-summary.md` for playlists

Return to the user:

- selected preset;
- selected source choices;
- transcript/subtitle language;
- frame analysis method, if any;
- summary method/prompt or Fabric pattern;
- artifact paths;
- concise summary and key points;
- warnings about missing tools, missing subtitles, transcription cost/quota, or frame download requirements.
