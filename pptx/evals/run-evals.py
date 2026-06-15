#!/usr/bin/env python3
"""
run-evals.py — Mechanical assertions for the pptx skill.

Runs inspect/validate/extract/sanitize on each fixture and checks expected
outcomes. Exits 0 if all pass, 1 if any fail.

Usage:
    python evals/run-evals.py [--fixtures evals/fixtures]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import zipfile
from pathlib import Path

_here   = Path(__file__).resolve().parent
_skill  = _here.parent
_scripts = _skill / "scripts"
_repo   = _skill.parent
sys.path.insert(0, str(_repo))

PASS = "\033[32m[PASS]\033[0m"
FAIL = "\033[31m[FAIL]\033[0m"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(script: str, *args: str) -> tuple[int, dict]:
    """Run a skill script and return (exit_code, parsed_json_or_empty)."""
    cmd = [sys.executable, str(_scripts / script)] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
    except Exception:
        data = {}
    return result.returncode, data


def _check(label: str, condition: bool, detail: str = "") -> bool:
    status = PASS if condition else FAIL
    msg = f"  {status} {label}"
    if detail and not condition:
        msg += f": {detail}"
    print(msg)
    return condition


# ---------------------------------------------------------------------------
# Per-fixture assertions
# ---------------------------------------------------------------------------

def eval_simple(fixture: Path, tmp: Path) -> list[bool]:
    results = []
    code, data = _run("inspect.py", str(fixture))
    results.append(_check("inspect/exits_0", code == 0))
    results.append(_check("inspect/format_pptx", data.get("format") == "pptx"))
    results.append(_check("inspect/slide_count_ge1", (data.get("flags") or {}).get("slide_count", 0) >= 1))

    code, data = _run("validate.py", str(fixture))
    results.append(_check("validate/ok", data.get("ok") is True))

    out_md = tmp / "simple.md"
    code, _ = _run("extract.py", str(fixture), "--format", "md", "-o", str(out_md))
    results.append(_check("extract/exits_0", code == 0))
    results.append(_check("extract/md_exists", out_md.exists()))
    if out_md.exists():
        content = out_md.read_text()
        results.append(_check("extract/md_has_text", len(content) > 20))

    # Roundtrip
    unpacked = tmp / "simple-unpacked"
    code, _ = _run("safe-unpack.py", str(fixture), str(unpacked))
    results.append(_check("roundtrip/unpack_ok", code == 0 and unpacked.is_dir()))

    repacked = tmp / "simple-repacked.pptx"
    code, _ = _run("pack.py", str(unpacked), str(repacked), "--original", str(fixture))
    results.append(_check("roundtrip/pack_ok", code == 0 and repacked.exists()))

    if repacked.exists():
        code, data = _run("validate.py", str(repacked))
        results.append(_check("roundtrip/validate_ok", data.get("ok") is True))

    return results


def eval_multi_slide(fixture: Path, tmp: Path) -> list[bool]:
    results = []
    code, data = _run("inspect.py", str(fixture))
    results.append(_check("inspect/slide_count_5", (data.get("flags") or {}).get("slide_count") == 5))
    code, data = _run("validate.py", str(fixture))
    results.append(_check("validate/ok", data.get("ok") is True))

    out_json = tmp / "multi-slide.json"
    code, _ = _run("extract.py", str(fixture), "--format", "json", "-o", str(out_json))
    results.append(_check("extract/json_ok", code == 0 and out_json.exists()))
    if out_json.exists():
        slides_data = json.loads(out_json.read_text()).get("slides", [])
        results.append(_check("extract/json_5_slides", len(slides_data) == 5))
    return results


def eval_tables(fixture: Path, tmp: Path) -> list[bool]:
    results = []
    code, data = _run("validate.py", str(fixture))
    results.append(_check("validate/ok", data.get("ok") is True))

    out_json = tmp / "tables.json"
    code, _ = _run("extract.py", str(fixture), "--format", "json", "-o", str(out_json))
    results.append(_check("extract/json_ok", code == 0 and out_json.exists()))
    if out_json.exists():
        slides = json.loads(out_json.read_text()).get("slides", [])
        has_table = any(s.get("tables") for s in slides)
        results.append(_check("extract/has_table", has_table))
    return results


def eval_speaker_notes(fixture: Path, tmp: Path) -> list[bool]:
    results = []
    code, data = _run("inspect.py", str(fixture))
    results.append(_check("inspect/has_speaker_notes",
                          (data.get("flags") or {}).get("has_speaker_notes") is True))

    out_json = tmp / "notes.json"
    code, _ = _run("extract.py", str(fixture), "--format", "json", "-o", str(out_json))
    results.append(_check("extract/json_ok", code == 0 and out_json.exists()))
    if out_json.exists():
        slides = json.loads(out_json.read_text()).get("slides", [])
        has_notes = any(s.get("notes") for s in slides)
        results.append(_check("extract/notes_present", has_notes))
    return results


def eval_external_rels(fixture: Path, tmp: Path) -> list[bool]:
    results = []
    code, data = _run("inspect.py", str(fixture))
    results.append(_check("inspect/exits_0", code == 0))
    # May or may not flag external links depending on how pptx stores hyperlinks
    results.append(_check("inspect/format_pptx", data.get("format") == "pptx"))

    # Sanitize: strip external-rels
    sanitized = tmp / "external-rels-sanitized.pptx"
    code, report = _run("sanitize.py", str(fixture), "-o", str(sanitized), "--remove", "external-rels")
    results.append(_check("sanitize/exits_0", code == 0, str(report)))
    results.append(_check("sanitize/output_exists", sanitized.exists()))
    return results


def eval_corrupt(fixture: Path, tmp: Path) -> list[bool]:
    results = []
    code, data = _run("validate.py", str(fixture))
    results.append(_check("validate/fails_as_expected", code == 1))
    code, data = _run("inspect.py", str(fixture))
    # inspect should exit 0 even on corrupt (it reports zip_safety)
    zip_ok = (data.get("zip_safety") or {}).get("ok", True)
    results.append(_check("inspect/detects_corrupt", not zip_ok or data.get("format") == "unknown"))
    return results


def eval_zipbomb(fixture: Path, tmp: Path) -> list[bool]:
    results = []
    code, data = _run("inspect.py", str(fixture))
    zip_ok = (data.get("zip_safety") or {}).get("ok", True)
    results.append(_check("inspect/zip_safety_not_ok", not zip_ok))
    return results


def eval_macro_stub(fixture: Path, tmp: Path) -> list[bool]:
    results = []
    code, data = _run("inspect.py", str(fixture))
    results.append(_check("inspect/exits_0", code == 0))
    has_macros = (data.get("flags") or {}).get("has_macros", False)
    results.append(_check("inspect/has_macros", has_macros))

    # Sanitize macros
    sanitized = tmp / "macro-stub-sanitized.pptx"
    code, report = _run("sanitize.py", str(fixture), "-o", str(sanitized), "--remove", "macros")
    results.append(_check("sanitize/exits_0", code in (0, 1)))  # may fail validate due to stub
    if sanitized.exists():
        code2, data2 = _run("inspect.py", str(sanitized))
        still_has = (data2.get("flags") or {}).get("has_macros", True)
        results.append(_check("sanitize/macros_removed", not still_has))
    return results


def eval_generic_roundtrip(fixture: Path, tmp: Path, name: str) -> list[bool]:
    """Generic roundtrip check for any valid fixture."""
    results = []
    code, data = _run("validate.py", str(fixture))
    results.append(_check("validate/ok", data.get("ok") is True))

    unpacked = tmp / f"{name}-unpacked"
    code, _ = _run("safe-unpack.py", str(fixture), str(unpacked))
    results.append(_check("roundtrip/unpack_ok", code == 0 and unpacked.is_dir()))

    repacked = tmp / f"{name}-repacked.pptx"
    code, _ = _run("pack.py", str(unpacked), str(repacked), "--original", str(fixture))
    results.append(_check("roundtrip/pack_ok", code == 0 and repacked.exists()))

    if repacked.exists():
        code, data = _run("validate.py", str(repacked))
        results.append(_check("roundtrip/validate_ok", data.get("ok") is True))

        # Sanitize
        sanitized = tmp / f"{name}-sanitized.pptx"
        code, _ = _run("sanitize.py", str(fixture), "-o", str(sanitized), "--remove", "all")
        results.append(_check("sanitize/exits_0", code == 0))
        if sanitized.exists():
            code, data = _run("validate.py", str(sanitized))
            results.append(_check("sanitize/output_valid", data.get("ok") is True))

    return results


# ---------------------------------------------------------------------------
# Fixture registry
# ---------------------------------------------------------------------------

def run_fixture(name: str, fixture: Path, tmp: Path) -> tuple[int, int]:
    """Run assertions for a fixture. Returns (passed, total)."""
    print(f"\n{name}")
    stem = Path(name).stem

    if stem == "simple":
        results = eval_simple(fixture, tmp)
    elif stem == "multi-slide":
        results = eval_multi_slide(fixture, tmp)
    elif stem == "tables":
        results = eval_tables(fixture, tmp)
    elif stem == "speaker-notes":
        results = eval_speaker_notes(fixture, tmp)
    elif stem == "external-rels":
        results = eval_external_rels(fixture, tmp)
    elif stem == "corrupt":
        results = eval_corrupt(fixture, tmp)
    elif stem == "zipbomb":
        results = eval_zipbomb(fixture, tmp)
    elif stem == "macro-stub":
        results = eval_macro_stub(fixture, tmp)
    else:
        results = eval_generic_roundtrip(fixture, tmp, stem)

    passed = sum(1 for r in results if r)
    return passed, len(results)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="run-evals.py",
        description="Run mechanical eval assertions for the pptx skill.",
    )
    parser.add_argument(
        "--fixtures", default=str(_here / "fixtures"),
        help="Fixtures directory (default: evals/fixtures/)",
    )
    args = parser.parse_args()

    fixtures_dir = Path(args.fixtures)
    if not fixtures_dir.is_dir():
        print(f"error: fixtures directory not found: {fixtures_dir}", file=sys.stderr)
        print("Run: python evals/make-fixtures.py", file=sys.stderr)
        sys.exit(2)

    fixtures = sorted(
        f for f in fixtures_dir.iterdir()
        if f.is_file() and f.suffix.lower() in (".pptx", ".pptm")
    )

    if not fixtures:
        print("No fixture files found. Run: python evals/make-fixtures.py")
        sys.exit(2)

    import tempfile
    total_passed = total_checks = 0

    with tempfile.TemporaryDirectory(prefix="pptx-evals-") as tmp_dir:
        tmp = Path(tmp_dir)
        for fixture in fixtures:
            passed, total = run_fixture(fixture.name, fixture, tmp)
            total_passed += passed
            total_checks += total

    print(f"\n{'='*50}")
    print(f"Results: {total_passed}/{total_checks} passed, {total_checks - total_passed} failed")
    sys.exit(0 if total_passed == total_checks else 1)


if __name__ == "__main__":
    main()
