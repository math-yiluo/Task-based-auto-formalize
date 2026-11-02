"""PDF pages as grayscale PNG images, for workers to look at.

Text extraction finds nothing in a scanned page and garbles formulas, so a worker can ask for
the page as an image, which it reads with its own image viewer. In the Linux pilot reviewers
otherwise installed PyMuPDF, pypdfium2 and OCR models into their workspaces, a few hundred MB
each. PDFium renders here; it is native code reading documents from anywhere and not
thread-safe, so it runs in a process of its own, with a time and a memory limit, and a crash or
a hang ends only that process.

The process reads the PDF from an inherited file descriptor and writes `<key>-p<page>.png` into
a directory; it prints a JSON list of {page, name, width, height}:
python -m formal_geyser.coordination.runtime.pdf_render FD DIRECTORY KEY PAGE...
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

DPI = 150
MAX_PIXELS = 2400  # On the longer side.
TIMEOUT_SECONDS = 60.0
MEMORY_BYTES = 2 * 2**30


def png(width: int, height: int, stride: int, buffer: Any) -> bytes:
    """A PNG of an 8-bit grayscale bitmap whose rows are `stride` bytes apart."""
    view = memoryview(buffer).cast("B")
    rows = b"".join(b"\x00" + view[row * stride:row * stride + width].tobytes() for row in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def render_pages(descriptor: int, directory: Path, key: str, pages: list[int]) -> list[dict[str, Any]]:
    """Render `pages` of the PDF open at `descriptor` (run in the separate process)."""
    import pypdfium2 as pdfium

    with os.fdopen(descriptor, "rb", closefd=False) as stream:
        stream.seek(0)
        document = pdfium.PdfDocument(stream)
        try:
            images = []
            for number in pages:
                page = document[number - 1]
                try:
                    longer = max(page.get_size()) or 1.0
                    bitmap = page.render(scale=min(DPI / 72, MAX_PIXELS / longer), grayscale=True)
                    try:
                        if bitmap.n_channels != 1:
                            raise ValueError("PDFium did not render the page in grayscale")
                        body = png(bitmap.width, bitmap.height, bitmap.stride, bitmap.buffer)
                        size = {"width": bitmap.width, "height": bitmap.height}
                    finally:
                        bitmap.close()
                finally:
                    page.close()
                name = f"{key}-p{number}.png"
                (directory / name).write_bytes(body)
                images.append({"page": number, "name": name, **size})
            return images
        finally:
            document.close()


def render(stream, directory: Path, key: str, pages: list[int]) -> list[dict[str, Any]]:
    """Render `pages` of the PDF open as `stream` into `directory`, in a separate process."""
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = stream.fileno()
    try:
        done = subprocess.run(
            [sys.executable, "-m", __name__, str(descriptor), str(directory), key, *map(str, pages)],
            pass_fds=(descriptor,), capture_output=True, timeout=TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"Rendering the PDF pages took longer than {TIMEOUT_SECONDS:g} seconds") from error
    if done.returncode != 0:
        reason = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [f"exit {done.returncode}"]
        raise ValueError(f"The PDF pages could not be rendered: {reason[0][:300]}")
    return json.loads(done.stdout)


def main(argv: list[str]) -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    descriptor, directory, key, *pages = argv
    print(json.dumps(render_pages(int(descriptor), Path(directory), key, [int(page) for page in pages])))


if __name__ == "__main__":
    main(sys.argv[1:])
