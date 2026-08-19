#!/usr/bin/env python3
"""Deterministic EPUB to Markdown importer for agent-readable book sources."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import posixpath
import re
import shutil
import sys
import unicodedata
import zipfile
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET


CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
IMPORTER_VERSION = "3"

CYRILLIC = str.maketrans(
    {
        "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E",
        "Ё": "Yo", "Ж": "Zh", "З": "Z", "И": "I", "Й": "Y", "К": "K",
        "Л": "L", "М": "M", "Н": "N", "О": "O", "П": "P", "Р": "R",
        "С": "S", "Т": "T", "У": "U", "Ф": "F", "Х": "Kh", "Ц": "Ts",
        "Ч": "Ch", "Ш": "Sh", "Щ": "Shch", "Ъ": "", "Ы": "Y", "Ь": "",
        "Э": "E", "Ю": "Yu", "Я": "Ya",
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
        "ё": "yo", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
        "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
        "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya",
    }
)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def normalize_href(value: str) -> str:
    path = unquote(value.split("#", 1)[0])
    return posixpath.normpath(path)


def slugify(value: str, fallback: str = "book") -> str:
    transliterated = value.translate(CYRILLIC)
    normalized = unicodedata.normalize("NFKD", transliterated)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return slug or fallback


def yaml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())


def first_metadata(metadata: ET.Element, name: str) -> str:
    for child in metadata:
        if local_name(child.tag) == name:
            value = element_text(child)
            if value:
                return value
    return ""


def all_metadata(metadata: ET.Element, name: str) -> list[str]:
    return [element_text(child) for child in metadata if local_name(child.tag) == name and element_text(child)]


def parse_epub_metadata(archive: zipfile.ZipFile) -> tuple[dict[str, Any], str, dict[str, dict[str, str]], list[str]]:
    container = ET.fromstring(archive.read("META-INF/container.xml"))
    rootfile = container.find(f".//{{{CONTAINER_NS}}}rootfile")
    if rootfile is None:
        raise ValueError("EPUB has no rootfile in META-INF/container.xml")

    opf_path = rootfile.attrib["full-path"]
    opf = ET.fromstring(archive.read(opf_path))
    metadata_element = opf.find(f"{{{OPF_NS}}}metadata")
    if metadata_element is None:
        raise ValueError("EPUB has no OPF metadata")

    identifiers = all_metadata(metadata_element, "identifier")
    isbn = next((value for value in identifiers if "isbn" in value.lower() or re.fullmatch(r"[0-9Xx-]{10,17}", value)), "")
    metadata: dict[str, Any] = {
        "title": first_metadata(metadata_element, "title") or "Untitled book",
        "authors": all_metadata(metadata_element, "creator") or ["Unknown author"],
        "language": first_metadata(metadata_element, "language") or "und",
        "year": first_metadata(metadata_element, "date"),
        "publisher": first_metadata(metadata_element, "publisher"),
        "description": first_metadata(metadata_element, "description"),
        "identifiers": identifiers,
        "isbn": isbn,
    }

    manifest: dict[str, dict[str, str]] = {}
    for item in opf.findall(f"{{{OPF_NS}}}manifest/{{{OPF_NS}}}item"):
        manifest[item.attrib["id"]] = dict(item.attrib)

    spine = [
        ref.attrib["idref"]
        for ref in opf.findall(f"{{{OPF_NS}}}spine/{{{OPF_NS}}}itemref")
        if ref.attrib.get("idref") in manifest
    ]
    return metadata, opf_path, manifest, spine


def parse_ncx_titles(archive: zipfile.ZipFile, opf_path: str, manifest: dict[str, dict[str, str]]) -> dict[str, str]:
    ncx_item = next((item for item in manifest.values() if item.get("media-type") == "application/x-dtbncx+xml"), None)
    if ncx_item is None:
        return {}

    opf_dir = posixpath.dirname(opf_path)
    ncx_path = posixpath.normpath(posixpath.join(opf_dir, ncx_item["href"]))
    try:
        root = ET.fromstring(archive.read(ncx_path))
    except (KeyError, ET.ParseError):
        return {}

    titles: dict[str, str] = {}
    for nav_point in root.findall(f".//{{{NCX_NS}}}navPoint"):
        label = nav_point.find(f"{{{NCX_NS}}}navLabel/{{{NCX_NS}}}text")
        content = nav_point.find(f"{{{NCX_NS}}}content")
        if label is None or content is None or not content.attrib.get("src"):
            continue
        href = normalize_href(posixpath.join(posixpath.dirname(ncx_path), content.attrib["src"]))
        titles.setdefault(href, element_text(label))
    return titles


def css_heading_level(tag: str, classes: set[str]) -> int | None:
    for class_name in classes:
        match = re.fullmatch(r"title([1-6]?)", class_name)
        if match:
            return int(match.group(1) or "1")
    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return int(tag[1])
    return None


class MarkdownParser(HTMLParser):
    """Small XHTML-to-Markdown serializer tuned for common ebook XHTML."""

    BLOCK_TAGS = {"address", "article", "aside", "blockquote", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ol", "p", "pre", "section", "table", "tr", "ul"}
    SKIP_TAGS = {"head", "style", "script", "title", "meta", "link"}

    def __init__(self, source_href: str, media_dir: Path, archive: zipfile.ZipFile, opf_dir: str):
        super().__init__(convert_charrefs=True)
        self.source_href = source_href
        self.media_dir = media_dir
        self.archive = archive
        self.opf_dir = opf_dir
        self.lines: list[str] = []
        self.current: list[str] = []
        self.stack: list[tuple[str, str, bool]] = []
        self.skip_depth = 0
        self.image_files: list[str] = []
        self.image_names: dict[str, str] = {}
        self.link_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs = dict(attrs_list)
        classes = set((attrs.get("class") or "").split())
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            self.stack.append((tag, "", True))
            return
        if self.skip_depth:
            self.stack.append((tag, "", True))
            return

        level = css_heading_level(tag, classes)
        block = tag in self.BLOCK_TAGS or level is not None or tag == "br"
        if block:
            self.flush()

        prefix = ""
        if level is not None:
            prefix = f"{'#' * level} "
        elif tag == "li":
            prefix = "- "
        elif tag == "blockquote" or "cite" in classes:
            prefix = "> "
        elif "text-author" in classes:
            prefix = "> "

        if prefix:
            self.current.append(prefix)
        if tag in {"strong", "b"}:
            self.current.append("**")
        elif tag in {"em", "i"}:
            self.current.append("*")
        elif tag in {"code", "kbd", "samp"}:
            self.current.append("`")
        elif tag == "a":
            self.link_stack.append(attrs.get("href") or "")
            self.current.append("[")
        elif tag == "img":
            self.add_image(attrs.get("src") or "", attrs.get("alt") or "")
        elif tag == "br":
            self.current.append("  ")
            self.flush()

        self.stack.append((tag, prefix, False))

    def handle_startendtag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs_list)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self.stack:
            return
        stored_tag, _, skipped = self.stack.pop()
        if skipped:
            if stored_tag in self.SKIP_TAGS:
                self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag in {"strong", "b"}:
            self.close_inline("**")
        elif tag in {"em", "i"}:
            self.close_inline("*")
        elif tag in {"code", "kbd", "samp"}:
            self.close_inline("`")
        elif tag == "a":
            href = self.link_stack.pop() if self.link_stack else ""
            self.current.append(f"]({href})" if href else "]")
        if tag in self.BLOCK_TAGS or css_heading_level(tag, set()) is not None:
            self.flush()

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if not data:
            return
        normalized = re.sub(r"\s+", " ", html.unescape(data))
        if normalized.strip():
            self.current.append(normalized)

    def add_image(self, src: str, alt: str) -> None:
        if not src:
            return
        source_path = normalize_href(posixpath.join(posixpath.dirname(self.source_href), src))
        if source_path not in self.image_names:
            original_name = Path(source_path).name or "image"
            stem = slugify(Path(original_name).stem, fallback="image")
            suffix = Path(original_name).suffix.lower() or ".bin"
            candidate = f"{stem}{suffix}"
            index = 2
            while candidate in self.image_names.values():
                candidate = f"{stem}-{index}{suffix}"
                index += 1
            self.image_names[source_path] = candidate
            self.image_files.append(source_path)
            target = self.media_dir / candidate
            target.parent.mkdir(parents=True, exist_ok=True)
            archive_path = source_path if source_path.startswith(self.opf_dir + "/") else posixpath.join(self.opf_dir, source_path)
            try:
                target.write_bytes(self.archive.read(archive_path))
            except KeyError:
                self.image_files.pop()
                self.image_names.pop(source_path)
                return
        self.current.append(f"![{alt}](../media/{self.image_names[source_path]})")

    def close_inline(self, marker: str) -> None:
        had_space = False
        if self.current and self.current[-1].endswith(" "):
            had_space = True
            self.current[-1] = self.current[-1].rstrip()
        self.current.append(marker)
        if had_space:
            self.current.append(" ")

    def flush(self) -> None:
        text = "".join(self.current).strip()
        if re.fullmatch(r"(?:#{1,6}|>|-)\s*", text):
            return
        self.current = []
        if not text:
            if self.lines and self.lines[-1] != "":
                self.lines.append("")
            return
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r" +([,.;:!?])", r"\1", text)
        self.lines.append(text)

    def markdown(self) -> str:
        self.flush()
        output: list[str] = []
        previous_blank = False
        for line in self.lines:
            line = line.rstrip()
            if not line:
                if not previous_blank:
                    output.append("")
                previous_blank = True
                continue
            output.append(line)
            previous_blank = False
        return "\n".join(output).strip() + "\n"


def chapter_title(markdown: str, nav_title: str, source_href: str) -> str:
    if nav_title:
        return nav_title
    basename = Path(source_href).name.lower()
    special_titles = {
        "cover.xhtml": "Обложка",
        "ch1.xhtml": "Титульный лист",
        "ch1-1.xhtml": "Выходные данные",
        "ch2.xhtml": "Примечания",
    }
    if basename in special_titles:
        return special_titles[basename]
    for line in markdown.splitlines():
        match = re.match(r"^#{1,6}\s+(.+)$", line.strip())
        if match:
            return match.group(1).strip()
    return Path(source_href).stem.replace("-", " ").title()


def remove_duplicate_leading_heading(markdown: str, title: str) -> str:
    lines = markdown.splitlines()
    if lines and re.sub(r"^#+\s+", "", lines[0]).strip() == title.strip():
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip() + "\n"


def render_frontmatter(values: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in values.items():
        if isinstance(value, list):
            rendered = "[" + ", ".join(yaml_string(item) for item in value) + "]"
        elif value is None or value == "":
            rendered = "null"
        else:
            rendered = yaml_string(value)
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def import_book(source: Path, books_root: Path, dry_run: bool = False) -> dict[str, Any]:
    source_hash = sha256(source)
    with zipfile.ZipFile(source) as archive:
        if archive.testzip() is not None:
            raise ValueError(f"EPUB has corrupt archive members: {source}")
        metadata, opf_path, manifest, spine = parse_epub_metadata(archive)
        opf_dir = posixpath.dirname(opf_path)
        nav_titles = parse_ncx_titles(archive, opf_path, manifest)

        slug = slugify(metadata["title"])
        book_dir = books_root / slug
        existing = read_manifest(book_dir / "manifest.json")
        chapter_dir = book_dir / "chapters"
        media_dir = book_dir / "media"
        original_dir = book_dir / "original"
        source_date = date.fromtimestamp(source.stat().st_mtime).isoformat()

        if (
            not dry_run
            and existing
            and existing.get("importer_version") == IMPORTER_VERSION
            and existing.get("sha256") == source_hash
            and (book_dir / "book.md").is_file()
            and chapter_dir.is_dir()
        ):
            return existing

        if dry_run:
            return {
                "slug": slug,
                "title": metadata["title"],
                "authors": metadata["authors"],
                "sha256": source_hash,
                "chapters": len(spine),
            }

        book_dir.mkdir(parents=True, exist_ok=True)
        if chapter_dir.exists():
            shutil.rmtree(chapter_dir)
        if media_dir.exists():
            shutil.rmtree(media_dir)
        chapter_dir.mkdir(parents=True)
        media_dir.mkdir(parents=True)
        original_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, original_dir / "book.epub")

        chapters: list[dict[str, Any]] = []
        used_names: set[str] = set()
        for number, item_id in enumerate(spine, start=1):
            item = manifest[item_id]
            href = normalize_href(posixpath.join(opf_dir, item["href"]))
            archive_path = href if href.startswith(opf_dir + "/") else posixpath.join(opf_dir, href)
            try:
                xhtml = archive.read(archive_path)
            except KeyError as error:
                raise ValueError(f"Spine item is missing from archive: {href}") from error
            parser = MarkdownParser(href, media_dir, archive, opf_dir)
            parser.feed(xhtml.decode("utf-8", "replace"))
            content = parser.markdown()
            title = chapter_title(content, nav_titles.get(href, ""), href)
            content = remove_duplicate_leading_heading(content, title)
            base_name = slugify(title, fallback=f"section-{number:03d}")
            file_name = f"{number:03d}-{base_name}.md"
            while file_name in used_names:
                file_name = f"{number:03d}-{base_name}-{len(used_names) + 1}.md"
            used_names.add(file_name)
            chapter_path = chapter_dir / file_name
            chapter_frontmatter = render_frontmatter(
                {
                    "type": "source",
                    "source": "ebook",
                    "format": "epub",
                    "book": metadata["title"],
                    "author": metadata["authors"],
                    "chapter": title,
                    "chapter_number": number,
                    "source_href": href,
                    "language": metadata["language"],
                }
            )
            chapter_body = f"# {title}\n"
            if content.strip():
                chapter_body += f"\n{content}"
            chapter_path.write_text(chapter_frontmatter + chapter_body, encoding="utf-8")
            chapters.append(
                {
                    "number": number,
                    "title": title,
                    "file": f"chapters/{file_name}",
                    "source_href": href,
                    "images": parser.image_files,
                }
            )

    manifest_data: dict[str, Any] = {
        "importer_version": IMPORTER_VERSION,
        "slug": slug,
        "title": metadata["title"],
        "authors": metadata["authors"],
        "language": metadata["language"],
        "year": metadata["year"],
        "publisher": metadata["publisher"],
        "description": metadata["description"],
        "isbn": metadata["isbn"],
        "identifiers": metadata["identifiers"],
        "source_filename": source.name,
        "sha256": source_hash,
        "imported": source_date,
        "chapters": chapters,
        "media": sorted({image for chapter in chapters for image in chapter["images"]}),
    }
    book_values = {
        "type": "book",
        "source": "ebook",
        "format": "epub",
        "title": metadata["title"],
        "author": metadata["authors"],
        "language": metadata["language"],
        "year": metadata["year"],
        "isbn": metadata["isbn"],
        "sha256": source_hash,
        "imported": source_date,
    }
    book_lines = [render_frontmatter(book_values), f"# {metadata['title']}", "", f"**Author:** {', '.join(metadata['authors'])}"]
    if metadata["publisher"]:
        book_lines.append(f"**Publisher:** {metadata['publisher']}")
    if metadata["description"]:
        book_lines.extend(["", metadata["description"]])
    book_lines.extend(["", "## Chapters", ""])
    book_lines.extend(f"{chapter['number']}. [{chapter['title']}]({chapter['file']})" for chapter in chapters)
    book_lines.append("")
    (book_dir / "book.md").write_text("\n".join(book_lines), encoding="utf-8")
    (book_dir / "manifest.json").write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_data


def build_index(books_root: Path) -> None:
    entries: list[dict[str, Any]] = []
    for manifest_path in sorted(books_root.glob("*/manifest.json")):
        manifest = read_manifest(manifest_path)
        if manifest:
            entries.append(manifest)
    lines = ["---", 'type: "index"', 'source: "ebook-import"', "---", "", "# Books", "", "| Title | Author | Language | Chapters | Source |", "| --- | --- | --- | ---: | --- |"]
    for entry in entries:
        author = ", ".join(entry.get("authors", []))
        title = entry.get("title", entry.get("slug", "book"))
        slug = entry.get("slug", slugify(title))
        lines.append(f"| [{title}]({slug}/book.md) | {author} | {entry.get('language', '')} | {len(entry.get('chapters', []))} | `{entry.get('source_filename', '')}` |")
    lines.extend(["", "## Reading notes", "", "The imported Markdown preserves the source text. Use the chapter files for focused retrieval; the original EPUB is kept under each book's `original/` directory.", ""])
    books_root.mkdir(parents=True, exist_ok=True)
    (books_root / "index.md").write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    import_parser = subparsers.add_parser("import", help="Import one or more EPUB files")
    import_parser.add_argument("--vault-root", type=Path, required=True)
    import_parser.add_argument("--input", type=Path, nargs="+", required=True)
    import_parser.add_argument("--output-dir", default="05-sources/books")
    import_parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.command != "import":
        return 2
    books_root = (args.vault_root / args.output_dir).resolve()
    results: list[dict[str, Any]] = []
    for source in args.input:
        source = source.expanduser().resolve()
        if not source.is_file():
            raise SystemExit(f"Input file does not exist: {source}")
        if source.suffix.lower() != ".epub":
            raise SystemExit(f"Input is not an EPUB: {source}")
        results.append(import_book(source, books_root, dry_run=args.dry_run))
    if not args.dry_run:
        build_index(books_root)
    print(json.dumps({"books_root": str(books_root), "books": results, "dry_run": args.dry_run}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
