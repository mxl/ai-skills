"""
Bootstrap: locate common/ooxml and add repo root to sys.path.
Import this at the top of every pptx skill script.

Usage:
    import _skillpath  # noqa: F401  (side-effect: sys.path fixed)
"""
from pathlib import Path as _Path
import importlib.util as _ilu

_bootstrap_path = _Path(__file__).resolve().parent.parent.parent / "common" / "ooxml" / "_bootstrap.py"
if not _bootstrap_path.exists():
    import os as _os
    _env = _os.environ.get("AI_SKILLS_ROOT")
    if _env:
        _bootstrap_path = _Path(_env) / "common" / "ooxml" / "_bootstrap.py"

if _bootstrap_path.exists():
    _spec = _ilu.spec_from_file_location("_bootstrap", _bootstrap_path)
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _mod.install(__file__)
else:
    import sys as _sys
    print(
        "error: cannot find common/ooxml/_bootstrap.py. "
        "Set AI_SKILLS_ROOT to the ai-skills repo root.",
        file=_sys.stderr,
    )
    _sys.exit(2)
