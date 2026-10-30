"""Rewriting images: downsampling by placement resolution and recompressing, ported from pylopdf ebb01d3c."""

from __future__ import annotations

import functools
import io
import shutil
import struct
import subprocess
import zlib
from pathlib import Path
from typing import Literal

import pytest
from PIL import Image

import pyropdf
from pyropdf import ImageRewriteReport, Name

CORPUS = Path(__file__).resolve().parent / "assets" / "real_world"
#: A noise raster's samples, which Flate cannot shrink.
SIDE = 128


@functools.cache
def source_jpeg() -> bytes:
    """Return the 675 by 882 RGB JPEG of a corpus manuscript."""
    stored = pyropdf.open(CORPUS / "wdl6812-manuscript.pdf").pages[0].images()[0].stored()
    assert stored is not None
    return stored.data


@functools.cache
def gray_jpeg() -> bytes:
    """Return a 64 by 64 grayscale gradient JPEG at quality 95."""
    out = io.BytesIO()
    Image.linear_gradient("L").resize((64, 64)).save(out, "JPEG", quality=95)
    return out.getvalue()


@functools.cache
def noise(components: int) -> bytes:
    state, samples = 0x12345678, bytearray()
    for _ in range(SIDE * SIDE * components):
        state = (1_664_525 * state + 1_013_904_223) & 0xFFFFFFFF
        samples.append(state >> 24)
    return bytes(samples)


def image_document(
    data: bytes,
    entries: dict[str, object],
    placements: list[tuple[float, float]],
    *,
    pages: int = 1,
) -> pyropdf.Document:
    """Return ``pages`` pages that each paint one shared image object at every ``(width, height)`` placement."""
    doc = pyropdf.Document()
    xref = doc.objects.add_stream(data, {"Type": Name("XObject"), "Subtype": Name("Image")}, filter=None)
    doc.objects[xref].update(entries)
    commands = "\n".join(f"q {width} 0 0 {height} 0 0 cm /Im0 Do Q" for width, height in placements)
    content = doc.objects.add_stream(commands.encode())
    for _ in range(pages):
        doc.pages.new((200, 100))
        page = doc.pages[-1].obj
        page["Resources"] = {"XObject": {"Im0": doc.objects[xref]}}
        page["Contents"] = doc.objects[content]
    return doc


def jpeg_document(
    placements: list[tuple[float, float]], *, gray: bool = False, pages: int = 1, extra: dict[str, object] | None = None
) -> pyropdf.Document:
    data = gray_jpeg() if gray else source_jpeg()
    width, height = (64, 64) if gray else (675, 882)
    entries = {
        "Width": width,
        "Height": height,
        "BitsPerComponent": 8,
        "ColorSpace": Name("DeviceGray" if gray else "DeviceRGB"),
        "Filter": Name("DCTDecode"),
        **(extra or {}),
    }
    return image_document(data, entries, placements, pages=pages)


def flate_document(
    *, gray: bool = False, predictor: int | None = None, data: bytes | None = None
) -> tuple[pyropdf.Document, bytes]:
    """Return a page painting a noise raster as one inch, and its stored data."""
    components = 1 if gray else 3
    samples = noise(components)
    if predictor is not None and predictor >= 10:
        row = SIDE * components
        samples = b"".join(b"\x00" + samples[at : at + row] for at in range(0, len(samples), row))
    encoded = zlib.compress(samples) if data is None else data
    entries: dict[str, object] = {
        "Width": SIDE,
        "Height": SIDE,
        "BitsPerComponent": 8,
        "ColorSpace": Name("DeviceGray" if gray else "DeviceRGB"),
        "Filter": Name("FlateDecode"),
    }
    if predictor is not None:
        entries["DecodeParms"] = {"Predictor": predictor, "Colors": components, "BitsPerComponent": 8, "Columns": SIDE}
    return image_document(encoded, entries, [(72, 72)]), encoded


def stream(doc: pyropdf.Document) -> pyropdf.Stream:
    image = doc.objects.get(doc.pages[0].images()[0].item.xref)
    assert isinstance(image, pyropdf.Stream)
    return image


def size(doc: pyropdf.Document) -> tuple[int, int]:
    item = doc.pages[0].images()[0].item
    return item.width, item.height


def stored(doc: pyropdf.Document) -> bytes:
    return stream(doc).read(decode=False, max_size=None)


def mean_difference(before: pyropdf.Pixmap, after: pyropdf.Pixmap) -> float:
    left, right = bytes(before), bytes(after)
    assert len(left) == len(right)
    return sum(abs(a - b) for a, b in zip(left, right, strict=True)) / len(left)


def test_downsamples_and_reduces_a_jpeg() -> None:
    doc = jpeg_document([(72, 72)])
    before_data = stored(doc)
    before = doc.pages[0].pixmap(dpi=100)

    report = doc.rewrite_images(dpi_threshold=150, dpi_target=100, quality=65)

    after_data = stored(doc)
    assert report == ImageRewriteReport(1, 1, len(before_data), len(after_data))
    assert (report.skipped, report.bytes_saved) == (0, len(before_data) - len(after_data))
    assert size(doc) == (100, 100)
    assert stream(doc)["Filter"] == "DCTDecode"
    assert mean_difference(before, doc.pages[0].pixmap(dpi=100)) < 8
    assert size(pyropdf.open(doc.to_bytes())) == (100, 100)


def test_keeps_the_pixels_that_the_largest_placement_needs() -> None:
    doc = jpeg_document([(72, 72), (18, 18)])

    assert doc.rewrite_images(dpi_threshold=150, dpi_target=100, quality=65).rewritten == 1
    assert size(doc) == (100, 100)


def test_an_image_on_several_pages_is_rewritten_once_for_its_largest_placement() -> None:
    doc = jpeg_document([(18, 72)], pages=2)
    second = doc.pages[1].obj
    second["Contents"] = doc.objects[doc.objects.add_stream(b"q 144 0 0 36 0 0 cm /Im0 Do Q")]

    report = doc.rewrite_images(dpi_threshold=150, dpi_target=100, quality=65)

    assert (report.considered, report.rewritten) == (1, 1)
    assert size(doc) == (200, 100)
    assert doc.pages[0].images()[0].item.xref == doc.pages[1].images()[0].item.xref


def test_each_axis_follows_the_length_that_the_placement_gives_it() -> None:
    doc = jpeg_document([(144, 72)])

    assert doc.rewrite_images(dpi_threshold=150, dpi_target=100, quality=65).rewritten == 1
    assert size(doc) == (200, 100)


def test_an_image_over_the_threshold_on_one_axis_is_resampled_on_both() -> None:
    # 675 pixels over 4.5 inches is 150 dpi, 882 over 5.876 inches just above it.
    doc = jpeg_document([(324, 423.1)])

    doc.rewrite_images(dpi_threshold=150, dpi_target=100, quality=65)

    assert size(doc) == (450, 588)


def test_a_rotated_placement_measures_its_axes() -> None:
    doc = jpeg_document([])
    doc.pages[0].obj["Contents"] = doc.objects[doc.objects.add_stream(b"q 0 72 -144 0 144 0 cm /Im0 Do Q")]

    doc.rewrite_images(dpi_threshold=150, dpi_target=100, quality=65)

    assert size(doc) == (100, 200)


def test_quality_recompresses_without_downsampling() -> None:
    doc = jpeg_document([(72, 72)])
    before = len(stored(doc))

    assert doc.rewrite_images(quality=60).rewritten == 1
    assert size(doc) == (675, 882)
    assert len(stored(doc)) < before


def test_downsamples_a_grayscale_jpeg() -> None:
    doc = jpeg_document([(72, 72)], gray=True)

    assert doc.rewrite_images(dpi_threshold=48, dpi_target=36, quality=65).rewritten == 1
    assert size(doc) == (36, 36)
    assert stream(doc)["ColorSpace"] == "DeviceGray"
    assert stored(doc).startswith(b"\xff\xd8\xff")


@pytest.mark.parametrize(("gray", "predictor"), [(False, None), (False, 15), (True, 15), (False, 2)])
def test_downsamples_a_flate_raster_losslessly(*, gray: bool, predictor: int | None) -> None:
    doc, encoded = flate_document(gray=gray, predictor=predictor)

    report = doc.rewrite_images(dpi_threshold=96, dpi_target=64)

    after = stored(doc)
    assert report == ImageRewriteReport(1, 1, len(encoded), len(after))
    assert size(doc) == (64, 64)
    image = stream(doc)
    assert image["Filter"] == "FlateDecode"
    assert "DecodeParms" not in image
    decoded = doc.pages[0].images()[0].pixmap()
    assert decoded.channels == (1 if gray else 3)
    assert zlib.decompress(after) == bytes(decoded)


def test_lossless_resampling_matches_the_pixmap_scale() -> None:
    doc, _ = flate_document()
    scaled = doc.pages[0].images()[0].pixmap().scale(64, 64)

    doc.rewrite_images(dpi_threshold=96, dpi_target=64)

    assert bytes(doc.pages[0].images()[0].pixmap()) == bytes(scaled)


def test_skips_flate_that_does_not_shrink() -> None:
    doc, _ = flate_document(data=zlib.compress(noise(3), 9))
    original = doc.to_bytes()

    report = doc.rewrite_images(quality=65)

    assert (report.rewritten, report.skipped) == (0, 1)
    assert doc.to_bytes() == original


def test_skips_an_encoding_that_is_not_smaller() -> None:
    doc = jpeg_document([(72, 72)], gray=True)

    report = doc.rewrite_images(quality=100)

    assert (report.rewritten, report.skipped) == (0, 1)
    assert stored(doc) == gray_jpeg()


def test_repeating_the_same_settings_changes_nothing() -> None:
    doc = jpeg_document([(72, 72)])
    assert doc.rewrite_images(dpi_threshold=150, dpi_target=100, quality=65).rewritten == 1
    once = doc.to_bytes()

    report = doc.rewrite_images(dpi_threshold=150, dpi_target=100, quality=65)

    assert (report.rewritten, report.skipped) == (0, 1)
    assert doc.to_bytes() == once


@pytest.mark.parametrize("entry", [{"Mask": [0, 0, 0, 0, 0, 0]}, {"Decode": [1, 0, 1, 0, 1, 0]}])
def test_color_key_masks_become_soft_masks_and_decode_arrays_are_applied(entry: dict[str, object]) -> None:
    doc = jpeg_document([(72, 72)], extra=entry)
    before = doc.pages[0].pixmap(dpi=100)

    report = doc.rewrite_images(dpi_threshold=150, dpi_target=100, quality=65)

    assert (report.considered, report.rewritten, report.unsupported) == (1, 1, ())
    assert "Decode" not in stream(doc)
    assert "Mask" not in stream(doc)
    assert mean_difference(before, doc.pages[0].pixmap(dpi=100)) < 8


def test_a_decoding_failure_rolls_back_every_image() -> None:
    doc = jpeg_document([(72, 72)])
    broken = doc.objects.add_stream(
        zlib.compress(noise(3)),
        {"Type": Name("XObject"), "Subtype": Name("Image"), "Width": SIDE, "Height": SIDE},
        filter=None,
    )
    doc.objects[broken].update(
        {
            "BitsPerComponent": 8,
            "ColorSpace": Name("DeviceRGB"),
            "Filter": Name("FlateDecode"),
            "DecodeParms": {"Predictor": 9, "Colors": 3, "BitsPerComponent": 8, "Columns": SIDE},
        }
    )
    page = doc.pages[0].obj
    page["Resources"]["XObject"]["Im1"] = doc.objects[broken]
    page["Contents"] = doc.objects[doc.objects.add_stream(b"q 72 0 0 72 0 0 cm /Im0 Do Q q 72 0 0 72 0 0 cm /Im1 Do Q")]
    original = doc.to_bytes()

    with pytest.raises(pyropdf.PdfError, match="cannot be decoded"):
        doc.rewrite_images(dpi_threshold=150, dpi_target=100, quality=65)

    assert doc.to_bytes() == original


def test_skips_a_source_above_the_pixel_limit_before_decoding() -> None:
    doc = jpeg_document([(72, 72)], extra={"Width": 10_000, "Height": 7_000})

    report = doc.rewrite_images(dpi_threshold=150, dpi_target=100, quality=65)

    assert (report.considered, report.rewritten) == (1, 0)


def test_many_placements_count_one_object() -> None:
    doc = jpeg_document([(1, 1)] * 10_000, gray=True)

    assert doc.rewrite_images(quality=100).considered == 1


def test_a_document_without_images_is_a_no_op() -> None:
    doc = pyropdf.Document()
    doc.pages.new((100, 100))
    assert doc.rewrite_images(quality=75) == ImageRewriteReport(0, 0, 0, 0)


@pytest.mark.parametrize(
    ("dpi", "error"),
    [(True, TypeError), ("150", TypeError), (35, ValueError), (2401, ValueError), (float("inf"), ValueError)],
)
def test_refuses_an_invalid_dpi(dpi: object, error: type[Exception]) -> None:
    doc = jpeg_document([(72, 72)])
    with pytest.raises(error, match="dpi_threshold must"):
        doc.rewrite_images(dpi_threshold=dpi, dpi_target=36, quality=75)  # ty: ignore[invalid-argument-type]
    with pytest.raises(error, match="dpi_target must"):
        doc.rewrite_images(dpi_threshold=2400, dpi_target=dpi, quality=75)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("quality", "error"), [(True, TypeError), (75.5, TypeError), ("75", TypeError), (0, ValueError), (101, ValueError)]
)
def test_refuses_an_invalid_quality(quality: object, error: type[Exception]) -> None:
    with pytest.raises(error, match="quality must"):
        jpeg_document([(72, 72)]).rewrite_images(quality=quality)  # ty: ignore[invalid-argument-type]


def test_refuses_a_flag_that_is_not_a_bool() -> None:
    with pytest.raises(TypeError, match="lossy must be a bool"):
        jpeg_document([(72, 72)]).rewrite_images(quality=75, lossy=1)  # ty: ignore[invalid-argument-type]


def test_the_target_must_be_below_the_threshold() -> None:
    doc = jpeg_document([(72, 72)])
    with pytest.raises(ValueError, match="must be less than"):
        doc.rewrite_images(dpi_threshold=100, dpi_target=100, quality=65)
    with pytest.raises(ValueError, match="needs a dpi_target"):
        doc.rewrite_images(dpi_threshold=100, quality=65)
    with pytest.raises(ValueError, match="needs a dpi_threshold"):
        doc.rewrite_images(dpi_target=100, quality=65)


def test_resampling_a_jpeg_without_quality_uses_75() -> None:
    doc = jpeg_document([(72, 72)])
    expected = doc.pages[0].images()[0].pixmap().scale(100, 100).to_bytes("jpeg", quality=75)

    assert doc.rewrite_images(dpi_threshold=150, dpi_target=100).rewritten == 1
    # The same pixels; the JFIF header's density may differ.
    assert bytes(pyropdf.Pixmap.from_bytes(stored(doc))) == bytes(pyropdf.Pixmap.from_bytes(expected))


def test_set_to_gray_converts_color_images() -> None:
    doc = jpeg_document([(72, 72)])

    assert doc.rewrite_images(set_to_gray=True).rewritten == 1
    assert stream(doc)["ColorSpace"] == "DeviceGray"
    assert size(doc) == (675, 882)
    assert doc.pages[0].images()[0].pixmap().channels == 1


def test_nothing_to_do_returns_zero_counts() -> None:
    doc = jpeg_document([(72, 72)])
    original = doc.to_bytes()

    assert doc.rewrite_images() == ImageRewriteReport(0, 0, 0, 0)
    assert doc.to_bytes() == original


def test_keeps_images_at_or_below_the_threshold() -> None:
    doc = jpeg_document([(72, 72)])

    doc.rewrite_images(dpi_threshold=882, dpi_target=100, quality=65)

    assert size(doc) == (675, 882)


@pytest.mark.parametrize(
    ("keyword", "kind"),
    [("lossy", "jpeg"), ("lossless", "rgb"), ("color", "rgb"), ("gray", "gray"), ("bitonal", "bitonal")],
)
def test_the_flags_select_kinds_of_image(keyword: str, kind: str) -> None:
    if kind == "jpeg":
        doc = jpeg_document([(72, 72)])
    elif kind == "bitonal":
        bits = bytes(range(256)) * (SIDE * SIDE // 8 // 256)
        doc = image_document(
            bits,
            {"Width": SIDE, "Height": SIDE, "BitsPerComponent": 1, "ColorSpace": Name("DeviceGray")},
            [(36, 36)],
        )
    else:
        doc, _ = flate_document(gray=kind == "gray")
    original = doc.to_bytes()

    excluded = doc.rewrite_images(dpi_threshold=96, dpi_target=64, **{keyword: False})  # ty: ignore[invalid-argument-type]

    assert (excluded.rewritten, excluded.skipped) == (0, 1)
    assert doc.to_bytes() == original
    assert doc.rewrite_images(dpi_threshold=96, dpi_target=64).rewritten == 1


def test_history_undoes_the_rewrite() -> None:
    doc = jpeg_document([(72, 72)])
    original = doc.to_bytes()
    doc.rewrite_images(dpi_threshold=150, dpi_target=100, quality=65)

    doc.history.undo()

    assert doc.to_bytes() == original


@pytest.mark.parametrize("name", ["bunka-kokugo-series-019-p4.pdf", "wdl6812-manuscript.pdf"])
def test_corpus_documents_with_large_images_shrink_and_render_alike(name: str) -> None:
    doc = pyropdf.open(CORPUS / name)
    before = len(doc.to_bytes())
    pixmaps = [page.pixmap(dpi=100) for page in doc.pages]

    report = doc.rewrite_images(dpi_threshold=150, dpi_target=100, quality=65)

    assert report.rewritten >= 1
    assert report.bytes_saved > 0
    assert len(doc.to_bytes()) < before
    for page, pixmap in zip(doc.pages, pixmaps, strict=True):
        assert mean_difference(pixmap, page.pixmap(dpi=100)) < 4


def gradient(components: int, side: int = SIDE) -> bytes:
    """Return smooth 8-bit samples that resampling keeps close to the source."""
    return bytes(
        (x * 2 + y + channel * 40) % 256 for y in range(side) for x in range(side) for channel in range(components)
    )


def add_image(doc: pyropdf.Document, data: bytes, entries: dict[str, object]) -> pyropdf.Stream:
    xref = doc.objects.add_stream(data, {"Type": Name("XObject"), "Subtype": Name("Image"), **entries})
    return doc.objects[xref]


def soft_mask(doc: pyropdf.Document, side: int = SIDE, **extra: object) -> pyropdf.Stream:
    alpha = bytes((x * 255 // (side - 1)) for _ in range(side) for x in range(side))
    entries = {"Width": side, "Height": side, "BitsPerComponent": 8, "ColorSpace": Name("DeviceGray"), **extra}
    return add_image(doc, alpha, entries)


def rgb_entries(**extra: object) -> dict[str, object]:
    return {"Width": SIDE, "Height": SIDE, "BitsPerComponent": 8, "ColorSpace": Name("DeviceRGB"), **extra}


def ghostscript(doc: pyropdf.Document) -> Image.Image:
    """Render the first page with Ghostscript at 72 dpi, an independent renderer."""
    executable = shutil.which("gs")
    if executable is None:
        pytest.skip("Ghostscript is not installed")
    result = subprocess.run(  # noqa: S603 - a fixed command on a document that the test wrote
        [executable, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=png16m", "-r72", "-sOutputFile=-", "-"],
        input=doc.to_bytes(),
        capture_output=True,
        check=True,
    )
    return Image.open(io.BytesIO(result.stdout)).convert("RGB")


def gs_difference(before: Image.Image, after: Image.Image) -> float:
    left, right = before.tobytes(), after.tobytes()
    return sum(abs(a - b) for a, b in zip(left, right, strict=True)) / len(left)


def test_a_soft_mask_is_resampled_into_a_new_mask_and_a_shared_one_stays() -> None:
    doc = image_document(gradient(3), rgb_entries(), [(36, 36)])
    mask = soft_mask(doc)
    stream(doc)["SMask"] = mask
    other = add_image(doc, zlib.compress(gradient(3)), rgb_entries(Filter=Name("FlateDecode"), SMask=mask))
    mask_data = mask.read(decode=False)
    before, gs_before = doc.pages[0].pixmap(dpi=72), ghostscript(doc)

    report = doc.rewrite_images(dpi_threshold=150, dpi_target=72)

    assert (report.rewritten, report.unsupported) == (1, ())
    assert size(doc) == (36, 36)
    new_mask = stream(doc)["SMask"]
    assert isinstance(new_mask, pyropdf.Stream)
    assert (new_mask["Width"], new_mask["Height"]) == (36, 36)
    assert new_mask.read() != mask.read()
    assert mask.read(decode=False) == mask_data
    assert other["SMask"].read(decode=False) == mask_data
    assert mean_difference(before, doc.pages[0].pixmap(dpi=72)) < 4
    assert gs_difference(gs_before, ghostscript(doc)) < 4


@pytest.mark.parametrize("kind", ["stencil", "matte", "other-size"])
def test_masks_that_a_rewrite_cannot_keep_are_reported(kind: str) -> None:
    doc = image_document(zlib.compress(gradient(3)), rgb_entries(Filter=Name("FlateDecode")), [(36, 36)])
    if kind == "stencil":
        stencil = add_image(
            doc, bytes(SIDE * SIDE // 4 // 8), {"Width": SIDE // 2, "Height": SIDE // 2, "ImageMask": True}
        )
        stream(doc)["Mask"] = stencil
        expected = "stencil mask of another size"
    elif kind == "matte":
        stream(doc)["SMask"] = soft_mask(doc, Matte=[0, 0, 0])
        expected = "soft mask with /Matte"
    else:
        stream(doc)["SMask"] = soft_mask(doc, side=SIDE // 2)
        expected = "soft mask of another size"
    original = doc.to_bytes()

    report = doc.rewrite_images(dpi_threshold=150, dpi_target=72)

    assert report.rewritten == 0
    assert len(report.unsupported) == 1
    assert expected in report.unsupported[0]
    assert doc.to_bytes() == original


def test_a_stencil_mask_of_the_image_size_becomes_a_soft_mask() -> None:
    doc = image_document(gradient(3), rgb_entries(), [(36, 36)])
    rows = bytes([0b11110000] * (SIDE // 8)) * SIDE
    stream(doc)["Mask"] = add_image(doc, rows, {"Width": SIDE, "Height": SIDE, "ImageMask": True})
    before = doc.pages[0].pixmap(dpi=72)

    assert doc.rewrite_images(dpi_threshold=150, dpi_target=72).rewritten == 1

    assert "Mask" not in stream(doc)
    assert isinstance(stream(doc)["SMask"], pyropdf.Stream)
    assert mean_difference(before, doc.pages[0].pixmap(dpi=72)) < 6


def test_cmyk_samples_keep_their_color_space_and_resample_by_channel() -> None:
    samples = gradient(4)
    entries = {**rgb_entries(Filter=Name("FlateDecode")), "ColorSpace": Name("DeviceCMYK")}
    doc = image_document(zlib.compress(samples), entries, [(36, 36)])
    before, gs_before = doc.pages[0].pixmap(dpi=72), ghostscript(doc)

    report = doc.rewrite_images(dpi_threshold=150, dpi_target=72, lossless_format="jpeg")

    assert (report.rewritten, report.unsupported) == (1, ())
    image = stream(doc)
    assert (image["ColorSpace"], image["Filter"], image["BitsPerComponent"]) == ("DeviceCMYK", "FlateDecode", 8)
    channels = [
        bytes(pyropdf.Pixmap.from_bytes(pam(SIDE, 1, samples[channel::4])).scale(36, 36)) for channel in range(4)
    ]
    assert image.read() == bytes(value for pixel in zip(*channels, strict=True) for value in pixel)
    assert mean_difference(before, doc.pages[0].pixmap(dpi=72)) < 4
    assert gs_difference(gs_before, ghostscript(doc)) < 4


def pam(side: int, depth: int, data: bytes) -> bytes:
    kinds = {1: "GRAYSCALE", 3: "RGB"}
    header = f"P7\nWIDTH {side}\nHEIGHT {side}\nDEPTH {depth}\nMAXVAL 255\nTUPLTYPE {kinds[depth]}\nENDHDR\n"
    return header.encode() + data


def test_an_icc_based_image_keeps_its_profile_and_its_samples() -> None:
    doc = image_document(gradient(3), rgb_entries(), [(36, 36)])
    profile = doc.objects[doc.objects.add_stream(b"not read", {"N": 3, "Alternate": Name("DeviceRGB")})]
    stream(doc)["ColorSpace"] = [Name("ICCBased"), profile]

    report = doc.rewrite_images(dpi_threshold=150, dpi_target=72, lossless_format="jpeg", quality=95)

    assert report.rewritten == 1
    space = stream(doc)["ColorSpace"]
    assert isinstance(space, pyropdf.Array)
    assert space[0] == "ICCBased"
    assert space[1].xref == profile.xref
    assert stream(doc)["Filter"] == "DCTDecode"
    expected = pyropdf.Pixmap.from_bytes(pam(SIDE, 3, gradient(3))).scale(36, 36)
    assert mean_difference(expected, pyropdf.Pixmap.from_bytes(stored(doc))) < 3


def test_a_cmyk_jpeg_is_reported() -> None:
    out = io.BytesIO()
    Image.new("CMYK", (64, 64), (10, 20, 30, 40)).save(out, "JPEG")
    entries = {"Width": 64, "Height": 64, "BitsPerComponent": 8, "ColorSpace": Name("DeviceCMYK")}
    doc = image_document(out.getvalue(), {**entries, "Filter": Name("DCTDecode")}, [(10, 10)])

    report = doc.rewrite_images(dpi_threshold=150, dpi_target=72)

    assert report.rewritten == 0
    assert report.unsupported == (f"object {stream(doc).xref}: a CMYK JPEG",)


def test_the_lossless_format_can_be_jpeg() -> None:
    doc, _ = flate_document()

    assert doc.rewrite_images(quality=80).rewritten == 0  # Flate again does not save 5%.
    assert doc.rewrite_images(quality=80, lossless_format="jpeg").rewritten == 1
    assert stream(doc)["Filter"] == "DCTDecode"


def scan(side: int = 256) -> bytes:
    """Return 1-bit rows, 1 for white, of black bars that Group 4 codes well."""
    rows = []
    for y in range(side):
        row = bytearray(b"\xff" * (side // 8))
        if (y // 8) % 3 == 0:
            for x in range(side // 8):
                row[x] = 0x0F if x % 4 else 0x00
        rows.append(bytes(row))
    return b"".join(rows)


def tiff_g4(data: bytes, width: int, height: int) -> Image.Image:
    """Wrap Group 4 data in a TIFF, as libtiff reads it: an independent decoder."""
    tags = [(256, 4, width), (257, 4, height), (258, 3, 1), (259, 3, 4), (262, 3, 0), (273, 4, 122)]
    tags += [(277, 3, 1), (278, 4, height), (279, 4, len(data))]
    entries = b"".join(
        struct.pack("<HHII", tag, kind, 1, value) if kind == 4 else struct.pack("<HHIHH", tag, kind, 1, value, 0)
        for tag, kind, value in tags
    )
    header = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", len(tags)) + entries + b"\x00" * 4
    return Image.open(io.BytesIO(header + data))


@pytest.mark.parametrize("bitonal_format", ["fax", "flate"])
def test_bitonal_images_are_written_as_group_4_or_one_bit_flate(bitonal_format: Literal["fax", "flate"]) -> None:
    rows = scan()
    entries: dict[str, object] = {"Width": 256, "Height": 256, "BitsPerComponent": 1, "ColorSpace": Name("DeviceGray")}
    doc = image_document(rows, entries, [(72, 72)])
    before = doc.pages[0].pixmap(dpi=72)

    report = doc.rewrite_images(quality=75, bitonal_format=bitonal_format)

    assert report.rewritten == 1
    image = stream(doc)
    assert image["BitsPerComponent"] == 1
    if bitonal_format == "fax":
        assert image["Filter"] == "CCITTFaxDecode"
        assert dict(image["DecodeParms"].items()) == {"K": -1, "Columns": 256, "Rows": 256}
        decoded = tiff_g4(stored(doc), 256, 256)
        decoded.load()
        assert decoded.convert("1").tobytes() == rows
    else:
        assert image["Filter"] == "FlateDecode"
        assert image.read() == rows
    assert bytes(doc.pages[0].pixmap(dpi=72)) == bytes(before)


def test_a_resampled_bitonal_image_stays_one_bit() -> None:
    rows = scan()
    entries: dict[str, object] = {"Width": 256, "Height": 256, "BitsPerComponent": 1, "ColorSpace": Name("DeviceGray")}
    doc = image_document(rows, entries, [(36, 36)])
    gs_before = ghostscript(doc)

    assert doc.rewrite_images(dpi_threshold=300, dpi_target=200).rewritten == 1

    assert size(doc) == (100, 100)
    assert (stream(doc)["BitsPerComponent"], stream(doc)["Filter"]) == (1, "CCITTFaxDecode")
    assert gs_difference(gs_before, ghostscript(doc)) < 12


def inline_document(dictionary: str, data: bytes, placement: str = "10 0 0 10 0 0") -> pyropdf.Document:
    doc = image_document(b"", {}, [])
    commands = f"q {placement} cm BI {dictionary} ID ".encode() + data + b"\nEI Q"
    doc.pages[0].obj["Contents"] = doc.objects[doc.objects.add_stream(commands)]
    return doc


def inline_images(doc: pyropdf.Document) -> list[pyropdf.InlineImage]:
    return [op for op in doc.pages[0].operations if isinstance(op, pyropdf.InlineImage)]


def test_an_inline_image_is_resampled_by_its_placement() -> None:
    samples = gradient(3, 64)
    doc = inline_document("/W 64 /H 64 /CS /RGB /BPC 8 /F /Fl", zlib.compress(samples), "32 0 0 32 10 10")
    before, gs_before = doc.pages[0].pixmap(dpi=72), ghostscript(doc)

    report = doc.rewrite_images(dpi_threshold=100, dpi_target=72)

    assert (report.considered, report.rewritten, report.unsupported) == (1, 1, ())
    (image,) = inline_images(doc)
    assert (image.dictionary["W"], image.dictionary["H"], image.dictionary["F"]) == (32, 32, "Fl")
    assert mean_difference(before, doc.pages[0].pixmap(dpi=72)) < 4
    assert gs_difference(gs_before, ghostscript(doc)) < 4


def test_an_inline_bitonal_image_becomes_group_4() -> None:
    rows = scan(64)
    doc = inline_document("/W 64 /H 64 /CS /G /BPC 1", rows, "64 0 0 64 0 0")
    before = doc.pages[0].pixmap(dpi=72)

    assert doc.rewrite_images(quality=75).rewritten == 1

    (image,) = inline_images(doc)
    assert image.dictionary["F"] == "CCF"
    assert bytes(doc.pages[0].pixmap(dpi=72)) == bytes(before)


def test_inline_images_that_a_rewrite_does_not_read_are_reported() -> None:
    doc = inline_document("/W 1 /H 1 /CS /CMYK /BPC 8", b"\x00\x00\x00\x00")
    form = doc.objects.add_stream(
        b"BI /W 1 /H 1 /CS /RGB /BPC 8 ID \xff\x00\x00\nEI",
        {"Type": Name("XObject"), "Subtype": Name("Form"), "BBox": [0, 0, 1, 1]},
    )
    doc.pages[0].obj["Resources"]["XObject"]["Fm0"] = doc.objects[form]
    contents = doc.pages[0].obj["Contents"]
    contents.write(contents.read() + b" q 10 0 0 10 0 0 cm /Fm0 Do Q")

    report = doc.rewrite_images(quality=75)

    assert report.rewritten == 0
    assert "an inline image in CMYK" in report.unsupported
    assert "page 0: 1 inline images in Form XObjects or annotation appearances" in report.unsupported


@pytest.mark.parametrize("keyword", ["lossless_format", "bitonal_format"])
def test_refuses_an_unknown_format(keyword: str) -> None:
    with pytest.raises(ValueError, match=keyword):
        pyropdf.Document().rewrite_images(quality=75, **{keyword: "png"})  # ty: ignore[invalid-argument-type]


def test_set_to_gray_leaves_vector_colors_to_recolor() -> None:
    doc, _ = flate_document()
    contents = doc.pages[0].obj["Contents"]
    contents.write(contents.read() + b" 1 0 0 rg 100 0 50 50 re f")

    doc.rewrite_images(set_to_gray=True)

    assert doc.pages[0].pixmap(dpi=72).pixel(120, 70)[:3] == (255, 0, 0)
    assert stream(doc)["ColorSpace"] == "DeviceGray"
    doc.recolor("gray")
    red = doc.pages[0].pixmap(dpi=72).pixel(120, 70)[:3]
    assert red[0] == red[1] == red[2]
