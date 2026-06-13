"""Shared pytest fixtures for docx validation tests."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Resolve paths relative to this file
DOCX_DIR = Path(__file__).parent.parent.resolve()
SCRIPTS_DIR = DOCX_DIR / "scripts"
EVALS_DIR = DOCX_DIR / "evals"
FIXTURES_DIR = EVALS_DIR / "fixtures"

# All .docx files in the project root (generated outputs)
PROJECT_ROOT = DOCX_DIR.parent


def _find_python() -> str:
    """Return a python3 that has python-docx available."""
    import shutil
    for candidate in [sys.executable, shutil.which("python3"), "/usr/bin/python3",
                      "/opt/homebrew/bin/python3"]:
        if not candidate:
            continue
        try:
            r = subprocess.run(
                [candidate, "-c", "from docx import Document"],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0:
                return candidate
        except Exception:
            continue
    return sys.executable


PYTHON = _find_python()


@pytest.fixture(scope="session")
def python():
    """Path to python3 with python-docx available."""
    return PYTHON


@pytest.fixture(scope="session")
def scripts_dir():
    """Path to the docx/scripts directory."""
    return SCRIPTS_DIR


@pytest.fixture(scope="session")
def fixtures_dir():
    """Path to the docx/evals/fixtures directory."""
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def valid_fixtures():
    """List of fixture .docx files that should pass validation."""
    return [
        p for p in sorted(FIXTURES_DIR.glob("*.docx"))
        if p.stem not in ("corrupt", "zipbomb")
    ]


@pytest.fixture(scope="session")
def invalid_fixtures():
    """List of fixture .docx files that should fail validation."""
    return [
        p for p in sorted(FIXTURES_DIR.glob("*.docx"))
        if p.stem in ("corrupt", "zipbomb")
    ]


@pytest.fixture(scope="session")
def generated_docx_files():
    """All .docx files in the project root (generated outputs)."""
    return sorted(PROJECT_ROOT.glob("*.docx"))
