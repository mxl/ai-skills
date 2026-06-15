"""I/O utilities for OOXML skill scripts."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def emit_json(obj: Any) -> None:
    """Print obj as indented JSON to stdout."""
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def fail(code: int, message: str) -> None:
    """Print error message to stderr and exit with code."""
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)
