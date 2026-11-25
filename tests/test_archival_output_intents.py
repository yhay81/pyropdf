"""Conditional archival colour characterization across resource scopes."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

import pyropdf
from pyropdf import Name

_PDFA = runpy.run_path(str(Path(__file__).parent / "assets/pdfa/generate.py"))
_PAINT = b"0 0 60 60 re f"
_CAL = [Name("CalRGB"), {"WhitePoint": [0.9505, 1, 1.089]}]


def blank(flavour: str) -> pyropdf.Document:
    version, claim, _ = _PDFA["FLAVOURS"][f"pdfa-{flavour}.pdf"]
    doc = pyropdf.Document(version=version)
    doc.pages.new((100, 100))
    doc.xmp = _PDFA["xmp"](claim).decode()
    return doc


def _put(doc: pyropdf.Document, content: bytes, **resources: object) -> None:
    page = doc.pages[0]
    page.obj["Resources"] = resources
    page.obj["Contents"] = doc.objects[doc.objects.add_stream(content)]


def _intent(doc: pyropdf.Document) -> list[dict[str, object]]:
    icc = doc.objects[doc.objects.add_stream(_PDFA["icc"](), {"N": 3})]
    return [
        {
            "Type": Name("OutputIntent"),
            "S": Name("GTS_PDFA1"),
            "OutputConditionIdentifier": "sRGB",
            "DestOutputProfile": icc,
        }
    ]


def _blending(doc: pyropdf.Document, case: str) -> object:
    if case == "cal-group":
        return _CAL
    if case == "icc-group":
        return [Name("ICCBased"), doc.objects[doc.objects.add_stream(_PDFA["icc"](), {"N": 3})]]
    return Name("DeviceRGB")


def control(flavour: str, case: str) -> pyropdf.Document:
    doc = blank(flavour)
    rgb = b"1 0 0 rg " + _PAINT
    simple = {
        "rgb": rgb,
        "selected": b"/DeviceRGB cs",
        "initial": _PAINT,
        "clipped": b"1 0 0 rg 0 0 10 10 re W n",
        "clip-only": b"0 0 10 10 re W n",
        "cmyk": b"0 .2 1 .1 k " + _PAINT,
        "inline": b"BI /W 1 /H 1 /BPC 8 /CS /RGB ID \xff\x00\x00 EI",
    }
    if case in simple:
        _put(doc, simple[case])
    elif case == "unused":
        _put(doc, b"", ColorSpace={"Unused": Name("DeviceRGB")})
    elif case in ("image", "unused-image"):
        pixels = doc.objects[
            doc.objects.add_stream(
                b"\xff\x00\x00",
                {
                    "Type": Name("XObject"),
                    "Subtype": Name("Image"),
                    "Width": 1,
                    "Height": 1,
                    "ColorSpace": Name("DeviceRGB"),
                    "BitsPerComponent": 8,
                },
            )
        ]
        _put(doc, b"/Im Do" if case == "image" else b"", XObject={"Im": pixels})
    elif case == "restored":
        _put(doc, b"q 1 0 0 rg Q /Cal cs .2 .4 .8 sc " + _PAINT, ColorSpace={"Cal": _CAL})
    elif case == "default":
        _put(doc, rgb, ColorSpace={"DefaultRGB": _CAL})
    elif case == "page-intent":
        _put(doc, rgb)
        doc.pages[0].obj["OutputIntents"] = _intent(doc)
    elif case in ("group-space", "empty-device-group", "cal-group", "icc-group"):
        _put(doc, b"" if case == "empty-device-group" else rgb)
        doc.pages[0].obj["Group"] = {"Type": Name("Group"), "S": Name("Transparency"), "CS": _blending(doc, case)}
    elif case in ("nested", "nested-default"):
        resources = {"ColorSpace": {"DefaultRGB": _CAL}} if case == "nested-default" else {}
        form = doc.objects[
            doc.objects.add_stream(
                rgb,
                {"Type": Name("XObject"), "Subtype": Name("Form"), "BBox": [0, 0, 100, 100], "Resources": resources},
            )
        ]
        _put(doc, b"/F Do", XObject={"F": form})
    return doc


def device_findings(doc: pyropdf.Document) -> list[str]:
    return [d.message for d in doc.check().defects if d.kind == "profile" and d.path == "OutputIntents"]


# The formal runner checks these exact single-rule negatives on the same files.
CONTROLS = (
    ("1b", "unused", None, None),
    ("2b", "restored", "DeviceRGB", "6.2.4.3-2"),
    ("2b", "clipped", "DeviceRGB", "6.2.4.3-2"),
    ("2b", "selected", "DeviceRGB", "6.2.4.3-2"),
    ("2b", "clip-only", None, None),
    ("2b", "initial", "DeviceGray", "6.2.4.3-4"),
    ("2b", "cmyk", "DeviceCMYK", "6.2.4.3-3"),
    ("2b", "image", "DeviceRGB", "6.2.4.3-2"),
    ("2b", "unused-image", None, None),
    ("2b", "inline", "DeviceRGB", "6.2.4.3-2"),
    ("4", "rgb", "DeviceRGB", "6.2.4.3-2"),
    ("4", "default", None, None),
    ("4", "page-intent", None, None),
    ("4", "group-space", "DeviceRGB", "6.2.4.3-2"),
    ("4", "empty-device-group", "DeviceRGB", "6.2.4.3-2"),
    ("4", "cal-group", None, None),
    ("4", "icc-group", None, None),
    ("2b", "nested", "DeviceRGB", "6.2.4.3-2"),
    ("2b", "nested-default", None, None),
)


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize(("flavour", "case", "device"), [(f, c, d) for f, c, d, _ in CONTROLS])
def test_only_used_uncalibrated_device_colours_are_reported(
    flavour: str, case: str, device: str | None, *, incremental: bool
) -> None:
    doc = pyropdf.open(control(flavour, case).to_bytes())
    doc.pages[0].rotation = 90
    for candidate in (doc, pyropdf.open(doc.to_bytes(incremental=incremental))):
        messages = device_findings(candidate)
        assert len(messages) == int(device is not None)
        if device is not None:
            assert device in messages[0]


def test_editing_the_active_forms_defaults_changes_the_current_finding() -> None:
    doc = control("2b", "nested-default")
    assert device_findings(doc) == []
    del doc.pages[0].obj["Resources"]["XObject"]["F"]["Resources"]["ColorSpace"]["DefaultRGB"]
    assert "DeviceRGB" in device_findings(doc)[0]
    doc.catalog["OutputIntents"] = _intent(doc)
    assert device_findings(doc) == []


def test_uninterpreted_space_increases_unchecked_without_a_false_device_finding() -> None:
    doc = blank("2b")
    _put(doc, b"/Pattern cs /Tile scn " + _PAINT)
    unknown = doc.check()
    assert not any(d.path == "OutputIntents" for d in unknown.defects)
    _put(doc, b"", ColorSpace={})
    assert unknown.unchecked > doc.check().unchecked


def test_output_profile_signature_controls_compatibility_even_without_n() -> None:
    doc = control("2b", "cmyk")
    intents = _intent(doc)
    doc.catalog["OutputIntents"] = intents
    del doc.catalog["OutputIntents"][0]["DestOutputProfile"]["N"]
    assert "DeviceCMYK" in device_findings(doc)[0]
    _put(doc, b"1 0 0 rg")
    assert device_findings(doc) == []


def test_cyclic_form_increases_unchecked_without_repeating_its_operations() -> None:
    doc = control("2b", "nested")
    form = doc.pages[0].obj["Resources"]["XObject"]["F"]
    form.write(b"/Loop Do")
    form["Resources"] = {"XObject": {"Loop": form}}
    unknown = doc.check()
    assert not any(d.path == "OutputIntents" for d in unknown.defects)
    del form["Resources"]["XObject"]["Loop"]
    form.write(b"")
    assert unknown.unchecked > doc.check().unchecked


def test_nested_forms_stop_at_the_colour_check_depth_bound() -> None:
    doc = control("2b", "nested")
    current = doc.pages[0].obj["Resources"]["XObject"]["F"]
    for _ in range(32):
        child = doc.objects[
            doc.objects.add_stream(
                b"", {"Type": Name("XObject"), "Subtype": Name("Form"), "BBox": [0, 0, 100, 100], "Resources": {}}
            )
        ]
        current["Resources"] = {"XObject": {"Next": child}}
        current.write(b"/Next Do")
        current = child
    with pytest.raises(pyropdf.LimitError) as caught:
        doc.check()
    assert caught.value.code == "object_depth"
