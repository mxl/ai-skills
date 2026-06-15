"""
common.ooxml — Shared OOXML engine for ai-skills.

Provides the generic unpack/pack/validate engine (engine.py) and utilities
(zipsafe, xmlutil, io) consumed by the docx and pptx skills.

Usage from a skill script:
    # At the very top of the script, before any other imports:
    from pathlib import Path as _Path
    import sys as _sys
    _here = _Path(__file__).resolve().parent
    # Locate and add repo root to sys.path:
    from common.ooxml._bootstrap import install as _install  # noqa: E402
    _install(__file__)

    # Then import normally:
    from common.ooxml.engine import FormatProfile, unpack, pack, validate
    from common.ooxml.zipsafe import zip_safety_report
    from common.ooxml.io import emit_json, fail
"""
from .engine import FormatProfile, CheckResult, unpack, pack, validate
from .zipsafe import zip_safety_report, safe_member_path, ZIP_LIMITS
from .xmlutil import (
    pretty_print_xml, condense_xml, parse_xml_bytes,
    register_namespaces, clark, SHARED_NAMESPACES, XML_NS,
    ensure_xml_space_preserve,
)
from .io import sha256_file, emit_json, fail
from ._bootstrap import install

__all__ = [
    # engine
    "FormatProfile", "CheckResult", "unpack", "pack", "validate",
    # zipsafe
    "zip_safety_report", "safe_member_path", "ZIP_LIMITS",
    # xmlutil
    "pretty_print_xml", "condense_xml", "parse_xml_bytes",
    "register_namespaces", "clark", "SHARED_NAMESPACES", "XML_NS",
    "ensure_xml_space_preserve",
    # io
    "sha256_file", "emit_json", "fail",
    # bootstrap
    "install",
]
