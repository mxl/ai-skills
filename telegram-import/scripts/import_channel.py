#!/usr/bin/env python3
"""Deterministic Telegram channel exporter for the telegram-import skill."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mimetypes
import os
import re
import shlex
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator


SKILL_HOME = Path.home() / ".config" / "opencode" / "telegram-import"
DEFAULT_CONFIG_NAME = "telegram-import.config.yaml"
CONFIG_ENV = "TELEGRAM_IMPORT_CONFIG"
ACCOUNT_ENV = "TELEGRAM_IMPORT_ACCOUNT"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".wav", ".flac"}
EMBEDDABLE_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | {".pdf"}
LINK_STYLES = ("obsidian", "markdown", "none")
DEFAULT_LINK_STYLE = "obsidian"


class ImportErrorBase(Exception):
    """Expected importer error with user-actionable text."""


@dataclass
class AccountConfig:
    name: str
    api_id: int
    api_hash: str
    phone: str | None
    session_path: Path
    session_string: str | None


@dataclass
class Config:
    path: Path
    accounts: dict[str, AccountConfig]
    default_account: str | None
    handlers: dict[str, dict[str, list[str]]]

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        config_path, path_source = resolve_config_path(path)
        if not config_path.exists():
            raise ImportErrorBase(
                f"Missing Telegram import YAML config ({path_source}): {config_path}. "
                "Create it with accounts, api_id, and api_hash."
            )
        try:
            import yaml

            data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ImportErrorBase(f"Cannot read YAML config {config_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ImportErrorBase(f"YAML config {config_path} must contain a mapping at the root.")

        accounts_data = data.get("accounts")
        if not isinstance(accounts_data, dict) or not accounts_data:
            raise ImportErrorBase(
                f"YAML config {config_path} must contain a non-empty 'accounts' mapping; "
                "legacy config formats are not supported."
            )
        accounts: dict[str, AccountConfig] = {}
        for name, values in accounts_data.items():
            if not isinstance(name, str) or not name.strip():
                raise ImportErrorBase("Account names must be non-empty strings.")
            if not isinstance(values, dict):
                raise ImportErrorBase(f"Account {name!r} must be a YAML mapping.")
            try:
                api_id = int(values["api_id"])
                api_hash = str(values["api_hash"]).strip()
            except (KeyError, TypeError, ValueError) as exc:
                raise ImportErrorBase(f"Account {name!r} must contain api_id and api_hash.") from exc
            if not api_hash:
                raise ImportErrorBase(f"Account {name!r} api_hash must not be empty.")
            phone = values.get("phone")
            session_value = values.get("session_path", str(SKILL_HOME / f"{name}.session"))
            session_path = Path(str(session_value)).expanduser()
            if not session_path.is_absolute():
                session_path = Path.cwd() / session_path
            session_string = values.get("session_string")
            if session_string is not None:
                session_string = str(session_string).strip() or None
            accounts[name] = AccountConfig(
                name=name,
                api_id=api_id,
                api_hash=api_hash,
                phone=str(phone) if phone else None,
                session_path=session_path,
                session_string=session_string,
            )

        default_account = data.get("default_account")
        if default_account is not None and not isinstance(default_account, str):
            raise ImportErrorBase("default_account must be a string.")
        if default_account and default_account not in accounts:
            raise ImportErrorBase(
                f"Unknown default_account {default_account!r}; available accounts: {', '.join(accounts)}"
            )
        handlers = data.get("handlers", {})
        normalized_handlers: dict[str, dict[str, list[str]]] = {}
        if not isinstance(handlers, dict):
            raise ImportErrorBase("handlers must be a YAML mapping.")
        for kind, values in handlers.items():
            if not isinstance(values, dict):
                raise ImportErrorBase(f"Handler group {kind!r} must be a YAML mapping.")
            normalized_handlers[kind] = {}
            for name, command in values.items():
                if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
                    raise ImportErrorBase(f"Handler {kind}.{name} must be an argv list.")
                normalized_handlers[kind][name] = command
        return cls(config_path, accounts, default_account, normalized_handlers)

    def select_account(self, requested: str | None = None) -> AccountConfig:
        selected = requested or os.environ.get(ACCOUNT_ENV) or self.default_account
        if selected is None and len(self.accounts) == 1:
            selected = next(iter(self.accounts))
        if selected is None:
            raise ImportErrorBase(
                "Multiple Telegram accounts are configured but no account was selected. "
                f"Use --account or {ACCOUNT_ENV}; available accounts: {', '.join(self.accounts)}"
            )
        try:
            return self.accounts[selected]
        except KeyError as exc:
            raise ImportErrorBase(
                f"Unknown Telegram account {selected!r}; available accounts: {', '.join(self.accounts)}"
            ) from exc


def resolve_config_path(explicit: Path | None = None) -> tuple[Path, str]:
    if explicit is not None:
        candidate, source = Path(explicit), "cli"
    elif os.environ.get(CONFIG_ENV):
        candidate, source = Path(os.environ[CONFIG_ENV]), "env"
    else:
        candidate, source = Path.cwd() / DEFAULT_CONFIG_NAME, "default"
    candidate = candidate.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve(), source


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slugify(value: str) -> str:
    transliteration = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
        "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    lowered = value.casefold()
    transliterated = "".join(transliteration.get(char, char) for char in lowered)
    result = re.sub(r"[^a-z0-9]+", "-", transliterated).strip("-")
    return result or "telegram-channel"


def safe_filename(value: str, fallback: str) -> str:
    name = Path(value or "").name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return stem or fallback


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        current = value if value.tzinfo else value.replace(tzinfo=UTC)
        return current.astimezone(UTC).isoformat()
    return str(value)


def telegram_url(chat_id: int, username: str | None, message_id: int) -> str:
    if username:
        return f"https://t.me/{username.lstrip('@')}/{message_id}"
    raw = str(abs(chat_id))
    internal_id = raw[3:] if raw.startswith("100") else raw
    return f"https://t.me/c/{internal_id}/{message_id}"


def embed_for(relative_path: str, embed_prefix: str | None = None, link_style: str = DEFAULT_LINK_STYLE) -> str:
    embed_path = f"{embed_prefix.rstrip('/')}/{relative_path}" if embed_prefix else relative_path
    if link_style == "none":
        return embed_path
    is_embeddable = Path(relative_path).suffix.casefold() in EMBEDDABLE_EXTENSIONS
    if is_embeddable and link_style == "obsidian":
        return f"![[{embed_path}]]"
    if is_embeddable and link_style == "markdown":
        return f"![{Path(relative_path).stem}]({embed_path})"
    return f"[{Path(relative_path).name}]({embed_path})"


def normalize_fixture_message(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a serializable fixture without importing Telethon."""
    message_id = int(raw["message_id"])
    text = str(raw.get("text") or "")
    return {
        "message_id": message_id,
        "sent_at": iso(raw.get("sent_at")),
        "edited_at": iso(raw.get("edited_at")),
        "text": text,
        "sender_id": raw.get("sender_id"),
        "sender_name": raw.get("sender_name"),
        "forward_origin": raw.get("forward_origin"),
        "reactions": raw.get("reactions"),
        "views": int(raw.get("views") or 0),
        "grouped_id": str(raw["grouped_id"]) if raw.get("grouped_id") is not None else None,
        "reply_to": raw.get("reply_to"),
        "telegram_url": raw.get("telegram_url"),
        "media": list(raw.get("media") or []),
    }


def message_from_telethon(message: Any, chat_id: int, username: str | None) -> dict[str, Any]:
    media = getattr(message, "media", None)
    file_obj = getattr(message, "file", None)
    media_type = None
    if media is not None:
        class_name = type(media).__name__.casefold()
        mime = getattr(file_obj, "mime_type", None)
        if "photo" in class_name:
            media_type = "photo"
        elif "poll" in class_name:
            media_type = "poll"
        elif mime and mime.startswith("video/"):
            media_type = "video"
        elif mime and mime.startswith("audio/"):
            media_type = "audio"
        elif "document" in class_name:
            media_type = "document"
        elif "webpage" in class_name:
            media_type = "webpage"
        else:
            media_type = "media"
    media_records: list[dict[str, Any]] = []
    if media is not None:
        media_records.append(
            {
                "media_id": str(getattr(message, "id", "")),
                "type": media_type,
                "original_name": getattr(file_obj, "name", None),
                "mime": getattr(file_obj, "mime_type", None),
                "size_bytes": getattr(file_obj, "size", None),
                "downloadable": media_type not in {"webpage", "poll"},
                "message_ref": message,
            }
        )
    sender = getattr(message, "sender", None)
    sender_name = None
    if sender is not None:
        sender_name = " ".join(
            part for part in [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")] if part
        ) or getattr(sender, "title", None)
    return {
        "message_id": int(message.id),
        "sent_at": iso(getattr(message, "date", None)),
        "edited_at": iso(getattr(message, "edit_date", None)),
        "text": getattr(message, "message", "") or "",
        "sender_id": str(getattr(message, "sender_id", "")) or None,
        "sender_name": sender_name,
        "forward_origin": None,
        "reactions": None,
        "views": int(getattr(message, "views", 0) or 0),
        "grouped_id": str(getattr(message, "grouped_id")) if getattr(message, "grouped_id", None) else None,
        "reply_to": getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None),
        "telegram_url": telegram_url(chat_id, username, int(message.id)),
        "media": media_records,
    }


def message_is_root(post: dict[str, Any]) -> bool:
    return post.get("reply_to") in (None, "", 0)


def render_metadata(post: dict[str, Any]) -> str:
    metadata = {
        "telegram_message_id": post["message_id"],
        "sent_at": post.get("sent_at"),
        "telegram_url": post.get("telegram_url"),
        "grouped_id": post.get("grouped_id"),
        "sender_id": post.get("sender_id"),
        "sender_name": post.get("sender_name"),
        "views": post.get("views", 0),
    }
    return "```yaml\n" + "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in metadata.items()
    ) + "\n```"


def render_post(post: dict[str, Any], content_file: str, media_refs: list[dict[str, Any]], processing: list[dict[str, Any]]) -> str:
    anchor = f"telegram-post-{post['message_id']}"
    lines = [f"<!-- {anchor} -->", f"## {post.get('sent_at', '')} · Telegram post {post['message_id']}", "", render_metadata(post), ""]
    if post.get("text"):
        lines.extend([post["text"], ""])
    for media in media_refs:
        if media.get("embed"):
            lines.append(f"{media['embed']} <!-- media_id: {media['media_id']} type: {media.get('type')} -->")
        lines.append("")
    for item in processing:
        if item.get("embed"):
            lines.append(f"{item['embed']} <!-- processing: {item['type']} -->")
            lines.append("")
    lines.append(f"[Открыть публикацию в Telegram]({post.get('telegram_url')})")
    lines.append("")
    return "\n".join(lines)


def upsert_section(current: str, rendered: str, anchor: str) -> str:
    marker = f"<!-- {anchor} -->"
    section = rendered if rendered.startswith(marker) else f"{marker}\n{rendered}"
    pattern = re.compile(
        rf"(?ms)^<!-- {re.escape(anchor)} -->\n.*?(?=^<!-- telegram-post-\d+ -->\n|\Z)"
    )
    if pattern.search(current):
        return pattern.sub(section.rstrip("\n") + "\n", current, count=1)
    legacy = re.compile(
        rf"(?ms)^## .*?Telegram post {re.escape(str(anchor.rsplit('-', 1)[-1]))}\n.*?(?=^## .*?Telegram post \d+\n|\Z)"
    )
    if legacy.search(current):
        return legacy.sub(section.rstrip("\n") + "\n", current, count=1)
    return current.rstrip("\n") + "\n\n" + section.rstrip("\n") + "\n"


def append_error(manifest: dict[str, Any], error: dict[str, Any]) -> None:
    identity = (error.get("stage"), error.get("message_id"), error.get("media_id"), error.get("kind"), error.get("error"))
    for previous in manifest["errors"]:
        previous_identity = (previous.get("stage"), previous.get("message_id"), previous.get("media_id"), previous.get("kind"), previous.get("error"))
        if previous_identity == identity:
            return
    manifest["errors"].append(error)


def clear_media_download_error(manifest: dict[str, Any], message_id: int, media_id: str) -> None:
    manifest["errors"] = [
        error
        for error in manifest["errors"]
        if not (
            error.get("stage") == "media-download"
            and error.get("message_id") == message_id
            and str(error.get("media_id")) == str(media_id)
        )
    ]


def clear_resolved_media_errors(manifest: dict[str, Any]) -> None:
    failed_media = {
        (str(message.get("message_id")), str(media.get("media_id")))
        for message in manifest.get("messages", [])
        for media in message.get("media", [])
        if media.get("status") == "failed"
    }
    manifest["errors"] = [
        error
        for error in manifest["errors"]
        if error.get("stage") != "media-download"
        or (str(error.get("message_id")), str(error.get("media_id"))) in failed_media
    ]


def artifact_is_healthy(output_dir: Path, item: dict[str, Any], options: dict[str, Any]) -> bool:
    content_file = item.get("content_file")
    content_anchor = item.get("content_anchor")
    if not content_file or not content_anchor:
        return False
    content_path = output_dir / content_file
    if not content_path.is_file() or content_path.is_symlink():
        return False
    if content_anchor not in content_path.read_text(encoding="utf-8"):
        return False
    for media in item.get("media", []):
        if media.get("status") == "not-downloadable":
            continue
        if media.get("status") == "downloaded":
            path = output_dir / media["relative_path"]
            if not path.is_file() or path.is_symlink():
                return False
    for processing in item.get("processing", []):
        if processing.get("status") == "completed":
            path = output_dir / processing["relative_path"]
            if not path.is_file() or path.is_symlink():
                return False
    for kind, key in (("ocr", "ocr"), ("transcription", "transcription")):
        if options.get(key, {}).get("enabled"):
            completed = [entry for entry in item.get("processing", []) if entry.get("type") == kind]
            if not completed or any(entry.get("status") != "completed" for entry in completed):
                return False
    return item.get("status") == "exported"


def recompute_counts(manifest: dict[str, Any]) -> None:
    messages = manifest.get("messages", [])
    media = [item for message in messages for item in message.get("media", [])]
    processing = [item for message in messages for item in message.get("processing", [])]
    manifest["counts"].update(
        {
            "messages_seen": len(messages),
            "posts_exported": sum(message.get("status") == "exported" for message in messages),
            "media_seen": len(media),
            "media_downloaded": sum(item.get("status") == "downloaded" for item in media),
            "media_failed": sum(item.get("status") == "failed" for item in media),
            "ocr_succeeded": sum(item.get("type") == "ocr" and item.get("status") == "completed" for item in processing),
            "ocr_failed": sum(item.get("type") == "ocr" and item.get("status") == "failed" for item in processing),
            "transcriptions_succeeded": sum(item.get("type") == "transcription" and item.get("status") == "completed" for item in processing),
            "transcriptions_failed": sum(item.get("type") == "transcription" and item.get("status") == "failed" for item in processing),
        }
    )


def frontmatter(source: dict[str, Any], options: dict[str, Any], exported_at: str) -> str:
    values = {
        "type": "source",
        "source": "telegram",
        "source_channel": source.get("title"),
        "telegram_id": source.get("telegram_id"),
        "exported_at": exported_at,
        "layout": options.get("layout"),
    }
    return "---\n" + "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in values.items()
    ) + "\n---\n\n"


def empty_manifest(source: dict[str, Any], options: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": 1,
        "export": {
            "export_id": str(uuid.uuid4()),
            "status": "running",
            "started_at": now,
            "finished_at": None,
            "source": source,
            "account_name": account["name"],
            "telegram_account_id": account["telegram_account_id"],
            "options": options,
        },
        "files": {
            "content": "channel.md" if options["layout"] == "single-file" else None,
            "media_dir": "media/",
            "ocr_dir": "ocr/",
            "transcripts_dir": "transcripts/",
        },
        "counts": {
            "messages_seen": 0,
            "posts_exported": 0,
            "posts_skipped": 0,
            "media_seen": 0,
            "media_downloaded": 0,
            "media_failed": 0,
            "ocr_succeeded": 0,
            "ocr_failed": 0,
            "transcriptions_succeeded": 0,
            "transcriptions_failed": 0,
        },
        "messages": [],
        "errors": [],
    }


def validate_output_dir(root_dir: Path, output_dir: Path) -> Path:
    root = root_dir.expanduser().resolve()
    target = output_dir.expanduser()
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ImportErrorBase(f"Output directory must be inside root directory: {target}") from exc
    return target


def resolve_handler(config: Config, kind: str, name: str | None) -> list[str] | None:
    if not name:
        return None
    command = config.handlers.get(kind, {}).get(name)
    if command is None:
        raise ImportErrorBase(f"Unknown configured {kind} handler: {name}")
    return command


def run_handler(command: list[str], input_path: Path, output_path: Path, metadata: dict[str, Any]) -> str:
    payload = {
        "input_path": str(input_path),
        "output_path": str(output_path),
        **metadata,
    }
    try:
        result = subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            check=False,
            timeout=900,
        )
    except OSError as exc:
        raise ImportErrorBase(f"Handler could not start: {shlex.join(command)}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ImportErrorBase(f"Handler timed out after 900 seconds: {shlex.join(command)}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "handler failed").strip()
        raise ImportErrorBase(f"Handler exited {result.returncode}: {detail[:500]}")
    return result.stdout.strip()


def ensure_media_record(
    media: dict[str, Any],
    post: dict[str, Any],
    output_dir: Path,
    embed_prefix: str | None = None,
    link_style: str = DEFAULT_LINK_STYLE,
) -> dict[str, Any]:
    original = media.get("original_name") or f"{media.get('media_id', post['message_id'])}-{media.get('type', 'media')}"
    extension = Path(original).suffix
    if not extension:
        guessed = mimetypes.guess_extension(media.get("mime") or "") or ""
        extension = guessed
    filename = safe_filename(f"{post['message_id']}-{media.get('media_id', post['message_id'])}{extension}", "media.bin")
    relative_path = f"media/{filename}"
    return {
        "media_id": str(media.get("media_id", post["message_id"])),
        "type": media.get("type"),
        "original_name": original,
        "mime": media.get("mime"),
        "size_bytes": media.get("size_bytes"),
        "downloadable": media.get("downloadable", True),
        "relative_path": relative_path,
        "embed": embed_for(relative_path, embed_prefix, link_style),
        "status": "pending",
        "_source": media.get("message_ref"),
    }


async def download_media_record(
    client: Any,
    record: dict[str, Any],
    output_dir: Path,
    embed_prefix: str | None = None,
    link_style: str = DEFAULT_LINK_STYLE,
) -> None:
    source = record.pop("_source", None)
    if source is None:
        return
    target = output_dir / record["relative_path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and (target.is_symlink() or not target.resolve().is_relative_to(output_dir.resolve())):
        raise ImportErrorBase(f"Refusing unsafe media target: {target}")
    downloaded = await client.download_media(source, file=str(target))
    if not downloaded:
        raise ImportErrorBase("Telegram returned no downloaded media path")
    downloaded_path = Path(downloaded)
    path = downloaded_path.resolve()
    if downloaded_path.is_symlink() or not path.is_file() or not path.is_relative_to(output_dir.resolve()):
        raise ImportErrorBase(f"Telegram returned an unsafe media path: {downloaded_path}")
    record["relative_path"] = path.relative_to(output_dir).as_posix()
    record["embed"] = embed_for(record["relative_path"], embed_prefix, link_style)
    record["size_bytes"] = path.stat().st_size
    record["sha256"] = sha256_file(path)
    record["status"] = "downloaded"


def period_key(sent_at: str | None, unit: str) -> str:
    if not sent_at:
        return "unknown"
    date_part = sent_at[:10]
    year, month, _ = [int(part) for part in date_part.split("-")]
    if unit == "year":
        return f"{year:04d}"
    if unit == "quarter":
        return f"{year:04d}-q{((month - 1) // 3) + 1}"
    return f"{year:04d}-{month:02d}"


def content_path_for(output_dir: Path, post: dict[str, Any], layout: str, period_unit: str | None) -> tuple[Path, str]:
    if layout == "single-file":
        return output_dir / "channel.md", f"telegram-post-{post['message_id']}"
    if layout == "per-message":
        date_part = (post.get("sent_at") or "unknown")[:10]
        return output_dir / f"{date_part}-{post['message_id']}.md", f"telegram-post-{post['message_id']}"
    key = period_key(post.get("sent_at"), period_unit or "month")
    return output_dir / f"{key}.md", f"telegram-post-{post['message_id']}"


def source_from_entity(entity: Any) -> dict[str, Any]:
    chat_id = int(getattr(entity, "id"))
    username = getattr(entity, "username", None)
    title = getattr(entity, "title", None) or getattr(entity, "name", None) or str(chat_id)
    return {
        "telegram_id": chat_id,
        "title": title,
        "username": username,
        "url": f"https://t.me/{username}" if username else None,
    }


async def iter_channel_posts(
    client: Any,
    entity: Any,
    since: datetime | None,
    until: datetime | None,
    min_id: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    chat_id = int(getattr(entity, "id"))
    username = getattr(entity, "username", None)
    request = {"reverse": True, "limit": None}
    if min_id is not None:
        request["min_id"] = min_id
    async for message in client.iter_messages(entity, **request):
        post = message_from_telethon(message, chat_id, username)
        sent_at = parse_datetime(post.get("sent_at"))
        if since and sent_at and sent_at < since:
            continue
        if until and sent_at and sent_at > until:
            continue
        if message_is_root(post):
            yield post


async def resolve_channel(client: Any, identifier: str) -> Any:
    """Resolve a channel even when its numeric entity is not cached locally."""
    try:
        return await client.get_entity(identifier)
    except (ValueError, TypeError):
        pass

    target_id: int | None = None
    try:
        target_id = int(identifier)
    except ValueError:
        pass

    dialogs = await client.get_dialogs()
    candidates = []
    for dialog in dialogs:
        entity = dialog.entity
        if target_id is not None and int(dialog.id) == target_id:
            return entity
        title = getattr(entity, "title", None) or getattr(dialog, "name", None)
        username = getattr(entity, "username", None)
        if identifier.casefold() in str(title or "").casefold() or identifier.lstrip("@").casefold() == str(username or "").casefold():
            candidates.append(entity)

    if target_id is None or not candidates:
        try:
            from telethon import functions

            result = await client(functions.contacts.SearchRequest(q=identifier, limit=100))
            for entity in getattr(result, "chats", []):
                if not getattr(entity, "broadcast", False):
                    continue
                if target_id is not None and int(getattr(entity, "id", 0)) == target_id:
                    return entity
                title = getattr(entity, "title", "")
                username = getattr(entity, "username", None)
                if identifier.casefold() in title.casefold() or identifier.lstrip("@").casefold() == str(username or "").casefold():
                    candidates.append(entity)
        except Exception as exc:
            raise ImportErrorBase(f"Telegram channel search failed for {identifier!r}: {exc}") from exc

    unique = {int(getattr(entity, "id")): entity for entity in candidates}
    if len(unique) == 1:
        return next(iter(unique.values()))
    if not unique:
        raise ImportErrorBase(f"Cannot find Telegram channel {identifier!r} in this account.")
    names = ", ".join(
        f"{getattr(entity, 'title', getattr(entity, 'username', entity.id))} ({entity.id})"
        for entity in unique.values()
    )
    raise ImportErrorBase(f"Ambiguous Telegram channel {identifier!r}; candidates: {names}")


async def export_posts(
    client: Any,
    entity: Any,
    output_dir: Path,
    options: dict[str, Any],
    config: Config,
    account: dict[str, Any],
    embed_prefix: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    link_style = options.get("link_style", DEFAULT_LINK_STYLE)
    source = source_from_entity(entity)
    manifest_path = output_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else empty_manifest(source, options, account)
    previous_account_name = manifest.get("export", {}).get("account_name")
    previous_account_id = manifest.get("export", {}).get("telegram_account_id")
    if previous_account_name != account["name"] or previous_account_id != account["telegram_account_id"]:
        raise ImportErrorBase(
            "Existing export belongs to a different Telegram account or an older manifest. "
            "Use a new output directory for this account."
        )
    previous_options = manifest.get("export", {}).get("options")
    if previous_options is not None and previous_options != options:
        raise ImportErrorBase(
            "Existing export options differ from this run. Use a new output directory "
            "or resume with the same layout, range, media, OCR, and transcription options."
        )
    manifest["export"]["source"] = source
    manifest["export"]["account_name"] = account["name"]
    manifest["export"]["telegram_account_id"] = account["telegram_account_id"]
    clear_resolved_media_errors(manifest)
    existing = {item["message_id"]: item for item in manifest.get("messages", [])}
    single_file_path = output_dir / "channel.md"
    if options["layout"] == "single-file" and not single_file_path.exists() and not dry_run:
        atomic_write(single_file_path, frontmatter(source, options, manifest["export"]["started_at"]))

    ocr_command = resolve_handler(config, "ocr", options.get("ocr", {}).get("handler"))
    transcription_command = resolve_handler(config, "transcription", options.get("transcription", {}).get("handler"))
    processed = 0
    run_had_errors = False
    since = parse_datetime(options.get("from"))
    until = parse_datetime(options.get("to"))
    can_resume_from_id = bool(existing) and all(
        artifact_is_healthy(output_dir, item, options) for item in existing.values()
    )
    resume_min_id = max(existing) if can_resume_from_id else None
    async for post in iter_channel_posts(client, entity, since, until, resume_min_id):
        if limit is not None and processed >= limit:
            break
        processed += 1
        manifest["counts"]["messages_seen"] += 1
        old = existing.get(post["message_id"])
        if old and not dry_run and artifact_is_healthy(output_dir, old, options):
            for media in old.get("media", []):
                if media.get("status") != "failed":
                    clear_media_download_error(manifest, post["message_id"], media.get("media_id", ""))
            manifest["counts"]["posts_skipped"] += 1
            continue
        if dry_run:
            continue
        media_refs: list[dict[str, Any]] = []
        processing_refs: list[dict[str, Any]] = []
        for media in post.get("media", []):
            manifest["counts"]["media_seen"] += 1
            record = ensure_media_record(media, post, output_dir, embed_prefix, link_style)
            if not record.get("downloadable", True):
                clear_media_download_error(manifest, post["message_id"], record["media_id"])
                record.pop("_source", None)
                record["relative_path"] = None
                record["embed"] = None
                record["status"] = "not-downloadable"
                media_refs.append(record)
                continue
            try:
                if options.get("download_media"):
                    await download_media_record(client, record, output_dir, embed_prefix, link_style)
                    clear_media_download_error(manifest, post["message_id"], record["media_id"])
                    manifest["counts"]["media_downloaded"] += 1
                else:
                    record.pop("_source", None)
                    record["status"] = "not-downloaded"
                media_refs.append(record)
            except Exception as exc:
                record.pop("_source", None)
                record["status"] = "failed"
                manifest["counts"]["media_failed"] += 1
                run_had_errors = True
                append_error(manifest, {
                    "stage": "media-download",
                    "message_id": post["message_id"],
                    "media_id": record["media_id"],
                    "kind": type(exc).__name__,
                    "error": str(exc),
                    "retryable": True,
                    "timestamp": datetime.now(UTC).isoformat(),
                })
                media_refs.append(record)

            source_path = output_dir / record["relative_path"]
            if record["status"] != "downloaded":
                continue
            for kind, enabled, command, folder, counter_key in (
                ("ocr", options.get("ocr", {}).get("enabled"), ocr_command, "ocr", "ocr_succeeded"),
                ("transcription", options.get("transcription", {}).get("enabled"), transcription_command, "transcripts", "transcriptions_succeeded"),
            ):
                if not enabled or (kind == "ocr" and record.get("type") not in {"photo", "document"}) or (kind == "transcription" and record.get("type") not in {"audio", "video"}):
                    continue
                result_path = output_dir / folder / f"{post['message_id']}-{record['media_id']}.md"
                processing = {
                    "type": kind,
                    "source_media_path": record["relative_path"],
                    "relative_path": result_path.relative_to(output_dir).as_posix(),
                    "embed": embed_for(result_path.relative_to(output_dir).as_posix(), embed_prefix, link_style),
                    "handler": options[kind].get("handler"),
                    "language": options[kind].get("language"),
                    "created_at": datetime.now(UTC).isoformat(),
                    "status": "pending",
                }
                try:
                    if command is None:
                        raise ImportErrorBase(f"No configured {kind} handler selected")
                    result = run_handler(command, source_path, result_path, {
                        "message_id": post["message_id"],
                        "media_id": record["media_id"],
                        "language": options[kind].get("language"),
                    })
                    body = (
                        f"---\nsource_message_id: {post['message_id']}\n"
                        f"source_media: {record['relative_path']}\nprocessing: {kind}\n"
                        f"handler: {options[kind].get('handler')}\nlanguage: {options[kind].get('language')}\n---\n\n{result}\n"
                    )
                    atomic_write(result_path, body)
                    processing["status"] = "completed"
                    manifest["counts"][counter_key] += 1
                except Exception as exc:
                    processing["status"] = "failed"
                    manifest["counts"]["ocr_failed" if kind == "ocr" else "transcriptions_failed"] += 1
                    run_had_errors = True
                    append_error(manifest, {
                        "stage": kind,
                        "message_id": post["message_id"],
                        "media_id": record["media_id"],
                        "kind": type(exc).__name__,
                        "error": str(exc),
                        "retryable": True,
                        "timestamp": datetime.now(UTC).isoformat(),
                    })
                processing_refs.append(processing)

        content_path, anchor = content_path_for(output_dir, post, options["layout"], options.get("period_unit"))
        content = render_post(post, content_path.name, media_refs, processing_refs)
        if options["layout"] == "single-file":
            current = content_path.read_text(encoding="utf-8") if content_path.exists() else frontmatter(source, options, manifest["export"]["started_at"])
            atomic_write(content_path, upsert_section(current, content, anchor))
        else:
            existing_content = content_path.read_text(encoding="utf-8") if content_path.exists() else frontmatter(source, options, manifest["export"]["started_at"])
            atomic_write(content_path, upsert_section(existing_content, content, anchor))

        record = {
            "message_id": post["message_id"],
            "sent_at": post.get("sent_at"),
            "telegram_url": post.get("telegram_url"),
            "content_file": content_path.relative_to(output_dir).as_posix(),
            "content_anchor": anchor,
            "text_sha256": sha256_bytes(post.get("text", "").encode("utf-8")),
            "grouped_id": post.get("grouped_id"),
            "media": [{key: value for key, value in item.items() if not key.startswith("_")} for item in media_refs],
            "processing": processing_refs,
            "status": "exported" if all(item.get("status") != "failed" for item in media_refs + processing_refs) else "partial",
        }
        existing[post["message_id"]] = record
        manifest["messages"] = [existing[key] for key in sorted(existing)]
        if record["status"] == "exported":
            manifest["counts"]["posts_exported"] += 1
        else:
            run_had_errors = True
        atomic_write_json(manifest_path, manifest)

    if dry_run:
        manifest["export"]["status"] = "dry-run"
    else:
        recompute_counts(manifest)
        has_partial_messages = any(item.get("status") != "exported" for item in manifest.get("messages", []))
        manifest["export"]["status"] = "partial" if run_had_errors or has_partial_messages else "completed"
        manifest["export"]["finished_at"] = datetime.now(UTC).isoformat()
        atomic_write_json(manifest_path, manifest)
    return manifest


def require_telethon() -> tuple[Any, Any]:
    try:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError
    except ImportError as exc:
        raise ImportErrorBase("Telethon is required. Run with: uv run --with telethon python ...") from exc
    return TelegramClient, SessionPasswordNeededError


async def open_client(account: AccountConfig) -> Any:
    TelegramClient, _ = require_telethon()
    if account.session_string:
        from telethon.sessions import StringSession

        return TelegramClient(StringSession(account.session_string), account.api_id, account.api_hash)
    account.session_path.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(account.session_path), account.api_id, account.api_hash)


async def authenticate(account: AccountConfig) -> None:
    _, SessionPasswordNeededError = require_telethon()
    client = await open_client(account)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            if not account.phone:
                raise ImportErrorBase(f"Telegram phone is missing for account {account.name!r}.")
            await client.send_code_request(account.phone)
            code = input("Telegram verification code: ").strip()
            try:
                await client.sign_in(account.phone, code)
            except SessionPasswordNeededError:
                import getpass

                await client.sign_in(password=getpass.getpass("Telegram 2FA password: "))
        me = await client.get_me()
        print(json.dumps({"account_name": account.name, "authenticated": True, "account_id": getattr(me, "id", None)}, ensure_ascii=False))
    finally:
        await client.disconnect()


async def check_auth(account: AccountConfig) -> None:
    client = await open_client(account)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print(json.dumps({"account_name": account.name, "authenticated": False}, ensure_ascii=False))
            return
        me = await client.get_me()
        print(json.dumps({"account_name": account.name, "authenticated": True, "account_id": getattr(me, "id", None)}, ensure_ascii=False))
    finally:
        await client.disconnect()


async def run_export(args: argparse.Namespace, config: Config, account: AccountConfig) -> None:
    if args.root_dir is None:
        raise ImportErrorBase("--root-dir is required (or its deprecated alias --vault-root).")
    root_dir = Path(args.root_dir).expanduser().resolve()
    output_dir = validate_output_dir(root_dir, Path(args.output_dir))
    if args.include_comments:
        raise ImportErrorBase("Discussion replies are not supported by this channel-only importer; use --exclude-comments.")
    options = {
        "from": args.from_date,
        "to": args.to_date,
        "include_comments": False,
        "download_media": args.download_media,
        "layout": args.layout,
        "period_unit": args.period_unit,
        "link_style": args.link_style,
        "ocr": {"enabled": bool(args.ocr_handler), "handler": args.ocr_handler, "language": args.ocr_language},
        "transcription": {"enabled": bool(args.transcription_handler), "handler": args.transcription_handler, "language": args.transcription_language},
    }
    client = await open_client(account)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise ImportErrorBase(f"Telegram importer session for account {account.name!r} is not authenticated. Run auth first.")
        entity = await resolve_channel(client, args.channel)
        me = await client.get_me()
        account_identity = {"name": account.name, "telegram_account_id": getattr(me, "id", None)}
        embed_prefix = output_dir.relative_to(root_dir).as_posix()
        manifest = await export_posts(
            client,
            entity,
            output_dir,
            options,
            config,
            account_identity,
            embed_prefix,
            args.limit,
            args.dry_run,
        )
        print(json.dumps({"output_dir": str(output_dir), "status": manifest["export"]["status"], "counts": manifest["counts"]}, ensure_ascii=False, indent=2))
    finally:
        await client.disconnect()


class _DeprecatedVaultRootAction(argparse.Action):
    """Back-compat shim: --vault-root still works but writes to root_dir and warns."""

    def __call__(self, parser_, namespace, values, option_string=None):
        print(
            "telegram-import: --vault-root is deprecated, use --root-dir instead",
            file=sys.stderr,
        )
        setattr(namespace, "root_dir", values)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", help="Authenticate a configured Telegram account")
    check = commands.add_parser("check-auth", help="Check a configured Telegram account")
    export = commands.add_parser("export", help="Export a Telegram channel")
    for command in (auth, check, export):
        command.add_argument("--config", help="YAML config path; default: ./telegram-import.config.yaml")
        command.add_argument("--account", help="Account profile; priority: CLI > TELEGRAM_IMPORT_ACCOUNT > default_account")
    export.add_argument("--channel", required=True)
    export.add_argument(
        "--root-dir",
        dest="root_dir",
        type=Path,
        default=None,
        help="Base directory that --output-dir must resolve inside (used to compute embed paths)",
    )
    export.add_argument(
        "--vault-root",
        type=Path,
        action=_DeprecatedVaultRootAction,
        default=None,
        help=argparse.SUPPRESS,
    )
    export.add_argument("--output-dir", required=True, type=Path)
    export.add_argument("--layout", choices=["single-file", "per-message", "period"], default="single-file")
    export.add_argument(
        "--link-style",
        choices=list(LINK_STYLES),
        default=DEFAULT_LINK_STYLE,
        help="Embed syntax for downloaded media: obsidian (![[path]]), markdown (![alt](path)), or none (plain path)",
    )
    export.add_argument("--period-unit", choices=["month", "quarter", "year"], default="month")
    export.add_argument("--from", dest="from_date")
    export.add_argument("--to", dest="to_date")
    export.add_argument("--include-comments", action="store_true")
    export.add_argument("--exclude-comments", action="store_true", default=True)
    export.add_argument("--download-media", action=argparse.BooleanOptionalAction, default=True)
    export.add_argument("--ocr-handler")
    export.add_argument("--ocr-language")
    export.add_argument("--transcription-handler")
    export.add_argument("--transcription-language")
    export.add_argument("--limit", type=int)
    export.add_argument("--dry-run", action="store_true")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = Config.load(Path(args.config) if args.config else None)
        account = config.select_account(args.account)
        if args.command == "auth":
            asyncio.run(authenticate(account))
        elif args.command == "check-auth":
            asyncio.run(check_auth(account))
        else:
            asyncio.run(run_export(args, config, account))
        return 0
    except ImportErrorBase as exc:
        print(f"telegram-import: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("telegram-import: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
