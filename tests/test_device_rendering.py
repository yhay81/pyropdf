"""Output-device resources, their edits, and independent process and spot colour samples."""

from __future__ import annotations

import io
from typing import Any

import pikepdf
import pytest
from test_content import build_raw_pdf

import pyropdf
from pyropdf import Name, typed

_D65 = [0.9505, 1, 1.089]
_FN = {"FunctionType": 2, "Domain": [0, 1], "C0": [0], "C1": [1], "N": 1}
_SCREEN = {"HalftoneType": 1, "Frequency": 30, "Angle": 45, "SpotFunction": Name("Round")}
_TINT = {**_FN, "C0": [0, 0, 0, 0], "C1": [0, 1, 0, 0]}
_PAINT = b"/Press gs 0.5 g 0 0 16 16 re f"


def _sample(
    resources: str = "/ExtGState << /Press 6 0 R >> /ColorSpace << >>",
    paint: bytes = _PAINT,
    state: str = "/LW 4",
) -> bytes:
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 32 16] /Resources 4 0 R /Contents 5 0 R >>",
            4: f"<< {resources} >>",
            5: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(paint), paint),
            6: f"<< /Type /ExtGState {state} >>",
        }
    )


def _pdf_value(doc: pyropdf.Document, value: object) -> object:
    """Construct linked dictionaries/arrays as PDF objects and retain their identity."""
    return doc.objects[doc.objects.add(value)] if isinstance(value, (dict, list)) else value


def _independent(value: Any, expected: Any) -> None:  # noqa: ANN401 - qpdf objects span all PDF value types.
    """Assert the independently parsed value, descending only through the fixed test expectation."""
    if isinstance(expected, dict):
        assert set(value.keys()) == {"/" + key for key in expected}
        for key, child in expected.items():
            _independent(value["/" + key], child)
    elif isinstance(expected, list):
        assert len(value) == len(expected)
        for index, child in enumerate(expected):
            _independent(value[index], child)
    elif isinstance(expected, Name):
        assert value == pikepdf.Name("/" + expected)
    elif isinstance(expected, bool):
        assert value is expected
    else:
        assert float(value) == pytest.approx(expected)


def _resource_view(doc: pyropdf.Document, resource: str) -> typed.DictionaryView:
    container = typed.PageObject(doc.pages[0].obj).Resources[resource]
    return container["Press"] if resource == "ExtGState" else container


def _check_typed(reread: typed.DictionaryView, values: dict[str, Any]) -> None:
    for key, expected in values.items():
        result = reread[key]
        if isinstance(expected, dict):
            assert isinstance(result, typed.DictionaryView)
            for member in expected:
                assert result[member] is not None
        elif isinstance(expected, list):
            assert isinstance(result, typed.ArrayView)
            assert result[0] == expected[0]
        else:
            assert result == expected


def _round_trip(
    resource: str, initial: dict[str, Any], edited: dict[str, Any], invalid: tuple[str, object], *, incremental: bool
) -> None:
    data = _sample()
    for values in (initial, edited):
        doc = pyropdf.open(data)
        view = _resource_view(doc, resource)
        for key, value in values.items():
            view[key] = _pdf_value(doc, value)
        with pytest.raises((TypeError, ValueError)):
            view[invalid[0]] = invalid[1]
        saved = doc.to_bytes(incremental=incremental)
        if incremental:
            assert saved.startswith(data)
        with pikepdf.Pdf.open(io.BytesIO(saved)) as other:
            page_dict = other.pages[0]
            independent = page_dict.Resources["/" + resource]
            if resource == "ExtGState":
                independent = independent.Press
            for key, expected in values.items():
                _independent(independent["/" + key], expected)
            assert page_dict.Resources.ExtGState.Press.LW == 4
            assert page_dict.Contents.read_bytes() == _PAINT
        _check_typed(_resource_view(pyropdf.open(saved), resource), values)
        data = saved


@pytest.mark.spec("overprint", "read", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_stroking_and_nonstroking_overprint_and_both_modes(*, incremental: bool) -> None:
    _round_trip(
        "ExtGState",
        {"OP": True, "op": False, "OPM": 0},
        {"OP": False, "op": True, "OPM": 1},
        ("op", 1),
        incremental=incremental,
    )


@pytest.mark.spec("rendering-intents", "read", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize(
    ("first", "second"), [("AbsoluteColorimetric", "RelativeColorimetric"), ("Perceptual", "Saturation")]
)
def test_device_intent_selectors(first: str, second: str, *, incremental: bool) -> None:
    _round_trip(
        "ExtGState", {"RI": Name(first)}, {"RI": Name(second)}, ("RI", "UnknownIntent"), incremental=incremental
    )


@pytest.mark.spec("device-color-conversion", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_black_generation_and_undercolor_removal_functions(*, incremental: bool) -> None:
    _round_trip(
        "ExtGState",
        {"BG2": _FN, "UCR2": {**_FN, "N": 2}},
        {"BG2": Name("Default"), "UCR2": _FN},
        ("BG2", 4),
        incremental=incremental,
    )


@pytest.mark.spec("transfer-functions", "read", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_transfer_function_and_device_default(*, incremental: bool) -> None:
    _round_trip(
        "ExtGState",
        {"TR2": {**_FN, "C0": [1], "C1": [0]}},
        {"TR2": Name("Default")},
        ("TR2", 4),
        incremental=incremental,
    )


@pytest.mark.spec("halftones", "read", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_halftone_spot_function_frequency_and_angle(*, incremental: bool) -> None:
    _round_trip(
        "ExtGState",
        {"HT": _SCREEN},
        {"HT": {**_SCREEN, "Frequency": 60, "Angle": 0, "SpotFunction": Name("Line")}},
        ("HT", 8),
        incremental=incremental,
    )


@pytest.mark.spec("scan-conversion", "read", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_scan_conversion_controls_and_endpoints(*, incremental: bool) -> None:
    _round_trip(
        "ExtGState",
        {"FL": 0.5, "SM": 0, "SA": True},
        {"FL": 100, "SM": 1, "SA": False},
        ("SA", 1),
        incremental=incremental,
    )


@pytest.mark.spec("color-conversion", "read", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_calibrated_source_for_device_colour_conversion(*, incremental: bool) -> None:
    colour = [Name("CalRGB"), {"WhitePoint": _D65, "Gamma": [1, 1, 1]}]
    changed = [Name("CalRGB"), {"WhitePoint": _D65, "Gamma": [2, 2, 2]}]
    _round_trip(
        "ColorSpace", {"DefaultRGB": colour}, {"DefaultRGB": changed}, ("DefaultRGB", 1), incremental=incremental
    )


@pytest.mark.spec("separation-rendering", "read", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_named_plate_its_alternate_and_no_ink(*, incremental: bool) -> None:
    spot = [Name("Separation"), Name("Varnish"), Name("DeviceCMYK"), _TINT]
    knockout = [Name("Separation"), Name("None"), Name("DeviceCMYK"), _TINT]
    _round_trip("ColorSpace", {"Ink": spot}, {"Ink": knockout}, ("Ink", b"wrong"), incremental=incremental)


@pytest.mark.spec("halftones", "read", "create", "edit", "preserve")
@pytest.mark.parametrize(
    ("kind", "size", "thresholds"),
    [
        (6, {"Width": 2, "Height": 1}, b"\x10\xe0"),
        (10, {"Xsquare": 1, "Ysquare": 1}, b"\x10\xe0"),
        (16, {"Width": 2, "Height": 1}, b"\x10\x00\xe0\x00"),
    ],
)
@pytest.mark.parametrize("incremental", [False, True])
def test_threshold_streams_and_per_colourant_halftone_selection(
    kind: int, size: dict[str, int], thresholds: bytes, *, incremental: bool
) -> None:
    doc = pyropdf.open(_sample())
    state = _resource_view(doc, "ExtGState")
    stream_id = doc.objects.add_stream(thresholds, {"HalftoneType": kind, **size})
    screen = doc.objects[stream_id]
    component_id = doc.objects.add({"HalftoneType": 5, "Default": _SCREEN, "Varnish": screen})
    state["HT"] = doc.objects[component_id]
    saved = doc.to_bytes(incremental=incremental)
    for expected in (thresholds, thresholds[::-1]):
        reopened = pyropdf.open(saved)
        state = typed.GraphicsStateParameter(_resource_view(reopened, "ExtGState").obj)
        selection = state.HT
        assert isinstance(selection, typed.HalftoneType5)
        assert isinstance(state.interpret("HT"), typed.HalftoneType5)
        assert selection.HalftoneType == 5
        assert selection.Default.HalftoneType == 1
        selected = selection.obj["Varnish"]
        assert isinstance(selected, pyropdf.Stream)
        assert selected.read() == expected
        with pikepdf.Pdf.open(io.BytesIO(saved)) as other:
            choice = other.pages[0].Resources.ExtGState.Press.HT
            assert choice.Default.SpotFunction == pikepdf.Name.Round
            assert choice.Varnish.HalftoneType == kind
            assert choice.Varnish.read_bytes() == expected
            assert other.pages[0].Contents.read_bytes() == _PAINT
        if expected == thresholds:
            selected.write(thresholds[::-1])
            saved = reopened.to_bytes(incremental=incremental)
