#!/usr/bin/env python3
"""
ocr-alttext.py — Extract images from a .docx, OCR them, and write the
recognized text back as alt-text on the drawing elements.

Workflow:
  1. Unpack the .docx
  2. OCR each image in word/media/ using tesseract
  3. Find all <w:drawing> elements and set <wp:docPrdescr> alt-text
  4. Repack the .docx

Exit codes:
  0  success
  1  extraction/OCR error
  2  usage error
  3  missing dependency
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# Remove skill-tree path entries that would shadow 'docx' (python-docx package).
import os as _os
_scripts_dir = str(Path(__file__).parent.resolve())
_skill_dir = str(Path(__file__).parent.parent.resolve())
sys.path = [p for p in sys.path if _os.path.realpath(p or ".") not in (_scripts_dir, _skill_dir)]

# Load _common by absolute path
import importlib.util as _ilu
_common_path = Path(__file__).parent / '_common.py'
_spec = _ilu.spec_from_file_location('_common', _common_path)
_common_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_common_mod)
detect_format = _common_mod.detect_format
fail = _common_mod.fail

try:
    import defusedxml.ElementTree as ET
except ImportError:
    import xml.etree.ElementTree as ET  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Namespace constants
# ---------------------------------------------------------------------------

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

def _tag(ns: str, local: str) -> str:
    return f"{{{ns}}}{local}"


# ---------------------------------------------------------------------------
# Tesseract OCR
# ---------------------------------------------------------------------------

def ocr_image(image_path: Path) -> str:
    """Run tesseract on an image file and return recognized text."""
    try:
        result = subprocess.run(
            ["tesseract", str(image_path), "stdout", "--psm", "6"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            print(f"warning: tesseract failed on {image_path.name}: {result.stderr.strip()}", file=sys.stderr)
            return ""
        return result.stdout.strip()
    except FileNotFoundError:
        fail(3, "tesseract not found; install with: brew install tesseract")
    except subprocess.TimeoutExpired:
        print(f"warning: tesseract timed out on {image_path.name}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------------------
# Find images and their relationship IDs
# ---------------------------------------------------------------------------

def _read_rels(zf: zipfile.ZipFile, rels_path: str) -> dict[str, str]:
    """Read relationships and return {Id: Target} for image relationships."""
    try:
        data = zf.read(rels_path)
    except KeyError:
        return {}
    root = ET.fromstring(data)
    rels = {}
    for el in root:
        if el.get("Type", "").endswith("/image"):
            rels[el.get("Id", "")] = el.get("Target", "")
    return rels


def _find_image_rels(zf: zipfile.ZipFile) -> dict[str, str]:
    """Find all image relationships from word/_rels/document.xml.rels."""
    return _read_rels(zf, "word/_rels/document.xml.rels")


# ---------------------------------------------------------------------------
# Update alt-text in document.xml
# ---------------------------------------------------------------------------

def _update_alt_text(document_xml: bytes, image_map: dict[str, str]) -> bytes:
    """
    Find all <w:drawing> elements and set alt-text from image_map.
    
    image_map: {relationship_id: ocr_text}
    """
    root = ET.fromstring(document_xml)
    
    # Find all drawing elements
    for drawing in root.iter(_tag(W_NS, "drawing")):
        # Find the inline or anchor element
        for container in drawing:
            tag = container.tag.split("}")[-1] if "}" in container.tag else container.tag
            if tag in ("inline", "anchor"):
                # Find docPr element
                for docPr in container.iter(_tag(WP_NS, "docPr")):
                    # Find the blip element to get the relationship ID
                    for blip in container.iter(_tag(A_NS, "blip")):
                        embed = blip.get(_tag(R_NS, "embed"), "")
                        if embed and embed in image_map:
                            ocr_text = image_map[embed]
                            if ocr_text:
                                # Set the descr attribute (alt-text)
                                docPr.set("descr", ocr_text)
                                print(f"  alt-text for {embed}: {ocr_text[:80]}...")
    
    return ET.tostring(root, encoding="unicode").encode("utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def ocr_docx(docx_path: Path, output_path: Path | None = None, keep_temp: bool = False) -> None:
    """Process a .docx file: extract images, OCR, write alt-text."""
    
    if output_path is None:
        output_path = docx_path.parent / f"{docx_path.stem}_ocr-alttext{docx_path.suffix}"
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # 1. Unpack
        print(f"Unpacking {docx_path.name}...")
        with zipfile.ZipFile(docx_path, "r") as zf:
            zf.extractall(tmpdir)
        
        # 2. Find image relationships
        with zipfile.ZipFile(docx_path, "r") as zf:
            image_rels = _find_image_rels(zf)
        
        if not image_rels:
            print("No images found in document.", file=sys.stderr)
            # Just copy the original
            shutil.copy2(docx_path, output_path)
            return
        
        print(f"Found {len(image_rels)} image(s) to OCR.")
        
        # 3. OCR each image
        media_dir = tmpdir / "word" / "media"
        image_map = {}  # {rel_id: ocr_text}
        
        for rel_id, target in image_rels.items():
            # target is relative to word/ directory
            image_path = tmpdir / "word" / target
            if image_path.exists():
                print(f"  OCR: {target}...")
                ocr_text = ocr_image(image_path)
                image_map[rel_id] = ocr_text
            else:
                print(f"  warning: image not found: {image_path}", file=sys.stderr)
        
        # 4. Update document.xml
        doc_xml_path = tmpdir / "word" / "document.xml"
        doc_xml = doc_xml_path.read_bytes()
        updated_xml = _update_alt_text(doc_xml, image_map)
        doc_xml_path.write_bytes(updated_xml)
        
        # 5. Repack
        print(f"Repacking to {output_path.name}...")
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for root_dir, dirs, files in os.walk(tmpdir):
                for file in files:
                    file_path = Path(root_dir) / file
                    arcname = file_path.relative_to(tmpdir)
                    zout.write(file_path, arcname)
        
        print(f"Done! Output: {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ocr-alttext.py",
        description="OCR embedded images in a .docx and write text as alt-text.",
    )
    parser.add_argument("file", help="Path to .docx file")
    parser.add_argument(
        "-o", "--output", metavar="OUTPUT",
        help="Output file path (default: <input>_ocr-alttext.docx)",
    )
    parser.add_argument(
        "--keep-temp", action="store_true",
        help="Keep temporary unpacked directory",
    )
    args = parser.parse_args()
    
    path = Path(args.file)
    if not path.exists():
        fail(2, f"file not found: {path}")
    
    fmt = detect_format(path)
    if fmt not in ("docx", "docm"):
        fail(3, f"unsupported format: {fmt}")
    
    output = Path(args.output) if args.output else None
    ocr_docx(path, output, keep_temp=args.keep_temp)


if __name__ == "__main__":
    main()
