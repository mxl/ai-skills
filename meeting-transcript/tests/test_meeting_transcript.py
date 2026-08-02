import importlib.util
import io
import json
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


def meeting(title="Planning", text="Keep this exact.\nSecond line."):
    return {
        "schema_version": 1,
        "raw": {"received": {"transcript": text}, "secret_duplicate": "do not prompt"},
        "source": "agent-session",
        "title": title,
        "started_at": "2026-08-01T09:00:00Z",
        "ended_at": "2026-08-01T10:00:00Z",
        "participants": [
            {"name": "Alice", "email": "alice@example.test"},
            {"name": "Bob", "email": ""},
        ],
        "transcript": [
            {
                "started_at": "2026-08-01T09:00:01Z",
                "ended_at": "2026-08-01T09:00:03Z",
                "speaker": "Alice",
                "text": text,
            }
        ],
        "notes": [{"title": "Provided", "text": "Check this note."}],
        "resources": [{"label": "Doc", "target": "https://example.test/doc"}],
    }


def summary():
    return {
        "context": "Planning context.",
        "summary": "Concise result.",
        "key_points": ["Point one"],
        "decisions": ["Ship it"],
        "entities": [{"name": "Alice", "role": "Lead", "facts": ["Owns delivery"]}],
        "links": [{"label": "Doc", "target": "https://example.test/doc"}],
        "action_items": [{"task": "Ship", "owner": "Alice", "due_date": "2026-08-02"}],
        "open_questions": ["What next?"],
        "verification": ["Alice confirmed"],
    }


class SchemaAndPromptTests(unittest.TestCase):
    def test_schema_and_content_validation(self):
        value = meeting()
        self.assertEqual(mt.validate_meeting(value), value)
        invalid = dict(value, extra=True)
        with self.assertRaisesRegex(mt.MeetingError, "Additional properties"):
            mt.validate_meeting(invalid)
        empty = meeting()
        empty["transcript"] = []
        empty["notes"] = []
        with self.assertRaisesRegex(mt.MeetingError, "transcript segments or notes"):
            mt.validate_meeting(empty)

    def test_prompt_is_deterministic_and_excludes_raw(self):
        text = mt.summarization_text(meeting())
        self.assertIn("Mode: improve", text)
        self.assertIn("Alice <alice@example.test>", text)
        self.assertIn("[2026-08-01T09:00:01Z - 2026-08-01T09:00:03Z] Alice", text)
        self.assertIn("Keep this exact.\nSecond line.", text)
        self.assertNotIn("secret_duplicate", text)

    def test_artifact_keys_avoid_shared_cache_collision(self):
        first = mt.artifact_key(meeting("Same"))
        second_meeting = meeting("Same")
        second_meeting["raw"]["received"]["transcript"] = "different source"
        second = mt.artifact_key(second_meeting)
        self.assertNotEqual(first, second)
        self.assertEqual(first, mt.artifact_key(meeting("Same")))


class CommandTests(unittest.TestCase):
    def write_json(self, path: Path, value) -> Path:
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_prepare_persists_source_and_returns_summary_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.write_json(root / "input.json", meeting())
            artifacts = root / "shared"
            output = io.StringIO()
            args = mt.build_parser().parse_args(
                ["prepare", str(source), "--artifacts-dir", str(artifacts)]
            )
            with redirect_stdout(output):
                self.assertEqual(args.func(args), 0)
            packet = json.loads(output.getvalue())
            self.assertTrue(Path(packet["meeting_json"]).is_file())
            self.assertEqual(Path(packet["summary_json"]).parent, Path(packet["meeting_json"]).parent)
            self.assertTrue(str(Path(packet["meeting_json"]).parent).startswith(str(artifacts.resolve())))
            self.assertIn("summary_schema", packet)
            self.assertNotIn("secret_duplicate", packet["user_prompt"])

    def test_import_writes_artifacts_and_default_markdown(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.write_json(root / "meeting.json", meeting())
            summary_path = self.write_json(root / "summary.json", summary())
            out = root / "meeting-folder"
            output = io.StringIO()
            args = mt.build_parser().parse_args(
                ["import", str(source), "--out", str(out), "--summary", str(summary_path)]
            )
            with redirect_stdout(output):
                self.assertEqual(args.func(args), 0)
            result = json.loads(output.getvalue())
            self.assertTrue(Path(result["meeting_json"]).is_file())
            self.assertTrue(Path(result["summary_json"]).is_file())
            transcript = (out / "transcript.md").read_text(encoding="utf-8")
            rendered_summary = (out / "summary.md").read_text(encoding="utf-8")
            self.assertIn("Keep this exact.\nSecond line.", transcript)
            self.assertIn("| Alice | Lead | Owns delivery |", rendered_summary)
            self.assertIn("[[transcript]]", rendered_summary)

    def test_custom_bundle_and_template_overrides(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "prompt.md").write_text("Return custom JSON.", encoding="utf-8")
            (bundle / "summary.schema.json").write_text(
                json.dumps(
                    {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object",
                        "required": ["headline"],
                        "properties": {"headline": {"type": "string"}},
                        "additionalProperties": False,
                    }
                ),
                encoding="utf-8",
            )
            (bundle / "summary.md").write_text("BUNDLE {{headline}}\n", encoding="utf-8")
            transcript_template = root / "transcript.md.tpl"
            transcript_template.write_text("CUSTOM TRANSCRIPT\n{{transcript_body}}\n", encoding="utf-8")
            summary_template = root / "summary.md.tpl"
            summary_template.write_text("OVERRIDE {{headline}}\n", encoding="utf-8")
            source = self.write_json(root / "meeting.json", meeting())
            custom_summary = self.write_json(root / "custom-summary.json", {"headline": "Result"})
            out = root / "out"
            args = mt.build_parser().parse_args(
                [
                    "import", str(source), "--out", str(out), "--summary", str(custom_summary),
                    "--bundle", str(bundle), "--transcript-template", str(transcript_template),
                    "--summary-template", str(summary_template),
                ]
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(args.func(args), 0)
            self.assertIn("CUSTOM TRANSCRIPT", (out / "transcript.md").read_text(encoding="utf-8"))
            self.assertEqual((out / "summary.md").read_text(encoding="utf-8"), "OVERRIDE Result\n")

    def test_environment_selects_bundle_and_templates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "prompt.md").write_text("Prompt", encoding="utf-8")
            (bundle / "summary.schema.json").write_text(
                json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}),
                encoding="utf-8",
            )
            (bundle / "summary.md").write_text("DEFAULT", encoding="utf-8")
            transcript = root / "transcript.tpl"
            transcript.write_text("TRANSCRIPT", encoding="utf-8")
            summary_template = root / "summary.tpl"
            summary_template.write_text("SUMMARY", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "MEETING_TRANSCRIPT_SUMMARY_BUNDLE": str(bundle),
                    "MEETING_TRANSCRIPT_TRANSCRIPT_TEMPLATE": str(transcript),
                    "MEETING_TRANSCRIPT_SUMMARY_TEMPLATE": str(summary_template),
                },
                clear=False,
            ):
                self.assertEqual(mt.resolve_bundle(None), bundle.resolve())
                fake_args = type("Args", (), {"transcript_template": None, "summary_template": None})()
                self.assertEqual(mt.resolved_assets(fake_args, bundle), (transcript.resolve(), summary_template.resolve()))

    def test_custom_renderer_and_unsafe_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin = root / "renderer.py"
            plugin.write_text(
                "def render(meeting, summary, options):\n"
                "    return {'custom.txt': meeting['title'] + ':' + summary['summary']}\n",
                encoding="utf-8",
            )
            source = self.write_json(root / "meeting.json", meeting())
            summary_path = self.write_json(root / "summary.json", summary())
            out = root / "out"
            args = mt.build_parser().parse_args(
                ["import", str(source), "--out", str(out), "--summary", str(summary_path), "--engine", str(plugin)]
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(args.func(args), 0)
            self.assertEqual((out / "custom.txt").read_text(encoding="utf-8"), "Planning:Concise result.")
            with self.assertRaisesRegex(mt.MeetingError, "unsafe renderer output"):
                mt.validate_outputs({"../bad": "x"}, out)


class APITests(unittest.TestCase):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def test_api_request_and_response_validation(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["authorization"] = request.headers["Authorization"]
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return self.Response({"choices": [{"message": {"content": json.dumps(summary())}}]})

        env = {
            "MEETING_TRANSCRIPT_API_BASE": "https://api.example.test/v1",
            "MEETING_TRANSCRIPT_API_KEY": "top-secret",
            "MEETING_TRANSCRIPT_MODEL": "model-1",
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
            mt.urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            result = mt.api_summary(meeting(), mt.DEFAULT_BUNDLE)
        self.assertEqual(result, summary())
        self.assertEqual(captured["url"], "https://api.example.test/v1/chat/completions")
        self.assertEqual(captured["authorization"], "Bearer top-secret")
        self.assertEqual(captured["body"]["response_format"]["type"], "json_schema")
        self.assertTrue(captured["body"]["response_format"]["json_schema"]["strict"])
        self.assertNotIn("secret_duplicate", captured["body"]["messages"][1]["content"])

    def test_import_api_is_one_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "meeting.json"
            source.write_text(json.dumps(meeting()), encoding="utf-8")
            out = root / "out"
            cache = root / "shared-cache"
            env = {
                "MEETING_TRANSCRIPT_API_BASE": "https://api.example.test/v1",
                "MEETING_TRANSCRIPT_API_KEY": "secret",
                "MEETING_TRANSCRIPT_MODEL": "model-1",
            }
            response = self.Response(
                {"choices": [{"message": {"content": json.dumps(summary())}}]}
            )
            args = mt.build_parser().parse_args(
                [
                    "import", str(source), "--out", str(out), "--api",
                    "--artifacts-dir", str(cache),
                ]
            )
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                mt.urllib.request, "urlopen", return_value=response
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(args.func(args), 0)
            self.assertTrue((out / "transcript.md").is_file())
            self.assertTrue((out / "summary.md").is_file())
            self.assertEqual(len(list(cache.glob("*/meeting.json"))), 1)
            self.assertEqual(len(list(cache.glob("*/summary.json"))), 1)

    def test_api_refusal_and_invalid_assistant_json(self):
        env = {
            "MEETING_TRANSCRIPT_API_BASE": "https://api.example.test/v1",
            "MEETING_TRANSCRIPT_API_KEY": "secret",
            "MEETING_TRANSCRIPT_MODEL": "model-1",
        }
        refusal = self.Response({"choices": [{"message": {"refusal": "No"}}]})
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
            mt.urllib.request, "urlopen", return_value=refusal
        ):
            with self.assertRaisesRegex(mt.MeetingError, "refused request"):
                mt.api_summary(meeting(), mt.DEFAULT_BUNDLE)
        malformed = self.Response({"choices": [{"message": {"content": "not-json"}}]})
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
            mt.urllib.request, "urlopen", return_value=malformed
        ):
            with self.assertRaisesRegex(mt.MeetingError, "assistant content is invalid JSON"):
                mt.api_summary(meeting(), mt.DEFAULT_BUNDLE)

    def test_api_missing_configuration_does_not_expose_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(mt.MeetingError, "MEETING_TRANSCRIPT_API_BASE") as raised:
                mt.api_summary(meeting(), mt.DEFAULT_BUNDLE)
        self.assertNotIn("Bearer", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
