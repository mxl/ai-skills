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


def transcript_only_meeting():
    value = meeting()
    value["notes"] = []
    return value


def notes_only_meeting():
    value = meeting()
    value["transcript"] = []
    return value


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
        self.assertIn("**Alice:** Keep this exact.\nSecond line.", text)
        self.assertNotIn("[2026-08-01T09:00:01Z", text)
        self.assertIn("Keep this exact.\nSecond line.", text)
        self.assertNotIn("secret_duplicate", text)

    def test_prompt_and_markdown_share_grouped_transcript_body(self):
        value = meeting()
        value["transcript"] = [
            {
                "started_at": "2026-08-01T09:00:01Z",
                "ended_at": "2026-08-01T09:00:02Z",
                "speaker": "Alice",
                "text": "First. ",
            },
            {
                "started_at": "2026-08-01T09:00:03Z",
                "ended_at": "2026-08-01T09:00:04Z",
                "speaker": "Alice",
                "text": " Second.",
            },
            {
                "started_at": "2026-08-01T09:00:05Z",
                "ended_at": "2026-08-01T09:00:06Z",
                "speaker": "Bob",
                "text": "Reply.",
            },
        ]
        body = "**Alice:** First. Second.\n\n**Bob:** Reply."

        self.assertEqual(mt.transcript_body(value), body)
        self.assertIn(f"Transcript:\n{body}", mt.summarization_text(value))
        self.assertNotIn("2026-08-01T09:00:01Z", mt.transcript_body(value))

    def test_transcript_only_meeting_uses_generate_summary_mode(self):
        value = transcript_only_meeting()
        text = mt.summarization_text(value)
        self.assertEqual(mt.infer_mode(value), "generate-summary")
        self.assertIn("Mode: generate-summary", text)
        self.assertIn("Keep this exact.\nSecond line.", text)
        self.assertNotIn("Provided notes:", text)

    def test_notes_only_meeting_uses_save_mode(self):
        value = notes_only_meeting()
        text = mt.summarization_text(value)
        self.assertEqual(mt.infer_mode(value), "save")
        self.assertEqual(mt.mode_reason(value), "notes_only")
        self.assertIn("Mode: save", text)
        self.assertIn("Provided notes:", text)
        self.assertNotIn("Transcript:", text)

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

    def test_script_is_source_agnostic(self):
        self.assertNotIn("granola", SCRIPT.read_text(encoding="utf-8").casefold())

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
            self.assertEqual(packet["mode_reason"], "transcript_and_notes")

    def test_prepare_transcript_only_returns_generate_summary_packet(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.write_json(root / "input.json", transcript_only_meeting())
            output = io.StringIO()
            args = mt.build_parser().parse_args(["prepare", str(source)])
            with redirect_stdout(output):
                self.assertEqual(args.func(args), 0)
            packet = json.loads(output.getvalue())
            self.assertEqual(packet["mode"], "generate-summary")
            self.assertEqual(packet["mode_reason"], "transcript_only")
            self.assertIn("Keep this exact.\nSecond line.", packet["user_prompt"])
            self.assertNotIn("Provided notes:", packet["user_prompt"])
            self.assertEqual(Path(packet["meeting_json"]).parent.parent, root.resolve())

    def test_prepare_improve_packet_separates_draft_and_notes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = meeting()
            value["notes"] = [{"title": "Provided summary", "text": "Prior claim."}]
            source = self.write_json(root / "input.json", value)
            output = io.StringIO()
            args = mt.build_parser().parse_args(["prepare", str(source)])
            with redirect_stdout(output):
                self.assertEqual(args.func(args), 0)
            packet = json.loads(output.getvalue())
            self.assertEqual(packet["mode"], "improve")
            self.assertEqual(packet["mode_reason"], "transcript_and_notes")
            self.assertEqual(packet["user_prompt"], packet["draft_prompt"])
            self.assertIn("Keep this exact.\nSecond line.", packet["draft_prompt"])
            self.assertNotIn("Prior claim.", packet["draft_prompt"])
            self.assertIn("### Provided summary\nPrior claim.", packet["reconcile_prompt"])
            self.assertIn("Keep this exact.\nSecond line.", packet["reconcile_prompt"])
            self.assertNotIn("secret_duplicate", json.dumps(packet))

    def test_prepare_runs_external_adapter(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.write_json(root / "source.json", {"canonical": meeting()})
            adapter = root / "adapter.py"
            adapter.write_text(
                "import argparse, json\n"
                "from pathlib import Path\n"
                "parser = argparse.ArgumentParser()\n"
                "parser.add_argument('source')\n"
                "parser.add_argument('--out', required=True)\n"
                "parser.add_argument('--schema', required=True)\n"
                "args = parser.parse_args()\n"
                "payload = json.loads(Path(args.source).read_text())['canonical']\n"
                "Path(args.out).write_text(json.dumps(payload))\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            args = mt.build_parser().parse_args(
                ["prepare", str(source), "--adapter", str(adapter)]
            )
            with redirect_stdout(output):
                self.assertEqual(args.func(args), 0)
            packet = json.loads(output.getvalue())
            canonical = json.loads(Path(packet["meeting_json"]).read_text(encoding="utf-8"))
            self.assertEqual(canonical, meeting())

    def test_prepare_rejects_invalid_adapter_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.write_json(root / "source.json", {"value": 1})
            adapter = root / "adapter.py"
            adapter.write_text(
                "import argparse\n"
                "from pathlib import Path\n"
                "parser = argparse.ArgumentParser()\n"
                "parser.add_argument('source')\n"
                "parser.add_argument('--out', required=True)\n"
                "parser.add_argument('--schema', required=True)\n"
                "args = parser.parse_args()\n"
                "Path(args.out).write_text('{}')\n",
                encoding="utf-8",
            )
            args = mt.build_parser().parse_args(
                ["prepare", str(source), "--adapter", str(adapter)]
            )
            with self.assertRaisesRegex(mt.MeetingError, "meeting validation failed"):
                args.func(args)

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
            self.assertEqual(result["status"], "created")
            self.assertTrue(Path(result["meeting_json"]).is_file())
            self.assertTrue(Path(result["summary_json"]).is_file())
            transcript = (out / "transcript.md").read_text(encoding="utf-8")
            transcript_json = out / "transcript.json"
            rendered_summary = (out / "summary.md").read_text(encoding="utf-8")
            self.assertIn("Keep this exact.\nSecond line.", transcript)
            self.assertIn("- Started: 2026-08-01T09:00:00Z", transcript)
            self.assertNotIn("[2026-08-01T09:00:01Z", transcript)
            self.assertEqual(transcript_json.read_bytes(), source.read_bytes())
            self.assertIn(str(transcript_json.resolve()), result["outputs"])
            self.assertEqual(result["transcript_segments"], 1)
            self.assertEqual(result["speakers"], ["Alice"])
            self.assertEqual(result["unknown_speakers"], 0)
            self.assertEqual(result["action_items"], summary()["action_items"])
            self.assertEqual(result["entity_count"], 1)
            self.assertEqual(result["link_count"], 1)
            self.assertIn("| Alice | Lead | Owns delivery |", rendered_summary)
            self.assertIn("[[transcript]]", rendered_summary)

    def test_identical_import_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.write_json(root / "meeting.json", meeting())
            summary_path = self.write_json(root / "summary.json", summary())
            out = root / "meeting-folder"

            first_output = io.StringIO()
            first_args = mt.build_parser().parse_args(
                ["import", str(source), "--out", str(out), "--summary", str(summary_path)]
            )
            with redirect_stdout(first_output):
                self.assertEqual(first_args.func(first_args), 0)
            mtimes = {path: path.stat().st_mtime_ns for path in out.rglob("*") if path.is_file()}

            second_output = io.StringIO()
            second_args = mt.build_parser().parse_args(
                ["import", str(source), "--out", str(out), "--summary", str(summary_path)]
            )
            with redirect_stdout(second_output):
                self.assertEqual(second_args.func(second_args), 0)
            result = json.loads(second_output.getvalue())

            self.assertEqual(result["status"], "unchanged")
            self.assertEqual(result["changed_outputs"], [])
            self.assertEqual(
                mtimes,
                {path: path.stat().st_mtime_ns for path in out.rglob("*") if path.is_file()},
            )

    def test_changed_summary_writes_only_changed_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.write_json(root / "meeting.json", meeting())
            summary_value = summary()
            summary_path = self.write_json(root / "summary.json", summary_value)
            out = root / "meeting-folder"
            args = mt.build_parser().parse_args(
                ["import", str(source), "--out", str(out), "--summary", str(summary_path)]
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(args.func(args), 0)

            summary_value["summary"] = "Changed result."
            self.write_json(summary_path, summary_value)
            output = io.StringIO()
            args = mt.build_parser().parse_args(
                ["import", str(source), "--out", str(out), "--summary", str(summary_path)]
            )
            with redirect_stdout(output):
                self.assertEqual(args.func(args), 0)
            result = json.loads(output.getvalue())

            self.assertEqual(result["status"], "updated")
            self.assertEqual(result["changed_outputs"], [str((out / "summary.md").resolve())])

    def test_import_notes_only_renders_placeholder_transcript(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.write_json(root / "meeting.json", notes_only_meeting())
            summary_path = self.write_json(root / "summary.json", summary())
            out = root / "meeting-folder"
            args = mt.build_parser().parse_args(
                ["import", str(source), "--out", str(out), "--summary", str(summary_path)]
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(args.func(args), 0)
            self.assertIn("_No transcript was provided._", (out / "transcript.md").read_text(encoding="utf-8"))
            self.assertIn("Concise result.", (out / "summary.md").read_text(encoding="utf-8"))

    def test_import_omits_empty_optional_sections_and_keeps_action_items(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.write_json(root / "meeting.json", transcript_only_meeting())
            empty_sections_summary = summary()
            empty_sections_summary.update(
                {"entities": [], "links": [], "action_items": [], "verification": []}
            )
            summary_path = self.write_json(root / "summary.json", empty_sections_summary)
            out = root / "meeting-folder"
            args = mt.build_parser().parse_args(
                ["import", str(source), "--out", str(out), "--summary", str(summary_path)]
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(args.func(args), 0)
            rendered = (out / "summary.md").read_text(encoding="utf-8")
            self.assertIn("## Context", rendered)
            self.assertIn("## Open Questions", rendered)
            self.assertNotIn("## Entities", rendered)
            self.assertNotIn("## Links And Resources", rendered)
            self.assertNotIn("## Verification", rendered)
            self.assertIn("## Action Items\n\n_None._\n\n## Open Questions", rendered)

    def test_import_uses_default_artifact_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = transcript_only_meeting()
            source = self.write_json(root / "meeting.json", value)
            summary_path = self.write_json(root / "summary.json", summary())
            out = root / "meeting-folder"
            output = io.StringIO()
            args = mt.build_parser().parse_args(
                ["import", str(source), "--out", str(out), "--summary", str(summary_path)]
            )
            with redirect_stdout(output):
                self.assertEqual(args.func(args), 0)
            result = json.loads(output.getvalue())
            expected = (out / ".meeting-transcript" / mt.artifact_key(value)).resolve()
            self.assertEqual(Path(result["meeting_json"]), expected / "meeting.json")
            self.assertEqual(Path(result["summary_json"]), expected / "summary.json")
            self.assertTrue((out / "transcript.md").is_file())
            self.assertTrue((out / "summary.md").is_file())
            self.assertTrue((out / "transcript.json").is_file())

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

    def test_renderer_cannot_overwrite_transcript_json(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin = root / "renderer.py"
            plugin.write_text(
                "def render(meeting, summary, options):\n"
                "    return {'transcript.json': 'changed'}\n",
                encoding="utf-8",
            )
            source = self.write_json(root / "meeting.json", meeting())
            summary_path = self.write_json(root / "summary.json", summary())
            args = mt.build_parser().parse_args(
                [
                    "import", str(source), "--out", str(root / "out"),
                    "--summary", str(summary_path), "--engine", str(plugin),
                ]
            )

            with self.assertRaisesRegex(mt.MeetingError, "reserved path: transcript.json"):
                args.func(args)


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
        captured = []

        def fake_urlopen(request, timeout):
            captured.append(
                {
                    "url": request.full_url,
                    "authorization": request.headers["Authorization"],
                    "body": json.loads(request.data),
                    "timeout": timeout,
                }
            )
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
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0]["url"], "https://api.example.test/v1/chat/completions")
        self.assertEqual(captured[0]["authorization"], "Bearer top-secret")
        self.assertEqual(captured[0]["body"]["response_format"]["type"], "json_schema")
        self.assertTrue(captured[0]["body"]["response_format"]["json_schema"]["strict"])
        self.assertNotIn("Check this note.", captured[0]["body"]["messages"][1]["content"])
        self.assertIn("Check this note.", captured[1]["body"]["messages"][1]["content"])
        self.assertIn("Keep this exact.\nSecond line.", captured[1]["body"]["messages"][1]["content"])
        self.assertIn("Draft summary:", captured[1]["body"]["messages"][1]["content"])
        self.assertNotIn("secret_duplicate", json.dumps(captured))

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
            ) as urlopen, redirect_stdout(io.StringIO()):
                self.assertEqual(args.func(args), 0)
            self.assertEqual(urlopen.call_count, 2)
            self.assertTrue((out / "transcript.md").is_file())
            self.assertTrue((out / "summary.md").is_file())
            self.assertEqual(len(list(cache.glob("*/meeting.json"))), 1)
            self.assertEqual(len(list(cache.glob("*/summary.json"))), 1)

    def test_generate_summary_api_uses_one_request(self):
        env = {
            "MEETING_TRANSCRIPT_API_BASE": "https://api.example.test/v1",
            "MEETING_TRANSCRIPT_API_KEY": "secret",
            "MEETING_TRANSCRIPT_MODEL": "model-1",
        }
        response = self.Response(
            {"choices": [{"message": {"content": json.dumps(summary())}}]}
        )
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
            mt.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            self.assertEqual(mt.api_summary(transcript_only_meeting(), mt.DEFAULT_BUNDLE), summary())
        self.assertEqual(urlopen.call_count, 1)

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
