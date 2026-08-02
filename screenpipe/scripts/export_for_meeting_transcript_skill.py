#!/usr/bin/env python3
"""Convert fetched Screenpipe meeting JSON to meeting-transcript's schema."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import tempfile

from jsonschema import Draft202012Validator


SUPPORTED_SCHEMA_VERSION = 1
SCHEMA_RELATIVE_PATH = Path("meeting-transcript/schemas/meeting.schema.json")


class ExportError(ValueError):
    pass


def _string(value) -> str:
    return value if isinstance(value, str) else ""


def _person(value) -> dict[str, str] | None:
    if isinstance(value, dict):
        name = _string(value.get("name"))
        email = _string(value.get("email"))
        if name or email:
            return {"name": name, "email": email}
        return None
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    match = re.fullmatch(r"(.*?)\s*<([^<>\s]+@[^<>\s]+)>\s*", text)
    if match:
        return {"name": match.group(1).strip(), "email": match.group(2)}
    if re.fullmatch(r"[^\s@]+@[^\s@]+", text):
        return {"name": "", "email": text}
    return {"name": text, "email": ""}


def participants(value) -> list[dict[str, str]]:
    parsed = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = [part for part in re.split(r"[;\n]+", text) if part.strip()]

    if isinstance(parsed, dict):
        values = [parsed]
    elif isinstance(parsed, list):
        values = parsed
    else:
        values = [parsed]

    result = []
    seen = set()
    for value in values:
        person = _person(value)
        if person is None:
            continue
        key = person["email"].casefold() or person["name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(person)
    return result


def build_export(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ExportError("Screenpipe input must be a JSON object")
    if raw.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise ExportError(
            f"unsupported Screenpipe schema_version {raw.get('schema_version')!r}; "
            f"expected {SUPPORTED_SCHEMA_VERSION}"
        )

    meeting = raw.get("meeting") if isinstance(raw.get("meeting"), dict) else {}
    transcript_input = raw.get("transcript") if isinstance(raw.get("transcript"), list) else []

    transcript = []
    for segment in transcript_input:
        if not isinstance(segment, dict):
            continue
        transcript.append(
            {
                "started_at": _string(segment.get("capturedAt")),
                "ended_at": "",
                "speaker": _string(segment.get("speakerName")),
                "text": _string(segment.get("transcript")),
            }
        )

    notes = []
    note = _string(meeting.get("note"))
    if note.strip():
        notes.append({"title": "Screenpipe note", "text": note})

    if not transcript and not notes:
        raise ExportError("meeting has neither transcript entries nor a non-empty note")

    title = _string(meeting.get("title"))
    meeting_app = _string(meeting.get("meeting_app"))
    if not title and meeting_app:
        title = f"{meeting_app} meeting"

    resources = []
    meeting_id = meeting.get("id")
    if isinstance(meeting_id, int):
        resources.append(
            {"label": "Screenpipe meeting", "target": f"screenpipe://meeting/{meeting_id}"}
        )

    return {
        "schema_version": 1,
        "raw": copy.deepcopy(raw),
        "source": "screenpipe",
        "title": title,
        "started_at": _string(meeting.get("meeting_start")),
        "ended_at": _string(meeting.get("meeting_end")),
        "participants": participants(meeting.get("attendees")),
        "transcript": transcript,
        "notes": notes,
        "resources": resources,
    }


def default_schema_path() -> Path:
    screenpipe_dir = Path(__file__).resolve().parent.parent
    return screenpipe_dir.parent / SCHEMA_RELATIVE_PATH


def load_schema(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ExportError(f"meeting schema not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ExportError(f"invalid JSON schema {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ExportError(f"meeting schema must be a JSON object: {path}")
    try:
        Draft202012Validator.check_schema(value)
    except Exception as exc:
        raise ExportError(f"invalid JSON schema {path}: {exc}") from exc
    return value


def validate_export(payload: dict, schema: dict) -> None:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.absolute_path))
    if errors:
        lines = []
        for error in errors:
            location = ".".join(str(part) for part in error.absolute_path) or "<root>"
            lines.append(f"{location}: {error.message}")
        raise ExportError("output failed meeting schema validation:\n  " + "\n  ".join(lines))


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(temp_name).replace(path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def canonical_stem(path: Path) -> str:
    stem = path.stem
    return stem[:-len(".screenpipe")] if stem.endswith(".screenpipe") else stem


def output_paths(inputs: list[Path], out: Path | None, out_dir: Path | None) -> list[Path]:
    if out is not None and out_dir is not None:
        raise ExportError("--out and --out-dir cannot be used together")
    if out is not None and len(inputs) != 1:
        raise ExportError("--out requires exactly one input file")
    if out is not None:
        outputs = [out]
    elif out_dir is not None:
        outputs = [out_dir / f"{canonical_stem(path)}.meeting-transcript.json" for path in inputs]
    else:
        outputs = [path.with_name(f"{canonical_stem(path)}.meeting-transcript.json") for path in inputs]
    resolved = [path.expanduser().resolve() for path in outputs]
    if len(set(resolved)) != len(resolved):
        raise ExportError("multiple inputs resolve to the same output path; rename inputs or use separate output directories")
    return outputs


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="fetched Screenpipe JSON file(s)")
    parser.add_argument("--out", type=Path, help="output file; valid with one input only")
    parser.add_argument("--out-dir", type=Path, help="output directory for one or more inputs")
    parser.add_argument("--schema", type=Path, default=default_schema_path(), help="canonical meeting schema")
    args = parser.parse_args(argv)
    try:
        args.outputs = output_paths(args.inputs, args.out, args.out_dir)
    except ExportError as exc:
        parser.error(str(exc))
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        schema = load_schema(args.schema.expanduser().resolve())
        for input_path, output_path in zip(args.inputs, args.outputs):
            try:
                raw = json.loads(input_path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise ExportError(f"Screenpipe input not found: {input_path}") from exc
            except json.JSONDecodeError as exc:
                raise ExportError(f"invalid Screenpipe JSON {input_path}: {exc}") from exc
            payload = build_export(raw)
            validate_export(payload, schema)
            atomic_write_json(output_path.expanduser().resolve(), payload)
            print(output_path)
    except ExportError as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
