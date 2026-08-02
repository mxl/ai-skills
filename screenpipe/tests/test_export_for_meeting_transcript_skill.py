import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "export_for_meeting_transcript_skill.py"
sys.path.insert(0, str(SCRIPT.parent))

import export_for_meeting_transcript_skill as exporter  # noqa: E402


def source_meeting():
    return {
        "schema_version": 1,
        "fetched_at": "2026-08-02T10:30:00Z",
        "meeting": {
            "id": 42,
            "meeting_start": "2026-08-02T09:00:00Z",
            "meeting_end": "2026-08-02T10:00:00Z",
            "meeting_app": "zoom.us",
            "title": "Planning",
            "attendees": json.dumps([
                {"name": "Alice", "email": "alice@example.test"},
                {"name": "Alice Duplicate", "email": "ALICE@example.test"},
                "Bob <bob@example.test>",
            ]),
            "note": "Decision: ship it.\n",
            "detection_source": "calendar",
            "created_at": "2026-08-02T08:59:00Z",
        },
        "transcript": [
            {
                "id": 1,
                "meetingId": 42,
                "source": "live",
                "provider": "deepgram",
                "model": None,
                "itemId": "one",
                "deviceName": "MacBook Microphone",
                "deviceType": "input",
                "audioTranscriptionId": None,
                "audioChunkId": 10,
                "audioFilePath": "/tmp/audio.mp4",
                "speakerId": 5,
                "speakerName": "Alice",
                "transcript": "  exact words  \n",
                "capturedAt": "2026-08-02T09:00:01Z",
                "createdAt": "2026-08-02T09:00:02Z",
            }
        ],
    }


class MappingTests(unittest.TestCase):
    def test_mapping_preserves_raw_and_transcript(self):
        raw = source_meeting()
        original = copy.deepcopy(raw)
        result = exporter.build_export(raw)

        self.assertEqual(result["raw"], original)
        self.assertIsNot(result["raw"], raw)
        self.assertEqual(result["source"], "screenpipe")
        self.assertEqual(result["title"], "Planning")
        self.assertEqual(result["started_at"], "2026-08-02T09:00:00Z")
        self.assertEqual(result["ended_at"], "2026-08-02T10:00:00Z")
        self.assertEqual(result["participants"], [
            {"name": "Alice", "email": "alice@example.test"},
            {"name": "Bob", "email": "bob@example.test"},
        ])
        self.assertEqual(result["transcript"], [{
            "started_at": "2026-08-02T09:00:01Z",
            "ended_at": "",
            "speaker": "Alice",
            "text": "  exact words  \n",
        }])
        self.assertEqual(result["notes"], [{
            "title": "Screenpipe note", "text": "Decision: ship it.\n",
        }])
        self.assertEqual(result["resources"], [{
            "label": "Screenpipe meeting", "target": "screenpipe://meeting/42",
        }])
        exporter.validate_export(result, exporter.load_schema(exporter.default_schema_path()))

    def test_missing_values_and_title_fallback(self):
        raw = source_meeting()
        raw["meeting"].update({
            "meeting_end": None, "title": None, "attendees": None, "note": None,
        })
        raw["transcript"][0].update({
            "capturedAt": None, "speakerName": None, "transcript": "hello",
        })
        result = exporter.build_export(raw)
        self.assertEqual(result["title"], "zoom.us meeting")
        self.assertEqual(result["ended_at"], "")
        self.assertEqual(result["participants"], [])
        self.assertEqual(result["transcript"], [{
            "started_at": "", "ended_at": "", "speaker": "", "text": "hello",
        }])

    def test_attendees_formats_and_deduplication(self):
        self.assertEqual(exporter.participants("Alice; Bob <bob@example.test>\nalice"), [
            {"name": "Alice", "email": ""},
            {"name": "Bob", "email": "bob@example.test"},
        ])
        self.assertEqual(exporter.participants({"name": "Alice", "email": "a@example.test"}), [
            {"name": "Alice", "email": "a@example.test"},
        ])
        self.assertEqual(exporter.participants("person@example.test"), [
            {"name": "", "email": "person@example.test"},
        ])

    def test_note_only_and_content_failures(self):
        raw = source_meeting()
        raw["transcript"] = []
        self.assertEqual(exporter.build_export(raw)["notes"][0]["text"], "Decision: ship it.\n")

        raw["meeting"]["note"] = "  "
        with self.assertRaisesRegex(exporter.ExportError, "neither transcript"):
            exporter.build_export(raw)

        raw = source_meeting()
        raw["schema_version"] = 2
        with self.assertRaisesRegex(exporter.ExportError, "unsupported Screenpipe schema_version"):
            exporter.build_export(raw)


class CLITests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, args)],
            text=True,
            capture_output=True,
            check=False,
        )

    def write_fixture(self, directory: Path, name: str, payload: dict) -> Path:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_default_explicit_and_multi_output(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            first = self.write_fixture(directory, "one.json", source_meeting())
            second_payload = source_meeting()
            second_payload["meeting"]["id"] = 43
            second = self.write_fixture(directory, "two.json", second_payload)

            result = self.run_cli(first)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((directory / "one.meeting-transcript.json").exists())

            explicit = directory / "custom.json"
            result = self.run_cli(first, "--out", explicit)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(explicit.exists())

            out_dir = directory / "converted"
            result = self.run_cli(first, second, "--out-dir", out_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(sorted(path.name for path in out_dir.iterdir()), [
                "one.meeting-transcript.json", "two.meeting-transcript.json",
            ])
            self.assertFalse(any(directory.rglob("*.tmp")))

    def test_default_output_strips_source_suffix(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = self.write_fixture(directory, "planning.screenpipe.json", source_meeting())
            result = self.run_cli(source)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((directory / "planning.meeting-transcript.json").exists())
            self.assertFalse((directory / "planning.screenpipe.meeting-transcript.json").exists())

    def test_conflicts_and_invalid_input(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = self.write_fixture(directory, "meeting.json", source_meeting())
            result = self.run_cli(source, "--out", directory / "a.json", "--out-dir", directory)
            self.assertEqual(result.returncode, 2)
            self.assertIn("cannot be used together", result.stderr)

            bad = self.write_fixture(directory, "bad.json", {"schema_version": 99})
            result = self.run_cli(bad)
            self.assertEqual(result.returncode, 1)
            self.assertIn("unsupported Screenpipe schema_version", result.stderr)

    def test_output_dir_rejects_same_stems(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            first = self.write_fixture(directory / "a", "meeting.json", source_meeting())
            second = self.write_fixture(directory / "b", "meeting.json", source_meeting())
            result = self.run_cli(first, second, "--out-dir", directory / "converted")
            self.assertEqual(result.returncode, 2)
            self.assertIn("same output path", result.stderr)

    def test_output_is_accepted_by_meeting_transcript_prepare(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = self.write_fixture(directory, "meeting.json", source_meeting())
            export_result = self.run_cli(source)
            self.assertEqual(export_result.returncode, 0, export_result.stderr)

            canonical = directory / "meeting.meeting-transcript.json"
            prepare_script = ROOT.parent / "meeting-transcript" / "scripts" / "meeting_transcript.py"
            prepare_result = subprocess.run(
                [
                    sys.executable,
                    str(prepare_script),
                    "prepare",
                    str(canonical),
                    "--artifacts-dir",
                    str(directory / "artifacts"),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(prepare_result.returncode, 0, prepare_result.stderr)
            packet = json.loads(prepare_result.stdout)
            self.assertEqual(packet["mode"], "improve")
            self.assertTrue(Path(packet["meeting_json"]).exists())


if __name__ == "__main__":
    unittest.main()
