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


def summary(rules=None, references=None, meeting_value=None):
    meeting_value = meeting_value or meeting()
    references = references or []
    return {
        "meeting_sha256": mt.sha256_bytes(mt.canonical_bytes(meeting_value)),
        "context": "Planning context.",
        "summary": "Concise result.",
        "key_points": ["Point one"],
        "decisions": ["Ship it"],
        "entities": [{"name": "Alice", "role": "Lead", "facts": ["Owns delivery"]}],
        "links": [{"label": "Doc", "target": "https://example.test/doc"}],
        "action_items": [
            {
                "task": "Ship",
                "owner": "Alice",
                "due_date": "2026-08-02",
                "status": "open",
                "todoist_id": "",
                "todoist_url": "",
            }
        ],
        "open_questions": ["What next?"],
        "verification": ["Alice confirmed"],
        "transcript_findings": [
            {
                "segment_index": 0,
                "category": "entity",
                "source_text": "Keep this exact.",
                "interpretation": "Keep this exact.",
                "status": "reference_confirmed" if references else "user_confirmed",
                "impact": "Preserves the named item used in the decision.",
                "reference_path": references[0]["path"] if references else "",
                "open_question": "",
                "user_resolution_index": -1 if references else 0,
            }
        ],
        "reference_sources": references,
        "user_resolutions": [
            {
                "question": "Should this material phrase be interpreted literally?",
                "answer": "Yes",
                "affected_field": "transcript_findings[0].interpretation"
            }
        ],
        "summarization_rules": rules or {"path": "", "sha256": ""},
        "summarization_rule_suggestions": [],
    }


class MeetingTranscriptTests(unittest.TestCase):
    def write_json(self, path: Path, value) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def run_args(self, values):
        output = io.StringIO()
        args = mt.build_parser().parse_args(values)
        with redirect_stdout(output):
            self.assertEqual(args.func(args), 0)
        return json.loads(output.getvalue())

    def prepare_packet(self, transcript: Path, bundle: Path | None = None):
        values = ["summarize", "prepare", str(transcript)]
        if bundle:
            values.extend(["--bundle", str(bundle)])
        return self.run_args(values)

    def apply_args(
        self,
        transcript: Path,
        summary_path: Path,
        *,
        bundle: Path | None = None,
        summary_template: Path | None = None,
        packet=None,
    ):
        packet = packet or self.prepare_packet(transcript, bundle)
        values = [
            "summarize", "apply", str(transcript),
            "--summary", str(summary_path),
            "--prepare-manifest", packet["prepare_manifest"],
        ]
        if bundle:
            values.extend(["--bundle", str(bundle)])
        if summary_template:
            values.extend(["--summary-template", str(summary_template)])
        return mt.build_parser().parse_args(values)

    def apply_summary(self, transcript: Path, summary_path: Path, **kwargs):
        output = io.StringIO()
        args = self.apply_args(transcript, summary_path, **kwargs)
        with redirect_stdout(output):
            self.assertEqual(args.func(args), 0)
        return json.loads(output.getvalue())

    def test_schema_validation_and_source_agnostic_script(self):
        value = meeting()
        self.assertEqual(mt.validate_meeting(value), value)
        with self.assertRaisesRegex(mt.MeetingError, "Additional properties"):
            mt.validate_meeting(dict(value, extra=True))
        self.assertNotIn("granola", SCRIPT.read_text(encoding="utf-8").casefold())

    def test_import_writes_only_canonical_transcript(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.write_json(root / "source.json", meeting())
            out = root / "meeting"
            result = self.run_args(["import", str(source), "--out", str(out)])

            self.assertEqual(result["phase"], "import")
            self.assertEqual(result["status"], "created")
            self.assertEqual(result["outputs"], [str((out / "transcript.json").resolve())])
            self.assertEqual(json.loads((out / "transcript.json").read_text()), meeting())
            self.assertFalse((out / "meeting.json").exists())
            self.assertFalse((out / "transcript.md").exists())
            self.assertFalse((out / ".meeting-transcript").exists())

    def test_identical_import_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.write_json(root / "source.json", meeting())
            out = root / "meeting"
            self.run_args(["import", str(source), "--out", str(out)])
            before = (out / "transcript.json").stat().st_mtime_ns
            result = self.run_args(["import", str(source), "--out", str(out)])
            self.assertEqual(result["status"], "unchanged")
            self.assertEqual(result["changed_outputs"], [])
            self.assertEqual((out / "transcript.json").stat().st_mtime_ns, before)

    def test_import_runs_adapter_without_persisting_adapter_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.write_json(root / "source.json", {"canonical": meeting()})
            adapter = root / "adapter.py"
            adapter.write_text(
                "import argparse, json\n"
                "from pathlib import Path\n"
                "p=argparse.ArgumentParser(); p.add_argument('source'); p.add_argument('--out', required=True); p.add_argument('--schema', required=True)\n"
                "a=p.parse_args(); Path(a.out).write_text(json.dumps(json.loads(Path(a.source).read_text())['canonical']))\n",
                encoding="utf-8",
            )
            out = root / "meeting"
            self.run_args(["import", str(source), "--out", str(out), "--adapter", str(adapter)])
            self.assertEqual(json.loads((out / "transcript.json").read_text()), meeting())
            self.assertEqual(list(out.glob("*")), [out / "transcript.json"])

    def test_invalid_adapter_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.write_json(root / "source.json", {"value": 1})
            adapter = root / "adapter.py"
            adapter.write_text(
                "import argparse\nfrom pathlib import Path\n"
                "p=argparse.ArgumentParser(); p.add_argument('source'); p.add_argument('--out', required=True); p.add_argument('--schema', required=True)\n"
                "a=p.parse_args(); Path(a.out).write_text('{}')\n",
                encoding="utf-8",
            )
            out = root / "meeting"
            args = mt.build_parser().parse_args(
                ["import", str(source), "--out", str(out), "--adapter", str(adapter)]
            )
            with self.assertRaisesRegex(mt.MeetingError, "meeting validation failed"):
                args.func(args)
            self.assertFalse(out.exists())

    def test_prepare_packet_has_stable_segments_and_excludes_raw(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = self.write_json(root / "transcript.json", meeting())
            packet = self.run_args(["summarize", "prepare", str(transcript)])
            self.assertEqual(packet["operation"], "prepare")
            self.assertIn("[S00000] Alice: Keep this exact.\nSecond line.", packet["user_prompt"])
            self.assertNotIn("Check this note.", packet["draft_prompt"])
            self.assertIn("### Provided\nCheck this note.", packet["reconcile_prompt"])
            self.assertNotIn("secret_duplicate", json.dumps(packet))
            self.assertEqual(packet["summarization_rules"], {"path": "", "sha256": ""})
            self.assertEqual(packet["summary_json"], str((root / "summary.json").resolve()))
            self.assertNotEqual(packet["summary_draft_json"], packet["summary_json"])
            self.assertTrue(Path(packet["summary_draft_json"]).parent.is_dir())

    def test_prepare_loads_relative_markdown_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = self.write_json(root / "transcript.json", meeting())
            rules = root / "rules.md"
            rules.write_text("Focus on operational risks.\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"MEETING_TRANSCRIPT_SUMMARY_RULES": "rules.md"}), mock.patch.object(
                Path, "cwd", return_value=root
            ):
                packet = self.run_args(["summarize", "prepare", str(transcript)])
            self.assertIn("Focus on operational risks.", packet["user_prompt"])
            self.assertEqual(packet["summarization_rules"]["path"], str(rules.resolve()))
            self.assertEqual(packet["summarization_rules"]["sha256"], mt.sha256_path(rules))

    def test_prepare_rejects_invalid_rules_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = self.write_json(root / "transcript.json", meeting())
            rules = root / "rules.txt"
            rules.write_text("Rules", encoding="utf-8")
            with mock.patch.dict(os.environ, {"MEETING_TRANSCRIPT_SUMMARY_RULES": str(rules)}):
                args = mt.build_parser().parse_args(["summarize", "prepare", str(transcript)])
                with self.assertRaisesRegex(mt.MeetingError, "Markdown file"):
                    args.func(args)

    def test_apply_renders_complete_summary_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = self.write_json(root / "transcript.json", meeting())
            reference = root / "people.yaml"
            reference.write_text("Alice: Lead\n", encoding="utf-8")
            references = [{"path": str(reference), "sha256": mt.sha256_path(reference)}]
            summary_path = self.write_json(root / "draft.json", summary(references=references))
            result = self.apply_summary(transcript, summary_path)

            self.assertEqual(result["status"], "created")
            rendered = (root / "summary.md").read_text(encoding="utf-8")
            self.assertIn("[transcript.json](./transcript.json)", rendered)
            self.assertIn("## Transcript Findings", rendered)
            self.assertIn("S00000", rendered)
            self.assertIn("## User Resolutions", rendered)
            self.assertIn("## Reference Sources", rendered)
            self.assertIn("| Ship | Alice | 2026-08-02 | open |", rendered)
            self.assertFalse((root / "transcript.md").exists())
            self.assertEqual(json.loads((root / "summary.json").read_text()), summary(references=references))

    def test_apply_is_unchanged_and_updates_only_summary_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = self.write_json(root / "transcript.json", meeting())
            draft = self.write_json(root / "draft.json", summary())
            packet = self.prepare_packet(transcript)
            self.apply_summary(transcript, draft, packet=packet)
            unchanged = self.apply_summary(transcript, draft, packet=packet)
            self.assertEqual(unchanged["status"], "unchanged")

            changed_summary = summary()
            changed_summary["summary"] = "Changed."
            self.write_json(draft, changed_summary)
            changed = self.apply_summary(transcript, draft, packet=packet)
            self.assertEqual(changed["status"], "updated")
            self.assertEqual(
                set(changed["changed_outputs"]),
                {str((root / "summary.json").resolve()), str((root / "summary.md").resolve())},
            )

    def test_apply_rejects_missing_segment_and_changed_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = self.write_json(root / "transcript.json", meeting())
            value = summary()
            value["transcript_findings"][0]["segment_index"] = 9
            draft = self.write_json(root / "draft.json", value)
            args = self.apply_args(transcript, draft)
            with self.assertRaisesRegex(mt.MeetingError, "missing segment"):
                args.func(args)

            reference = root / "people.yaml"
            reference.write_text("Alice: Lead\n", encoding="utf-8")
            value = summary(references=[{"path": str(reference), "sha256": "0" * 64}])
            self.write_json(draft, value)
            with self.assertRaisesRegex(mt.MeetingError, "changed during summarization"):
                args.func(args)

    def test_apply_requires_matching_rules_provenance_and_suggestions_need_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = self.write_json(root / "transcript.json", meeting())
            value = summary()
            value["summarization_rule_suggestions"] = [
                {
                    "proposed_markdown": "- Prefer risks.",
                    "rationale": "Reusable",
                    "status": "proposed",
                    "resulting_sha256": "",
                }
            ]
            draft = self.write_json(root / "draft.json", value)
            args = self.apply_args(transcript, draft)
            with self.assertRaisesRegex(mt.MeetingError, "require MEETING_TRANSCRIPT_SUMMARY_RULES"):
                args.func(args)

            rules = root / "rules.md"
            rules.write_text("Focus on risks.\n", encoding="utf-8")
            value["summarization_rules"] = {"path": str(rules), "sha256": mt.sha256_path(rules)}
            self.write_json(draft, value)
            with mock.patch.dict(os.environ, {"MEETING_TRANSCRIPT_SUMMARY_RULES": str(rules)}):
                result = self.apply_summary(transcript, draft)
            self.assertEqual(result["rule_suggestions"][0]["status"], "proposed")
            self.assertIn("## Suggested Rule Improvements", (root / "summary.md").read_text())

    def test_apply_rejects_stale_meeting_and_inconsistent_cross_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = meeting()
            transcript = self.write_json(root / "transcript.json", original)
            value = summary(meeting_value=original)
            draft = self.write_json(root / "draft.json", value)
            packet = self.prepare_packet(transcript)

            changed_meeting = meeting(text="Changed transcript.")
            self.write_json(transcript, changed_meeting)
            args = self.apply_args(transcript, draft, packet=packet)
            with self.assertRaisesRegex(mt.MeetingError, "changed after summarize prepare"):
                args.func(args)

            self.write_json(transcript, original)
            value = summary(meeting_value=original)
            value["action_items"][0].update(
                {"status": "todoist_created", "todoist_id": "", "todoist_url": ""}
            )
            self.write_json(draft, value)
            with self.assertRaisesRegex(mt.MeetingError, "Todoist ID and URL"):
                args.func(args)

            value = summary(meeting_value=original)
            value["transcript_findings"][0]["status"] = "unresolved"
            value["transcript_findings"][0]["open_question"] = "Which item was meant?"
            value["transcript_findings"][0]["user_resolution_index"] = -1
            value["open_questions"] = []
            self.write_json(draft, value)
            with self.assertRaisesRegex(mt.MeetingError, "must name its open question"):
                args.func(args)

    def test_reference_confirmed_finding_requires_reference_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = self.write_json(root / "transcript.json", meeting())
            value = summary()
            value["transcript_findings"][0]["status"] = "reference_confirmed"
            draft = self.write_json(root / "draft.json", value)
            args = self.apply_args(transcript, draft)
            with self.assertRaisesRegex(mt.MeetingError, "must name a used reference source"):
                args.func(args)

    def test_user_confirmed_finding_requires_exact_resolution_index(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = self.write_json(root / "transcript.json", meeting())
            value = summary()
            value["transcript_findings"][0]["user_resolution_index"] = 9
            draft = self.write_json(root / "draft.json", value)
            args = self.apply_args(transcript, draft)
            with self.assertRaisesRegex(mt.MeetingError, "must name its user resolution"):
                args.func(args)

    def test_applied_rule_suggestion_accepts_resulting_file_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = self.write_json(root / "transcript.json", meeting())
            rules = root / "rules.md"
            rules.write_text("Focus on risks.\n", encoding="utf-8")
            original_rules = {"path": str(rules.resolve()), "sha256": mt.sha256_path(rules)}
            value = summary(rules=original_rules)
            with mock.patch.dict(os.environ, {"MEETING_TRANSCRIPT_SUMMARY_RULES": str(rules)}):
                packet = self.prepare_packet(transcript)
            rules.write_text("Focus on risks.\n- Include dependencies.\n", encoding="utf-8")
            value["summarization_rule_suggestions"] = [
                {
                    "proposed_markdown": "- Include dependencies.",
                    "rationale": "Reusable project need",
                    "status": "applied",
                    "resulting_sha256": mt.sha256_path(rules),
                }
            ]
            draft = self.write_json(root / "draft.json", value)
            with mock.patch.dict(os.environ, {"MEETING_TRANSCRIPT_SUMMARY_RULES": str(rules)}):
                result = self.apply_summary(transcript, draft, packet=packet)
            self.assertEqual(result["status"], "created")
            self.assertIn(mt.sha256_path(rules), (root / "summary.md").read_text())

    def test_custom_bundle_and_summary_template(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "prompt.md").write_text("Custom prompt", encoding="utf-8")
            (bundle / "summary.schema.json").write_text(
                json.dumps(
                    {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object",
                        "required": ["meeting_sha256", "headline"],
                        "properties": {
                            "meeting_sha256": {"type": "string"},
                            "headline": {"type": "string"}
                        },
                        "additionalProperties": False,
                    }
                ),
                encoding="utf-8",
            )
            (bundle / "summary.md").write_text("DEFAULT {{headline}}\n", encoding="utf-8")
            override = root / "override.md"
            override.write_text("OVERRIDE {{headline}}\n", encoding="utf-8")
            transcript = self.write_json(root / "transcript.json", meeting())
            draft = self.write_json(
                root / "draft.json",
                {"meeting_sha256": mt.sha256_bytes(mt.canonical_bytes(meeting())), "headline": "Result"},
            )
            result = self.apply_summary(
                transcript, draft, bundle=bundle, summary_template=override
            )
            self.assertEqual(result["status"], "created")
            self.assertEqual((root / "summary.md").read_text(), "OVERRIDE Result\n")

            default_packet = self.prepare_packet(transcript)
            args = self.apply_args(
                transcript,
                draft,
                bundle=bundle,
                summary_template=override,
                packet=default_packet,
            )
            with self.assertRaisesRegex(mt.MeetingError, "different summary bundle"):
                args.func(args)


if __name__ == "__main__":
    unittest.main()
