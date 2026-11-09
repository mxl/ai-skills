"""On-demand, fail-closed calendar import orchestration (stdlib, Python 3.11+).

Supports normalized metadata and read-only native Amanu JSON snapshots.
Native transcript readiness does not establish rename safety. No authentication lives here.
Persisted workflow documents are Markdown containing a versioned TOML block.
"""
from __future__ import annotations

import hashlib
import fcntl
import copy
import contextlib
import json
import secrets
import os
import math
import subprocess
from pathlib import Path
import re
import stat
import sys
from datetime import datetime, timezone, timedelta
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import meeting_transcript as canonical
import amanu_native as native

Error = canonical.MeetingError
NAME_FORMAT = 'YYYY-MM-DD HH-mm title'


def digest(value):
    return hashlib.sha256(value).hexdigest()


def file_digest(path):
    """Hash recordings in fixed chunks, never materializing multi-GB media in RAM."""
    checksum = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise Error('source hashing requires a regular file')
        with os.fdopen(descriptor, 'rb') as handle:
            descriptor = None
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                checksum.update(chunk)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return checksum.hexdigest()


def key(*values):
    return digest(json.dumps(values, ensure_ascii=False, separators=(',', ':')).encode())


def aware(value):
    if not isinstance(value, str):
        raise Error('time must be an ISO-8601 string with an explicit timezone')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise Error(f'invalid aware time: {value}') from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise Error(f'time requires explicit timezone: {value}')
    return result


def stamp(value):
    return aware(value).isoformat()


def quote(value):
    return json.dumps(value, ensure_ascii=False)


def toml_dump(data):
    """Small schema-independent TOML writer; workflow values are strings/bools/lists."""
    lines = []

    def scalar(value):
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (str, int)):
            return quote(value)
        if isinstance(value, list):
            return '[' + ', '.join(scalar(item) for item in value) + ']'
        raise Error('unsupported workflow value')

    def table(values, prefix):
        for name, value in values.items():
            if not isinstance(value, dict) and not (isinstance(value, list) and value and isinstance(value[0], dict)):
                lines.append(f'{quote(name)} = {scalar(value)}')
        for name, value in values.items():
            path = prefix + [name]
            heading = '.'.join(quote(item) for item in path)
            if isinstance(value, dict):
                lines.extend(['', '[' + heading + ']'])
                table(value, path)
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                for item in value:
                    lines.extend(['', '[[' + heading + ']]'])
                    table(item, path)
    table(data, [])
    return '\n'.join(lines) + '\n'


def markdown(data, title):
    return f'# {title}\n\n```toml\n' + toml_dump(data) + '```\n'


def parse_markdown(content, label):
    text = content.decode('utf-8')
    match = re.fullmatch(r'# [^\n]+\n\n```toml\n(.*)```\n', text, re.S)
    if not match:
        raise Error(f'invalid workflow Markdown: {label}')
    data = tomllib.loads(match[1])
    if type(data.get('version')) is not int or data.get('version') != 1:
        raise Error('unsupported workflow version')
    return data


def load_markdown(path):
    return parse_markdown(Path(path).read_bytes(), str(path))


def config_file(path, allowed):
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding='utf-8'))
    if type(data.get('version')) is not int or data.get('version') != 1:
        raise Error(f'unsupported configuration version: {path}')
    if set(data) - allowed - {'version'}:
        raise Error(f'unknown configuration keys (credentials are prohibited): {path}')
    return data


def checked_table(value, allowed, label):
    if not isinstance(value, dict):
        raise Error(f'{label} must be a TOML table/object')
    if set(value) - allowed:
        raise Error(f'unknown {label} keys (credentials are prohibited)')
    return value


def normalize_config(path, *, global_scope):
    """Accept the approved nested schema and non-contradictory legacy flat aliases."""
    flat_keys = {'provider', 'calendars', 'timezone', 'destination', 'sources'}
    allowed = flat_keys | {'calendar', 'import'}
    if global_scope:
        allowed |= {'naming', 'name_format'}
    raw = config_file(path, allowed)
    result = {name: raw[name] for name in flat_keys if name in raw}

    def alias(name, value):
        if name in result and result[name] != value:
            raise Error(f'contradictory mixed configuration values for {name}: {path}')
        result[name] = value

    if 'calendar' in raw:
        calendar = checked_table(raw['calendar'], {'provider', 'calendar_ids', 'timezone'}, 'calendar')
        for name, value in calendar.items():
            alias('calendars' if name == 'calendar_ids' else name, value)
    if 'import' in raw:
        imports = checked_table(raw['import'], {'destination', 'sources'}, 'import')
        for name, value in imports.items():
            # Global sources is the catalog, not a selection alias.
            if global_scope and name == 'sources':
                raise Error('import.sources selection belongs in project configuration')
            alias(name, value)
    for name in ('provider', 'timezone', 'destination'):
        if name in result and (not isinstance(result[name], str) or not result[name]):
            raise Error(f'{name} must be a nonempty string: {path}')
    if 'calendars' in result and (not isinstance(result['calendars'], list) or not result['calendars'] or not all(isinstance(v, str) and v for v in result['calendars'])):
        raise Error(f'calendar_ids/calendars must be a nonempty list of identifier strings: {path}')
    if global_scope:
        if 'name_format' in raw and raw['name_format'] != NAME_FORMAT:
            raise Error(f'only supported name_format: {NAME_FORMAT}')
        if 'naming' in raw:
            naming = checked_table(raw['naming'], {'format', 'date_format', 'time_format'}, 'naming')
            for name, expected in {'format': '{date} {time} {title}', 'date_format': '%Y-%m-%d', 'time_format': '%H-%M'}.items():
                if name in naming and naming[name] != expected:
                    raise Error(f'only supported naming.{name}: {expected}')
        catalog = result.get('sources', {})
        if not isinstance(catalog, dict):
            raise Error('global sources must be a table of named source tables')
        for name, source in catalog.items():
            source = checked_table(source, {'type', 'path'}, f'sources.{name}')
            if set(source) != {'type', 'path'} or not all(isinstance(source[k], str) and source[k] for k in ('type', 'path')):
                raise Error('sources require nonempty type and path strings')
    elif 'sources' in result and (not isinstance(result['sources'], list) or not all(isinstance(v, str) and v for v in result['sources'])):
        raise Error('import.sources/sources must be a list of configured source-name strings')
    return result


def contained(project, value):
    path = Path(value)
    if path.is_absolute() or '..' in path.parts:
        raise Error('destination must be a project-relative path without ..')
    result = (project / path).resolve()
    if not result.is_relative_to(project) or result == project or result.is_relative_to(project / '.meeting-transcript'):
        raise Error('destination must stay inside project, outside settings')
    return result


def configuration(args):
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        raise Error('project must exist')
    global_path = Path(args.global_config).expanduser().resolve() if args.global_config else Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'meeting-transcript' / 'config.toml'
    global_data = normalize_config(global_path, global_scope=True)
    local_path = project / '.meeting-transcript' / 'config.toml'
    local = normalize_config(local_path, global_scope=False)
    values = {**global_data, **local}
    for field, arg in [('provider', args.provider), ('calendars', args.calendar_id), ('timezone', args.timezone), ('destination', args.destination)]:
        if arg is not None:
            values[field] = arg
    for required in ('provider', 'calendars', 'timezone', 'destination'):
        if not values.get(required):
            raise Error(f'setup required: choose {required}')
    if not isinstance(values['provider'], str) or not isinstance(values['calendars'], list) or not all(isinstance(v, str) and v for v in values['calendars']):
        raise Error('provider/calendars must be explicit nonempty identifiers')
    try:
        ZoneInfo(values['timezone'])
    except (ZoneInfoNotFoundError, TypeError) as exc:
        raise Error('setup required: choose a valid IANA timezone') from exc
    if global_data.get('name_format', NAME_FORMAT) != NAME_FORMAT:
        raise Error(f'only supported name_format: {NAME_FORMAT}')
    sources = []
    if args.source:
        for item in args.source:
            kind, sep, value = item.partition('=')
            if not sep:
                raise Error('--source expects TYPE=absolute-path')
            sources.append({'name': kind, 'type': kind, 'path': value})
    else:
        catalog = global_data.get('sources', {})
        selected = local.get('sources', list(catalog))
        if not isinstance(selected, list):
            raise Error('project sources must be a list of configured names')
        for name in selected:
            if name not in catalog:
                raise Error(f'unknown configured source: {name}')
            source = catalog[name]
            if set(source) != {'type', 'path'}:
                raise Error('sources require only type and path; credentials are prohibited')
            sources.append({'name': name, **source})
    for source in sources:
        p = Path(source['path']).expanduser()
        if source['type'] not in ('obs', 'amanu') or not p.is_absolute() or not p.is_dir() or p.is_symlink():
            raise Error('sources must be existing absolute OBS/Amanu directories, not symlinks')
        source['path'] = str(p.resolve())
    return dict(project=str(project), provider=values['provider'], calendars=values['calendars'], timezone=values['timezone'], destination=str(contained(project, values['destination'])), sources=sources,
                config_files=[dict(path=str(p), sha256=digest(p.read_bytes()) if p.exists() else '') for p in (global_path, local_path)])


def calendar_events(path, config):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict) or type(data.get('version')) is not int or data.get('version') != 1 or data.get('provider') != config['provider'] or not isinstance(data.get('events'), list):
        raise Error('calendar input requires version=1, configured provider and events list')
    events = {}
    for raw in data['events']:
        if not isinstance(raw, dict) or not all(isinstance(raw.get(k), str) and raw[k] for k in ('calendar_id', 'event_id', 'title', 'start', 'end')):
            raise Error('calendar event needs calendar_id, event_id, title, aware start/end')
        if raw['calendar_id'] not in config['calendars']:
            continue
        recurrence = raw.get('recurrence_id', '')
        if not isinstance(recurrence, str):
            raise Error('recurrence_id must be a stable provider instance identifier string')
        if raw.get('recurring') and not recurrence:
            raise Error('recurring event requires stable recurrence_id; clarify provider identity')
        start, end = aware(raw['start']), aware(raw['end'])
        if end <= start:
            raise Error('calendar event end must be after start')
        event = {k: raw[k] for k in ('calendar_id', 'event_id', 'title')}
        event.update(recurrence_id=recurrence, start=start.isoformat(), end=end.isoformat())
        identity = key(config['provider'], raw['calendar_id'], raw['event_id'], recurrence)
        if identity in events:
            raise Error('duplicate stable calendar event identity')
        event['key'] = identity
        events[identity] = event
    return events


def relevant_calendar(events, period_start, period_end, known_keys):
    return [event for identity, event in sorted(events.items()) if identity in known_keys or (aware(event['end']) >= aware(period_start) and aware(event['start']) <= aware(period_end))]


def safe_name(event, zone):
    title = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', '_', event['title'])
    # Leave room for the planned date/time and OBS extension within common 255-byte limits.
    title = title.strip(' .').encode('utf-8')[:200].decode('utf-8', errors='ignore').rstrip(' .') or 'Untitled meeting'
    return aware(event['start']).astimezone(ZoneInfo(zone)).strftime('%Y-%m-%d %H-%M ') + title


def artifact_snapshot(path, verified=False):
    if path.is_symlink():
        raise Error('symlink artifacts are not supported')
    stat = path.stat()
    result = dict(device=stat.st_dev, inode=stat.st_ino, size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    if verified:
        files = sorted(path.rglob('*')) if path.is_dir() else [path]
        values = []
        for child in files:
            if child.is_symlink():
                raise Error('symlink within source package')
            if child.is_file():
                values.append((str(child.relative_to(path)) if path.is_dir() else '', file_digest(child)))
        result['content_sha256'] = key(*values)
    return result


def container_interval(path):
    """Read container-level timing only; never decode frames or infer recorder idle.

    Disable report files and network/other input protocols. Missing ffprobe,
    malformed/unsupported containers and bounded timeouts are ordinary pending
    discovery, not a failure of the entire batch. Do not expose tool stderr.
    """
    env = {name: value for name, value in os.environ.items() if name != 'FFREPORT'}
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:format_tags=creation_time', '-of', 'json', '-protocol_whitelist', 'file', '-i', str(path)], shell=False, capture_output=True, timeout=10, env=env)
        if result.returncode:
            return None
        data = json.loads(result.stdout)
        info = data['format']
        raw_duration = info['duration']
        if type(raw_duration) not in (str, int, float):
            return None
        duration = float(raw_duration)
        if not math.isfinite(duration) or duration <= 0:
            return None
        start = aware(info['tags']['creation_time'])
        return start, start + timedelta(seconds=duration)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError, OverflowError, Error):
        return None


def discover(config, events, state):
    artifacts = []
    associated = [a for meeting in state['meetings'] for a in meeting.get('artifacts', [])]
    for source in config['sources']:
        root = Path(source['path'])
        for path in sorted(root.iterdir()):
            if path.is_symlink() or (source['type'] == 'obs' and (not path.is_file() or path.suffix.casefold() not in ('.mkv', '.mp4', '.mov', '.avi', '.webm'))) or (source['type'] == 'amanu' and not path.is_dir()):
                continue
            artifact = dict(id=key(source['type'], str(root), path.stat().st_dev, path.stat().st_ino), source=source['name'], type=source['type'], original_path=str(path), path=str(path), status='unverified', warning='native metadata schema unverified; transcript readiness and rename safety unknown', event_key='', candidates=[], evidence=[], transcripts=[], safe=False)
            prior = next((a for a in associated if a['snapshot']['device'] == path.stat().st_dev and a['snapshot']['inode'] == path.stat().st_ino and a['path'] == str(path)), None)
            if prior:
                artifact.update(id=prior['id'], original_path=prior['original_path'])
            meta_path = Path(prior['metadata_path']) if prior else (path / 'meeting-import.toml' if path.is_dir() else path.with_name(path.name + '.meeting.toml'))
            is_native = source['type'] == 'amanu' and (meta_path.name == 'meta.json' or (not meta_path.exists() and (path / 'meta.json').exists()))
            if is_native:
                meta_path = path / 'meta.json'
            artifact['metadata_path'] = str(meta_path)
            artifact['metadata_sha256'] = ''
            try:
                if is_native:
                    values, bindings = native.read_inputs(path)
                    start, end = native.interval(values['meta.json'])
                    # Validate complete structured JSON in memory. No canonical
                    # sidecar is ever written back into the source package.
                    native.render(values, 'Amanu snapshot')
                    artifact['snapshot_evidence'] = 'ended UTC metadata + complete structured JSON + matching timestamp/count completion record; no subsequent transcription start'
                    artifact.update(status='verified', warning='native immutable transcript snapshot; recorder/transcriber idle is not established', started_at=start.isoformat(), ended_at=end.isoformat(), processing='unknown', metadata_sha256=bindings[0]['sha256'], format='native_amanu_json')
                    for event_key, event in events.items():
                        if start < aware(event['end']) and end > aware(event['start']):
                            artifact['candidates'].append(event_key)
                            artifact['evidence'].append(event_key + ': native recorded interval intersects event; app/title is not event identity; explicit choice required')
                    transcript_path = path / 'transcript.json'
                    artifact['transcripts'] = [dict(id=key(str(transcript_path)), path=str(transcript_path), sha256=bindings[1]['sha256'], relative_path='transcript.json', format='native_amanu_json', inputs=bindings)]
                elif meta_path.exists():
                    metadata_bytes = meta_path.read_bytes()
                    meta = tomllib.loads(metadata_bytes.decode('utf-8'))
                    artifact['metadata_sha256'] = digest(metadata_bytes)
                    if meta.get('schema') != 'meeting-transcript-artifact-v1':
                        raise Error('unsupported metadata schema')
                    for field in ('title', 'processing', 'transcript'):
                        if field in meta and not isinstance(meta[field], str):
                            raise Error(f'metadata {field} must be a string')
                    if 'event' in meta:
                        identity = checked_table(meta['event'], {'calendar_id', 'event_id', 'recurrence_id'}, 'metadata event')
                        if set(identity) != {'calendar_id', 'event_id', 'recurrence_id'} or not all(isinstance(v, str) for v in identity.values()) or not identity['calendar_id'] or not identity['event_id']:
                            raise Error('metadata event requires string calendar_id, event_id and recurrence_id')
                    start, end = aware(meta['started_at']), aware(meta['ended_at'])
                    if end <= start:
                        raise Error('recording interval end must be after start')
                    artifact.update(status='verified', warning='', started_at=start.isoformat(), ended_at=end.isoformat(), processing=meta.get('processing', 'unknown'))
                    artifact['safe'] = meta.get('processing') == 'idle'
                    identity = meta.get('event', {})
                    for event_key, event in events.items():
                        overlap = start < aware(event['end']) and end > aware(event['start'])
                        title_match = meta.get('title', '').casefold() == event['title'].casefold()
                        exact_id = identity == {k: event[k] for k in ('calendar_id', 'event_id', 'recurrence_id')}
                        if exact_id or (not identity and overlap and title_match):
                            artifact['candidates'].append(event_key)
                            artifact['evidence'].append(event_key + ': ' + ('verified explicit event identity' if exact_id else 'exact metadata title + intersecting aware recording interval'))
                    transcript = meta.get('transcript', '')
                    if transcript and meta.get('processing') == 'idle':
                        rel = Path(transcript)
                        if not path.is_dir() or rel.is_absolute() or '..' in rel.parts:
                            raise Error('transcript must be a package-relative path')
                        transcript_path = path / rel
                        if transcript_path.is_symlink() or not transcript_path.resolve().is_relative_to(path.resolve()):
                            raise Error('unsafe transcript path')
                        content = transcript_path.read_text(encoding='utf-8')
                        canonical.parse_transcript(content)
                        artifact['transcripts'] = [dict(id=key(str(transcript_path)), path=str(transcript_path), sha256=digest(transcript_path.read_bytes()), relative_path=transcript)]
                # Filename title alone is informative, never sufficient for automatic association.
                else:
                    timing = container_interval(path) if source['type'] == 'obs' else None
                    if timing:
                        start, end = timing
                        artifact.update(status='verified', warning='container timing only; recorder/transcriber idle is not established', started_at=start.isoformat(), ended_at=end.isoformat(), processing='unknown', format='native_obs_container')
                        for event_key, event in events.items():
                            if start < aware(event['end']) and end > aware(event['start']):
                                artifact['candidates'].append(event_key)
                                artifact['evidence'].append(event_key + ': container creation_time + duration intersects event; explicit choice required')
                    for event_key, event in events.items():
                        if event['title'].casefold() in path.stem.casefold():
                            if event_key not in artifact['candidates']:
                                artifact['candidates'].append(event_key)
                            artifact['evidence'].append(event_key + ': filename title only; explicit choice required')
                artifact['snapshot'] = artifact_snapshot(path, artifact['status'] == 'verified')
            except (OSError, ValueError, KeyError, TypeError, Error) as exc:
                artifact.update(status='unverified', safe=False, transcripts=[], event_key='', warning=f'metadata/transcript unavailable or invalid: {exc}')
                artifact['snapshot'] = artifact_snapshot(path)
            if artifact['status'] == 'verified' and not artifact.get('format', '').startswith('native_') and len(artifact['candidates']) == 1:
                artifact['event_key'] = artifact['candidates'][0]
            if prior and not artifact['event_key'] and prior['event_key'] in events and artifact['snapshot'] == prior['snapshot'] and artifact['metadata_sha256'] == prior['metadata_sha256']:
                artifact['event_key'] = prior['event_key']
                artifact['evidence'].append('persisted event association and unchanged filesystem/content identity')
            artifacts.append(artifact)
    return artifacts


def require_fields(record, fields, label):
    if not isinstance(record, dict):
        raise Error(f'invalid {label}: expected table/object')
    for field, expected in fields.items():
        value = record.get(field)
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise Error(f'invalid {label}.{field}: expected {expected.__name__}')


def validate_event_shape(event, label):
    require_fields(event, {k: str for k in ('key', 'calendar_id', 'event_id', 'recurrence_id', 'title', 'start', 'end')}, label)
    if aware(event['end']) <= aware(event['start']):
        raise Error(f'invalid {label}: reversed event interval')


def validate_artifact_shape(artifact, label):
    require_fields(artifact, {**{k: str for k in ('id', 'source', 'type', 'original_path', 'path', 'status', 'metadata_path', 'metadata_sha256', 'event_key')}, 'snapshot': dict, 'transcripts': list, 'safe': bool}, label)
    require_fields(artifact['snapshot'], {k: int for k in ('device', 'inode', 'size', 'mtime_ns')}, label + '.snapshot')
    for transcript in artifact['transcripts']:
        require_fields(transcript, {k: str for k in ('id', 'path', 'sha256', 'relative_path')}, label + '.transcript')


def validate_state_shape(state):
    require_fields(state, dict(generation=int, initial_start=str, period_end=str, meetings=list), 'state')
    if state['generation'] < 0:
        raise Error('invalid state.generation: must not be negative')
    for field in ('initial_start', 'period_end'):
        if state[field]:
            aware(state[field])
    for meeting in state['meetings']:
        require_fields(meeting, dict(event=dict, status=str, reason=str, imported_path=str, artifacts=list), 'state.meeting')
        validate_event_shape(meeting['event'], 'state.meeting.event')
        for artifact in meeting['artifacts']:
            validate_artifact_shape(artifact, 'state.meeting.artifact')


def validate_plan_shape(plan):
    require_fields(plan, dict(kind=str, config=dict, state_generation=int, state_sha256=str, recovery_journal_sha256=str, period_start=str, period_end=str, initial_start=str, calendar_snapshot=list, calendar_known_keys=list, meetings=list, artifacts=list), 'plan')
    config = plan['config']
    require_fields(config, dict(project=str, provider=str, calendars=list, timezone=str, destination=str, sources=list, config_files=list), 'plan.config')
    for source in config['sources']:
        require_fields(source, dict(name=str, type=str, path=str), 'plan.config.source')
    for item in config['config_files']:
        require_fields(item, dict(path=str, sha256=str), 'plan.config.config_file')
    for event in plan['calendar_snapshot']:
        validate_event_shape(event, 'plan.calendar_snapshot.event')
    if not all(isinstance(k, str) for k in plan['calendar_known_keys'] + config['calendars']):
        raise Error('invalid plan calendar identity list')
    for meeting in plan['meetings']:
        require_fields(meeting, dict(event=dict, status=str, reason=str, artifact_ids=list, transcripts=list, selected_transcript=str, imported_path=str, previous_event=dict, accept_update=bool, selected=bool), 'plan.meeting')
        validate_event_shape(meeting['event'], 'plan.meeting.event')
        for transcript in meeting['transcripts']:
            require_fields(transcript, {k: str for k in ('id', 'path', 'sha256', 'relative_path')}, 'plan.meeting.transcript')
    for artifact in plan['artifacts']:
        validate_artifact_shape(artifact, 'plan.artifact')
        require_fields(artifact, dict(proposed_path=str, rename_requested=bool, idle_attested=bool, collision=bool, rename_blocked=str), 'plan.artifact')


def validate_journal_shape(journal):
    require_fields(journal, dict(kind=str, plan_sha256=str, allowed_state_hashes=list, operations=list, plan=dict), 'journal')
    if not all(isinstance(v, str) for v in journal['allowed_state_hashes']):
        raise Error('invalid journal.allowed_state_hashes: expected strings')
    validate_plan_shape(journal['plan'])
    for operation in journal['operations']:
        require_fields(operation, dict(kind=str, destination=str, event_key=str), 'journal.operation')
        if operation['kind'] == 'import':
            require_fields(operation, dict(sha256=str), 'journal.import')
        elif operation['kind'] == 'rename':
            require_fields(operation, dict(source=str, artifact_id=str, snapshot=dict), 'journal.rename')
        else:
            raise Error('invalid journal.operation.kind')


def read_state(project):
    path = Path(project) / '.meeting-transcript' / 'state.md'
    state = load_markdown(path) if path.exists() else dict(version=1, generation=0, initial_start='', period_end='', meetings=[])
    validate_state_shape(state)
    return state


def pairs(values, label):
    result = {}
    for item in values:
        left, sep, right = item.partition('=')
        if not sep or not left or not right or left in result:
            raise Error(f'{label} expects unique ID=ID choices')
        result[left] = right
    return result


def preview(args):
    # Capture end before configuration/discovery to keep the batch fixed.
    captured_now = datetime.now(timezone.utc)
    period_end = stamp(args.until) if args.until else captured_now.isoformat()
    if aware(period_end) > captured_now:
        raise Error('--until cannot be in the future; unfinished meetings must remain deferred')
    config = configuration(args)
    state = read_state(config['project'])
    actual_state_sha256 = state_hash(state)
    journal_path = Path(config['project']) / '.meeting-transcript' / 'journal.md'
    recovery_journal_sha256 = ''
    if journal_path.exists():
        journal_bytes = journal_path.read_bytes()
        recovery_journal_sha256 = digest(journal_bytes)
        _, state = reconcile_journal(parse_markdown(journal_bytes, str(journal_path)), state)
    period_start = state['period_end'] or args.since
    if not period_start:
        raise Error('setup required: choose first-period --since with an explicit timezone')
    period_start = stamp(period_start)
    if aware(period_end) <= aware(period_start):
        raise Error('period end must be after period start')
    events = calendar_events(args.calendar, config)
    missing_known = [m['event']['key'] for m in state['meetings'] if m['status'] != 'declined' and m['event']['key'] not in events]
    if missing_known:
        raise Error('calendar input omits known event identities; fetch known events as well as the new period: ' + ', '.join(missing_known))
    artifacts = discover(config, events, state)
    matches = pairs(args.match, '--match')
    transcript_choices = pairs(args.transcript, '--transcript')
    by_id = {a['id']: a for a in artifacts}
    if set(matches) - set(by_id) or set(args.rename + args.idle_source) - set(by_id):
        raise Error('unknown artifact choice; regenerate preview')
    for artifact_id, event_key in matches.items():
        if event_key not in events:
            raise Error('unknown event choice')
        by_id[artifact_id]['event_key'] = event_key
        by_id[artifact_id]['evidence'].append('explicit operator match choice')
    # Bind the exact candidate output only after its actual event context is
    # known. Raw-input signatures remain independent of this derived digest.
    for artifact in artifacts:
        event = events.get(artifact['event_key'])
        if not event:
            continue
        for transcript in artifact['transcripts']:
            if transcript.get('format') == 'native_amanu_json':
                output, _ = native.prepare(Path(transcript['path']).parent, event['title'], transcript['inputs'])
                transcript['title'] = event['title']
            else:
                output = canonical.prepare_import(Path(transcript['path']))[1]
            transcript['content_sha256'] = digest(output)
    known = {m['event']['key']: m for m in state['meetings']}
    meetings = []
    for event_key, event in events.items():
        prior = known.get(event_key)
        if prior and prior['status'] == 'declined':
            continue
        if not prior and not (aware(event['end']) >= aware(period_start) and aware(event['start']) <= aware(period_end)):
            continue
        matched = [a for a in artifacts if a['event_key'] == event_key]
        transcripts = [t for a in sorted(matched, key=lambda a: a['type'] != 'amanu') for t in a['transcripts']]
        unique = {}
        for transcript in transcripts:
            unique.setdefault(transcript.get('content_sha256', transcript['sha256']), transcript)
        selected = next(iter(unique.values()))['id'] if len(unique) == 1 else ''
        if event_key in transcript_choices:
            selected = transcript_choices[event_key]
            if selected not in [t['id'] for t in transcripts]:
                raise Error('transcript choice is not a ready candidate for this event')
        status = 'ready' if selected else 'pending'
        reason = '' if selected else ('choose competing transcripts' if len(unique) > 1 else 'recording/transcript missing or unverified; offer transcription separately')
        if not matched and any(event_key in a['candidates'] and not a['event_key'] for a in artifacts):
            reason = 'ambiguous or insufficient matching evidence; choose --match artifact-id=event-key explicitly'
        if aware(event['end']) > aware(period_end):
            status, reason = 'deferred', 'calendar event has not ended at captured period end'
        if prior and prior.get('imported_path'):
            status, reason = 'imported', ''
        if event_key in args.decline:
            status, reason = 'declined', 'explicit operator decline'
        proposal = bool(prior and prior['event'] != event and prior.get('imported_path'))
        if proposal:
            status, reason = 'calendar-update', 'calendar title/time changed; no automatic source or transcript changes'
        meetings.append(dict(event=event, status=status, reason=reason, artifact_ids=[a['id'] for a in matched], transcripts=transcripts, selected_transcript=selected, imported_path=prior.get('imported_path', '') if prior else '', previous_event=prior['event'] if proposal else {}, accept_update=event_key in args.accept_update, selected=args.select is None or event_key in args.select))
    valid_events = {m['event']['key'] for m in meetings}
    if set(args.decline + (args.select or []) + args.accept_update + list(transcript_choices)) - valid_events:
        raise Error('unknown/suppressed event choice')
    for artifact in artifacts:
        event = events.get(artifact['event_key'])
        if not event and len(artifact['candidates']) == 1:
            event = events[artifact['candidates'][0]]
        proposed = str(Path(artifact['path']).with_name(safe_name(event, config['timezone']) + (Path(artifact['path']).suffix if artifact['type'] == 'obs' else ''))) if event else ''
        artifact['proposed_path'] = proposed
        artifact['rename_requested'] = artifact['id'] in args.rename
        artifact['idle_attested'] = artifact['id'] in args.idle_source
        artifact['collision'] = bool(proposed and proposed != artifact['path'] and Path(proposed).exists())
        meeting = next((m for m in meetings if m['event']['key'] == artifact['event_key']), None)
        blocked = ''
        if not meeting or not meeting['selected'] or meeting['status'] in ('declined', 'deferred'):
            blocked = 'meeting unresolved, deferred, declined or unselected'
        elif meeting['status'] == 'calendar-update' and not meeting['accept_update']:
            blocked = 'calendar update requires explicit --accept-update'
        elif not artifact['safe'] or not artifact['idle_attested']:
            blocked = 'requires verified idle metadata AND explicit operator --idle-source attestation; timestamps/markers alone are insufficient'
        elif aware(artifact['ended_at']) > aware(period_end):
            blocked = 'recording has not ended at captured period end'
        elif artifact['collision']:
            blocked = 'destination exists; no overwrite or automatic suffix'
        artifact['rename_blocked'] = blocked
    calendar_known_keys = [m['event']['key'] for m in state['meetings'] if m['status'] != 'declined']
    calendar_snapshot = relevant_calendar(events, period_start, period_end, calendar_known_keys)
    plan = dict(version=1, kind='meeting-calendar-plan', calendar_snapshot=calendar_snapshot, calendar_known_keys=calendar_known_keys, config=config, state_generation=state['generation'], state_sha256=actual_state_sha256, recovery_journal_sha256=recovery_journal_sha256, period_start=period_start, period_end=period_end, initial_start=state['initial_start'] or period_start, meetings=meetings, artifacts=artifacts)
    print(markdown(plan, 'Meeting calendar preview'), end='')
    return 0


def state_hash(state):
    return digest(markdown(state, 'Meeting calendar state').encode())


_CHECKPOINTS_SEEN = set()


def checkpoint(name):
    """Deterministic subprocess race seam, opt-in only and never a permission grant."""
    if os.environ.get('MEETING_TRANSCRIPT_TEST_CHECKPOINT') == name and name not in _CHECKPOINTS_SEEN:
        _CHECKPOINTS_SEEN.add(name)
        print(f'test: checkpoint {name}', flush=True)
        sys.stdin.readline()


def failpoint(name):
    # Explicit opt-in fault injection for subprocess behavioral tests; never grants permissions.
    if os.environ.get('MEETING_TRANSCRIPT_TEST_FAILPOINT') == name:
        os._exit(86)


def save_journal(path, journal):
    canonical.atomic_write(path, markdown(journal, 'Meeting calendar recovery journal').encode())


def save_state(project, state, journal_path, journal):
    checksum = state_hash(state)
    if checksum not in journal['allowed_state_hashes']:
        journal['allowed_state_hashes'].append(checksum)
    save_journal(journal_path, journal)
    failpoint('before-state')
    canonical.atomic_write(Path(project) / '.meeting-transcript' / 'state.md', markdown(state, 'Meeting calendar state').encode())
    failpoint('after-state')


def relocate_artifact(artifact, destination):
    source = Path(artifact['path'])
    artifact['path'] = str(destination)
    if artifact['type'] == 'amanu':
        artifact['metadata_path'] = str(destination / Path(artifact['metadata_path']).relative_to(source))
        for transcript in artifact['transcripts']:
            transcript['path'] = str(destination / transcript['relative_path'])
    artifact['snapshot'] = artifact_snapshot(destination, True)


def reconcile_journal(journal, state):
    """Read-only recovery: recognize only journaled exact content/filesystem identities."""
    validate_journal_shape(journal)
    validate_state_shape(state)
    if journal.get('kind') != 'meeting-calendar-journal' or state_hash(state) not in journal['allowed_state_hashes']:
        raise Error('state differs from recoverable journal; inspect manually, no blind retry')
    plan = copy.deepcopy(journal['plan'])
    recovered = copy.deepcopy(state)
    imported = {}
    for operation in journal['operations']:
        destination = Path(operation['destination'])
        if operation['kind'] == 'import':
            if destination.exists():
                if destination.is_symlink() or digest(destination.read_bytes()) != operation['sha256']:
                    raise Error('journaled canonical destination changed; manual reconciliation required')
                imported[operation['event_key']] = str(destination)
        else:
            source = Path(operation['source'])
            artifact = next(a for a in plan['artifacts'] if a['id'] == operation['artifact_id'])
            if source.exists():
                if artifact_snapshot(source, True) != operation['snapshot']:
                    raise Error('journaled source changed; regenerate only after manual reconciliation')
            elif destination.exists() and artifact_snapshot(destination, True) == operation['snapshot']:
                relocate_artifact(artifact, destination)
            else:
                raise Error('cannot prove journaled rename outcome; manual reconciliation required')
    records = {m['event']['key']: m for m in recovered['meetings']}
    for meeting in plan['meetings']:
        event_key = meeting['event']['key']
        artifacts = [a for a in plan['artifacts'] if a['id'] in meeting['artifact_ids']]
        meeting['transcripts'] = [t for a in artifacts for t in a['transcripts']]
        if event_key in imported:
            meeting.update(status='imported', imported_path=imported[event_key])
            records[event_key] = dict(event=meeting['event'], status='imported', reason='', imported_path=imported[event_key], artifacts=artifacts)
        elif event_key in records:
            records[event_key]['artifacts'] = artifacts
        elif any(op['kind'] == 'rename' and op['event_key'] == event_key for op in journal['operations']):
            records[event_key] = dict(event=meeting['event'], status='pending', reason='interrupted import; source association recovered from journal', imported_path='', artifacts=artifacts)
    recovered['meetings'] = list(records.values())
    return plan, recovered


def validate_plan(plan, calendar_path):
    if plan.get('kind') != 'meeting-calendar-plan':
        raise Error('not a calendar preview plan')
    config = plan['config']
    project = Path(config['project'])
    destination = Path(config['destination'])
    if not destination.is_relative_to(project) or destination == project or destination.is_relative_to(project / '.meeting-transcript') or destination.resolve() != destination:
        raise Error('unsafe destination; regenerate preview')
    for record in config['config_files']:
        path = Path(record['path'])
        current = digest(path.read_bytes()) if path.exists() else ''
        if current != record['sha256']:
            raise Error('configuration changed; regenerate preview')
    fresh = calendar_events(calendar_path, config)
    if relevant_calendar(fresh, plan['period_start'], plan['period_end'], plan['calendar_known_keys']) != plan['calendar_snapshot']:
        raise Error('calendar changed (relevant events added, deleted or edited); regenerate preview')
    for meeting in plan['meetings']:
        if fresh.get(meeting['event']['key']) != meeting['event']:
            raise Error('calendar changed or known event missing; fetch it and regenerate preview')
    for source in config['sources']:
        root = Path(source['path'])
        if not root.is_dir() or root.resolve() != root or root.is_symlink():
            raise Error('source root changed or became a symlink; regenerate preview')
    for artifact in plan['artifacts']:
        path = Path(artifact['path'])
        roots = [Path(source['path']) for source in config['sources'] if source['name'] == artifact['source'] and source['type'] == artifact['type']]
        if path.resolve() != path or path.parent not in roots:
            raise Error('source must remain a top-level artifact of the configured root')
        if artifact_snapshot(path, artifact['status'] == 'verified') != artifact['snapshot']:
            raise Error(f'source changed; regenerate preview: {path}')
        metadata = Path(artifact['metadata_path'])
        if artifact['status'] == 'verified' and artifact.get('format') != 'native_amanu_json' and (file_digest(metadata) if metadata.exists() else '') != artifact['metadata_sha256']:
            raise Error('metadata changed; regenerate preview')
        for transcript in artifact['transcripts']:
            prepare_snapshot(transcript)
    for meeting in plan['meetings']:
        for transcript in meeting['transcripts']:
            prepare_snapshot(transcript, meeting['event']['title'])


def prepare_snapshot(transcript, title=None):
    source = Path(transcript['path'])
    if transcript.get('format') == 'native_amanu_json':
        if title is not None and (transcript.get('title') != title or not transcript.get('content_sha256')):
            raise Error('native derived context changed; regenerate preview')
        output, _ = native.prepare(source.parent, transcript.get('title', 'Amanu snapshot'), transcript['inputs'])
    else:
        if source.is_symlink() or file_digest(source) != transcript['sha256']:
            raise Error('transcript changed; regenerate preview')
        output = canonical.prepare_import(source)[1]
    if transcript.get('content_sha256') and digest(output) != transcript['content_sha256']:
        raise Error('derived transcript changed; regenerate preview')
    return output


def rename_exclusive(source, destination):
    """Atomic no-replace rename; fail closed on unsupported platforms/filesystems."""
    import ctypes
    import errno
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        function = library.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = function(os.fsencode(source), os.fsencode(destination), 4)  # RENAME_EXCL
    elif sys.platform.startswith('linux') and hasattr(library, 'renameat2'):
        function = library.renameat2
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = function(-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # RENAME_NOREPLACE
    else:
        raise Error('atomic no-clobber rename is unsupported on this platform')
    if result:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise Error(f'rename collision; no overwrite: {destination}')
        raise Error(f'no-clobber rename failed: {os.strerror(code)}')


@contextlib.contextmanager
def project_lock_file(path):
    if not hasattr(os, 'O_NOFOLLOW'):
        raise Error('safe no-follow project locking is unsupported on this platform')
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise Error('project lock must be a regular file, never a symlink/device/FIFO')
        handle = os.fdopen(descriptor, 'r+')
        descriptor = None
        with handle:
            yield handle
    finally:
        if descriptor is not None:
            os.close(descriptor)


def file_identity(value):
    return value.st_dev, value.st_ino


@contextlib.contextmanager
def directory_anchor(path, *, create=False, label='canonical destination'):
    """Pin each ancestor from / with no-follow opens and retain its parent edge.

    A descriptor alone is insufficient: the original directory may itself have
    been moved outside the project. verify() checks every pinned parent/name edge,
    rejecting symlinks, replacement directories and unlinked/moved originals.
    """
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or not all(hasattr(os, flag) for flag in ('O_DIRECTORY', 'O_NOFOLLOW')):
        raise Error(f'{label}: safe directory traversal is unsupported')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
    descriptors = []
    edges = []

    def verify():
        for parent, name, child in edges:
            try:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                pinned = os.fstat(child)
            except OSError as exc:
                raise Error(f'{label} directory changed; regenerate preview') from exc
            if not stat.S_ISDIR(current.st_mode) or file_identity(current) != file_identity(pinned):
                raise Error(f'{label} directory changed or was replaced; regenerate preview')

    try:
        descriptors.append(os.open('/', flags))
        for component in path.parts[1:]:
            verify()
            parent = descriptors[-1]
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            child = os.open(component, flags, dir_fd=parent)
            descriptors.append(child)
            edges.append((parent, component, child))
        verify()
        yield descriptors[-1], verify
    except OSError as exc:
        raise Error(f'{label} directory is unsafe or changed: {exc}') from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def exclusive_import(source, out, output, settings, *, prepare=None):
    """Stage canonical bytes in settings, publish relative to verified directory FDs."""
    destination = out / 'transcript.md'
    with directory_anchor(settings, label='canonical staging') as (staging_fd, verify_staging), directory_anchor(out, create=True) as (destination_fd, verify_destination):
        prepared = prepare() if prepare else canonical.prepare_import(source)[1]
        if prepared != output:
            raise Error('transcript changed during canonical conversion; regenerate preview')
        stage_name = '.calendar-import-' + secrets.token_hex(16)
        staged_identity = None
        created = False
        try:
            verify_staging()
            descriptor = os.open(stage_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=staging_fd)
            with os.fdopen(descriptor, 'wb') as handle:
                staged_identity = file_identity(os.fstat(handle.fileno()))
                handle.write(prepared)
                handle.flush()
                os.fsync(handle.fileno())
            checkpoint('before-exclusive-publish')
            # Check rooted ancestry, not just whether the pinned FD still works.
            # The exact moved-directory + symlink race aborts BEFORE publication.
            verify_staging()
            verify_destination()
            if prepare and prepare() != output:
                raise Error('transcript changed before publication; regenerate preview')
            staged = os.stat(stage_name, dir_fd=staging_fd, follow_symlinks=False)
            if not stat.S_ISREG(staged.st_mode) or file_identity(staged) != staged_identity:
                raise Error('canonical staging file changed; regenerate preview')
            try:
                os.link(stage_name, 'transcript.md', src_dir_fd=staging_fd, dst_dir_fd=destination_fd, follow_symlinks=False)
                created = True
                status = 'created'
            except FileExistsError as exc:
                descriptor = os.open('transcript.md', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=destination_fd)
                with os.fdopen(descriptor, 'rb') as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode) or handle.read() != output:
                        raise Error(f'canonical destination conflict; exclusive create did not overwrite: {destination}') from exc
                status = 'unchanged'
            checkpoint('after-exclusive-link')
            # A last-instant ancestor move must not become a successful checkpoint.
            verify_staging()
            verify_destination()
        except BaseException:
            if created:
                # Remove only OUR just-published inode through the pinned directory;
                # never follow a replacement pathname or remove concurrent human data.
                try:
                    current = os.stat('transcript.md', dir_fd=destination_fd, follow_symlinks=False)
                    if file_identity(current) == staged_identity:
                        os.unlink('transcript.md', dir_fd=destination_fd)
                except FileNotFoundError:
                    pass
            raise
        finally:
            if staged_identity is not None:
                try:
                    current = os.stat(stage_name, dir_fd=staging_fd, follow_symlinks=False)
                    if file_identity(current) == staged_identity:
                        os.unlink(stage_name, dir_fd=staging_fd)
                except FileNotFoundError:
                    pass
    print(f'phase: import\nstatus: {status}\ntranscript_md: {destination}\nchanged: {"yes" if status == "created" else "no"}\nnext_phase: summarize')


def apply(args):
    plan_bytes = Path(args.plan).read_bytes()
    if digest(plan_bytes) != args.approve:
        raise Error('approval must equal SHA-256 of exact preview bytes')
    checkpoint('after-plan-read')
    plan = parse_markdown(plan_bytes, str(args.plan))
    validate_plan_shape(plan)
    config = plan['config']
    project = Path(config['project'])
    settings = project / '.meeting-transcript'
    if settings.is_symlink() or settings.resolve() != settings:
        raise Error('settings must not be a symlink')
    settings.mkdir(parents=True, exist_ok=True)
    with project_lock_file(settings / 'workflow.lock') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Error('another calendar import holds the project lock; retry after it exits') from exc
        if os.environ.get('MEETING_TRANSCRIPT_TEST_HOLD_LOCK') == '1':
            print('test: project lock held', flush=True)
            sys.stdin.readline()
        state = read_state(project)
        journal_path = settings / 'journal.md'
        journal = None
        if journal_path.exists():
            journal_bytes = journal_path.read_bytes()
            journal = parse_markdown(journal_bytes, str(journal_path))
            validate_journal_shape(journal)
            if journal['plan_sha256'] == args.approve:
                plan, state = reconcile_journal(journal, state)
            elif plan['recovery_journal_sha256'] == digest(journal_bytes) and state_hash(state) == plan['state_sha256']:
                _, state = reconcile_journal(journal, state)
                journal = None  # A newly reviewed plan explicitly supersedes reconciled partial work.
            else:
                raise Error('incomplete prior run; regenerate preview to reconcile journal or replay exact approved plan')
        elif state_hash(state) != plan['state_sha256']:
            raise Error('state changed; regenerate preview')
        if state['generation'] != plan['state_generation']:
            if journal and state['generation'] == plan['state_generation'] + 1 and state['period_end'] == plan['period_end']:
                validate_plan(plan, args.calendar)
                journal_path.unlink()
                print('status: already completed; recovered final checkpoint')
                return 0
            raise Error('state changed; regenerate preview')
        validate_plan(plan, args.calendar)
        if any(a['rename_requested'] and not a['rename_blocked'] and a['proposed_path'] != a['path'] for a in plan['artifacts']) and not args.allow_source_renames:
            raise Error('approved plan includes source renames; additionally pass --allow-source-renames')
        if journal is None:
            journal = dict(version=1, kind='meeting-calendar-journal', plan_sha256=args.approve, plan=copy.deepcopy(plan), allowed_state_hashes=[state_hash(read_state(project)), state_hash(state)], operations=[])
            save_journal(journal_path, journal)
        records = {m['event']['key']: m for m in state['meetings']}
        for meeting in plan['meetings']:
            event_key = meeting['event']['key']
            if not meeting['selected']:
                records.setdefault(event_key, dict(event=meeting['event'], status='pending', reason='not selected in approved batch', imported_path='', artifacts=[a for a in plan['artifacts'] if a['id'] in meeting['artifact_ids']]))
                state['meetings'] = list(records.values())
                continue
            record = dict(event=meeting['event'], status=meeting['status'], reason=meeting['reason'], imported_path=meeting['imported_path'], artifacts=[a for a in plan['artifacts'] if a['id'] in meeting['artifact_ids']])
            if meeting['status'] == 'ready':
                selected = next(t for t in meeting['transcripts'] if t['id'] == meeting['selected_transcript'])
                out = Path(config['destination']) / event_key
                destination = out / 'transcript.md'
                if out.resolve() != out or destination.is_symlink():
                    raise Error('unsafe canonical destination symlink; no writes')
                source = Path(selected['path'])
                output = prepare_snapshot(selected, meeting['event']['title'])
                if destination.exists() and destination.read_bytes() != output:
                    raise Error(f'canonical destination conflict; not overwriting: {destination}')
                journal['operations'].append(dict(kind='import', event_key=event_key, destination=str(destination), sha256=digest(output)))
                save_journal(journal_path, journal)
                failpoint('before-import')
                if file_digest(source) != selected['sha256']:
                    raise Error('transcript changed immediately before import; regenerate preview')
                exclusive_import(source, out, output, settings, prepare=lambda: prepare_snapshot(selected, meeting['event']['title']))
                failpoint('after-import')
                record.update(status='imported', reason='', imported_path=str(destination))
            elif meeting['status'] == 'calendar-update':
                # An observed update never rewrites canonical transcript content.
                record.update(status='imported', reason='', event=meeting['event'] if meeting['accept_update'] else meeting['previous_event'])
            for artifact in record.get('artifacts', []):
                if not artifact['rename_requested'] or artifact['rename_blocked'] or artifact['proposed_path'] == artifact['path']:
                    continue
                source, destination = Path(artifact['path']), Path(artifact['proposed_path'])
                if source.parent != destination.parent or artifact_snapshot(source, True) != artifact['snapshot']:
                    raise Error('source changed before rename; regenerate preview')
                journal['operations'].append(dict(kind='rename', event_key=event_key, artifact_id=artifact['id'], source=str(source), destination=str(destination), snapshot=artifact['snapshot']))
                save_journal(journal_path, journal)
                failpoint('before-rename')
                checkpoint('before-rename-validation')
                # Recheck external OBS sidecars too: hashing the video alone cannot
                # detect a recorder/transcriber becoming active during earlier imports.
                if source.resolve() != source or source.parent != destination.parent or artifact_snapshot(source, True) != artifact['snapshot']:
                    raise Error('source changed immediately before rename; regenerate preview')
                # Keep the small processing/identity check LAST, after potentially long media hashes.
                current_metadata = Path(artifact['metadata_path']).read_bytes()
                if digest(current_metadata) != artifact['metadata_sha256']:
                    raise Error('metadata changed immediately before rename; regenerate preview')
                metadata = tomllib.loads(current_metadata.decode('utf-8'))
                if metadata.get('processing') != 'idle' or not artifact['idle_attested'] or not artifact['safe']:
                    raise Error('source is not verifiably idle immediately before rename')
                rename_exclusive(source, destination)
                failpoint('after-rename')
                relocate_artifact(artifact, destination)
            records[event_key] = record
            state['meetings'] = list(records.values())
            state['initial_start'] = plan['initial_start']
            save_state(project, state, journal_path, journal)
        state['period_end'] = plan['period_end']
        state['generation'] += 1
        state['initial_start'] = plan['initial_start']
        save_state(project, state, journal_path, journal)
        failpoint('after-final-state')
        journal_path.unlink()
    print('status: complete\nnext_phase: summarize (separate approval); pending transcription is never automatic')
    return 0


def run(args):
    try:
        return preview(args) if args.operation == 'preview' else apply(args)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise Error(str(exc)) from exc
