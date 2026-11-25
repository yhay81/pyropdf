"""Evaluate authored functions, tiling patterns and all seven shading types in two renderers."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pikepdf
import pytest
from test_canvas import _hayro, _poppler

import pyropdf
from pyropdf import Name, PatternPaint, typed

if TYPE_CHECKING:
    from collections.abc import Callable


def _document() -> pyropdf.Document:
    doc = pyropdf.Document()
    doc.pages.new((64, 64))
    doc.catalog["Version"] = Name("2.0")
    doc.catalog["Unrelated"] = doc.objects[doc.objects.add_stream(b"unchanged\x00\xff", filter=None)]
    return doc


def _xref(view: typed.View) -> int:
    number = view.obj.xref
    assert number is not None
    return number


def _pattern(doc: pyropdf.Document, shading: typed.DictionaryView) -> typed.PatternType2:
    pattern = typed.PatternType2.create(doc, {"PatternType": 2, "Shading": shading.obj})
    with doc.pages[0].draw() as canvas:
        canvas.rect((0, 0, 64, 64), fill=PatternPaint(pattern=pattern.obj))
    return pattern


def _levels(data: bytes, expected: Callable[[float], float]) -> None:
    """Sample analytical positions away from edges in both Poppler and hayro."""
    for render in (_hayro, _poppler):
        stride, pixels = render(data)
        channels = stride // 64
        for x in (1, 7, 19, 35, 53, 62):
            offset = 32 * stride + x * channels
            light = expected((x + 0.5) / 64) * 255
            assert list(pixels[offset : offset + 3]) == pytest.approx([light] * 3, abs=7), (render.__name__, x)


def _saves(doc: pyropdf.Document, base: bytes, expected: Callable[[float], float]) -> None:
    for incremental in (False, True):
        result = doc.to_bytes(incremental=incremental)
        if incremental:
            assert result.startswith(base)
        with pikepdf.Pdf.open(io.BytesIO(result)) as check:
            assert check.Root.Unrelated.read_raw_bytes() == b"unchanged\x00\xff"
            assert len(check.pages[0].Resources.Pattern) == 1
        _levels(result, expected)


def _exponential(doc: pyropdf.Document, first: int = 0, last: int = 1) -> typed.FunctionType2:
    return typed.FunctionType2.create(doc, {"FunctionType": 2, "Domain": [0, 1], "N": 1, "C0": [first], "C1": [last]})


def _function_case(kind: int) -> None:
    doc = _document()
    common = {"FunctionType": kind, "Domain": [0, 1], "Range": [0, 1]}
    if kind == 0:
        view = typed.FunctionType0.create(doc, {**common, "Size": [3], "BitsPerSample": 8}, data=b"\x00\x80\xff")
    elif kind == 2:
        view = _exponential(doc)
    elif kind == 3:
        linear = _exponential(doc)
        view = typed.FunctionType3.create(
            doc, {**common, "Functions": [linear.obj, linear.obj], "Bounds": [0.5], "Encode": [0, 0.5, 0.5, 1]}
        )
    else:
        view = typed.FunctionType4.create(doc, common, data=b"{ }")
    shading = typed.ShadingType2.create(
        doc, {"ShadingType": 2, "ColorSpace": Name("DeviceGray"), "Coords": [0, 32, 64, 32], "Function": view.obj}
    )
    _pattern(doc, shading)
    base = doc.to_bytes()
    _levels(base, lambda x: x)
    edited = pyropdf.open(base)
    function = type(view)(edited.objects[_xref(view)])
    # A malformed required domain cannot replace the live value.
    with pytest.raises((TypeError, pyropdf.PdfError)):
        function["Domain"] = Name("broken")
    if isinstance(function, typed.FunctionType0):
        function.obj.write(b"\xff\x40\x00", filter=None)
    elif isinstance(function, typed.FunctionType2):
        function.obj["C0"] = [1]
        function.obj["C1"] = [0]
        function["N"] = 2
    elif isinstance(function, typed.FunctionType3):
        function.obj["Encode"] = [1, 0.25, 0.25, 0]
    else:
        assert isinstance(function, typed.FunctionType4)
        function.obj.write(b"{ dup mul 1 exch sub }", filter=None)
    # One piecewise linear segment per side of x = 1/2, or a true quadratic.
    if kind in (0, 3):
        _saves(edited, base, lambda x: 1 - 1.5 * x if x <= 0.5 else (1 - x) / 2)
    else:
        _saves(edited, base, lambda x: 1 - x * x)
    # Untouched encoded bytes survive a save after an unrelated change.
    pristine = pyropdf.open(base)
    pristine.catalog["Marker"] = True
    for incremental in (False, True):
        with pikepdf.Pdf.open(io.BytesIO(pristine.to_bytes(incremental=incremental))) as check:
            kept = check.get_object((_xref(view), 0))
            if isinstance(view.obj, pyropdf.Stream):
                assert kept.read_raw_bytes() == view.obj.read(decode=False)
            assert list(kept.Domain) == [0, 1]


@pytest.mark.spec("sampled-functions", "create", "edit", "preserve")
def test_edit_sampled_function() -> None:
    _function_case(0)


@pytest.mark.spec("exponential-functions", "edit")
def test_edit_exponential_function() -> None:
    _function_case(2)


@pytest.mark.spec("stitching-functions", "edit")
def test_edit_stitching_function() -> None:
    _function_case(3)


@pytest.mark.spec("calculator-functions", "create", "edit", "preserve")
def test_edit_calculator_function() -> None:
    _function_case(4)


def _mesh(kind: int) -> bytes:
    if kind in (4, 5):
        points = [(0, 0, 0), (255, 0, 255), (0, 255, 0), (255, 255, 255)]
        return b"".join(bytes([int(i == 3)] if kind == 4 else []) + bytes(point) for i, point in enumerate(points))
    # Twelve clockwise boundary points, then the tensor patch's four interior points.
    boundary = [0, 0, 85, 0, 170, 0, 255, 0, 255, 85, 255, 170, 255, 255, 170, 255, 85, 255, 0, 255, 0, 170, 0, 85]
    interior = [85, 85, 170, 85, 170, 170, 85, 170] if kind == 7 else []
    return bytes([0, *boundary, *interior, 0, 255, 255, 0])


def _shading_case(kind: int) -> None:
    doc = _document()
    fields = {"ShadingType": kind, "ColorSpace": Name("DeviceGray")}
    cls = getattr(typed, f"ShadingType{kind}")
    if kind == 1:
        fn = typed.FunctionType4.create(
            doc, {"FunctionType": 4, "Domain": [0, 1, 0, 1], "Range": [0, 1]}, data=b"{ pop }"
        )
        shading = cls.create(doc, {**fields, "Function": fn.obj, "Matrix": [64, 0, 0, 64, 0, 0]})
    elif kind in (2, 3):
        coords = [0, 32, 64, 32] if kind == 2 else [0, 32, 0, 0, 32, 64]
        shading = cls.create(doc, {**fields, "Function": _exponential(doc).obj, "Coords": coords})
    else:
        form = {"VerticesPerRow": 2} if kind == 5 else {"BitsPerFlag": 8}
        shading = cls.create(
            doc,
            {**fields, **form, "BitsPerCoordinate": 8, "BitsPerComponent": 8, "Decode": [0, 64, 0, 64, 0, 1]},
            data=_mesh(kind),
        )
    pattern = _pattern(doc, shading)
    base = doc.to_bytes()
    check = pyropdf.open(base)
    saved_pattern = typed.PatternType2(check.objects[_xref(pattern)])
    assert isinstance(saved_pattern.interpret("Shading"), cls)
    extracted = check.pages[0].content().paths[0].fill
    assert isinstance(extracted, pyropdf.ShadingPattern)
    assert extracted.shading_type == (4 if kind == 5 else kind)
    _levels(base, lambda x: x)
    kept = pyropdf.open(base)
    kept.catalog["Marker"] = True
    for incremental in (False, True):
        with pikepdf.Pdf.open(io.BytesIO(kept.to_bytes(incremental=incremental))) as pdf:
            subject = pdf.get_object((_xref(shading), 0))
            assert int(subject.ShadingType) == kind
            if kind >= 4:
                assert subject.read_raw_bytes() == _mesh(kind)
            else:
                assert subject.Function.is_indirect
        _levels(kept.to_bytes(incremental=incremental), lambda x: x)
    view = cls(check.objects[_xref(shading)])
    with pytest.raises((TypeError, pyropdf.PdfError)):
        view["ColorSpace"] = 9
    if kind == 1:
        view["Matrix"] = [-64, 0, 0, 64, 64, 0]
    elif kind in (2, 3):
        view.obj["Coords"] = [64, 32, 0, 32] if kind == 2 else [64, 32, 0, 64, 32, 64]
    else:
        view.obj["Decode"] = [0, 64, 0, 64, 1, 0]
    _saves(check, base, lambda x: 1 - x)


@pytest.mark.spec("function-shading", "read", "interpret", "create", "edit", "preserve")
def test_edit_function_based_shading() -> None:
    _shading_case(1)


@pytest.mark.spec("axial-shading", "edit")
def test_edit_axial_shading() -> None:
    _shading_case(2)


@pytest.mark.spec("radial-shading", "edit")
def test_edit_radial_shading() -> None:
    _shading_case(3)


@pytest.mark.spec("free-form-triangle-shading", "read", "interpret", "create", "edit", "preserve")
def test_edit_free_form_triangle_mesh() -> None:
    _shading_case(4)


@pytest.mark.spec("lattice-triangle-shading", "read", "interpret", "create", "edit", "preserve")
def test_edit_lattice_triangle_mesh() -> None:
    _shading_case(5)


@pytest.mark.spec("coons-patch-shading", "read", "interpret", "create", "edit", "preserve")
def test_edit_coons_patch() -> None:
    _shading_case(6)


@pytest.mark.spec("tensor-patch-shading", "read", "interpret", "create", "edit", "preserve")
def test_edit_tensor_patch() -> None:
    _shading_case(7)


def _tile(doc: pyropdf.Document, *, colored: bool) -> typed.PatternType1:
    entries = {
        "PatternType": 1,
        "PaintType": 1 if colored else 2,
        "TilingType": 1,
        "BBox": [0, 0, 8, 8],
        "XStep": 8,
        "YStep": 8,
        "Resources": {},
    }
    return typed.PatternType1.create(doc, entries, data=(b"0 g " if colored else b"") + b"0 0 4 8 re f")


def _tile_pixels(data: bytes, painted: tuple[int, int, int], *, solid: bool = False) -> None:
    for render in (_hayro, _poppler):
        stride, pixels = render(data)
        channels = stride // 64
        for x in (2, 6, 10, 14):
            offset = 30 * stride + x * channels
            expected = painted if solid or x % 8 < 4 else (255, 255, 255)
            assert tuple(pixels[offset : offset + 3]) == pytest.approx(expected, abs=2), (render.__name__, x)


@pytest.mark.spec("tiling-patterns", "create", "edit")
def test_edit_colored_tiling_pattern_cell() -> None:
    doc = _document()
    tile = _tile(doc, colored=True)
    with doc.pages[0].draw() as canvas:
        canvas.rect((0, 0, 64, 64), fill=PatternPaint(pattern=tile.obj))
    base = doc.to_bytes()
    _tile_pixels(base, (0, 0, 0))
    current = pyropdf.open(base)
    stored = typed.PatternType1(current.objects[_xref(tile)])
    with pytest.raises(TypeError):
        stored["TilingType"] = Name("broken")
    stored.obj.write(b"1 0 0 rg 0 0 8 8 re f")
    for incremental in (False, True):
        result = current.to_bytes(incremental=incremental)
        with pikepdf.Pdf.open(io.BytesIO(result)) as pdf:
            assert pdf.Root.Unrelated.read_raw_bytes() == b"unchanged\x00\xff"
            assert pdf.get_object((_xref(tile), 0)).PaintType == 1
        _tile_pixels(result, (255, 0, 0), solid=True)


@pytest.mark.spec("pattern-color-space", "create", "edit")
def test_edit_uncolored_pattern_base_space_and_keep_resource_collisions() -> None:
    doc = _document()
    tile = _tile(doc, colored=False)
    space = typed.PatternColorSpace.create(doc, [Name("Pattern"), Name("DeviceGray")])
    paint = PatternPaint(pattern=tile.obj, space=space.obj, components=(0,))
    pattern_name, color_name = f"PyroRP{_xref(tile)}G0", f"PyroPCS{_xref(space)}G0"
    page = doc.pages[0]
    page.obj["Resources"] = {"Pattern": {pattern_name: {}}, "ColorSpace": {color_name: Name("DeviceRGB")}}
    with page.draw() as canvas:
        with pytest.raises(ValueError, match="requires 1 component"):
            canvas.rect((0, 0, 64, 64), fill=PatternPaint(pattern=tile.obj, space=space.obj, components=(0, 1)))
        canvas.rect((0, 0, 64, 64), fill=paint)
    base = doc.to_bytes()
    _tile_pixels(base, (0, 0, 0))
    edited = pyropdf.open(base)
    view = typed.PatternColorSpace(edited.objects[_xref(space)])
    # Indexed still takes one operand, and index zero selects red.
    view.obj[1] = [Name("Indexed"), Name("DeviceRGB"), 0, b"\xff\x00\x00"]
    for incremental in (False, True):
        result = edited.to_bytes(incremental=incremental)
        _tile_pixels(result, (255, 0, 0))
        with pikepdf.Pdf.open(io.BytesIO(result)) as pdf:
            resources = pdf.pages[0].Resources
            assert len(resources.Pattern["/" + pattern_name]) == 0
            assert resources.ColorSpace["/" + color_name] == pikepdf.Name.DeviceRGB
            assert resources.ColorSpace["/" + color_name + "_1"].objgen == (_xref(space), 0)
            assert pdf.Root.Unrelated.read_raw_bytes() == b"unchanged\x00\xff"


@pytest.mark.spec("shading-patterns", "edit")
def test_edit_shading_pattern_matrix() -> None:
    doc = _document()
    shading = typed.ShadingType2.create(
        doc,
        {
            "ShadingType": 2,
            "ColorSpace": Name("DeviceGray"),
            "Coords": [0, 32, 64, 32],
            "Function": _exponential(doc).obj,
        },
    )
    pattern = _pattern(doc, shading)
    base = doc.to_bytes()
    _levels(base, lambda x: x)
    edited = pyropdf.open(base)
    saved = typed.PatternType2(edited.objects[_xref(pattern)])
    with pytest.raises((TypeError, ValueError)):
        saved["Matrix"] = [0, 1]
    saved["Matrix"] = [-1, 0, 0, 1, 64, 0]
    _saves(edited, base, lambda x: 1 - x)


def test_stored_pattern_in_strokes_and_text_uses_one_resource() -> None:
    doc = _document()
    tile = _tile(doc, colored=False)
    space = typed.PatternColorSpace.create(doc, [Name("Pattern"), Name("DeviceRGB")])
    paint = PatternPaint(pattern=tile.obj, space=space.obj, components=(1, 0, 0))
    with doc.pages[0].draw() as canvas:
        canvas.line((0, 20), (64, 20), stroke=pyropdf.Stroke(paint=paint, width=8))
        canvas.text("Ink", (4, 40), fill=paint)
    data = doc.to_bytes()
    with pikepdf.Pdf.open(io.BytesIO(data)) as pdf:
        page = pdf.pages[0]
        assert len(page.Resources.Pattern) == len(page.Resources.ColorSpace) == 1
        ops = [str(operator) for _args, operator in pikepdf.parse_content_stream(page)]
        assert "SCN" in ops
        assert "scn" in ops
    for render in (_hayro, _poppler):
        stride, pixels = render(data)
        channels = stride // 64
        point = 20 * stride + 2 * channels
        assert tuple(pixels[point : point + 3]) == (255, 0, 0)


def test_refuse_foreign_pattern_and_invalid_paint_operands_before_drawing() -> None:
    doc = _document()
    foreign_doc = _document()
    foreign = _tile(foreign_doc, colored=True)
    mask = _tile(doc, colored=False)
    colored = _tile(doc, colored=True)
    wrong_space = doc.objects[doc.objects.add([Name("Pattern"), Name("Pattern")])]
    rgb = typed.PatternColorSpace.create(doc, [Name("Pattern"), Name("DeviceRGB")]).obj
    pdf_text = doc.objects[doc.objects.add(["Pattern", "DeviceRGB"])]
    cases = [
        PatternPaint(pattern=foreign.obj),
        PatternPaint(pattern=colored.obj, space=rgb, components=(0, 1, 1)),
        PatternPaint(pattern=mask.obj),
        PatternPaint(pattern=mask.obj, space=wrong_space, components=(1,)),
        PatternPaint(pattern=mask.obj, space=rgb, components=(float("inf"), 0, 0)),
        PatternPaint(pattern=mask.obj, space=pdf_text, components=(0, 0, 0)),
        PatternPaint(pattern=mask.obj, space=rgb, components=(True, 0, 0)),
        PatternPaint(
            pattern=mask.obj,
            space=typed.PatternColorSpace.create(foreign_doc, [Name("Pattern"), Name("DeviceGray")]).obj,
            components=(0,),
        ),
    ]
    with doc.pages[0].draw() as canvas:
        for paint in cases:
            with pytest.raises((ValueError, TypeError)):
                canvas.rect((0, 0, 64, 64), fill=paint)
    assert len(doc.pages[0].content().paths) == 0


@pytest.mark.parametrize("kind", [6, 7], ids=["curved-coons-boundary", "displaced-tensor-interior"])
def test_edit_patch_control_point_changes_the_evaluated_mesh(kind: int) -> None:
    doc = _document()
    cls = typed.ShadingType6 if kind == 6 else typed.ShadingType7
    shading = cls.create(
        doc,
        {
            "ShadingType": kind,
            "ColorSpace": Name("DeviceGray"),
            "BitsPerCoordinate": 8,
            "BitsPerComponent": 8,
            "BitsPerFlag": 8,
            "Decode": [0, 64, 0, 64, 0, 1],
        },
        data=_mesh(kind),
    )
    _pattern(doc, shading)
    base = doc.to_bytes()
    edited = pyropdf.open(base)
    patch = cls(edited.objects[_xref(shading)])
    control_points = bytearray(patch.obj.read())
    # Translate point 2 on the bottom edge or point 13 in the interior by one third of the width.
    control_points[3 if kind == 6 else 25] = 170
    patch.obj.write(bytes(control_points))

    def expected(x: float) -> float:
        # The color is u. Only x moves; invert x(u,v) in this scan line.
        v = 1 - 32.5 / 64
        weight = 1 - v if kind == 6 else 3 * v * (1 - v) ** 2
        low, high = 0.0, 1.0
        for _ in range(20):
            u = (low + high) / 2
            if u + u * (1 - u) ** 2 * weight < x:
                low = u
            else:
                high = u
        return (low + high) / 2

    _saves(edited, base, expected)


def test_shared_edge_of_free_form_triangles_has_no_seam() -> None:
    doc = _document()
    mesh = typed.ShadingType4.create(
        doc,
        {
            "ShadingType": 4,
            "ColorSpace": Name("DeviceGray"),
            "BitsPerCoordinate": 8,
            "BitsPerComponent": 8,
            "BitsPerFlag": 8,
            "Decode": [0, 64, 0, 64, 0, 1],
        },
        data=_mesh(4),
    )
    _pattern(doc, mesh)
    data = doc.to_bytes()
    for render in (_hayro, _poppler):
        stride, pixels = render(data)
        channels = stride // 64
        # Cover both triangles and their shared diagonal. Pixel membership
        # on the outer contour depends on the renderer's scan conversion.
        for y in range(1, 63):
            for x in range(1, 63):
                assert abs(pixels[y * stride + x * channels] - x * 255 / 64) <= 7
