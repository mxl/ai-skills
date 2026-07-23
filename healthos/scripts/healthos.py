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

import yaml


VERSION = "0.3.0"
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


def load_family(target: Path) -> list[dict[str, Any]]:
    family_path = target / "family.yaml"
    if not family_path.is_file():
        raise RecognitionError(f"Missing family configuration: {family_path}")
    data = yaml.safe_load(family_path.read_text(encoding="utf-8"))
    people = data.get("people") if isinstance(data, dict) else None
    if not isinstance(people, list) or not people:
        raise RecognitionError("family.yaml must contain a non-empty people list")
    seen_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for person in people:
        if not isinstance(person, dict):
            raise RecognitionError("Each family person must be a mapping")
        person_id = person.get("id")
        names = person.get("names")
        birth_date = normalize_date(person.get("birth_date"))
        roots = person.get("source_roots", [])
        if not isinstance(person_id, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", person_id):
            raise RecognitionError(f"Invalid family person id: {person_id!r}")
        if person_id in seen_ids:
            raise RecognitionError(f"Duplicate family person id: {person_id}")
        if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
            raise RecognitionError(f"Person {person_id} must have names")
        if birth_date is None:
            raise RecognitionError(f"Person {person_id} must have a valid birth_date")
        if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots):
            raise RecognitionError(f"Person {person_id} source_roots must be strings")
        seen_ids.add(person_id)
        normalized.append(
            {
                "id": person_id,
                "names": names,
                "name_token_variants": [name_tokens(name) for name in names],
                "birth_date": birth_date,
                "birth_date_forms": birthdate_forms(birth_date),
                "source_roots": [normalize_root(root) for root in roots],
            }
        )
    return normalized


def normalize_root(value: str) -> str:
    value = value.strip().strip("/")
    return "." if not value or value == "." else value.casefold()


def fold_text(value: str) -> str:
    return value.casefold().replace("ё", "е")


def name_tokens(value: str) -> list[str]:
    return re.findall(r"[a-zа-я0-9]+", fold_text(value))


def normalize_date(value: Any) -> str | None:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if not isinstance(value, str):
        return None
    value = value.strip()
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def birthdate_forms(iso_date: str) -> list[str]:
    year, month, day = (int(part) for part in iso_date.split("-"))
    return [
        f"{day:02d}.{month:02d}.{year}",
        f"{day}.{month}.{year}",
        f"{year:04d}-{month:02d}-{day:02d}",
        f"{day:02d}/{month:02d}/{year}",
    ]


def name_in_text(folded_text: str, tokens: list[str]) -> bool:
    if not tokens:
        return False
    pattern = r"\b" + r"[^0-9a-zа-я]+".join(re.escape(token) for token in tokens) + r"\b"
    return re.search(pattern, folded_text) is not None


def birthdate_in_text(text: str, forms: list[str]) -> bool:
    return any(form in text for form in forms)


def source_root_candidates(relative: str, people: list[dict[str, Any]]) -> set[str]:
    relative_folded = relative.casefold()
    candidates: set[str] = set()
    for person in people:
        for root in person["source_roots"]:
            if root == "." or relative_folded == root or relative_folded.startswith(f"{root}/"):
                candidates.add(person["id"])
    return candidates


def assign_person(
    markdown: str, relative: str, people: list[dict[str, Any]]
) -> tuple[str | None, str]:
    folded = fold_text(markdown)
    name_matches: set[str] = set()
    strong_matches: set[str] = set()
    for person in people:
        has_name = any(
            name_in_text(folded, tokens) for tokens in person["name_token_variants"]
        )
        if not has_name:
            continue
        name_matches.add(person["id"])
        if birthdate_in_text(markdown, person["birth_date_forms"]):
            strong_matches.add(person["id"])

    if len(strong_matches) == 1:
        return next(iter(strong_matches)), "identity_match"
    if len(strong_matches) > 1:
        return None, "identity_conflict"
    if len(name_matches) == 1:
        return next(iter(name_matches)), "identity_match"
    if len(name_matches) > 1:
        return None, "identity_conflict"

    roots = source_root_candidates(relative, people)
    if len(roots) == 1:
        return next(iter(roots)), "source_root_match"
    return None, "identity_missing"


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


def infer_year(relative: str) -> str:
    for part in Path(relative).parts:
        if re.fullmatch(r"(?:19|20)\d{2}", part):
            return part
    return "unknown"


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


def output_path_for(
    config: Config, document: SourceDocument, person_id: str | None, year: str
) -> Path:
    parent = config.target / ("people" if person_id else "_unassigned")
    if person_id:
        parent = parent / person_id
    parent = parent / year
    candidate = parent / f"{safe_source_name(document.path)}.md"
    existing_source = read_frontmatter_source(candidate)
    if existing_source is not None and existing_source != document.relative:
        candidate = parent / f"{document.sha256[:8]}-{safe_source_name(document.path)}.md"
    return candidate


def build_markdown(
    config: Config,
    document: SourceDocument,
    markdown: str,
    person_id: str | None,
    assignment_reason: str,
    status: str,
    issues: list[str],
    recognized_at: str,
) -> str:
    metadata = {
        "type": "medical-document",
        "person": person_id,
        "source_path": document.relative,
        "source_sha256": document.sha256,
        "source_type": document.path.suffix.lower().lstrip("."),
        "engine": config.engine,
        "vision_model": config.vision_model,
        "ocr_script": config.ocr_script.name,
        "profile_sha256": config.profile_hash,
        "recognized_at": recognized_at,
        "assignment_reason": assignment_reason,
        "status": status,
        "issues": issues,
    }
    body = strip_leading_h1(markdown)
    return f"{markdown_frontmatter(metadata)}\n\n{body}\n".rstrip() + "\n"


def build_index(config: Config, results: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for result in results:
        key = result["person_id"] or "_unassigned"
        counts[key] = counts.get(key, 0) + 1
    lines = [
        "# Recognized Medical Documents",
        "",
        f"- Profile: `{config.profile_hash}`",
        f"- Engine: `{config.engine}`",
        f"- Documents: {len(results)}",
        "",
        "## By Person",
        "",
    ]
    for person_id in sorted(counts):
        lines.append(f"- `{person_id}`: {counts[person_id]}")
    lines.extend(["", "## Documents", ""])
    for result in sorted(results, key=lambda item: item["relative"].casefold()):
        lines.append(
            f"- `{result['status']}` · `{result['person_id'] or '_unassigned'}` · "
            f"`{result['relative']}` → `{result['output_relative']}`"
        )
        for issue in result["issues"]:
            lines.append(f"  - {issue}")
    return "\n".join(lines).rstrip() + "\n"


def process(config: Config, check: bool) -> int:
    if not check:
        config.target.mkdir(parents=True, exist_ok=True)
        config.cache.mkdir(parents=True, exist_ok=True)
    people = load_family(config.target)
    before_documents = scan_sources(config.sources)
    before_manifest = source_manifest(before_documents)
    results: list[dict[str, Any]] = []
    mismatches: list[str] = []
    for index, document in enumerate(before_documents, 1):
        print(f"[{index}/{len(before_documents)}] {document.relative}", flush=True)
        try:
            markdown, cache_hit, recognized_at = recognize_document(config, document, check)
            issues = validate_markdown(markdown)
            person_id, assignment_reason = assign_person(markdown, document.relative, people)
            status = "failed" if issues else ("recognized" if person_id else "unassigned")
        except Exception as exc:
            if check:
                mismatches.append(f"{document.relative}: {exc}")
                continue
            print(f"Error: {document.relative}: {exc}", file=sys.stderr)
            return 1

        year = infer_year(document.relative)
        output_path = output_path_for(
            config, document, person_id if status == "recognized" else None, year
        )
        rendered = build_markdown(
            config,
            document,
            markdown,
            person_id if status == "recognized" else None,
            assignment_reason,
            status,
            issues,
            recognized_at,
        )
        if check:
            if not output_path.is_file() or output_path.read_text(encoding="utf-8") != rendered:
                mismatches.append(f"{document.relative}: output mismatch at {output_path}")
        else:
            atomic_write(output_path, rendered, [config.target])
        results.append(
            {
                "relative": document.relative,
                "person_id": person_id if status == "recognized" else None,
                "status": status,
                "issues": issues,
                "cache_hit": cache_hit,
                "output_relative": output_path.relative_to(config.target).as_posix(),
            }
        )

    index_content = build_index(config, results)
    index_path = config.target / "recognition-index.md"
    if check:
        if not index_path.is_file() or index_path.read_text(encoding="utf-8") != index_content:
            mismatches.append("recognition-index.md mismatch")
    else:
        atomic_write(index_path, index_content, [config.target])

    after_manifest = source_manifest(scan_sources(config.sources))
    if before_manifest != after_manifest:
        raise RecognitionError("Source manifest changed during processing")

    if mismatches:
        print("Check failed:")
        for mismatch in mismatches:
            print(f"- {mismatch}")
        return 1
    print(f"Processed {len(results)} documents. Profile: {config.profile_hash}")
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
        help="Command to run (default: index — recognize documents and rebuild the index)",
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
