#!/usr/bin/env python3
"""Fetch Screenpipe meetings and create meeting-transcript handoff JSON."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from export_for_meeting_transcript_skill import (
    ExportError,
    atomic_write_json,
    build_export,
    default_schema_path,
    load_schema,
    validate_export,
)


DEFAULT_API_BASE = "http://localhost:3030"
DEFAULT_DAYS = 1
PAGE_SIZE = 100


class FetchError(RuntimeError):
    pass


def api_key_from_env() -> str:
    value = os.environ.get("SCREENPIPE_LOCAL_API_KEY", "").strip()
    if value:
        return value
    raise FetchError(
        "SCREENPIPE_LOCAL_API_KEY is not set; obtain one with "
        "`bun x screenpipe@latest auth token` and export it in this shell"
    )


def request_json(api_base: str, path: str, api_key: str | None = None):
    url = f"{api_base.rstrip('/')}{path}"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read()
    except HTTPError as exc:
        if exc.code == 403:
            raise FetchError("Screenpipe rejected the API key with HTTP 403") from exc
        if exc.code == 404:
            raise FetchError(f"Screenpipe endpoint not found: {path}") from exc
        raise FetchError(f"Screenpipe request failed with HTTP {exc.code}: {path}") from exc
    except URLError as exc:
        raise FetchError(
            f"cannot connect to Screenpipe at {api_base}; ensure the desktop app is running"
        ) from exc
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchError(f"Screenpipe returned invalid JSON for {path}") from exc


def verify_health(api_base: str) -> None:
    health = request_json(api_base, "/health")
    if not isinstance(health, dict):
        raise FetchError("Screenpipe health endpoint returned an unexpected response")


def list_meetings(
    api_base: str,
    api_key: str,
    start_time: str,
    end_time: str,
    query: str,
) -> list[dict]:
    meetings = []
    offset = 0
    while True:
        params = {
            "start_time": start_time,
            "end_time": end_time,
            "limit": PAGE_SIZE,
            "offset": offset,
        }
        if query:
            params["q"] = query
        payload = request_json(api_base, f"/meetings?{urlencode(params)}", api_key)
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise FetchError("Screenpipe meetings endpoint returned an unexpected response")
        meetings.extend(payload)
        if len(payload) < PAGE_SIZE:
            return meetings
        offset += len(payload)


def get_meeting(api_base: str, api_key: str, meeting_id: int) -> tuple[dict, list[dict]]:
    meeting = request_json(api_base, f"/meetings/{meeting_id}", api_key)
    transcript = request_json(api_base, f"/meetings/{meeting_id}/transcript", api_key)
    if not isinstance(meeting, dict):
        raise FetchError(f"meeting {meeting_id} metadata has an unexpected shape")
    if not isinstance(transcript, list) or not all(isinstance(item, dict) for item in transcript):
        raise FetchError(f"meeting {meeting_id} transcript has an unexpected shape")
    return meeting, transcript


def default_range(now: datetime | None = None, days: int = DEFAULT_DAYS) -> tuple[str, str]:
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    return start.isoformat().replace("+00:00", "Z"), now.isoformat().replace("+00:00", "Z")


def display_title(meeting: dict) -> str:
    title = meeting.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    app = meeting.get("meeting_app")
    if isinstance(app, str) and app.strip():
        return f"{app.strip()} meeting"
    return "Untitled meeting"


def select_meetings(meetings: list[dict], select_all: bool, input_fn=input, output=sys.stderr) -> list[dict]:
    if not meetings:
        raise FetchError("no Screenpipe meetings matched the requested range or query")
    if select_all or len(meetings) == 1:
        return meetings

    print("Screenpipe meetings:", file=output)
    for index, meeting in enumerate(meetings, start=1):
        print(
            f"  {index}. [{meeting.get('id', '?')}] {meeting.get('meeting_start', '')} "
            f"{display_title(meeting)}",
            file=output,
        )
    try:
        answer = input_fn("Select meetings by number (comma-separated), or 'all': ").strip()
    except EOFError as exc:
        raise FetchError("meeting selection requires input; pass --all for bulk export") from exc
    if answer.casefold() == "all":
        return meetings

    indexes = []
    for part in answer.split(","):
        part = part.strip()
        if not part.isdigit():
            raise FetchError(f"invalid meeting selection: {answer!r}")
        index = int(part)
        if index < 1 or index > len(meetings):
            raise FetchError(f"meeting selection out of range: {index}")
        if index not in indexes:
            indexes.append(index)
    if not indexes:
        raise FetchError("no meetings selected")
    return [meetings[index - 1] for index in indexes]


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:60] or "meeting"


def output_stem(meeting: dict) -> str:
    started_at = meeting.get("meeting_start")
    date = started_at[:10] if isinstance(started_at, str) and len(started_at) >= 10 else "unknown-date"
    meeting_id = meeting.get("id")
    identifier = str(meeting_id) if isinstance(meeting_id, int) else "unknown-id"
    return f"{date}-{slugify(display_title(meeting))}-{identifier}"


def source_envelope(meeting: dict, transcript: list[dict], fetched_at: str | None = None) -> dict:
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": 1,
        "fetched_at": fetched_at,
        "meeting": meeting,
        "transcript": transcript,
    }


def export_meeting(
    meeting: dict,
    transcript: list[dict],
    out_dir: Path,
    schema: dict,
) -> tuple[Path, Path]:
    envelope = source_envelope(meeting, transcript)
    canonical = build_export(envelope)
    validate_export(canonical, schema)

    stem = output_stem(meeting)
    raw_path = out_dir / f"{stem}.screenpipe.json"
    canonical_path = out_dir / f"{stem}.meeting-transcript.json"
    atomic_write_json(raw_path, envelope)
    atomic_write_json(canonical_path, canonical)
    return raw_path, canonical_path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meeting-id", type=int, action="append", default=[], help="meeting ID; repeatable")
    parser.add_argument("--days", type=int, help="look back this many days; default: 1")
    parser.add_argument("--from", dest="from_time", help="range start accepted by Screenpipe")
    parser.add_argument("--to", dest="to_time", help="range end accepted by Screenpipe")
    parser.add_argument("--query", default="", help="case-insensitive title, attendee, note, or app filter")
    parser.add_argument("--all", action="store_true", help="export every meeting matching the range/query")
    parser.add_argument("--out", type=Path, default=Path(".screenpipe"), help="output directory")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="Screenpipe local API base URL")
    parser.add_argument("--schema", type=Path, default=default_schema_path(), help="canonical meeting schema")
    args = parser.parse_args(argv)

    if args.days is not None and args.days < 1:
        parser.error("--days must be at least 1")
    if bool(args.from_time) != bool(args.to_time):
        parser.error("--from and --to must be used together")
    if args.meeting_id and (args.days is not None or args.from_time or args.query or args.all):
        parser.error("--meeting-id cannot be combined with range, query, or --all options")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        api_key = api_key_from_env()
        verify_health(args.api_base)
        schema = load_schema(args.schema.expanduser().resolve())

        if args.meeting_id:
            selected_ids = list(dict.fromkeys(args.meeting_id))
        else:
            if args.from_time:
                start_time, end_time = args.from_time, args.to_time
            else:
                start_time, end_time = default_range(days=args.days or DEFAULT_DAYS)
            candidates = list_meetings(args.api_base, api_key, start_time, end_time, args.query)
            selected = select_meetings(candidates, args.all)
            selected_ids = []
            for meeting in selected:
                meeting_id = meeting.get("id")
                if not isinstance(meeting_id, int):
                    raise FetchError("Screenpipe meeting result is missing an integer id")
                selected_ids.append(meeting_id)

        out_dir = args.out.expanduser().resolve()
        for meeting_id in selected_ids:
            meeting, transcript = get_meeting(args.api_base, api_key, meeting_id)
            raw_path, canonical_path = export_meeting(meeting, transcript, out_dir, schema)
            print(raw_path)
            print(canonical_path)
    except (FetchError, ExportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
