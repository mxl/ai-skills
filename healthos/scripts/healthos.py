#!/usr/bin/env python3
"""Recognize external family medical documents into tracked Markdown.

Recognition itself is delegated to the `ocr` skill's `ocr.py`, imported and
called as a library (not spawned as a subprocess). This module owns only the
deterministic wrapper: scanning the read-only source tree, caching recognized
Markdown, routing each document to a family member, and writing tracked
Markdown under the target directory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable


VERSION = "0.4.0"
SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
VISION_ENGINE = "vision-api"
BANNED_OPENERS = (
    "вот распознанный текст",
    "вот транскрипция",
    "here is the recognized text",
    "here is the transcription",
    "i cannot",
    "i can't",
    "я не могу",
    "summary:",
)


class RecognitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    sources: tuple[Path, ...]
    target: Path
    cache: Path
    engine: str
    vision_api_url: str | None
    vision_api_key: str | None
    vision_model: str | None
    ocr_script: Path
    timeout: int
    profile_hash: str


@dataclass(frozen=True)
class SourceDocument:
    path: Path
    relative: str
    sha256: str
    size: int
    mtime_ns: int


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def expanded_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_roots(sources: Iterable[Path], target: Path, cache: Path) -> None:
    # Sources are read-only inputs and must never overlap each other, the
    # target, or the cache — otherwise a scan could pick up generated output.
    named_sources = [(f"source[{i}]", src) for i, src in enumerate(sources)]
    strict = named_sources + [("target", target), ("cache", cache)]
    for index, (left_name, left) in enumerate(strict):
        for right_name, right in strict[index + 1 :]:
            # target/cache may be nested (cache inside target); handle separately.
            if {left_name, right_name} == {"target", "cache"}:
                continue
            if left == right or is_within(left, right) or is_within(right, left):
                raise RecognitionError(
                    f"{left_name} and {right_name} must be separate, non-nested paths: "
                    f"{left} / {right}"
                )
    # The cache may live inside the target (the user is responsible for keeping
    # it out of version control). The reverse would bury tracked output inside
    # the cache, so it stays forbidden, as does using one path for both.
    if target == cache:
        raise RecognitionError(
            f"target and cache must not be the same path: {target}"
        )
    if is_within(target, cache):
        raise RecognitionError(
            f"target must not be nested inside cache: {target} / {cache}"
        )


def parse_source_dirs(value: str) -> tuple[Path, ...]:
    parts = [part.strip() for part in value.split(":")]
    parts = [part for part in parts if part]
    if not parts:
        raise RecognitionError("AGENT_HEALTH_SOURCE_DIR is empty")
    paths: list[Path] = []
    seen: set[Path] = set()
    for part in parts:
        path = expanded_path(part)
        if not path.is_dir():
            raise RecognitionError(f"Source directory does not exist: {path}")
        if path in seen:
            raise RecognitionError(f"Duplicate source directory: {path}")
        seen.add(path)
        paths.append(path)
    if len(paths) > 1:
        names = [path.name for path in paths]
        if len(set(names)) != len(names):
            raise RecognitionError(
                "Source directories must have unique names when multiple are given"
            )
    return tuple(paths)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RecognitionError(f"Missing required environment variable: {name}")
    return value


def default_ocr_script() -> Path:
    return Path(__file__).resolve().parents[2] / "ocr" / "scripts" / "ocr.py"


def load_config() -> Config:
    sources = parse_source_dirs(required_env("AGENT_HEALTH_SOURCE_DIR"))
    target = expanded_path(required_env("AGENT_HEALTH_TARGET_DIR"))
    cache = expanded_path(required_env("AGENT_HEALTH_CACHE_DIR"))
    validate_roots(sources, target, cache)

    engine = required_env("AGENT_HEALTH_OCR_ENGINE")

    vision_api_url: str | None = None
    vision_api_key: str | None = None
    vision_model: str | None = None
    if engine == VISION_ENGINE:
        vision_api_url = required_env("AGENT_HEALTH_VISION_API_URL")
        vision_api_key = required_env("AGENT_HEALTH_VISION_API_KEY")
        vision_model = required_env("AGENT_HEALTH_VISION_MODEL")

    ocr_override = os.environ.get("AGENT_HEALTH_OCR_SCRIPT", "").strip()
    ocr_script = expanded_path(ocr_override) if ocr_override else default_ocr_script()
    if not ocr_script.is_file():
        raise RecognitionError(f"ocr.py script not found: {ocr_script}")

    try:
        timeout = int(os.environ.get("AGENT_HEALTH_OCR_TIMEOUT_SECONDS", "600"))
    except ValueError as exc:
        raise RecognitionError("AGENT_HEALTH_OCR_TIMEOUT_SECONDS must be an integer") from exc
    if timeout <= 0:
        raise RecognitionError("AGENT_HEALTH_OCR_TIMEOUT_SECONDS must be positive")

    profile = {
        "engine": engine,
        "vision_api_url": vision_api_url,
        "vision_model": vision_model,
        "ocr_script": ocr_script.name,
        "script_version": VERSION,
    }
    return Config(
        sources=sources,
        target=target,
        cache=cache,
        engine=engine,
        vision_api_url=vision_api_url,
        vision_api_key=vision_api_key,
        vision_model=vision_model,
        ocr_script=ocr_script,
        timeout=timeout,
        profile_hash=sha256_bytes(canonical_json(profile).encode("utf-8")),
    )


def ensure_write_path(path: Path, roots: Iterable[Path]) -> None:
    resolved = path.resolve()
    if not any(is_within(resolved, root.resolve()) for root in roots):
        raise RecognitionError(f"Refusing write outside target/cache: {resolved}")


def atomic_write(path: Path, content: str | bytes, roots: Iterable[Path]) -> bool:
    ensure_write_path(path, roots)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8") if isinstance(content, str) else content
    if path.exists() and path.read_bytes() == data:
        return False
    temporary = path.with_name(f".{path.name}.tmp")
    ensure_write_path(temporary, roots)
    temporary.write_bytes(data)
    temporary.replace(path)
    return True


def scan_source(source: Path, prefix: str = "") -> list[SourceDocument]:
    documents: list[SourceDocument] = []
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file() or path.name.startswith(".") or path.name.startswith("~$"):
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        stat = path.stat()
        documents.append(
            SourceDocument(
                path=path,
                relative=prefix + path.relative_to(source).as_posix(),
                sha256=sha256_file(path),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        )
    return documents


def scan_sources(sources: Iterable[Path]) -> list[SourceDocument]:
    sources = tuple(sources)
    multi = len(sources) > 1
    documents: list[SourceDocument] = []
    for source in sources:
        prefix = f"{source.name}/" if multi else ""
        documents.extend(scan_source(source, prefix))
    documents.sort(key=lambda doc: doc.relative.casefold())
    return documents


def source_manifest(documents: list[SourceDocument]) -> dict[str, dict[str, Any]]:
    return {
        doc.relative: {
            "sha256": doc.sha256,
            "size": doc.size,
            "mtime_ns": doc.mtime_ns,
        }
        for doc in documents
    }


_OCR_MODULES: dict[Path, ModuleType] = {}


def load_ocr_module(script_path: Path) -> ModuleType:
    """Import ocr.py as a library module, cached by resolved path.

    ocr.py has no package structure, so it is loaded directly from its file
    path via importlib instead of a normal `import` statement.
    """
    resolved = script_path.resolve()
    module = _OCR_MODULES.get(resolved)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(f"healthos_ocr_{resolved.stem}", resolved)
    if spec is None or spec.loader is None:
        raise RecognitionError(f"Could not load ocr module from: {resolved}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec_module(): dataclasses on Python 3.14 resolve their
    # own module via sys.modules[cls.__module__] during class creation, which
    # crashes with AttributeError on None if the module isn't registered yet.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _OCR_MODULES[resolved] = module
    return module


def run_ocr(config: Config, document: SourceDocument) -> str:
    ocr_module = load_ocr_module(config.ocr_script)
    options = ocr_module.RecognizeOptions(
        engine=config.engine,
        vision_api_url=config.vision_api_url or "",
        vision_api_key=config.vision_api_key or "",
        vision_model=config.vision_model or "",
        # Bounds the vision-api HTTP request via the openai SDK's own client
        # timeout. Local engines run in-process with no external kill switch
        # now that recognition is a library call rather than a subprocess.
        timeout=float(config.timeout),
    )
    try:
        pages = ocr_module.recognize(document.path, options)
    except Exception as exc:
        raise RecognitionError(f"ocr recognition failed: {exc}") from exc
    markdown = ocr_module.to_markdown(pages, document.path.name)
    if not markdown.strip():
        raise RecognitionError("ocr recognition produced empty output")
    return markdown


def recognize_document(
    config: Config, document: SourceDocument, check: bool
) -> tuple[str, bool, str]:
    cache_path = config.cache / "responses" / document.sha256 / f"{config.profile_hash}.json"
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        return cached["markdown"], True, cached["recognized_at"]
    if check:
        raise RecognitionError("cache_missing")
    markdown = run_ocr(config, document)
    recognized_at = dt.datetime.now(dt.timezone.utc).date().isoformat()
    cached = {"markdown": markdown, "recognized_at": recognized_at}
    atomic_write(
        cache_path,
        json.dumps(cached, ensure_ascii=False, indent=2) + "\n",
        [config.cache],
    )
    return markdown, False, recognized_at


def validate_markdown(markdown: str) -> list[str]:
    issues: list[str] = []
    stripped = markdown.strip()
    if not stripped:
        issues.append("empty_markdown")
        return issues
    opener = stripped.casefold()[:240]
    if any(opener.startswith(value) for value in BANNED_OPENERS):
        issues.append("model_preamble_or_refusal")
    return issues


def sanitize_scalar(value: str) -> str:
    collapsed = re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
    return collapsed[:500]


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(sanitize_scalar(str(value)), ensure_ascii=False)


def markdown_frontmatter(metadata: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in metadata.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {yaml_scalar(item)}" for item in value)
        else:
            lines.append(f"{key}: {yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def safe_source_name(path: Path) -> str:
    stem = unicodedata.normalize("NFC", path.stem).strip().replace("/", "-").replace("\\", "-")
    stem = re.sub(r"[\x00-\x1f:]", "-", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .-")
    return stem or "document"


def strip_leading_h1(markdown: str) -> str:
    lines = markdown.splitlines()
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index < len(lines) and lines[index].lstrip().startswith("# "):
        index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        return "\n".join(lines[index:]).strip()
    return markdown.strip()


def read_frontmatter_source(path: Path) -> str | None:
    if not path.is_file():
        return None
    content = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r'^source_path:\s*"(.*)"\s*$', content, re.MULTILINE)
    return json.loads(f'"{match.group(1)}"') if match else None


def output_path_for(config: Config, document: SourceDocument) -> Path:
    """Mirror the source tree one-to-one under target.

    The recognized Markdown lands at the same relative path as its source
    document (subfolders preserved), with the source filename stem sanitized
    and a `.md` suffix.
    """
    parent = config.target / Path(document.relative).parent
    candidate = parent / f"{safe_source_name(document.path)}.md"
    existing_source = read_frontmatter_source(candidate)
    if existing_source is not None and existing_source != document.relative:
        candidate = parent / f"{document.sha256[:8]}-{safe_source_name(document.path)}.md"
    return candidate


def build_markdown(
    config: Config,
    document: SourceDocument,
    markdown: str,
    status: str,
    issues: list[str],
    recognized_at: str,
) -> str:
    metadata = {
        "type": "medical-document",
        "source_path": document.relative,
        "source_sha256": document.sha256,
        "source_type": document.path.suffix.lower().lstrip("."),
        "engine": config.engine,
        "vision_model": config.vision_model,
        "ocr_script": config.ocr_script.name,
        "profile_sha256": config.profile_hash,
        "recognized_at": recognized_at,
        "status": status,
        "issues": issues,
    }
    body = strip_leading_h1(markdown)
    return f"{markdown_frontmatter(metadata)}\n\n{body}\n".rstrip() + "\n"


def process(config: Config, check: bool) -> int:
    if not check:
        config.target.mkdir(parents=True, exist_ok=True)
        config.cache.mkdir(parents=True, exist_ok=True)
    before_documents = scan_sources(config.sources)
    before_manifest = source_manifest(before_documents)
    count = 0
    mismatches: list[str] = []
    for index, document in enumerate(before_documents, 1):
        print(f"[{index}/{len(before_documents)}] {document.relative}", flush=True)
        try:
            markdown, cache_hit, recognized_at = recognize_document(config, document, check)
            issues = validate_markdown(markdown)
            status = "failed" if issues else "recognized"
        except Exception as exc:
            if check:
                mismatches.append(f"{document.relative}: {exc}")
                continue
            print(f"Error: {document.relative}: {exc}", file=sys.stderr)
            return 1

        output_path = output_path_for(config, document)
        rendered = build_markdown(config, document, markdown, status, issues, recognized_at)
        if check:
            if not output_path.is_file() or output_path.read_text(encoding="utf-8") != rendered:
                mismatches.append(f"{document.relative}: output mismatch at {output_path}")
        else:
            atomic_write(output_path, rendered, [config.target])
        count += 1

    after_manifest = source_manifest(scan_sources(config.sources))
    if before_manifest != after_manifest:
        raise RecognitionError("Source manifest changed during processing")

    if mismatches:
        print("Check failed:")
        for mismatch in mismatches:
            print(f"- {mismatch}")
        return 1
    print(f"Processed {count} documents. Profile: {config.profile_hash}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recognize external family medical documents into Markdown via the ocr skill"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="index",
        choices=["index"],
        help="Command to run (default: index — mirror recognized documents into target)",
    )
    parser.add_argument("--check", action="store_true", help="Verify cache and outputs without writes")
    args = parser.parse_args(argv)
    try:
        return process(load_config(), check=args.check)
    except RecognitionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
