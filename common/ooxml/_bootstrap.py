"""
Bootstrap: locate the ai-skills repo root so that common/ooxml is importable
from any skill script, including per-skill symlink installs.

Usage in each skill's scripts/_skillpath.py:
    from pathlib import Path as _Path
    import sys as _sys
    _here = _Path(__file__).resolve().parent
    exec(compile((_here / '_skillpath.py').read_text(), '_skillpath.py', 'exec'))

Or more simply, call install() from a skill's _skillpath.py:
    from common.ooxml._bootstrap import install
    install(__file__)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _find_repo_root(start: Path) -> Path | None:
    """
    Walk up from start looking for the ai-skills repo root.
    Identified by containing both 'common/' and 'README.md' siblings.
    Also respects AI_SKILLS_ROOT env var.
    """
    env_root = os.environ.get("AI_SKILLS_ROOT")
    if env_root:
        p = Path(env_root).resolve()
        if p.is_dir():
            return p

    current = start.resolve()
    for _ in range(10):  # max 10 levels up
        if (current / "common").is_dir() and (current / "README.md").is_file():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def install(script_file: str | Path) -> None:
    """
    Add the repo root to sys.path so that 'common.ooxml' is importable.
    Also removes the skill's scripts/ and skill/ dirs from sys.path to prevent
    them from shadowing third-party packages (e.g. python-docx 'docx', python-pptx 'pptx').

    Call this at the top of any skill script before importing common or third-party packages.

    Args:
        script_file: pass __file__ from the calling script
    """
    script_path = Path(script_file).resolve()
    scripts_dir = str(script_path.parent)
    skill_dir = str(script_path.parent.parent)

    # Remove skill-tree entries that shadow third-party packages
    sys.path = [
        p for p in sys.path
        if os.path.realpath(p or ".") not in (scripts_dir, skill_dir)
    ]

    repo_root = _find_repo_root(script_path)
    if repo_root is None:
        print(
            f"error: could not locate ai-skills repo root from {script_path}.\n"
            "Set AI_SKILLS_ROOT to the repo root directory.",
            file=sys.stderr,
        )
        sys.exit(2)

    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
