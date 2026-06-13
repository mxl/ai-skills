#!/usr/bin/env python3
"""Validate generated DOCX files in a directory tree."""
from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


DEFAULT_EXCLUDES = [
    "docx/evals/fixtures/**",
    "~$*.docx",
    "**/~$*.docx",
    ".*/**",
]


def _load_validate():
    script_path = Path(__file__).resolve().parent / "validate.py"
    spec = importlib.util.spec_from_file_location("docx_validate", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load validator: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _excluded(rel_path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(rel_path, pattern) for pattern in patterns)


def discover_docx_files(root: Path, excludes: list[str]) -> list[Path]:
    return [
        path for path in sorted(root.rglob("*.docx"))
        if path.is_file() and not _excluded(_rel(path, root), excludes)
    ]


def validate_generated(root: Path, excludes: list[str]) -> dict[str, Any]:
    validate = _load_validate()
    files = discover_docx_files(root, excludes)
    reports = [validate(path) for path in files]
    failures = [report for report in reports if not report["ok"]]
    return {
        "root": str(root),
        "ok": not failures,
        "validated_count": len(reports),
        "failed_count": len(failures),
        "excluded": excludes,
        "reports": reports,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover and validate generated .docx files under a directory.",
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Directory to scan recursively (default: current directory).",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Additional POSIX-style glob to exclude, relative to root. Can be repeated.",
    )
    parser.add_argument(
        "--include-fixtures",
        action="store_true",
        help="Do not exclude docx/evals/fixtures/** by default.",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        sys.exit(2)

    excludes = list(DEFAULT_EXCLUDES)
    if args.include_fixtures:
        excludes.remove("docx/evals/fixtures/**")
    excludes.extend(args.exclude)

    report = validate_generated(root, excludes)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
