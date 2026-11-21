"""Resource edits, source operands and independent colour samples (ISO 32000-2 §8.6)."""

from __future__ import annotations

import io
import shutil
import subprocess
from typing import TYPE_CHECKING, Any

import pikepdf
import pytest
from PIL import Image
from test_pdfa_corpus import GENERATOR
from test_recolor import pages_pdf

import pyropdf
from pyropdf import Name, Operation, typed

if TYPE_CHECKING:
    from pathlib import Path

_WHITE = [0.9505, 1, 1.089]
_MATRIX = [0.4124, 0.2126, 0.0193, 0.3576, 0.7152, 0.1192, 0.1805, 0.0722, 0.9505]
_PROFILE = GENERATOR["icc"]()
_LINEAR_PROFILE = _PROFILE.replace(b"curv\0\0\0\0\0\0\0\1\x02\x33", b"curv\0\0\0\0\0\0\0\1\x01\x00")
_TINT = {"FunctionType": 2, "Domain": [0, 1], "C0": [1, 1, 1], "C1": [1, 0, 0], "N": 1}
_IDENTITY = {"FunctionType": 2, "Domain": [0, 1], "C0": [0], "C1": [1], "N": 1}
_LOOKUP = bytes.fromhex("102030 cc3366 aa4488")
_EDITED_LOOKUP = bytes.fromhex("102030 28c814 aa4488")
_UNEDITED = b".25 g 0 0 100 100 re f"


def _poppler(data: bytes, tmp_path: Path, xy: tuple[int, int] = (20, 80)) -> tuple[int, ...]:
    binary = shutil.which("pdftoppm")
    if binary is None:
        pytest.skip("Poppler is the independent calibrated-colour raster oracle")
    pdf, output = tmp_path / "space.pdf", tmp_path / "space"
    pdf.write_bytes(data)
    subprocess.run(  # noqa: S603 - fixed program and switches, test-owned PDF.
        [binary, "-f", "1", "-l", "1", "-r", "72", "-singlefile", "-png", str(pdf), str(output)],
        check=True,
        capture_output=True,
        timeout=15,
    )
    with Image.open(output.with_suffix(".png")) as image:
        pixel = image.convert("RGB").getpixel(xy)
        assert isinstance(pixel, tuple)
        return pixel


def _start() -> pyropdf.Document:
    source = pages_pdf("", _UNEDITED.decode(), resources="/ColorSpace << /Shared /DeviceGray >>")
    doc = pyropdf.open(source)
    doc.pages[0].obj["Resources"] = {
        "ColorSpace": {"Other": Name("DeviceGray")},
        "Properties": {"Unchanged": {"Tag": 17}},
    }
    return doc


def _spaces(doc: pyropdf.Document) -> typed.ColorSpaceMap:
    table = typed.PageObject(doc.pages[0].obj).Resources.ColorSpace
    assert table is not None
    return table


def _paint(doc: pyropdf.Document, values: tuple[float, ...]) -> None:
    doc.pages[0].operations = [
        Operation("cs", (Name("Paint"),)),
        Operation("scn", values),
        Operation("re", (10, 10, 50, 50)),
        Operation("f"),
    ]


def _resource(doc: pyropdf.Document, family: str) -> tuple[list[object], tuple[float, ...]]:
    if family == "CalGray":
        return [Name(family), {"WhitePoint": _WHITE, "Gamma": 1}], (0.5,)
    if family == "CalRGB":
        return [Name(family), {"WhitePoint": _WHITE, "Matrix": _MATRIX, "Gamma": [1, 1, 1]}], (0.25, 0.5, 0.75)
    if family == "Lab":
        return [Name(family), {"WhitePoint": _WHITE, "Range": [-100, 100, -100, 100]}], (55, 55, -40)
    if family == "ICCBased":
        profile = doc.objects.add_stream(_PROFILE, {"N": 3, "Alternate": Name("DeviceRGB")})
        return [Name(family), doc.objects[profile]], (0.25, 0.5, 0.75)
    if family == "Indexed":
        return [Name(family), Name("DeviceRGB"), 2, _LOOKUP], (1,)
    if family == "Separation":
        return [Name(family), Name("Varnish"), Name("DeviceRGB"), _TINT], (0.75,)
    raise ValueError(family)


def _edit(space: typed.ArrayView, family: str) -> None:
    if family == "CalGray":
        space[1].Gamma = 2.2
    elif family == "CalRGB":
        for i in range(3):
            space[1].Gamma[i] = 2.2
    elif family == "Lab":
        for i, value in enumerate((-10, 10, -10, 10)):
            space[1].Range[i] = value
    elif family == "ICCBased":
        space[1].obj.write(_LINEAR_PROFILE)
    elif family == "Indexed":
        space[3] = _EDITED_LOOKUP
    else:
        space[3].C1[0] = 0
        space[3].C1[2] = 1


def _independent(space: Any, family: str, *, edited: bool) -> None:  # noqa: ANN401 - independent pikepdf object.
    assert str(space[0]) == "/" + family
    if family in ("CalGray", "CalRGB", "Lab"):
        assert list(map(float, space[1].WhitePoint)) == pytest.approx(_WHITE)
    if family == "CalGray":
        assert float(space[1].Gamma) == pytest.approx(2.2 if edited else 1)
    elif family == "CalRGB":
        assert list(map(float, space[1].Matrix)) == pytest.approx(_MATRIX)
        assert list(map(float, space[1].Gamma)) == pytest.approx([2.2 if edited else 1] * 3)
    elif family == "Lab":
        assert list(space[1].Range) == ([-10, 10, -10, 10] if edited else [-100, 100, -100, 100])
    elif family == "ICCBased":
        assert (space[1].N, str(space[1].Alternate)) == (3, "/DeviceRGB")
        assert space[1].read_bytes() == (_LINEAR_PROFILE if edited else _PROFILE)
    elif family == "Indexed":
        assert space[2] == 2
        assert bytes(space[3]) == (_EDITED_LOOKUP if edited else _LOOKUP)
    else:
        assert str(space[1]) == "/Varnish"
        assert list(space[3].C1) == ([0, 0, 1] if edited else [1, 0, 0])


def _malformed_member(space: typed.ArrayView, family: str) -> None:
    if family == "CalGray":
        space[1].Gamma = b"not a number"
    elif family == "CalRGB":
        space[1].Gamma[1] = b"not a number"
    elif family == "Lab":
        space[1].Range[0] = b"not a number"
    elif family == "ICCBased":
        space[1].N = b"not an integer"
    elif family == "Indexed":
        space[2] = b"not an integer"
    else:
        space[3].N = b"not an exponent"


def _round_trip(family: str, tmp_path: Path, *, incremental: bool) -> None:
    doc = _start()
    table = _spaces(doc)
    specification, values = _resource(doc, family)
    table["Paint"] = doc.objects[doc.objects.add(specification)]
    _paint(doc, values)
    reference = []
    for edited in (False, True):
        if edited:
            _edit(_spaces(doc)["Paint"], family)
            if family == "Lab":
                # Paint inside the narrowed ranges. Both boundary components are valid.
                values = (55, 10, -10)
                _paint(doc, values)
        saved = doc.to_bytes(incremental=incremental)
        doc = pyropdf.open(saved)
        view = _spaces(doc).interpret("Paint")
        assert type(view).__name__ == f"{family}ColorSpace"
        with pikepdf.Pdf.open(io.BytesIO(saved)) as other:
            page, untouched = other.pages
            _independent(page.Resources.ColorSpace.Paint, family, edited=edited)
            assert str(page.Resources.ColorSpace.Other) == "/DeviceGray"
            assert page.Resources.Properties.Unchanged.Tag == 17
            assert untouched.Contents.read_bytes() == _UNEDITED
            assert list(untouched.Resources.ColorSpace.keys()) == ["/Shared"]
            operations = pikepdf.parse_content_stream(page)
            assert str(operations[0].operands[0]) == "/Paint"
            assert tuple(map(float, operations[1].operands)) == values
        native = doc.pages[0].pixmap(dpi=72).pixel(20, 80)
        independent = _poppler(saved, tmp_path)
        tolerance = {"CalGray": 12, "CalRGB": 3, "Lab": 7, "ICCBased": 3}.get(family, 0)
        assert native == pytest.approx(independent, abs=tolerance)
        reference.append(independent)
    assert max(abs(a - b) for a, b in zip(*reference, strict=True)) > 10
    # The generated typed view rejects an operand of the wrong PDF type atomically.
    with pytest.raises(TypeError):
        _malformed_member(_spaces(doc)["Paint"], family)
    with pikepdf.Pdf.open(io.BytesIO(doc.to_bytes())) as reader:
        _independent(reader.pages[0].Resources.ColorSpace.Paint, family, edited=True)


@pytest.mark.spec("calgray-color-space", "create", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_author_and_edit_calgray_gamma(tmp_path: Path, *, incremental: bool) -> None:
    _round_trip("CalGray", tmp_path, incremental=incremental)


@pytest.mark.spec("calrgb-color-space", "create", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_author_and_edit_calrgb_matrix_and_gamma(tmp_path: Path, *, incremental: bool) -> None:
    _round_trip("CalRGB", tmp_path, incremental=incremental)


@pytest.mark.spec("lab-color-space", "create", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_author_and_edit_lab_ranges(tmp_path: Path, *, incremental: bool) -> None:
    _round_trip("Lab", tmp_path, incremental=incremental)


@pytest.mark.spec("icc-based-color-space", "create", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_embed_and_edit_a_real_icc_profile(tmp_path: Path, *, incremental: bool) -> None:
    assert _PROFILE != _LINEAR_PROFILE
    _round_trip("ICCBased", tmp_path, incremental=incremental)


@pytest.mark.spec("indexed-color-space", "create", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_author_and_edit_an_indexed_palette(tmp_path: Path, *, incremental: bool) -> None:
    _round_trip("Indexed", tmp_path, incremental=incremental)


@pytest.mark.spec("separation-color-space", "create", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_author_and_edit_a_separation_tint(tmp_path: Path, *, incremental: bool) -> None:
    _round_trip("Separation", tmp_path, incremental=incremental)


def _device_n(doc: pyropdf.Document) -> list[object]:
    calculator = doc.objects.add_stream(
        b"{ 1 exch sub exch 1 exch sub exch 1 }",
        {"FunctionType": 4, "Domain": [0, 1, 0, 1], "Range": [0, 1] * 3},
    )
    varnish = [Name("Separation"), Name("Varnish"), Name("DeviceRGB"), {**_TINT, "C1": [1, 0, 1]}]
    attributes = {
        "Subtype": Name("NChannel"),
        "Colorants": {"Varnish": varnish},
        "Process": {
            "ColorSpace": Name("DeviceCMYK"),
            "Components": [Name(v) for v in ("Cyan", "Magenta", "Yellow", "Black")],
        },
        "MixingHints": {
            "Solidities": {"Default": 0.5, "Varnish": 0.75},
            "PrintingOrder": [Name("Cyan"), Name("Varnish")],
            "DotGain": {"Default": _IDENTITY, "Varnish": _IDENTITY},
        },
    }
    return [Name("DeviceN"), [Name("Cyan"), Name("Varnish")], Name("DeviceRGB"), doc.objects[calculator], attributes]


@pytest.mark.spec("devicen-color-space", "read", "interpret", "create", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_nchannel_process_spot_and_mixing_hints(tmp_path: Path, *, incremental: bool) -> None:
    doc = _start()
    _spaces(doc)["Paint"] = doc.objects[doc.objects.add(_device_n(doc))]
    _paint(doc, (0.25, 0.75))
    for edited in (False, True):
        if edited:
            hints = _spaces(doc)["Paint"][4].MixingHints
            hints.Solidities["Varnish"] = 0.25
            hints.PrintingOrder[0] = "Varnish"
            hints.PrintingOrder[1] = "Cyan"
            hints.DotGain["Varnish"].N = 2
        saved = doc.to_bytes(incremental=incremental)
        doc = pyropdf.open(saved)
        space = _spaces(doc).interpret("Paint")
        assert isinstance(space, typed.DeviceNColorSpace)
        assert tuple(space[1].obj) == ("Cyan", "Varnish")
        attributes = space[4]
        assert isinstance(attributes, typed.DeviceNDict)
        assert attributes.Subtype == "NChannel"
        assert isinstance(attributes.Colorants["Varnish"], typed.SeparationColorSpace)
        assert tuple(attributes.Process.Components.obj) == ("Cyan", "Magenta", "Yellow", "Black")
        hints = attributes.MixingHints
        assert isinstance(hints, typed.DeviceNMixingHints)
        assert hints.Solidities["Varnish"] == pytest.approx(0.25 if edited else 0.75)
        assert tuple(hints.PrintingOrder.obj) == (("Varnish", "Cyan") if edited else ("Cyan", "Varnish"))
        assert (2 if edited else 1) == hints.DotGain["Varnish"].N
        with pikepdf.Pdf.open(io.BytesIO(saved)) as independent:
            original = independent.pages[0].Resources.ColorSpace.Paint
            assert original[3].read_bytes() == b"{ 1 exch sub exch 1 exch sub exch 1 }"
            checked = original[4].MixingHints
            assert float(checked.Solidities.Varnish) == pytest.approx(0.25 if edited else 0.75)
            assert list(map(str, checked.PrintingOrder)) == (["/Varnish", "/Cyan"] if edited else ["/Cyan", "/Varnish"])
            assert (2 if edited else 1) == checked.DotGain.Varnish.N
            assert checked.DotGain.Default.N == 1
            assert independent.pages[1].Contents.read_bytes() == _UNEDITED
        # The source two-component calculator maps (c,v) to (1-c,1-v,1).
        assert doc.pages[0].pixmap(dpi=72).pixel(20, 80) == pytest.approx((191, 64, 255), abs=1)
        assert _poppler(saved, tmp_path) == pytest.approx((191, 64, 255), abs=1)
    before = tuple(hints.PrintingOrder.obj)
    with pytest.raises(TypeError):
        hints.PrintingOrder[0] = 12
    assert tuple(hints.PrintingOrder.obj) == before


def _operator_case(doc: pyropdf.Document, case: str) -> tuple[str, tuple[object, ...], tuple[object, ...]]:
    rgb = ((1, 0, 0), (0, 0, 1))
    if case == "gray":
        return "g", (0.1,), (0.8,)
    if case == "rgb":
        return "rg", *rgb
    if case == "cmyk":
        return "k", (0, 0, 0, 0), (0, 0, 0, 1)
    table = _spaces(doc)
    if case == "generic":
        table["Paint"] = "DeviceRGB"
        return "sc", *rgb
    if case == "spot":
        space, _ = _resource(doc, "Separation")
        table["Paint"] = doc.objects[doc.objects.add(space)]
        return "scn", (0.2,), (0.9,)
    pattern = doc.objects.add_stream(
        b"0 0 2 2 re f",
        {
            "Type": Name("Pattern"),
            "PatternType": 1,
            "PaintType": 2,
            "TilingType": 1,
            "BBox": [0, 0, 2, 2],
            "XStep": 2,
            "YStep": 2,
            "Resources": {},
        },
    )
    doc.pages[0].obj["Resources"]["Pattern"] = {"Tile": doc.objects[pattern]}
    table["Paint"] = doc.objects[doc.objects.add([Name("Pattern"), Name("DeviceRGB")])]
    return "scn", (*rgb[0], Name("Tile")), (*rgb[1], Name("Tile"))


def _source_operand(value: object) -> str | float:
    if isinstance(value, (Name, pikepdf.Name)):
        return str(value).removeprefix("/")
    return float(value)  # ty: ignore[invalid-argument-type] - a PDF colour component.


@pytest.mark.spec("colour-operators", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.parametrize("case", ["gray", "rgb", "cmyk", "generic", "spot", "pattern"])
@pytest.mark.parametrize("incremental", [False, True])
def test_stroking_and_nonstroking_operators_retain_their_source_operands(
    case: str, tmp_path: Path, *, incremental: bool
) -> None:
    doc = _start()
    operator, first, second = _operator_case(doc, case)
    samples = []
    for fill, stroke in ((first, second), (second, first)):
        source = [Operation("q")]
        if operator.startswith("sc"):
            source.extend([Operation("cs", (Name("Paint"),)), Operation("CS", (Name("Paint"),))])
        source.extend(
            [
                Operation(operator, fill),
                Operation("re", (10, 10, 30, 30)),
                Operation("f"),
                Operation(operator.upper(), stroke),
                Operation("w", (6,)),
                Operation("m", (60, 20)),
                Operation("l", (85, 20)),
                Operation("S"),
                Operation("Q"),
            ]
        )
        doc.pages[0].operations = source
        saved = doc.to_bytes(incremental=incremental)
        doc = pyropdf.open(saved)
        assert [(op.operator, op.operands) for op in doc.pages[0].operations] == [
            (op.operator, op.operands) for op in source
        ]
        with pikepdf.Pdf.open(io.BytesIO(saved)) as reader:
            parsed = pikepdf.parse_content_stream(reader.pages[0])
            assert [str(op.operator) for op in parsed] == [op.operator for op in source]
            for decoded, written in zip(parsed, source, strict=True):
                assert tuple(map(_source_operand, decoded.operands)) == tuple(map(_source_operand, written.operands))
            assert reader.pages[1].Contents.read_bytes() == _UNEDITED
        pixmap = doc.pages[0].pixmap(dpi=72)
        pair = tuple(pixmap.pixel(x, 80) for x in (20, 70))
        for x, pixel in zip((20, 70), pair, strict=True):
            assert pixel == pytest.approx(_poppler(saved, tmp_path, (x, 80)), abs=3)
        samples.append(pair)
    assert samples[0][0] != samples[0][1]
    assert samples[0] == tuple(reversed(samples[1]))
    before = doc.pages[0].operations.to_bytes()
    with pytest.raises(ValueError, match="finite"):
        doc.pages[0].operations = [Operation("rg", (float("nan"), 0, 0))]
    assert doc.pages[0].operations.to_bytes() == before


def test_devicen_process_and_spot_plates_preserve_the_original_tints(tmp_path: Path) -> None:
    gs = shutil.which("gs")
    if gs is None:
        pytest.skip("Ghostscript tiffsep is the independent spot-plate oracle")
    doc = _start()
    _spaces(doc)["Paint"] = doc.objects[doc.objects.add(_device_n(doc))]
    _paint(doc, (0.25, 0.75))
    for solidity in (0.75, 0.25):
        _spaces(doc)["Paint"][4].MixingHints.Solidities["Varnish"] = solidity
        pdf, composite = tmp_path / "inks.pdf", tmp_path / "ink.tif"
        pdf.write_bytes(doc.to_bytes())
        subprocess.run(  # noqa: S603 - fixed device and test-owned paths.
            [
                gs,
                "-q",
                "-dSAFER",
                "-dBATCH",
                "-dNOPAUSE",
                "-dFirstPage=1",
                "-dLastPage=1",
                "-r72",
                "-sDEVICE=tiffsep",
                f"-sOutputFile={composite}",
                str(pdf),
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        for ink, expected in (("Cyan", 192), ("Varnish", 64), ("Magenta", 255), ("Yellow", 255), ("Black", 255)):
            with Image.open(tmp_path / f"ink({ink}).tif") as plate:
                assert plate.getpixel((20, 80)) == pytest.approx(expected, abs=1)


def test_a_nonstandard_lab_white_point_survives_for_independent_colour_management(tmp_path: Path) -> None:
    doc = _start()
    resource, values = _resource(doc, "Lab")
    _spaces(doc)["Paint"] = doc.objects[doc.objects.add(resource)]
    _paint(doc, values)
    base = _poppler(doc.to_bytes(), tmp_path)
    white = _spaces(doc)["Paint"][1].WhitePoint
    white[0], white[2] = 0.75, 1.4
    changed = doc.to_bytes()
    with pikepdf.Pdf.open(io.BytesIO(changed)) as saved:
        assert list(map(float, saved.pages[0].Resources.ColorSpace.Paint[1].WhitePoint)) == [0.75, 1.0, 1.4]
    alternate = _poppler(changed, tmp_path)
    # The independent display transform responds to the changed white point.
    assert base[0] - alternate[0] > 15
    assert alternate[1] - base[1] > 10
