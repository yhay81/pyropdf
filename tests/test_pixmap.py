"""page.pixmap and Pixmap: rendering the current version through the hayro bridge.

That every corpus page renders at 1x as pylopdf renders it from the original
bytes is crates/pyropdf-engine/src/render/tests.rs; these tests check the
Python surface, edits, and output.
"""

from __future__ import annotations

import stat
import struct
import sys
import zlib
from pathlib import Path

import pytest
from test_pages import build_raw_pdf

import pyropdf
from pyropdf import Pixmap

ASSETS = Path(__file__).parent / "assets" / "real_world"

#: A 200 by 100 point page: a black 40 by 20 rectangle 10 points from the
#: left and 30 from the bottom.
CONTENT = "0 g 10 30 40 20 re f"


def boxed() -> pyropdf.Document:
    return pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Contents 4 0 R >>",
                4: f"<< /Length {len(CONTENT)} >>\nstream\n{CONTENT}\nendstream",
            }
        )
    )


def rows(pixmap: Pixmap) -> list[bytes]:
    data = bytes(memoryview(pixmap))
    return [data[y * pixmap.stride : (y + 1) * pixmap.stride] for y in range(pixmap.height)]


def black(pixmap: Pixmap, x: int, y: int) -> bool:
    at = y * pixmap.stride + x * pixmap.channels
    return bytes(memoryview(pixmap))[at : at + 3] == b"\0\0\0"


def png_header(data: bytes) -> tuple[int, int, int]:
    """Return the width, height, and color type of a PNG."""
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height, _, color = struct.unpack(">IIBB", data[16:26])
    return width, height, color


def test_a_page_renders_on_white_at_72_dpi() -> None:
    pixmap = boxed().pages[0].pixmap()
    assert (pixmap.width, pixmap.height, pixmap.channels, pixmap.alpha) == (200, 100, 3, False)
    assert pixmap.stride == 600
    assert pixmap.resolution == (72, 72)
    assert repr(pixmap) == "<Pixmap 200x100 rgb>"
    # The rectangle covers x 10 to 50 and, from the top, y 50 to 70.
    assert black(pixmap, 10, 50)
    assert black(pixmap, 49, 69)
    assert not black(pixmap, 9, 50)
    assert not black(pixmap, 50, 69)
    assert rows(pixmap)[0] == b"\xff" * 600


def test_alpha_renders_on_transparency() -> None:
    pixmap = boxed().pages[0].pixmap(alpha=True)
    assert (pixmap.channels, pixmap.alpha) == (4, True)
    data = bytes(memoryview(pixmap))
    assert data[:4] == b"\0\0\0\0"
    at = 50 * pixmap.stride + 20 * 4
    assert data[at : at + 4] == b"\0\0\0\xff"


def test_dpi_and_matrix_scale_the_raster() -> None:
    page = boxed().pages[0]
    assert (page.pixmap(dpi=144).width, page.pixmap(dpi=144).height) == (400, 200)
    scaled = page.pixmap(matrix=(0.5, 0, 0, 2, 100, 100))
    assert (scaled.width, scaled.height, scaled.resolution) == (100, 200, (36, 144))
    turned = page.pixmap(matrix=(0, 1, -1, 0))
    assert (turned.width, turned.height) == (100, 200)
    for bad in ({"dpi": 0}, {"dpi": float("inf")}, {"matrix": (1, 0, 0, 0)}, {"matrix": (1, 2, 3)}):
        with pytest.raises(ValueError, match=r"dpi|matrix"):
            page.pixmap(**bad)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="not both"):
        page.pixmap(dpi=72, matrix=(1, 0, 0, 1))
    with pytest.raises(TypeError, match="alpha"):
        page.pixmap(alpha=1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="too small"):
        page.pixmap(dpi=0.01)


def test_the_next_pixmap_after_a_rotation_shows_it() -> None:
    doc = boxed()
    page = doc.pages[0]
    upright = page.pixmap()
    page.rotation = 90
    turned = page.pixmap()
    assert (turned.width, turned.height) == (100, 200)
    # A quarter turn clockwise: upright pixel (x, y) lands at (H - 1 - y, x).
    for x, y in [(10, 50), (49, 69), (9, 50), (50, 69), (0, 0)]:
        assert black(turned, upright.height - 1 - y, x) == black(upright, x, y)
    page.rotation = 0
    assert bytes(memoryview(page.pixmap())) == bytes(memoryview(upright))


def test_an_edited_page_renders_its_new_content() -> None:
    doc = boxed()
    before = doc.pages[0].pixmap()
    doc.pages.new((50, 50), index=0)
    after = doc.pages[1].pixmap()
    assert bytes(memoryview(after)) == bytes(memoryview(before))
    blank = doc.pages[0].pixmap()
    assert bytes(memoryview(blank)) == b"\xff" * (50 * 50 * 3)


def test_corpus_pages_render_with_their_display_size() -> None:
    for path in sorted(ASSETS.glob("*.pdf")):
        doc = pyropdf.open(path)
        page = doc.pages[0]
        pixmap = page.pixmap()
        _, _, width, height = page.rect
        assert abs(pixmap.width - width) <= 1, path.name
        assert abs(pixmap.height - height) <= 1, path.name


def test_the_buffer_is_read_only_and_shaped() -> None:
    pixmap = boxed().pages[0].pixmap(alpha=True)
    view = memoryview(pixmap)
    assert view.readonly
    assert view.shape == (100, 200, 4)
    assert view.strides == (800, 4, 1)
    assert view.format == "B"
    with pytest.raises(TypeError):
        view[0, 0, 0] = 1


def test_to_bytes_writes_png_within_its_bound() -> None:
    pixmap = boxed().pages[0].pixmap()
    data = pixmap.to_bytes()
    assert png_header(data) == (200, 100, 2)
    assert png_header(boxed().pages[0].pixmap(alpha=True).to_bytes("png"))[2] == 6
    # The pHYs chunk records 72 dpi as 2835 pixels per metre.
    at = data.index(b"pHYs")
    assert struct.unpack(">IIB", data[at + 4 : at + 13]) == (2835, 2835, 1)
    assert zlib.crc32(data[at : at + 13]) == struct.unpack(">I", data[at + 13 : at + 17])[0]
    with pytest.raises(pyropdf.LimitError) as caught:
        pixmap.to_bytes(max_size=100)
    assert caught.value.code == "pixmap_output_size"
    assert pixmap.to_bytes(max_size=None) == data
    with pytest.raises(ValueError, match="format"):
        pixmap.to_bytes("jpeg")
    with pytest.raises(ValueError, match="max_size"):
        pixmap.to_bytes(max_size=0)


def test_save_replaces_its_target_atomically(tmp_path: Path) -> None:
    pixmap = boxed().pages[0].pixmap()
    path = tmp_path / "page.png"
    pixmap.save(path)
    assert path.read_bytes() == pixmap.to_bytes()
    other = tmp_path / "page.out"
    pixmap.save(other, format="png")
    assert png_header(other.read_bytes())[:2] == (200, 100)
    with pytest.raises(ValueError, match="image format"):
        pixmap.save(tmp_path / "page.bmp")
    with pytest.raises(pyropdf.PdfError, match="failed to save"):
        pixmap.save(tmp_path / "missing" / "page.png")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["page.out", "page.png"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions and symlinks")
def test_save_keeps_permissions_and_a_final_symlink(tmp_path: Path) -> None:
    pixmap = boxed().pages[0].pixmap()
    real = tmp_path / "real.png"
    real.write_bytes(b"old")
    real.chmod(0o640)
    link = tmp_path / "link.png"
    link.symlink_to(real)
    pixmap.save(link)
    assert link.is_symlink()
    assert stat.S_IMODE(real.stat().st_mode) == 0o640
    assert real.read_bytes() == pixmap.to_bytes()


def test_a_deleted_page_or_a_closed_document_refuses_to_render() -> None:
    doc = boxed()
    doc.pages.new()
    page = doc.pages[0]
    del doc.pages[0]
    with pytest.raises(pyropdf.StalePageError):
        page.pixmap()
    page = doc.pages[0]
    doc.close()
    with pytest.raises(pyropdf.DocumentClosedError):
        page.pixmap()
