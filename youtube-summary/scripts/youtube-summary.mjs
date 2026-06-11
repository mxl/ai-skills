#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync, readdirSync } from "node:fs";
import { basename, join, resolve } from "node:path";
import { tmpdir } from "node:os";

function printUsageAndExit() {
  console.error(`Usage:\n  youtube-summary.mjs <youtube-url> [--out-dir DIR] [--language LANG]\n  youtube-summary.mjs --input-vtt FILE [--out-dir DIR]`);
  process.exit(2);
}

function parseArgs(argv) {
  const args = { url: null, inputVtt: null, outDir: null, language: null };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--out-dir") args.outDir = argv[++i];
    else if (arg === "--language") args.language = argv[++i];
    else if (arg === "--input-vtt") args.inputVtt = argv[++i];
    else if (!args.url) args.url = arg;
    else printUsageAndExit();
  }
  if ((!args.url && !args.inputVtt) || (args.url && args.inputVtt)) {
    printUsageAndExit();
  }
  return args;
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { encoding: "utf8", maxBuffer: 20 * 1024 * 1024, ...options });
  if (result.error) {
    throw new Error(`${command} failed to start: ${result.error.message}`);
  }
  return result;
}

function ensureYtDlp() {
  const result = run("yt-dlp", ["--version"]);
  if (result.status !== 0) {
    throw new Error("yt-dlp is not available in PATH");
  }
  return result.stdout.trim();
}

function slugify(input) {
  return input
    .toLowerCase()
    .replace(/^https?:\/\//, "")
    .replace(/[^a-z0-9а-яё]+/gi, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80) || "youtube-summary";
}

function parseSubtitleLanguages(output) {
  const languages = [];
  const lines = output.split(/\r?\n/);
  let inSubtitleSection = false;
  for (const line of lines) {
    if (/Available (automatic captions|subtitles)/i.test(line)) {
      inSubtitleSection = true;
      continue;
    }
    if (!inSubtitleSection || /^Language\s+Name\s+Formats/i.test(line) || !line.trim()) continue;
    const match = line.match(/^([a-zA-Z0-9-]+)\s+(.+?)\s{2,}(.+)$/);
    if (match) {
      languages.push({ code: match[1], name: match[2].trim(), formats: match[3].trim() });
    }
  }
  return languages;
}

function chooseLanguage(languages, requestedLanguage) {
  if (!languages.length) return null;
  const byCode = new Map(languages.map((lang) => [lang.code, lang]));
  const original = languages.filter((lang) => lang.code.endsWith("-orig"));
  const preferred = [];
  if (requestedLanguage) preferred.push(`${requestedLanguage}-orig`, requestedLanguage);
  preferred.push("ru-orig", "en-orig", "ru", "en");
  for (const code of preferred) {
    if (byCode.has(code)) return { ...byCode.get(code), isFallback: !code.endsWith("-orig") };
  }
  if (original.length) return { ...original[0], isFallback: false };
  return { ...languages[0], isFallback: true };
}

function downloadSubtitle(url, outDir, langCode) {
  const outputTemplate = join(outDir, "source.%(ext)s");
  const result = run("yt-dlp", [
    "--skip-download",
    "--write-auto-subs",
    "--write-subs",
    "--sub-langs",
    langCode,
    "--sub-format",
    "vtt",
    "-o",
    outputTemplate,
    url
  ]);
  if (result.status !== 0) {
    throw new Error(`yt-dlp subtitle download failed:\n${result.stderr || result.stdout}`);
  }
  const candidates = readdirSync(outDir)
    .filter((name) => name.endsWith(".vtt"))
    .map((name) => join(outDir, name));
  if (!candidates.length) {
    throw new Error(`yt-dlp completed but no .vtt file was written in ${outDir}`);
  }
  return candidates[0];
}

function cleanVtt(raw) {
  const cueLines = [];
  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    if (trimmed === "WEBVTT" || /^Kind:/i.test(trimmed) || /^Language:/i.test(trimmed)) continue;
    if (/^\d{2}:\d{2}:\d{2}\.\d{3}\s+-->/.test(trimmed)) continue;
    if (/^NOTE\b/i.test(trimmed)) continue;

    const cleaned = trimmed
      .replace(/<\d{2}:\d{2}:\d{2}\.\d{3}>/g, "")
      .replace(/<\/?c[^>]*>/g, "")
      .replace(/<[^>]+>/g, "")
      .replace(/&amp;/g, "&")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/\s+/g, " ")
      .trim();
    if (!cleaned || /^\[.*\]$/.test(cleaned)) continue;
    cueLines.push(cleaned);
  }

  const mergedWords = [];
  for (const line of cueLines) {
    const words = line.split(/\s+/).filter(Boolean);
    if (!words.length) continue;

    let overlap = 0;
    const maxOverlap = Math.min(mergedWords.length, words.length);
    for (let size = maxOverlap; size > 0; size--) {
      const suffix = mergedWords.slice(mergedWords.length - size).join(" ").toLowerCase();
      const prefix = words.slice(0, size).join(" ").toLowerCase();
      if (suffix === prefix) {
        overlap = size;
        break;
      }
    }
    mergedWords.push(...words.slice(overlap));
  }

  return mergedWords.join(" ").replace(/\s+/g, " ").trim();
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const warnings = [];
  const startedAt = new Date().toISOString();
  const outDir = resolve(args.outDir || join(tmpdir(), "opencode-youtube-summary", `${Date.now()}-${slugify(args.url || basename(args.inputVtt))}`));
  mkdirSync(outDir, { recursive: true });

  let vttPath;
  let selectedLanguage = null;
  let ytdlpVersion = null;
  if (args.inputVtt) {
    vttPath = resolve(args.inputVtt);
    if (!existsSync(vttPath)) throw new Error(`Input VTT not found: ${vttPath}`);
  } else {
    ytdlpVersion = ensureYtDlp();
    const listResult = run("yt-dlp", ["--skip-download", "--list-subs", args.url]);
    if (listResult.status !== 0) {
      throw new Error(`yt-dlp subtitle listing failed:\n${listResult.stderr || listResult.stdout}`);
    }
    const languages = parseSubtitleLanguages(`${listResult.stdout}\n${listResult.stderr}`);
    selectedLanguage = chooseLanguage(languages, args.language);
    if (!selectedLanguage) throw new Error("No subtitle tracks found by yt-dlp");
    if (selectedLanguage.isFallback) warnings.push(`No original subtitle track matched the preference; using fallback ${selectedLanguage.code}.`);
    vttPath = downloadSubtitle(args.url, outDir, selectedLanguage.code);
  }

  const raw = readFileSync(vttPath, "utf8");
  const transcript = cleanVtt(raw);
  if (!transcript) throw new Error("Subtitle file was downloaded but transcript cleaning produced no text");

  const transcriptPath = join(outDir, "transcript.txt");
  const metadataPath = join(outDir, "metadata.json");

  writeFileSync(transcriptPath, transcript + "\n", "utf8");
  writeFileSync(metadataPath, JSON.stringify({
    startedAt,
    url: args.url,
    inputVtt: args.inputVtt,
    outDir,
    selectedLanguage,
    ytdlpVersion,
    vttPath,
    transcriptPath,
    transcriptWords: transcript.split(/\s+/).filter(Boolean).length,
    warnings
  }, null, 2) + "\n", "utf8");

  console.log(JSON.stringify({
    ok: true,
    selectedLanguage,
    outDir,
    vttPath,
    transcriptPath,
    metadataPath,
    warnings
  }, null, 2));
}

try {
  main();
} catch (error) {
  console.error(JSON.stringify({ ok: false, error: error.message }, null, 2));
  process.exit(1);
}
