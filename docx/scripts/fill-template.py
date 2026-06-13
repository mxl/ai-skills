#!/usr/bin/env python3
"""
fill-template.py — Fill a Word (.docx) template with data from a JSON file.

The template must be authored in Word using Jinja2-style tags: {{ variable }},
{% for row in rows %}...{% endfor %}, etc.

Exit codes:
  0  success
  1  rendering error
  2  usage error
  3  docxtpl not installed
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Remove skill-tree path entries that would shadow 'docx' (python-docx package).
import os as _os
_scripts_dir = str(Path(__file__).parent.resolve())
_skill_dir   = str(Path(__file__).parent.parent.resolve())
sys.path = [p for p in sys.path if _os.path.realpath(p or ".") not in (_scripts_dir, _skill_dir)]

# Load _common by absolute path so sys.path manipulation doesn't shadow
# third-party packages (python-docx) that share the 'docx' package name.
import importlib.util as _ilu
_common_path = Path(__file__).parent / '_common.py'
_spec = _ilu.spec_from_file_location('_common', _common_path)
_common_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_common_mod)
emit_json = _common_mod.emit_json
fail = _common_mod.fail


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fill-template.py",
        description="Fill a .docx Jinja2 template with JSON data.",
    )
    parser.add_argument("template", help="Path to .docx template file")
    parser.add_argument("data", help="Path to JSON data file")
    parser.add_argument("-o", "--output", required=True, help="Output .docx path")
    args = parser.parse_args()

    try:
        from docxtpl import DocxTemplate
        from jinja2 import Environment, StrictUndefined, UndefinedError
    except ImportError:
        fail(
            3,
            "docxtpl not installed.\n"
            "Install: pip install docxtpl\n"
            "  or: uv pip install docxtpl",
        )

    template_path = Path(args.template)
    data_path = Path(args.data)
    output_path = Path(args.output)

    if not template_path.exists():
        fail(2, f"template not found: {template_path}")
    if not data_path.exists():
        fail(2, f"data file not found: {data_path}")

    try:
        with open(data_path, encoding="utf-8") as fh:
            context = json.load(fh)
    except json.JSONDecodeError as exc:
        fail(2, f"invalid JSON in {data_path}: {exc}")
    except OSError as exc:
        fail(2, str(exc))

    if not isinstance(context, dict):
        fail(2, "JSON data must be an object (dict at top level)")

    try:
        tpl = DocxTemplate(str(template_path))
        jinja_env = Environment(autoescape=False, undefined=StrictUndefined)
        tpl.render(context, jinja_env=jinja_env)
    except UndefinedError as exc:
        # Collect all undefined keys via a permissive second pass
        missing: list[str] = []
        try:
            from jinja2 import Undefined
            class _CollectUndefined(Undefined):
                def __str__(self):
                    missing.append(self._undefined_name)
                    return ""
                __iter__ = __str__
                __call__ = lambda self, *a, **kw: ""
            tpl2 = DocxTemplate(str(template_path))
            tpl2.render(context, jinja_env=Environment(autoescape=False, undefined=_CollectUndefined))
        except Exception:
            pass
        report = {
            "error": "undefined_variables",
            "message": str(exc),
            "missing_keys": list(set(missing)),
        }
        emit_json(report)
        sys.exit(1)
    except Exception as exc:
        fail(1, f"template rendering failed: {exc}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        tpl.save(str(output_path))
    except Exception as exc:
        fail(1, f"failed to save output: {exc}")

    emit_json({"output": str(output_path), "context_keys": list(context.keys())})


if __name__ == "__main__":
    main()
