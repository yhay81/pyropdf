"""``doc.history.save()`` and ``restore()`` carry the named steps to the same document opened again (#428).

PyMuPDF's ``Document.journal_save`` and ``journal_load`` keep its undo
journal the same way; their bytes are not compatible.
"""

from __future__ import annotations

import hashlib
import struct

import pytest
from test_pages import numbered

import pyropdf
from pyropdf import Document, DocumentLimits

SOURCE = numbered(3)


def widths(doc: Document) -> list[int]:
    """Return the short side of each page, which rotation leaves alone."""
    return [int(min(page.rect[2] - page.rect[0], page.rect[3] - page.rect[1])) for page in doc.pages]


def state(doc: Document) -> tuple[list[int], list[int]]:
    return widths(doc), [page.rotation for page in doc.pages]


def edited(source: bytes = SOURCE) -> Document:
    doc = pyropdf.open(source)
    with doc.transaction("rotate"):
        doc.pages[0].rotation = 90
    with doc.transaction("delete"):
        del doc.pages[1]
    doc.pages[0].rotation = 180
    with doc.transaction("add"):
        doc.objects.add({"Kind": pyropdf.Name("Note"), "Items": [1, 2, 3]})
    return doc


def resealed(body: bytes) -> bytes:
    return body + hashlib.sha256(body).digest()


def test_restore_undoes_and_redoes_the_named_steps() -> None:
    saved = edited()
    data = saved.history.save()

    doc = pyropdf.open(SOURCE)
    doc.history.restore(data)

    assert doc.history.steps == ("rotate", "delete", "", "add")
    assert doc.history.position == 4
    assert state(doc) == state(saved) == ([100, 102], [180, 0])
    assert doc.history.save() == data
    doc.history.undo()
    doc.history.undo()
    assert state(doc) == ([100, 102], [90, 0])
    doc.history.undo()
    doc.history.undo()
    assert state(doc) == ([100, 101, 102], [0, 0, 0])
    assert not doc.history.can_undo
    for _ in range(4):
        doc.history.redo()
    assert state(doc) == ([100, 102], [180, 0])
    assert doc.to_bytes() == saved.to_bytes()


def test_restore_keeps_the_position_and_the_redo_steps() -> None:
    saved = edited()
    saved.history.undo()
    saved.history.undo()
    data = saved.history.save()

    doc = pyropdf.open(SOURCE)
    doc.history.restore(data)

    assert doc.history.steps == ("rotate", "delete", "", "add")
    assert doc.history.position == 2
    assert state(doc) == ([100, 102], [90, 0])
    doc.history.redo()
    assert state(doc) == ([100, 102], [180, 0])


def test_a_step_after_restore_replaces_the_redo_steps() -> None:
    saved = edited()
    saved.history.undo()
    saved.history.undo()
    saved.history.undo()
    doc = pyropdf.open(SOURCE)
    doc.history.restore(saved.history.save())

    with doc.transaction("shrink"):
        doc.pages[1].rotation = 270

    assert doc.history.steps == ("rotate", "shrink")
    assert state(doc) == ([100, 101, 102], [90, 270, 0])
    again = pyropdf.open(SOURCE)
    again.history.restore(doc.history.save())
    assert again.history.steps == ("rotate", "shrink")
    assert state(again) == state(doc)
    again.history.undo()
    assert state(again) == ([100, 101, 102], [90, 0, 0])


def test_a_history_restores_from_the_oldest_step_it_kept() -> None:
    saved = pyropdf.open(SOURCE)
    for index in range(3):
        with saved.transaction(f"step {index}"):
            saved.pages[index].rotation = 90
    saved.history.max_size = 0
    with saved.transaction("last"):
        saved.pages[0].rotation = 180
    saved.history.max_size = None
    with saved.transaction("after"):
        saved.pages[1].rotation = 180
    assert saved.history.steps == ("after",)

    doc = pyropdf.open(SOURCE)
    doc.history.restore(saved.history.save())

    assert doc.history.steps == ("after",)
    assert state(doc) == ([100, 101, 102], [180, 180, 90])
    doc.history.undo()
    assert state(doc) == ([100, 101, 102], [180, 90, 90])
    assert not doc.history.can_undo


def test_the_history_limit_drops_steps_on_restore() -> None:
    saved = edited()
    doc = pyropdf.open(SOURCE)
    doc.history.max_size = 0

    doc.history.restore(saved.history.save())

    assert doc.history.steps == ()
    assert state(doc) == state(saved)


def test_a_new_document_restores_into_a_new_document() -> None:
    saved = pyropdf.Document()
    with saved.transaction("pages"):
        saved.pages.new((200, 300))
        saved.pages.new((400, 500))

    doc = pyropdf.Document()
    doc.history.restore(saved.history.save())

    assert doc.history.steps == ("pages",)
    assert widths(doc) == [200, 400]
    doc.history.undo()
    assert widths(doc) == []


def test_another_document_is_refused() -> None:
    data = edited().history.save()
    doc = pyropdf.open(numbered(2))

    with pytest.raises(ValueError, match="another document"):
        doc.history.restore(data)
    with pytest.raises(ValueError, match="another document"):
        pyropdf.Document().history.restore(data)

    assert doc.history.steps == ()
    assert widths(doc) == [100, 101]


def test_an_edited_document_is_refused() -> None:
    data = edited().history.save()
    doc = pyropdf.open(SOURCE)
    doc.pages[2].rotation = 90

    with pytest.raises(pyropdf.PdfError, match="as opened"):
        doc.history.restore(data)
    assert doc.history.steps == ("",)

    # Undo back to the opened version still leaves a step to redo.
    doc.history.undo()
    with pytest.raises(pyropdf.PdfError, match="as opened"):
        doc.history.restore(data)
    assert doc.history.steps == ("",)
    assert state(doc) == ([100, 101, 102], [0, 0, 0])


def test_save_and_restore_are_refused_inside_a_transaction() -> None:
    data = edited().history.save()
    doc = pyropdf.open(SOURCE)

    with doc.transaction("open"):
        with pytest.raises(pyropdf.PdfError, match="inside a transaction"):
            doc.history.save()
        with pytest.raises(pyropdf.PdfError, match="inside a transaction"):
            doc.history.restore(data)
    assert doc.history.steps == ()


def corruptions(data: bytes) -> dict[str, bytes]:
    body = data[:-32]
    versions_at = 16 + 4 + 32 + 4
    return {
        "empty": b"",
        "truncated": data[:-1],
        "flipped": data[:100] + bytes([data[100] ^ 1]) + data[101:],
        "magic": resealed(b"X" + body[1:]),
        "format": resealed(body[:16] + struct.pack("<I", 2) + body[20:]),
        "position": resealed(body[: versions_at - 4] + struct.pack("<I", 9) + body[versions_at:]),
        "count": resealed(body[:versions_at] + struct.pack("<I", 99) + body[versions_at + 4 :]),
        "trailing": resealed(body + b"\0"),
        "object": resealed(body.replace(b"/Note", b"(Note")),
    }


@pytest.mark.parametrize("kind", list(corruptions(b"\0" * 200)))
def test_corrupt_data_leaves_the_document_and_its_history_unchanged(kind: str) -> None:
    data = edited().history.save()
    corrupt = corruptions(data)[kind]
    assert corrupt != data
    doc = pyropdf.open(SOURCE)

    with pytest.raises(pyropdf.MalformedPdfError, match=r"saved history|object"):
        doc.history.restore(corrupt)

    assert doc.history.steps == ()
    assert state(doc) == ([100, 101, 102], [0, 0, 0])
    doc.history.restore(data)
    assert doc.history.position == 4


def test_data_over_the_document_limits_leaves_it_unchanged() -> None:
    saved = pyropdf.open(SOURCE)
    with saved.transaction("deep"):
        saved.objects.add([[[[[[1]]]]]])
    data = saved.history.save()
    doc = pyropdf.open(SOURCE, limits=DocumentLimits(max_object_depth=4))

    with pytest.raises(pyropdf.LimitError):
        doc.history.restore(data)

    assert doc.history.steps == ()
    pyropdf.open(SOURCE).history.restore(data)


def test_restore_takes_bytes() -> None:
    with pytest.raises(TypeError, match="bytes"):
        pyropdf.open(SOURCE).history.restore(bytearray(b"x"))  # ty: ignore[invalid-argument-type]
