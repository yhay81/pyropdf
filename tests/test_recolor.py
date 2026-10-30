"""Converting the colors of pages to gray, RGB, or CMYK with ``page.recolor`` and ``doc.recolor``.

pylopdf has no recoloring to port (issue 400): these cases are new, and
the corpus cases check the issue's acceptance on every real document.
"""

from __future__ import annotations

import warnings
import zlib

import pytest
from corpus import ASSETS, NAMES

import pyropdf
from pyropdf import Operations


def build_raw_pdf(objects: dict[int, str]) -> bytes:
    """Return a PDF of ``objects`` by number, with a correct cross-reference table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode("latin-1")
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 00000 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def stream(content: str | bytes, entries: str = "") -> str:
    data = content.decode("latin-1") if isinstance(content, bytes) else content
    return f"<< /Length {len(data.encode('latin-1'))} {entries}>>\nstream\n{data}\nendstream"


def pages_pdf(*contents: str, resources: str = "", extra: dict[int, str] | None = None, page: str = "") -> bytes:
    """Return one page per content stream, sharing ``resources`` through the page tree; ``extra`` objects from 20."""
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(contents)))
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {len(contents)} /MediaBox [0 0 100 100] "
        f"/Resources << {resources} >> >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for i, content in enumerate(contents):
        objects[4 + 2 * i] = f"<< /Type /Page /Parent 2 0 R /Contents {5 + 2 * i} 0 R {page}>>"
        objects[5 + 2 * i] = stream(content)
    objects.update(extra or {})
    return build_raw_pdf(objects)


def colors(operations: Operations) -> list[tuple[str, tuple[float, ...]]]:
    """Return the color-setting operations, with their operands as floats."""
    names = {"g", "G", "rg", "RG", "k", "K", "sc", "SC", "scn", "SCN", "cs", "CS"}
    return [
        (op.operator, tuple(v if isinstance(v, str) else float(v) for v in op.operands))
        for op in operations
        if op.operator in names
    ]


def assert_gray(page: pyropdf.Page) -> None:
    """Assert that every pixel of the page renders with R, G, and B within 1."""
    pixmap = page.pixmap(dpi=36)
    data, step = bytes(pixmap), pixmap.channels
    worst = max(max(abs(data[i] - data[i + 1]), abs(data[i + 1] - data[i + 2])) for i in range(0, len(data), step))
    assert worst <= 1


def approx(values: tuple[float, ...]) -> tuple[object, ...]:
    return tuple(pytest.approx(v, abs=1e-3) for v in values)


# The color operators of each family.


def test_device_operators_convert_to_gray() -> None:
    doc = pyropdf.open(pages_pdf("1 0 0 rg 0 0 1 RG 0 0 0 1 k 0.5 g 0 0 10 10 re B"))
    report = doc.pages[0].recolor("gray")
    assert colors(doc.pages[0].operations) == [
        ("g", approx((0.299,))),
        ("G", approx((0.114,))),
        ("g", (0.0,)),
        ("g", (0.5,)),
    ]
    assert report.operations == 3
    assert report.pages == 1
    assert report.unconverted == ()
    assert_gray(doc.pages[0])


def test_rgb_converts_to_cmyk_with_full_black_generation() -> None:
    doc = pyropdf.open(pages_pdf("0.2 0.4 0.6 rg 0.5 g 0 0 10 10 re f"))
    doc.recolor("cmyk")
    assert colors(doc.pages[0].operations) == [("k", approx((0.4, 0.2, 0.0, 0.4))), ("k", (0.0, 0.0, 0.0, 0.5))]


def test_cmyk_converts_to_rgb() -> None:
    doc = pyropdf.open(pages_pdf("0.1 0.2 0.3 0.4 K 0 0 10 10 re S"))
    doc.recolor("rgb")
    assert colors(doc.pages[0].operations) == [("RG", approx((0.5, 0.4, 0.3)))]


def test_colors_already_in_the_target_stay() -> None:
    data = pages_pdf("0.5 g 0 0 10 10 re f")
    doc = pyropdf.open(data)
    report = doc.recolor("gray")
    assert report.operations == 0
    assert doc.to_bytes() == pyropdf.open(data).to_bytes()


def test_spaces_by_name_convert_with_their_initial_color() -> None:
    icc = "<< /N 3 /Length 0 >>\nstream\n\nendstream"
    content = "/CS0 cs 1 0 0 sc 0 0 10 10 re f /CS0 CS /CS1 cs 0.5 0.5 0.5 scn /DeviceRGB cs"
    doc = pyropdf.open(
        pages_pdf(
            content,
            resources="/ColorSpace << /CS0 [/ICCBased 20 0 R] /CS1 [/CalRGB << /WhitePoint [1 1 1] >>] >>",
            extra={20: icc},
        )
    )
    doc.recolor("gray")
    assert colors(doc.pages[0].operations) == [
        ("g", (0.0,)),
        ("g", approx((0.299,))),
        ("G", (0.0,)),
        ("g", (0.0,)),
        ("g", approx((0.5,))),
        ("g", (0.0,)),
    ]


def test_indexed_and_separation_convert_through_their_base() -> None:
    resources = (
        "/ColorSpace << /I0 [/Indexed /DeviceRGB 1 <000000FF0000>] "
        "/S0 [/Separation /Spot /DeviceCMYK << /FunctionType 2 /Domain [0 1] /C0 [0 0 0 0] /C1 [0 1 1 0] /N 1 >>] >>"
    )
    doc = pyropdf.open(pages_pdf("/I0 cs 1 sc /S0 CS 0.5 SC", resources=resources))
    doc.recolor("rgb")
    assert colors(doc.pages[0].operations) == [
        ("rg", (0.0, 0.0, 0.0)),
        ("rg", (1.0, 0.0, 0.0)),
        ("RG", (1.0, 0.0, 0.0)),
        ("RG", (1.0, 0.5, 0.5)),
    ]


def test_spaces_that_do_not_convert_are_reported_and_left() -> None:
    resources = (
        "/ColorSpace << /L0 [/Lab << /WhitePoint [1 1 1] >>] "
        "/N0 [/DeviceN [/A /B] /DeviceCMYK << /FunctionType 4 /Domain [0 1 0 1] /Range [0 1 0 1 0 1 0 1] >>] >> "
        "/Shading << /Sh0 << /ShadingType 2 /ColorSpace /DeviceRGB /Coords [0 0 1 0] "
        "/Function << /FunctionType 2 /Domain [0 1] /N 1 >> >> >>"
    )
    content = "/L0 cs 50 0 0 sc /N0 CS 1 1 SC /Pattern cs /P0 scn /Sh0 sh 1 0 0 rg"
    doc = pyropdf.open(pages_pdf(content, resources=resources))
    report = doc.recolor("gray")
    assert [(u.page, u.kind, u.detail) for u in report.unconverted] == [
        (0, "colorspace", "Lab"),
        (0, "colorspace", "DeviceN"),
        (0, "pattern", "Pattern"),
        (0, "shading", "Sh0"),
    ]
    assert colors(doc.pages[0].operations)[:6] == colors(pyropdf.open(pages_pdf(content)).pages[0].operations)[:6]
    assert colors(doc.pages[0].operations)[-1] == ("g", approx((0.299,)))


# Form XObjects.


FORM = "<< /Type /XObject /Subtype /Form /BBox [0 0 100 100] /Length {length} >>\nstream\n{content}\nendstream"


def form(content: str) -> str:
    return FORM.format(length=len(content), content=content)


def test_a_form_that_another_page_draws_is_copied() -> None:
    data = pages_pdf("/Fm0 Do", "/Fm0 Do", resources="/XObject << /Fm0 20 0 R >>", extra={20: form("1 0 0 rg")})
    doc = pyropdf.open(data)
    doc.pages[0].recolor("gray")
    first, second = doc.pages
    copy = first.obj["Resources"]["XObject"]["Fm0"]
    assert copy.xref != 20
    assert colors(Operations.parse(copy.read())) == [("g", approx((0.299,)))]
    assert "Resources" not in second.obj
    assert colors(Operations.parse(doc.objects[20].read())) == [("rg", (1.0, 0.0, 0.0))]
    assert colors(second.operations) == []


def test_a_form_inherits_the_space_where_it_is_drawn() -> None:
    # The Form sets a color in the space of each drawing: one RGB, one CMYK.
    content = "/DeviceRGB cs /Fm0 Do /DeviceCMYK cs /Fm0 Do /DeviceRGB cs /Fm0 Do"
    data = pages_pdf(content, resources="/XObject << /Fm0 20 0 R >>", extra={20: form("0 0 0 1 sc 0 1 0 sc")})
    doc = pyropdf.open(data)
    doc.recolor("gray")
    page = doc.pages[0]
    names = [str(op.operands[0]) for op in page.operations if op.operator == "Do"]
    assert names == ["Fm0", "Fm0_1", "Fm0"]
    xobjects = page.obj["Resources"]["XObject"]
    rgb = colors(Operations.parse(xobjects["Fm0"].read()))
    cmyk = colors(Operations.parse(xobjects["Fm0_1"].read()))
    # In RGB, four components do not convert and are left; in CMYK, three do not.
    assert rgb == [("sc", (0.0, 0.0, 0.0, 1.0)), ("g", approx((0.587,)))]
    assert cmyk == [("g", (0.0,)), ("sc", (0.0, 1.0, 0.0))]


def test_forms_nested_too_deep_are_reported() -> None:
    extra = {20 + i: form(f"1 0 0 rg /Fm{i + 1} Do") for i in range(17)}
    resources = "/XObject << " + " ".join(f"/Fm{i} {20 + i} 0 R" for i in range(18)) + " >>"
    extra[37] = form("0 1 0 rg")
    doc = pyropdf.open(pages_pdf("/Fm0 Do", resources=resources, extra=extra))
    report = doc.recolor("gray")
    assert [(u.kind, u.detail) for u in report.unconverted] == [("form", "Form XObjects nested deeper than 16")]


# Images.


def image_pdf(samples: bytes, space: str, width: int = 2, extra: str = "") -> bytes:
    image = stream(
        samples,
        f"/Type /XObject /Subtype /Image /Width {width} /Height 1 /BitsPerComponent 8 /ColorSpace {space} {extra}",
    )
    return pages_pdf("q 100 0 0 100 0 0 cm /Im0 Do Q", resources="/XObject << /Im0 20 0 R >>", extra={20: image})


def test_an_rgb_image_converts_to_gray_samples() -> None:
    doc = pyropdf.open(image_pdf(b"\xff\x00\x00\x00\x00\xff", "/DeviceRGB"))
    report = doc.recolor("gray")
    assert report.images == 1
    image = doc.pages[0].obj["Resources"]["XObject"]["Im0"]
    assert image.xref != 20
    assert image["ColorSpace"] == "DeviceGray"
    assert image["BitsPerComponent"] == 8
    assert image.read() == bytes([76, 29])
    assert_gray(doc.pages[0])


def test_an_rgb_image_converts_to_cmyk_samples() -> None:
    doc = pyropdf.open(image_pdf(b"\xff\x00\x00\x80\x80\x80", "/DeviceRGB"))
    doc.recolor("cmyk")
    image = doc.pages[0].obj["Resources"]["XObject"]["Im0"]
    assert image["ColorSpace"] == "DeviceCMYK"
    assert image.read() == bytes([0, 255, 255, 0, 0, 0, 0, 127])


def test_a_jpeg_stays_a_jpeg_at_its_quality() -> None:
    doc = pyropdf.open(image_pdf(b"\xff\x00\x00" * 64, "/DeviceRGB", width=64))
    pixmap = doc.pages[0].pixmap(dpi=72)
    doc.replace_image(20, pixmap.to_bytes("jpeg", quality=60))
    doc.recolor("gray")
    image = doc.pages[0].obj["Resources"]["XObject"]["Im0"]
    assert image["Filter"] == "DCTDecode"
    assert image["ColorSpace"] == "DeviceGray"
    stored = doc.pages[0].images()[0].stored()
    assert stored is not None
    from pyropdf._recolor import _jpeg_quality  # noqa: PLC0415 - the estimate, checked against its source.

    assert abs(_jpeg_quality(stored.data) - 60) <= 1
    assert_gray(doc.pages[0])


def test_an_image_that_its_mask_reads_is_reported() -> None:
    doc = pyropdf.open(image_pdf(b"\xff\x00\x00\x00\x00\xff", "/DeviceRGB", extra="/Mask [0 10 0 10 0 10]"))
    report = doc.recolor("gray")
    assert report.images == 0
    assert [(u.kind, u.detail) for u in report.unconverted] == [
        ("image", "/Im0 (object 20): its mask reads its samples")
    ]


def test_a_gray_image_stays_for_gray() -> None:
    doc = pyropdf.open(image_pdf(b"\x00\xff", "/DeviceGray"))
    assert doc.recolor("gray").images == 0
    assert doc.pages[0].obj["Parent"]["Resources"]["XObject"]["Im0"].xref == 20


def test_an_inline_image_converts() -> None:
    samples = zlib.compress(b"\xff\x00\x00\x00\xff\x00")
    content = b"q 100 0 0 100 0 0 cm BI /W 2 /H 1 /BPC 8 /CS /RGB /F /Fl ID " + samples + b"\nEI Q"
    doc = pyropdf.open(pages_pdf(content.decode("latin-1")))
    report = doc.recolor("gray")
    assert report.images == 1
    (image,) = [op for op in doc.pages[0].operations if op.operator == "BI"]
    assert isinstance(image, pyropdf.InlineImage)
    assert image.dictionary["CS"] == "G"
    assert zlib.decompress(image.data) == bytes([76, 150])
    assert_gray(doc.pages[0])


# Annotations.


def test_annotation_appearances_and_colors_convert() -> None:
    annotation = "<< /Type /Annot /Subtype /Square /Rect [10 10 90 90] /C [1 0 0] /IC [0 0 1] /AP << /N 21 0 R >> >>"
    extra = {20: f"[{annotation}]", 21: form("1 0 0 RG 0 0 1 rg 10 10 70 70 re B")}
    doc = pyropdf.open(pages_pdf("", page="/Annots 20 0 R", extra=extra))
    doc.recolor("gray")
    annot = doc.pages[0].obj["Annots"][0]
    assert [float(v) for v in annot["C"]] == [pytest.approx(0.299)]
    assert [float(v) for v in annot["IC"]] == [pytest.approx(0.114)]
    appearance = annot["AP"]["N"]
    assert appearance.xref != 21
    assert colors(Operations.parse(appearance.read())) == [("G", approx((0.299,))), ("g", approx((0.114,)))]
    assert_gray(doc.pages[0])


# The call.


def test_pages_select_the_pages_and_the_call_is_one_step() -> None:
    doc = pyropdf.open(pages_pdf("1 0 0 rg", "0 1 0 rg", "0 0 1 rg"))
    report = doc.recolor("gray", pages=[-1, 0, 2])
    assert report.pages == 2
    assert [colors(page.operations)[0][0] for page in doc.pages] == ["g", "rg", "g"]
    doc.history.undo()
    assert [colors(page.operations)[0][0] for page in doc.pages] == ["rg", "rg", "rg"]


def test_a_bad_target_raises() -> None:
    doc = pyropdf.open(pages_pdf("1 0 0 rg"))
    with pytest.raises(ValueError, match="target must be"):
        doc.recolor("grey")  # ty: ignore[invalid-argument-type]
    with pytest.raises(IndexError):
        doc.recolor("gray", pages=[1])


@pytest.mark.parametrize("name", NAMES)
def test_corpus_pages_render_gray_with_their_text(name: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", pyropdf.PyropdfWarning)
        doc = pyropdf.open(ASSETS / name)
        before = [(page.text(), [word.text for word in page.words()]) for page in doc.pages]
        count = len(doc.pages)
        doc.recolor("gray")
        saved = pyropdf.open(doc.to_bytes())
        assert len(saved.pages) == count
        assert [(page.text(), [word.text for word in page.words()]) for page in saved.pages] == before
    for page in saved.pages:
        assert_gray(page)
