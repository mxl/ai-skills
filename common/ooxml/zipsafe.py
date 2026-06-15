"""ZIP safety utilities for OOXML skill scripts."""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

ZIP_LIMITS: dict[str, int | float] = {
    "max_entries": 10_000,
    "max_uncompressed_bytes": 2 * 1024 ** 3,  # 2 GB
    "max_ratio": 100,
}


# ---------------------------------------------------------------------------
# Safety inspection
# ---------------------------------------------------------------------------

def zip_safety_report(path: str | Path) -> dict[str, Any]:
    """
    Inspect a ZIP archive for safety issues without extracting.

    Returns a dict with keys:
        ok                  bool
        entry_count         int
        total_compressed    int (bytes)
        total_uncompressed  int (bytes)
        max_ratio           float
        issues              list[str]
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

                name = info.filename

                # Absolute paths
                if name.startswith("/") or name.startswith("\\"):
                    issues.append(f"absolute path: {name!r}")

                # Path traversal
                if ".." in name.split("/"):
                    issues.append(f"path traversal: {name!r}")

                # Duplicate names
                if name in seen_names:
                    issues.append(f"duplicate entry: {name!r}")
                seen_names.add(name)

                # Non-UTF-8 names
                try:
                    name.encode("utf-8")
                except UnicodeEncodeError:
                    issues.append(f"non-UTF8 entry name: {name!r}")

                # Per-entry compression ratio
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
            "max_ratio": 0.0,
            "issues": [f"bad zip: {exc}"],
        }

    if entry_count > ZIP_LIMITS["max_entries"]:
        issues.append(
            f"entry count {entry_count} exceeds limit {ZIP_LIMITS['max_entries']}"
        )
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
# Path traversal guard for extraction
# ---------------------------------------------------------------------------

def safe_member_path(outdir: Path, member_name: str) -> Path:
    """
    Resolve a ZIP member path inside outdir.
    Raises ValueError if the resolved path escapes outdir (path traversal).
    """
    target = (outdir / member_name).resolve()
    if not str(target).startswith(str(outdir.resolve()) + "/") and \
       str(target) != str(outdir.resolve()):
        raise ValueError(f"path traversal attempt: {member_name!r}")
    return target
