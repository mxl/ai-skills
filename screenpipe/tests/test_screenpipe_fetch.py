import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "screenpipe_fetch.py"
sys.path.insert(0, str(SCRIPT.parent))

import screenpipe_fetch as fetcher  # noqa: E402


def meeting(meeting_id=42, title="Planning"):
    return {
        "id": meeting_id,
        "meeting_start": "2026-08-02T09:00:00Z",
        "meeting_end": "2026-08-02T10:00:00Z",
        "meeting_app": "zoom.us",
        "title": title,
        "attendees": "Alice; Bob",
        "note": "Decision",
        "detection_source": "calendar",
        "created_at": "2026-08-02T08:59:00Z",
    }


def transcript(meeting_id=42):
    return [{
        "id": 1,
        "meetingId": meeting_id,
        "source": "live",
        "provider": "deepgram",
        "model": None,
        "itemId": "one",
        "deviceName": "Microphone",
        "deviceType": "input",
        "audioTranscriptionId": None,
        "audioChunkId": 10,
        "audioFilePath": "/tmp/audio.mp4",
        "speakerId": None,
        "speakerName": None,
        "transcript": " exact ",
        "capturedAt": "2026-08-02T09:00:01Z",
        "createdAt": "2026-08-02T09:00:02Z",
    }]


class FakeScreenpipe:
    def __init__(self):
        self.requests = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                parent.requests.append((parsed.path, parse_qs(parsed.query), self.headers.get("Authorization")))
                if parsed.path == "/health":
                    return self.send_json({"status": "ok"})
                if self.headers.get("Authorization") != "Bearer test-key":
                    return self.send_json({"error": "forbidden"}, status=403)
                if parsed.path == "/meetings":
                    return self.send_json([meeting(), meeting(43, "Review")])
                if parsed.path == "/meetings/42":
                    return self.send_json(meeting())
                if parsed.path == "/meetings/42/transcript":
                    return self.send_json(transcript())
                if parsed.path == "/meetings/43":
                    return self.send_json(meeting(43, "Review"))
                if parsed.path == "/meetings/43/transcript":
                    return self.send_json(transcript(43))
                return self.send_json({"error": "missing"}, status=404)

            def send_json(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def api_base(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class UnitTests(unittest.TestCase):
    def test_default_range_and_output_stem(self):
        now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(fetcher.default_range(now), (
            "2026-08-01T12:00:00Z", "2026-08-02T12:00:00Z",
        ))
        self.assertEqual(fetcher.output_stem(meeting()), "2026-08-02-planning-42")

    def test_selection(self):
        meetings = [meeting(), meeting(43, "Review")]
        output = io.StringIO()
        selected = fetcher.select_meetings(meetings, False, input_fn=lambda _: "2,1,2", output=output)
        self.assertEqual([item["id"] for item in selected], [43, 42])
        self.assertIn("Planning", output.getvalue())
        with self.assertRaisesRegex(fetcher.FetchError, "out of range"):
            fetcher.select_meetings(meetings, False, input_fn=lambda _: "3", output=output)

    def test_source_envelope_and_export(self):
        with tempfile.TemporaryDirectory() as temp:
            envelope = fetcher.source_envelope(meeting(), transcript(), "2026-08-02T11:00:00Z")
            self.assertEqual(envelope["transcript"][0]["transcript"], " exact ")
            schema = fetcher.load_schema(fetcher.default_schema_path())
            raw_path, canonical_path = fetcher.export_meeting(
                meeting(), transcript(), Path(temp), schema,
            )
            self.assertTrue(raw_path.exists())
            self.assertTrue(canonical_path.exists())
            canonical = json.loads(canonical_path.read_text())
            self.assertEqual(canonical["raw"]["meeting"]["id"], 42)
            self.assertEqual(canonical["transcript"][0]["text"], " exact ")

    def test_missing_key_message_does_not_leak(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(fetcher.FetchError, "auth token"):
                fetcher.api_key_from_env()


class CLITests(unittest.TestCase):
    def run_cli(self, api_base, *args, input_text=None, include_key=True):
        env = os.environ.copy()
        if include_key:
            env["SCREENPIPE_LOCAL_API_KEY"] = "test-key"
        else:
            env.pop("SCREENPIPE_LOCAL_API_KEY", None)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--api-base", api_base, *map(str, args)],
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    def test_explicit_id_writes_raw_and_canonical(self):
        with FakeScreenpipe() as api, tempfile.TemporaryDirectory() as temp:
            result = self.run_cli(api.api_base, "--meeting-id", 42, "--out", temp)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = sorted(Path(temp).iterdir())
            self.assertEqual([path.name for path in outputs], [
                "2026-08-02-planning-42.meeting-transcript.json",
                "2026-08-02-planning-42.screenpipe.json",
            ])
            self.assertNotIn("test-key", result.stdout + result.stderr)
            authenticated = [request for request in api.requests if request[0] != "/health"]
            self.assertTrue(all(request[2] == "Bearer test-key" for request in authenticated))

    def test_range_query_interactive_selection(self):
        with FakeScreenpipe() as api, tempfile.TemporaryDirectory() as temp:
            result = self.run_cli(
                api.api_base,
                "--from", "2026-08-01T00:00:00Z",
                "--to", "2026-08-03T00:00:00Z",
                "--query", "plan",
                "--out", temp,
                input_text="1\n",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            meeting_requests = [request for request in api.requests if request[0] == "/meetings"]
            self.assertEqual(len(meeting_requests), 1)
            params = meeting_requests[0][1]
            self.assertEqual(params["start_time"], ["2026-08-01T00:00:00Z"])
            self.assertEqual(params["end_time"], ["2026-08-03T00:00:00Z"])
            self.assertEqual(params["q"], ["plan"])
            self.assertTrue((Path(temp) / "2026-08-02-planning-42.screenpipe.json").exists())
            self.assertFalse((Path(temp) / "2026-08-02-review-43.screenpipe.json").exists())

    def test_all_exports_every_match(self):
        with FakeScreenpipe() as api, tempfile.TemporaryDirectory() as temp:
            result = self.run_cli(api.api_base, "--days", 2, "--all", "--out", temp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(list(Path(temp).glob("*.screenpipe.json"))), 2)
            self.assertEqual(len(list(Path(temp).glob("*.meeting-transcript.json"))), 2)

    def test_missing_key_and_argument_errors(self):
        with FakeScreenpipe() as api, tempfile.TemporaryDirectory() as temp:
            result = self.run_cli(api.api_base, "--meeting-id", 42, "--out", temp, include_key=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("SCREENPIPE_LOCAL_API_KEY", result.stderr)

            result = self.run_cli(api.api_base, "--from", "2026-08-01", "--out", temp)
            self.assertEqual(result.returncode, 2)
            self.assertIn("must be used together", result.stderr)


if __name__ == "__main__":
    unittest.main()
