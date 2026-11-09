"""Behavioral calendar workflow tests: subprocess CLI, synthetic sources only."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
import shutil
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'meeting_transcript.py'
CANONICAL = '''---
type: source
source:
  - amanu
---

# Transcript: Planning

## Metadata

- Meeting date: 2026-08-01
- Started: 2026-08-01T09:00:00Z
- Ended: 2026-08-01T10:00:00Z
- Participants: Alice, Bob

## Transcript

Alice: Keep this exact.
Second line.

Bob: Принято.
'''


class CalendarWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.obs = self.root / 'obs'
        self.obs.mkdir()
        self.amanu = self.root / 'amanu'
        self.amanu.mkdir()
        self.global_config = self.root / 'global.toml'
        self.global_config.write_text('version = 1\n[sources.obs]\ntype = "obs"\npath = ' + json.dumps(str(self.obs)) + '\n[sources.amanu]\ntype = "amanu"\npath = ' + json.dumps(str(self.amanu)) + '\n')
        self.settings = self.project / '.meeting-transcript'
        self.settings.mkdir()
        self.config = self.settings / 'config.toml'
        self.config.write_text('version = 1\nprovider = "fixture"\ncalendars = ["work"]\ntimezone = "Europe/Moscow"\ndestination = "meetings"\nsources = ["obs", "amanu"]\n')
        self.calendar = self.root / 'calendar.json'
        self.events = [self.event()]
        self.save_calendar()

    def event(self, event_id='one', **changes):
        value = dict(calendar_id='work', event_id=event_id, recurrence_id='', title='Planning', start='2026-08-01T09:00:00Z', end='2026-08-01T10:00:00Z')
        value.update(changes)
        return value

    def save_calendar(self):
        self.calendar.write_text(json.dumps(dict(version=1, provider='fixture', events=self.events)))

    def run_cli(self, *args, ok=True, env=None):
        result = subprocess.run([sys.executable, str(SCRIPT), 'calendar', *map(str, args)], text=True, capture_output=True, timeout=15, env={**os.environ, **(env or {})})
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def preview(self, *args, start=True, ok=True, env=None):
        values = ['preview', '--project', self.project, '--global-config', self.global_config, '--calendar', self.calendar, '--until', '2026-08-02T00:00:00Z']
        if start:
            values += ['--since', '2026-08-01T00:00:00Z']
        return self.run_cli(*values, *args, ok=ok, env=env)

    def data(self, text):
        return tomllib.loads(text.split('```toml\n', 1)[1].split('\n```', 1)[0])

    def metadata(self, path, *, event=None, transcript='', processing='idle', title='Planning'):
        metadata = path / 'meeting-import.toml' if path.is_dir() else path.with_name(path.name + '.meeting.toml')
        value = 'schema = "meeting-transcript-artifact-v1"\nstarted_at = "2026-08-01T09:01:00Z"\nended_at = "2026-08-01T09:59:00Z"\nprocessing = ' + json.dumps(processing) + '\ntitle = ' + json.dumps(title) + '\ntranscript = ' + json.dumps(transcript) + '\n'
        if event:
            value += '[event]\n' + ''.join(f'{k} = {json.dumps(event.get(k, ""))}\n' for k in ('calendar_id', 'event_id', 'recurrence_id'))
        metadata.write_text(value)
        return metadata

    def package(self, name='package', content=CANONICAL, **kwargs):
        package = self.amanu / name
        package.mkdir()
        (package / 'transcript.md').write_text(content)
        self.metadata(package, transcript='transcript.md', **kwargs)
        return package

    def save_plan(self, report):
        path = self.root / 'plan.md'
        path.write_text(report.stdout)
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def apply(self, plan, approval, *args, ok=True, env=None):
        return self.run_cli('apply', '--plan', plan, '--approve', approval, '--calendar', self.calendar, *args, ok=ok, env=env)

    def start_checkpoint(self, plan, approval, checkpoint, *args):
        command = [sys.executable, str(SCRIPT), 'calendar', 'apply', '--plan', str(plan), '--approve', approval, '--calendar', str(self.calendar), *args]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ, 'MEETING_TRANSCRIPT_TEST_CHECKPOINT': checkpoint})
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate()
        self.addCleanup(cleanup)
        lines = []
        while True:
            line = process.stdout.readline()
            if not line:
                self.fail('CLI exited before checkpoint: ' + ''.join(lines) + process.stderr.read())
            lines.append(line)
            if line.strip() == 'test: checkpoint ' + checkpoint:
                break
        return process

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}

    def native_package(self, name='native', *, segments=None, speakers=None):
        package = self.amanu / name
        package.mkdir()
        (package / 'meta.json').write_text(json.dumps(dict(started='2026-08-01T09:01:00Z', ended='2026-08-01T09:59:00Z', duration_seconds=3480, app='Planning', mic_capture=True)))
        if segments is None:
            segments = [dict(start_ms=0, end_ms=1000, speaker='speaker_0', text='Exact {{placeholder}} **speech**.'), dict(start_ms=1000, end_ms=2000, speaker='speaker_0', text='Сохрани 🌍.\nSecond line.'), dict(start_ms=2000, end_ms=3000, speaker='speaker_1', text='Done.')]
        (package / 'transcript.json').write_text(json.dumps(dict(created_at='2026-08-01T10:01:00.750Z', engine='fixture', model='synthetic', segments=segments), ensure_ascii=False))
        if speakers is not None:
            (package / 'speakers.json').write_text(json.dumps(dict(created_at='2026-08-01T10:02:00Z', speakers=speakers), ensure_ascii=False))
        (package / 'transcribe.log').write_text('2026-08-01T10:00:00Z transcribing synthetic-input\n2026-08-01T10:01:00Z done — ' + str(len(segments)) + ' segments\n2026-08-01T10:01:01Z archive complete\n')
        return package

    def native_match(self, *flags):
        plan = self.data(self.preview().stdout)
        artifact = next(a for a in plan['artifacts'] if a['type'] == 'amanu')
        event = plan['meetings'][0]['event']['key']
        return self.preview('--match', artifact['id'] + '=' + event, *flags)

    def test_native_json_snapshot_preserves_segments_labels_and_literal_speech(self):
        package = self.native_package(speakers={'speaker_0': {'name': 'Synthetic Alice', 'source': 'manual'}})
        before = self.snapshot()
        report = self.native_match()
        plan = self.data(report.stdout)
        self.assertEqual(plan['meetings'][0]['status'], 'ready')
        self.assertEqual(before, self.snapshot())
        self.assertNotIn('Exact {{placeholder}}', report.stdout)
        path, approval = self.save_plan(report)
        original = {p.name: p.read_bytes() for p in package.iterdir()}
        self.apply(path, approval)
        transcript = next((self.project / 'meetings').glob('*/transcript.md')).read_text()
        self.assertIn('Synthetic Alice: Exact {{placeholder}} **speech**.\nСохрани 🌍.\nSecond line.\n\nspeaker_1: Done.', transcript)
        self.assertEqual({p.name: p.read_bytes() for p in package.iterdir()}, original)
        self.assertFalse(list(self.project.rglob('*.json')))

    def test_native_approved_derived_hash_matches_actual_calendar_title_output(self):
        self.native_package()
        report = self.native_match()
        candidate = self.data(report.stdout)['meetings'][0]['transcripts'][0]
        path, approval = self.save_plan(report)
        self.apply(path, approval)
        output = next((self.project / 'meetings').glob('*/transcript.md')).read_bytes()
        self.assertEqual(candidate['content_sha256'], hashlib.sha256(output).hexdigest())
        self.assertEqual(candidate['title'], 'Planning')

    def test_native_derived_context_tampering_is_not_an_approved_output(self):
        self.native_package()
        report = self.native_match()
        path, approval = self.save_plan(report)
        original = path.read_text()
        tampered = original.replace('"title" = "Planning"\n"content_sha256"', '"title" = "Tampered"\n"content_sha256"')
        self.assertNotEqual(tampered, original)
        path.write_text(tampered)
        self.apply(path, approval, ok=False)
        approval = hashlib.sha256(path.read_bytes()).hexdigest()
        result = self.apply(path, approval, ok=False)
        self.assertIn('derived', result.stderr)
        self.assertFalse((self.project / 'meetings').exists())

    def test_native_changed_template_rejects_approval_without_publishing(self):
        self.native_package()
        copied = self.root / 'copied-skill'
        shutil.copytree(SCRIPT.parent, copied / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(SCRIPT.parent.parent / 'templates', copied / 'templates')
        with mock.patch(__name__ + '.SCRIPT', copied / 'scripts' / SCRIPT.name):
            path, approval = self.save_plan(self.native_match())
            template = copied / 'templates' / 'meeting-transcript.md'
            original = template.read_text()
            template.write_text(original.replace('- Participants: ', '- Participants: Modified '))
            result = self.apply(path, approval, ok=False)
            self.assertIn('derived', result.stderr)
            self.assertFalse((self.project / 'meetings').exists())
            template.write_text(original)
            process = self.start_checkpoint(path, approval, 'before-exclusive-publish')
            template.write_text(original.replace('- Participants: ', '- Participants: Modified '))
            stdout, stderr = process.communicate('\n', timeout=10)
            self.assertEqual(process.returncode, 2, stdout + stderr)
            self.assertIn('derived', stderr)
            self.assertFalse(list((self.project / 'meetings').glob('*/transcript.md')))

    def test_native_fifo_inputs_are_pending_without_blocking_other_imports(self):
        self.native_package('ready')
        for filename in ('meta.json', 'transcript.json', 'speakers.json', 'transcribe.log'):
            package = self.native_package('fifo_' + filename)
            path = package / filename
            path.unlink(missing_ok=True)
            os.mkfifo(path)
        initial = self.data(self.preview().stdout)
        bad = [a for a in initial['artifacts'] if Path(a['path']).name.startswith('fifo_')]
        self.assertTrue(all(a['status'] == 'unverified' and not a['transcripts'] for a in bad))
        ready = next(a for a in initial['artifacts'] if Path(a['path']).name == 'ready')
        event_key = initial['meetings'][0]['event']['key']
        path, approval = self.save_plan(self.preview('--match', ready['id'] + '=' + event_key))
        self.apply(path, approval)
        self.assertEqual(len(list((self.project / 'meetings').glob('*/transcript.md'))), 1)

    def test_native_unrepresentable_utterances_or_labels_remain_pending_without_rewriting(self):
        variants = [('delimiter', 'Raw', 'First.\n\nOther: Synthetic speech.'), ('final_newline', 'Raw', 'Synthetic speech.\n'), ('label_newline', 'Raw\nLabel', 'Synthetic speech.'), ('label_colon', 'Raw:Label', 'Synthetic speech.')]
        for name, label, text in variants:
            self.native_package(name, segments=[dict(start_ms=0, end_ms=1000, speaker=label, text=text)])
        plan = self.data(self.preview().stdout)
        self.assertTrue(all(a['status'] == 'unverified' and not a['transcripts'] for a in plan['artifacts']))
        self.assertEqual(plan['meetings'][0]['status'], 'pending')
        self.assertFalse((self.project / 'meetings').exists())

    def test_native_distinct_case_and_edge_space_labels_are_never_normalized_or_merged(self):
        self.native_package(segments=[dict(start_ms=0, end_ms=1000, speaker='Alice', text='One.'), dict(start_ms=1000, end_ms=2000, speaker='ALICE', text='Two.'), dict(start_ms=2000, end_ms=3000, speaker=' Alice ', text='Three.')])
        path, approval = self.save_plan(self.native_match())
        self.apply(path, approval)
        content = next((self.project / 'meetings').glob('*/transcript.md')).read_text()
        self.assertIn('Alice: One.\n\nALICE: Two.\n\n Alice : Three.', content)
        self.assertIn('- Participants: Alice, ALICE,  Alice \n', content)

    def test_native_string_maps_and_unmatched_labels_preserve_spaces_and_timing_overshoot(self):
        package = self.native_package(segments=[dict(start_ms=0, end_ms=4000000, speaker='Raw Label', text='  leading\n\n**Markdown** and {{title}} trailing  ')], speakers={'unused': 'Synthetic Display', 'also_unused': {'name': 'Other Display', 'source': 'fixture'}})
        meta = json.loads((package / 'meta.json').read_text())
        meta['duration_seconds'] = 3479.75
        (package / 'meta.json').write_text(json.dumps(meta))
        report = self.native_match()
        self.assertEqual(self.data(report.stdout)['meetings'][0]['status'], 'ready')
        path, approval = self.save_plan(report)
        self.apply(path, approval)
        content = next((self.project / 'meetings').glob('*/transcript.md')).read_text()
        self.assertIn('- Participants: Raw Label\n', content)
        self.assertTrue(content.endswith('Raw Label:   leading\n\n**Markdown** and {{title}} trailing  \n'))
        self.assertNotIn('Synthetic Display', content)

    def test_native_interval_requires_explicit_match_groups_obs_and_repeat_is_idempotent(self):
        self.native_package()
        recording = self.obs / 'recording.mkv'
        recording.write_bytes(b'synthetic video')
        self.metadata(recording, event=self.events[0])
        initial = self.data(self.preview().stdout)
        native = next(a for a in initial['artifacts'] if a['type'] == 'amanu')
        self.assertEqual(native['event_key'], '')
        self.assertEqual(native['candidates'], [initial['meetings'][0]['event']['key']])
        self.assertEqual(initial['meetings'][0]['status'], 'pending')
        self.assertEqual(initial['meetings'][0]['artifact_ids'], [next(a['id'] for a in initial['artifacts'] if a['type'] == 'obs')])
        report = self.native_match('--rename', native['id'], '--idle-source', native['id'])
        plan = self.data(report.stdout)
        self.assertEqual(len(plan['meetings'][0]['artifact_ids']), 2)
        self.assertFalse(next(a for a in plan['artifacts'] if a['type'] == 'amanu')['safe'])
        self.assertIn('verified idle', next(a for a in plan['artifacts'] if a['type'] == 'amanu')['rename_blocked'])
        path, approval = self.save_plan(report)
        self.apply(path, approval, '--allow-source-renames')
        transcript = next((self.project / 'meetings').glob('*/transcript.md'))
        before = transcript.stat().st_mtime_ns
        report = self.preview('--until', '2026-08-03T00:00:00Z', start=False)
        repeated = self.data(report.stdout)
        self.assertEqual(repeated['meetings'][0]['status'], 'imported')
        self.assertEqual(len(repeated['meetings'][0]['artifact_ids']), 2)
        path, approval = self.save_plan(report)
        self.apply(path, approval)
        self.assertEqual(transcript.stat().st_mtime_ns, before)
        self.assertEqual(len(list((self.project / 'meetings').glob('*/transcript.md'))), 1)

    def test_native_requires_positive_completion_matching_snapshot_and_no_later_rerun(self):
        variants = {
            'missing': None,
            'empty': '',
            'bad_count': '2026-08-01T10:01:00Z done — 99 segments\n',
            'stale': '2026-08-01T10:00:59Z done — 3 segments\n',
            'rerun': '2026-08-01T10:01:00Z done — 3 segments\n2026-08-01T10:02:00Z transcribing synthetic-input\n',
            'not_done': '2026-08-01T10:01:00Z transcribing synthetic-input\n',
        }
        for name, log in variants.items():
            package = self.native_package(name)
            if log is None:
                (package / 'transcribe.log').unlink()
            else:
                (package / 'transcribe.log').write_text(log)
        plan = self.data(self.preview().stdout)
        self.assertTrue(all(not a['transcripts'] for a in plan['artifacts']))
        self.assertEqual(plan['meetings'][0]['status'], 'pending')
        self.assertTrue(all(not a['safe'] for a in plan['artifacts']))

    def test_native_raw_input_edits_invalidate_exact_approval(self):
        package = self.native_package(speakers={'speaker_0': {'name': 'Synthetic Alice', 'source': 'manual'}})
        for filename in ('meta.json', 'transcript.json', 'speakers.json', 'transcribe.log'):
            with self.subTest(filename=filename):
                report = self.native_match()
                path, approval = self.save_plan(report)
                source = package / filename
                original = source.read_bytes()
                source.write_bytes(original + b' ')
                result = self.apply(path, approval, ok=False)
                self.assertIn('changed', result.stderr)
                self.assertFalse((self.project / 'meetings').exists())
                source.write_bytes(original)
        (package / 'speakers.json').unlink()
        path, approval = self.save_plan(self.native_match())
        (package / 'speakers.json').write_text(json.dumps(dict(created_at='2026-08-01T10:02:00Z', speakers={})))
        self.apply(path, approval, ok=False)
        self.assertFalse((self.project / 'meetings').exists())

    def test_native_rechecks_raw_inputs_immediately_before_publication(self):
        package = self.native_package()
        plan, approval = self.save_plan(self.native_match())
        process = self.start_checkpoint(plan, approval, 'before-exclusive-publish')
        source = package / 'transcript.json'
        source.write_bytes(source.read_bytes() + b' ')
        stdout, stderr = process.communicate('\n', timeout=10)
        self.assertEqual(process.returncode, 2, stdout + stderr)
        self.assertIn('changed', stderr)
        self.assertFalse(list((self.project / 'meetings').glob('*/transcript.md')))
        self.assertFalse(list(self.settings.glob('.calendar-import-*')))

    def test_native_invalid_packages_stay_pending_without_blocking_ready_meeting(self):
        self.package()
        invalids = [
            ('empty', 'transcript.json', dict(created_at='2026-08-01T10:01:00Z', engine='fixture', model='fixture', segments=[])),
            ('active', 'meta.json', dict(started='2026-08-01T09:01:00Z', duration_seconds=100)),
            ('bad_duration', 'meta.json', dict(started='2026-08-01T09:01:00Z', ended='2026-08-01T09:59:00Z', duration_seconds=True)),
            ('bad_speakers', 'speakers.json', dict(created_at='2026-08-01T10:01:00Z', speakers={'speaker_0': 42})),
            ('null_speakers', 'speakers.json', None),
            ('bad_label', 'speakers.json', dict(created_at='2026-08-01T10:01:00Z', speakers={'speaker_0': 'Not: representable'})),
            ('bad_segment', 'transcript.json', dict(created_at='2026-08-01T10:01:00Z', engine='fixture', model='fixture', segments=[dict(start_ms=False, end_ms=1000, speaker='raw', text='Synthetic text')])),
        ]
        for name, filename, data in invalids:
            package = self.native_package(name)
            (package / filename).write_text(json.dumps(data))
        partial = self.native_package('partial')
        (partial / 'transcript.json').write_text('{"segments":[')
        plan = self.data(self.preview().stdout)
        self.assertEqual(plan['meetings'][0]['status'], 'ready')
        native_artifacts = [a for a in plan['artifacts'] if Path(a['path']).name != 'package']
        self.assertEqual(len(native_artifacts), len(invalids) + 1)
        self.assertTrue(all(a['status'] == 'unverified' and not a['transcripts'] for a in native_artifacts))
        path, approval = self.save_plan(self.preview())
        self.apply(path, approval)
        self.assertEqual(len(list((self.project / 'meetings').glob('*/transcript.md'))), 1)

    def fake_ffprobe(self, payload):
        binary_dir = self.root / 'bin'
        binary_dir.mkdir(exist_ok=True)
        binary = binary_dir / 'ffprobe'
        binary.write_text('#!' + sys.executable + '\nimport json, os, sys\nassert "FFREPORT" not in os.environ\nassert sys.argv[1:] == ["-v", "error", "-show_entries", "format=duration:format_tags=creation_time", "-of", "json", "-protocol_whitelist", "file", "-i", ' + repr(str((self.obs / 'raw.mov').resolve())) + ']\nprint(' + repr(json.dumps(payload)) + ')\n')
        binary.chmod(0o700)
        return {'PATH': str(binary_dir), 'FFREPORT': 'file=should-never-exist.log'}

    def test_native_obs_missing_or_invalid_probe_stays_pending_with_filename_evidence(self):
        recording = self.obs / 'raw.mov'
        recording.write_bytes(b'synthetic container bytes')
        self.events[0]['title'] = 'raw'
        self.save_calendar()
        variants = [{}, {'format': {'duration': 'NaN', 'tags': {'creation_time': '2026-08-01T09:01:00Z'}}}, {'format': {'duration': '-1', 'tags': {'creation_time': '2026-08-01T09:01:00Z'}}}, {'format': {'duration': True, 'tags': {'creation_time': '2026-08-01T09:01:00Z'}}}, {'format': {'duration': '100', 'tags': {'creation_time': '2026-08-01T09:01:00'}}}]
        for index in range(len(variants) + 1):
            with self.subTest(index=index):
                env = {'PATH': ''} if index == 0 else self.fake_ffprobe(variants[index - 1])
                plan = self.data(self.preview(env=env).stdout)
                artifact = plan['artifacts'][0]
                self.assertEqual(artifact['status'], 'unverified')
                self.assertEqual(artifact['candidates'], [plan['meetings'][0]['event']['key']])
                self.assertEqual(artifact['event_key'], '')
                self.assertIn('filename title only', ' '.join(artifact['evidence']))
                self.assertEqual(plan['meetings'][0]['status'], 'pending')
        self.assertFalse((self.project / 'meetings').exists())

    def test_native_both_sources_group_only_after_explicit_choices(self):
        self.native_package()
        recording = self.obs / 'raw.mov'
        recording.write_bytes(b'synthetic container bytes')
        env = self.fake_ffprobe(dict(format=dict(duration='3480.25', tags=dict(creation_time='2026-08-01T09:01:00+00:00'))))
        first = self.data(self.preview(env=env).stdout)
        self.assertTrue(all(not a['event_key'] for a in first['artifacts']))
        event_key = first['meetings'][0]['event']['key']
        flags = [value for artifact in first['artifacts'] for value in ('--match', artifact['id'] + '=' + event_key)]
        report = self.preview(*flags, env=env)
        meeting = self.data(report.stdout)['meetings'][0]
        self.assertEqual(len(meeting['artifact_ids']), 2)
        self.assertEqual(meeting['status'], 'ready')
        path, approval = self.save_plan(report)
        self.apply(path, approval, env=env)
        self.assertEqual(len(list((self.project / 'meetings').glob('*/transcript.md'))), 1)
        self.assertTrue(recording.exists())

    def test_native_obs_container_timing_is_candidate_not_automatic_identity_or_idle(self):
        recording = self.obs / 'raw.mov'
        recording.write_bytes(b'synthetic container bytes')
        env = self.fake_ffprobe(dict(format=dict(duration='3480.25', tags=dict(creation_time='2026-08-01T09:01:00Z'))))
        first = self.data(self.preview(env=env).stdout)
        artifact = first['artifacts'][0]
        event_key = first['meetings'][0]['event']['key']
        self.assertEqual(artifact['status'], 'verified')
        self.assertEqual(artifact['candidates'], [event_key])
        self.assertEqual(artifact['event_key'], '')
        self.assertFalse(artifact['safe'])
        self.assertIn('container', ' '.join(artifact['evidence']))
        report = self.preview('--match', artifact['id'] + '=' + event_key, '--rename', artifact['id'], '--idle-source', artifact['id'], env=env)
        self.assertIn('verified idle', self.data(report.stdout)['artifacts'][0]['rename_blocked'])
        plan, approval = self.save_plan(report)
        recording.write_bytes(b'changed source bytes')
        self.assertIn('source changed', self.apply(plan, approval, ok=False).stderr)
        self.assertTrue(recording.exists())

    def test_preview_uses_bound_calendar_and_timezone_without_writes(self):
        recording = self.obs / '2026-08-01 09-00 Planning.mkv'
        recording.write_bytes(b'recording')
        before = self.snapshot()
        report = self.preview()
        plan = self.data(report.stdout)
        self.assertEqual(plan['version'], 1)
        self.assertEqual(plan['period_end'], '2026-08-02T00:00:00+00:00')
        self.assertEqual(plan['meetings'][0]['event']['title'], 'Planning')
        self.assertEqual(plan['artifacts'][0]['status'], 'unverified')
        self.assertIn('2026-08-01 12-00 Planning.mkv', report.stdout)
        self.assertEqual(before, self.snapshot())

    def test_verified_sources_group_but_active_transcripts_are_not_ready(self):
        package = self.package(processing='active')
        (package / '.transcribing').write_text('stale or active is not knowable from marker')
        recording = self.obs / 'video.mkv'
        recording.write_bytes(b'video')
        self.metadata(recording, event=self.events[0])
        plan = self.data(self.preview().stdout)
        self.assertEqual(len(plan['meetings']), 1)
        self.assertEqual(len(plan['meetings'][0]['artifact_ids']), 2)
        self.assertEqual(plan['meetings'][0]['status'], 'pending')
        self.assertEqual(plan['meetings'][0]['transcripts'], [])
        self.assertFalse(next(a for a in plan['artifacts'] if a['type'] == 'amanu')['safe'])

    def test_exact_approval_imports_once_through_canonical_flow_without_renaming(self):
        package = self.package()
        report = self.preview()
        path, approval = self.save_plan(report)
        before = (package / 'transcript.md').read_bytes()
        self.apply(path, 'wrong', ok=False)
        self.assertFalse((self.project / 'meetings').exists())
        self.apply(path, approval)
        transcripts = list((self.project / 'meetings').glob('*/transcript.md'))
        self.assertEqual(len(transcripts), 1)
        self.assertEqual(transcripts[0].read_bytes(), before)
        self.assertEqual((package / 'transcript.md').read_bytes(), before)
        mtime = transcripts[0].stat().st_mtime_ns
        next_report = self.preview('--until', '2026-08-03T00:00:00Z', start=False)
        second_path, second_approval = self.save_plan(next_report)
        self.apply(second_path, second_approval)
        self.assertEqual(transcripts[0].stat().st_mtime_ns, mtime)
        state = self.data((self.settings / 'state.md').read_text())
        self.assertEqual(state['period_end'], '2026-08-03T00:00:00+00:00')
        self.assertEqual(state['meetings'][0]['status'], 'imported')
        self.assertEqual(len(list((self.project / 'meetings').glob('*/transcript.md'))), 1)
        self.assertFalse(list(self.project.rglob('*.json')))
        self.assertFalse(list(self.project.rglob('summary.md')))

    def test_renames_require_exact_plan_idle_attestation_and_separate_opt_in(self):
        package = self.package()
        (package / '.transcribing').write_text('stale marker alone proves nothing')
        original = {p.name: p.read_bytes() for p in package.iterdir()}
        recording = self.obs / 'raw.MKV'
        recording.write_bytes(b'video')
        self.metadata(recording, event=self.events[0])
        first = self.data(self.preview().stdout)
        ids = [a['id'] for a in first['artifacts']]
        flags = [item for aid in ids for item in ('--rename', aid, '--idle-source', aid)]
        report = self.preview(*flags)
        plan, approval = self.save_plan(report)
        self.apply(plan, approval, ok=False)
        self.assertTrue(package.exists())
        self.assertTrue(recording.exists())
        self.assertFalse((self.project / 'meetings').exists())
        self.apply(plan, approval, '--allow-source-renames')
        renamed = self.amanu / '2026-08-01 12-00 Planning'
        self.assertFalse(package.exists())
        self.assertEqual({p.name: p.read_bytes() for p in renamed.iterdir()}, original)
        self.assertEqual((self.obs / '2026-08-01 12-00 Planning.MKV').read_bytes(), b'video')
        again = self.data(self.preview('--until', '2026-08-03T00:00:00Z', start=False).stdout)
        self.assertEqual(len(again['meetings'][0]['artifact_ids']), 2)
        self.assertEqual(set(again['meetings'][0]['artifact_ids']), set(ids))
        self.assertEqual(again['meetings'][0]['status'], 'imported')

    def test_interruption_after_rename_recovers_exact_associations_without_advancing_boundary(self):
        package = self.package()
        first = self.data(self.preview().stdout)
        aid = first['artifacts'][0]['id']
        report = self.preview('--rename', aid, '--idle-source', aid)
        path, approval = self.save_plan(report)
        self.apply(path, approval, '--allow-source-renames', ok=False, env={'MEETING_TRANSCRIPT_TEST_FAILPOINT': 'after-rename'})
        self.assertFalse(package.exists())
        renamed = self.amanu / '2026-08-01 12-00 Planning'
        self.assertTrue(renamed.exists())
        if (self.settings / 'state.md').exists():
            self.assertEqual(self.data((self.settings / 'state.md').read_text())['period_end'], '')
        self.apply(path, approval, '--allow-source-renames')
        state = self.data((self.settings / 'state.md').read_text())
        self.assertEqual(state['period_end'], '2026-08-02T00:00:00+00:00')
        self.assertEqual(state['meetings'][0]['artifacts'][0]['original_path'], str(package.resolve()))
        self.assertEqual(state['meetings'][0]['artifacts'][0]['path'], str(renamed.resolve()))
        self.assertEqual(len(list((self.project / 'meetings').glob('*/transcript.md'))), 1)
        self.assertFalse((self.settings / 'journal.md').exists())

    def test_known_calendar_change_is_proposal_until_separately_accepted(self):
        self.package()
        initial = self.preview()
        path, approval = self.save_plan(initial)
        self.apply(path, approval)
        transcript = next((self.project / 'meetings').glob('*/transcript.md'))
        original = transcript.read_bytes()
        self.events[0].update(title='New/title?', start='2026-08-01T11:00:00Z', end='2026-08-01T12:00:00Z')
        self.save_calendar()
        report = self.preview('--until', '2026-08-03T00:00:00Z', start=False)
        proposed = self.data(report.stdout)
        meeting = proposed['meetings'][0]
        self.assertEqual(meeting['status'], 'calendar-update')
        self.assertEqual(meeting['previous_event']['title'], 'Planning')
        self.assertEqual(len(meeting['artifact_ids']), 1)
        path, approval = self.save_plan(report)
        self.apply(path, approval)
        self.assertTrue((self.amanu / 'package').exists())
        aid, event_key = proposed['artifacts'][0]['id'], meeting['event']['key']
        accepted = self.preview('--until', '2026-08-04T00:00:00Z', '--accept-update', event_key, '--rename', aid, '--idle-source', aid, start=False)
        path, approval = self.save_plan(accepted)
        self.apply(path, approval, '--allow-source-renames')
        self.assertTrue((self.amanu / '2026-08-01 14-00 New_title_').is_dir())
        self.assertEqual(transcript.read_bytes(), original)
        self.assertEqual(len(list((self.project / 'meetings').glob('*/transcript.md'))), 1)
        final = self.data(self.preview('--until', '2026-08-05T00:00:00Z', start=False).stdout)
        self.assertEqual(final['meetings'][0]['status'], 'imported')

    def test_explicit_event_identity_does_not_also_match_nearby_same_title(self):
        self.events.append(self.event('two', start='2026-08-01T09:30:00Z', end='2026-08-01T10:30:00Z'))
        self.save_calendar()
        self.package(event=self.events[0])
        plan = self.data(self.preview().stdout)
        self.assertEqual(len(plan['artifacts'][0]['candidates']), 1)
        one, two = plan['meetings']
        self.assertEqual(one['status'], 'ready')
        self.assertEqual(two['status'], 'pending')

    def test_pending_deferred_and_unselected_meetings_retry_after_fixed_watermark(self):
        self.events += [self.event('late'), self.event('ongoing', start='2026-08-01T23:00:00Z', end='2026-08-02T01:00:00Z'), self.event('declined')]
        self.save_calendar()
        self.package(event=self.events[0])
        initial = self.data(self.preview().stdout)
        by_event = {m['event']['event_id']: m for m in initial['meetings']}
        self.assertEqual(by_event['ongoing']['status'], 'deferred')
        report = self.preview('--decline', by_event['declined']['event']['key'], '--select', by_event['one']['event']['key'], '--select', by_event['declined']['event']['key'])
        path, approval = self.save_plan(report)
        self.apply(path, approval)
        state = self.data((self.settings / 'state.md').read_text())
        self.assertEqual(state['period_end'], '2026-08-02T00:00:00+00:00')
        self.assertEqual({m['event']['event_id'] for m in state['meetings']}, {'one', 'late', 'ongoing', 'declined'})
        self.package('late-package', event=self.events[1])
        again = self.preview('--until', '2026-08-03T00:00:00Z', start=False)
        next_plan = self.data(again.stdout)
        self.assertEqual(next_plan['period_start'], '2026-08-02T00:00:00+00:00')
        statuses = {m['event']['event_id']: m['status'] for m in next_plan['meetings']}
        self.assertEqual(statuses, {'one': 'imported', 'late': 'ready', 'ongoing': 'pending'})
        path, approval = self.save_plan(again)
        self.apply(path, approval)
        self.assertEqual(len(list((self.project / 'meetings').glob('*/transcript.md'))), 2)

    def test_known_event_refresh_is_required_even_outside_new_period(self):
        self.package(event=self.events[0])
        path, approval = self.save_plan(self.preview())
        self.apply(path, approval)
        self.events = []
        self.save_calendar()
        result = self.preview('--until', '2026-08-03T00:00:00Z', start=False, ok=False)
        self.assertIn('known event', result.stderr)
        self.assertIn('fetch', result.stderr)

    def test_ambiguous_match_then_competing_transcripts_require_explicit_choices(self):
        self.events.append(self.event('two'))
        self.save_calendar()
        self.package('a')
        self.package('b', content=CANONICAL.replace('Keep this exact.', 'Different words.'))
        ambiguous = self.data(self.preview().stdout)
        self.assertEqual([m['status'] for m in ambiguous['meetings']], ['pending', 'pending'])
        self.assertIn('ambiguous', ambiguous['meetings'][0]['reason'])
        event_key = ambiguous['meetings'][0]['event']['key']
        flags = [x for a in ambiguous['artifacts'] for x in ('--match', a['id'] + '=' + event_key)]
        competing = self.data(self.preview(*flags).stdout)
        self.assertIn('competing', competing['meetings'][0]['reason'])
        choices = competing['meetings'][0]['transcripts']
        self.assertEqual(len(choices), 2)
        selected = self.preview(*flags, '--transcript', event_key + '=' + choices[1]['id'])
        path, approval = self.save_plan(selected)
        self.apply(path, approval)
        result = next((self.project / 'meetings').glob('*/transcript.md'))
        self.assertIn('Different words.', result.read_text())
        self.assertEqual(len(list((self.project / 'meetings').glob('*/transcript.md'))), 1)

    def test_import_rejects_destination_symlink_escape_before_any_transcript_write(self):
        self.package()
        report = self.preview()
        event_key = self.data(report.stdout)['meetings'][0]['event']['key']
        outside = self.root / 'outside'
        outside.mkdir()
        (self.project / 'meetings').mkdir()
        (self.project / 'meetings' / event_key).symlink_to(outside, target_is_directory=True)
        path, approval = self.save_plan(report)
        result = self.apply(path, approval, ok=False)
        self.assertIn('destination', result.stderr)
        self.assertEqual(list(outside.iterdir()), [])

    def test_crash_checkpoints_keep_boundary_and_support_exact_replay(self):
        for checkpoint in ('before-import', 'after-import', 'before-rename', 'before-state', 'after-state', 'after-final-state'):
            with self.subTest(checkpoint=checkpoint):
                # Independent public-workflow fixtures for every abrupt process exit.
                fixture = CalendarWorkflowTests('runTest')
                fixture.setUp()
                try:
                    fixture.package()
                    aid = fixture.data(fixture.preview().stdout)['artifacts'][0]['id']
                    report = fixture.preview('--rename', aid, '--idle-source', aid)
                    path, approval = fixture.save_plan(report)
                    fixture.apply(path, approval, '--allow-source-renames', ok=False, env={'MEETING_TRANSCRIPT_TEST_FAILPOINT': checkpoint})
                    if (fixture.settings / 'state.md').exists() and checkpoint != 'after-final-state':
                        self.assertEqual(fixture.data((fixture.settings / 'state.md').read_text())['period_end'], '')
                    fixture.apply(path, approval, '--allow-source-renames')
                    self.assertEqual(fixture.data((fixture.settings / 'state.md').read_text())['period_end'], '2026-08-02T00:00:00+00:00')
                    self.assertEqual(len(list((fixture.project / 'meetings').glob('*/transcript.md'))), 1)
                    self.assertTrue((fixture.amanu / '2026-08-01 12-00 Planning').exists())
                finally:
                    fixture.doCleanups()

    def test_new_preview_reconciles_interrupted_rename_without_losing_original_path(self):
        recording = self.obs / 'raw.mkv'
        recording.write_bytes(b'video')
        self.metadata(recording, event=self.events[0])
        aid = self.data(self.preview().stdout)['artifacts'][0]['id']
        path, approval = self.save_plan(self.preview('--rename', aid, '--idle-source', aid))
        self.apply(path, approval, '--allow-source-renames', ok=False, env={'MEETING_TRANSCRIPT_TEST_FAILPOINT': 'after-rename'})
        before = self.snapshot()
        new = self.preview()
        self.assertEqual(before, self.snapshot())
        artifact = self.data(new.stdout)['artifacts'][0]
        self.assertEqual(artifact['id'], aid)
        self.assertEqual(artifact['original_path'], str(recording.resolve()))
        self.assertEqual(artifact['event_key'], self.data(new.stdout)['meetings'][0]['event']['key'])
        path, approval = self.save_plan(new)
        self.apply(path, approval)
        self.assertFalse((self.settings / 'journal.md').exists())
        state = self.data((self.settings / 'state.md').read_text())
        self.assertEqual(state['meetings'][0]['artifacts'][0]['original_path'], str(recording.resolve()))

    def test_two_cli_writers_cannot_mutate_concurrently_and_exit_releases_lock(self):
        self.package()
        path, approval = self.save_plan(self.preview())
        command = [sys.executable, str(SCRIPT), 'calendar', 'apply', '--plan', str(path), '--approve', approval, '--calendar', str(self.calendar)]
        first = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ, 'MEETING_TRANSCRIPT_TEST_HOLD_LOCK': '1'})
        try:
            self.assertEqual(first.stdout.readline().strip(), 'test: project lock held')
            blocked = self.apply(path, approval, ok=False)
            self.assertIn('project lock', blocked.stderr)
            self.assertFalse((self.project / 'meetings').exists())
            output, error = first.communicate('\n', timeout=10)
            self.assertEqual(first.returncode, 0, error)
            self.assertIn('status: complete', output)
            self.assertEqual(len(list((self.project / 'meetings').glob('*/transcript.md'))), 1)
            stale = self.apply(path, approval, ok=False)
            self.assertIn('state changed', stale.stderr)
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate()

    def test_setup_choices_aware_times_and_explicit_sources_do_not_persist_overrides(self):
        before = self.snapshot()
        self.assertIn('--since', self.preview(start=False, ok=False).stderr)
        self.assertEqual(before, self.snapshot())
        original = self.config.read_text()
        for line, field in [('provider = "fixture"\n', 'provider'), ('calendars = ["work"]\n', 'calendars'), ('timezone = "Europe/Moscow"\n', 'timezone'), ('destination = "meetings"\n', 'destination')]:
            with self.subTest(field=field):
                self.config.write_text(original.replace(line, ''))
                self.assertIn(field, self.preview(ok=False).stderr)
        self.config.write_text(original)
        self.assertIn('timezone', self.preview('--since', '2026-08-01T00:00:00', ok=False).stderr)
        self.events[0]['start'] = '2026-08-01T09:00:00'
        self.save_calendar()
        self.assertIn('timezone', self.preview(ok=False).stderr)
        self.events[0]['start'] = '2026-08-01T09:00:00Z'
        self.save_calendar()
        self.package()
        empty = self.root / 'targeted'
        empty.mkdir()
        report = self.preview('--source', 'obs=' + str(empty), '--timezone', 'UTC', '--destination', 'target-meetings')
        plan = self.data(report.stdout)
        self.assertEqual(plan['artifacts'], [])
        self.assertEqual(plan['config']['timezone'], 'UTC')
        self.assertTrue(plan['config']['destination'].endswith('/target-meetings'))
        self.assertEqual(self.config.read_text(), original)
        self.assertFalse((self.settings / 'state.md').exists())

    def test_unknown_native_amanu_invalid_transcript_and_marker_stay_pending(self):
        native = self.amanu / 'Planning'
        native.mkdir()
        (native / 'meta.json').write_text('{"event_id":"one","complete":true}')
        (native / 'transcript.md').write_text(CANONICAL)
        (native / '.transcribing').write_text('stale marker')
        bad = self.package('bad', content='# not canonical')
        recording = self.obs / 'nearest.mkv'
        recording.write_bytes(b'video')
        self.metadata(recording, title='Different title')
        before = self.snapshot()
        report = self.preview()
        plan = self.data(report.stdout)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(plan['meetings'][0]['status'], 'pending')
        self.assertEqual(plan['meetings'][0]['transcripts'], [])
        self.assertTrue(all(not a['safe'] for a in plan['artifacts'] if a['type'] == 'amanu'))
        self.assertEqual(next(a for a in plan['artifacts'] if a['path'] == str(recording.resolve()))['candidates'], [])
        path, approval = self.save_plan(report)
        self.apply(path, approval)
        self.assertFalse((self.project / 'meetings').exists())
        self.assertTrue(native.exists())
        self.assertTrue(bad.exists())

    def test_collision_unsafe_and_modified_sources_never_overwrite_or_rename(self):
        package = self.package()
        collision = self.amanu / '2026-08-01 12-00 Planning'
        collision.mkdir()
        (collision / 'keep.txt').write_bytes(b'irreplaceable')
        aid = next(a['id'] for a in self.data(self.preview().stdout)['artifacts'] if a['path'] == str(package.resolve()))
        report = self.preview('--rename', aid, '--idle-source', aid)
        artifact = next(a for a in self.data(report.stdout)['artifacts'] if a['id'] == aid)
        self.assertTrue(artifact['collision'])
        self.assertIn('no overwrite', artifact['rename_blocked'])
        path, approval = self.save_plan(report)
        self.apply(path, approval, '--allow-source-renames')
        self.assertTrue(package.exists())
        self.assertEqual((collision / 'keep.txt').read_bytes(), b'irreplaceable')
        unchanged = self.preview('--until', '2026-08-03T00:00:00Z', '--rename', aid, start=False)
        self.assertIn('attestation', next(a for a in self.data(unchanged.stdout)['artifacts'] if a['id'] == aid)['rename_blocked'])
        path, approval = self.save_plan(unchanged)
        (package / 'transcript.md').write_text(CANONICAL.replace('exact.', 'modified.'))
        self.assertIn('source changed', self.apply(path, approval, ok=False).stderr)
        state = self.data((self.settings / 'state.md').read_text())
        self.assertEqual(state['period_end'], '2026-08-02T00:00:00+00:00')

    def test_identity_distinguishes_calendars_and_recurring_instances(self):
        self.events = [self.event('same', recurrence_id='2026-08-01T09:00:00Z', recurring=True), self.event('same', recurrence_id='2026-08-02T09:00:00Z', recurring=True), self.event('same', calendar_id='personal'), self.event('same')]
        self.save_calendar()
        plan = self.data(self.preview('--calendar-id', 'work', '--calendar-id', 'personal').stdout)
        self.assertEqual(len(plan['meetings']), 4)
        self.assertEqual(len({m['event']['key'] for m in plan['meetings']}), 4)
        self.events[0]['recurrence_id'] = ''
        self.save_calendar()
        self.assertIn('recurrence_id', self.preview(ok=False).stderr)

    def test_reused_source_inode_with_changed_metadata_does_not_inherit_old_event_match(self):
        self.events.append(self.event('two', title='Other meeting'))
        self.save_calendar()
        recording = self.obs / 'raw.mkv'
        recording.write_bytes(b'first video')
        self.metadata(recording, event=self.events[0])
        path, approval = self.save_plan(self.preview())
        self.apply(path, approval)
        recording.write_bytes(b'new video reusing same inode')
        self.metadata(recording, event=self.events[1], title='Other meeting')
        plan = self.data(self.preview('--until', '2026-08-03T00:00:00Z', start=False).stdout)
        new_key = next(m['event']['key'] for m in plan['meetings'] if m['event']['event_id'] == 'two')
        self.assertEqual(plan['artifacts'][0]['event_key'], new_key)
        old = next(m for m in plan['meetings'] if m['event']['event_id'] == 'one')
        self.assertEqual(old['artifact_ids'], [])

    def test_unreadable_metadata_does_not_block_other_ready_meetings(self):
        self.package(event=self.events[0])
        unreadable = self.amanu / 'unreadable'
        unreadable.mkdir()
        # A directory at the contract filename deterministically raises an I/O error,
        # including when tests run with elevated filesystem permissions.
        (unreadable / 'meeting-import.toml').mkdir()
        before = self.snapshot()
        report = self.preview()
        self.assertEqual(before, self.snapshot())
        bad = next(a for a in self.data(report.stdout)['artifacts'] if a['path'] == str(unreadable.resolve()))
        self.assertEqual(bad['status'], 'unverified')
        self.assertIn('unavailable', bad['warning'])
        path, approval = self.save_plan(report)
        self.apply(path, approval)
        self.assertEqual(len(list((self.project / 'meetings').glob('*/transcript.md'))), 1)
        self.assertTrue(unreadable.exists())

    def test_cli_reports_actionable_errors_without_tracebacks(self):
        report = self.preview('--since', '2026-08-01T00:00:00', ok=False)
        self.assertEqual(report.returncode, 2)
        self.assertTrue(report.stderr.startswith('error: '), report.stderr)
        self.assertNotIn('Traceback', report.stderr)

    def test_future_cutoff_cannot_make_unfinished_events_eligible(self):
        result = self.preview('--until', '9999-01-01T00:00:00Z', ok=False)
        self.assertIn('future', result.stderr)
        self.assertFalse((self.settings / 'state.md').exists())

    def test_replaced_source_root_symlink_invalidates_previously_approved_plan(self):
        package = self.package()
        aid = self.data(self.preview().stdout)['artifacts'][0]['id']
        path, approval = self.save_plan(self.preview('--rename', aid, '--idle-source', aid))
        moved = self.root / 'moved-amanu'
        self.amanu.rename(moved)
        self.amanu.symlink_to(moved, target_is_directory=True)
        result = self.apply(path, approval, '--allow-source-renames', ok=False)
        self.assertIn('source', result.stderr)
        self.assertTrue((moved / 'package').exists())
        self.assertFalse((self.project / 'meetings').exists())

    def test_long_unicode_calendar_title_has_a_valid_non_destructive_filename(self):
        self.events[0]['title'] = 'Планирование' * 30
        self.save_calendar()
        self.package(event=self.events[0], title=self.events[0]['title'])
        aid = self.data(self.preview().stdout)['artifacts'][0]['id']
        report = self.preview('--rename', aid, '--idle-source', aid)
        new_path = Path(self.data(report.stdout)['artifacts'][0]['proposed_path'])
        self.assertLessEqual(len(new_path.name.encode()), 255)
        path, approval = self.save_plan(report)
        self.apply(path, approval, '--allow-source-renames')
        self.assertTrue(new_path.exists())

    def test_new_artifact_reusing_pre_rename_filename_has_distinct_identity(self):
        original = self.obs / 'raw.mkv'
        original.write_bytes(b'first video')
        self.metadata(original, event=self.events[0])
        first = self.data(self.preview().stdout)
        old_id = first['artifacts'][0]['id']
        path, approval = self.save_plan(self.preview('--rename', old_id, '--idle-source', old_id))
        self.apply(path, approval, '--allow-source-renames')
        original.write_bytes(b'second video')
        current = self.data(self.preview('--until', '2026-08-03T00:00:00Z', start=False).stdout)
        self.assertEqual(len(current['artifacts']), 2)
        self.assertEqual(len({a['id'] for a in current['artifacts']}), 2)
        renamed = next(a for a in current['artifacts'] if a['path'] != str(original.resolve()))
        self.assertEqual(renamed['id'], old_id)

    def test_partial_batch_failure_preserves_completed_meeting_and_old_watermark(self):
        self.events.append(self.event('two', title='Second'))
        self.save_calendar()
        self.package('one', event=self.events[0])
        self.package('two', event=self.events[1], title='Second')
        report = self.preview()
        meetings = self.data(report.stdout)['meetings']
        conflict = self.project / 'meetings' / meetings[1]['event']['key'] / 'transcript.md'
        conflict.parent.mkdir(parents=True)
        conflict.write_bytes(b'user work - never overwrite')
        path, approval = self.save_plan(report)
        self.assertIn('conflict', self.apply(path, approval, ok=False).stderr)
        state = self.data((self.settings / 'state.md').read_text())
        self.assertEqual(state['period_end'], '')
        self.assertEqual(state['meetings'][0]['status'], 'imported')
        first = Path(state['meetings'][0]['imported_path'])
        before = first.stat().st_mtime_ns
        self.assertEqual(conflict.read_bytes(), b'user work - never overwrite')
        conflict.rename(conflict.with_name('user-preserved.md'))
        self.apply(path, approval)
        self.assertEqual(first.stat().st_mtime_ns, before)
        self.assertEqual(len(list((self.project / 'meetings').glob('*/transcript.md'))), 2)
        self.assertEqual(self.data((self.settings / 'state.md').read_text())['period_end'], '2026-08-02T00:00:00+00:00')

    def test_fresh_calendar_and_config_changes_invalidate_exact_old_approval(self):
        self.package()
        path, approval = self.save_plan(self.preview())
        self.events[0]['title'] = 'Changed after approval'
        self.save_calendar()
        self.assertIn('calendar changed', self.apply(path, approval, ok=False).stderr)
        self.assertFalse((self.project / 'meetings').exists())
        self.events[0]['title'] = 'Planning'
        self.save_calendar()
        self.config.write_text(self.config.read_text() + '# changed after approval\n')
        self.assertIn('configuration changed', self.apply(path, approval, ok=False).stderr)
        self.assertFalse((self.project / 'meetings').exists())

    def test_approved_nested_config_resolves_sources_naming_and_request_overrides(self):
        self.global_config.write_text('version = 1\n[naming]\nformat = "{date} {time} {title}"\ndate_format = "%Y-%m-%d"\ntime_format = "%H-%M"\n[sources.obs]\ntype = "obs"\npath = ' + json.dumps(str(self.obs)) + '\n[sources.amanu]\ntype = "amanu"\npath = ' + json.dumps(str(self.amanu)) + '\n')
        self.config.write_text('version = 1\n[calendar]\nprovider = "fixture"\ncalendar_ids = ["work"]\ntimezone = "Europe/Moscow"\n[import]\ndestination = "approved-meetings"\nsources = ["amanu"]\n')
        self.package(event=self.events[0])
        recording = self.obs / 'excluded.mkv'
        recording.write_bytes(b'video')
        self.metadata(recording, event=self.events[0])
        before = self.snapshot()
        report = self.preview()
        self.assertEqual(before, self.snapshot())
        plan = self.data(report.stdout)
        self.assertEqual(plan['config']['provider'], 'fixture')
        self.assertEqual(plan['config']['calendars'], ['work'])
        self.assertEqual(plan['config']['timezone'], 'Europe/Moscow')
        self.assertEqual([a['type'] for a in plan['artifacts']], ['amanu'])
        self.assertTrue(plan['config']['destination'].endswith('/approved-meetings'))
        self.assertTrue(plan['artifacts'][0]['proposed_path'].endswith('2026-08-01 12-00 Planning'))
        override = self.data(self.preview('--timezone', 'UTC', '--destination', 'one-run').stdout)
        self.assertEqual(override['config']['timezone'], 'UTC')
        self.assertTrue(override['config']['destination'].endswith('/one-run'))
        path, approval = self.save_plan(report)
        self.apply(path, approval)
        self.assertEqual(len(list((self.project / 'approved-meetings').glob('*/transcript.md'))), 1)
        self.assertTrue((self.amanu / 'package').exists())

    def test_malformed_source_field_is_pending_and_calendar_container_has_clean_error(self):
        self.package('ready', event=self.events[0])
        invalid = self.package('bad', event=self.events[0])
        metadata = invalid / 'meeting-import.toml'
        metadata.write_text(metadata.read_text().replace('title = "Planning"', 'title = 123'))
        report = self.preview()
        bad = next(a for a in self.data(report.stdout)['artifacts'] if a['path'] == str(invalid.resolve()))
        self.assertEqual(bad['status'], 'unverified')
        self.assertIn('title', bad['warning'])
        path, approval = self.save_plan(report)
        self.apply(path, approval)
        self.assertEqual(len(list((self.project / 'meetings').glob('*/transcript.md'))), 1)
        self.calendar.write_text('[]')
        error = self.preview('--until', '2026-08-03T00:00:00Z', start=False, ok=False)
        self.assertEqual(error.returncode, 2)
        self.assertNotIn('Traceback', error.stderr)
        self.assertIn('calendar input', error.stderr)

    def test_nested_config_rejects_conflicts_unsupported_naming_and_malformed_types(self):
        original = self.config.read_text()
        compatible = original + '[calendar]\nprovider = "fixture"\ncalendar_ids = ["work"]\ntimezone = "Europe/Moscow"\n[import]\ndestination = "meetings"\nsources = ["obs", "amanu"]\n'
        self.config.write_text(compatible)
        self.preview()
        cases = [compatible.replace('[calendar]\nprovider = "fixture"', '[calendar]\nprovider = "different"'), original + 'calendar = []\n', original.replace('sources = ["obs", "amanu"]', 'sources = [123]'), original.replace('destination = "meetings"', 'destination = ["meetings"]')]
        for content in cases:
            with self.subTest(content=content):
                self.config.write_text(content)
                result = self.preview(ok=False)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn('Traceback', result.stderr)
        self.config.write_text(original)
        global_original = self.global_config.read_text()
        for setting in ('format = "{title}"', 'date_format = "%d-%m-%Y"', 'time_format = "%H:%M"', 'format = 123'):
            with self.subTest(setting=setting):
                self.global_config.write_text(global_original + '[naming]\n' + setting + '\n')
                result = self.preview(ok=False)
                self.assertIn('naming.', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
        self.global_config.write_text('version = 1\nsources = ["obs"]\n')
        self.assertNotIn('Traceback', self.preview(ok=False).stderr)

    @unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'), 'ru_maxrss units are specified here for macOS/Linux')
    def test_large_sparse_recording_preview_hashing_has_bounded_process_memory(self):
        recording = self.obs / 'large.mkv'
        with recording.open('wb') as handle:
            handle.truncate(128 * 1024 * 1024)
        self.metadata(recording, event=self.events[0])
        wrapper = 'import json,resource,subprocess,sys; r=subprocess.run(sys.argv[1:],text=True,capture_output=True); print(json.dumps(dict(code=r.returncode,out=r.stdout,err=r.stderr,rss=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)))'
        command = [sys.executable, '-c', wrapper, sys.executable, str(SCRIPT), 'calendar', 'preview', '--project', str(self.project), '--global-config', str(self.global_config), '--calendar', str(self.calendar), '--since', '2026-08-01T00:00:00Z', '--until', '2026-08-02T00:00:00Z']
        measured = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(measured.returncode, 0, measured.stderr)
        result = json.loads(measured.stdout)
        self.assertEqual(result['code'], 0, result['err'])
        peak_bytes = result['rss'] if sys.platform == 'darwin' else result['rss'] * 1024
        self.assertLess(peak_bytes, 96 * 1024 * 1024, f'CLI peak RSS was {peak_bytes} bytes for a sparse 128MiB video')
        self.assertEqual(self.data(result['out'])['artifacts'][0]['status'], 'verified')
        self.assertEqual(recording.stat().st_size, 128 * 1024 * 1024)
        self.assertFalse((self.settings / 'state.md').exists())

    def test_fresh_calendar_addition_rejects_old_plan_before_any_mutation(self):
        self.package(event=self.events[0])
        path, approval = self.save_plan(self.preview())
        self.events.append(self.event('added-after-preview', title='New meeting'))
        self.save_calendar()
        result = self.apply(path, approval, ok=False)
        self.assertIn('calendar', result.stderr)
        self.assertFalse((self.project / 'meetings').exists())
        self.assertFalse((self.settings / 'state.md').exists())
        self.assertFalse((self.settings / 'journal.md').exists())
        # Events strictly outside this period and not known are irrelevant.
        self.events[1].update(start='2026-07-01T09:00:00Z', end='2026-07-01T10:00:00Z')
        self.save_calendar()
        self.apply(path, approval)
        self.assertEqual(self.data((self.settings / 'state.md').read_text())['period_end'], '2026-08-02T00:00:00+00:00')

    def test_plan_path_replacement_after_digest_cannot_change_verified_plan(self):
        self.package()
        path, approval = self.save_plan(self.preview())
        process = self.start_checkpoint(path, approval, 'after-plan-read')
        path.write_text('# replacement is not the approved plan\n')
        _, error = process.communicate('\n', timeout=10)
        self.assertEqual(process.returncode, 0, error)
        self.assertEqual(len(list((self.project / 'meetings').glob('*/transcript.md'))), 1)
        self.assertEqual(self.data((self.settings / 'state.md').read_text())['period_end'], '2026-08-02T00:00:00+00:00')

    def test_canonical_destination_race_preserves_human_file_and_watermark(self):
        self.package()
        report = self.preview()
        destination = self.project / 'meetings' / self.data(report.stdout)['meetings'][0]['event']['key'] / 'transcript.md'
        path, approval = self.save_plan(report)
        process = self.start_checkpoint(path, approval, 'before-exclusive-publish')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b'human/direct-importer concurrent work')
        _, error = process.communicate('\n', timeout=10)
        self.assertEqual(process.returncode, 2, error)
        self.assertIn('conflict', error)
        self.assertEqual(destination.read_bytes(), b'human/direct-importer concurrent work')
        self.assertFalse((self.settings / 'state.md').exists())
        self.assertTrue((self.amanu / 'package').exists())
        self.assertFalse(list(destination.parent.glob('.calendar-import-*')))

    def test_obs_sidecar_rechecked_after_imports_immediately_before_rename(self):
        self.package(event=self.events[0])
        recording = self.obs / 'raw.mkv'
        recording.write_bytes(b'video')
        metadata = self.metadata(recording, event=self.events[0])
        initial = self.data(self.preview().stdout)
        artifact = next(a for a in initial['artifacts'] if a['type'] == 'obs')
        path, approval = self.save_plan(self.preview('--rename', artifact['id'], '--idle-source', artifact['id']))
        process = self.start_checkpoint(path, approval, 'before-rename-validation', '--allow-source-renames')
        self.assertEqual(len(list((self.project / 'meetings').glob('*/transcript.md'))), 1)
        metadata.write_text(metadata.read_text().replace('processing = "idle"', 'processing = "active"').replace('event_id = "one"', 'event_id = "different"'))
        _, error = process.communicate('\n', timeout=10)
        self.assertEqual(process.returncode, 2, error)
        self.assertIn('metadata changed', error)
        self.assertTrue(recording.exists())
        self.assertFalse(Path(artifact['proposed_path']).exists())
        if (self.settings / 'state.md').exists():
            self.assertEqual(self.data((self.settings / 'state.md').read_text())['period_end'], '')

    def test_lock_symlinks_existing_and_dangling_never_touch_external_target(self):
        self.package()
        path, approval = self.save_plan(self.preview())
        external = self.root / 'external-lock-target'
        lock = self.settings / 'workflow.lock'
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    external.write_bytes(b'preserve external bytes')
                lock.symlink_to(external)
                result = self.apply(path, approval, ok=False)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn('Traceback', result.stderr)
                self.assertFalse((self.project / 'meetings').exists())
                self.assertFalse((self.settings / 'state.md').exists())
                if existing:
                    self.assertEqual(external.read_bytes(), b'preserve external bytes')
                else:
                    self.assertFalse(external.exists())
                lock.unlink()

    def test_malformed_state_and_journal_report_document_field_without_traceback(self):
        state = self.settings / 'state.md'
        state.write_text('# Meeting calendar state\n\n```toml\nversion = 1\ngeneration = 0\ninitial_start = ""\nperiod_end = ""\nmeetings = 123\n```\n')
        result = self.preview(ok=False)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('Traceback', result.stderr)
        self.assertIn('state.meetings', result.stderr)
        state.unlink()
        journal = self.settings / 'journal.md'
        journal.write_text('# Meeting calendar recovery journal\n\n```toml\nversion = 1\nkind = "meeting-calendar-journal"\nplan_sha256 = "invalid"\nallowed_state_hashes = []\noperations = 123\n[plan]\n```\n')
        result = self.preview(ok=False)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('Traceback', result.stderr)
        self.assertIn('journal.operations', result.stderr)

    def test_output_directory_move_and_symlink_replacement_never_publishes_outside_project(self):
        self.package()
        report = self.preview()
        event_key = self.data(report.stdout)['meetings'][0]['event']['key']
        output_directory = self.project / 'meetings' / event_key
        path, approval = self.save_plan(report)
        process = self.start_checkpoint(path, approval, 'before-exclusive-publish')
        outside = self.root / 'moved-event-directory'
        output_directory.rename(outside)
        output_directory.symlink_to(outside, target_is_directory=True)
        _, error = process.communicate('\n', timeout=10)
        self.assertEqual(process.returncode, 2, error)
        self.assertIn('destination', error)
        self.assertEqual(list(outside.iterdir()), [], 'no staged or canonical transcript may be written in the moved directory')
        self.assertFalse((self.settings / 'state.md').exists())
        self.assertTrue((self.amanu / 'package').exists())
        self.assertFalse(list(self.settings.glob('.calendar-import-*')))

    def test_regular_directory_replacement_or_unlinked_pinned_directory_is_rejected(self):
        for mode in ('regular-replacement', 'parent-replacement', 'unlinked-original'):
            with self.subTest(mode=mode):
                fixture = CalendarWorkflowTests('runTest')
                fixture.setUp()
                try:
                    fixture.package()
                    report = fixture.preview()
                    event_key = fixture.data(report.stdout)['meetings'][0]['event']['key']
                    output = fixture.project / 'meetings' / event_key
                    path, approval = fixture.save_plan(report)
                    process = fixture.start_checkpoint(path, approval, 'before-exclusive-publish')
                    outside = fixture.root / 'moved-output'
                    if mode == 'parent-replacement':
                        output.parent.rename(outside)
                        output.mkdir(parents=True)
                    elif mode == 'unlinked-original':
                        output.rmdir()
                        output.mkdir()
                    else:
                        output.rename(outside)
                        output.mkdir()
                    _, error = process.communicate('\n', timeout=10)
                    self.assertEqual(process.returncode, 2, error)
                    self.assertIn('destination', error)
                    self.assertFalse(list(output.rglob('transcript.md')))
                    if outside.exists():
                        self.assertFalse(list(outside.rglob('transcript.md')))
                        self.assertFalse(list(outside.rglob('.calendar-import-*')))
                    self.assertFalse((fixture.settings / 'state.md').exists())
                    self.assertFalse(list(fixture.settings.glob('.calendar-import-*')))
                finally:
                    fixture.doCleanups()

    def test_successful_apply_reports_only_existing_final_transcript_path(self):
        self.package()
        path, approval = self.save_plan(self.preview())
        report = self.apply(path, approval)
        paths = [Path(line.removeprefix('transcript_md: ')) for line in report.stdout.splitlines() if line.startswith('transcript_md: ')]
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].is_file())
        self.assertTrue(paths[0].is_relative_to(self.project.resolve() / 'meetings'))
        self.assertNotIn('.calendar-import-', report.stdout)
        self.assertFalse(list(self.settings.glob('.calendar-import-*')))

    def test_last_instant_directory_move_rolls_back_own_link_through_pinned_fd(self):
        self.package()
        report = self.preview()
        event_key = self.data(report.stdout)['meetings'][0]['event']['key']
        output = self.project / 'meetings' / event_key
        path, approval = self.save_plan(report)
        process = self.start_checkpoint(path, approval, 'after-exclusive-link')
        self.assertTrue((output / 'transcript.md').exists())
        outside = self.root / 'moved-after-link'
        output.rename(outside)
        output.symlink_to(outside, target_is_directory=True)
        _, error = process.communicate('\n', timeout=10)
        self.assertEqual(process.returncode, 2, error)
        self.assertIn('destination', error)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.settings / 'state.md').exists())
        self.assertFalse(list(self.settings.glob('.calendar-import-*')))


if __name__ == '__main__':
    unittest.main()
