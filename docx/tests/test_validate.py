"""Regression tests for DOCX package validation."""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Iterable

import pytest


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def run_validator(scripts_dir: Path, path: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(scripts_dir / "validate.py"), str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    payload = json.loads(result.stdout)
    payload["exit_code"] = result.returncode
    return payload


def load_validate_generated_module(scripts_dir: Path):
    import importlib.util

    script_path = scripts_dir / "validate-generated-docx.py"
    spec = importlib.util.spec_from_file_location("validate_generated_docx", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(report: dict, name: str) -> dict:
    return next(item for item in report["checks"] if item["name"] == name)


def rewrite_docx(src: Path, dst: Path, replacements: dict[str, bytes | None]) -> None:
    with zipfile.ZipFile(src, "r") as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            if info.filename in replacements:
                data = replacements[info.filename]
                if data is None:
                    continue
            else:
                data = zin.read(info.filename)
            zout.writestr(info, data)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "generated_docx" not in metafunc.fixturenames:
        return

    project_root = Path(__file__).parent.parent.parent.resolve()
    paths = [
        path for path in sorted(project_root.glob("*.docx"))
        if not path.name.startswith("~$")
    ]
    metafunc.parametrize("generated_docx", paths, ids=[path.name for path in paths])


def check_names(report: dict) -> Iterable[str]:
    return (item["name"] for item in report["checks"])


@pytest.fixture()
def simple_docx(fixtures_dir: Path) -> Path:
    return fixtures_dir / "simple.docx"


def test_valid_fixture_passes_all_automated_checks(scripts_dir: Path, simple_docx: Path) -> None:
    report = run_validator(scripts_dir, simple_docx)

    assert report["exit_code"] == 0
    assert report["ok"] is True
    assert set(check_names(report)) == {
        "zip_integrity",
        "required_parts",
        "content_types",
        "relationships",
        "wellformed_xml",
        "tracked_changes",
        "comments",
    }


def test_rejects_corrupt_zip(scripts_dir: Path, tmp_path: Path) -> None:
    bad_docx = tmp_path / "corrupt.docx"
    bad_docx.write_bytes(b"not a zip")

    report = run_validator(scripts_dir, bad_docx)

    assert report["exit_code"] == 1
    assert check(report, "zip_integrity")["ok"] is False


def test_rejects_missing_required_ooxml_part(scripts_dir: Path, simple_docx: Path, tmp_path: Path) -> None:
    bad_docx = tmp_path / "missing-document.docx"
    rewrite_docx(simple_docx, bad_docx, {"word/document.xml": None})

    report = run_validator(scripts_dir, bad_docx)

    assert report["exit_code"] == 1
    assert check(report, "required_parts")["ok"] is False


def test_rejects_missing_office_document_relationship(scripts_dir: Path, simple_docx: Path, tmp_path: Path) -> None:
    bad_docx = tmp_path / "missing-office-document-rel.docx"
    rels_path = "_rels/.rels"
    with zipfile.ZipFile(simple_docx, "r") as zin:
        rels = zin.read(rels_path).replace(
            b"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
            b"http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties",
        )
    rewrite_docx(simple_docx, bad_docx, {rels_path: rels})

    report = run_validator(scripts_dir, bad_docx)

    assert report["exit_code"] == 1
    required_parts = check(report, "required_parts")
    assert required_parts["ok"] is False
    assert "officeDocument" in required_parts["details"]


def test_rejects_uncovered_content_type(scripts_dir: Path, simple_docx: Path, tmp_path: Path) -> None:
    bad_docx = tmp_path / "missing-content-type.docx"
    with zipfile.ZipFile(simple_docx, "r") as zin, zipfile.ZipFile(bad_docx, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            zout.writestr(info, zin.read(info.filename))
        zout.writestr("word/extra.unregistered", b"payload")

    report = run_validator(scripts_dir, bad_docx)
    assert report["exit_code"] == 1
    assert check(report, "content_types")["ok"] is False


def test_rejects_wrong_document_content_type(scripts_dir: Path, simple_docx: Path, tmp_path: Path) -> None:
    bad_docx = tmp_path / "wrong-document-content-type.docx"
    content_types_path = "[Content_Types].xml"
    with zipfile.ZipFile(simple_docx, "r") as zin:
        content_types = zin.read(content_types_path).replace(
            b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
            b"application/xml",
        )
    rewrite_docx(simple_docx, bad_docx, {content_types_path: content_types})

    report = run_validator(scripts_dir, bad_docx)

    assert report["exit_code"] == 1
    content_types_check = check(report, "content_types")
    assert content_types_check["ok"] is False
    assert "word/document.xml" in content_types_check["details"]


def test_rejects_broken_relationship_target(scripts_dir: Path, simple_docx: Path, tmp_path: Path) -> None:
    bad_docx = tmp_path / "broken-rel.docx"
    rels_path = "_rels/.rels"
    with zipfile.ZipFile(simple_docx, "r") as zin:
        rels = zin.read(rels_path).replace(b"word/document.xml", b"word/missing.xml")
    rewrite_docx(simple_docx, bad_docx, {rels_path: rels})

    report = run_validator(scripts_dir, bad_docx)
    assert report["exit_code"] == 1
    assert check(report, "relationships")["ok"] is False


def test_rejects_nested_tracked_changes(scripts_dir: Path, simple_docx: Path, tmp_path: Path) -> None:
    bad_docx = tmp_path / "nested-tracked-change.docx"
    document_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    <w:p>
      <w:ins w:id="1" w:author="Reviewer" w:date="2024-01-01T00:00:00Z">
        <w:del w:id="2" w:author="Reviewer" w:date="2024-01-01T00:00:00Z">
          <w:r><w:delText>bad</w:delText></w:r>
        </w:del>
      </w:ins>
    </w:p>
  </w:body>
</w:document>
'''.encode()
    rewrite_docx(simple_docx, bad_docx, {"word/document.xml": document_xml})

    report = run_validator(scripts_dir, bad_docx)
    assert report["exit_code"] == 1
    tracked_changes = check(report, "tracked_changes")
    assert tracked_changes["ok"] is False
    assert "nested tracked change" in tracked_changes["details"]


def test_generated_docx_discovery_excludes_root_word_lock_files(scripts_dir: Path, simple_docx: Path, tmp_path: Path) -> None:
    module = load_validate_generated_module(scripts_dir)
    normal_docx = tmp_path / "normal.docx"
    lock_docx = tmp_path / "~$normal.docx"
    normal_docx.write_bytes(simple_docx.read_bytes())
    lock_docx.write_bytes(b"not a zip")

    discovered = module.discover_docx_files(tmp_path, module.DEFAULT_EXCLUDES)

    assert discovered == [normal_docx]


def test_generated_docx_outputs_validate(scripts_dir: Path, generated_docx: Path) -> None:
    report = run_validator(scripts_dir, generated_docx)

    assert report["exit_code"] == 0, json.dumps(report, indent=2)
    assert report["ok"] is True
