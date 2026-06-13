"""Shared utilities for docx skill scripts."""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# OOXML namespace map
# ---------------------------------------------------------------------------

NAMESPACES: dict[str, str] = {
    "w":       "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r":       "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel":     "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct":      "http://schemas.openxmlformats.org/package/2006/content-types",
    "cp":      "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc":      "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "xsi":     "http://www.w3.org/2001/XMLSchema-instance",
    "app":     "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
    "vt":      "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes",
    "wp":      "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a":       "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic":     "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "mc":      "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "w14":     "http://schemas.microsoft.com/office/word/2010/wordml",
    "w15":     "http://schemas.microsoft.com/office/word/2012/wordml",
}

# Reverse map: uri -> prefix
_URI_TO_PREFIX: dict[str, str] = {v: k for k, v in NAMESPACES.items()}


def register_namespaces() -> None:
    """Register all OOXML namespaces with xml.etree.ElementTree."""
    try:
        import xml.etree.ElementTree as ET
        for prefix, uri in NAMESPACES.items():
            ET.register_namespace(prefix, uri)
    except Exception:
        pass


def clark(prefix: str, local: str) -> str:
    """Return Clark-notation tag: {uri}local."""
    return f"{{{NAMESPACES[prefix]}}}{local}"


# ---------------------------------------------------------------------------
# ZIP safety
# ---------------------------------------------------------------------------

ZIP_LIMITS = {
    "max_entries": 10_000,
    "max_uncompressed_bytes": 2 * 1024 ** 3,  # 2 GB
    "max_ratio": 100,
}


def zip_safety_report(path: str | Path) -> dict[str, Any]:
    """
    Inspect a ZIP archive for safety issues without extracting.
    Returns a dict with keys: ok, entry_count, total_compressed,
    total_uncompressed, max_ratio, issues (list of strings).
    """
    path = Path(path)
    issues: list[str] = []
    entry_count = 0
    total_compressed = 0
    total_uncompressed = 0
    max_ratio = 0.0
    seen_names: set[str] = set()

    try:
        with zipfile.ZipFile(path, "r") as zf:
            for info in zf.infolist():
                entry_count += 1
                total_compressed += info.compress_size
                total_uncompressed += info.file_size

                # path traversal / absolute paths
                name = info.filename
                if name.startswith("/") or name.startswith("\\"):
                    issues.append(f"absolute path: {name!r}")
                if ".." in name.split("/"):
                    issues.append(f"path traversal: {name!r}")

                # duplicate names
                if name in seen_names:
                    issues.append(f"duplicate entry: {name!r}")
                seen_names.add(name)

                # non-UTF-8 names
                try:
                    name.encode("utf-8")
                except UnicodeEncodeError:
                    issues.append(f"non-UTF8 entry name: {name!r}")

                # per-entry ratio
                if info.compress_size > 0:
                    ratio = info.file_size / info.compress_size
                    if ratio > max_ratio:
                        max_ratio = ratio
                    if ratio > ZIP_LIMITS["max_ratio"]:
                        issues.append(
                            f"high compression ratio {ratio:.0f}x: {name!r}"
                        )

    except zipfile.BadZipFile as exc:
        return {
            "ok": False,
            "entry_count": 0,
            "total_compressed": 0,
            "total_uncompressed": 0,
            "max_ratio": 0,
            "issues": [f"bad zip: {exc}"],
        }

    if entry_count > ZIP_LIMITS["max_entries"]:
        issues.append(f"entry count {entry_count} exceeds limit {ZIP_LIMITS['max_entries']}")
    if total_uncompressed > ZIP_LIMITS["max_uncompressed_bytes"]:
        issues.append(
            f"uncompressed size {total_uncompressed} exceeds limit "
            f"{ZIP_LIMITS['max_uncompressed_bytes']}"
        )

    return {
        "ok": len(issues) == 0,
        "entry_count": entry_count,
        "total_compressed": total_compressed,
        "total_uncompressed": total_uncompressed,
        "max_ratio": round(max_ratio, 2),
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

_OLE_MAGIC = b"\xd0\xcf\x11\xe0"
_ZIP_MAGIC = b"PK\x03\x04"


def detect_format(path: str | Path) -> str:
    """
    Detect file format by magic bytes and internal structure.
    Returns: 'docx', 'docm', 'doc', 'unknown'.
    """
    path = Path(path)
    try:
        with open(path, "rb") as fh:
            magic = fh.read(8)
    except OSError:
        return "unknown"

    if magic[:4] == _OLE_MAGIC:
        return "doc"

    if magic[:4] == _ZIP_MAGIC:
        try:
            with zipfile.ZipFile(path, "r") as zf:
                names = set(zf.namelist())
                if "vbaProject.bin" in names or any(
                    n.endswith("vbaProject.bin") for n in names
                ):
                    return "docm"
                if "[Content_Types].xml" in names:
                    return "docx"
        except Exception:
            pass
        return "unknown"

    return "unknown"


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def emit_json(obj: Any) -> None:
    """Print obj as indented JSON to stdout."""
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def fail(code: int, message: str) -> None:
    """Print error message to stderr and exit with code."""
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)
