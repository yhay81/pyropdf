from __future__ import annotations

import ast
import base64
import inspect
import subprocess
import sys
import zlib
from pathlib import Path

import pyropdf
from pyropdf import Dictionary, Name, Operations, Stream
from pyropdf._xobjects import (
    _MAX_FORM_DEPTH,
    _MAX_FORMS,
    _MAX_INLINE_FLATE_BYTES,
    _MAX_INLINE_PIXELS,
    _MAX_RESOURCE_ANCESTORS,
    _STREAM_KEYS,
    _appearance_streams,
    _clone_form_resources,
    _copy_form,
    _inline_data,
    _parse_form,
    _repoint_page,
    _resources,
    _set_appearance,
    _sub,
)

ROOT = Path(__file__).resolve().parents[1]


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
            for alias in node.names:
                imported.add(f"{node.module}.{alias.name}")
    return imported


def test_images_and_recolor_share_xobjects_without_cross_imports() -> None:
    images_imports = _imported_modules(ROOT / "python/pyropdf/_images.py")
    recolor_imports = _imported_modules(ROOT / "python/pyropdf/_recolor.py")

    assert not any(name == "pyropdf._recolor" or name.startswith("pyropdf._recolor.") for name in images_imports)
    assert not any(name == "pyropdf._images" or name.startswith("pyropdf._images.") for name in recolor_imports)
    assert "pyropdf._xobjects" in images_imports
    assert "pyropdf._xobjects" in recolor_imports

    modules = ("pyropdf._xobjects", "pyropdf._images", "pyropdf._recolor")
    script = (
        "import importlib, sys\n"
        "for name in sys.argv[1:]:\n"
        "    for key in [k for k in sys.modules if k == 'pyropdf' or k.startswith('pyropdf.')]:\n"
        "        if key != 'pyropdf._core':\n"
        "            del sys.modules[key]\n"
        "    importlib.import_module(name)\n"
    )
    subprocess.run([sys.executable, "-c", script, *modules], check=True)  # noqa: S603 - fixed interpreter and module names.


def test_xobjects_bounds_and_helper_signatures() -> None:
    assert _MAX_FORM_DEPTH == 16
    assert _MAX_FORMS == 4096
    assert _MAX_INLINE_PIXELS == 1 << 20
    assert _MAX_RESOURCE_ANCESTORS == 64
    assert isinstance(_STREAM_KEYS, frozenset)
    assert set(_STREAM_KEYS) == {"Length", "Filter", "DecodeParms", "DL"}

    for helper in (
        _inline_data,
        _resources,
        _sub,
        _parse_form,
        _clone_form_resources,
        _copy_form,
        _repoint_page,
        _appearance_streams,
        _set_appearance,
    ):
        assert inspect.getdoc(helper)
        assert "return" in helper.__annotations__


def test_inline_data_decodes_supported_filters_and_bounds_flate() -> None:
    payload = b"\x10\x20\x30\xff"
    assert _inline_data(None, payload) == payload
    assert _inline_data(["Fl"], payload) is None

    assert _inline_data(Name("AHx"), b"10 20\n30f>ignored") == b"\x10\x20\x30\xf0"
    assert _inline_data(Name("ASCII85Decode"), base64.a85encode(payload) + b"~>trailing") == payload
    assert _inline_data(Name("FlateDecode"), zlib.compress(payload)) == payload

    chained = zlib.compress(payload).hex().encode("ascii")
    assert _inline_data((Name("ASCIIHexDecode"), Name("Fl")), chained) == payload

    assert _inline_data(Name("LZWDecode"), payload) is None
    assert _inline_data(Name("Fl"), b"not-deflate") is None

    oversized = zlib.compress(b"\x00" * (_MAX_INLINE_FLATE_BYTES + 128))
    decoded = _inline_data(Name("Fl"), oversized)
    assert decoded is not None
    assert len(decoded) == _MAX_INLINE_FLATE_BYTES


def test_resources_and_sub_follow_bounded_ancestors() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((100, 100))

    assert _sub(None, "XObject") is None
    page.obj["Resources"] = {"XObject": [1, 2], "Font": {"F1": {}}}
    own = _resources(page)
    assert isinstance(own, Dictionary)
    assert _sub(own, "XObject") is None
    assert isinstance(_sub(own, "Font"), Dictionary)

    # Walk 63 /Parent hops (64 nodes checked including page.obj) vs 64 hops (65th node).
    del page.obj["Resources"]
    current = page.obj
    for step in range(_MAX_RESOURCE_ANCESTORS - 1):
        parent_num = doc.objects.add({"Marker": step})
        current["Parent"] = doc.objects[parent_num]
        current = doc.objects[parent_num]
    current["Resources"] = {"ExtGState": {"GS0": {}}}
    inherited = _resources(page)
    assert isinstance(inherited, Dictionary)
    assert isinstance(_sub(inherited, "ExtGState"), Dictionary)

    del current["Resources"]
    too_deep = doc.objects.add({"Resources": {"ExtGState": {"GS1": {}}}})
    current["Parent"] = doc.objects[too_deep]
    assert _resources(page) is None


def test_parse_and_copy_form_clone_resources_copy_on_write() -> None:
    doc = pyropdf.Document()
    leaf_a = doc.objects[doc.objects.add_stream(b"0 g", {"Type": Name("XObject"), "Subtype": Name("Form")})]
    leaf_b = doc.objects[doc.objects.add_stream(b"1 g", {"Type": Name("XObject"), "Subtype": Name("Form")})]
    shared_xobjects = doc.objects[doc.objects.add({"Leaf": leaf_a, "Kept": leaf_a})]
    shared_resources = doc.objects[doc.objects.add({"XObject": shared_xobjects, "ProcSet": [Name("PDF")]})]
    form_num = doc.objects.add_stream(
        b"/Leaf Do",
        {
            "Type": Name("XObject"),
            "Subtype": Name("Form"),
            "BBox": [0, 0, 10, 10],
            "DL": 8,
            "Resources": shared_resources,
        },
    )
    form = doc.objects[form_num]
    assert isinstance(form, Stream)

    parsed = _parse_form(form)
    assert isinstance(parsed, Operations)
    assert len(parsed) == 1

    unreadable_num = doc.objects.add_stream(b"not-jpeg", filter=None)
    doc.objects[unreadable_num]["Filter"] = Name("DCTDecode")
    reopened = pyropdf.open(doc.to_bytes())
    assert _parse_form(reopened.objects[unreadable_num]) is None

    cloned = _clone_form_resources(shared_resources, {"Leaf": leaf_b})
    assert list(cloned["ProcSet"]) == [Name("PDF")]  # ty: ignore[invalid-argument-type]

    copied_num = _copy_form(doc, form, b"q /Leaf Do Q", shared_resources, {"Leaf": leaf_b})
    copied = doc.objects[copied_num]
    assert isinstance(copied, Stream)
    assert "DL" not in copied
    assert list(copied["BBox"]) == [0, 0, 10, 10]
    assert copied["Resources"]["XObject"]["Leaf"].xref == leaf_b.xref
    assert copied["Resources"]["XObject"]["Kept"].xref == leaf_a.xref
    assert shared_resources["XObject"]["Leaf"].xref == leaf_a.xref


def test_repoint_page_preserves_shared_indirect_resources() -> None:
    doc = pyropdf.Document()
    first = doc.pages.new((100, 100))
    second = doc.pages.new((100, 100))

    original = doc.objects[doc.objects.add_stream(b"0 g", {"Type": Name("XObject"), "Subtype": Name("Form")})]
    replacement = doc.objects[doc.objects.add_stream(b"1 g", {"Type": Name("XObject"), "Subtype": Name("Form")})]
    shared_xobjects = doc.objects[doc.objects.add({"Fm0": original, "Fm1": original})]
    shared_resources = doc.objects[doc.objects.add({"XObject": shared_xobjects})]
    first.obj["Resources"] = shared_resources
    second.obj["Resources"] = shared_resources

    _repoint_page(first, {"Fm0": replacement})

    assert first.obj["Resources"]["XObject"]["Fm0"].xref == replacement.xref
    assert first.obj["Resources"]["XObject"]["Fm1"].xref == original.xref
    assert second.obj["Resources"]["XObject"]["Fm0"].xref == original.xref


def test_appearance_streams_and_set_appearance_copy_on_write() -> None:
    doc = pyropdf.Document()
    normal = doc.objects[doc.objects.add_stream(b"0 g", {"BBox": [0, 0, 10, 10]})]
    off = doc.objects[doc.objects.add_stream(b"0.5 g", {"BBox": [0, 0, 10, 10]})]
    on = doc.objects[doc.objects.add_stream(b"1 g", {"BBox": [0, 0, 10, 10]})]
    updated = doc.objects[doc.objects.add_stream(b"0.2 g", {"BBox": [0, 0, 10, 10]})]

    shared_states = doc.objects[doc.objects.add({"Off": off, "Yes": on})]
    shared_ap = doc.objects[doc.objects.add({"N": normal, "D": shared_states})]
    first = doc.objects[doc.objects.add({"AP": shared_ap})]
    second = doc.objects[doc.objects.add({"AP": shared_ap})]

    empty = doc.objects[doc.objects.add({})]
    assert _appearance_streams(empty) == []
    assert [(key, state, stream.xref) for key, state, stream in _appearance_streams(first)] == [
        ("N", None, normal.xref),
        ("D", "Off", off.xref),
        ("D", "Yes", on.xref),
    ]
    assert [(key, state, stream.xref) for key, state, stream in _appearance_streams(first, ("N",))] == [
        ("N", None, normal.xref)
    ]

    _set_appearance(first, "D", "Yes", updated)
    assert first["AP"]["D"]["Yes"].xref == updated.xref
    assert first["AP"]["D"]["Off"].xref == off.xref
    assert second["AP"]["D"]["Yes"].xref == on.xref

    _set_appearance(first, "N", None, updated)
    assert first["AP"]["N"].xref == updated.xref
    assert second["AP"]["N"].xref == normal.xref
