from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import import_channel as importer


class ImporterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_path = Path(__file__).parent / "fixtures" / "messages.json"
        cls.messages = [importer.normalize_fixture_message(item) for item in json.loads(fixture_path.read_text())]

    def test_root_filter_preserves_posts_only(self):
        roots = [item for item in self.messages if importer.message_is_root(item)]
        self.assertEqual([item["message_id"] for item in roots], [1, 2])

    def test_media_embeds_default_to_obsidian_style(self):
        self.assertEqual(importer.embed_for("media/photo.jpg"), "![[media/photo.jpg]]")
        self.assertEqual(importer.embed_for("media/video.mp4"), "![[media/video.mp4]]")
        self.assertEqual(importer.embed_for("media/audio.ogg"), "![[media/audio.ogg]]")
        self.assertEqual(importer.embed_for("media/archive.zip"), "[archive.zip](media/archive.zip)")
        self.assertEqual(
            importer.embed_for("media/video.mp4", "sources/telegram/example"),
            "![[sources/telegram/example/media/video.mp4]]",
        )

    def test_media_embeds_support_markdown_style(self):
        self.assertEqual(
            importer.embed_for("media/photo.jpg", link_style="markdown"),
            "![photo](media/photo.jpg)",
        )
        self.assertEqual(
            importer.embed_for("media/archive.zip", link_style="markdown"),
            "[archive.zip](media/archive.zip)",
        )

    def test_media_embeds_support_no_style(self):
        self.assertEqual(importer.embed_for("media/photo.jpg", link_style="none"), "media/photo.jpg")
        self.assertEqual(
            importer.embed_for("media/photo.jpg", "sources/telegram/example", link_style="none"),
            "sources/telegram/example/media/photo.jpg",
        )

    def test_webpage_preview_is_not_downloadable_media(self):
        class MessageMediaWebPage:
            pass

        class Message:
            id = 7
            media = MessageMediaWebPage()
            file = None
            sender = None
            date = "2026-01-01T00:00:00+00:00"
            edit_date = None
            message = "https://example.com"
            sender_id = None
            grouped_id = None
            reply_to = None
            views = 0

        normalized = importer.message_from_telethon(Message(), -1, None)
        self.assertEqual(normalized["media"][0]["type"], "webpage")
        self.assertFalse(normalized["media"][0]["downloadable"])

    def test_poll_is_not_downloadable_media(self):
        class MessageMediaPoll:
            pass

        class Message:
            id = 8
            media = MessageMediaPoll()
            file = None
            sender = None
            date = "2026-01-01T00:00:00+00:00"
            edit_date = None
            message = ""
            sender_id = None
            grouped_id = None
            reply_to = None
            views = 0

        normalized = importer.message_from_telethon(Message(), -1, None)
        self.assertEqual(normalized["media"][0]["type"], "poll")
        self.assertFalse(normalized["media"][0]["downloadable"])

    def test_rendered_post_contains_ordered_embeds(self):
        post = [item for item in self.messages if item["message_id"] == 2][0]
        media = [
            {"media_id": "2", "type": "video", "embed": "![[media/video.mp4]]"},
            {"media_id": "3", "type": "audio", "embed": "![[media/audio.ogg]]"},
        ]
        rendered = importer.render_post(post, "channel.md", media, [])
        self.assertLess(rendered.index("media/video.mp4"), rendered.index("media/audio.ogg"))
        self.assertIn("Второй пост", rendered)

    def test_output_dir_is_contained(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(
                importer.validate_output_dir(root, Path("sources/channel")),
                (root / "sources/channel").resolve(),
            )
            with self.assertRaises(importer.ImportErrorBase):
                importer.validate_output_dir(root, Path("../outside"))

    def test_manifest_shape(self):
        manifest = importer.empty_manifest(
            {"telegram_id": -1, "title": "Test", "username": None, "url": None},
            {"layout": "single-file", "download_media": True, "ocr": {}, "transcription": {}},
            {"name": "personal", "telegram_account_id": 10},
        )
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["files"]["ocr_dir"], "ocr/")
        self.assertEqual(manifest["files"]["transcripts_dir"], "transcripts/")
        self.assertEqual(manifest["export"]["account_name"], "personal")

    def test_resolved_media_error_is_cleared(self):
        manifest = {"errors": [{"stage": "media-download", "message_id": 8, "media_id": "8", "error": "old"}]}
        importer.clear_media_download_error(manifest, 8, "8")
        self.assertEqual(manifest["errors"], [])

    def test_only_current_media_errors_are_retained(self):
        manifest = {
            "messages": [{"message_id": 8, "media": [{"media_id": "8", "status": "not-downloadable"}]}],
            "errors": [{"stage": "media-download", "message_id": 8, "media_id": "8"}],
        }
        importer.clear_resolved_media_errors(manifest)
        self.assertEqual(manifest["errors"], [])

    def test_yaml_profiles_and_account_precedence(self):
        config_text = """
default_account: personal
accounts:
  personal:
    api_id: 1
    api_hash: personal-hash
  work:
    api_id: 2
    api_hash: work-hash
handlers: {}
"""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "telegram-import.config.yaml"
            path.write_text(config_text, encoding="utf-8")
            config = importer.Config.load(path)
            self.assertEqual(config.select_account().name, "personal")
            with patch.dict(os.environ, {importer.ACCOUNT_ENV: "work"}):
                self.assertEqual(config.select_account().name, "work")
            self.assertEqual(config.select_account("work").name, "work")

    def test_example_yaml_loads(self):
        config = importer.Config.load(Path(__file__).parents[1] / "config.example.yaml")
        self.assertEqual(sorted(config.accounts), ["personal", "work"])
        self.assertEqual(config.select_account().name, "personal")

    def test_config_path_precedence(self):
        with patch.dict(os.environ, {importer.CONFIG_ENV: "from-env.yaml"}):
            env_path, env_source = importer.resolve_config_path()
        self.assertEqual(env_source, "env")
        self.assertEqual(env_path, (Path.cwd() / "from-env.yaml").resolve())
        cli_path, cli_source = importer.resolve_config_path(Path("from-cli.yaml"))
        self.assertEqual(cli_source, "cli")
        self.assertEqual(cli_path, (Path.cwd() / "from-cli.yaml").resolve())

    def test_parser_accepts_account_on_commands(self):
        args = importer.parser().parse_args(["check-auth", "--account", "work"])
        self.assertEqual(args.account, "work")

    def test_root_dir_flag_defaults_link_style_to_obsidian(self):
        args = importer.parser().parse_args(
            ["export", "--channel", "x", "--root-dir", ".", "--output-dir", "y"]
        )
        self.assertEqual(args.root_dir, Path("."))
        self.assertEqual(args.link_style, "obsidian")

    def test_deprecated_vault_root_alias_sets_root_dir(self):
        args = importer.parser().parse_args(
            ["export", "--channel", "x", "--vault-root", ".", "--output-dir", "y"]
        )
        self.assertEqual(args.root_dir, Path("."))

    def test_link_style_flag_is_configurable(self):
        args = importer.parser().parse_args(
            [
                "export", "--channel", "x", "--root-dir", ".", "--output-dir", "y",
                "--link-style", "markdown",
            ]
        )
        self.assertEqual(args.link_style, "markdown")

    def test_upsert_replaces_a_post_without_duplication(self):
        post = [item for item in self.messages if item["message_id"] == 1][0]
        old = importer.render_post(post, "channel.md", [], [])
        changed = dict(post)
        changed["text"] = "Обновлённый пост"
        new = importer.render_post(changed, "channel.md", [], [])
        current = importer.frontmatter(
            {"title": "Test", "telegram_id": -1},
            {"layout": "single-file"},
            "2026-01-01T00:00:00+00:00",
        ) + old
        updated = importer.upsert_section(current, new, "telegram-post-1")
        self.assertEqual(updated.count("telegram-post-1"), 1)
        self.assertIn("Обновлённый пост", updated)
        self.assertNotIn("Первый пост", updated)

    def test_handler_timeout_is_reported(self):
        with patch.object(importer.subprocess, "run", side_effect=importer.subprocess.TimeoutExpired(["handler"], 900)):
            with self.assertRaises(importer.ImportErrorBase):
                importer.run_handler(["handler"], Path("input"), Path("output"), {})


if __name__ == "__main__":
    unittest.main()
