"""The chars of a layout as columns: `Page.chars` and `Layout.chars` (#99)."""

from __future__ import annotations

import ctypes
import struct
from typing import TYPE_CHECKING

import pytest
from corpus import ASSETS, NAMES
from test_content import _page as build_text_pdf

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Iterator


def _records(layout: pyropdf.Layout) -> Iterator[tuple[int, int, int, pyropdf.Char]]:
    """Yield each char of ``layout`` with the indexes of its block, line, and span."""
    for block_index, block in enumerate(layout.blocks):
        for line_index, line in enumerate(block.lines):
            for span_index, span in enumerate(line.spans):
                for char in span.chars:
                    yield block_index, line_index, span_index, char


def _assert_columns(chars: pyropdf.Chars, layout: pyropdf.Layout) -> None:
    """Assert that every column of ``chars`` holds the fields of the records of ``layout``."""
    records = list(_records(layout))
    columns = {name: chars.column(name).tolist() for name in chars.names}
    offsets, data = columns["text_offsets"], bytes(chars.column("text_data"))
    assert len(offsets) == len(records) + 1
    for row, (block, line, span, char) in enumerate(records):
        assert data[offsets[row] : offsets[row + 1]].decode() == char.text
        assert (columns["x0"][row], columns["y0"][row], columns["x1"][row], columns["y1"][row]) == char.bbox
        assert (columns["origin_x"][row], columns["origin_y"][row]) == char.origin
        assert (columns["block"][row], columns["line"][row], columns["span"][row]) == (block, line, span)
        assert columns["is_synthetic"][row] == char.is_synthetic
        assert columns["stream"][row] == (char.stream or 0)
        assert columns["operation"][row] == (char.operation or 0)


@pytest.mark.parametrize("name", NAMES)
def test_the_chars_of_every_corpus_page_as_columns_equal_the_records(name: str) -> None:
    document = pyropdf.open(ASSETS / name)
    for page in document.pages:
        layout = page.layout(chars=True, unmapped="glyph_id")
        chars = page.chars(unmapped="glyph_id")
        assert layout.chars == chars
        assert list(chars) == [char for _, _, _, char in _records(layout)]
        _assert_columns(chars, layout)


def test_chars_take_the_options_of_the_layout() -> None:
    page = pyropdf.open(ASSETS / "bill-hr815.pdf").pages[0]
    layout = page.layout(chars=True, clip=(0, 0, 400, 400), ligatures=False, dehyphenate=True, styles=True)
    chars = page.chars(clip=(0, 0, 400, 400), ligatures=False, dehyphenate=True, styles=True)
    assert len(chars) < len(page.chars())
    assert layout.chars == chars
    _assert_columns(chars, layout)
    assert page.layout().chars is None


def test_the_row_view_indexes_slices_and_iterates_records() -> None:
    page = pyropdf.open(build_text_pdf(b"BT /F1 20 Tf 30 200 Td (Hello) Tj ( world) Tj ET")).pages[0]
    chars = page.chars()
    records = list(chars)
    assert "".join(char.text for char in records) == "Hello world"
    assert chars[0] == records[0]
    assert chars[-1] == records[-1]
    with pytest.raises(IndexError):
        chars[len(records)]
    view = chars[2:7]
    assert isinstance(view, pyropdf.Chars)
    assert list(view) == records[2:7]
    assert list(view[1:-1]) == records[3:6]
    assert view.column("x0").tolist() == [char.bbox[0] for char in records[2:7]]
    # A view's offsets index the text of the whole page.
    offsets = view.column("text_offsets").tolist()
    assert bytes(view.column("text_data"))[offsets[0] : offsets[-1]] == b"llo w"
    assert len(chars[5:2]) == 0
    with pytest.raises(ValueError, match="step of 1"):
        chars[::2]
    with pytest.raises(ValueError, match="no column"):
        chars.column("width")
    column = chars.column("x0")
    assert column.readonly
    assert column.format == "d"
    with pytest.raises(TypeError):
        column[0] = 1.0  # ty: ignore[invalid-assignment]
    assert repr(chars) == f"<Chars {len(records)} rows>"
    assert hash(chars) == hash(page.chars())


def test_a_page_without_text_has_no_rows_and_a_stale_page_refuses() -> None:
    document = pyropdf.open(build_text_pdf(b""))
    page = document.pages[0]
    chars = page.chars()
    assert len(chars) == 0
    assert list(chars) == []
    assert chars.column("text_offsets").tolist() == [0]
    del document.pages[0]
    with pytest.raises(pyropdf.StalePageError):
        page.chars()


class _ArrowSchema(ctypes.Structure):
    pass


_ArrowSchema._fields_ = [
    ("format", ctypes.c_char_p),
    ("name", ctypes.c_char_p),
    ("metadata", ctypes.c_char_p),
    ("flags", ctypes.c_int64),
    ("n_children", ctypes.c_int64),
    ("children", ctypes.POINTER(ctypes.POINTER(_ArrowSchema))),
    ("dictionary", ctypes.POINTER(_ArrowSchema)),
    ("release", ctypes.CFUNCTYPE(None, ctypes.POINTER(_ArrowSchema))),
    ("private_data", ctypes.c_void_p),
]


class _ArrowArray(ctypes.Structure):
    pass


_ArrowArray._fields_ = [
    ("length", ctypes.c_int64),
    ("null_count", ctypes.c_int64),
    ("offset", ctypes.c_int64),
    ("n_buffers", ctypes.c_int64),
    ("n_children", ctypes.c_int64),
    ("buffers", ctypes.POINTER(ctypes.c_void_p)),
    ("children", ctypes.POINTER(ctypes.POINTER(_ArrowArray))),
    ("dictionary", ctypes.POINTER(_ArrowArray)),
    ("release", ctypes.CFUNCTYPE(None, ctypes.POINTER(_ArrowArray))),
    ("private_data", ctypes.c_void_p),
]

_get_pointer = ctypes.pythonapi.PyCapsule_GetPointer
_get_pointer.restype = ctypes.c_void_p
_get_pointer.argtypes = [ctypes.py_object, ctypes.c_char_p]


def _read(address: int, size: int) -> bytes:
    return ctypes.string_at(address, size)


def _bit(address: int, index: int) -> bool:
    return bool(_read(address + index // 8, 1)[0] >> (index % 8) & 1)


def test_the_arrow_capsules_hold_a_struct_array_over_the_columns() -> None:
    chars = pyropdf.open(ASSETS / "f1040.pdf").pages[0].chars()[100:300]
    records = list(chars)
    schema_capsule = chars.__arrow_c_schema__()
    schema = _ArrowSchema.from_address(_get_pointer(schema_capsule, b"arrow_schema"))
    assert schema.format == b"+s"
    fields = [schema.children[i].contents for i in range(schema.n_children)]
    assert [(field.name, field.format, field.flags) for field in fields] == [
        (b"x0", b"g", 0),
        (b"y0", b"g", 0),
        (b"x1", b"g", 0),
        (b"y1", b"g", 0),
        (b"origin_x", b"g", 0),
        (b"origin_y", b"g", 0),
        (b"text", b"u", 0),
        (b"block", b"I", 0),
        (b"line", b"I", 0),
        (b"span", b"I", 0),
        (b"is_synthetic", b"b", 0),
        (b"stream", b"I", 2),
        (b"operation", b"I", 2),
    ]

    schema_capsule, array_capsule = chars.__arrow_c_array__()
    array = _ArrowArray.from_address(_get_pointer(array_capsule, b"arrow_array"))
    assert (array.length, array.null_count, array.n_children) == (200, 0, 13)
    children = [array.children[i].contents for i in range(array.n_children)]
    for child in children:
        assert (child.length, child.offset) == (200, 100)
    x0, text, synthetic, stream = children[0], children[6], children[10], children[11]
    values = struct.unpack("<200d", _read(x0.buffers[1] + 100 * 8, 200 * 8))
    assert list(values) == [char.bbox[0] for char in records]
    offsets = struct.unpack("<201i", _read(text.buffers[1] + 100 * 4, 201 * 4))
    data = _read(text.buffers[2] + offsets[0], offsets[-1] - offsets[0])
    assert data.decode() == "".join(char.text for char in records)
    assert [_bit(synthetic.buffers[1], 100 + row) for row in range(200)] == [char.is_synthetic for char in records]
    assert stream.null_count == sum(char.stream is None for char in records)
    for row, char in enumerate(records):
        assert _bit(stream.buffers[0], 100 + row) == (char.stream is not None)

    # A consumer releases what it moved out, and the capsules then free
    # nothing twice.
    array.release(ctypes.byref(array))
    assert not array.release
    del array_capsule, schema_capsule


def test_pyarrow_imports_the_chars_without_a_copy() -> None:
    pa = pytest.importorskip("pyarrow")
    chars = pyropdf.open(ASSETS / "f1040.pdf").pages[0].chars()
    table = pa.array(chars)
    table.validate(full=True)
    records = list(chars)
    assert table.field("text").to_pylist() == [char.text for char in records]
    assert table.field("operation").to_pylist() == [char.operation for char in records]
    assert pa.array(chars[10:20]).field("x0").to_pylist() == [char.bbox[0] for char in records[10:20]]
