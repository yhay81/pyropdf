"""FileAttachment annotations and the files they embed.

pylopdf's tests/test_annotation_files.py at ebb01d3c. Intended differences:

- the core's checks are called as ``_core.annotation_add`` with the page's
  object, and its error for data on another kind names both directions;
- a helper builds the raw PDF here, as pyropdf has no ``conftest`` helper;
- the file name, description, and dates are assigned and deleted in a test
  of their own, which pylopdf's file covers only through ``Attachment``.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

import pyropdf
from pyropdf import AttachmentIcon, Color, FileAttachment, Rect, _core

WHITE = (255, 255, 255)


def build_raw_pdf(objects: dict[int, str]) -> bytes:
    """Return a PDF of ``objects`` by number, with a correct cross-reference table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode()
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 00000 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _pixel(pixmap: pyropdf.Pixmap, x: float, y: float) -> tuple[int, int, int]:
    offset = int(y) * pixmap.stride + int(x) * pixmap.channels
    red, green, blue = bytes(pixmap)[offset : offset + 3]
    return red, green, blue


@pytest.mark.parametrize("icon", list(AttachmentIcon))
def test_file_attachments_embed_their_file(icon: AttachmentIcon) -> None:
    page = pyropdf.Document().pages.new((200, 200))

    annotation = page.annotations.add(
        FileAttachment((20, 30), b"payload", filename="data.bin", description="Raw", icon=icon, color=Color(0, 1, 0))
    )

    assert annotation.type is pyropdf.AnnotationType.FILE_ATTACHMENT
    assert annotation.rect == Rect(20, 30, 40, 50)
    assert annotation.icon == "".join(part.capitalize() for part in icon.value.split("_"))
    attached = annotation.file
    assert attached is not None
    assert (attached.filename, attached.description, attached.size) == ("data.bin", "Raw", 7)
    assert attached.read() == b"payload"
    pixmap = page.pixmap(dpi=72)
    inside = {_pixel(pixmap, x, y) for x in range(20, 40) for y in range(30, 50)}
    assert inside != {WHITE}
    # An annotation's file is not one of the document's attachments.
    assert len(page.document.attachments) == 0


def test_attached_files_survive_save_and_are_replaced() -> None:
    page = pyropdf.Document().pages.new((200, 200))
    page.rotation = 270
    page.annotations.add(FileAttachment((20, 30), bytearray(b"first"), filename="note.txt"))

    reopened = pyropdf.open(page.document.to_bytes()).pages[0]
    (annotation,) = reopened.annotations
    assert annotation.rotation == 0
    attached = annotation.file
    assert attached is not None
    assert attached.read() == b"first"
    assert attached.description is None

    attached.write(memoryview(b"second and longer"))

    assert attached.read() == b"second and longer"
    assert attached.size == 17
    assert attached.filename == "note.txt"
    again = pyropdf.open(reopened.document.to_bytes()).pages[0].annotations[0].file
    assert again is not None
    assert again.read() == b"second and longer"


def test_attached_files_are_bounded() -> None:
    page = pyropdf.Document().pages.new((200, 200))

    with pytest.raises(pyropdf.LimitError) as raised:
        FileAttachment((0, 0), b"12345", filename="x", max_size=4)
    assert raised.value.code == "embedded_file_size"
    with pytest.raises(pyropdf.LimitError) as raised:
        FileAttachment((0, 0), b"", filename="x" * (1024 * 1024 + 1))
    assert raised.value.code == "embedded_file_input_size"
    with pytest.raises(TypeError, match="data must be bytes"):
        FileAttachment((0, 0), cast("Any", "text"), filename="x")
    with pytest.raises(TypeError, match="filename"):
        FileAttachment((0, 0), b"", filename=cast("Any", None))
    with pytest.raises(ValueError, match="'clip'"):
        FileAttachment((0, 0), b"", filename="x", icon=cast("Any", "clip"))
    annotation = page.annotations.add(FileAttachment((0, 0), b"12345", filename="x"))
    attached = annotation.file
    assert attached is not None
    with pytest.raises(pyropdf.LimitError):
        attached.read(max_size=4)
    with pytest.raises(pyropdf.LimitError):
        attached.write(b"123456789", max_size=8)
    assert attached.read() == b"12345"
    location = (page.document._core(), page._number, page._generation)  # noqa: SLF001
    spec = {"subtype": "FileAttachment", "rect": (0.0, 0.0, 9.0, 9.0), "data": b"123", "filename": "f"}
    with pytest.raises(pyropdf.LimitError) as raised:
        _core.annotation_add(*location, {**spec, "max_size": 2})
    assert raised.value.code == "embedded_file_size"
    with pytest.raises(pyropdf.PdfError, match="unknown file attachment icon"):
        _core.annotation_add(*location, {**spec, "icon": "Clip"})
    with pytest.raises(pyropdf.PdfError, match="embeds data with a file name"):
        _core.annotation_add(*location, {**spec, "subtype": "Square"})
    with pytest.raises(pyropdf.PdfError, match="embeds data with a file name"):
        _core.annotation_add(*location, {"subtype": "FileAttachment", "rect": (0.0, 0.0, 9.0, 9.0)})
    assert len(page.annotations) == 1


def test_oversized_filename_is_refused_before_encoding() -> None:
    class OversizedName(str):
        __slots__ = ()

        def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:  # noqa: ARG002
            msg = "oversized input must not be encoded"
            raise AssertionError(msg)

    with pytest.raises(pyropdf.LimitError) as raised:
        FileAttachment((0, 0), b"", filename=OversizedName("x" * (1024 * 1024 + 1)))
    assert raised.value.code == "embedded_file_input_size"


def test_other_annotations_have_no_file() -> None:
    page = pyropdf.Document().pages.new((200, 200))
    note = page.annotations.add(pyropdf.Note((10, 10)))

    assert note.file is None


def test_a_direct_filespec_is_read_and_replaced() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: (
                    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Annots [<< /Type /Annot "
                    "/Subtype /FileAttachment /Rect [10 10 30 30] /Name /Tag "
                    "/FS << /Type /Filespec /F (a.txt) /EF << /F 4 0 R >> >> >>] >>"
                ),
                4: "<< /Type /EmbeddedFile /Length 3 >>\nstream\nabc\nendstream",
            }
        )
    )
    (annotation,) = doc.pages[0].annotations
    attached = annotation.file
    assert attached is not None
    assert (attached.filename, attached.size, attached.read()) == ("a.txt", -1, b"abc")

    attached.write(b"xyz!")

    assert annotation.xref > 0
    assert attached.read() == b"xyz!"
    assert annotation.obj["FS"]["F"] == b"a.txt"
    # Rendering draws the icon that the file attachment has no appearance for.
    assert bytes(doc.pages[0].pixmap(dpi=72)) != b"\xff" * (200 * 100 * 3)


def test_failed_edit_keeps_direct_annotation_handle_usable() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: (
                    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Annots [<< /Type /Annot "
                    "/Subtype /FileAttachment /Rect [10 10 30 30] /FS << /F (a.txt) >> >>] >>"
                ),
            }
        )
    )
    annotation = doc.pages[0].annotations[0]
    attached = annotation.file
    assert attached is not None
    before = doc.history.position

    with pytest.raises(pyropdf.PdfError, match="no embedded file stream"):
        attached.created = "D:20260101120000Z"

    assert doc.history.position == before
    assert annotation.xref == 0
    assert attached.filename == "a.txt"


def test_the_file_name_description_and_dates_are_edited() -> None:
    page = pyropdf.Document().pages.new((200, 200))
    annotation = page.annotations.add(FileAttachment((20, 30), b"data", filename="a.txt"))
    attached = annotation.file
    assert attached is not None
    assert attached.created is not None
    assert attached.created == attached.modified
    assert repr(attached) == "AnnotationFile('a.txt')"

    attached.filename = "b.txt"
    attached.description = "Notes"
    attached.created = "D:20260101120000Z"
    del attached.modified

    assert (attached.filename, attached.description) == ("b.txt", "Notes")
    assert (attached.created, attached.modified) == ("D:20260101120000Z", None)
    assert annotation.obj["FS"]["F"] == b"b.txt"
    del attached.description
    assert attached.description is None
    with pytest.raises(ValueError, match="empty"):
        attached.filename = ""
    with pytest.raises(ValueError, match="PDF date"):
        attached.modified = "yesterday"
    with pytest.raises(TypeError, match="description"):
        attached.description = cast("Any", 1)

    del page.annotations[0]

    with pytest.raises(pyropdf.StaleObjectError):
        attached.read()
    assert repr(attached) == "<stale AnnotationFile>"
