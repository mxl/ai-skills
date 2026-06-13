#!/usr/bin/env python3
"""
run-evals.py — Run mechanical assertions against all fixtures.

For each fixture, runs inspect/validate/extract and checks expected outcomes.

Exit codes:
  0  all assertions passed
  1  one or more assertions failed
  2  usage error
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


SCRIPTS = Path(__file__).parent.parent / "scripts"
FIXTURES = Path(__file__).parent / "fixtures"


def _find_python() -> str:
    """Return a python3 executable that has python-docx available."""
    import shutil
    # First try sys.executable itself
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
    return sys.executable  # fallback even if docx missing


_PYTHON = _find_python()


def _run(script: str, *args: str) -> tuple[int, dict | str]:
    """Run a script and return (returncode, parsed_json_or_raw_stdout)."""
    cmd = [_PYTHON, str(SCRIPTS / script)] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return result.returncode, json.loads(result.stdout)
    except Exception:
        return result.returncode, result.stdout.strip()


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------

def _assert(results: list, fixture: str, check: str, ok: bool, detail: str = "") -> None:
    results.append({
        "fixture": fixture,
        "check": check,
        "ok": ok,
        "detail": detail,
    })
    status = "PASS" if ok else "FAIL"
    suffix = f" — {detail}" if detail and not ok else ""
    print(f"  [{status}] {check}{suffix}")


# ---------------------------------------------------------------------------
# Per-fixture assertions
# ---------------------------------------------------------------------------

def assert_simple(results: list, fixture_path: Path) -> None:
    name = fixture_path.name
    code, report = _run("validate.py", str(fixture_path))
    _assert(results, name, "validate/passes", code == 0)

    code, data = _run("inspect.py", str(fixture_path))
    _assert(results, name, "inspect/format=docx", isinstance(data, dict) and data.get("format") == "docx")
    # python-docx default template includes customXml/ — check only security-relevant flags
    security_flags = {"has_macros", "has_external_links", "has_comments",
                      "has_tracked_changes", "has_hidden_text", "has_embedded_objects"}
    flags = data.get("flags", {}) if isinstance(data, dict) else {}
    active_security = {k: v for k, v in flags.items() if k in security_flags and v}
    _assert(results, name, "inspect/no_security_flags", len(active_security) == 0, str(active_security))

    code, md = _run("extract.py", str(fixture_path), "--format", "md")
    _assert(results, name, "extract/has_heading", "Simple Document" in str(md))
    _assert(results, name, "extract/has_sections", "Section One" in str(md) and "Section Two" in str(md))


def assert_report_toc(results: list, fixture_path: Path) -> None:
    name = fixture_path.name
    code, report = _run("validate.py", str(fixture_path))
    _assert(results, name, "validate/passes", code == 0)

    code, md = _run("extract.py", str(fixture_path), "--format", "md")
    _assert(results, name, "extract/h1_present", "Annual Report" in str(md))
    _assert(results, name, "extract/h2_present", "Financial Overview" in str(md))
    _assert(results, name, "extract/h3_present", "Revenue Breakdown" in str(md))


def assert_tables(results: list, fixture_path: Path) -> None:
    name = fixture_path.name
    code, report = _run("validate.py", str(fixture_path))
    _assert(results, name, "validate/passes", code == 0)

    code, data = _run("extract.py", str(fixture_path), "--format", "json")
    if isinstance(data, dict):
        tables = data.get("tables", [])
        _assert(results, name, "extract/has_table", len(tables) > 0)
        if tables:
            headers = tables[0][0] if tables[0] else []
            _assert(results, name, "extract/table_headers", "Name" in headers)
    else:
        _assert(results, name, "extract/json_parseable", False, str(data)[:100])


def assert_images_alt(results: list, fixture_path: Path) -> None:
    name = fixture_path.name
    code, report = _run("validate.py", str(fixture_path))
    _assert(results, name, "validate/passes", code == 0)

    code, data = _run("inspect.py", str(fixture_path))
    _assert(results, name, "inspect/has_media", isinstance(data, dict) and data.get("media_count", 0) > 0)


def assert_headers_footers(results: list, fixture_path: Path) -> None:
    name = fixture_path.name
    code, report = _run("validate.py", str(fixture_path))
    _assert(results, name, "validate/passes", code == 0)

    code, data = _run("extract.py", str(fixture_path), "--format", "json")
    if isinstance(data, dict):
        headers = data.get("headers", [])
        footers = data.get("footers", [])
        _assert(results, name, "extract/has_header", len(headers) > 0, str(headers))
        _assert(results, name, "extract/has_footer", len(footers) > 0, str(footers))
    else:
        _assert(results, name, "extract/json_parseable", False)


def assert_comments(results: list, fixture_path: Path) -> None:
    name = fixture_path.name
    code, report = _run("validate.py", str(fixture_path))
    _assert(results, name, "validate/passes", code == 0)

    code, data = _run("inspect.py", str(fixture_path))
    _assert(results, name, "inspect/has_comments", isinstance(data, dict) and data.get("flags", {}).get("has_comments", False))

    code, jdata = _run("extract.py", str(fixture_path), "--format", "json")
    if isinstance(jdata, dict):
        comments = jdata.get("comments", [])
        _assert(results, name, "extract/has_comments", len(comments) > 0, f"found {len(comments)}")
    else:
        _assert(results, name, "extract/json_parseable", False)


def assert_tracked_changes(results: list, fixture_path: Path) -> None:
    name = fixture_path.name
    code, report = _run("validate.py", str(fixture_path))
    _assert(results, name, "validate/passes", code == 0)

    code, data = _run("inspect.py", str(fixture_path))
    _assert(
        results, name, "inspect/has_tracked_changes",
        isinstance(data, dict) and data.get("flags", {}).get("has_tracked_changes", False),
    )


def assert_hidden_custom(results: list, fixture_path: Path) -> None:
    name = fixture_path.name
    code, report = _run("validate.py", str(fixture_path))
    _assert(results, name, "validate/passes", code == 0)

    code, data = _run("inspect.py", str(fixture_path))
    _assert(
        results, name, "inspect/has_hidden_text",
        isinstance(data, dict) and data.get("flags", {}).get("has_hidden_text", False),
    )


def assert_external_rels(results: list, fixture_path: Path) -> None:
    name = fixture_path.name
    code, report = _run("validate.py", str(fixture_path))
    _assert(results, name, "validate/passes", code == 0)

    code, data = _run("inspect.py", str(fixture_path))
    _assert(
        results, name, "inspect/has_external_links",
        isinstance(data, dict) and data.get("flags", {}).get("has_external_links", False),
    )


def assert_corrupt(results: list, fixture_path: Path) -> None:
    name = fixture_path.name
    code, report = _run("validate.py", str(fixture_path))
    _assert(results, name, "validate/fails_as_expected", code != 0, f"exit={code}")


def assert_zipbomb(results: list, fixture_path: Path) -> None:
    name = fixture_path.name
    code, data = _run("inspect.py", str(fixture_path))
    if isinstance(data, dict):
        safety = data.get("zip_safety", {})
        _assert(results, name, "inspect/zip_safety_not_ok", not safety.get("ok", True), str(safety.get("issues", [])))
    else:
        _assert(results, name, "inspect/json_parseable", False)


def assert_scanned_image(results: list, fixture_path: Path) -> None:
    name = fixture_path.name
    code, data = _run("inspect.py", str(fixture_path))
    _assert(results, name, "inspect/has_media", isinstance(data, dict) and data.get("media_count", 0) > 0)


# ---------------------------------------------------------------------------
# Sanitize roundtrip assertions
# ---------------------------------------------------------------------------

def assert_sanitize_all(results: list, fixture_path: Path, tmp_dir: Path) -> None:
    name = fixture_path.name
    out = tmp_dir / f"sanitized_{name}"

    code, report = _run(
        "sanitize.py", str(fixture_path),
        "-o", str(out),
        "--remove", "metadata,comments,revisions,hidden-text,external-rels,macros,embedded-objects",
        "--accept-revisions",
    )
    _assert(results, name, "sanitize/exits_0", code == 0, str(report)[:200] if code != 0 else "")

    if code == 0 and out.exists():
        code2, vreport = _run("validate.py", str(out))
        _assert(results, name, "sanitize/output_valid", code2 == 0)


# ---------------------------------------------------------------------------
# Roundtrip assertion (unpack -> pack -> validate)
# ---------------------------------------------------------------------------

def assert_roundtrip(results: list, fixture_path: Path, tmp_dir: Path) -> None:
    name = fixture_path.name
    unpack_dir = tmp_dir / f"unpack_{fixture_path.stem}"
    repacked = tmp_dir / f"repacked_{name}"

    code1, _ = _run("safe-unpack.py", str(fixture_path), str(unpack_dir))
    _assert(results, name, "roundtrip/unpack_ok", code1 == 0)

    if code1 == 0:
        code2, _ = _run("pack.py", str(unpack_dir), str(repacked),
                        "--original", str(fixture_path), "--no-validate")
        _assert(results, name, "roundtrip/pack_ok", code2 == 0)

        if code2 == 0 and repacked.exists():
            code3, vreport = _run("validate.py", str(repacked))
            _assert(results, name, "roundtrip/validate_ok", code3 == 0)


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

ASSERTIONS = {
    "simple.docx":          assert_simple,
    "report-toc.docx":      assert_report_toc,
    "tables.docx":          assert_tables,
    "images-alt.docx":      assert_images_alt,
    "headers-footers.docx": assert_headers_footers,
    "comments.docx":        assert_comments,
    "tracked-changes.docx": assert_tracked_changes,
    "hidden-custom.docx":   assert_hidden_custom,
    "external-rels.docx":   assert_external_rels,
    "corrupt.docx":         assert_corrupt,
    "zipbomb.docx":         assert_zipbomb,
    "scanned-image.docx":   assert_scanned_image,
}

ROUNDTRIP_SKIP = {"corrupt.docx", "zipbomb.docx"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="run-evals.py",
        description="Run mechanical assertions against all DOCX fixtures.",
    )
    parser.add_argument(
        "--fixtures", default=str(FIXTURES),
        help=f"Fixtures directory (default: {FIXTURES})",
    )
    parser.add_argument(
        "--only", metavar="NAME",
        help="Run assertions only for this fixture",
    )
    parser.add_argument(
        "--no-roundtrip", action="store_true",
        help="Skip roundtrip (unpack/pack) assertions",
    )
    parser.add_argument(
        "--no-sanitize", action="store_true",
        help="Skip sanitize roundtrip assertions",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print final JSON report to stdout",
    )
    args = parser.parse_args()

    fixtures_dir = Path(args.fixtures)
    if not fixtures_dir.exists():
        print(f"error: fixtures directory not found: {fixtures_dir}", file=sys.stderr)
        print("Run: python evals/make-fixtures.py", file=sys.stderr)
        sys.exit(2)

    import tempfile
    results: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="docx-evals-") as tmp:
        tmp_dir = Path(tmp)

        for fixture_name, assert_fn in ASSERTIONS.items():
            if args.only and fixture_name != args.only:
                continue
            fixture_path = fixtures_dir / fixture_name
            if not fixture_path.exists():
                print(f"\n[SKIP] {fixture_name} — not found (run make-fixtures.py)")
                continue

            print(f"\n{fixture_name}")
            assert_fn(results, fixture_path)

            if not args.no_roundtrip and fixture_name not in ROUNDTRIP_SKIP:
                assert_roundtrip(results, fixture_path, tmp_dir)

            if not args.no_sanitize and fixture_name not in ROUNDTRIP_SKIP:
                assert_sanitize_all(results, fixture_path, tmp_dir)

    # Summary
    total  = len(results)
    passed = sum(1 for r in results if r["ok"])
    failed = total - passed

    print(f"\n{'='*50}")
    print(f"Results: {passed}/{total} passed, {failed} failed")

    if args.json:
        print(json.dumps({"summary": {"total": total, "passed": passed, "failed": failed}, "results": results}, indent=2))

    if failed > 0:
        print("\nFailed checks:")
        for r in results:
            if not r["ok"]:
                print(f"  {r['fixture']} / {r['check']}: {r.get('detail', '')}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
