#!/usr/bin/env python3
"""
bulk-fill-template.py - Fill a Word (.docx) Jinja2 template for many records.

Input JSON may be either:
  - a top-level array of record objects
  - an object containing a records array, configurable with --records-key

Exit codes:
  0  success
  1  one or more rendering or validation errors
  2  usage or data-shape error
  3  required package not installed
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# Remove skill-tree path entries that would shadow 'docx' (python-docx package).
import os as _os
_scripts_dir = str(Path(__file__).parent.resolve())
_skill_dir = str(Path(__file__).parent.parent.resolve())
sys.path = [p for p in sys.path if _os.path.realpath(p or ".") not in (_scripts_dir, _skill_dir)]

# Load _common by absolute path so sys.path manipulation doesn't shadow
# third-party packages (python-docx) that share the 'docx' package name.
import importlib.util as _ilu
_common_path = Path(__file__).parent / "_common.py"
_spec = _ilu.spec_from_file_location("_common", _common_path)
_common_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_common_mod)
emit_json = _common_mod.emit_json
fail = _common_mod.fail


def _load_records(path: Path, records_key: str) -> list[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        fail(2, f"invalid JSON in {path}: {exc}")
    except OSError as exc:
        fail(2, str(exc))

    if isinstance(data, list):
        records = data
    elif isinstance(data, dict) and isinstance(data.get(records_key), list):
        records = data[records_key]
    else:
        fail(2, f"JSON must be an array or an object with a '{records_key}' array")

    bad_indexes = [idx for idx, item in enumerate(records) if not isinstance(item, dict)]
    if bad_indexes:
        fail(2, f"all records must be objects; non-object records at indexes: {bad_indexes}")
    return records


def _safe_filename(value: str) -> str:
    value = value.strip()
    value = re.sub(r"[\\/:*?\"<>|]+", "-", value)
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value)
    value = value.strip("._-")
    return value or "contract"


def _validate_docx(path: Path) -> dict[str, Any]:
    validate_script = Path(__file__).with_name("validate.py")
    result = subprocess.run(
        [sys.executable, str(validate_script), str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    details: Any
    try:
        details = json.loads(result.stdout) if result.stdout else None
    except json.JSONDecodeError:
        details = result.stdout.strip()
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "details": details,
        "stderr": result.stderr.strip(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="bulk-fill-template.py",
        description="Fill a .docx Jinja2 template once per JSON record.",
    )
    parser.add_argument("template", help="Path to .docx template file")
    parser.add_argument("data", help="Path to JSON array or object containing records")
    parser.add_argument("-o", "--output-dir", required=True, help="Directory for generated .docx files")
    parser.add_argument(
        "--filename-template",
        default="{{ contract_id | default(loop_index) }}-{{ client_name | default('contract') }}.docx",
        help="Jinja2 template for output filenames; record fields and loop_index are available",
    )
    parser.add_argument("--records-key", default="contracts", help="Array key when input JSON is an object")
    parser.add_argument("--validate", action="store_true", help="Run validate.py on each generated .docx")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue rendering after record errors")
    args = parser.parse_args()

    try:
        from docxtpl import DocxTemplate
        from jinja2 import Environment, StrictUndefined, UndefinedError
    except ImportError:
        fail(3, "docxtpl not installed. Install: pip install docxtpl or uv pip install docxtpl")

    template_path = Path(args.template)
    data_path = Path(args.data)
    output_dir = Path(args.output_dir)

    if not template_path.exists():
        fail(2, f"template not found: {template_path}")
    if not data_path.exists():
        fail(2, f"data file not found: {data_path}")

    records = _load_records(data_path, args.records_key)
    output_dir.mkdir(parents=True, exist_ok=True)

    filename_env = Environment(autoescape=False, undefined=StrictUndefined)
    filename_template = filename_env.from_string(args.filename_template)
    generated: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    used_names: set[str] = set()

    for index, record in enumerate(records, start=1):
        context = {**record, "loop_index": index}
        record_id = record.get("contract_id", index)
        try:
            rendered_name = filename_template.render(context)
            safe_name = _safe_filename(rendered_name)
            if not safe_name.lower().endswith(".docx"):
                safe_name += ".docx"
            stem = Path(safe_name).stem
            suffix = Path(safe_name).suffix
            candidate = safe_name
            duplicate = 2
            while candidate in used_names:
                candidate = f"{stem}-{duplicate}{suffix}"
                duplicate += 1
            used_names.add(candidate)
            output_path = output_dir / candidate

            tpl = DocxTemplate(str(template_path))
            jinja_env = Environment(autoescape=False, undefined=StrictUndefined)
            tpl.render(record, jinja_env=jinja_env)
            tpl.save(str(output_path))

            item: dict[str, Any] = {"record": record_id, "output": str(output_path)}
            if args.validate:
                validation = _validate_docx(output_path)
                item["validation"] = validation
                if not validation["ok"]:
                    errors.append({"record": record_id, "error": "validation_failed", "output": str(output_path), "validation": validation})
                    if not args.continue_on_error:
                        break
            generated.append(item)
        except UndefinedError as exc:
            errors.append({"record": record_id, "error": "undefined_variable", "message": str(exc)})
            if not args.continue_on_error:
                break
        except Exception as exc:
            errors.append({"record": record_id, "error": "render_failed", "message": str(exc)})
            if not args.continue_on_error:
                break

    report = {
        "template": str(template_path),
        "data": str(data_path),
        "output_dir": str(output_dir),
        "records": len(records),
        "generated_count": len(generated),
        "error_count": len(errors),
        "generated": generated,
        "errors": errors,
    }
    emit_json(report)
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
