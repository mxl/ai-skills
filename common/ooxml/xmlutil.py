"""XML utilities for OOXML skill scripts."""
from __future__ import annotations

import xml.etree.ElementTree as _StdET

# ---------------------------------------------------------------------------
# defusedxml loader
# ---------------------------------------------------------------------------

try:
    import defusedxml.minidom as _minidom
    import defusedxml.ElementTree as _ET
    DEFUSEDXML_AVAILABLE = True
except ImportError:
    import xml.dom.minidom as _minidom  # type: ignore[no-redef]
    import xml.etree.ElementTree as _ET  # type: ignore[no-redef]
    DEFUSEDXML_AVAILABLE = False


def parse_xml_bytes(data: bytes):
    """Parse XML bytes using defusedxml if available, else stdlib ET."""
    return _ET.fromstring(data)


# ---------------------------------------------------------------------------
# Package-level OOXML namespaces shared by all Office formats
# ---------------------------------------------------------------------------

# Namespaces common to all OOXML formats (not format-specific like w: or p:)
SHARED_NAMESPACES: dict[str, str] = {
    "r":       "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel":     "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct":      "http://schemas.openxmlformats.org/package/2006/content-types",
    "cp":      "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc":      "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "xsi":     "http://www.w3.org/2001/XMLSchema-instance",
    "app":     "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
    "vt":      "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes",
    "a":       "http://schemas.openxmlformats.org/drawingml/2006/main",
    "mc":      "http://schemas.openxmlformats.org/markup-compatibility/2006",
}

# XML namespace (for xml:space etc.)
XML_NS = "http://www.w3.org/XML/1998/namespace"


def register_namespaces(extra: dict[str, str] | None = None) -> None:
    """Register shared + optional extra namespaces with stdlib ElementTree."""
    all_ns = {**SHARED_NAMESPACES, **(extra or {})}
    for prefix, uri in all_ns.items():
        try:
            _StdET.register_namespace(prefix, uri)
        except Exception:
            pass


def clark(ns_map: dict[str, str], prefix: str, local: str) -> str:
    """Return Clark-notation tag: {uri}local."""
    return f"{{{ns_map[prefix]}}}{local}"


# ---------------------------------------------------------------------------
# XML pretty-printing / condensing
# ---------------------------------------------------------------------------

def pretty_print_xml(data: bytes) -> bytes:
    """
    Pretty-print XML bytes with 2-space indent.
    Returns original bytes on any parse error.
    """
    try:
        dom = _minidom.parseString(data)
        pretty = dom.toprettyxml(indent="  ", encoding="UTF-8")
        lines = pretty.decode("utf-8").splitlines()
        cleaned = "\n".join(line for line in lines if line.strip())
        return cleaned.encode("utf-8")
    except Exception:
        return data


def condense_xml(data: bytes) -> bytes:
    """
    Re-serialise XML without pretty-print whitespace between elements.
    Preserves text content (including whitespace inside text nodes).
    Returns original bytes on any parse error.
    """
    try:
        root = _ET.fromstring(data)
        return _StdET.tostring(
            root, encoding="unicode", xml_declaration=False
        ).encode("utf-8")
    except Exception:
        return data


# ---------------------------------------------------------------------------
# xml:space="preserve" auto-repair helper
# ---------------------------------------------------------------------------

def ensure_xml_space_preserve(
    root,
    text_tag: str,
    repairs: list[str],
    filename: str,
) -> None:
    """
    Add xml:space="preserve" to any element with tag `text_tag` that has
    leading or trailing whitespace in its text content.
    Modifies root in-place; appends to repairs list.
    """
    xml_space_attr = f"{{{XML_NS}}}space"
    for el in root.iter(text_tag):
        text = el.text or ""
        if text and (text[0] == " " or text[-1] == " "):
            if el.get(xml_space_attr) != "preserve":
                el.set(xml_space_attr, "preserve")
                repairs.append(
                    f"{filename}: added xml:space='preserve' to "
                    f"<{el.tag.split('}')[-1]}> with leading/trailing space: "
                    f"{text[:40]!r}"
                )
