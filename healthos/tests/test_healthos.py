import importlib.util
import os
import sys
import textwrap
from pathlib import Path

import pytest
import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "healthos.py"
SPEC = importlib.util.spec_from_file_location("healthos_recognize", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000000020001e221bc330000000049454e44ae426082"
)

FAKE_OCR = textwrap.dedent(
    '''\
    import argparse, os, sys
    p = argparse.ArgumentParser()
    p.add_argument("inputs", nargs="+")
    p.add_argument("--engine", default="auto")
    p.add_argument("--format", default="md")
    p.add_argument("--vision-api-url", default="")
    p.add_argument("--vision-api-key", default="")
    p.add_argument("--vision-model", default="")
    a = p.parse_args()
    log = os.environ.get("FAKE_OCR_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write("|".join([a.inputs[0], a.engine, a.vision_api_url, a.vision_model]) + "\\n")
    name = os.path.basename(a.inputs[0]).lower()
    if "child" in name:
        body = "# scan\\n\\nALEX EXAMPLE 02.03.2020\\n\\n| Test | Result |\\n| --- | --- |\\n| HGB | 120 |"
    elif "adult" in name:
        body = "# doc\\n\\nMORGAN EXAMPLE 01.01.1990"
    elif "stranger" in name:
        body = "# doc\\n\\nUNLISTED PERSON 05.05.1980"
    else:
        body = "# doc\\n\\ntext without a name"
    sys.stdout.write(body + "\\n")
    '''
)


def write_family(target: Path, *, ambiguous_roots: bool = False) -> None:
    child_root = "." if ambiguous_roots else "child"
    (target / "family.yaml").write_text(
        textwrap.dedent(
            f"""\
            people:
              - id: child
                names:
                  - Alex Example
                birth_date: 2020-03-02
                source_roots:
                  - {child_root}
              - id: adult
                names:
                  - Morgan Example
                birth_date: 1990-01-01
                source_roots:
                  - adult
            """
        ),
        encoding="utf-8",
    )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    cache = tmp_path / "cache"
    source.mkdir()
    target.mkdir()
    ocr_script = tmp_path / "fake_ocr.py"
    ocr_script.write_text(FAKE_OCR, encoding="utf-8")
    ocr_log = tmp_path / "ocr_calls.log"
    write_family(target)

    env = {
        "AGENT_HEALTH_SOURCE_DIR": str(source),
        "AGENT_HEALTH_TARGET_DIR": str(target),
        "AGENT_HEALTH_CACHE_DIR": str(cache),
        "AGENT_HEALTH_ENGINE": "tesseract",
        "AGENT_HEALTH_OCR_SCRIPT": str(ocr_script),
        "AGENT_HEALTH_TIMEOUT_SECONDS": "30",
        "FAKE_OCR_LOG": str(ocr_log),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return source, target, cache, ocr_log


def ocr_calls(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_recognizes_routes_and_reuses_cache(workspace):
    source, target, _, log = workspace
    (source / "child-scan.png").write_bytes(PNG_1X1)

    assert MODULE.main([]) == 0
    output = target / "people" / "child" / "unknown" / "child-scan.md"
    assert output.is_file()
    content = output.read_text(encoding="utf-8")
    assert 'person: "child"' in content
    assert "| HGB | 120 |" in content
    assert "# scan" not in content
    assert len(ocr_calls(log)) == 1

    before = output.read_bytes()
    assert MODULE.main([]) == 0
    assert output.read_bytes() == before
    assert len(ocr_calls(log)) == 1
    assert MODULE.main(["--check"]) == 0


def test_routes_by_year_path(workspace):
    source, target, _, _ = workspace
    (source / "2023").mkdir()
    (source / "2023" / "child-visit.png").write_bytes(PNG_1X1)
    assert MODULE.main([]) == 0
    assert (target / "people" / "child" / "2023" / "child-visit.md").is_file()


def test_unmatched_identity_goes_to_unassigned(workspace):
    source, target, _, _ = workspace
    (source / "stranger-doc.png").write_bytes(PNG_1X1)
    assert MODULE.main([]) == 0
    output = target / "_unassigned" / "unknown" / "stranger-doc.md"
    assert output.is_file()
    assert 'status: "unassigned"' in output.read_text(encoding="utf-8")


def test_build_ocr_command_passes_engine(workspace):
    source, _, _, _ = workspace
    document_path = source / "child.png"
    document_path.write_bytes(PNG_1X1)
    config = MODULE.load_config()
    document = MODULE.scan_source(source)[0]
    command = MODULE.build_ocr_command(config, document)
    assert command[-4:] == ["--engine", "tesseract", "--format", "md"]


def test_vision_api_env_maps_to_ocr_flags(workspace, monkeypatch):
    source, _, _, _ = workspace
    (source / "child.png").write_bytes(PNG_1X1)
    monkeypatch.setenv("AGENT_HEALTH_ENGINE", "vision-api")
    monkeypatch.setenv("AGENT_HEALTH_VISION_API_URL", "http://vision.test/v1")
    monkeypatch.setenv("AGENT_HEALTH_VISION_API_KEY", "secret-token")
    monkeypatch.setenv("AGENT_HEALTH_VISION_MODEL", "vision-model")
    config = MODULE.load_config()
    command = MODULE.build_ocr_command(config, MODULE.scan_source(source)[0])
    assert command[-6:] == [
        "--vision-api-url",
        "http://vision.test/v1",
        "--vision-api-key",
        "secret-token",
        "--vision-model",
        "vision-model",
    ]
    assert "secret-token" not in config.profile_hash


@pytest.mark.parametrize(
    "missing",
    [
        "AGENT_HEALTH_VISION_API_URL",
        "AGENT_HEALTH_VISION_API_KEY",
        "AGENT_HEALTH_VISION_MODEL",
    ],
)
def test_vision_api_requires_all_config(workspace, monkeypatch, missing):
    monkeypatch.setenv("AGENT_HEALTH_ENGINE", "vision-api")
    monkeypatch.setenv("AGENT_HEALTH_VISION_API_URL", "http://vision.test/v1")
    monkeypatch.setenv("AGENT_HEALTH_VISION_API_KEY", "secret-token")
    monkeypatch.setenv("AGENT_HEALTH_VISION_MODEL", "vision-model")
    monkeypatch.delenv(missing)
    with pytest.raises(MODULE.RecognitionError, match=missing):
        MODULE.load_config()


def test_identity_matching(tmp_path):
    write_family(tmp_path)
    people = MODULE.load_family(tmp_path)
    assert MODULE.assign_person(
        "ALEX EXAMPLE 02.03.2020", "report.png", people
    ) == ("child", "identity_match")
    assert MODULE.assign_person(
        "Patient: Alex Example", "report.png", people
    ) == ("child", "identity_match")
    assert MODULE.assign_person(
        "Alex Example and Morgan Example", "report.png", people
    ) == (None, "identity_conflict")


def test_source_root_fallback(tmp_path):
    write_family(tmp_path)
    people = MODULE.load_family(tmp_path)
    assert MODULE.assign_person("text without a name", "adult/report.png", people) == (
        "adult",
        "source_root_match",
    )
    assert MODULE.assign_person("text without a name", "other/report.png", people) == (
        None,
        "identity_missing",
    )


def test_ambiguous_source_roots_are_unassigned(tmp_path):
    write_family(tmp_path, ambiguous_roots=True)
    people = MODULE.load_family(tmp_path)
    assert MODULE.assign_person("text without a name", "adult/report.png", people) == (
        None,
        "identity_missing",
    )


def test_rejects_nested_roots():
    source = Path("/tmp/source")
    with pytest.raises(MODULE.RecognitionError):
        MODULE.validate_roots([source], source / "target", Path("/tmp/cache"))


def test_yaml_scalar_sanitizes_control_chars():
    scalar = MODULE.yaml_scalar('line1\nline2\ttab "quote"')
    parsed = yaml.safe_load(f"value: {scalar}")
    assert "\n" not in parsed["value"]
    assert "\t" not in parsed["value"]


def test_strip_leading_h1():
    assert MODULE.strip_leading_h1("# Title\n\nbody") == "body"
    assert MODULE.strip_leading_h1("no heading\n\nmore") == "no heading\n\nmore"


def test_index_command_matches_default(workspace):
    source, target, _, _ = workspace
    (source / "child-scan.png").write_bytes(PNG_1X1)
    assert MODULE.main(["index"]) == 0
    assert (target / "recognition-index.md").is_file()
    assert (target / "people" / "child" / "unknown" / "child-scan.md").is_file()


def test_parse_source_dirs_splits_on_colon(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    assert MODULE.parse_source_dirs(f"{first}:{second}") == (
        first.resolve(),
        second.resolve(),
    )


def test_parse_source_dirs_rejects_duplicate_names(tmp_path):
    first = tmp_path / "a" / "docs"
    second = tmp_path / "b" / "docs"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    with pytest.raises(MODULE.RecognitionError, match="unique names"):
        MODULE.parse_source_dirs(f"{first}:{second}")


def test_multiple_source_dirs_route_by_basename(tmp_path, monkeypatch):
    src_child = tmp_path / "child"
    src_adult = tmp_path / "adult"
    target = tmp_path / "target"
    cache = tmp_path / "cache"
    for directory in (src_child, src_adult, target):
        directory.mkdir()
    ocr_script = tmp_path / "fake_ocr.py"
    ocr_script.write_text(FAKE_OCR, encoding="utf-8")
    write_family(target)
    # Neutral filenames so routing relies on the source-dir basename prefix,
    # which maps to each person's configured source_root.
    (src_child / "report.png").write_bytes(PNG_1X1)
    (src_adult / "report.png").write_bytes(PNG_1X1)

    env = {
        "AGENT_HEALTH_SOURCE_DIR": f"{src_child}:{src_adult}",
        "AGENT_HEALTH_TARGET_DIR": str(target),
        "AGENT_HEALTH_CACHE_DIR": str(cache),
        "AGENT_HEALTH_ENGINE": "tesseract",
        "AGENT_HEALTH_OCR_SCRIPT": str(ocr_script),
        "AGENT_HEALTH_TIMEOUT_SECONDS": "30",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    assert MODULE.main(["index"]) == 0
    assert (target / "people" / "child" / "unknown" / "report.md").is_file()
    assert (target / "people" / "adult" / "unknown" / "report.md").is_file()
    assert MODULE.main(["index", "--check"]) == 0
