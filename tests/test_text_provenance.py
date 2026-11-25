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


def test_redraw_refuses_a_declared_program_that_only_loads_with_a_substitute() -> None:
    doc = pyropdf.open(build_provenance_pdf("BT /F1 16 Tf 20 100 Td (Text) Tj ET"))
    font = doc.objects[9]
    font["Subtype"] = pyropdf.Name("TrueType")
    font["BaseFont"] = pyropdf.Name("UnavailableFace")
    font["FirstChar"], font["LastChar"], font["Widths"] = 32, 127, [500] * 96
    program = doc.objects.add_stream(b"not an installed font")
    descriptor = doc.objects.add(
        {
            "Type": pyropdf.Name("FontDescriptor"),
            "FontName": pyropdf.Name("UnavailableFace"),
            "FontFile2": doc.objects[program],
        }
    )
    font["FontDescriptor"] = doc.objects[descriptor]
    run = doc.pages[0].content().text_runs[0]
    target = pyropdf.Document()
    blank = target.pages.new((200, 200))
    before = target.to_bytes(), target.history.steps
    with blank.draw() as canvas, pytest.raises(ValueError, match="substitute"):
        canvas.draw(run)
    assert (target.to_bytes(), target.history.steps) == before


def test_retrieved_synthetic_space_has_its_gap_box_and_no_painted_operator() -> None:
    with pyropdf.open(build_provenance_pdf("BT /F1 20 Tf 20 250 Td [(C) -2000 (D)] TJ ET")) as document:
        chars = _chars(document.pages[0])
        (gap,) = [char for char in chars if char.is_synthetic]
        sources = [
            s for c in document.to_chunks(provenance=True, table_strategy=None) for e in c.elements for s in e.sources
        ]
        (space,) = [s for s in sources if s.kind == "inferred" and s.bbox == gap.bbox]
        assert space.text == " "
        assert space.stream is space.operation is space.operation_sha256 is space.resolve() is None
        real = [s for s in sources if s.text in {"C", "D"}]
        assert len(real) == 2
        assert real[0].operation_sha256 == real[1].operation_sha256
        assert real[0].operation_sha256 is not None


def test_identical_stored_operators_keep_their_distinct_streams_indexes_and_placements() -> None:
    import hashlib  # noqa: PLC0415

    with pyropdf.open(
        build_provenance_pdf("BT /F1 12 Tf 10 250 Td (Twin) Tj ET", "BT /F1 12 Tf 80 160 Td (Twin) Tj ET")
    ) as document:
        locations = [
            s
            for c in document.to_chunks(provenance=True, table_strategy=None, max_chars=2)
            for e in c.elements
            for s in e.sources
            if s.text == "T"
        ]
        expected = hashlib.sha256(b" (Twin) Tj").hexdigest()
        assert [(s.stream, s.operation, s.operation_sha256) for s in locations] == [
            (11, 3, expected),
            (12, 8, expected),
        ]
        boxes = [s.bbox for s in locations]
        assert [box.x0 for box in boxes if box is not None] == [10, 80]
        assert locations[0].version.same_as(locations[1].version)


def _twice_nested_form() -> bytes:
    """Build two placements that reach the same inner Form through an outer Form."""
    program = "BT /F1 16 Tf 10 270 Td (Header) Tj ET q 1 0 0 1 20 180 cm /Outer Do Q q 1 0 0 1 95 90 cm /Outer Do Q"
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300]"
            " /Resources << /Font << /F1 4 0 R >> /XObject << /Outer 6 0 R >> >> /Contents 5 0 R >>",
            4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            5: _stream(program),
            6: _stream(
                "q 1 0 0 1 2 4 cm /Inner Do Q",
                " /Type /XObject /Subtype /Form /BBox [0 0 100 40] /Resources << /XObject << /Inner 7 0 R >> >>",
            ),
            7: _stream(
                "BT /F1 12 Tf 3 8 Td (Echo) Tj ET",
                " /Type /XObject /Subtype /Form /BBox [0 0 100 40] /Resources << /Font << /F1 4 0 R >> >>",
            ),
        }
    )


def test_retrieval_keeps_independently_resolved_repeated_form_locations() -> None:
    import io  # noqa: PLC0415

    import pikepdf  # noqa: PLC0415

    data = _twice_nested_form()
    # Independent syntax and CTMs establish the object identities and both display positions.
    with pikepdf.open(io.BytesIO(data)) as control:
        page = control.pages[0]
        top = pikepdf.parse_content_stream(page)
        outer = page.Resources.XObject.Outer
        inner = outer.Resources.XObject.Inner
        assert (outer.objgen, inner.objgen) == ((6, 0), (7, 0))
        assert [str(op.operands[0]) for op in top if str(op.operator) == "Do"] == ["/Outer", "/Outer"]
        outer_ops = pikepdf.parse_content_stream(outer)
        assert [str(op.operands[0]) for op in outer_ops if str(op.operator) == "Do"] == ["/Inner"]
        inner_ops = pikepdf.parse_content_stream(inner)
        assert [str(op.operands[0]) for op in inner_ops if str(op.operator) == "Tj"] == ["Echo"]
        text_origin = next(op.operands for op in inner_ops if str(op.operator) == "Td")
        offset = next(op.operands for op in outer_ops if str(op.operator) == "cm")
        top_translations = [op.operands for op in top if str(op.operator) == "cm"]
        expected = [
            (float(cm[4] + offset[4] + text_origin[0]), 300 - float(cm[5] + offset[5] + text_origin[1]))
            for cm in top_translations
        ]

    with pyropdf.open(data) as document:
        elements = [
            element for chunk in document.to_chunks(provenance=True, table_strategy=None) for element in chunk.elements
        ]
        header = next(e for e in elements if e.text == "Header")
        for src in header.sources:
            op = src.resolve()
            assert src.kind == "pdf"
            assert src.stream == 5
            assert op is not None
            assert op.operator == "Tj"
        e_glyphs = [src for e in elements for src in e.sources if src.text == "E"]
        assert len(e_glyphs) == 2
        for glyph, (x, baseline) in zip(e_glyphs, expected, strict=True):
            assert glyph.kind == "unresolved"
            assert glyph.stream is glyph.operation is glyph.resolve() is None
            assert glyph.bbox is not None
            assert glyph.bbox.x0 == pytest.approx(x)
            assert glyph.bbox.y0 < baseline < glyph.bbox.y1
