#!/usr/bin/env python3
"""Validate, summarize, and render canonical meeting transcripts."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator


SKILL_DIR = Path(__file__).resolve().parent.parent
MEETING_SCHEMA = SKILL_DIR / "schemas" / "meeting.schema.json"
DEFAULT_BUNDLE = SKILL_DIR / "summaries" / "default"
DEFAULT_TRANSCRIPT_TEMPLATE = SKILL_DIR / "templates" / "meeting-transcript.md"
PLACEHOLDER_RE = re.compile(r"{{\s*([A-Za-z0-9_.-]+)\s*}}")


class MeetingError(Exception):
    pass


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MeetingError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MeetingError(f"invalid JSON in {path}: {exc}") from exc


def read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise MeetingError(f"cannot read {path}: {exc}") from exc


def load_schema(path: Path) -> dict:
    schema = load_json(path)
    if not isinstance(schema, dict):
        raise MeetingError(f"schema must be a JSON object: {path}")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise MeetingError(f"invalid JSON schema {path}: {exc}") from exc
    return schema


def format_json_path(parts) -> str:
    path = "$"
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


def validate(instance: Any, schema: dict, label: str) -> None:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(instance),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if errors:
        details = "\n".join(
            f"  {format_json_path(error.absolute_path)}: {error.message}"
            for error in errors
        )
        raise MeetingError(f"{label} validation failed:\n{details}")


def validate_meeting(meeting: Any) -> dict:
    if not isinstance(meeting, dict):
        raise MeetingError("meeting JSON must be an object")
    validate(meeting, load_schema(MEETING_SCHEMA), "meeting")
    if not meeting["transcript"] and not meeting["notes"]:
        raise MeetingError("meeting must contain transcript segments or notes")
    return meeting


def resolve_bundle(value: str | None) -> Path:
    selected = value or os.getenv("MEETING_TRANSCRIPT_SUMMARY_BUNDLE")
    bundle = Path(selected).expanduser().resolve() if selected else DEFAULT_BUNDLE
    required = (bundle / "prompt.md", bundle / "summary.schema.json", bundle / "summary.md")
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise MeetingError(f"invalid summary bundle; missing: {', '.join(missing)}")
    return bundle


def resolve_template(value: str | None, env_name: str, default: Path) -> Path:
    selected = value or os.getenv(env_name)
    path = Path(selected).expanduser().resolve() if selected else default
    if not path.is_file():
        raise MeetingError(f"template does not exist: {path}")
    return path


def canonical_bytes(meeting: dict) -> bytes:
    return (json.dumps(meeting, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower().encode("ascii", "ignore").decode())
    return slug.strip("-")[:48] or "meeting"


def artifact_key(meeting: dict) -> str:
    meeting_date = (meeting.get("started_at") or "")[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", meeting_date):
        meeting_date = "undated"
    digest = hashlib.sha256(canonical_bytes(meeting)).hexdigest()[:12]
    return f"{meeting_date}-{slugify(meeting.get('title', ''))}-{digest}"


def artifact_paths(meeting: dict, root: Path) -> tuple[Path, Path, Path]:
    directory = root.expanduser().resolve() / artifact_key(meeting)
    return directory, directory / "meeting.json", directory / "summary.json"


def display_participant(participant: dict) -> str:
    name = participant["name"].strip()
    email = participant["email"].strip()
    if name and email:
        return f"{name} <{email}>"
    return name or email or "Unknown"


def infer_mode(meeting: dict) -> str:
    if meeting["transcript"] and meeting["notes"]:
        return "improve"
    if meeting["transcript"]:
        return "generate-summary"
    return "save"


def mode_reason(meeting: dict) -> str:
    if meeting["transcript"] and meeting["notes"]:
        return "transcript_and_notes"
    if meeting["transcript"]:
        return "transcript_only"
    return "notes_only"


def summarization_text(
    meeting: dict,
    *,
    include_notes: bool = True,
    include_transcript: bool = True,
) -> str:
    participants = ", ".join(display_participant(item) for item in meeting["participants"])
    lines = [
        f"Mode: {infer_mode(meeting)}",
        f"Title: {meeting['title']}",
        f"Started: {meeting['started_at']}",
        f"Ended: {meeting['ended_at']}",
        f"Participants: {participants}",
    ]
    if meeting["resources"]:
        lines.extend(["", "Resources:"])
        lines.extend(f"- {item['label']}: {item['target']}" for item in meeting["resources"])
    if include_notes and meeting["notes"]:
        lines.extend(["", "Provided notes:"])
        for item in meeting["notes"]:
            heading = item["title"] or "Untitled"
            lines.extend([f"\n### {heading}", item["text"]])
    if include_transcript and meeting["transcript"]:
        lines.extend(["", "Transcript:", transcript_body(meeting)])
    return "\n".join(lines).rstrip() + "\n"


def reconciliation_text(meeting: dict, draft: dict | None = None) -> str:
    text = summarization_text(meeting, include_notes=True, include_transcript=True).rstrip()
    draft_text = (
        json.dumps(draft, ensure_ascii=False, indent=2)
        if draft is not None
        else "Use the draft summary produced from draft_prompt."
    )
    return (
        f"{text}\n\nDraft summary:\n{draft_text}\n\n"
        "Reconcile the draft with the provided notes against the transcript. Keep supported details, "
        "record contradictions and unconfirmed material, and return the final summary JSON.\n"
    )


def prompt_packet(meeting: dict, bundle: Path) -> dict:
    packet = {
        "system_prompt": (bundle / "prompt.md").read_text(encoding="utf-8").strip(),
        "summary_schema": load_schema(bundle / "summary.schema.json"),
    }
    if infer_mode(meeting) == "improve":
        draft_prompt = summarization_text(meeting, include_notes=False, include_transcript=True)
        packet.update(
            {
                "user_prompt": draft_prompt,
                "draft_prompt": draft_prompt,
                "reconcile_prompt": reconciliation_text(meeting),
            }
        )
    else:
        packet["user_prompt"] = summarization_text(meeting)
    return packet


def api_completion(system_prompt: str, user_prompt: str, summary_schema: dict) -> dict:
    base = os.getenv("MEETING_TRANSCRIPT_API_BASE", "").strip()
    api_key = os.getenv("MEETING_TRANSCRIPT_API_KEY", "").strip()
    model = os.getenv("MEETING_TRANSCRIPT_MODEL", "").strip()
    missing = [
        name
        for name, value in (
            ("MEETING_TRANSCRIPT_API_BASE", base),
            ("MEETING_TRANSCRIPT_API_KEY", api_key),
            ("MEETING_TRANSCRIPT_MODEL", model),
        )
        if not value
    ]
    if missing:
        raise MeetingError(f"missing API configuration: {', '.join(missing)}")
    url = base.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "meeting_summary",
                "strict": True,
                "schema": summary_schema,
            },
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise MeetingError(f"summary API HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise MeetingError(f"summary API request failed: {exc.reason}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MeetingError(f"summary API returned invalid JSON: {exc}") from exc
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MeetingError("summary API response has no assistant message") from exc
    if message.get("refusal"):
        raise MeetingError(f"summary API refused request: {message['refusal']}")
    content = message.get("content")
    if not isinstance(content, str):
        raise MeetingError("summary API assistant content is not a JSON string")
    try:
        summary = json.loads(content)
    except json.JSONDecodeError as exc:
        raise MeetingError(f"summary API assistant content is invalid JSON: {exc}") from exc
    validate(summary, summary_schema, "summary")
    return summary


def api_summary(meeting: dict, bundle: Path) -> dict:
    packet = prompt_packet(meeting, bundle)
    draft = api_completion(
        packet["system_prompt"],
        packet["user_prompt"],
        packet["summary_schema"],
    )
    if infer_mode(meeting) != "improve":
        return draft
    return api_completion(
        packet["system_prompt"],
        reconciliation_text(meeting, draft),
        packet["summary_schema"],
    )


def markdown_list(values: list) -> str:
    return "\n".join(f"- {value}" for value in values) if values else "_None._"


def table_cell(value: Any) -> str:
    if isinstance(value, list):
        value = "; ".join(str(item) for item in value)
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_None._"
    output = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    output.extend("| " + " | ".join(table_cell(cell) for cell in row) + " |" for row in rows)
    return "\n".join(output)


def generic_markdown(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return markdown_list(value)
        return "\n".join(f"- `{json.dumps(item, ensure_ascii=False)}`" for item in value) or "_None._"
    if isinstance(value, dict):
        return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"
    return str(value)


def lookup(context: dict, key: str) -> Any:
    value: Any = context
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise MeetingError(f"template references unknown placeholder: {key}")
        value = value[part]
    return value


def render_template(template: str, context: dict) -> str:
    rendered = PLACEHOLDER_RE.sub(lambda match: generic_markdown(lookup(context, match.group(1))), template)
    unresolved = PLACEHOLDER_RE.findall(rendered)
    if unresolved:
        raise MeetingError(f"unresolved template placeholders: {', '.join(sorted(set(unresolved)))}")
    return rendered.rstrip() + "\n"


def grouped_transcript(meeting: dict) -> list[dict[str, str]]:
    grouped: list[dict[str, str]] = []
    for item in meeting["transcript"]:
        speaker = item["speaker"] or "Unknown"
        if grouped and grouped[-1]["speaker"] == speaker:
            grouped[-1]["text"] = grouped[-1]["text"].rstrip() + " " + item["text"].lstrip()
            continue
        grouped.append({"speaker": speaker, "text": item["text"]})
    return grouped


def transcript_body(meeting: dict) -> str:
    chunks = [f"**{item['speaker']}:** {item['text']}" for item in grouped_transcript(meeting)]
    return "\n\n".join(chunks) if chunks else "_No transcript was provided._"


def default_render(meeting: dict, summary: dict, options: dict) -> Mapping[str, str]:
    participants = ", ".join(display_participant(item) for item in meeting["participants"]) or "Unknown"
    meeting_date = (meeting["started_at"] or "")[:10]
    base = {
        "created_date": date.today().isoformat(),
        "title": meeting["title"] or "Untitled meeting",
        "source": meeting["source"],
        "meeting_date": meeting_date,
        "started_at": meeting["started_at"],
        "ended_at": meeting["ended_at"],
        "participants": participants,
        "transcript_body": transcript_body(meeting),
    }
    summary_context = dict(base)
    summary_context.update(summary)
    summary_context["key_points"] = markdown_list(summary.get("key_points", []))
    summary_context["decisions"] = markdown_list(summary.get("decisions", []))
    summary_context["open_questions"] = markdown_list(summary.get("open_questions", []))
    entities = summary.get("entities", [])
    summary_context["entities_section"] = (
        "## Entities\n\n"
        + markdown_table(
            ["Entity", "Role", "Facts"],
            [[item["name"], item["role"], item["facts"]] for item in entities],
        )
        + "\n"
        if entities
        else ""
    )
    links = summary.get("links", [])
    summary_context["links_section"] = (
        "## Links And Resources\n\n" + "\n".join(f"- [{item['label']}]({item['target']})" for item in links) + "\n"
        if links
        else ""
    )
    summary_context["action_items_table"] = markdown_table(
        ["Task", "Owner", "Due date"],
        [[item["task"], item["owner"], item["due_date"]] for item in summary.get("action_items", [])],
    )
    verification = summary.get("verification", [])
    summary_context["verification_section"] = (
        "## Verification\n\n" + markdown_list(verification) + "\n" if verification else ""
    )
    transcript_template = Path(options["transcript_template"]).read_text(encoding="utf-8")
    summary_template = Path(options["summary_template"]).read_text(encoding="utf-8")
    return {
        "transcript.md": render_template(transcript_template, base),
        "summary.md": render_template(summary_template, summary_context),
    }


def load_renderer(path: Path):
    spec = importlib.util.spec_from_file_location("meeting_transcript_renderer", path)
    if spec is None or spec.loader is None:
        raise MeetingError(f"cannot load renderer plugin: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise MeetingError(f"renderer plugin failed to load: {exc}") from exc
    renderer = getattr(module, "render", None)
    if not callable(renderer):
        raise MeetingError("renderer plugin must define callable render(meeting, summary, options)")
    return renderer


def validate_outputs(outputs: Any, out_dir: Path) -> dict[Path, str]:
    if not isinstance(outputs, Mapping) or not outputs:
        raise MeetingError("renderer must return non-empty mapping of relative paths to text")
    result: dict[Path, str] = {}
    root = out_dir.resolve()
    for name, content in outputs.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise MeetingError("renderer output paths and contents must be strings")
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise MeetingError(f"unsafe renderer output path: {name}")
        destination = (root / relative).resolve()
        try:
            destination.relative_to(root)
        except ValueError as exc:
            raise MeetingError(f"unsafe renderer output path: {name}") from exc
        if destination in result:
            raise MeetingError(f"duplicate renderer output path: {name}")
        result[destination] = content
    return result


def atomic_write_many(files: Mapping[Path, bytes]) -> None:
    temporary: list[tuple[Path, Path]] = []
    try:
        for destination, content in files.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
            temp_path = Path(temp_name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.append((temp_path, destination))
        for temp_path, destination in temporary:
            temp_path.replace(destination)
    except Exception:
        for temp_path, _ in temporary:
            temp_path.unlink(missing_ok=True)
        raise


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def changed_files(files: Mapping[Path, bytes]) -> dict[Path, bytes]:
    changed: dict[Path, bytes] = {}
    for path, content in files.items():
        try:
            existing = path.read_bytes()
        except FileNotFoundError:
            changed[path] = content
            continue
        except OSError as exc:
            raise MeetingError(f"cannot read {path}: {exc}") from exc
        if sha256_bytes(existing) != sha256_bytes(content):
            changed[path] = content
    return changed


def load_prepared_meeting(source_path: Path, adapter_value: str | None) -> dict:
    if not adapter_value:
        return validate_meeting(load_json(source_path))

    adapter = Path(adapter_value).expanduser().resolve()
    if not adapter.is_file():
        raise MeetingError(f"adapter does not exist: {adapter}")
    with tempfile.TemporaryDirectory(prefix="meeting-transcript-adapter-") as temp:
        output_path = Path(temp) / "meeting.json"
        command = [
            sys.executable,
            str(adapter),
            str(source_path),
            "--out",
            str(output_path),
            "--schema",
            str(MEETING_SCHEMA),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        except subprocess.TimeoutExpired as exc:
            raise MeetingError(f"adapter timed out after {exc.timeout} seconds: {adapter}") from exc
        except OSError as exc:
            raise MeetingError(f"cannot run adapter {adapter}: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise MeetingError(f"adapter failed ({result.returncode}): {detail[:500]}")
        if not output_path.is_file():
            raise MeetingError(f"adapter did not create canonical output: {output_path}")
        meeting = validate_meeting(load_json(output_path))
    return meeting


def resolved_assets(args, bundle: Path) -> tuple[Path, Path]:
    transcript = resolve_template(
        getattr(args, "transcript_template", None),
        "MEETING_TRANSCRIPT_TRANSCRIPT_TEMPLATE",
        DEFAULT_TRANSCRIPT_TEMPLATE,
    )
    summary = resolve_template(
        getattr(args, "summary_template", None),
        "MEETING_TRANSCRIPT_SUMMARY_TEMPLATE",
        bundle / "summary.md",
    )
    return transcript, summary


def prepare_command(args) -> int:
    source_path = Path(args.meeting).expanduser().resolve()
    meeting = load_prepared_meeting(source_path, args.adapter)
    bundle = resolve_bundle(args.bundle)
    root = Path(args.artifacts_dir).expanduser() if args.artifacts_dir else source_path.parent
    directory, meeting_path, summary_path = artifact_paths(meeting, root)
    atomic_write_many({meeting_path: canonical_bytes(meeting)})
    packet = prompt_packet(meeting, bundle)
    packet.update(
        {
            "mode": infer_mode(meeting),
            "mode_reason": mode_reason(meeting),
            "artifact_directory": str(directory),
            "meeting_json": str(meeting_path),
            "summary_json": str(summary_path),
        }
    )
    json.dump(packet, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def import_command(args) -> int:
    source_path = Path(args.meeting).expanduser().resolve()
    source_bytes = read_bytes(source_path)
    meeting = validate_meeting(load_json(source_path))
    bundle = resolve_bundle(args.bundle)
    summary_schema = load_schema(bundle / "summary.schema.json")
    if args.api:
        summary = api_summary(meeting, bundle)
    else:
        summary = load_json(Path(args.summary).expanduser().resolve())
        validate(summary, summary_schema, "summary")
    transcript_template, summary_template = resolved_assets(args, bundle)
    options = {
        "bundle": str(bundle),
        "summary_schema": str(bundle / "summary.schema.json"),
        "summary_template": str(summary_template),
        "transcript_template": str(transcript_template),
    }
    if args.engine:
        renderer = load_renderer(Path(args.engine).expanduser().resolve())
        try:
            rendered = renderer(meeting, summary, options)
        except Exception as exc:
            raise MeetingError(f"renderer plugin failed: {exc}") from exc
    else:
        rendered = default_render(meeting, summary, options)
    out_dir = Path(args.out).expanduser().resolve()
    output_files = validate_outputs(rendered, out_dir)
    transcript_json_path = (out_dir / "transcript.json").resolve()
    if transcript_json_path in output_files:
        raise MeetingError("renderer output conflicts with reserved path: transcript.json")
    root = Path(args.artifacts_dir).expanduser() if args.artifacts_dir else out_dir / ".meeting-transcript"
    _, meeting_path, summary_path = artifact_paths(meeting, root)
    files: dict[Path, bytes] = {
        meeting_path: canonical_bytes(meeting),
        summary_path: (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        transcript_json_path: source_bytes,
    }
    files.update({path: content.encode("utf-8") for path, content in output_files.items()})
    target_paths = [transcript_json_path, *output_files]
    target_existed = any(path.exists() for path in target_paths)
    changed = changed_files(files)
    if changed:
        atomic_write_many(changed)
    changed_outputs = [str(path) for path in target_paths if path in changed]
    status = "unchanged" if not changed else ("updated" if target_existed else "created")
    speakers = list(dict.fromkeys((item["speaker"] or "Unknown") for item in meeting["transcript"]))
    unknown_speakers = sum(
        1 for item in meeting["transcript"] if not item["speaker"] or item["speaker"].casefold() == "unknown"
    )
    result = {
        "status": status,
        "meeting_json": str(meeting_path),
        "summary_json": str(summary_path),
        "outputs": [str(path) for path in target_paths],
        "changed_outputs": changed_outputs,
        "transcript_segments": len(meeting["transcript"]),
        "speakers": speakers,
        "unknown_speakers": unknown_speakers,
        "action_items": summary.get("action_items", []) if isinstance(summary, dict) else [],
        "entity_count": len(summary.get("entities", [])) if isinstance(summary.get("entities", []), list) else 0,
        "link_count": len(summary.get("links", [])) if isinstance(summary.get("links", []), list) else 0,
        "warnings": [],
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="prepare current-agent summarization")
    prepare.add_argument("meeting")
    prepare.add_argument("--bundle")
    prepare.add_argument("--artifacts-dir")
    prepare.add_argument("--adapter")
    prepare.set_defaults(func=prepare_command)

    import_parser = subparsers.add_parser("import", help="summarize and render a meeting")
    import_parser.add_argument("meeting")
    import_parser.add_argument("--out", required=True)
    import_parser.add_argument("--bundle")
    summary_group = import_parser.add_mutually_exclusive_group(required=True)
    summary_group.add_argument("--summary")
    summary_group.add_argument("--api", action="store_true")
    import_parser.add_argument("--artifacts-dir")
    import_parser.add_argument("--engine")
    import_parser.add_argument("--transcript-template")
    import_parser.add_argument("--summary-template")
    import_parser.set_defaults(func=import_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except MeetingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
