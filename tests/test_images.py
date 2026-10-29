"""Images: their pixels and stored data, and replacing and removing image objects."""

from __future__ import annotations

import zlib
from pathlib import Path

import pytest

import pyropdf
from pyropdf import Name, StoredImage

CORPUS = sorted((Path(__file__).resolve().parent / "assets" / "real_world").glob("*.pdf"))
WIDTH, HEIGHT = 8, 6
#: Each pixel's red rises to the right and green downward.
SAMPLES = bytes(value for y in range(HEIGHT) for x in range(WIDTH) for value in (x * 32, y * 40, 128))
#: The render tolerance of one 8-bit sample.
TOLERANCE = 2


def image_document(
    samples: bytes = SAMPLES, *, pages: int = 2, extra: dict[str, object] | None = None
) -> pyropdf.Document:
    """Return ``pages`` pages of WIDTH by HEIGHT points, each painting one shared RGB image over the whole page."""
    doc = pyropdf.Document()
    image = doc.objects.add_stream(
        samples,
        {
            "Type": Name("XObject"),
            "Subtype": Name("Image"),
            "Width": WIDTH,
            "Height": HEIGHT,
            "BitsPerComponent": 8,
            "ColorSpace": Name("DeviceRGB"),
            "Interpolate": False,
            **(extra or {}),
        },
    )
    content = doc.objects.add_stream(f"q {WIDTH} 0 0 {HEIGHT} 0 0 cm /Im0 Do Q".encode())
    for _ in range(pages):
        doc.pages.new((WIDTH, HEIGHT))
        page = doc.pages[-1].obj
        page["Resources"] = {"XObject": {"Im0": doc.objects[image]}}
        page["Contents"] = doc.objects[content]
    return doc


def _close(left: bytes, right: bytes) -> bool:
    return len(left) == len(right) and all(abs(a - b) <= TOLERANCE for a, b in zip(left, right, strict=True))


def _jpeg(samples: bytes = SAMPLES) -> bytes:
    """Return a JPEG of ``samples``, rendered from a page that paints them."""
    return image_document(samples, pages=1).pages[0].pixmap(dpi=72).to_bytes("jpeg")


def _png(*, alpha: bool) -> bytes:
    return image_document(pages=1).pages[0].pixmap(dpi=72, alpha=alpha).to_bytes("png")


@pytest.mark.parametrize("path", CORPUS, ids=lambda path: path.name)
def test_every_corpus_image_extracts(path: Path) -> None:
    with pyropdf.open(path) as doc:
        for page in doc.pages:
            for image in page.images():
                pixmap = image.pixmap()
                assert pixmap.width > 0
                assert pixmap.height > 0
                stored = image.stored()
                if stored is None or stored.filter != "DCTDecode":
                    continue
                stream = doc.objects[image.item.xref]
                raw = stream.read(decode=False)
                assert stored.data == (raw if stream["Filter"] == "DCTDecode" else zlib.decompress(raw))
                assert stored.data.startswith(b"\xff\xd8\xff")
                assert stored.extension == "jpg"


def test_corpus_image_filters_pass_through() -> None:
    filters = set()
    for path in CORPUS:
        with pyropdf.open(path) as doc:
            filters |= {stored.filter for page in doc.pages for image in page.images() if (stored := image.stored())}
    assert filters == {"CCITTFaxDecode", "DCTDecode", "JPXDecode"}


def test_ccitt_keeps_its_parameters() -> None:
    with pyropdf.open(next(path for path in CORPUS if path.name == "patent-us223898.pdf")) as doc:
        stored = doc.pages[0].images()[0].stored()
        assert stored is not None
        assert stored.filter == "CCITTFaxDecode"
        assert stored.parameters is not None
        assert "K" in stored.parameters or "Columns" in stored.parameters


def test_images_follow_the_content_model() -> None:
    doc = image_document()
    (image,) = doc.pages[0].images()
    assert image.item == doc.pages[0].content().images[0]
    assert image.index == 0
    assert image.stored() is None  # Unfiltered samples have no file format.


def test_the_decoded_image_equals_hayros_rendering() -> None:
    doc = image_document()
    pixmap = doc.pages[0].images()[0].pixmap()
    assert (pixmap.width, pixmap.height, pixmap.alpha) == (WIDTH, HEIGHT, False)
    assert bytes(pixmap) == SAMPLES
    rendered = doc.pages[0].pixmap(dpi=72)
    assert _close(bytes(pixmap), bytes(rendered))


def test_a_soft_mask_is_the_alpha() -> None:
    doc = image_document(pages=1)
    image = doc.pages[0].images()[0]
    mask = doc.objects.add_stream(
        bytes(range(0, 240, 5)),
        {
            "Type": Name("XObject"),
            "Subtype": Name("Image"),
            "Width": WIDTH,
            "Height": HEIGHT,
            "BitsPerComponent": 8,
            "ColorSpace": Name("DeviceGray"),
        },
    )
    doc.objects[image.item.xref]["SMask"] = doc.objects[mask]
    pixmap = doc.pages[0].images()[0].pixmap()
    assert pixmap.alpha
    assert bytes(pixmap)[3::4] == bytes(range(0, 240, 5))


def test_an_inline_image_decodes() -> None:
    doc = pyropdf.Document()
    doc.pages.new((2, 1))
    data = b"\xff\x00\x00\x00\x00\xff"
    doc.pages[0].obj["Contents"] = doc.objects[
        doc.objects.add_stream(b"q 2 0 0 1 0 0 cm BI /W 2 /H 1 /BPC 8 /CS /RGB ID " + data + b" EI Q")
    ]
    (image,) = doc.pages[0].images()
    assert image.item.xref == 0
    assert image.stored() is None
    assert bytes(image.pixmap()) == data


def test_replacing_a_shared_image_updates_every_page_in_one_transaction() -> None:
    doc = image_document()
    xref = doc.pages[0].images()[0].item.xref
    before = [bytes(page.pixmap(dpi=72)) for page in doc.pages]
    steps = doc.history.position
    doc.replace_image(xref, _png(alpha=False))
    assert doc.history.position == steps + 1
    assert doc.history.steps[-1] == "replace image"
    after = [bytes(page.pixmap(dpi=72)) for page in doc.pages]
    assert after[0] == after[1]
    assert _close(after[0], before[0])
    doc.history.undo()
    assert [bytes(page.pixmap(dpi=72)) for page in doc.pages] == before


def test_replacing_with_jpeg_stores_it_as_it_is() -> None:
    doc = image_document()
    xref = doc.pages[0].images()[0].item.xref
    jpeg = _jpeg()
    doc.replace_image(xref, jpeg)
    stream = doc.objects[xref]
    assert stream["Filter"] == "DCTDecode"
    assert (stream["Width"], stream["Height"], stream["ColorSpace"]) == (WIDTH, HEIGHT, "DeviceRGB")
    assert stream["Interpolate"] is False  # The image's other entries stay.
    for page in pyropdf.open(doc.to_bytes()).pages:
        image = page.images()[0]
        assert image.stored() == StoredImage("DCTDecode", jpeg, None)
        assert all(abs(a - b) <= 24 for a, b in zip(bytes(image.pixmap()), SAMPLES, strict=True))


def test_replacing_with_png_writes_its_alpha_as_a_soft_mask() -> None:
    doc = image_document()
    xref = doc.pages[0].images()[0].item.xref
    source = image_document(pages=1)
    source.objects[source.pages[0].images()[0].item.xref]["SMask"] = source.objects[
        source.objects.add_stream(
            bytes([128]) * (WIDTH * HEIGHT),
            {
                "Type": Name("XObject"),
                "Subtype": Name("Image"),
                "Width": WIDTH,
                "Height": HEIGHT,
                "BitsPerComponent": 8,
                "ColorSpace": Name("DeviceGray"),
            },
        )
    ]
    png = source.pages[0].images()[0].pixmap().to_bytes("png")
    doc.replace_image(xref, png)
    stream = doc.objects[xref]
    assert stream["SMask"]["Width"] == WIDTH
    pixmap = doc.pages[1].images()[0].pixmap()
    assert pixmap.alpha
    assert bytes(pixmap)[3::4] == bytes([128]) * (WIDTH * HEIGHT)
    assert _close(bytes(pixmap)[:3], SAMPLES[:3])


def test_replacing_with_jpeg_2000() -> None:
    with pyropdf.open(next(path for path in CORPUS if path.name == "pdfium-jpx-lzw.pdf")) as source:
        stored = source.pages[0].images()[0].stored()
    assert stored is not None
    doc = image_document()
    xref = doc.pages[0].images()[0].item.xref
    doc.replace_image(xref, stored.data)
    stream = doc.objects[xref]
    assert stream["Filter"] == "JPXDecode"
    assert (stream["Width"], stream["Height"]) == (612, 792)
    assert "ColorSpace" not in stream
    pixmap = doc.pages[1].images()[0].pixmap()
    assert (pixmap.width, pixmap.height) == (612, 792)


def test_removing_an_image_leaves_every_placement_transparent() -> None:
    doc = image_document()
    xref = doc.pages[0].images()[0].item.xref
    doc.remove_image(xref)
    for page in doc.pages:
        assert set(bytes(page.pixmap(dpi=72))) == {255}
        assert bytes(page.images()[0].pixmap()) == b"\x00\x00"


@pytest.mark.parametrize(
    ("data", "error", "match"),
    [
        (b"GIF89a", ValueError, "not JPEG"),
        (b"\xff\xd8\xff\xd9", ValueError, "frame header"),
        (b"\x89PNG\r\n\x1a\nbroken", ValueError, "not a PNG"),
    ],
)
def test_replacing_refuses_data_it_cannot_store(data: bytes, error: type[Exception], match: str) -> None:
    doc = image_document()
    xref = doc.pages[0].images()[0].item.xref
    before = doc.to_bytes()
    with pytest.raises(error, match=match):
        doc.replace_image(xref, data)
    assert doc.to_bytes() == before


def test_replacing_refuses_an_object_that_is_not_an_image() -> None:
    doc = image_document()
    page = doc.pages[0].obj.xref
    assert page is not None
    with pytest.raises(ValueError, match="not an image"):
        doc.replace_image(page, _jpeg())
    with pytest.raises(TypeError, match="xref"):
        doc.remove_image("1")  # ty: ignore[invalid-argument-type]


def test_decoding_is_bounded_by_the_document_limits() -> None:
    doc = image_document()
    limited = pyropdf.open(doc.to_bytes(), limits=pyropdf.DocumentLimits(max_total_decompressed_size=64))
    image = limited.pages[0].images()[0]
    with pytest.raises(pyropdf.LimitError) as caught:
        image.pixmap()
    assert caught.value.code == "total_decompressed_size"
