"""Where each char of a layout comes from (DESIGN.md sections 5 and 13).

A char that a page's own content streams show names the content stream
and the index in ``Page.operations`` of the operation that showed it.
"""

from __future__ import annotations

import pytest
from corpus import ASSETS, NAMES
from test_content import _page as build_text_pdf
from test_content import build_raw_pdf

import pyropdf

SHOWS_TEXT = {"Tj", "TJ", "'", '"'}
RESOURCES = "<< /Font << /F1 9 0 R >> /XObject << /X1 8 0 R >> /Properties << /Off 10 0 R >> >>"


def _stream(data: str, entries: str = "") -> str:
    return f"<< /Length {len(data)}{entries} >>\nstream\n{data}\nendstream"


def build_provenance_pdf(*streams: str) -> bytes:
    """Build one page whose content is ``streams`` and whose annotation shows "Note".

    The resources hold the font ``/F1``, a Form XObject ``/X1`` that shows
    "Form", and an optional content group ``/Off`` that is hidden.
    """
    contents = " ".join(f"{11 + index} 0 R" for index in range(len(streams)))
    form = f" /Type /XObject /Subtype /Form /BBox [0 0 300 300] /Resources {RESOURCES}"
    objects: dict[int, str | bytes] = {
        1: "<< /Type /Catalog /Pages 2 0 R /OCProperties << /OCGs [10 0 R] /D << /OFF [10 0 R] >> >> >>",
        2: f"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 300 300] /Resources {RESOURCES} >>",
        3: f"<< /Type /Page /Parent 2 0 R /Contents [{contents}] /Annots [5 0 R] >>",
        5: "<< /Type /Annot /Subtype /Square /Rect [150 20 250 120] /AP << /N 7 0 R >> >>",
        7: _stream("BT /F1 10 Tf 5 50 Td (Note) Tj ET", form),
        8: _stream("BT /F1 10 Tf 150 280 Td (Form) Tj ET", form),
        9: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        10: "<< /Type /OCG /Name (Off) >>",
    }
    for index, stream in enumerate(streams):
        objects[11 + index] = _stream(stream)
    return build_raw_pdf(objects)


def _chars(page: pyropdf.Page) -> list[pyropdf.Char]:
    layout = page.layout(chars=True, unmapped="replacement")
    return [char for block in layout.blocks for line in block.lines for span in line.spans for char in span.chars]


def _sources(page: pyropdf.Page) -> list[tuple[str, int | None, int | None]]:
    """Return the text, stream, and operation of each char but the synthetic spaces, in layout order."""
    return [(char.text, char.stream, char.operation) for char in _chars(page) if not char.is_synthetic]


def _shows(page: pyropdf.Page) -> list[int]:
    """Return the indexes of the page's text-showing operations."""
    return [index for index, operation in enumerate(page.operations) if operation.operator in SHOWS_TEXT]


def test_chars_name_the_stream_and_operation_that_showed_them() -> None:
    page = pyropdf.open(
        build_provenance_pdf(
            "BT /F1 20 Tf 20 250 Td (Ab) Tj 0 -30 Td [(C) -2000 (D)] TJ ET",
            "BT /F1 20 Tf 30 TL 20 150 Td 2 Tr (E) ' 3 Tr 1 2 (G) \" ET",
        )
    ).pages[0]
    tj, tj_array, quote, double_quote = _shows(page)

    assert sorted(_sources(page), key=str) == sorted(
        [
            ("A", 11, tj),
            ("b", 11, tj),
            ("C", 11, tj_array),
            ("D", 11, tj_array),
            # Filled and stroked text, and invisible text, are the page's own.
            ("E", 12, quote),
            ("G", 12, double_quote),
            # An annotation's glyphs are not.
            *((char, None, None) for char in "Note"),
        ],
        key=str,
    )
    synthetic = [char for char in _chars(page) if char.is_synthetic]
    assert synthetic
    assert all(char.stream is None and char.operation is None for char in synthetic)


def test_runs_inside_a_form_and_hidden_runs_do_not_shift_the_sources() -> None:
    page = pyropdf.open(
        build_provenance_pdf(
            "BT /F1 20 Tf 20 250 Td (Ab) Tj ET /X1 Do"
            " /OC /Off BDC BT /F1 20 Tf 20 220 Td (Hidden) Tj ET EMC"
            " BT /F1 20 Tf 20 150 Td (Cd) Tj ET"
        )
    ).pages[0]
    first, _hidden, last = _shows(page)

    sources = {text: (stream, operation) for text, stream, operation in _sources(page) if stream is not None}

    assert sources == {"A": (11, first), "b": (11, first), "C": (11, last), "d": (11, last)}


def test_a_content_that_uses_the_marking_tags_has_no_sources() -> None:
    page = pyropdf.open(build_provenance_pdf("/pyropdf.operation.3 BMC BT /F1 20 Tf 20 250 Td (Ab) Tj ET EMC")).pages[0]

    assert page.text().startswith("Ab")
    assert all(char.operation is None for char in _chars(page))


@pytest.mark.parametrize("prefix", ["", "EMC ", "/pyropdf.operation.form BMC "])
def test_a_form_cannot_claim_a_page_operation_with_a_marking_tag(prefix: str) -> None:
    document = pyropdf.open(build_provenance_pdf("BT /F1 20 Tf 20 250 Td (A) Tj ET /X1 Do"))
    page = document.pages[0]
    operation = _shows(page)[0]
    document.objects[8].write(
        f"{prefix}/pyropdf.operation.{operation} BMC BT /F1 10 Tf 150 280 Td (Form) Tj ET EMC".encode()
    )

    sources = _sources(page)

    assert ("A", 11, operation) in sources
    assert ("F", None, None) in sources
    assert all(stream is None and index is None for text, stream, index in sources if text in "Form")


@pytest.mark.parametrize(
    ("limits", "code"),
    [
        (pyropdf.DocumentLimits(max_page_content_size=16), "page_content_size"),
        (pyropdf.DocumentLimits(max_decompressed_size=16), "decompressed_size"),
        (pyropdf.DocumentLimits(max_total_decompressed_size=16), "total_decompressed_size"),
    ],
)
def test_provenance_propagates_content_reading_limits(limits: pyropdf.DocumentLimits, code: str) -> None:
    document = pyropdf.open(build_text_pdf(b"BT /F1 20 Tf 20 250 Td (Ab) Tj ET", filtered=True), limits=limits)
    with pytest.raises(pyropdf.LimitError) as operations_error:
        _ = document.pages[0].operations
    assert operations_error.value.code == code
    for _ in range(2):
        with pytest.raises(pyropdf.LimitError) as error:
            document.pages[0].layout(chars=True)
        assert error.value.code == code


def test_sources_follow_an_edit_and_leave_words_and_text_unchanged() -> None:
    document = pyropdf.open(build_provenance_pdf("BT /F1 20 Tf 20 250 Td (Ab) Tj ET"))
    page = document.pages[0]
    words, text = page.words(), page.text()
    assert _sources(page)[0] == ("A", 11, 3)

    page.obj["Contents"][0].write(b"q Q BT /F1 20 Tf 20 250 Td (Ab) Tj ET")

    assert _sources(page)[0] == ("A", 11, 5)
    assert page.words() == words
    assert page.text() == text


@pytest.mark.parametrize("name", NAMES)
def test_every_corpus_source_names_a_text_operation_of_the_page(name: str) -> None:
    document = pyropdf.open((ASSETS / name).read_bytes())
    for page in list(document.pages)[:3]:
        operations = list(page.operations)
        contents = page.obj.get("Contents")
        streams = {contents.xref} if isinstance(contents, pyropdf.Stream) else {item.xref for item in contents or ()}
        for char in _chars(page):
            if char.operation is None:
                assert char.stream is None
                continue
            assert operations[char.operation].operator in SHOWS_TEXT
            assert char.stream in streams
