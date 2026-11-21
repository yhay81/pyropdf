"""Converting the colors of pages to gray, RGB, or CMYK with ``page.recolor`` and ``doc.recolor``.

pylopdf has no recoloring to port (issue 400): these cases are new, and
the corpus cases check the issue's acceptance on every real document.
"""

from __future__ import annotations

import io
import warnings
import zlib

import pikepdf
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


@pytest.mark.spec("device-color-conversion", "edit")
@pytest.mark.spec("device-color-spaces", "read", "interpret", "edit")
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


@pytest.mark.spec("device-color-spaces", "create")
def test_rgb_converts_to_cmyk_with_full_black_generation() -> None:
    doc = pyropdf.open(pages_pdf("0.2 0.4 0.6 rg 0.5 g 0 0 10 10 re f"))
    doc.recolor("cmyk")
    assert colors(doc.pages[0].operations) == [("k", approx((0.4, 0.2, 0.0, 0.4))), ("k", (0.0, 0.0, 0.0, 0.5))]


def test_cmyk_converts_to_rgb() -> None:
    doc = pyropdf.open(pages_pdf("0.1 0.2 0.3 0.4 K 0 0 10 10 re S"))
    doc.recolor("rgb")
    assert colors(doc.pages[0].operations) == [("RG", approx((0.5, 0.4, 0.3)))]


@pytest.mark.spec("device-color-spaces", "preserve")
def test_colors_already_in_the_target_stay() -> None:
    data = pages_pdf("0.5 g 0 0 10 10 re f")
    doc = pyropdf.open(data)
    report = doc.recolor("gray")
    assert report.operations == 0
    assert doc.to_bytes() == pyropdf.open(data).to_bytes()


@pytest.mark.spec("calrgb-color-space", "read", "interpret", "edit")
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


@pytest.mark.spec("exponential-functions", "read", "interpret")
@pytest.mark.spec("indexed-color-space", "read", "interpret")
@pytest.mark.spec("separation-color-space", "read", "interpret", "edit")
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


@pytest.mark.spec("form-xobjects", "edit")
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


def test_an_indexed_inline_image_converts_by_index() -> None:
    content = b"q 100 0 0 100 0 0 cm BI /W 2 /H 1 /BPC 8 /CS [/I /RGB 1 <000000FF0000>] ID \x00\x01\nEI Q"
    doc = pyropdf.open(pages_pdf(content.decode("latin-1")))
    assert doc.recolor("gray").images == 1
    (image,) = [op for op in doc.pages[0].operations if op.operator == "BI"]
    assert isinstance(image, pyropdf.InlineImage)
    assert zlib.decompress(image.data) == bytes([0, 76])


def test_an_inline_image_with_decode_parameters_is_reported() -> None:
    # PNG Up predictor: each row starts with its filter byte.
    samples = zlib.compress(b"\x02\xff\x00\x00\x00\xff\x00")
    content = (
        b"q 100 0 0 100 0 0 cm BI /W 2 /H 1 /BPC 8 /CS /RGB /F /Fl "
        b"/DP << /Predictor 12 /Colors 3 /Columns 2 >> ID " + samples + b"\nEI Q"
    )
    doc = pyropdf.open(pages_pdf(content.decode("latin-1")))
    report = doc.recolor("gray")
    assert report.images == 0
    assert [(u.kind, u.detail) for u in report.unconverted] == [("image", "inline image in DeviceRGB")]
    (image,) = [op for op in doc.pages[0].operations if op.operator == "BI"]
    assert isinstance(image, pyropdf.InlineImage)
    assert image.dictionary["CS"] == "RGB"


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


def test_shared_appearance_characteristics_stay_on_other_pages() -> None:
    extra = {
        4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /Annots [21 0 R] >>",
        6: "<< /Type /Page /Parent 2 0 R /Contents 7 0 R /Annots [22 0 R] >>",
        20: "<< /BC [1 0 0] /BG [0 0 1] >>",
        21: "<< /Type /Annot /Subtype /Widget /Rect [10 10 90 90] /MK 20 0 R >>",
        22: "<< /Type /Annot /Subtype /Widget /Rect [10 10 90 90] /MK 20 0 R >>",
    }
    doc = pyropdf.open(pages_pdf("", "", extra=extra))
    doc.pages[0].recolor("gray")
    first, second = (page.obj["Annots"][0]["MK"] for page in doc.pages)
    assert [float(v) for v in first["BC"]] == [pytest.approx(0.299)]
    assert [float(v) for v in first["BG"]] == [pytest.approx(0.114)]
    assert second.xref == 20
    assert [float(v) for v in second["BC"]] == [1.0, 0.0, 0.0]
    assert [float(v) for v in doc.objects[20]["BG"]] == [0.0, 0.0, 1.0]


# The call.


def test_pages_select_the_pages_and_the_call_is_one_step() -> None:
    doc = pyropdf.open(pages_pdf("1 0 0 rg", "0 1 0 rg", "0 0 1 rg"))
    report = doc.recolor("gray", pages=[-1, 0, 2])
    assert report.pages == 2
    assert [colors(page.operations)[0][0] for page in doc.pages] == ["g", "rg", "g"]
    doc.history.undo()
    assert [colors(page.operations)[0][0] for page in doc.pages] == ["rg", "rg", "rg"]


def test_shared_objects_are_reported_on_every_page_that_draws_them() -> None:
    image = stream(
        "\xff\x00\x00\x00\x00\xff",
        "/Type /XObject /Subtype /Image /Width 2 /Height 1 /BitsPerComponent 8 /ColorSpace /DeviceRGB "
        "/Mask [0 10 0 10 0 10]",
    )
    resources = "/XObject << /Im0 20 0 R /Fm0 21 0 R >> /ColorSpace << /L0 [/Lab << /WhitePoint [1 1 1] >>] >>"
    extra = {20: image, 21: form("/L0 cs 50 0 0 sc")}
    doc = pyropdf.open(pages_pdf("/Im0 Do /Fm0 Do", "/Fm0 Do /Im0 Do", resources=resources, extra=extra))
    report = doc.recolor("gray")
    image_detail = "/Im0 (object 20): its mask reads its samples"
    assert [(u.page, u.kind, u.detail) for u in report.unconverted] == [
        (0, "image", image_detail),
        (0, "colorspace", "Lab"),
        (1, "colorspace", "Lab"),
        (1, "image", image_detail),
    ]


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


def _curve(start: float, end: float, exponent: int = 1) -> str:
    return f"<< /FunctionType 2 /Domain [0 1] /C0 [{start}] /C1 [{end}] /N {exponent} >>"


@pytest.mark.spec("device-color-conversion", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_active_black_and_removal_at_paint_time_survive_both_saves(*, incremental: bool) -> None:
    zero, identity = _curve(0, 0), _curve(0, 1)
    resources = (
        f"/ExtGState << /Zero << /BG2 {zero} /UCR2 {zero} >> "
        f"/Boost << /BG {zero} /BG2 {identity} /UCR2 {_curve(-0.75, -0.75)} >> "
        f"/Defaults << /UCR {zero} /UCR2 /Default /BG2 /Default >> >>"
    )
    neutral = ".5 .5 .5 rg "
    paint = "0 0 4 4 re f "
    content = (
        neutral
        + paint
        + "q /Zero gs "
        + paint
        + "/Boost gs "
        + paint
        + "Q "
        + paint
        + "/Defaults gs "
        + neutral
        + paint
    )
    source = pages_pdf(content, "0 1 0 rg 0 0 1 1 re f", resources=resources)
    doc = pyropdf.open(source)
    assert doc.pages[0].recolor("cmyk").unconverted == ()
    saved = doc.to_bytes(incremental=incremental)
    if incremental:
        assert saved.startswith(source)
    k_only = [0, 0, 0, 0.5]
    with pikepdf.Pdf.open(io.BytesIO(saved)) as checked:
        operations = pikepdf.parse_content_stream(checked.pages[0])
        inks = [[float(x) for x in command.operands] for command in operations if str(command.operator) == "k"]
        assert inks == [k_only, [0.5, 0.5, 0.5, 0], [1, 1, 1, 0.5], k_only]
        assert checked.pages[0].Resources.ExtGState.Zero.BG2.C1 == [0]
        assert checked.pages[1].Contents.read_bytes() == b"0 1 0 rg 0 0 1 1 re f"
    assert [tuple(ink) for ink in inks] == [
        operands for operator, operands in colors(pyropdf.open(saved).pages[0].operations) if operator == "k"
    ]


@pytest.mark.parametrize(
    ("entry", "extra", "black"),
    [
        (
            "20 0 R",
            {20: stream(b"\x00\xff", "/FunctionType 0 /Domain [0 1] /Range [0 1] /BitsPerSample 8 /Size [2]")},
            0.5,
        ),
        ("20 0 R", {20: stream("{ dup mul }", "/FunctionType 4 /Domain [0 1] /Range [0 1]")}, 0.25),
        (
            (
                f"<< /FunctionType 3 /Domain [0 1] /Functions [{_curve(0, 0)} {_curve(1, 1)}] "
                "/Bounds [0.5] /Encode [0 1 0 1] >>"
            ),
            {},
            1.0,
        ),
        (_curve(0, 1, 2), {}, 0.25),
    ],
)
def test_all_four_scalar_function_families_use_the_existing_evaluator(
    entry: str, extra: dict[int, str], black: float
) -> None:
    doc = pyropdf.open(
        pages_pdf(
            "/G gs .5 .5 .5 rg 0 0 10 10 re f",
            resources=f"/ExtGState << /G << /BG2 {entry} /UCR2 {_curve(0, 0)} >> >>",
            extra=extra,
        )
    )
    assert not doc.recolor("cmyk").unconverted
    assert colors(doc.pages[0].operations) == [("k", approx((0.5, 0.5, 0.5, black)))]


@pytest.mark.parametrize("target", ["rgb", "gray"])
def test_other_targets_do_not_apply_output_black_curves(target: str) -> None:
    doc = pyropdf.open(
        pages_pdf("/G gs .5 .5 .5 rg 0 0 10 10 re f", resources="/ExtGState << /G << /BG2 /Unknown >> >>")
    )
    assert not doc.recolor(target).unconverted  # ty: ignore[invalid-argument-type]
    assert colors(doc.pages[0].operations)[0][1] == approx((0.5,) * (3 if target == "rgb" else 1))


def test_an_unevaluable_function_restores_the_original_selection() -> None:
    paint = "/DeviceRGB cs .3 .5 .7 sc /Bad gs 0 0 5 5 re f"
    doc = pyropdf.open(pages_pdf(paint, resources="/ExtGState << /Bad << /BG2 << /FunctionType 88 >> >> >>"))
    report = doc.recolor("cmyk")
    assert [entry.kind for entry in report.unconverted] == ["conversion"]
    assert colors(doc.pages[0].operations)[-1] == ("rg", approx((0.3, 0.5, 0.7)))


@pytest.mark.spec("device-color-conversion", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_reused_forms_and_images_observe_their_invocation_state(*, incremental: bool) -> None:
    pixel = bytes([128] * 3)
    inline = "BI /W 1 /H 1 /BPC 8 /CS /RGB ID " + pixel.decode("latin-1") + "\nEI"
    draw = f"/Fm Do /Im Do {inline} "
    # Repeated invocations of the same resources within two levels of q/Q.
    content = draw + "q /Z gs q " + draw + "Q Q " + draw
    resources = f"/ExtGState << /Z << /BG {_curve(0, 0)} /UCR {_curve(0, 0)} >> >> /XObject << /Fm 20 0 R /Im 21 0 R >>"
    extra = {
        20: form(".5 .5 .5 rg 0 0 4 4 re f"),
        21: stream(
            pixel, "/Type /XObject /Subtype /Image /Width 1 /Height 1 /BitsPerComponent 8 /ColorSpace /DeviceRGB"
        ),
    }
    source = pages_pdf(content, "/Im Do", resources=resources, extra=extra)
    doc = pyropdf.open(source)
    report = doc.pages[0].recolor("cmyk")
    assert not report.unconverted
    assert report.images == 5  # Two indirect variants and three inline occurrences.
    with pikepdf.Pdf.open(io.BytesIO(doc.to_bytes(incremental=incremental))) as checked:
        page = checked.pages[0]
        names = [str(op.operands[0]) for op in pikepdf.parse_content_stream(page) if str(op.operator) == "Do"]
        assert names == ["/Fm", "/Im", "/Fm_1", "/Im_1", "/Fm", "/Im"]
        variants = page.Resources.XObject
        assert [variants[name].read_bytes() for name in ("/Im", "/Im_1")] == [
            bytes([0, 0, 0, 127]),
            bytes([127, 127, 127, 0]),
        ]
        for name, expected in (("/Fm", [0, 0, 0, 0.5]), ("/Fm_1", [0.5, 0.5, 0.5, 0])):
            (paint,) = [op for op in pikepdf.parse_content_stream(variants[name]) if str(op.operator) == "k"]
            assert [float(n) for n in paint.operands] == expected
        assert checked.pages[1].Resources.XObject.Im.read_bytes() == pixel
    inline_pixels = [zlib.decompress(op.data) for op in doc.pages[0].operations if isinstance(op, pyropdf.InlineImage)]
    assert inline_pixels == [bytes([0, 0, 0, 127]), bytes([127, 127, 127, 0]), bytes([0, 0, 0, 127])]


def test_fill_and_stroke_have_separate_sources_for_active_curves() -> None:
    resources = f"/ExtGState << /Z << /BG2 {_curve(0, 0)} /UCR2 {_curve(0, 0)} >> >>"
    source = pages_pdf(".5 .5 .5 rg .25 .5 .75 RG /Z gs 0 0 1 1 re B", resources=resources)
    doc = pyropdf.open(source)
    doc.recolor("cmyk")
    assert colors(doc.pages[0].operations)[-2:] == [
        ("k", approx((0.5, 0.5, 0.5, 0))),
        ("K", approx((0.75, 0.5, 0.25, 0))),
    ]


def test_out_of_bounds_curve_preserves_images_and_reports_each_page() -> None:
    pixel = bytes([128] * 3)
    inline = "BI /W 1 /H 1 /BPC 8 /CS /RGB ID " + pixel.decode("latin-1") + "\nEI"
    content = f"/Huge gs .5 .5 .5 rg /Im Do {inline}"
    resources = "/ExtGState << /Huge << /BG2 20 0 R >> >> /XObject << /Im 21 0 R >>"
    extra = {
        20: stream(b"\0" * 4100, "/FunctionType 0 /Domain [0 1] /Range [0 1] /Size [4100] /BitsPerSample 8"),
        21: stream(
            pixel, "/Type /XObject /Subtype /Image /Width 1 /Height 1 /BitsPerComponent 8 /ColorSpace /DeviceRGB"
        ),
    }
    source = pages_pdf(content, content, resources=resources, extra=extra)
    doc = pyropdf.open(source)
    report = doc.recolor("cmyk")
    assert [(u.page, u.kind) for u in report.unconverted] == [
        (page, "conversion") for page in range(2) for _ in range(3)
    ]
    assert report.images == 0
    assert [page.operations.to_bytes() for page in doc.pages] == [
        page.operations.to_bytes() for page in pyropdf.open(source).pages
    ]


def test_sampled_curve_charges_the_shared_read_budget_before_any_write() -> None:
    sampled = stream(bytes(range(256)) * 16, "/FunctionType 0 /Domain [0 1] /Range [0 1] /Size [4096] /BitsPerSample 8")
    source = pages_pdf(
        ".5 .5 .5 rg", "/G gs .5 .5 .5 rg", resources="/ExtGState << /G << /BG2 20 0 R >> >>", extra={20: sampled}
    )
    with pyropdf.open(source, limits=pyropdf.DocumentLimits(max_memory_size=25_000)) as limited:
        before = [page.operations.to_bytes() for page in limited.pages]
        with pytest.raises(pyropdf.LimitError) as refused:
            limited.recolor("cmyk")
        assert refused.value.code == "memory_size"
        assert [page.operations.to_bytes() for page in limited.pages] == before
    with pyropdf.open(source, limits=pyropdf.DocumentLimits(max_memory_size=150_000)) as enough:
        assert enough.recolor("cmyk").operations == 2


def test_current_function_edit_and_partial_gs_are_both_observed() -> None:
    resources = "/ExtGState << /G << /BG2 20 0 R >> /U << /UCR2 21 0 R >> >>"
    source = pages_pdf("/G gs /U gs .5 .5 .5 rg", resources=resources, extra={20: _curve(0, 0), 21: _curve(0, 0)})
    doc = pyropdf.open(source)
    doc.objects[20]["C1"] = [1]
    assert not doc.recolor("cmyk").unconverted
    assert colors(doc.pages[0].operations) == [("k", approx((0.5, 0.5, 0.5, 0.5)))]


def test_a_password_unlocked_sampled_black_curve_uses_its_plaintext_values() -> None:
    sampled = stream(b"\x00\xff", "/FunctionType 0 /Domain [0 1] /Range [0 1] /Size [2] /BitsPerSample 8")
    source = pages_pdf("/G gs .5 .5 .5 rg", resources="/ExtGState << /G << /BG2 20 0 R >> >>", extra={20: sampled})
    encrypted = io.BytesIO()
    with pikepdf.Pdf.open(io.BytesIO(source)) as plain:
        plain.save(encrypted, compress_streams=False, encryption=pikepdf.Encryption(owner="owner", user="reader", R=6))
    doc = pyropdf.open(encrypted.getvalue(), password="owner")  # noqa: S106 - generated test fixture.
    assert not doc.recolor("cmyk").unconverted
    assert colors(doc.pages[0].operations) == [("k", approx((0, 0, 0, 0.5)))]


def test_appearance_uses_its_own_resources_and_initial_graphics_state() -> None:
    local = f"/ExtGState << /G << /BG2 {_curve(0, 0)} /UCR2 {_curve(0, 0)} >> >>"
    appear = stream(
        ".5 .5 .5 rg /G gs 0 0 1 1 re f",
        f"/Type /XObject /Subtype /Form /BBox [0 0 1 1] /Resources << {local} >>",
    )
    annotation = "<< /Type /Annot /Subtype /Square /Rect [0 0 1 1] /AP << /N 21 0 R >> >>"
    source = pages_pdf(
        "/G gs",
        resources="/ExtGState << /G << /BG2 /Unknown >> >>",
        page="/Annots [20 0 R]",
        extra={20: annotation, 21: appear},
    )
    doc = pyropdf.open(source)
    assert not doc.recolor("cmyk").unconverted
    with pikepdf.Pdf.open(io.BytesIO(doc.to_bytes())) as reopened:
        painted = pikepdf.parse_content_stream(reopened.pages[0].Annots[0].AP.N)
        assert [[float(n) for n in op.operands] for op in painted if str(op.operator) == "k"] == [
            [0, 0, 0, 0.5],
            [0.5, 0.5, 0.5, 0],
        ]
