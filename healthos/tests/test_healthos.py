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
    # Minimal stand-in for ocr.py's library API: RecognizeOptions, recognize(),
    # to_markdown(), OcrError. healthos.py imports this file the same way it
    # imports the real ocr.py (via importlib, not a subprocess).
    #
    # `from __future__ import annotations` matches real ocr.py and matters:
    # it turns dataclass field annotations into strings, which Python 3.14's
    # dataclass machinery resolves via sys.modules[cls.__module__] during
    # class creation — this is what load_ocr_module() must register before
    # exec_module(), or class creation crashes with AttributeError on None.
    from __future__ import annotations

    import os
    from dataclasses import dataclass


    class OcrError(Exception):
        def __init__(self, message, code=2):
            super().__init__(message)
            self.code = code


    @dataclass
    class RecognizeOptions:
        engine: str = "auto"
        vision_api_url: str = ""
        vision_api_key: str = ""
        vision_model: str = ""
        timeout: float | None = None
        verbose: bool = False


    def recognize(path, options=None):
        options = options or RecognizeOptions()
        log = os.environ.get("FAKE_OCR_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(
                    "|".join([str(path), options.engine, options.vision_api_url, options.vision_model])
                    + "\\n"
                )
        name = os.path.basename(str(path)).lower()
        if "child" in name:
            text = "ALEX EXAMPLE 02.03.2020\\n\\n| Test | Result |\\n| --- | --- |\\n| HGB | 120 |"
        elif "adult" in name:
            text = "MORGAN EXAMPLE 01.01.1990"
        elif "stranger" in name:
            text = "UNLISTED PERSON 05.05.1980"
        else:
            text = "text without a name"
        return [{"n": 1, "source": "fake", "mean_conf": 100.0, "flag": None, "text": text, "words": []}]


    def to_markdown(pages, filename):
        parts = [f"# {filename}", ""]
        for page in pages:
            parts.append("## Page " + str(page.get("n")))
            parts.append("")
            parts.append(page.get("text", "").strip())
            parts.append("")
        return "\\n".join(parts)
    '''
)

FAILING_OCR = textwrap.dedent(
    '''\
    class OcrError(Exception):
        def __init__(self, message, code=2):
            super().__init__(message)
            self.code = code


    class RecognizeOptions:
        def __init__(self, **kwargs):
            pass


    def recognize(path, options=None):
        raise OcrError("boom")
    '''
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

    env = {
        "AGENT_HEALTH_SOURCE_DIR": str(source),
        "AGENT_HEALTH_TARGET_DIR": str(target),
        "AGENT_HEALTH_CACHE_DIR": str(cache),
        "AGENT_HEALTH_OCR_ENGINE": "tesseract",
        "AGENT_HEALTH_OCR_SCRIPT": str(ocr_script),
        "AGENT_HEALTH_OCR_TIMEOUT_SECONDS": "30",
        "FAKE_OCR_LOG": str(ocr_log),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return source, target, cache, ocr_log


def ocr_calls(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_mirrors_source_and_reuses_cache(workspace):
    source, target, _, log = workspace
    (source / "child-scan.png").write_bytes(PNG_1X1)

    assert MODULE.main([]) == 0
    output = target / "child-scan.md"
    assert output.is_file()
    content = output.read_text(encoding="utf-8")
    assert "person:" not in content
    assert "assignment_reason:" not in content
    assert 'status: "recognized"' in content
    assert "| HGB | 120 |" in content
    assert "# scan" not in content
    assert len(ocr_calls(log)) == 1

    before = output.read_bytes()
    assert MODULE.main([]) == 0
    assert output.read_bytes() == before
    assert len(ocr_calls(log)) == 1
    assert MODULE.main(["--check"]) == 0


def test_mirrors_nested_source_structure(workspace):
    source, target, _, _ = workspace
    (source / "2023").mkdir()
    (source / "2023" / "child-visit.png").write_bytes(PNG_1X1)
    assert MODULE.main([]) == 0
    assert (target / "2023" / "child-visit.md").is_file()
    assert not (target / "people").exists()
    assert not (target / "_unassigned").exists()


def test_mirror_no_family_yaml_required(workspace):
    source, target, _, _ = workspace
    assert not (target / "family.yaml").exists()
    (source / "stranger-doc.png").write_bytes(PNG_1X1)
    assert MODULE.main([]) == 0
    assert (target / "stranger-doc.md").is_file()


def test_name_collision_gets_sha_prefix(workspace):
    source, target, _, _ = workspace
    # Two different source files with the same stem in the same folder.
    (source / "report.png").write_bytes(PNG_1X1)
    (source / "report.jpg").write_bytes(PNG_1X1 + b"\x00")
    assert MODULE.main([]) == 0
    outputs = sorted(p.name for p in target.glob("*.md"))
    assert "report.md" in outputs
    assert any(name != "report.md" and name.endswith("report.md") for name in outputs)
    assert len(outputs) == 2


def test_run_ocr_passes_engine_to_recognize_options(workspace):
    source, _, _, log = workspace
    (source / "child.png").write_bytes(PNG_1X1)
    config = MODULE.load_config()
    document = MODULE.scan_sources(config.sources)[0]
    markdown = MODULE.run_ocr(config, document)
    assert "| HGB | 120 |" in markdown
    path, engine, vision_url, vision_model = ocr_calls(log)[0].split("|")
    assert engine == "tesseract"
    assert vision_url == ""
    assert vision_model == ""


def test_load_ocr_module_is_cached(tmp_path):
    script = tmp_path / "fake_ocr_cache.py"
    script.write_text(FAKE_OCR, encoding="utf-8")
    first = MODULE.load_ocr_module(script)
    second = MODULE.load_ocr_module(script)
    assert first is second


def test_load_ocr_module_registers_in_sys_modules(tmp_path):
    # Regression guard: the loaded module must be registered in sys.modules
    # *before* exec_module() runs, or dataclasses using
    # `from __future__ import annotations` (like FAKE_OCR and real ocr.py)
    # crash on Python 3.14 while resolving their own field annotations.
    script = tmp_path / "fake_ocr_registered.py"
    script.write_text(FAKE_OCR, encoding="utf-8")
    module = MODULE.load_ocr_module(script)
    assert sys.modules.get(f"healthos_ocr_{script.stem}") is module
    # Constructing a dataclass instance from the loaded module must not raise.
    module.RecognizeOptions(engine="tesseract")


def test_run_ocr_wraps_ocr_module_errors(workspace, monkeypatch):
    source, _, _, _ = workspace
    (source / "child.png").write_bytes(PNG_1X1)
    failing_script = source.parent / "failing_ocr.py"
    failing_script.write_text(FAILING_OCR, encoding="utf-8")
    monkeypatch.setenv("AGENT_HEALTH_OCR_SCRIPT", str(failing_script))
    config = MODULE.load_config()
    document = MODULE.scan_sources(config.sources)[0]
    with pytest.raises(MODULE.RecognitionError, match="boom"):
        MODULE.run_ocr(config, document)


def test_main_stops_and_reports_stderr_on_recognition_error(workspace, monkeypatch, capsys):
    source, target, _, _ = workspace
    (source / "child.png").write_bytes(PNG_1X1)
    failing_script = source.parent / "failing_ocr.py"
    failing_script.write_text(FAILING_OCR, encoding="utf-8")
    monkeypatch.setenv("AGENT_HEALTH_OCR_SCRIPT", str(failing_script))

    assert MODULE.main([]) == 1

    captured = capsys.readouterr()
    assert "Error: child.png: ocr recognition failed: boom" in captured.err
    assert not (target / "child.md").exists()


def test_ocr_timeout_is_optional_and_defaults(workspace, monkeypatch):
    monkeypatch.delenv("AGENT_HEALTH_OCR_TIMEOUT_SECONDS", raising=False)
    assert MODULE.load_config().timeout == 600


def test_ocr_timeout_override_and_validation(workspace, monkeypatch):
    monkeypatch.setenv("AGENT_HEALTH_OCR_TIMEOUT_SECONDS", "45")
    assert MODULE.load_config().timeout == 45
    monkeypatch.setenv("AGENT_HEALTH_OCR_TIMEOUT_SECONDS", "0")
    with pytest.raises(MODULE.RecognitionError, match="positive"):
        MODULE.load_config()


def test_run_ocr_passes_vision_api_fields(workspace, monkeypatch):
    source, _, _, log = workspace
    (source / "child.png").write_bytes(PNG_1X1)
    monkeypatch.setenv("AGENT_HEALTH_OCR_ENGINE", "vision-api")
    monkeypatch.setenv("AGENT_HEALTH_VISION_API_URL", "http://vision.test/v1")
    monkeypatch.setenv("AGENT_HEALTH_VISION_API_KEY", "secret-token")
    monkeypatch.setenv("AGENT_HEALTH_VISION_MODEL", "vision-model")
    config = MODULE.load_config()
    document = MODULE.scan_sources(config.sources)[0]
    MODULE.run_ocr(config, document)
    path, engine, vision_url, vision_model = ocr_calls(log)[0].split("|")
    assert engine == "vision-api"
    assert vision_url == "http://vision.test/v1"
    assert vision_model == "vision-model"
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
    monkeypatch.setenv("AGENT_HEALTH_OCR_ENGINE", "vision-api")
    monkeypatch.setenv("AGENT_HEALTH_VISION_API_URL", "http://vision.test/v1")
    monkeypatch.setenv("AGENT_HEALTH_VISION_API_KEY", "secret-token")
    monkeypatch.setenv("AGENT_HEALTH_VISION_MODEL", "vision-model")
    monkeypatch.delenv(missing)
    with pytest.raises(MODULE.RecognitionError, match=missing):
        MODULE.load_config()


def test_rejects_nested_roots():
    source = Path("/tmp/source")
    with pytest.raises(MODULE.RecognitionError):
        MODULE.validate_roots([source], source / "target", Path("/tmp/cache"))


def test_cache_may_live_inside_target():
    source = Path("/tmp/source")
    target = Path("/tmp/target")
    MODULE.validate_roots([source], target, target / ".cache")


def test_target_inside_cache_is_rejected():
    source = Path("/tmp/source")
    cache = Path("/tmp/cache")
    with pytest.raises(MODULE.RecognitionError, match="nested inside cache"):
        MODULE.validate_roots([source], cache / "target", cache)


def test_target_equals_cache_is_rejected():
    source = Path("/tmp/source")
    shared = Path("/tmp/shared")
    with pytest.raises(MODULE.RecognitionError, match="same path"):
        MODULE.validate_roots([source], shared, shared)


def test_cache_inside_source_is_rejected():
    source = Path("/tmp/source")
    with pytest.raises(MODULE.RecognitionError):
        MODULE.validate_roots([source], Path("/tmp/target"), source / ".cache")


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
    assert (target / "child-scan.md").is_file()
    assert not (target / "recognition-index.md").exists()


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


def test_multiple_source_dirs_mirror_with_prefix(tmp_path, monkeypatch):
    src_child = tmp_path / "child"
    src_adult = tmp_path / "adult"
    target = tmp_path / "target"
    cache = tmp_path / "cache"
    for directory in (src_child, src_adult, target):
        directory.mkdir()
    ocr_script = tmp_path / "fake_ocr.py"
    ocr_script.write_text(FAKE_OCR, encoding="utf-8")
    # With multiple source dirs, each is mirrored under its basename prefix.
    (src_child / "report.png").write_bytes(PNG_1X1)
    (src_adult / "report.png").write_bytes(PNG_1X1)

    env = {
        "AGENT_HEALTH_SOURCE_DIR": f"{src_child}:{src_adult}",
        "AGENT_HEALTH_TARGET_DIR": str(target),
        "AGENT_HEALTH_CACHE_DIR": str(cache),
        "AGENT_HEALTH_OCR_ENGINE": "tesseract",
        "AGENT_HEALTH_OCR_SCRIPT": str(ocr_script),
        "AGENT_HEALTH_OCR_TIMEOUT_SECONDS": "30",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    assert MODULE.main(["index"]) == 0
    assert (target / "child" / "report.md").is_file()
    assert (target / "adult" / "report.md").is_file()
    assert MODULE.main(["index", "--check"]) == 0
