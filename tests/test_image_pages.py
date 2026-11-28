"""Pages made from images with doc.pages.new_from_image (issue 361)."""

from __future__ import annotations

import io
import random
import struct
import zipfile
import zlib
from typing import TYPE_CHECKING

import pytest
from PIL import Image, ImageOps

import pyropdf

if TYPE_CHECKING:
    from pathlib import Path

    from pyropdf import Pixmap

#: The render tolerance of one 8-bit sample, wider for JPEG's loss.
TOLERANCE = 2
JPEG_TOLERANCE = 12


def _image(mode: str = "RGB", size: tuple[int, int] = (40, 20)) -> Image.Image:
    """Return an image whose left half is red and right half blue, translucent on the right with alpha."""
    width, height = size
    image = Image.new(mode, size)
    left = {"RGB": (255, 0, 0), "RGBA": (255, 0, 0, 255), "L": 60}[mode]
    right = {"RGB": (0, 0, 255), "RGBA": (0, 0, 255, 128), "L": 200}[mode]
    image.paste(left, (0, 0, width // 2, height))
    image.paste(right, (width // 2, 0, width, height))
    return image


def _encoded(image: Image.Image, kind: str, **options: object) -> bytes:
    out = io.BytesIO()
    image.save(out, kind, **options)
    return out.getvalue()


def _close(pixmap: Pixmap, image: Image.Image, tolerance: int) -> bool:
    mode = {(1, False): "L", (1, True): "LA", (3, False): "RGB", (3, True): "RGBA"}[
        (pixmap.channels - pixmap.alpha, pixmap.alpha)
    ]
    rendered = Image.frombytes(mode, (pixmap.width, pixmap.height), bytes(pixmap)).convert(image.mode).tobytes()
    return all(abs(a - b) <= tolerance for a, b in zip(rendered, image.tobytes(), strict=True))


@pytest.mark.spec("dct-filter", "create")
@pytest.mark.spec("images", "create")
def test_a_jpeg_is_embedded_as_it_is() -> None:
    jpeg = _encoded(_image(), "JPEG", quality=95, dpi=(144, 144))
    doc = pyropdf.Document()
    (page,) = doc.pages.new_from_image(jpeg)
    (image,) = page.images()
    stored = image.stored()
    assert stored is not None
    assert stored.filter == "DCTDecode"
    assert stored.data == jpeg
    # 40 by 20 pixels at 144 dpi are 20 by 10 points.
    assert tuple(page.rect) == pytest.approx((0, 0, 20, 10))
    assert _close(page.pixmap(dpi=144), Image.open(io.BytesIO(jpeg)).convert("RGB"), JPEG_TOLERANCE)


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L"])
def test_a_png_renders_like_the_image(mode: str) -> None:
    image = _image(mode)
    doc = pyropdf.Document()
    (page,) = doc.pages.new_from_image(_encoded(image, "PNG", dpi=(72, 72)))
    assert tuple(page.rect) == pytest.approx((0, 0, 40, 20))
    pixmap = page.pixmap(dpi=72, alpha=mode == "RGBA")
    assert _close(pixmap, image, TOLERANCE)


def test_the_page_size_follows_the_resolution() -> None:
    image = _image()
    doc = pyropdf.Document()
    # PNG records pixels per metre; 96 dpi rounds to 3780.
    (png,) = doc.pages.new_from_image(_encoded(image, "PNG", dpi=(96, 96)))
    assert png.rect.width == pytest.approx(40 * 72 / 96)
    (centimetres,) = doc.pages.new_from_image(_encoded(image, "JPEG"))
    assert centimetres.rect.width == pytest.approx(40 * 72 / 96)
    (stretched,) = doc.pages.new_from_image(_encoded(image, "PNG"), dpi=(72, 36))
    assert tuple(stretched.rect) == pytest.approx((0, 0, 40, 40))
    (overridden,) = doc.pages.new_from_image(_encoded(image, "JPEG", dpi=(300, 300)), dpi=144)
    assert tuple(overridden.rect) == pytest.approx((0, 0, 20, 10))
    with pytest.raises(ValueError, match="dpi"):
        doc.pages.new_from_image(_encoded(image, "PNG"), dpi=0)


def test_a_jfif_density_in_centimetres() -> None:
    jpeg = bytearray(_encoded(_image(), "JPEG", dpi=(72, 72)))
    assert jpeg[6:11] == b"JFIF\x00"
    jpeg[13] = 2
    jpeg[14:18] = (100).to_bytes(2) + (100).to_bytes(2)
    (page,) = pyropdf.Document().pages.new_from_image(bytes(jpeg))
    assert page.rect.width == pytest.approx(40 * 72 / 254)


def test_a_jpeg_2000_is_embedded_as_it_is() -> None:
    pytest.importorskip("PIL.Jpeg2KImagePlugin")
    try:
        jpx = _encoded(_image(), "JPEG2000")
    except OSError:
        pytest.skip("Pillow has no OpenJPEG")
    doc = pyropdf.Document()
    (page,) = doc.pages.new_from_image(jpx)
    stored = page.images()[0].stored()
    assert stored is not None
    assert stored.data == jpx
    assert tuple(page.rect) == pytest.approx((0, 0, 30, 15))
    assert _close(page.pixmap(dpi=96), _image(), JPEG_TOLERANCE)


def test_a_multi_page_tiff_adds_one_page_per_frame(tmp_path: Path) -> None:
    first, second = _image(), _image("L", (10, 30))
    path = tmp_path / "pages.tiff"
    first.save(path, save_all=True, append_images=[second], dpi=(72, 72))
    doc = pyropdf.Document()
    doc.pages.new()
    steps = len(doc.history.steps)
    pages = doc.pages.new_from_image(path, index=0)
    assert [tuple(page.rect) for page in pages] == [(0, 0, 40, 20), (0, 0, 10, 30)]
    assert [doc.pages.index(page) for page in pages] == [0, 1]
    assert len(doc.pages) == 3
    assert len(doc.history.steps) == steps + 1
    assert _close(pages[1].pixmap(dpi=72), second, TOLERANCE)


def test_frames_at_a_negative_index_keep_their_order(tmp_path: Path) -> None:
    path = tmp_path / "pages.tiff"
    _image().save(path, save_all=True, append_images=[_image("L", (10, 30))], dpi=(72, 72))
    for index, expected in ((-100, [0, 1]), (-1, [0, 1]), (100, [1, 2])):
        doc = pyropdf.Document()
        doc.pages.new()
        pages = doc.pages.new_from_image(path, index=index)
        assert [tuple(page.rect) for page in pages] == [(0, 0, 40, 20), (0, 0, 10, 30)]
        assert [doc.pages.index(page) for page in pages] == expected


def test_a_jpeg_exif_resolution_is_read() -> None:
    exif = Image.Exif()
    exif[0x011A], exif[0x011B], exif[0x0128] = 300.0, 150.0, 2
    doc = pyropdf.Document()
    (page,) = doc.pages.new_from_image(_encoded(_image(), "JPEG", exif=exif.tobytes()))
    assert tuple(page.rect) == pytest.approx((0, 0, 40 * 72 / 300, 20 * 72 / 150))
    # A JFIF density in inches comes first.
    (page,) = doc.pages.new_from_image(_encoded(_image(), "JPEG", exif=exif.tobytes(), dpi=(72, 72)))
    assert tuple(page.rect) == pytest.approx((0, 0, 40, 20))


@pytest.mark.parametrize("dpi", [0.0254, 0.0508, 0.0762])
def test_a_png_below_a_tenth_of_a_dpi(dpi: float) -> None:
    # One, two, and three pixels per metre.
    (page,) = pyropdf.Document().pages.new_from_image(_encoded(_image(), "PNG", dpi=(dpi, dpi)))
    assert tuple(page.rect) == pytest.approx((0, 0, 40 * 72 / dpi, 20 * 72 / dpi))


def test_a_jpeg_exif_resolution_after_fill_bytes() -> None:
    exif = Image.Exif()
    exif[0x011A], exif[0x011B], exif[0x0128] = 300.0, 150.0, 2
    data = _encoded(_image(), "JPEG", exif=exif.tobytes())
    # Fill bytes may come before any marker; the JFIF segment ends at 20.
    assert data[20:22] == b"\xff\xe1"
    (page,) = pyropdf.Document().pages.new_from_image(data[:20] + b"\xff\xff" + data[20:])
    assert tuple(page.rect) == pytest.approx((0, 0, 40 * 72 / 300, 20 * 72 / 150))


def test_image_data_is_limited_in_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _encoded(_image(), "PNG")
    monkeypatch.setattr("pyropdf._pages._MAX_IMAGE_INPUT", len(data) - 1)
    path = tmp_path / "image.png"
    path.write_bytes(data)
    pages = pyropdf.Document().pages
    padded = data + b"\0" * (-len(data) % 4)
    wide_view = memoryview(padded).cast("I")
    for source in (data, path, wide_view):
        with pytest.raises(pyropdf.LimitError) as raised:
            pages.new_from_image(source)
        assert raised.value.code == "image_input_size"
    assert len(pages) == 0


def test_the_frames_of_one_call_are_limited_in_pixels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyropdf._pages._MAX_FRAME_PIXELS", 1000)
    path = tmp_path / "pages.tiff"
    _image().save(path, save_all=True, append_images=[_image()])
    doc = pyropdf.Document()
    with pytest.raises(pyropdf.LimitError) as raised:
        doc.pages.new_from_image(path)
    assert raised.value.code == "content_element_count"
    assert len(doc.pages) == 0


def test_pixmaps_and_files(tmp_path: Path) -> None:
    doc = pyropdf.Document()
    (source,) = doc.pages.new_from_image(_encoded(_image(), "PNG", dpi=(72, 72)))
    pixmap = source.pixmap(dpi=144)
    (from_pixmap,) = doc.pages.new_from_image(pixmap)
    # The pixmap's 80 by 40 pixels at its 144 dpi.
    assert tuple(from_pixmap.rect) == pytest.approx((0, 0, 40, 20))
    path = tmp_path / "image.jpg"
    path.write_bytes(_encoded(_image(), "JPEG"))
    (from_path,) = doc.pages.new_from_image(path)
    (from_name,) = doc.pages.new_from_image(str(path))
    stored = [page.images()[0].stored() for page in (from_path, from_name)]
    assert stored[0] is not None
    assert stored[1] is not None
    assert stored[0].data == stored[1].data == path.read_bytes()
    reopened = pyropdf.open(doc.to_bytes())
    assert len(reopened.pages) == 4
    assert _close(reopened.pages[0].pixmap(dpi=72), _image(), TOLERANCE)


def test_unreadable_sources_are_refused() -> None:
    pages = pyropdf.Document().pages
    with pytest.raises(ValueError, match="Pillow reads"):
        pages.new_from_image(b"not an image")
    with pytest.raises(TypeError, match="source"):
        pages.new_from_image(42)  # ty: ignore[invalid-argument-type]
    assert len(pages) == 0


def _asymmetric_image(size: tuple[int, int] = (40, 20)) -> Image.Image:
    """Return a four-quadrant image so every Exif orientation (1..8) renders uniquely."""
    w, h = size
    image = Image.new("RGB", size)
    image.paste((255, 0, 0), (0, 0, w // 2, h // 2))
    image.paste((0, 255, 0), (w // 2, 0, w, h // 2))
    image.paste((0, 0, 255), (0, h // 2, w // 2, h))
    image.paste((255, 255, 0), (w // 2, h // 2, w, h))
    return image


def _with_png_exif(png: bytes, exif_tiff: bytes) -> bytes:
    """Insert an ``eXIf`` chunk holding raw TIFF bytes right after the PNG ``IHDR`` chunk."""
    ihdr_len = int.from_bytes(png[8:12])
    split = 8 + 12 + ihdr_len
    chunk = struct.pack(">I", len(exif_tiff)) + b"eXIf" + exif_tiff + struct.pack(">I", zlib.crc32(b"eXIf" + exif_tiff))
    return png[:split] + chunk + png[split:]


def _make_cbz(entries: list[tuple[str, bytes]], *, compress: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compress) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return buf.getvalue()


@pytest.mark.parametrize("orientation", list(range(1, 9)))
def test_exif_orientation_all_eight_for_jpeg_lossless(orientation: int) -> None:
    raw = _asymmetric_image((40, 20))
    exif = Image.Exif()
    exif[0x0112] = orientation
    jpeg = _encoded(raw, "JPEG", quality=95, dpi=(72, 72), exif=exif.tobytes())
    expected = ImageOps.exif_transpose(Image.open(io.BytesIO(jpeg))).convert("RGB")
    doc = pyropdf.Document()
    (page,) = doc.pages.new_from_image(jpeg)
    # JPEG stream must remain byte-for-byte verbatim (no lossy recompression).
    stored = page.images()[0].stored()
    assert stored is not None
    assert stored.filter == "DCTDecode"
    assert stored.data == jpeg
    assert tuple(page.rect) == pytest.approx((0, 0, expected.width, expected.height))
    assert _close(page.pixmap(dpi=72), expected, JPEG_TOLERANCE)


def test_exif_orientation_png_and_tiff() -> None:
    raw = _asymmetric_image((40, 20))
    exif = Image.Exif()
    exif[0x0112] = 6
    exif_bytes = exif.tobytes()
    assert exif_bytes.startswith(b"Exif\x00\x00")
    png = _with_png_exif(_encoded(raw, "PNG", dpi=(72, 72)), exif_bytes[6:])
    expected = raw.transpose(Image.Transpose.ROTATE_270)
    doc = pyropdf.Document()
    (png_page,) = doc.pages.new_from_image(png)
    assert tuple(png_page.rect) == pytest.approx((0, 0, 20, 40))
    assert _close(png_page.pixmap(dpi=72), expected, TOLERANCE)

    tiff = _encoded(raw, "TIFF", dpi=(72, 72), tiffinfo={0x0112: 6})
    (tiff_page,) = doc.pages.new_from_image(tiff)
    assert tuple(tiff_page.rect) == pytest.approx((0, 0, 20, 40))
    assert _close(tiff_page.pixmap(dpi=72), expected, TOLERANCE)

    webp = _encoded(raw, "WEBP", lossless=True, exif=exif_bytes)
    (webp_page,) = doc.pages.new_from_image(webp, dpi=72)
    assert tuple(webp_page.rect) == pytest.approx((0, 0, 20, 40))
    assert _close(webp_page.pixmap(dpi=72), expected, TOLERANCE)


def test_size_fitting_centers_oriented_image_and_refuses_both_dpi_and_size() -> None:
    raw = _asymmetric_image((40, 20))
    exif = Image.Exif()
    exif[0x0112] = 6
    jpeg = _encoded(raw, "JPEG", quality=95, dpi=(72, 72), exif=exif.tobytes())
    doc = pyropdf.Document()
    with pytest.raises(ValueError, match="either dpi or size"):
        doc.pages.new_from_image(jpeg, dpi=72, size=(100, 100))
    # Oriented 20x40 fitted into 100x100 becomes 50x100 centered at x=[25, 75].
    (page,) = doc.pages.new_from_image(jpeg, size=(100, 100))
    assert tuple(page.rect) == pytest.approx((0, 0, 100, 100))
    pix = page.pixmap(dpi=72)
    # Left and right margins are white; interior carries the rotated quadrants.
    assert pix.pixel(5, 50) == (255, 255, 255)
    assert pix.pixel(95, 50) == (255, 255, 255)
    # Orientation 6 puts original bottom-left (blue) at top-left (x~35, y~20)
    # and original top-left (red) at top-right (x~65, y~20).
    assert pix.pixel(35, 20)[2] > 200
    assert pix.pixel(65, 20)[0] > 200


def test_new_from_images_batch_and_undo_atomicity(tmp_path: Path) -> None:
    img1 = _encoded(_image("RGB", (40, 20)), "JPEG", quality=95, dpi=(72, 72))
    img2 = _encoded(_image("RGBA", (30, 30)), "PNG", dpi=(72, 72))
    p1 = tmp_path / "one.jpg"
    p1.write_bytes(img1)
    doc = pyropdf.Document()
    doc.pages.new((100, 100))
    steps = len(doc.history.steps)

    # Empty collection is a no-op without modifying history.
    assert doc.pages.new_from_images([]) == []
    assert len(doc.history.steps) == steps

    pages = doc.pages.new_from_images([p1, img2], index=0)
    assert len(pages) == 2
    assert [doc.pages.index(page) for page in pages] == [0, 1]
    assert len(doc.pages) == 3
    assert len(doc.history.steps) == steps + 1
    stored0 = pages[0].images()[0].stored()
    assert stored0 is not None
    assert stored0.data == img1
    assert _close(pages[1].pixmap(dpi=72, alpha=True), _image("RGBA", (30, 30)), TOLERANCE)

    # Single undo removes the entire batch atomically.
    doc.history.undo()
    assert len(doc.pages) == 1

    # Passing a single string/bytes/Pixmap directly raises TypeError.
    for bad in (str(p1), p1, img1):
        with pytest.raises(TypeError, match="iterable"):
            doc.pages.new_from_images(bad)  # ty: ignore[invalid-argument-type]

    # A failure mid-batch rolls back earlier images in the same call.
    with pytest.raises(ValueError, match="Pillow reads"):
        doc.pages.new_from_images([img1, b"corrupt"])
    assert len(doc.pages) == 1


def test_new_from_cbz_ordering_passthrough_alpha_exif_and_undo(tmp_path: Path) -> None:
    exif6 = Image.Exif()
    exif6[0x0112] = 6
    jpg_plain = _encoded(_image("RGB", (40, 20)), "JPEG", quality=95, dpi=(72, 72))
    png_alpha = _encoded(_image("RGBA", (30, 24)), "PNG", dpi=(72, 72))
    jpg_exif = _encoded(_asymmetric_image((40, 20)), "JPEG", quality=95, dpi=(72, 72), exif=exif6.tobytes())
    png_ch2 = _encoded(_image("L", (22, 18)), "PNG", dpi=(72, 72))
    png_ch10 = _encoded(_image("RGB", (16, 16)), "PNG", dpi=(72, 72))

    # Deliberately insert entries out of order with metadata noise.
    entries: list[tuple[str, bytes]] = [
        ("ComicInfo.xml", b"<ComicInfo/>"),
        ("__MACOSX/ch1/._page1.jpg", b"not a jpeg"),
        ("ch1/.DS_Store", b"finder"),
        ("Thumbs.db", b"windows"),
        ("ch10/page1.png", png_ch10),
        ("ch1/page10.jpg", jpg_exif),
        ("ch2/page1.png", png_ch2),
        ("ch1/page2.png", png_alpha),
        ("ch1/page1.jpg", jpg_plain),
    ]
    cbz_bytes = _make_cbz(entries)
    cbz_path = tmp_path / "issue.cbz"
    cbz_path.write_bytes(cbz_bytes)

    doc = pyropdf.Document()
    steps = len(doc.history.steps)
    pages = doc.pages.new_from_cbz(cbz_path)
    assert len(pages) == 5
    assert len(doc.history.steps) == steps + 1

    # Verify natural order: ch1/page1, ch1/page2, ch1/page10 (rotated 20x40), ch2/page1, ch10/page1.
    assert [tuple(page.rect) for page in pages] == [
        pytest.approx((0, 0, 40, 20)),
        pytest.approx((0, 0, 30, 24)),
        pytest.approx((0, 0, 20, 40)),
        pytest.approx((0, 0, 22, 18)),
        pytest.approx((0, 0, 16, 16)),
    ]
    # JPEGs (plain and EXIF-rotated) pass through without lossy recompression.
    stored_plain = pages[0].images()[0].stored()
    stored_exif = pages[2].images()[0].stored()
    assert stored_plain is not None
    assert stored_plain.filter == "DCTDecode"
    assert stored_plain.data == jpg_plain
    assert stored_exif is not None
    assert stored_exif.filter == "DCTDecode"
    assert stored_exif.data == jpg_exif

    # PNG alpha soft mask and EXIF orientation render accurately.
    assert _close(pages[1].pixmap(dpi=72, alpha=True), _image("RGBA", (30, 24)), TOLERANCE)
    expected_rotated = ImageOps.exif_transpose(Image.open(io.BytesIO(jpg_exif))).convert("RGB")
    assert _close(pages[2].pixmap(dpi=72), expected_rotated, JPEG_TOLERANCE)

    # Save and reopen preserves everything.
    reopened = pyropdf.open(doc.to_bytes())
    assert len(reopened.pages) == 5
    assert _close(reopened.pages[2].pixmap(dpi=72), expected_rotated, JPEG_TOLERANCE)

    # One undo reverts the entire CBZ conversion.
    doc.history.undo()
    assert len(doc.pages) == 0


def test_new_from_cbz_sources_bytes_seekable_and_non_seekable_streams() -> None:
    jpg = _encoded(_image("RGB", (20, 10)), "JPEG", quality=95, dpi=(72, 72))
    cbz = _make_cbz([("01.jpg", jpg)])

    class NonSeekableStream(io.RawIOBase):
        def __init__(self, payload: bytes) -> None:
            self._inner = io.BytesIO(payload)

        def readable(self) -> bool:
            return True

        def readinto(self, buffer: object, /) -> int:
            view = memoryview(buffer).cast("B")  # ty: ignore[invalid-argument-type]
            chunk = self._inner.read(len(view))
            view[: len(chunk)] = chunk
            return len(chunk)

    for source in (cbz, bytearray(cbz), memoryview(cbz), io.BytesIO(cbz), io.BufferedReader(NonSeekableStream(cbz))):
        doc = pyropdf.Document()
        (page,) = doc.pages.new_from_cbz(source)
        assert tuple(page.rect) == pytest.approx((0, 0, 20, 10))
        stored = page.images()[0].stored()
        assert stored is not None
        assert stored.data == jpg

    with pytest.raises(TypeError, match="binary"):
        pyropdf.Document().pages.new_from_cbz(io.StringIO("abc"))  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="source"):
        pyropdf.Document().pages.new_from_cbz(123)  # ty: ignore[invalid-argument-type]


def test_new_from_cbz_jpeg2000_passthrough() -> None:
    pytest.importorskip("PIL.Jpeg2KImagePlugin")
    try:
        jpx = _encoded(_image(), "JPEG2000")
    except OSError:
        pytest.skip("Pillow has no OpenJPEG")
    cbz = _make_cbz([("01.jp2", jpx)])
    doc = pyropdf.Document()
    (page,) = doc.pages.new_from_cbz(cbz)
    stored = page.images()[0].stored()
    assert stored is not None
    assert stored.filter == "JPXDecode"
    assert stored.data == jpx


def test_new_from_cbz_refuses_zip_bombs_atomically() -> None:
    good_jpg = _encoded(_image("RGB", (20, 10)), "JPEG", quality=95, dpi=(72, 72))

    # 1. High-ratio DEFLATE zip bomb (2 MiB of zeros compresses to ~2 KiB -> ~1000x ratio).
    bomb_bytes = _make_cbz([("01.jpg", good_jpg), ("02.jpg", b"\x00" * (2 * 1024 * 1024))])
    doc = pyropdf.Document()
    doc.pages.new((50, 50))
    with pytest.raises(pyropdf.LimitError) as raised:
        doc.pages.new_from_cbz(bomb_bytes)
    assert raised.value.code == "decompressed_size"
    assert len(doc.pages) == 1

    # 2. Lying central-directory header: patching uncompressed file_size down to 100 bytes
    #    refuses atomically during chunked reading without adding earlier pages.
    patched = bytearray(bomb_bytes)
    cd_offset = patched.rfind(b"PK\x01\x02")
    assert cd_offset > 0
    # In ZIP Central Directory header: offset +24 = file_size (4B).
    struct.pack_into("<I", patched, cd_offset + 24, 100)
    with pytest.raises((pyropdf.LimitError, ValueError)):
        doc.pages.new_from_cbz(bytes(patched))
    assert len(doc.pages) == 1

    # 3. Forged compress_size creating overlapped entries refuses with ValueError atomically.
    overlapped = bytearray(bomb_bytes)
    struct.pack_into("<II", overlapped, cd_offset + 20, 0x7FFFFFFF, 100)
    with pytest.raises(ValueError, match="corrupt CBZ entry"):
        doc.pages.new_from_cbz(bytes(overlapped))
    assert len(doc.pages) == 1

    # 4. Live chunked memory_size charging refuses mid-stream when an entry exceeds memory_size.
    noise = random.Random(0).randbytes(256 * 256 * 3)  # noqa: S311 - deterministic incompressible test payload
    big_png = _encoded(Image.frombytes("RGB", (256, 256), noise), "PNG")
    assert len(big_png) > 100_000
    big_cbz = _make_cbz([("01.jpg", good_jpg), ("02.png", big_png)], compress=zipfile.ZIP_STORED)
    # Pass via seekable BytesIO so the archive buffer itself is not charged, only live member chunks.
    with pyropdf.Document(limits=pyropdf.DocumentLimits(max_memory_size=64 * 1024)) as mem_doc:
        mem_doc.pages.new((50, 50))
        with pytest.raises(pyropdf.LimitError) as raised:
            mem_doc.pages.new_from_cbz(io.BytesIO(big_cbz))
        assert raised.value.code == "memory_size"
        assert len(mem_doc.pages) == 1


def test_new_from_cbz_enforces_document_limits_atomically() -> None:
    good_jpg = _encoded(_image("RGB", (20, 10)), "JPEG", quality=95, dpi=(72, 72))
    two_img = _make_cbz([("01.jpg", good_jpg), ("02.jpg", good_jpg)])
    with pyropdf.Document(limits=pyropdf.DocumentLimits(max_total_decompressed_size=len(good_jpg))) as lim_doc:
        with pytest.raises(pyropdf.LimitError) as raised:
            lim_doc.pages.new_from_cbz(two_img)
        assert raised.value.code == "total_decompressed_size"
        assert len(lim_doc.pages) == 0

    with pyropdf.Document(limits=pyropdf.DocumentLimits(max_decompressed_size=len(good_jpg) - 1)) as lim_doc:
        with pytest.raises(pyropdf.LimitError) as raised:
            lim_doc.pages.new_from_cbz(two_img)
        assert raised.value.code == "decompressed_size"
        assert len(lim_doc.pages) == 0

    with pyropdf.Document(limits=pyropdf.DocumentLimits(max_pages=1)) as lim_doc:
        with pytest.raises(pyropdf.LimitError) as raised:
            lim_doc.pages.new_from_cbz(two_img)
        assert raised.value.code == "page_count"
        assert len(lim_doc.pages) == 0

    with pyropdf.Document(limits=pyropdf.DocumentLimits(max_file_size=len(two_img) - 1)) as lim_doc:
        with pytest.raises(pyropdf.LimitError) as raised:
            lim_doc.pages.new_from_cbz(two_img)
        assert raised.value.code == "file_size"
        assert len(lim_doc.pages) == 0


@pytest.mark.parametrize(
    "bad_name",
    [
        "../evil.jpg",
        "dir/../../evil.jpg",
        "/etc/passwd.jpg",
        "\\windows\\evil.jpg",
        "C:\\evil.jpg",
        "safe/..\\evil.jpg",
        "../ignored.txt",
    ],
)
def test_new_from_cbz_refuses_path_traversal_and_symlinks(bad_name: str) -> None:
    good_jpg = _encoded(_image("RGB", (20, 10)), "JPEG", quality=95)
    cbz = _make_cbz([("01.jpg", good_jpg), (bad_name, good_jpg)])
    doc = pyropdf.Document()
    doc.pages.new((50, 50))
    with pytest.raises(ValueError, match="CBZ entry"):
        doc.pages.new_from_cbz(cbz)
    assert len(doc.pages) == 1


def test_new_from_cbz_refuses_symlink_entry() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        info = zipfile.ZipInfo("01.jpg")
        info.external_attr = 0o120777 << 16
        archive.writestr(info, b"/etc/passwd")
    doc = pyropdf.Document()
    with pytest.raises(ValueError, match="symbolic link"):
        doc.pages.new_from_cbz(buf.getvalue())
    assert len(doc.pages) == 0


def test_new_from_cbz_refuses_over_4096_frames_and_pixel_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    tiny = _encoded(_image("L", (4, 4)), "PNG")
    # 4,097 stored image entries exceed the hard 4,096-frame cap before decompression.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as archive:
        for i in range(4097):
            archive.writestr(f"{i:05d}.png", tiny)
    doc = pyropdf.Document()
    with pytest.raises(pyropdf.LimitError) as raised:
        doc.pages.new_from_cbz(buf.getvalue())
    assert raised.value.code == "content_element_count"
    assert len(doc.pages) == 0

    # Pixel cap across multiple PNGs refuses before decoding the offending image.
    monkeypatch.setattr("pyropdf._pages._MAX_FRAME_PIXELS", 1000)
    two_png = _make_cbz(
        [("01.png", _encoded(_image("RGB", (40, 20)), "PNG")), ("02.png", _encoded(_image("RGB", (40, 20)), "PNG"))]
    )
    with pytest.raises(pyropdf.LimitError) as raised:
        doc.pages.new_from_cbz(two_png)
    assert raised.value.code == "content_element_count"
    assert len(doc.pages) == 0


def test_new_from_cbz_refuses_corrupt_empty_encrypted_and_cancelled() -> None:
    good_jpg = _encoded(_image("RGB", (20, 10)), "JPEG", quality=95)
    doc = pyropdf.Document()
    doc.pages.new((50, 50))

    # Not a ZIP at all.
    with pytest.raises(ValueError, match="invalid CBZ"):
        doc.pages.new_from_cbz(b"not a zip file")
    assert len(doc.pages) == 1

    # Valid ZIP with no image entries.
    with pytest.raises(ValueError, match="no image entries"):
        doc.pages.new_from_cbz(_make_cbz([("ComicInfo.xml", b"<xml/>")]))
    assert len(doc.pages) == 1

    # Corrupt second image rolls back the valid first image atomically.
    with pytest.raises(ValueError, match="Pillow reads"):
        doc.pages.new_from_cbz(_make_cbz([("01.jpg", good_jpg), ("02.jpg", b"not an image")]))
    assert len(doc.pages) == 1

    # Corrupt CRC-32 inside ZIP member raises ValueError and leaves document unchanged.
    good_cbz = bytearray(_make_cbz([("01.jpg", good_jpg), ("02.jpg", good_jpg)]))
    # Flip a byte inside the second entry's compressed payload.
    second_local = good_cbz.find(b"PK\x03\x04", 4)
    assert second_local > 0
    good_cbz[second_local + 40] ^= 0xFF
    with pytest.raises(ValueError, match="corrupt CBZ entry"):
        doc.pages.new_from_cbz(bytes(good_cbz))
    assert len(doc.pages) == 1

    # CancellationToken aborts cleanly before or during chunked archive reading.
    token = pyropdf.CancellationToken()
    token.cancel()
    with pyropdf.Document(limits=pyropdf.DocumentLimits(cancellation=token)) as cancelled_doc:
        with pytest.raises(pyropdf.LimitError) as raised:
            cancelled_doc.pages.new_from_cbz(_make_cbz([("01.jpg", good_jpg)]))
        assert raised.value.code == "cancelled"


def test_new_from_cbz_hardening_preflight_ratio_bypasses_nul_and_long_digits(monkeypatch: pytest.MonkeyPatch) -> None:
    good_jpg = _encoded(_image("RGB", (20, 10)), "JPEG", quality=95, dpi=(72, 72))

    # 1. Pre-flight Central Directory entry cap refuses before zipfile instantiates all ZipInfo objects.
    monkeypatch.setattr("pyropdf._images._MAX_ARCHIVE_ENTRIES", 5)
    many_entries = _make_cbz([(f"{i:02d}.jpg", good_jpg) for i in range(6)])
    doc = pyropdf.Document()
    with pytest.raises(pyropdf.LimitError) as raised:
        doc.pages.new_from_cbz(many_entries)
    assert raised.value.code == "content_element_count"
    monkeypatch.undo()

    # 2. Unsupported compression method (BZIP2) is refused with ValueError.
    bzip_buf = io.BytesIO()
    with zipfile.ZipFile(bzip_buf, "w", compression=zipfile.ZIP_BZIP2) as archive:
        archive.writestr("01.jpg", good_jpg)
    with pytest.raises(ValueError, match="unsupported compression method"):
        doc.pages.new_from_cbz(bzip_buf.getvalue())

    # 3. Archive-wide multi-member ratio bomb (each member under 64 KiB floor, archive ratio >> 100x).
    chunk = b"\x00" * 60_000
    multi_bomb = _make_cbz([(f"{i:02d}.png", chunk) for i in range(25)], compress=zipfile.ZIP_DEFLATED)
    with pytest.raises(pyropdf.LimitError) as raised:
        doc.pages.new_from_cbz(multi_bomb)
    assert raised.value.code == "total_decompressed_size"

    # 4. Overlapping entries (two CD records pointing to the same local header offset) are refused.
    two = bytearray(_make_cbz([("01.jpg", good_jpg), ("02.jpg", good_jpg)]))
    second_cd = two.rfind(b"PK\x01\x02")
    assert second_cd > 0
    # Overwrite second entry's relative local header offset (+42) to 0 (matching first entry).
    struct.pack_into("<I", two, second_cd + 42, 0)
    with pytest.raises(ValueError, match="overlapping entries"):
        doc.pages.new_from_cbz(bytes(two))

    # 5. Embedded NUL byte in ZIP filename (hidden by ZipInfo.filename truncation) is caught via orig_filename.
    nul_cbz = bytearray(_make_cbz([("01.jpgX.txt", good_jpg)]))
    nul_cbz = bytearray(bytes(nul_cbz).replace(b"01.jpgX.txt", b"01.jpg\x00.txt"))
    with pytest.raises(ValueError, match="NUL byte"):
        doc.pages.new_from_cbz(bytes(nul_cbz))

    # 6. Filenames with >4,300 consecutive digits sort cleanly without hitting int() digit limits.
    long_a = "1" * 5000 + ".jpg"
    long_b = "2" + "0" * 4999 + ".jpg"
    red_jpg = _encoded(Image.new("RGB", (12, 12), (255, 0, 0)), "JPEG", quality=95)
    blue_jpg = _encoded(Image.new("RGB", (12, 12), (0, 0, 255)), "JPEG", quality=95)
    pages = doc.pages.new_from_cbz(_make_cbz([(long_b, blue_jpg), (long_a, red_jpg)]))
    assert len(pages) == 2
    assert pages[0].pixmap(dpi=72).pixel(6, 6)[0] > 200
    assert pages[1].pixmap(dpi=72).pixel(6, 6)[2] > 200


def _jp2_box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


def _make_exif_orientation(orientation: int, x_dpi: int = 72, y_dpi: int = 72) -> bytes:
    """Build a minimal big-endian Exif TIFF block with XResolution, YResolution, ResolutionUnit, Orientation."""
    # Header (8B) + IFD count (2B) + 4 entries (48B) + next IFD (4B) = 62B; rationals at 62 and 70.
    entries = b"".join(
        [
            struct.pack(">HHII", 0x0112, 3, 1, orientation << 16),
            struct.pack(">HHII", 0x011A, 5, 1, 62),
            struct.pack(">HHII", 0x011B, 5, 1, 70),
            struct.pack(">HHII", 0x0128, 3, 1, 2 << 16),
        ]
    )
    return (
        b"MM\x00*\x00\x00\x00\x08"
        + struct.pack(">H", 4)
        + entries
        + b"\x00\x00\x00\x00"
        + struct.pack(">IIII", x_dpi, 1, y_dpi, 1)
    )


def test_new_from_image_hardening_dpi_exif_jp2_zero_jpeg_pillow_limits_and_locked() -> None:
    # 1. Non-square embedded DPI + Exif orientation 6 matches between JPEG and Pillow TIFF,
    #    and caller dpi=(x, y) override is always in display orientation.
    base = _image("RGB", (40, 20))
    exif_bytes = b"Exif\x00\x00" + _make_exif_orientation(6, x_dpi=200, y_dpi=100)
    jpeg = _encoded(base, "JPEG", quality=95, exif=exif_bytes)
    tiff = _encoded(base, "TIFF", dpi=(200, 100), exif=exif_bytes)
    doc = pyropdf.Document()
    (jpg_page,) = doc.pages.new_from_image(jpeg)
    (tif_page,) = doc.pages.new_from_image(tiff)
    # Transposed 40x20 -> display 20px wide at 100 dpi (14.4 pt), 40px tall at 200 dpi (14.4 pt).
    assert tuple(jpg_page.rect) == pytest.approx((0, 0, 20 * 72 / 100, 40 * 72 / 200))
    assert tuple(tif_page.rect) == pytest.approx(tuple(jpg_page.rect))
    (jpg_override,) = doc.pages.new_from_image(jpeg, dpi=(200, 100))
    (tif_override,) = doc.pages.new_from_image(tiff, dpi=(200, 100))
    assert tuple(jpg_override.rect) == pytest.approx((0, 0, 20 * 72 / 200, 40 * 72 / 100))
    assert tuple(tif_override.rect) == pytest.approx(tuple(jpg_override.rect))

    # 2. Zero-dimension JPEG raises ValueError (not ZeroDivisionError) even when size= is given.
    zero_jpg = b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x00\x00\x00\x03\x01\x11\x00\xff\xd9"
    with pytest.raises(ValueError, match="non-positive"):
        doc.pages.new_from_image(zero_jpg, size=(100, 100))

    # 3. JP2 box parser ignores decoy `ihdr` in earlier uuid box and honors JP2 Exif orientation.
    jp2_raw = _encoded(base, "JPEG2000")
    assert jp2_raw.startswith(b"\x00\x00\x00\x0cjP  \r\n\x87\n")
    decoy_uuid = _jp2_box(b"uuid", b"\x00" * 16 + b"ihdr" + struct.pack(">II", 999, 999))
    exif_uuid = _jp2_box(b"uuid", b"JpgTiffExif->JP2" + _make_exif_orientation(6, x_dpi=72, y_dpi=72))
    # Insert both boxes right after signature (12B) + ftyp box.
    ftyp_len = struct.unpack(">I", jp2_raw[12:16])[0]
    jp2_with_meta = jp2_raw[: 12 + ftyp_len] + decoy_uuid + exif_uuid + jp2_raw[12 + ftyp_len :]
    (jp2_page,) = doc.pages.new_from_image(jp2_with_meta)
    # 40x20 transposed by Exif 6 at 72 dpi -> 20x40 pt.
    assert tuple(jp2_page.rect) == pytest.approx((0, 0, 20, 40))

    # 4. Pillow decode path enforces DocumentLimits before decoding.
    big_tiff = _encoded(Image.new("RGB", (200, 200), (10, 20, 30)), "TIFF", compression="tiff_deflate")
    with pyropdf.Document(limits=pyropdf.DocumentLimits(max_decompressed_size=10_000)) as limited:
        with pytest.raises(pyropdf.LimitError) as raised:
            limited.pages.new_from_image(big_tiff)
        assert raised.value.code == "decompressed_size"
        assert len(limited.pages) == 0

    # 5. Locked encrypted documents fail up front with EncryptedDocumentError.
    enc_src = pyropdf.Document()
    enc_src.pages.new((100, 100))
    encrypted = enc_src.to_bytes(
        encryption=pyropdf.Encryption(user_password="secret", owner_password="owner")  # noqa: S106 - test fixture
    )
    with pyropdf.open(encrypted) as locked:
        with pytest.raises(pyropdf.EncryptedDocumentError):
            locked.pages.new_from_images([])
        with pytest.raises(pyropdf.EncryptedDocumentError):
            locked.pages.new_from_image(jpeg)
        with pytest.raises(pyropdf.EncryptedDocumentError):
            locked.pages.new_from_cbz(_make_cbz([("01.jpg", jpeg)]))
