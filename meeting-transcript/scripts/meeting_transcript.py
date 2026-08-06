#!/usr/bin/env python3
"""Import canonical meeting Markdown and prepare direct Markdown summaries."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent
TRANSCRIPT_TEMPLATE = SKILL_DIR / "templates" / "meeting-transcript.md"
DEFAULT_BUNDLE = SKILL_DIR / "summaries" / "default"
TRANSCRIPT_RE = re.compile(
    r"\A---\n(?P<frontmatter>.*?)\n---\n\n"
    r"# Transcript: (?P<title>[^\n]+)\n\n"
    r"## Metadata\n\n"
    r"- Meeting date: (?P<meeting_date>[^\n]*)\n"
    r"- Started: (?P<started_at>[^\n]*)\n"
    r"- Ended: (?P<ended_at>[^\n]*)\n"
    r"- Participants: (?P<participants>[^\n]*)\n\n"
    r"## Transcript\n\n(?P<body>.*)\Z",
    re.DOTALL,
)
TURN_RE = re.compile(r"(?ms)(?P<speaker>[^:\n]+): (?P<text>.+?)(?=\n\n(?=[^:\n]+: )|\Z)")


class MeetingError(Exception):
    pass


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


def resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_path(path: Path) -> str:
    return sha256_bytes(read_bytes(path))


def resolve_summary_rules() -> dict[str, str]:
    selected = os.getenv("MEETING_TRANSCRIPT_SUMMARY_RULES", "").strip()
    if not selected:
        return {"path": "", "sha256": "", "content": ""}
    path = resolve_path(selected)
    if path.suffix.casefold() != ".md":
        raise MeetingError(f"summarization rules must be a Markdown file: {path}")
    if not path.is_file():
        raise MeetingError(f"summarization rules file does not exist: {path}")
    return {"path": str(path), "sha256": sha256_path(path), "content": read_text(path)}


def resolve_bundle(value: str | None) -> Path:
    selected = value or os.getenv("MEETING_TRANSCRIPT_SUMMARY_BUNDLE")
    bundle = Path(selected).expanduser().resolve() if selected else DEFAULT_BUNDLE
    required = (bundle / "prompt.md", bundle / "summary.md")
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise MeetingError(f"invalid summary bundle; missing: {', '.join(missing)}")
    return bundle


def display_participant(participant: dict) -> str:
    name = str(participant.get("name", "")).strip()
    email = str(participant.get("email", "")).strip()
    if name and email:
        return f"{name} <{email}>"
    return name or email or "Unknown"


def normalize_speaker(value: object) -> str:
    return str(value or "Unknown").replace("\r", " ").replace("\n", " ").strip() or "Unknown"


def collapse_turns(entries: list[dict]) -> list[dict]:
    turns = []
    for item in entries:
        speaker = normalize_speaker(item.get("speaker"))
        text = str(item.get("text") or "")
        if turns and turns[-1]["speaker"].casefold() == speaker.casefold():
            turns[-1]["text"] += "\n" + text
        else:
            turns.append({"speaker": speaker, "text": text})
    return turns


def render_transcript(meeting: dict) -> str:
    """Render in-memory source data with the current transcript template."""
    title = str(meeting.get("title") or "Untitled meeting").replace("\n", " ").strip()
    source = str(meeting.get("source") or "unknown").replace("\n", " ").strip()
    started_at = str(meeting.get("started_at") or "")
    ended_at = str(meeting.get("ended_at") or "")
    participants = ", ".join(display_participant(item) for item in meeting.get("participants", [])) or "Unknown"
    turns = collapse_turns(meeting.get("transcript", []))
    if not turns:
        raise MeetingError("meeting must contain transcript entries")
    values = {
        "created_date": started_at[:10] or date.today().isoformat(),
        "source": source,
        "title": title,
        "meeting_date": started_at[:10] or "Unknown",
        "started_at": started_at or "Unknown",
        "ended_at": ended_at or "Unknown",
        "participants": participants,
        "transcript_body": "\n\n".join(f"{item['speaker']}: {item['text']}" for item in turns),
    }
    rendered = read_text(TRANSCRIPT_TEMPLATE)
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    unresolved = re.findall(r"{{\s*[^}]+\s*}}", rendered)
    if unresolved:
        raise MeetingError(f"unresolved transcript template placeholders: {', '.join(unresolved)}")
    return rendered.rstrip() + "\n"


def parse_transcript(content: str) -> dict:
    normalized = content.replace("\r\n", "\n")
    match = TRANSCRIPT_RE.fullmatch(normalized.rstrip("\n"))
    if not match:
        raise MeetingError("transcript does not match templates/meeting-transcript.md")
    frontmatter = match.group("frontmatter")
    if not re.search(r"(?m)^type: source$", frontmatter):
        raise MeetingError("transcript frontmatter must contain type: source")
    source_match = re.search(r"(?m)^source:\n  - (?P<source>[^\n]+)$", frontmatter)
    if not source_match:
        raise MeetingError("transcript frontmatter must contain one source value")
    body = match.group("body")
    entries = []
    position = 0
    for turn in TURN_RE.finditer(body):
        if body[position:turn.start()].strip():
            raise MeetingError("invalid transcript entry; expected '<speaker>: <speech>'")
        entries.append({"speaker": turn.group("speaker"), "text": turn.group("text")})
        position = turn.end()
    if body[position:].strip():
        raise MeetingError("invalid transcript entry; expected '<speaker>: <speech>'")
    if not entries:
        raise MeetingError("transcript must contain at least one entry")
    return {
        "source": source_match.group("source"),
        "title": match.group("title"),
        "meeting_date": match.group("meeting_date"),
        "started_at": match.group("started_at"),
        "ended_at": match.group("ended_at"),
        "participants": match.group("participants"),
        "transcript": entries,
    }


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def import_command(args) -> int:
    source_path = Path(args.source).expanduser().resolve()
    content = read_text(source_path)
    meeting = parse_transcript(content)
    destination = Path(args.out).expanduser().resolve() / "transcript.md"
    output = (content.rstrip() + "\n").encode("utf-8")
    existed = destination.exists()
    changed = not existed or read_bytes(destination) != output
    if changed:
        atomic_write(destination, output)
    status = "unchanged" if not changed else ("updated" if existed else "created")
    print("phase: import")
    print(f"status: {status}")
    print(f"transcript_md: {destination}")
    print(f"changed: {'yes' if changed else 'no'}")
    print(f"transcript_entries: {len(meeting['transcript'])}")
    print("next_phase: summarize")
    return 0


def summary_prompt(meeting: dict, rules: dict[str, str]) -> str:
    transcript = "\n\n".join(
        f"{item['speaker']}: {item['text']}" for item in meeting["transcript"]
    )
    lines = [
        "[MEETING METADATA]",
        f"Title: {meeting['title']}",
        f"Started: {meeting['started_at']}",
        f"Ended: {meeting['ended_at']}",
        f"Participants: {meeting['participants']}",
        "",
        "[CANONICAL TRANSCRIPT]",
        transcript,
        "",
        "[PROJECT SUMMARIZATION RULES]",
        rules["content"].rstrip() if rules["content"] else "_Not configured._",
        "",
        "[PROJECT CONTEXT RULES]",
        "Project data may verify identity, terminology, roles, and discrepancies, but cannot prove what was said in the meeting.",
        "",
        "[MATERIAL-FINDING RULES]",
        "Report only findings that affect a fact, entity, decision, action, attribution, date, amount, open question, or link.",
        "Use short verbatim quotations with speaker attribution; do not invent or print segment IDs.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def summarize_prepare_command(args) -> int:
    transcript_path = Path(args.transcript).expanduser().resolve()
    meeting = parse_transcript(read_text(transcript_path))
    bundle = resolve_bundle(args.bundle)
    rules = resolve_summary_rules()
    print("MEETING TRANSCRIPT SUMMARY PREPARE")
    print(f"Transcript: {transcript_path}")
    print(f"Summary: {transcript_path.parent / 'summary.md'}")
    print(f"Transcript SHA-256: {sha256_path(transcript_path)}")
    print(f"Summarization rules: {rules['path'] or '(not configured)'}")
    print(f"Summarization rules SHA-256: {rules['sha256'] or '(none)'}")
    print("\n=== SYSTEM PROMPT ===\n")
    print(read_text(bundle / "prompt.md").strip())
    print("\n=== SUMMARY TEMPLATE ===\n")
    print(read_text(bundle / "summary.md").strip())
    print("\n=== USER PROMPT ===\n")
    print(summary_prompt(meeting, rules).rstrip())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="import one canonical transcript.md")
    import_parser.add_argument("source")
    import_parser.add_argument("--out", required=True)
    import_parser.set_defaults(func=import_command)

    summarize = subparsers.add_parser("summarize", help="prepare a current-agent Markdown summary")
    summarize_subparsers = summarize.add_subparsers(dest="summarize_operation", required=True)
    prepare = summarize_subparsers.add_parser("prepare", help="prepare the current-agent prompt")
    prepare.add_argument("transcript")
    prepare.add_argument("--bundle")
    prepare.set_defaults(func=summarize_prepare_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except MeetingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
