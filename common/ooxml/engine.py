"""
Generic OOXML unpack / pack / validate engine.

Parameterized by a FormatProfile so the same engine handles .docx and .pptx
(and any future OOXML format) with format-specific hooks for run-merging,
auto-repair, and validation rules.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

try:
    import defusedxml.ElementTree as _ET
except ImportError:
    import xml.etree.ElementTree as _ET  # type: ignore[no-redef]

import xml.etree.ElementTree as _StdET

from .zipsafe import zip_safety_report, safe_member_path, ZIP_LIMITS
from .xmlutil import pretty_print_xml, condense_xml
from .io import sha256_file, fail


# ---------------------------------------------------------------------------
# CheckResult — validation output
# ---------------------------------------------------------------------------

class CheckResult:
    """Holds the result of a single validation check."""

    def __init__(self, name: str, ok: bool, details: str = "") -> None:
        self.name = name
        self.ok = ok
        self.details = details

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "details": self.details}


# ---------------------------------------------------------------------------
# FormatProfile
# ---------------------------------------------------------------------------

class FormatProfile:
    """
    Describes the format-specific behaviour for the shared engine.

    Subclass and override hooks to customise per format.
    """

    def __init__(
        self,
        name: str,
        required_parts: "list[str] | None" = None,
        meta_filename: str = ".ooxml-meta.json",
        xml_extensions: "set[str] | None" = None,
    ) -> None:
        self.name = name
        self.required_parts: list[str] = required_parts or []
        self.meta_filename = meta_filename
        self.xml_extensions: set[str] = xml_extensions or {".xml", ".rels"}

    def pre_write_transform(self, name: str, data: bytes) -> bytes:
        """
        Called on each XML part during unpack before writing to disk.
        Default: pass-through.
        Override for e.g. docx run-merging.
        """
        return data

    def autorepair(self, name: str, data: bytes) -> tuple[bytes, list[str]]:
        """
        Apply format-specific auto-repair to XML bytes during pack.
        Returns (repaired_bytes, list_of_messages).
        Default: no-op.
        """
        return data, []

    def extra_checks(self, zf: zipfile.ZipFile) -> list[CheckResult]:
        """
        Format-specific validation checks run after the generic ones.
        Default: no extra checks.
        """
        return []


# ---------------------------------------------------------------------------
# Unpack
# ---------------------------------------------------------------------------

def unpack(
    src: Path,
    outdir: Path,
    profile: FormatProfile,
    force: bool = False,
) -> dict[str, Any]:
    """
    Safely unpack an OOXML ZIP into outdir.

    Steps:
    1. ZIP safety check (entry count, size, ratio, path traversal, duplicates).
    2. Traversal-safe extraction.
    3. Pretty-print XML parts; copy binary parts as-is.
    4. Call profile.pre_write_transform on XML parts.
    5. Write <profile.meta_filename> with source path and sha256.

    Returns the meta dict.
    """
    import shutil

    safety = zip_safety_report(src)
    if not safety["ok"] and not force:
        print(
            "error: ZIP safety check failed:\n"
            + "\n".join(f"  - {i}" for i in safety["issues"]),
            file=sys.stderr,
        )
        sys.exit(1)
    elif not safety["ok"] and force:
        print(
            "warning: ZIP safety issues (--force active):\n"
            + "\n".join(f"  - {i}" for i in safety["issues"]),
            file=sys.stderr,
        )

    if outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True)

    with zipfile.ZipFile(src, "r") as zf:
        for info in zf.infolist():
            member_path = safe_member_path(outdir, info.filename)

            if info.filename.endswith("/"):
                member_path.mkdir(parents=True, exist_ok=True)
                continue

            member_path.parent.mkdir(parents=True, exist_ok=True)
            data = zf.read(info.filename)

            suffix = Path(info.filename).suffix.lower()
            if suffix in profile.xml_extensions:
                data = profile.pre_write_transform(info.filename, data)
                data = pretty_print_xml(data)

            member_path.write_bytes(data)

    meta: dict[str, Any] = {
        "source": str(src.resolve()),
        "sha256": sha256_file(src),
        "format": profile.name,
    }
    (outdir / profile.meta_filename).write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    return meta


# ---------------------------------------------------------------------------
# Pack
# ---------------------------------------------------------------------------

def pack(
    unpacked_dir: Path,
    output: Path,
    profile: FormatProfile,
    original: Path | None = None,
    autorepair: bool = True,
    validate: bool = True,
    keep_invalid: bool = False,
    validate_script: Path | None = None,
) -> tuple[bytes, list[str]]:
    """
    Repack an unpacked OOXML directory into a ZIP file.

    Steps:
    1. Collect all files from unpacked_dir (skip meta file).
    2. Optionally merge missing parts from original ZIP.
    3. For XML: optionally auto-repair (profile.autorepair), then condense.
    4. Write deterministic ZIP ([Content_Types].xml first, then sorted).
    5. Optionally validate via validate_script.

    Returns (zip_bytes, repair_messages).
    """
    repairs: list[str] = []
    all_files: dict[str, bytes] = {}

    for path in sorted(unpacked_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.name == profile.meta_filename:
            continue
        rel = path.relative_to(unpacked_dir).as_posix()
        data = path.read_bytes()

        suffix = Path(rel).suffix.lower()
        if suffix in profile.xml_extensions:
            if autorepair:
                data, r = profile.autorepair(rel, data)
                repairs.extend(r)
            data = condense_xml(data)

        all_files[rel] = data

    # Carry over missing parts from original
    if original is not None and original.exists():
        try:
            with zipfile.ZipFile(original, "r") as ozf:
                for orig_name in ozf.namelist():
                    if orig_name not in all_files and not orig_name.endswith("/"):
                        all_files[orig_name] = ozf.read(orig_name)
        except Exception as exc:
            print(
                f"warning: could not read original for missing parts: {exc}",
                file=sys.stderr,
            )

    # Build ZIP with [Content_Types].xml first
    ordered: list[str] = []
    if "[Content_Types].xml" in all_files:
        ordered.append("[Content_Types].xml")
    for name in sorted(all_files):
        if name != "[Content_Types].xml":
            ordered.append(name)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in ordered:
            zf.writestr(name, all_files[name])

    zip_bytes = buf.getvalue()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(zip_bytes)

    for msg in repairs:
        print(f"repair: {msg}", file=sys.stderr)
    print(f"packed -> {output}", file=sys.stderr)

    if validate and validate_script and validate_script.exists():
        result = subprocess.run(
            [sys.executable, str(validate_script), str(output)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(result.stdout, file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            if not keep_invalid:
                output.unlink(missing_ok=True)
                fail(1, "validation failed; output removed (use --keep-invalid to keep)")
            else:
                print(
                    "warning: validation failed but --keep-invalid set",
                    file=sys.stderr,
                )
        else:
            print("validation passed", file=sys.stderr)

    return zip_bytes, repairs


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------

def validate(
    path: Path,
    profile: FormatProfile,
) -> list[CheckResult]:
    """
    Run generic OOXML validation checks plus profile.extra_checks.

    Checks:
    1. ZIP integrity
    2. Required parts present
    3. Content-type coverage (every part has a Default or Override)
    4. Relationship targets resolve (Internal) or are marked External
    5. All *.xml and *.rels are well-formed
    6. profile.extra_checks(zf)

    Returns a list of CheckResult. Caller decides the exit code.
    """
    results: list[CheckResult] = []

    # 1 — ZIP integrity
    try:
        with zipfile.ZipFile(path, "r") as zf:
            bad = zf.testzip()
        if bad:
            results.append(CheckResult("zip_integrity", False, f"bad entry: {bad}"))
        else:
            results.append(CheckResult("zip_integrity", True))
    except zipfile.BadZipFile as exc:
        results.append(CheckResult("zip_integrity", False, str(exc)))
        return results  # Cannot continue if ZIP is unreadable

    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())

        # 2 — Required parts
        missing = [p for p in profile.required_parts if p not in names]
        if missing:
            results.append(CheckResult(
                "required_parts", False, f"missing: {missing}"
            ))
        else:
            results.append(CheckResult("required_parts", True))

        # 3 — Content-type coverage
        ct_check = _check_content_types(zf, names)
        results.append(ct_check)

        # 4 — Relationship targets resolve
        rel_check = _check_relationships(zf, names)
        results.append(rel_check)

        # 5 — Well-formed XML
        xml_check = _check_xml_wellformed(zf, names, profile.xml_extensions)
        results.append(xml_check)

        # 6 — Format-specific extra checks
        results.extend(profile.extra_checks(zf))

    return results


def _check_content_types(
    zf: zipfile.ZipFile,
    names: set[str],
) -> CheckResult:
    """Every part must be covered by a Default or Override content-type."""
    try:
        ct_data = zf.read("[Content_Types].xml")
        root = _ET.fromstring(ct_data)
    except Exception as exc:
        return CheckResult("content_types", False, f"cannot parse [Content_Types].xml: {exc}")

    ct_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    defaults: set[str] = set()
    overrides: set[str] = set()

    for el in root:
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "Default":
            ext = el.get("Extension", "")
            if ext:
                defaults.add(ext.lower())
        elif tag == "Override":
            part = el.get("PartName", "")
            if part:
                overrides.add(part.lstrip("/"))

    uncovered: list[str] = []
    for name in names:
        if name.endswith("/"):
            continue
        if name == "[Content_Types].xml":
            continue
        # .rels files are package infrastructure; they are not required to have
        # a content-type entry per the OOXML spec.
        if name.endswith(".rels"):
            continue
        ext = Path(name).suffix.lstrip(".").lower()
        if not ext:
            continue
        if name not in overrides and ext not in defaults:
            uncovered.append(name)

    if uncovered:
        return CheckResult(
            "content_types", False,
            f"uncovered parts: {uncovered[:5]}{'...' if len(uncovered) > 5 else ''}"
        )
    return CheckResult("content_types", True)


def _check_relationships(
    zf: zipfile.ZipFile,
    names: set[str],
) -> CheckResult:
    """Internal relationship targets must point to existing parts."""
    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    broken: list[str] = []

    for name in names:
        if not name.endswith(".rels"):
            continue
        try:
            data = zf.read(name)
            root = _ET.fromstring(data)
        except Exception:
            continue

        # Base dir: _rels files sit in <dir>/_rels/<file>.rels
        # The part they describe is <dir>/<file without .rels>
        rels_path = Path(name)
        base_dir = rels_path.parent.parent  # e.g. ppt/_rels → ppt

        for el in root:
            tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if tag != "Relationship":
                continue
            target_mode = el.get("TargetMode", "Internal")
            target = el.get("Target", "")
            rel_id = el.get("Id", "?")

            if target_mode == "External":
                continue  # External refs are allowed, not resolved here
            if not target:
                continue

            # Resolve target relative to base_dir
            if target.startswith("/"):
                resolved = target.lstrip("/")
            else:
                resolved = (base_dir / target).as_posix()
                # Normalise away any ..
                parts = []
                for part in resolved.split("/"):
                    if part == "..":
                        if parts:
                            parts.pop()
                    elif part and part != ".":
                        parts.append(part)
                resolved = "/".join(parts)

            if resolved and resolved not in names:
                broken.append(f"{name}:{rel_id} → {resolved!r} (not found)")

    if broken:
        return CheckResult(
            "relationships", False,
            "; ".join(broken[:3]) + ("..." if len(broken) > 3 else "")
        )
    return CheckResult("relationships", True)


def _check_xml_wellformed(
    zf: zipfile.ZipFile,
    names: set[str],
    xml_extensions: set[str],
) -> CheckResult:
    """All XML and .rels files must be well-formed."""
    bad: list[str] = []
    for name in names:
        if Path(name).suffix.lower() not in xml_extensions:
            continue
        try:
            data = zf.read(name)
            _ET.fromstring(data)
        except Exception as exc:
            bad.append(f"{name}: {exc}")

    if bad:
        return CheckResult(
            "xml_wellformed", False,
            "; ".join(bad[:3]) + ("..." if len(bad) > 3 else "")
        )
    return CheckResult("xml_wellformed", True)
