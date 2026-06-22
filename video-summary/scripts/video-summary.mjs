#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync, readdirSync } from "node:fs";
import { basename, dirname, extname, join, resolve } from "node:path";
import { homedir, tmpdir } from "node:os";
import http from "node:http";
import https from "node:https";

const COLLECTIONS = ["peepshowPresets", "ocrPresets", "prompts", "presets"];
const CONFIG_NAME = "video-summary-config.json";
const SKILL_DIR = resolve(dirname(new URL(import.meta.url).pathname), "..");

function usage() {
  console.error(`Usage:
  video-summary.mjs config
  video-summary.mjs discover <input> [--language LANG] [--out-dir DIR]
  video-summary.mjs playlist <url>
  video-summary.mjs subtitles <url> [--language LANG] [--out-dir DIR]
  video-summary.mjs subtitles --input-subtitle FILE [--out-dir DIR]
  video-summary.mjs peepshow-args --preset NAME`);
  process.exit(2);
}

function parseArgs(argv) {
  const args = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--language") args.language = argv[++i];
    else if (arg === "--out-dir") args.outDir = argv[++i];
    else if (arg === "--input-subtitle") args.inputSubtitle = argv[++i];
    else if (arg === "--preset") args.preset = argv[++i];
    else if (arg.startsWith("--")) usage();
    else args._.push(arg);
  }
  return args;
}

function run(command, args, options = {}) {
  return spawnSync(command, args, { encoding: "utf8", maxBuffer: 50 * 1024 * 1024, ...options });
}

function commandOk(command) {
  const result = run(command, ["--version"]);
  if (!result.error && result.status === 0) return true;
  const help = run(command, ["--help"]);
  return !help.error && help.status === 0;
}

function expandHome(path) {
  if (!path) return path;
  return path === "~" ? homedir() : path.replace(/^~\//, `${homedir()}/`);
}

function candidateConfigPaths() {
  const paths = [];
  if (process.env.VIDEO_SUMMARY_CONFIG) paths.push(resolve(expandHome(process.env.VIDEO_SUMMARY_CONFIG)));
  paths.push(join(SKILL_DIR, CONFIG_NAME));
  paths.push(resolve(".opencode", CONFIG_NAME));
  paths.push(resolve(".claude", CONFIG_NAME));
  paths.push(resolve(".agents", CONFIG_NAME));
  paths.push(resolve(CONFIG_NAME));
  paths.push(join(homedir(), ".config", "opencode", CONFIG_NAME));
  paths.push(join(homedir(), ".claude", CONFIG_NAME));
  paths.push(join(homedir(), ".agents", CONFIG_NAME));
  return [...new Set(paths)];
}

function loadMergedConfig() {
  const highToLow = candidateConfigPaths().filter((path) => existsSync(path));
  const merged = Object.fromEntries(COLLECTIONS.map((key) => [key, {}]));
  const lowToHigh = [...highToLow].reverse();
  for (const path of lowToHigh) {
    const raw = JSON.parse(readFileSync(path, "utf8"));
    for (const key of COLLECTIONS) {
      if (raw[key] && typeof raw[key] === "object" && !Array.isArray(raw[key])) {
        merged[key] = { ...merged[key], ...raw[key] };
      }
    }
  }
  return { config: merged, files: highToLow, mergedFromLowToHigh: lowToHigh };
}

function redactConfig(config) {
  return JSON.parse(JSON.stringify(config, (key, value) => {
    if (key === "llmApiKey" && typeof value === "string" && !value.startsWith("env:")) return "<redacted>";
    return value;
  }));
}

function slugify(input) {
  return String(input || "video-summary")
    .toLowerCase()
    .replace(/^https?:\/\//, "")
    .replace(/[^a-z0-9а-яё]+/gi, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 90) || "video-summary";
}

function defaultOutDir(input) {
  return join(tmpdir(), "opencode-video-summary", `${Date.now()}-${slugify(basename(input || "input"))}`);
}

function isUrl(input) {
  return /^[a-z][a-z0-9+.-]*:\/\//i.test(input);
}

function isHttpUrl(input) {
  return /^https?:\/\//i.test(input);
}

function isYouTube(input) {
  return /(^|\.)youtu\.be\//i.test(input) || /(^|\.)youtube\.com\//i.test(input);
}

function parseSubtitleLanguages(output) {
  const languages = [];
  const lines = output.split(/\r?\n/);
  let inSection = false;
  let section = null;
  for (const line of lines) {
    if (/Available automatic captions/i.test(line)) {
      inSection = true;
      section = "automatic";
      continue;
    }
    if (/Available subtitles/i.test(line)) {
      inSection = true;
      section = "manual";
      continue;
    }
    if (!inSection || /^Language\s+Name\s+Formats/i.test(line) || !line.trim()) continue;
    const match = line.match(/^([a-zA-Z0-9._-]+)\s+(.+?)\s{2,}(.+)$/);
    if (match) languages.push({ code: match[1], name: match[2].trim(), formats: match[3].trim(), source: section });
  }
  return languages;
}

function chooseLanguage(languages, requestedLanguage) {
  if (!languages.length) return null;
  const req = requestedLanguage ? requestedLanguage.toLowerCase() : null;
  const manual = languages.filter((lang) => lang.source === "manual");
  const byCode = new Map(languages.map((lang) => [lang.code.toLowerCase(), lang]));
  const preferred = [];
  if (req) preferred.push(`${req}-orig`, req);
  preferred.push("ru-orig", "en-orig", "ru", "en");
  for (const code of preferred) if (byCode.has(code)) return byCode.get(code);
  if (manual.length) return manual[0];
  return languages[0];
}

function ytdlpJson(input, extraArgs = []) {
  if (!commandOk("yt-dlp")) return { ok: false, error: "yt-dlp is not available in PATH" };
  const result = run("yt-dlp", ["--skip-download", ...extraArgs, input]);
  if (result.status !== 0) return { ok: false, error: result.stderr || result.stdout };
  try {
    return { ok: true, data: JSON.parse(result.stdout) };
  } catch (error) {
    return { ok: false, error: `yt-dlp JSON parse failed: ${error.message}` };
  }
}

function listSubtitles(input) {
  if (!commandOk("yt-dlp")) return { ok: false, languages: [], error: "yt-dlp is not available in PATH" };
  const result = run("yt-dlp", ["--skip-download", "--list-subs", input]);
  if (result.status !== 0) return { ok: false, languages: [], error: result.stderr || result.stdout };
  return { ok: true, languages: parseSubtitleLanguages(`${result.stdout}\n${result.stderr}`) };
}

function discoverLocalSidecars(input) {
  if (isUrl(input)) return [];
  const path = resolve(input);
  if (!existsSync(path)) return [];
  const dir = dirname(path);
  const base = basename(path, extname(path));
  const exts = new Set([".vtt", ".srt", ".txt"]);
  return readdirSync(dir)
    .filter((name) => name.startsWith(base) && exts.has(extname(name).toLowerCase()))
    .map((name) => ({ path: join(dir, name), kind: "local-sidecar" }));
}

function headOk(url) {
  return new Promise((resolveHead) => {
    const client = url.startsWith("https:") ? https : http;
    const req = client.request(url, { method: "HEAD", timeout: 5000 }, (res) => {
      res.resume();
      resolveHead(res.statusCode >= 200 && res.statusCode < 400);
    });
    req.on("timeout", () => { req.destroy(); resolveHead(false); });
    req.on("error", () => resolveHead(false));
    req.end();
  });
}

async function discoverRemoteSidecars(input) {
  if (!isHttpUrl(input)) return [];
  const url = new URL(input);
  const currentExt = extname(url.pathname);
  if (!currentExt) return [];
  const stem = url.pathname.slice(0, -currentExt.length);
  const suffixes = [".vtt", ".srt", ".txt", ".ru.vtt", ".en.vtt", ".ru.srt", ".en.srt"];
  const out = [];
  for (const suffix of suffixes) {
    const copy = new URL(input);
    copy.pathname = `${stem}${suffix}`;
    const candidate = copy.toString();
    if (await headOk(candidate)) out.push({ url: candidate, kind: "remote-sidecar" });
  }
  return out;
}

function rankPresets(config, input, metadata, subtitles, sidecars) {
  const haystack = [input, metadata?.title, metadata?.description, metadata?.extractor_key, metadata?.webpage_url]
    .filter(Boolean).join(" ").toLowerCase();
  return Object.entries(config.presets || {}).map(([name, preset]) => {
    let score = 0;
    const reasons = [];
    for (const token of preset.match || []) {
      if (haystack.includes(String(token).toLowerCase())) {
        score += 2;
        reasons.push(`matched ${token}`);
      }
    }
    if (isYouTube(input) && /youtube/i.test(`${name} ${preset.description || ""}`)) score += 3;
    if ((subtitles?.languages?.length || 0) && preset.preferSubtitles) score += 1;
    if ((sidecars?.length || 0) && preset.preferSubtitles) score += 1;
    if (!score && /general/i.test(name)) score = 1;
    return { name, score, description: preset.description || "", reasons };
  }).sort((a, b) => b.score - a.score || a.name.localeCompare(b.name));
}

function cleanSubtitle(raw) {
  const lines = [];
  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    if (trimmed === "WEBVTT" || /^Kind:/i.test(trimmed) || /^Language:/i.test(trimmed)) continue;
    if (/^\d+$/.test(trimmed)) continue;
    if (/^\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->/.test(trimmed)) continue;
    if (/^\d{2}:\d{2}[,.]\d{3}\s+-->/.test(trimmed)) continue;
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
    lines.push(cleaned);
  }

  const mergedWords = [];
  for (const line of lines) {
    const words = line.split(/\s+/).filter(Boolean);
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

function downloadSubtitle(input, outDir, language) {
  if (!commandOk("yt-dlp")) throw new Error("yt-dlp is not available in PATH");
  const subtitles = listSubtitles(input);
  if (!subtitles.languages.length) throw new Error("No subtitle tracks found by yt-dlp");
  const selected = chooseLanguage(subtitles.languages, language);
  const result = run("yt-dlp", [
    "--skip-download",
    "--write-auto-subs",
    "--write-subs",
    "--sub-langs", selected.code,
    "--sub-format", "vtt/srt/best",
    "-o", join(outDir, "source.%(ext)s"),
    input
  ]);
  if (result.status !== 0) throw new Error(`yt-dlp subtitle download failed:\n${result.stderr || result.stdout}`);
  const candidates = readdirSync(outDir).filter((name) => /\.(vtt|srt|txt)$/i.test(name)).map((name) => join(outDir, name));
  if (!candidates.length) throw new Error(`yt-dlp completed but no subtitle file was written in ${outDir}`);
  return { selected, subtitlePath: candidates[0], available: subtitles.languages };
}

function writeTranscript(subtitlePath, outDir, metadata = {}) {
  const raw = readFileSync(subtitlePath, "utf8");
  const transcript = extname(subtitlePath).toLowerCase() === ".txt" ? raw.trim() : cleanSubtitle(raw);
  if (!transcript) throw new Error("Subtitle cleaning produced no text");
  const transcriptPath = join(outDir, "transcript.txt");
  const metadataPath = join(outDir, "metadata.json");
  const transcriptWords = transcript.split(/\s+/).filter(Boolean).length;
  writeFileSync(transcriptPath, `${transcript}\n`, "utf8");
  writeFileSync(metadataPath, `${JSON.stringify({ ...metadata, transcriptPath, transcriptWords }, null, 2)}\n`, "utf8");
  return { transcriptPath, metadataPath, transcriptWords };
}

function peepshowArgsForPreset(config, presetName) {
  const preset = config.peepshowPresets?.[presetName];
  if (!preset) throw new Error(`Unknown peepshow preset: ${presetName}`);
  const args = [];
  const valueFlags = {
    threshold: "--threshold",
    max: "--max",
    min: "--min",
    fps: "--fps",
    strategy: "--strategy",
    width: "--width",
    format: "--format",
    gpu: "--gpu",
    dedup: "--dedup",
    dedupDistance: "--dedup-distance",
    adaptive: "--adaptive",
    transcribe: "--transcribe",
    start: "--start",
    duration: "--duration",
    portrait: "--portrait",
    vr: "--vr",
    ocrLang: "--ocr-lang",
    ocrPsm: "--ocr-psm",
    describe: "--describe",
    embedModel: "--embed-model",
    blur: "--blur",
    blurStrength: "--blur-strength"
  };
  for (const [key, flag] of Object.entries(valueFlags)) {
    if (preset[key] === undefined || preset[key] === null || preset[key] === false) continue;
    if (key === "transcribe" && preset[key] === "env") continue;
    args.push(flag, String(preset[key]));
  }
  const boolFlags = {
    ocr: "--ocr",
    diarise: "--diarise",
    forceWhisper: "--force-whisper",
    stabilise: "--stabilise",
    ignoreChapters: "--ignore-chapters",
    noTonemap: "--no-tonemap",
    embedFrames: "--embed-frames",
    noReport: "--no-report",
    noManifest: "--no-manifest",
    noIndex: "--no-index",
    noAudio: "--no-audio"
  };
  for (const [key, flag] of Object.entries(boolFlags)) {
    if (preset[key] === true) args.push(flag);
  }
  if (typeof preset.multiRes === "number") args.push("--multi-res", String(preset.multiRes));
  else if (preset.multiRes === true) args.push("--multi-res");
  return args;
}

async function discover(input, args) {
  const outDir = resolve(args.outDir || defaultOutDir(input));
  mkdirSync(outDir, { recursive: true });
  const { config, files } = loadMergedConfig();
  const tools = {
    node: true,
    ytdlp: commandOk("yt-dlp"),
    peepshow: commandOk("peepshow"),
    fabric: commandOk("fabric"),
    tesseract: commandOk("tesseract")
  };
  const inputInfo = { raw: input, isUrl: isUrl(input), isHttpUrl: isHttpUrl(input), isYouTube: isYouTube(input), existsLocal: !isUrl(input) && existsSync(resolve(input)) };
  const metadataResult = isUrl(input) && tools.ytdlp ? ytdlpJson(input, ["--dump-single-json", "--flat-playlist"]) : { ok: false };
  const metadata = metadataResult.ok ? metadataResult.data : null;
  const warnings = [];
  if (isUrl(input) && !metadataResult.ok && metadataResult.error) warnings.push(metadataResult.error.trim());
  const playlistEntries = metadata?.entries?.map((entry, index) => ({
    index: index + 1,
    id: entry.id,
    title: entry.title || entry.id || `Video ${index + 1}`,
    url: entry.url || entry.webpage_url || null
  })) || [];
  const subtitles = isUrl(input) && tools.ytdlp ? listSubtitles(input) : { ok: false, languages: [] };
  const sidecars = [...discoverLocalSidecars(input), ...await discoverRemoteSidecars(input)];
  const result = {
    ok: true,
    outDir,
    configFiles: files,
    config: redactConfig(config),
    tools,
    input: inputInfo,
    metadata: metadata ? {
      id: metadata.id,
      title: metadata.title,
      description: metadata.description,
      duration: metadata.duration,
      webpage_url: metadata.webpage_url,
      extractor: metadata.extractor,
      extractor_key: metadata.extractor_key,
      playlist_count: metadata.playlist_count
    } : null,
    isPlaylist: playlistEntries.length > 0,
    playlistEntries,
    subtitles,
    preferredSubtitle: chooseLanguage(subtitles.languages || [], args.language),
    sidecars,
    presetRecommendations: rankPresets(config, input, metadata, subtitles, sidecars),
    warnings
  };
  writeFileSync(join(outDir, "discovery.json"), `${JSON.stringify(result, null, 2)}\n`, "utf8");
  return result;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const command = args._[0];
  if (!command) usage();
  if (command === "config") {
    const loaded = loadMergedConfig();
    console.log(JSON.stringify({ ...loaded, config: redactConfig(loaded.config) }, null, 2));
    return;
  }
  if (command === "discover") {
    const input = args._[1];
    if (!input) usage();
    console.log(JSON.stringify(await discover(input, args), null, 2));
    return;
  }
  if (command === "playlist") {
    const input = args._[1];
    if (!input) usage();
    const result = ytdlpJson(input, ["--dump-single-json", "--flat-playlist"]);
    if (!result.ok) throw new Error(result.error || "yt-dlp playlist discovery failed");
    console.log(JSON.stringify({ ok: true, entries: result.data.entries || [] }, null, 2));
    return;
  }
  if (command === "subtitles") {
    const input = args._[1];
    const outDir = resolve(args.outDir || defaultOutDir(input || args.inputSubtitle));
    mkdirSync(outDir, { recursive: true });
    let subtitlePath;
    let selected = null;
    let available = [];
    if (args.inputSubtitle) {
      subtitlePath = resolve(args.inputSubtitle);
      if (!existsSync(subtitlePath)) throw new Error(`Input subtitle not found: ${subtitlePath}`);
    } else {
      if (!input) usage();
      const downloaded = downloadSubtitle(input, outDir, args.language);
      subtitlePath = downloaded.subtitlePath;
      selected = downloaded.selected;
      available = downloaded.available;
    }
    const transcript = writeTranscript(subtitlePath, outDir, { input, inputSubtitle: args.inputSubtitle || null, subtitlePath, selectedLanguage: selected, availableSubtitles: available });
    console.log(JSON.stringify({ ok: true, outDir, subtitlePath, selectedLanguage: selected, availableSubtitles: available, ...transcript }, null, 2));
    return;
  }
  if (command === "peepshow-args") {
    if (!args.preset) usage();
    const { config } = loadMergedConfig();
    const peepshowArgs = peepshowArgsForPreset(config, args.preset);
    console.log(JSON.stringify({ ok: true, preset: args.preset, args: peepshowArgs, shell: peepshowArgs.map((arg) => JSON.stringify(arg)).join(" ") }, null, 2));
    return;
  }
  usage();
}

main().catch((error) => {
  console.error(JSON.stringify({ ok: false, error: error.message }, null, 2));
  process.exit(1);
});
