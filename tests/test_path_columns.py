"""The paths of a page as columns: `Page.paths` and `Paths` (#606)."""

from __future__ import annotations

import ctypes
import math
import struct
from typing import TYPE_CHECKING

import pytest
from corpus import ASSETS, NAMES
from test_content_model import page_with, stream_object
from test_text_buffers import _ArrowArray, _ArrowSchema, _bit, _get_pointer, _read

import pyropdf
from pyropdf import (
    BlendMode,
    ClosePath,
    Color,
    CurveTo,
    FillRule,
    LineCap,
    LineJoin,
    LineTo,
    MoveTo,
    ShadingPattern,
    SoftMask,
    SoftMaskType,
    TilingPattern,
    _core,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

NAN = float("nan")


def _same(value: float, expected: float) -> bool:
    """Return whether ``value`` is ``expected``, NaN included."""
    return (math.isnan(value) and math.isnan(expected)) or value == expected


def _assert_paint(
    columns: dict[str, list], prefix: str, row: int, paint: pyropdf.Paint | None, paths: pyropdf.Paths
) -> None:
    """Assert that the ``prefix`` paint columns of ``row`` hold ``paint``."""
    rgba = [columns[f"{prefix}_{name}"][row] for name in ("red", "green", "blue", "alpha")]
    pattern = columns[f"{prefix}_pattern"][row]
    if isinstance(paint, Color):
        assert rgba == [paint.red, paint.green, paint.blue, paint.alpha]
        assert pattern == -1
    else:
        assert all(math.isnan(value) for value in rgba)
        assert pattern == (-1 if paint is None else paths.patterns.index(paint))


def _assert_segments(columns: dict[str, list], rows: range, path: pyropdf.Path, row: int) -> None:
    """Assert that the segment columns ``rows`` hold the segments of ``path``, the path at ``row``."""
    assert len(rows) == len(path.segments)
    start = (NAN, NAN)
    for at, segment in zip(rows, path.segments, strict=True):
        assert columns["path"][at] == row
        to = (columns["to_x"][at], columns["to_y"][at])
        controls = [columns[name][at] for name in ("c1_x", "c1_y", "c2_x", "c2_y")]
        verb = chr(columns["verb"][at])
        if isinstance(segment, CurveTo):
            assert verb == "c"
            assert controls == [*segment.c1, *segment.c2]
        else:
            assert all(math.isnan(value) for value in controls)
        if isinstance(segment, MoveTo):
            assert verb == "m"
            start = segment.to
        elif isinstance(segment, LineTo):
            assert verb == "l"
        if isinstance(segment, ClosePath):
            # A closing segment ends where its subpath starts.
            assert verb == "h"
            assert all(_same(value, expected) for value, expected in zip(to, start, strict=True))
        else:
            assert to == segment.to


def _assert_columns(paths: pyropdf.Paths, records: Sequence[pyropdf.Path]) -> None:
    """Assert that every column of ``paths`` holds the fields of ``records``."""
    columns = {name: paths.column(name).tolist() for name in paths.names}
    offsets, dash_offsets = columns["segment_offsets"], columns["stroke_dashes_offsets"]
    assert len(columns["x0"]) == len(records)
    assert len(offsets) == len(dash_offsets) == len(records) + 1
    assert offsets[-1] == len(columns["verb"]) == len(columns["path"])
    assert dash_offsets[-1] == len(columns["stroke_dashes"])
    for row, path in enumerate(records):
        assert (columns["x0"][row], columns["y0"][row], columns["x1"][row], columns["y1"][row]) == path.bbox
        _assert_segments(columns, range(offsets[row], offsets[row + 1]), path, row)
        assert columns["has_fill"][row] == (path.fill is not None)
        _assert_paint(columns, "fill", row, path.fill, paths)
        assert columns["fill_rule"][row] == list(FillRule).index(path.fill_rule)
        stroke = path.stroke
        assert columns["has_stroke"][row] == (stroke is not None)
        _assert_paint(columns, "stroke", row, None if stroke is None else stroke.paint, paths)
        dashes = columns["stroke_dashes"][dash_offsets[row] : dash_offsets[row + 1]]
        style = [columns[f"stroke_{name}"][row] for name in ("width", "cap", "join", "miter_limit", "dash_offset")]
        if stroke is None:
            assert dashes == []
            assert all(_same(value, expected) for value, expected in zip(style, [NAN, 0, 0, NAN, NAN], strict=True))
        else:
            assert dashes == list(stroke.dashes)
            cap, join = list(LineCap).index(stroke.cap), list(LineJoin).index(stroke.join)
            assert style == [stroke.width, cap, join, stroke.miter_limit, stroke.dash_offset]
        assert paths.contexts[columns["context"][row]] == path.context
        assert columns["blend_mode"][row] == list(BlendMode).index(path.blend_mode)
        mask = columns["soft_mask"][row]
        assert (None if mask < 0 else paths.soft_masks[mask]) == path.soft_mask


@pytest.mark.parametrize("name", NAMES)
def test_the_paths_of_every_corpus_page_as_columns_equal_the_records(name: str) -> None:
    document = pyropdf.open(ASSETS / name)
    for page in document.pages:
        records = page.content(text_runs=False, images=False, max_elements=None).paths
        paths = page.paths(max_elements=None)
        assert list(paths) == list(records)
        _assert_columns(paths, records)
        # The records share the contexts of the columns.
        contexts = {id(context) for context in paths.contexts}
        assert all(id(path.context) in contexts for path in paths)


def _drawn_page() -> pyropdf.Page:
    """Return a page whose paths use every kind of segment, paint, and stroke."""
    tiling = "/PatternType 1 /PaintType 1 /TilingType 1 /BBox [0 0 10 10] /XStep 10 /YStep 12 /Resources << >>"
    return page_with(
        # A line, as CAD output draws one, in red.
        "1 0 0 RG 0.5 w 10 10 m 14 13 l S "
        # A closed triangle filled even-odd and stroked with dashes, round caps, and bevel joins.
        "0 0 1 rg 0 1 0 RG 2 w 1 J 2 j [3 2] 1 d 20 10 m 40 10 l 30 30 l h B* "
        # A curve, clipped and marked.
        "q 0 0 100 100 re W n /Span << /MCID 3 >> BDC 0.5 g [] 0 d 0 J 0 j "
        "50 10 m 60 40 70 0 80 30 c f EMC Q "
        # A tiling pattern's fill under a blend mode.
        "/GS1 gs /Pattern cs /P1 scn 120 10 40 40 re f",
        resources="/Pattern << /P1 6 0 R >> /ExtGState << /GS1 7 0 R >>",
        objects={6: stream_object("0 0 5 5 re f", tiling), 7: "<< /Type /ExtGState /BM /Multiply >>"},
    )


def test_the_columns_hold_the_segments_paints_and_strokes() -> None:
    page = _drawn_page()
    paths = page.paths()
    line, triangle, curve, square = paths
    assert len(paths) == 4
    assert list(paths) == list(page.content().paths)

    verbs = bytes(paths.column("verb"))
    assert verbs == b"ml" + b"mllh" + b"mc" + b"mlllh"
    assert paths.column("segment_offsets").tolist() == [0, 2, 6, 8, 13]
    assert paths.column("path").tolist() == [0, 0, 1, 1, 1, 1, 2, 2, 3, 3, 3, 3, 3]
    # The closing segment ends where its subpath starts, so each segment
    # other than a move runs from the previous point to its own.
    to_x, to_y = paths.column("to_x").tolist(), paths.column("to_y").tolist()
    start, end = triangle.segments[0], line.segments[1]
    assert isinstance(start, MoveTo)
    assert isinstance(end, LineTo)
    assert (to_x[5], to_y[5]) == (to_x[2], to_y[2]) == start.to
    assert (to_x[1], to_y[1]) == end.to
    (_, cubic) = curve.segments
    assert isinstance(cubic, CurveTo)
    controls = [paths.column(name).tolist()[7] for name in ("c1_x", "c1_y", "c2_x", "c2_y")]
    assert controls == [*cubic.c1, *cubic.c2]
    assert all(math.isnan(value) for value in paths.column("c1_x").tolist()[:7])

    assert paths.column("has_stroke").tolist() == [1, 1, 0, 0]
    assert paths.column("stroke_red").tolist()[:2] == [1.0, 0.0]
    assert paths.column("stroke_green").tolist()[:2] == [0.0, 1.0]
    assert all(math.isnan(value) for value in paths.column("stroke_red").tolist()[2:])
    assert paths.column("stroke_width").tolist()[:2] == [0.5, 2.0]
    assert paths.column("stroke_cap").tolist() == [0, 1, 0, 0]
    assert paths.column("stroke_join").tolist() == [0, 2, 0, 0]
    assert paths.column("stroke_dashes_offsets").tolist() == [0, 0, 2, 2, 2]
    assert paths.column("stroke_dashes").tolist() == [3.0, 2.0]
    assert paths.column("stroke_dash_offset").tolist()[:2] == [0.0, 1.0]

    assert paths.column("has_fill").tolist() == [0, 1, 1, 1]
    assert paths.column("fill_rule").tolist() == [0, 1, 0, 0]
    assert paths.column("fill_blue").tolist()[1] == 1.0
    assert paths.column("fill_red").tolist()[2] == pytest.approx(0.5, abs=1 / 255)
    assert paths.column("fill_pattern").tolist() == [-1, -1, -1, 0]
    (pattern,) = paths.patterns
    assert isinstance(pattern, TilingPattern)
    assert square.fill is pattern
    assert math.isnan(paths.column("fill_red").tolist()[3])

    assert paths.column("blend_mode").tolist() == [0, 0, 0, list(BlendMode).index(BlendMode.MULTIPLY)]
    assert paths.column("soft_mask").tolist() == [-1, -1, -1, -1]
    assert paths.soft_masks == ()
    contexts = paths.column("context").tolist()
    assert curve.context is paths.contexts[contexts[2]]
    assert curve.context.marked_content[0].mcid == 3
    assert len(curve.context.clips) == 1
    assert line.context is triangle.context is paths.contexts[contexts[0]]


def test_patterns_and_soft_masks_are_shared_values() -> None:
    group = "/Type /XObject /Subtype /Form /BBox [0 0 200 100] /Group << /S /Transparency /CS /DeviceGray >>"
    page = page_with(
        "q 20 10 60 60 re W n /Sh sh Q /Pattern cs /P1 scn 120 10 40 40 re f /GS1 gs 0 0 1 rg 10 60 20 20 re f",
        resources="/Shading << /Sh 6 0 R >> /Pattern << /P1 9 0 R >> /ExtGState << /GS1 7 0 R >>",
        objects={
            6: (
                "<< /ShadingType 2 /ColorSpace /DeviceRGB /Coords [20 0 80 0] "
                "/Function << /FunctionType 2 /Domain [0 1] /C0 [1 0 0] /C1 [0 0 1] /N 1 >> >>"
            ),
            7: "<< /Type /ExtGState /SMask << /S /Luminosity /G 8 0 R >> >>",
            8: stream_object("0 0 200 100 re f", group),
            9: "<< /PatternType 2 /Shading 6 0 R >>",
        },
    )
    paths = page.paths()
    records = page.content().paths
    assert list(paths) == list(records)
    _assert_columns(paths, records)
    # `sh` and the shading pattern paint the same shading in the same space,
    # which the columns hold once.
    shaded, patterned, masked = paths
    (pattern,) = paths.patterns
    assert isinstance(pattern, ShadingPattern)
    assert paths.column("fill_pattern").tolist() == [0, 0, -1]
    assert shaded.fill is patterned.fill is pattern
    assert paths.column("soft_mask").tolist() == [-1, -1, 0]
    assert paths.soft_masks == (SoftMask(xref=8, subtype=SoftMaskType.LUMINOSITY),)
    assert masked.soft_mask is paths.soft_masks[0]


def test_a_cad_page_reads_its_lines_by_color() -> None:
    lines = 2_000
    page = page_with(
        "0.5 w "
        + " ".join(
            f"{'1 0 0' if index % 10 == 0 else '0 0 0'} RG {index % 40 * 5} {index // 40} m "
            f"{index % 40 * 5 + 4} {index // 40 + 3} l S"
            for index in range(lines)
        )
    )
    paths = page.paths()
    assert len(paths) == lines
    verbs = bytes(paths.column("verb"))
    assert verbs == b"ml" * lines
    owner = paths.column("path").tolist()
    red = paths.column("stroke_red").tolist()
    ends = [index for index, verb in enumerate(verbs) if verb == ord("l")]
    assert [owner[end] for end in ends if red[owner[end]] == 1] == list(range(0, lines, 10))


def test_paths_take_the_options_of_content() -> None:
    page = page_with(
        "10 10 20 20 re f 150 10 20 20 re f",
        annots="6 0 R",
        objects={
            6: "<< /Type /Annot /Subtype /Square /Rect [100 10 150 60] /AP << /N 7 0 R >> >>",
            7: stream_object("0 0 1 rg 0 0 50 50 re f", "/Type /XObject /Subtype /Form /BBox [0 0 50 50]"),
        },
    )
    for options in ({}, {"clip": (0, 0, 100, 100)}, {"annotations": False}, {"clip": (31, 0, 99, 100)}):
        records = page.content(text_runs=False, images=False, **options).paths  # ty: ignore[invalid-argument-type]
        paths = page.paths(**options)  # ty: ignore[invalid-argument-type]
        assert list(paths) == list(records), options
        _assert_columns(paths, records)
    assert [len(page.paths(**options)) for options in ({}, {"annotations": False})] == [3, 2]  # ty: ignore[invalid-argument-type]


def test_paths_and_content_share_one_interpretation() -> None:
    page = page_with("10 10 20 20 re f 30 10 m 40 20 l S")
    document = page._document  # noqa: SLF001
    before = _core.page_result_counts(document._core())  # noqa: SLF001
    page.content(text_runs=False, images=False)
    page.paths()
    hits, computed = _core.page_result_counts(document._core())  # noqa: SLF001
    assert (hits - before[0], computed - before[1]) == (1, 1)


def test_max_elements_counts_what_content_counts() -> None:
    page = page_with("[1 1 1] 0 d 10 10 20 20 re B 30 10 m 40 20 l S")
    needed = 1
    while True:
        try:
            page.content(text_runs=False, images=False, max_elements=needed)
        except pyropdf.LimitError:
            needed += 1
        else:
            break
    for limit in (needed - 1, 1):
        with pytest.raises(pyropdf.LimitError) as caught:
            page.paths(max_elements=limit)
        assert caught.value.code == "content_element_count"
    assert len(page.paths(max_elements=needed)) == 2
    # 90,000 strokes of two segments each pass the default of 262,144.
    dense = page_with(" ".join(f"{index % 190} 10 m {index % 190 + 5} 20 l S" for index in range(90_000)))
    with pytest.raises(pyropdf.LimitError, match="over the limit of 262144"):
        dense.paths()
    assert len(dense.paths(max_elements=None)) == 90_000


def test_the_row_view_indexes_slices_and_iterates_records() -> None:
    paths = _drawn_page().paths()
    records = list(paths)
    assert paths[0] == records[0]
    assert paths[-1] == records[-1]
    with pytest.raises(IndexError):
        paths[len(records)]
    view = paths[1:3]
    assert isinstance(view, pyropdf.Paths)
    assert list(view) == records[1:3]
    assert list(view[1:]) == records[2:3]
    assert view.column("x0").tolist() == [path.bbox.x0 for path in records[1:3]]
    # A view's offsets index the segments of the whole page.
    assert view.column("segment_offsets").tolist() == [2, 6, 8]
    assert view.column("verb").tolist() == paths.column("verb").tolist()
    assert view.column("stroke_dashes_offsets").tolist() == [0, 2, 2]
    # Its records share the contexts and patterns of the page.
    assert view[0].context is paths[1].context
    assert view.contexts is paths.contexts
    assert len(paths[3:1]) == 0
    with pytest.raises(ValueError, match="step of 1"):
        paths[::2]
    with pytest.raises(ValueError, match="no column"):
        paths.column("width")
    column = paths.column("to_x")
    assert column.readonly
    assert (column.format, column.itemsize) == ("d", 8)
    assert (paths.column("fill_red").format, paths.column("fill_red").itemsize) == ("f", 4)
    assert (paths.column("fill_pattern").format, paths.column("verb").format) == ("i", "B")
    with pytest.raises(TypeError):
        column[0] = 1.0  # ty: ignore[invalid-assignment]
    assert repr(paths) == f"<Paths {len(records)} rows>"
    assert hash(paths) == hash(_drawn_page().paths())
    assert paths == _drawn_page().paths()
    assert paths != view


def test_a_page_without_paths_has_no_rows_and_a_stale_page_refuses() -> None:
    document = pyropdf.Document()
    page = document.pages.new((200, 100))
    paths = page.paths()
    assert len(paths) == 0
    assert list(paths) == []
    assert paths.column("segment_offsets").tolist() == [0]
    assert paths.column("verb").tolist() == []
    assert paths.contexts == ()
    del document.pages[0]
    with pytest.raises(pyropdf.StalePageError):
        page.paths()


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ({"max_elements": 0}, ValueError),
        ({"max_elements": True}, TypeError),
        ({"annotations": 1}, TypeError),
        ({"clip": (0, 0, 1)}, ValueError),
    ],
)
def test_paths_validate_their_options(options: dict[str, object], error: type[Exception]) -> None:
    page = page_with("10 10 20 20 re f")
    with pytest.raises(error):
        page.paths(**options)  # ty: ignore[invalid-argument-type]


def _children(owner: _ArrowSchema | _ArrowArray) -> list:
    return [owner.children[index].contents for index in range(owner.n_children)]


def test_the_arrow_capsules_hold_a_struct_array_over_the_columns() -> None:
    paths = _drawn_page().paths()[1:4]
    records = list(paths)
    schema = _ArrowSchema.from_address(_get_pointer(paths.__arrow_c_schema__(), b"arrow_schema"))
    assert schema.format == b"+s"
    fields = _children(schema)
    names = [field.name.decode() for field in fields]
    assert names == [
        "x0",
        "y0",
        "x1",
        "y1",
        "segments",
        *(name for name in pyropdf.Paths.names[13:30]),
        "stroke_dashes",
        "stroke_dash_offset",
        "context",
        "blend_mode",
        "soft_mask",
    ]
    segments, dashes = fields[4], fields[22]
    assert (segments.format, segments.flags, dashes.format, dashes.flags) == (b"+l", 0, b"+l", 2)
    (item,) = _children(segments)
    assert item.format == b"+s"
    assert [(field.name, field.format, field.flags) for field in _children(item)] == [
        (b"path", b"I", 0),
        (b"verb", b"C", 0),
        (b"to_x", b"g", 0),
        (b"to_y", b"g", 0),
        (b"c1_x", b"g", 2),
        (b"c1_y", b"g", 2),
        (b"c2_x", b"g", 2),
        (b"c2_y", b"g", 2),
    ]
    formats = {field.name: (field.format, field.flags) for field in fields}
    assert formats[b"has_fill"] == (b"b", 0)
    assert formats[b"fill_red"] == (b"f", 2)
    assert formats[b"fill_pattern"] == (b"i", 2)
    assert formats[b"stroke_width"] == (b"g", 2)

    schema_capsule, array_capsule = paths.__arrow_c_array__()
    array = _ArrowArray.from_address(_get_pointer(array_capsule, b"arrow_array"))
    assert (array.length, array.null_count, array.n_children) == (3, 0, 27)
    children = _children(array)
    for child in children:
        assert (child.length, child.offset) == (3, 1)
    x0 = struct.unpack("<3d", _read(children[0].buffers[1] + 8, 3 * 8))
    assert list(x0) == [path.bbox.x0 for path in records]
    offsets = struct.unpack("<4i", _read(children[4].buffers[1] + 4, 4 * 4))
    assert offsets == (2, 6, 8, 13)
    (values,) = _children(children[4])
    assert (values.length, values.offset, values.n_children) == (13, 0, 8)
    curve = _children(values)[4]
    assert curve.null_count == 12
    assert [_bit(curve.buffers[0], index) for index in range(13)] == [index == 7 for index in range(13)]
    stroked = children[12]
    assert [_bit(stroked.buffers[1], 1 + row) for row in range(3)] == [path.stroke is not None for path in records]
    stroke_width = children[18]
    assert stroke_width.null_count == 2
    pattern = children[10]
    assert pattern.null_count == 2

    # A consumer releases what it moved out, and the capsules then free
    # nothing twice.
    array.release(ctypes.byref(array))
    assert not array.release
    del array_capsule, schema_capsule


def test_numpy_reads_the_coordinates_without_a_copy() -> None:
    numpy = pytest.importorskip("numpy")
    paths = _drawn_page().paths()
    to_x = numpy.asarray(paths.column("to_x"))
    assert to_x.dtype == numpy.float64
    assert not to_x.flags.owndata
    assert not to_x.flags.writeable
    # Two reads point at one buffer, the one that Arrow exports too.
    assert numpy.asarray(paths.column("to_x")).ctypes.data == to_x.ctypes.data
    _, array_capsule = paths.__arrow_c_array__()
    array = _ArrowArray.from_address(_get_pointer(array_capsule, b"arrow_array"))
    (values,) = _children(_children(array)[4])
    assert _children(values)[2].buffers[1] == to_x.ctypes.data
    assert to_x.tolist() == paths.column("to_x").tolist()
    assert numpy.asarray(paths.column("fill_red")).dtype == numpy.float32
    assert numpy.asarray(paths.column("path")).dtype == numpy.uint32


def test_pyarrow_imports_the_paths_without_a_copy() -> None:
    pa = pytest.importorskip("pyarrow")
    paths = _drawn_page().paths()
    records = list(paths)
    table = pa.array(paths)
    table.validate(full=True)
    assert table.field("has_stroke").to_pylist() == [path.stroke is not None for path in records]
    assert table.field("fill_pattern").to_pylist() == [None, None, None, 0]
    assert table.field("stroke_dashes").to_pylist() == [[], [3.0, 2.0], None, None]
    segments = table.field("segments").to_pylist()
    assert [len(row) for row in segments] == [len(path.segments) for path in records]
    assert segments[2][1]["c1_x"] == records[2].segments[1].c1.x  # ty: ignore[unresolved-attribute]
    assert segments[0][1]["c1_x"] is None
    assert pa.array(paths[1:3]).field("x0").to_pylist() == [path.bbox.x0 for path in records[1:3]]
