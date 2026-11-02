"""PDF pages rendered as images for workers, in a process of their own."""
from __future__ import annotations

import struct
import zlib

import pytest

from formal_geyser.coordination.runtime.files import ScopedFiles
from formal_geyser.coordination.runtime.pdf_render import render

pypdf = pytest.importorskip("pypdf")
pytest.importorskip("pypdfium2")


def blank_pdf(path, pages=2):
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=144)
    with path.open("wb") as stream:
        writer.write(stream)


def decoded(body: bytes) -> tuple[int, int, bytes]:
    """Width, height and pixels of a grayscale PNG this renderer writes."""
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, depth, color = struct.unpack(">IIBB", body[16:26])
    assert (depth, color) == (8, 0)
    start = body.index(b"IDAT") + 4
    length = struct.unpack(">I", body[start - 8:start - 4])[0]
    rows = zlib.decompress(body[start:start + length])
    assert len(rows) == height * (width + 1) and set(rows[::width + 1]) == {0}  # One filter byte per row.
    return width, height, b"".join(rows[row * (width + 1) + 1:(row + 1) * (width + 1)] for row in range(height))


def test_rendered_pages_are_grayscale_images_beside_their_text(tmp_path):
    blank_pdf(tmp_path / "scan.pdf")
    original = (tmp_path / "scan.pdf").read_bytes()
    out = tmp_path / "work" / ".private" / "tmp" / "pdf-pages"
    result = ScopedFiles(tmp_path).pdf("scan.pdf", page=1, count=3, render=out)
    assert [page["page"] for page in result["pages"]] == [1, 2]
    assert [(image["page"], image["width"], image["height"]) for image in result["images"]] == [
        (1, 150, 300), (2, 150, 300)]  # 72 by 144 points at 150 dpi.
    width, height, pixels = decoded((out / result["images"][0]["path"].rsplit("/", 1)[1]).read_bytes())
    assert (width, height) == (150, 300) and set(pixels) == {255}  # A blank page is white.
    assert (tmp_path / "scan.pdf").read_bytes() == original
    assert "images" not in ScopedFiles(tmp_path).pdf("scan.pdf")  # Only on request.


def test_a_document_pdfium_cannot_read_fails_in_its_own_process(tmp_path):
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 not really a document")
    with (tmp_path / "broken.pdf").open("rb") as stream, pytest.raises(ValueError, match="could not be rendered"):
        render(stream, tmp_path / "out", "key", [1])
