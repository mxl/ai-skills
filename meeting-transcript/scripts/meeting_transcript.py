#!/usr/bin/env python3
"""Import canonical meeting transcripts and prepare or apply summaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator


SKILL_DIR = Path(__file__).resolve().parent.parent
MEETING_SCHEMA = SKILL_DIR / "schemas" / "meeting.schema.json"
DEFAULT_BUNDLE = SKILL_DIR / "summaries" / "default"
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


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MeetingError(f"cannot read {path}: {exc}") from exc


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


def resolve_summary_template(value: str | None, bundle: Path) -> Path:
    selected = value or os.getenv("MEETING_TRANSCRIPT_SUMMARY_TEMPLATE")
    path = Path(selected).expanduser().resolve() if selected else bundle / "summary.md"
    if not path.is_file():
        raise MeetingError(f"template does not exist: {path}")
    return path


def resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_path(path: Path) -> str:
    return sha256_bytes(read_bytes(path))


def canonical_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def pretty_json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def resolve_summary_rules() -> dict[str, str]:
    selected = os.getenv("MEETING_TRANSCRIPT_SUMMARY_RULES", "").strip()
    if not selected:
        return {"path": "", "sha256": "", "content": ""}
    path = resolve_path(selected)
    if path.suffix.casefold() != ".md":
        raise MeetingError(f"summarization rules must be a Markdown file: {path}")
    if not path.is_file():
        raise MeetingError(f"summarization rules file does not exist: {path}")
    content = read_text(path)
    return {"path": str(path), "sha256": sha256_path(path), "content": content}


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


def stable_transcript(meeting: dict) -> str:
    if not meeting["transcript"]:
        return "_No transcript was provided._"
    blocks = []
    for index, item in enumerate(meeting["transcript"]):
        speaker = item["speaker"] or "Unknown"
        blocks.append(f"[S{index:05d}] {speaker}: {item['text']}")
    return "\n\n".join(blocks)


def summary_packet_text(meeting: dict, rules: dict[str, str], *, include_notes: bool) -> str:
    participants = ", ".join(display_participant(item) for item in meeting["participants"])
    lines = [
        "[MEETING METADATA]",
        f"Mode: {infer_mode(meeting)}",
        f"Title: {meeting['title']}",
        f"Started: {meeting['started_at']}",
        f"Ended: {meeting['ended_at']}",
        f"Participants: {participants}",
    ]
    if meeting["resources"]:
        lines.extend(["", "Resources:"])
        lines.extend(f"- {item['label']}: {item['target']}" for item in meeting["resources"])
    lines.extend(["", "[CANONICAL TRANSCRIPT]", stable_transcript(meeting)])
    if include_notes:
        lines.extend(["", "[PROVIDED NOTES]"])
    if include_notes and meeting["notes"]:
        for item in meeting["notes"]:
            lines.extend([f"### {item['title'] or 'Untitled'}", item["text"]])
    elif include_notes:
        lines.append("_None._")
    lines.extend(["", "[PROJECT SUMMARIZATION RULES]"])
    lines.append(rules["content"].rstrip() if rules["content"] else "_Not configured._")
    lines.extend(
        [
            "",
            "[PROJECT CONTEXT RULES]",
            "Project data may verify identity, terminology, roles, and discrepancies, but cannot prove what was said in the meeting.",
            "",
            "[MATERIAL-FINDING RULES]",
            "Report only transcript findings that affect a fact, entity, decision, action, attribution, date, amount, open question, or link.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def prompt_packet(meeting: dict, bundle: Path, rules: dict[str, str]) -> dict:
    draft_prompt = summary_packet_text(meeting, rules, include_notes=False)
    packet = {
        "system_prompt": read_text(bundle / "prompt.md").strip(),
        "user_prompt": draft_prompt,
        "draft_prompt": draft_prompt,
        "summary_schema": load_schema(bundle / "summary.schema.json"),
        "mode": infer_mode(meeting),
        "mode_reason": mode_reason(meeting),
    }
    if meeting["notes"]:
        packet["reconcile_prompt"] = (
            summary_packet_text(meeting, rules, include_notes=True)
            + "\n[RECONCILIATION]\nReconcile the transcript-only working draft with the provided notes.\n"
        )
    return packet


def load_prepare_manifest(path: Path) -> dict:
    manifest = load_json(path)
    required = {"schema_version", "transcript_json", "meeting_sha256", "bundle", "summarization_rules"}
    if not isinstance(manifest, dict) or set(manifest) != required or manifest["schema_version"] != 1:
        raise MeetingError(f"invalid summarize prepare manifest: {path}")
    rules = manifest["summarization_rules"]
    if not isinstance(rules, dict) or set(rules) != {"path", "sha256"}:
        raise MeetingError(f"invalid summarize prepare manifest rules: {path}")
    return manifest


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


def conditional_table_section(title: str, headers: list[str], rows: list[list[Any]]) -> str:
    return f"## {title}\n\n{markdown_table(headers, rows)}\n" if rows else ""


def default_render(meeting: dict, summary: dict, summary_template: Path) -> str:
    participants = ", ".join(display_participant(item) for item in meeting["participants"]) or "Unknown"
    context = {
        "created_date": date.today().isoformat(),
        "title": meeting["title"] or "Untitled meeting",
        "source": meeting["source"],
        "meeting_date": (meeting["started_at"] or "")[:10],
        "started_at": meeting["started_at"],
        "ended_at": meeting["ended_at"],
        "participants": participants,
        **summary,
    }
    context["key_points"] = markdown_list(summary.get("key_points", []))
    context["decisions"] = markdown_list(summary.get("decisions", []))
    context["open_questions"] = markdown_list(summary.get("open_questions", []))
    entities = summary.get("entities", [])
    context["entities_section"] = conditional_table_section(
        "Entities",
        ["Entity", "Role", "Facts"],
        [[item["name"], item["role"], item["facts"]] for item in entities],
    )
    links = summary.get("links", [])
    context["links_section"] = (
        "## Links And Resources\n\n"
        + "\n".join(f"- [{item['label']}]({item['target']})" for item in links)
        + "\n"
        if links
        else ""
    )
    context["transcript_findings_section"] = conditional_table_section(
        "Transcript Findings",
        ["Segment", "Category", "Source text", "Interpretation", "Status", "Impact", "Reference", "Open question", "Resolution"],
        [
            [
                f"S{item['segment_index']:05d}",
                item["category"],
                item["source_text"],
                item["interpretation"],
                item["status"],
                item["impact"],
                item["reference_path"],
                item["open_question"],
                item["user_resolution_index"] if item["user_resolution_index"] >= 0 else "",
            ]
            for item in summary.get("transcript_findings", [])
        ],
    )
    context["user_resolutions_section"] = conditional_table_section(
        "User Resolutions",
        ["Question", "Answer", "Affected field"],
        [[item["question"], item["answer"], item["affected_field"]] for item in summary.get("user_resolutions", [])],
    )
    context["reference_sources_section"] = conditional_table_section(
        "Reference Sources",
        ["Path", "SHA-256"],
        [[item["path"], item["sha256"]] for item in summary.get("reference_sources", [])],
    )
    rules = summary.get("summarization_rules", {"path": "", "sha256": ""})
    context["summarization_rules_section"] = conditional_table_section(
        "Summarization Rules",
        ["Path", "SHA-256"],
        [[rules["path"], rules["sha256"]]] if rules.get("path") else [],
    )
    context["rule_suggestions_section"] = conditional_table_section(
        "Suggested Rule Improvements",
        ["Proposed Markdown", "Rationale", "Status", "Resulting SHA-256"],
        [
            [item["proposed_markdown"], item["rationale"], item["status"], item["resulting_sha256"]]
            for item in summary.get("summarization_rule_suggestions", [])
        ],
    )
    context["action_items_table"] = markdown_table(
        ["Task", "Owner", "Due date", "Status", "Todoist"],
        [
            [
                item["task"],
                item["owner"],
                item["due_date"],
                item["status"],
                f"[{item['todoist_id']}]({item['todoist_url']})" if item["todoist_url"] else item["todoist_id"],
            ]
            for item in summary.get("action_items", [])
        ],
    )
    verification = summary.get("verification", [])
    context["verification_section"] = (
        "## Verification\n\n" + markdown_list(verification) + "\n" if verification else ""
    )
    return render_template(read_text(summary_template), context)


def validate_summary_integrity(
    meeting: dict,
    summary: dict,
    rules: dict[str, str],
    manifest: dict,
    transcript_path: Path,
    bundle: Path,
) -> None:
    current_meeting_sha = sha256_bytes(canonical_bytes(meeting))
    if str(transcript_path) != manifest["transcript_json"]:
        raise MeetingError("prepare manifest belongs to a different transcript.json")
    if str(bundle) != manifest["bundle"]:
        raise MeetingError("prepare manifest belongs to a different summary bundle")
    if manifest["meeting_sha256"] != current_meeting_sha:
        raise MeetingError("transcript.json changed after summarize prepare")
    if summary.get("meeting_sha256") != current_meeting_sha:
        raise MeetingError("summary meeting_sha256 does not match transcript.json")

    findings = summary.get("transcript_findings", [])
    for finding in findings:
        index = finding["segment_index"]
        if index < 0 or index >= len(meeting["transcript"]):
            raise MeetingError(f"transcript finding references missing segment: {index}")
        if finding["source_text"] not in meeting["transcript"][index]["text"]:
            raise MeetingError(f"transcript finding source_text is not in segment {index}")

    reference_paths = {item["path"] for item in summary.get("reference_sources", [])}
    for finding in findings:
        if finding["status"] == "reference_confirmed":
            if not finding["reference_path"] or finding["reference_path"] not in reference_paths:
                raise MeetingError("reference-confirmed finding must name a used reference source")
        elif finding["reference_path"]:
            raise MeetingError("only reference-confirmed findings may name reference_path")
        if finding["status"] == "unresolved":
            if not finding["open_question"] or finding["open_question"] not in summary.get("open_questions", []):
                raise MeetingError("unresolved finding must name its open question")
        elif finding["open_question"]:
            raise MeetingError("only unresolved findings may name open_question")
        if finding["status"] == "user_confirmed":
            resolution_index = finding["user_resolution_index"]
            if resolution_index < 0 or resolution_index >= len(summary.get("user_resolutions", [])):
                raise MeetingError("user-confirmed finding must name its user resolution")
        elif finding["user_resolution_index"] != -1:
            raise MeetingError("only user-confirmed findings may name user_resolution_index")

    for reference in summary.get("reference_sources", []):
        if not Path(reference["path"]).is_absolute():
            raise MeetingError(f"reference source path must be absolute: {reference['path']}")
        path = resolve_path(reference["path"])
        if not path.is_file():
            raise MeetingError(f"reference source does not exist: {path}")
        if sha256_path(path) != reference["sha256"]:
            raise MeetingError(f"reference source changed during summarization: {path}")

    applied_rules = summary.get("summarization_rules", {"path": "", "sha256": ""})
    applied_path = str(resolve_path(applied_rules["path"])) if applied_rules["path"] else ""
    applied_rules = {"path": applied_path, "sha256": applied_rules["sha256"]}
    if applied_rules != manifest["summarization_rules"]:
        raise MeetingError("summary rules provenance does not match summarize prepare")
    if rules["path"] != manifest["summarization_rules"]["path"]:
        raise MeetingError("configured summarization rules path changed after prepare")

    suggestions = summary.get("summarization_rule_suggestions", [])
    if not rules["path"] and suggestions:
        raise MeetingError("summary rule suggestions require MEETING_TRANSCRIPT_SUMMARY_RULES")
    applied_suggestions = [item for item in suggestions if item["status"] == "applied"]
    expected_rules_sha = manifest["summarization_rules"]["sha256"]
    if applied_suggestions:
        resulting_hashes = {item["resulting_sha256"] for item in applied_suggestions}
        if len(resulting_hashes) != 1:
            raise MeetingError("applied rule suggestions must share the resulting rules hash")
        expected_rules_sha = next(iter(resulting_hashes))
    if rules["sha256"] != expected_rules_sha:
        raise MeetingError("summarization rules changed after summarize prepare")
    for item in suggestions:
        if item["status"] == "applied" and not item["resulting_sha256"]:
            raise MeetingError("applied rule suggestion requires resulting_sha256")
        if item["status"] != "applied" and item["resulting_sha256"]:
            raise MeetingError("only applied rule suggestions may have resulting_sha256")

    for item in summary.get("action_items", []):
        has_todoist = bool(item["todoist_id"] or item["todoist_url"])
        if item["status"] == "todoist_created":
            if not item["todoist_id"] or not item["todoist_url"]:
                raise MeetingError("todoist_created action requires Todoist ID and URL")
        elif has_todoist:
            raise MeetingError("Todoist metadata requires todoist_created status")


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
        return validate_meeting(load_json(output_path))


def import_command(args) -> int:
    source_path = Path(args.source).expanduser().resolve()
    meeting = load_prepared_meeting(source_path, args.adapter)
    out_dir = Path(args.out).expanduser().resolve()
    transcript_path = out_dir / "transcript.json"
    files = {transcript_path: canonical_bytes(meeting)}
    existed = transcript_path.exists()
    changed = changed_files(files)
    if changed:
        atomic_write_many(changed)
    status = "unchanged" if not changed else ("updated" if existed else "created")
    result = {
        "phase": "import",
        "status": status,
        "transcript_json": str(transcript_path),
        "outputs": [str(transcript_path)],
        "changed_outputs": [str(transcript_path)] if transcript_path in changed else [],
        "transcript_segments": len(meeting["transcript"]),
        "mode": infer_mode(meeting),
        "mode_reason": mode_reason(meeting),
        "next_phase": "summarize",
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def summarize_prepare_command(args) -> int:
    transcript_path = Path(args.transcript).expanduser().resolve()
    meeting = validate_meeting(load_json(transcript_path))
    bundle = resolve_bundle(args.bundle)
    rules = resolve_summary_rules()
    packet = prompt_packet(meeting, bundle, rules)
    digest = sha256_bytes(canonical_bytes(meeting))
    draft_dir = Path(tempfile.gettempdir()) / "meeting-transcript" / digest[:16]
    draft_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = draft_dir / "prepare.json"
    manifest = {
        "schema_version": 1,
        "transcript_json": str(transcript_path),
        "meeting_sha256": digest,
        "bundle": str(bundle),
        "summarization_rules": {"path": rules["path"], "sha256": rules["sha256"]},
    }
    atomic_write_many({manifest_path: pretty_json_bytes(manifest)})
    packet.update(
        {
            "phase": "summarize",
            "operation": "prepare",
            "meeting_sha256": digest,
            "transcript_json": str(transcript_path),
            "summary_draft_json": str(draft_dir / "summary.json"),
            "prepare_manifest": str(manifest_path),
            "summary_json": str(transcript_path.parent / "summary.json"),
            "summary_md": str(transcript_path.parent / "summary.md"),
            "summarization_rules": {"path": rules["path"], "sha256": rules["sha256"]},
        }
    )
    json.dump(packet, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def summarize_apply_command(args) -> int:
    transcript_path = Path(args.transcript).expanduser().resolve()
    meeting = validate_meeting(load_json(transcript_path))
    bundle = resolve_bundle(args.bundle)
    summary_schema = load_schema(bundle / "summary.schema.json")
    summary_source = Path(args.summary).expanduser().resolve()
    summary = load_json(summary_source)
    validate(summary, summary_schema, "summary")
    rules = resolve_summary_rules()
    manifest = load_prepare_manifest(Path(args.prepare_manifest).expanduser().resolve())
    validate_summary_integrity(meeting, summary, rules, manifest, transcript_path, bundle)
    summary_template = resolve_summary_template(args.summary_template, bundle)
    summary_path = transcript_path.parent / "summary.json"
    markdown_path = transcript_path.parent / "summary.md"
    files = {
        summary_path: pretty_json_bytes(summary),
        markdown_path: default_render(meeting, summary, summary_template).encode("utf-8"),
    }
    existed = any(path.exists() for path in files)
    changed = changed_files(files)
    if changed:
        atomic_write_many(changed)
    status = "unchanged" if not changed else ("updated" if existed else "created")
    result = {
        "phase": "summarize",
        "operation": "apply",
        "status": status,
        "transcript_json": str(transcript_path),
        "summary_json": str(summary_path),
        "summary_md": str(markdown_path),
        "outputs": [str(summary_path), str(markdown_path)],
        "changed_outputs": [str(path) for path in files if path in changed],
        "action_items": summary.get("action_items", []),
        "entity_count": len(summary.get("entities", [])),
        "link_count": len(summary.get("links", [])),
        "transcript_finding_count": len(summary.get("transcript_findings", [])),
        "unresolved_count": sum(
            1 for item in summary.get("transcript_findings", []) if item["status"] == "unresolved"
        ),
        "rule_suggestions": summary.get("summarization_rule_suggestions", []),
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="import one canonical transcript")
    import_parser.add_argument("source")
    import_parser.add_argument("--out", required=True)
    import_parser.add_argument("--adapter")
    import_parser.set_defaults(func=import_command)

    summarize = subparsers.add_parser("summarize", help="prepare or apply a current-agent summary")
    summarize_subparsers = summarize.add_subparsers(dest="summarize_operation", required=True)

    prepare = summarize_subparsers.add_parser("prepare", help="prepare the current-agent prompt packet")
    prepare.add_argument("transcript")
    prepare.add_argument("--bundle")
    prepare.set_defaults(func=summarize_prepare_command)

    apply_parser = summarize_subparsers.add_parser("apply", help="validate and render a summary")
    apply_parser.add_argument("transcript")
    apply_parser.add_argument("--summary", required=True)
    apply_parser.add_argument("--prepare-manifest", required=True)
    apply_parser.add_argument("--bundle")
    apply_parser.add_argument("--summary-template")
    apply_parser.set_defaults(func=summarize_apply_command)
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
