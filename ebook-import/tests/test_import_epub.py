from __future__ import annotations

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from import_epub import main  # noqa: E402


def make_epub(path: Path, title: str = "Тестовая книга") -> None:
    container = """<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>"""
    opf = f"""<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title><dc:creator>Автор</dc:creator><dc:language>ru</dc:language><dc:date>2024</dc:date><dc:identifier>isbn:9780000000000</dc:identifier></metadata><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/><item id="image" href="images/picture.png" media-type="image/png"/><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest><spine toc="ncx"><itemref idref="chapter"/></spine></package>"""
    ncx = """<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap><navPoint id="n1"><navLabel><text>Глава 1</text></navLabel><content src="chapter.xhtml#start"/></navPoint></navMap></ncx>"""
    xhtml = """<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>ignored</title></head><body><div class="title2">Глава 1</div><p class="p1">Привет, <strong>мир</strong>.</p><div class="cite">Цитата.</div><p><img src="images/picture.png" alt="Иллюстрация"/></p></body></html>"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OPS/content.opf", opf)
        archive.writestr("OPS/toc.ncx", ncx)
        archive.writestr("OPS/chapter.xhtml", xhtml)
        archive.writestr("OPS/images/picture.png", b"png-fixture")


def test_import_generates_agent_corpus(tmp_path: Path) -> None:
    source = tmp_path / "author_title.fb2.epub"
    vault = tmp_path / "vault"
    make_epub(source)

    assert main(["import", "--vault-root", str(vault), "--input", str(source)]) == 0
    book_dir = vault / "05-sources/books/testovaya-kniga"
    book = (book_dir / "book.md").read_text(encoding="utf-8")
    chapter = next((book_dir / "chapters").glob("*.md")).read_text(encoding="utf-8")

    assert "Тестовая книга" in book
    assert "Глава 1" in chapter
    assert "Привет, **мир**." in chapter
    assert "![Иллюстрация](../media/picture.png)" in chapter
    assert (book_dir / "original/book.epub").is_file()
    assert (book_dir / "media/picture.png").read_bytes() == b"png-fixture"
    assert "testovaya-kniga/book.md" in (vault / "05-sources/books/index.md").read_text(encoding="utf-8")


def test_same_hash_is_a_noop(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    vault = tmp_path / "vault"
    make_epub(source)
    assert main(["import", "--vault-root", str(vault), "--input", str(source)]) == 0
    book_dir = vault / "05-sources/books/testovaya-kniga"
    chapter = next((book_dir / "chapters").glob("*.md"))
    chapter.write_text(chapter.read_text(encoding="utf-8") + "\nUser note\n", encoding="utf-8")
    assert main(["import", "--vault-root", str(vault), "--input", str(source)]) == 0
    assert "User note" in chapter.read_text(encoding="utf-8")
