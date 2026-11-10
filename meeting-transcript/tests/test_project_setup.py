"""Project-local ignore setup uses disposable files only; no live acquisition."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'meeting_transcript.py'
RULES = (b'/.granola/', b'/.meeting-transcript/meeting-transcript.lock')


class ProjectSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        self.ignore = self.project / '.gitignore'

    def command(self):
        return [sys.executable, str(SCRIPT), 'calendar', 'setup', '--project', str(self.project)]

    def setup_project(self, *, ok=True):
        result = subprocess.run(self.command(), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0 if ok else 2, result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        return result

    def test_setup_creates_only_project_gitignore_without_calendar_or_config(self):
        self.setup_project()
        self.assertEqual(set(self.project.iterdir()), {self.ignore})
        self.assertEqual(self.ignore.read_bytes().splitlines(), list(RULES))

    def test_setup_preserves_existing_bytes_and_line_endings(self):
        for original, newline in [(b'# custom\ncache/\n', b'\n'),
                                  (b'# custom\r\ncache/\r\n', b'\r\n'),
                                  (b'# custom\r\ncache/', b'\r\n'),
                                  (b'# custom\ncache/', b'\n')]:
            with self.subTest(original=original):
                self.ignore.write_bytes(original)
                self.setup_project()
                prefix = original if original.endswith(b'\n') else original + newline
                suffix = newline if original.endswith(b'\n') else b''
                self.assertEqual(self.ignore.read_bytes(), prefix + newline.join(RULES) + suffix)

    def test_setup_is_idempotent_and_preserves_mtime_when_rules_exist(self):
        self.setup_project()
        before = self.ignore.read_bytes(), self.ignore.stat().st_mtime_ns
        self.setup_project()
        self.assertEqual((self.ignore.read_bytes(), self.ignore.stat().st_mtime_ns), before)

    def test_equivalent_existing_rules_are_not_duplicated(self):
        for granola in (b'.granola', b'.granola/', b'/.granola', b'/.granola/'):
            with self.subTest(granola=granola):
                content = b'# keep\n' + granola + b'\n.meeting-transcript/meeting-transcript.lock\n'
                self.ignore.write_bytes(content)
                before = self.ignore.stat().st_mtime_ns
                self.setup_project()
                self.assertEqual(self.ignore.read_bytes(), content)
                self.assertEqual(self.ignore.stat().st_mtime_ns, before)

    def test_unsafe_ignore_paths_do_not_touch_external_targets(self):
        external = self.project.parent / 'external'
        for kind in ('dangling-symlink', 'symlink', 'directory', 'fifo'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    external.write_bytes(b'preserve')
                    self.ignore.symlink_to(external)
                elif kind == 'dangling-symlink':
                    self.ignore.symlink_to(external)
                elif kind == 'directory':
                    self.ignore.mkdir()
                else:
                    os.mkfifo(self.ignore)
                self.setup_project(ok=False)
                if kind == 'symlink':
                    self.assertEqual(external.read_bytes(), b'preserve')
                elif kind == 'dangling-symlink':
                    self.assertFalse(external.exists())
                if kind == 'directory':
                    self.ignore.rmdir()
                else:
                    self.ignore.unlink()

    def test_parallel_setup_does_not_duplicate_rules_or_lose_existing_content(self):
        self.ignore.write_bytes(b'# preserve\nuser-cache/')
        processes = [subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(4)]
        try:
            for process in processes:
                _, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, error)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertEqual(self.ignore.read_bytes(), b'# preserve\nuser-cache/\n' + b'\n'.join(RULES))

    @unittest.skipUnless(shutil.which('git'), 'git required for ignore semantics')
    def test_ignore_rules_cover_export_and_lock_but_not_state_or_plans(self):
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True, timeout=10)
        self.ignore.write_bytes(b'/.granola/\n!/.granola/\n!/.granola/**\n/.meeting-transcript/meeting-transcript.lock\n!*.lock\n')
        self.setup_project()
        before = self.ignore.read_bytes(), self.ignore.stat().st_mtime_ns
        self.setup_project()
        self.assertEqual((self.ignore.read_bytes(), self.ignore.stat().st_mtime_ns), before)
        for name, ignored in [('.granola/export.md', True),
                              ('.meeting-transcript/meeting-transcript.lock', True),
                              ('.meeting-transcript/state.md', False),
                              ('.meeting-transcript/plans/review.md', False),
                              ('meetings/meeting/transcript.md', False)]:
            with self.subTest(name=name):
                target = self.project / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('fixture')
                result = subprocess.run(['git', '-C', str(self.project), 'check-ignore', '-q', '--', name], timeout=10)
                self.assertEqual(result.returncode, 0 if ignored else 1)


if __name__ == '__main__':
    unittest.main()
