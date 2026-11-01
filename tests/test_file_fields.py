"""The file fields of attachments: file name, description, and dates.

The attachment cases of pylopdf's tests/test_file_fields.py at ebb01d3c.
The annotation cases belong to #468. Intended differences:

- a helper builds the raw PDF here, as pyropdf has no ``conftest`` helper;
- the checks that pylopdf's core ``embfile_edit`` repeats are those of
  ``Attachment._edit_file``, since pyropdf edits through its object model and
  has no core method for this;
- a failed edit leaves ``doc.history.position`` unchanged, as pyropdf has no
  ``is_dirty``;
- a date must also start with ``D:``, as an annotation's file dates do.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

import pyropdf
from pyropdf import LimitError, PdfError


def build_raw_pdf(objects: dict[int, str | bytes]) -> bytes:
    """Return a PDF of ``objects`` by number, with a correct cross-reference table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        body = objects[number]
        out += f"{number} 0 obj\n".encode() + (body.encode() if isinstance(body, str) else body) + b"\nendobj\n"
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 00000 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _attachment_pdf(*, inline: bool) -> bytes:
    filespec = "<< /Type /Filespec /F (data.csv) /UF (data.csv) /Desc (Rows) /EF << /F 4 0 R >> >>"
    names = f"[(data) {filespec}]" if inline else "[(data) 5 0 R]"
    content = b"a,b"
    return build_raw_pdf(
        {
            1: f"<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << /Names {names} >> >> >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
            4: (
                b"<< /Type /EmbeddedFile /Params << /Size 3 /CreationDate (D:20260101000000Z) >> /Length 3 >>\n"
                b"stream\n" + content + b"\nendstream"
            ),
            5: filespec,
        }
    )


@pytest.mark.parametrize("inline", [False, True])
def test_attachment_fields_are_assignable(*, inline: bool) -> None:
    doc = pyropdf.open(_attachment_pdf(inline=inline))
    attachment = doc.attachments["data"]

    assert (attachment.created, attachment.modified) == ("D:20260101000000Z", None)
    attachment.filename = "données.csv"
    attachment.description = "Zeilen"
    attachment.created = "D:20250101000000+09'00'"
    attachment.modified = "D:20260301000000Z"

    for current in (doc, pyropdf.open(doc.to_bytes())):
        edited = current.attachments["data"]
        assert (edited.filename, edited.description) == ("données.csv", "Zeilen")
        assert (edited.created, edited.modified) == ("D:20250101000000+09'00'", "D:20260301000000Z")
        assert (edited.size, edited.read()) == (3, b"a,b")
        assert list(current.attachments) == ["data"]

    del attachment.description
    del attachment.created
    del attachment.modified
    assert (attachment.description, attachment.created, attachment.modified) == (None, None, None)


def test_writing_sets_the_modification_date() -> None:
    doc = pyropdf.open(_attachment_pdf(inline=False))
    attachment = doc.attachments["data"]

    attachment.write(b"x")

    assert attachment.modified is not None
    assert attachment.modified.startswith("D:2")
    assert attachment.created == "D:20260101000000Z"


def test_values_are_validated() -> None:
    doc = pyropdf.open(_attachment_pdf(inline=False))
    attachment = doc.attachments["data"]
    before = doc.history.position

    with pytest.raises(TypeError):
        attachment.created = 2026  # ty: ignore[invalid-assignment]
    with pytest.raises(ValueError, match="PDF date"):
        attachment.modified = "D:2026\n"
    with pytest.raises(ValueError, match="PDF date"):
        attachment.modified = "D" * 65
    with pytest.raises(ValueError, match="PDF date"):
        attachment.modified = "20260101000000Z"
    with pytest.raises(ValueError, match="empty"):
        attachment.filename = ""
    with pytest.raises(TypeError):
        attachment.description = None  # ty: ignore[invalid-assignment]
    with pytest.raises(LimitError) as caught:
        attachment.description = "x" * (1024 * 1024 + 1)
    assert caught.value.code == "embedded_file_input_size"
    # The edit repeats the checks for any caller, as pylopdf's core does.
    edit_file = cast("Any", attachment)._edit_file  # noqa: SLF001 - the edit repeats the checks.
    edits: list[dict[str, Any]] = [
        {"filename": None},
        {"unknown": "x"},
        {"created": "bad\n"},
        {"filename": ""},
        {"description": 1},
        {"filename": "a", "description": "x" * (1024 * 1024)},
    ]
    for edit in edits:
        with pytest.raises(PdfError):
            edit_file(edit)
    assert doc.history.position == before
    assert (attachment.filename, attachment.description) == ("data.csv", "Rows")


def test_dates_need_an_embedded_file_stream() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: (
                    "<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << /Names "
                    "[(bare) << /Type /Filespec /F (bare.txt) >>] >> >> >>"
                ),
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
            }
        )
    )
    attachment = doc.attachments["bare"]

    attachment.description = "No stream"
    with pytest.raises(PdfError):
        attachment.created = "D:20200101000000Z"
    assert attachment.description == "No stream"


def test_the_file_name_follows_the_version() -> None:
    old = build_raw_pdf(
        {
            1: (
                "<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << /Names "
                "[(old) << /Type /Filespec /F (old.txt) >>] >> >> >>"
            ),
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
        }
    ).replace(b"%PDF-1.7", b"%PDF-1.4")
    doc = pyropdf.open(old)
    attachment = doc.attachments["old"]

    attachment.filename = "new.txt"
    filespec = doc.catalog["Names"]["EmbeddedFiles"]["Names"][1]
    assert (attachment.filename, "UF" in filespec) == ("new.txt", False)

    attachment.description = "Described"
    assert str(doc.catalog["Version"]) == "1.6"
