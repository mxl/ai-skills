import importlib.util
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "meeting_transcript.py"
SPEC = importlib.util.spec_from_file_location("meeting_transcript", SCRIPT)
mt = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(mt)


def meeting(text="Keep this exact.\nSecond line."):
    return {
        "source": "granola",
        "title": "Planning",
        "started_at": "2026-08-01T09:00:00Z",
        "ended_at": "2026-08-01T10:00:00Z",
        "participants": [
            {"name": "Alice", "email": "alice@example.test"},
            {"name": "Bob", "email": ""},
        ],
        "transcript": [
            {"speaker": "Alice", "text": text},
            {"speaker": "alice", "text": "Third chunk."},
            {"speaker": "Bob", "text": "Принято."},
        ],
    }


class MeetingTranscriptTests(unittest.TestCase):
    def write_transcript(self, path: Path, value=None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(mt.render_transcript(value or meeting()), encoding="utf-8")
        return path

    def run_args(self, values):
        output = io.StringIO()
        args = mt.build_parser().parse_args(values)
        with redirect_stdout(output):
            self.assertEqual(args.func(args), 0)
        return output.getvalue()

    def test_render_uses_current_template_without_segment_ids(self):
        rendered = mt.render_transcript(meeting())
        self.assertIn("# Transcript: Planning", rendered)
        self.assertIn("- Participants: Alice <alice@example.test>, Bob", rendered)
        self.assertIn("Alice: Keep this exact.\nSecond line.\nThird chunk.", rendered)
        self.assertNotIn("<!-- transcript-entry -->", rendered)
        self.assertNotIn("**Speaker:**", rendered)
        self.assertIn("Принято.", rendered)
        self.assertNotIn("S00000", rendered)

    def test_parse_round_trip_preserves_order_speakers_and_text(self):
        parsed = mt.parse_transcript(mt.render_transcript(meeting()))
        self.assertEqual(parsed["source"], "granola")
        self.assertEqual(parsed["title"], "Planning")
        self.assertEqual(parsed["started_at"], "2026-08-01T09:00:00Z")
        self.assertEqual(parsed["participants"], "Alice <alice@example.test>, Bob")
        self.assertEqual(
            parsed["transcript"],
            [
                {"speaker": "Alice", "text": "Keep this exact.\nSecond line.\nThird chunk."},
                {"speaker": "Bob", "text": "Принято."},
            ],
        )

    def test_parse_preserves_multiline_speech_and_turn_boundaries(self):
        parsed = mt.parse_transcript(mt.render_transcript(meeting("First paragraph.\n\nSecond paragraph.")))
        self.assertEqual(parsed["transcript"][0]["text"], "First paragraph.\n\nSecond paragraph.\nThird chunk.")
        self.assertEqual(parsed["transcript"][1], {"speaker": "Bob", "text": "Принято."})

    def test_import_writes_only_transcript_markdown(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.write_transcript(root / "source.md")
            out = root / "meeting"
            report = self.run_args(["import", str(source), "--out", str(out)])

            self.assertIn("status: created", report)
            self.assertEqual(list(out.iterdir()), [out / "transcript.md"])
            self.assertEqual((out / "transcript.md").read_text(), source.read_text())

    def test_identical_import_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.write_transcript(root / "source.md")
            out = root / "meeting"
            self.run_args(["import", str(source), "--out", str(out)])
            before = (out / "transcript.md").stat().st_mtime_ns
            report = self.run_args(["import", str(source), "--out", str(out)])
            self.assertIn("status: unchanged", report)
            self.assertIn("changed: no", report)
            self.assertEqual((out / "transcript.md").stat().st_mtime_ns, before)

    def test_invalid_import_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "bad.md"
            source.write_text("# Not canonical\n", encoding="utf-8")
            out = root / "meeting"
            args = mt.build_parser().parse_args(["import", str(source), "--out", str(out)])
            with self.assertRaisesRegex(mt.MeetingError, "does not match"):
                args.func(args)
            self.assertFalse(out.exists())

    def test_prepare_outputs_prompt_without_json_files_or_segment_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = self.write_transcript(root / "transcript.md")
            report = self.run_args(["summarize", "prepare", str(transcript)])

            self.assertIn("Summary: " + str((root / "summary.md").resolve()), report)
            self.assertIn("Alice: Keep this exact.\nSecond line.", report)
            self.assertIn("=== SUMMARY TEMPLATE ===", report)
            self.assertIn("[transcript.md](./transcript.md)", report)
            self.assertNotIn("S00000", report)
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["transcript.md"])

    def test_prepare_loads_relative_markdown_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = self.write_transcript(root / "transcript.md")
            rules = root / "rules.md"
            rules.write_text("Focus on operational risks.\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"MEETING_TRANSCRIPT_SUMMARY_RULES": "rules.md"}), mock.patch.object(
                Path, "cwd", return_value=root
            ):
                report = self.run_args(["summarize", "prepare", str(transcript)])
            self.assertIn("Focus on operational risks.", report)
            self.assertIn(str(rules.resolve()), report)
            self.assertIn(mt.sha256_path(rules), report)

    def test_prepare_rejects_invalid_rules_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = self.write_transcript(root / "transcript.md")
            rules = root / "rules.txt"
            rules.write_text("Rules", encoding="utf-8")
            with mock.patch.dict(os.environ, {"MEETING_TRANSCRIPT_SUMMARY_RULES": str(rules)}):
                args = mt.build_parser().parse_args(["summarize", "prepare", str(transcript)])
                with self.assertRaisesRegex(mt.MeetingError, "Markdown file"):
                    args.func(args)


if __name__ == "__main__":
    unittest.main()
