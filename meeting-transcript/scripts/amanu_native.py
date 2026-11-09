"""Read-only native Amanu snapshot conversion; no source writes or idle inference.

The observed flat meta/structured transcript contract is intentionally strict.
An ended recording, complete valid JSON and matching producer completion record
establish a transcript snapshot, not that Amanu has released every writer.
Logs/markers never grant rename safety.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import os
import stat
from pathlib import Path
from datetime import datetime

import meeting_transcript as canonical

Error = canonical.MeetingError
INPUTS = ('meta.json', 'transcript.json', 'speakers.json', 'transcribe.log')
LOG_TIME = r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z'
DONE = re.compile(r'^(' + LOG_TIME + r') done — (\d+) segments$')
TRANSCRIBING = re.compile(r'^' + LOG_TIME + r' transcribing(?:\s|$)')


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def utc_time(value):
    if not isinstance(value, str) or not value.endswith('Z'):
        raise Error('native Amanu time requires UTC ISO-8601 Z')
    try:
        result = datetime.fromisoformat(value[:-1] + '+00:00')
    except ValueError as exc:
        raise Error('invalid native Amanu UTC time') from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise Error('native Amanu time requires explicit UTC time of day')
    return result


def read_regular(path):
    """Open once, nonblocking/no-follow, and verify the descriptor before reads."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise Error('native Amanu inputs must be regular files')
        with os.fdopen(descriptor, 'rb') as handle:
            descriptor = None
            return b''.join(iter(lambda: handle.read(1024 * 1024), b''))
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_inputs(package):
    values, bindings = {}, []
    for name in INPUTS:
        path = package / name
        try:
            raw = read_regular(path)
        except FileNotFoundError:
            raw = None
        bindings.append(dict(relative_path=name, sha256=checksum(raw) if raw is not None else ''))
        if raw is None:
            if name != 'speakers.json':
                raise Error('native Amanu required input unavailable')
            values[name] = None
            continue
        try:
            values[name] = raw.decode('utf-8') if name == 'transcribe.log' else json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise Error('invalid native Amanu JSON input') from exc
        if name == 'speakers.json' and not isinstance(values[name], dict):
            raise Error('invalid optional native Amanu speakers object')
    return values, bindings


def interval(meta):
    if not isinstance(meta, dict):
        raise Error('native Amanu metadata requires an object')
    start, end = utc_time(meta.get('started')), utc_time(meta.get('ended'))
    duration = meta.get('duration_seconds')
    if end <= start or type(duration) not in (int, float) or (type(duration) is float and not math.isfinite(duration)) or duration <= 0:
        raise Error('native Amanu requires ended recording and positive finite numeric duration_seconds')
    # Duration is validated as recorded, not recomputed/guessed from wall-clock times.
    return start, end


def completed_snapshot(log, transcript):
    """Observed producer completion, not a claim that source writers are idle.

    Archive/cleanup messages may follow done. Compare whole log records, never
    marker existence, and reject a later explicit transcription start. Log times
    have whole-second precision; JSON creation in that same second is allowed.
    """
    completed = None
    last_start = -1
    for index, line in enumerate(log.splitlines()):
        match = DONE.fullmatch(line)
        if match:
            completed = (index, utc_time(match[1]), int(match[2]))
        if TRANSCRIBING.match(line):
            last_start = index
    created = utc_time(transcript['created_at']).replace(microsecond=0)
    if completed is None or completed[0] < last_start or completed[1] < created or completed[2] != len(transcript['segments']):
        raise Error('native Amanu snapshot completion missing, stale, mismatched or followed by transcription restart')


def render(values, title):
    meta = values['meta.json']
    start, end = interval(meta)
    transcript = values['transcript.json']
    if not isinstance(transcript, dict):
        raise Error('native Amanu transcript requires an object')
    utc_time(transcript.get('created_at'))
    if not all(isinstance(transcript.get(k), str) and transcript[k] for k in ('engine', 'model')):
        raise Error('native Amanu transcript requires engine/model strings')
    segments = transcript.get('segments')
    if not isinstance(segments, list) or not segments:
        raise Error('native Amanu transcript requires nonempty complete segments')
    completed_snapshot(values['transcribe.log'], transcript)
    speakers = values['speakers.json']
    names = {}
    if speakers is not None:
        if not isinstance(speakers, dict) or not isinstance(speakers.get('speakers'), dict):
            raise Error('invalid optional native Amanu speakers mapping')
        utc_time(speakers.get('created_at'))
        for speaker_id, info in speakers['speakers'].items():
            if isinstance(info, str):
                names[speaker_id] = info or speaker_id
            elif isinstance(info, dict) and isinstance(info.get('name'), str) and isinstance(info.get('source'), str):
                names[speaker_id] = info['name'] or speaker_id
            else:
                raise Error('invalid optional native Amanu speaker entry')
    entries = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise Error('invalid native Amanu segment')
        first, last = segment.get('start_ms'), segment.get('end_ms')
        if type(first) is not int or type(last) is not int or first < 0 or last < first:
            raise Error('native Amanu segment requires nonnegative integer millisecond bounds')
        speaker, text = segment.get('speaker'), segment.get('text')
        if not isinstance(speaker, str) or not speaker or not isinstance(text, str) or not text:
            raise Error('native Amanu segment requires nonempty speaker/text strings')
        label = names.get(speaker, speaker)
        if any(character in label for character in ':\r\n'):
            raise Error('native Amanu speaker label cannot be represented in canonical Markdown')
        # Canonical turns collapse consecutive chunks only when their resolved
        # source labels are exactly equal. Never casefold or strip identities.
        if entries and entries[-1]['speaker'] == label:
            entries[-1]['text'] += '\n' + text
        else:
            entries.append(dict(speaker=label, text=text))
    content = canonical.render_transcript(dict(title=title, source='amanu', started_at=meta['started'], ended_at=meta['ended'], participants=[dict(name=name) for name in dict.fromkeys(e['speaker'] for e in entries)], transcript=entries), preserve_entries=True)
    parsed = canonical.parse_transcript(content)
    # Fail closed if Markdown-like speech becomes a turn delimiter, or canonical
    # parsing would discard any text (including a source utterance's final LF).
    if parsed['transcript'] != entries:
        raise Error('native Amanu utterance cannot be represented losslessly in canonical Markdown')
    return content.encode('utf-8')


def prepare(package, title, expected_inputs=None):
    values, bindings = read_inputs(Path(package))
    if expected_inputs is not None and bindings != expected_inputs:
        raise Error('native Amanu raw inputs changed; regenerate preview')
    output = render(values, title)
    return output, bindings
