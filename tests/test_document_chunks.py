"""Document.to_json and Document.to_chunks: layout elements as JSON, and retrieval chunks built from them."""

from __future__ import annotations

import json

import pytest
from test_content import build_raw_pdf

import pyropdf


def _stream(data: str | bytes, extra: str = "") -> bytes:
    raw = data.encode() if isinstance(data, str) else data
    return f"<< /Length {len(raw)} {extra}>>\nstream\n".encode() + raw + b"\nendstream"


# A heading, a two-line paragraph, a list, a bordered table, an image, and a paragraph after it.
FIRST = (
    "BT /F1 18 Tf 40 360 Td (Introduction) Tj ET\n"
    "BT /F1 10 Tf 40 340 Td (First paragraph line one.) Tj 0 -12 Td (Line two of it.) Tj ET\n"
    "BT /F1 10 Tf 40 300 Td (- apples) Tj 0 -12 Td (- pears) Tj ET\n"
    "0.5 w 40 250 m 240 250 l S 40 230 m 240 230 l S 40 210 m 240 210 l S\n"
    "40 250 m 40 210 l S 140 250 m 140 210 l S 240 250 m 240 210 l S\n"
    "BT /F1 10 Tf 45 236 Td (Name) Tj 100 0 Td (Qty) Tj ET\n"
    "BT /F1 10 Tf 45 216 Td (Bolt) Tj 100 0 Td (12) Tj ET\n"
    "q 50 0 0 30 40 150 cm /Im1 Do Q\n"
    "BT /F1 10 Tf 40 120 Td (After the image.) Tj ET\n"
)

# A smaller heading and one paragraph of forty words over eight lines.
WORDS = [f"word{index}" for index in range(40)]
SECOND = (
    "BT /F1 14 Tf 40 360 Td (Details) Tj ET\nBT /F1 10 Tf 40 340 Td "
    + " 0 -12 Td ".join(f"({' '.join(WORDS[line * 5 : line * 5 + 5])}) Tj" for line in range(8))
    + " ET\n"
)


def fixture_bytes(first: str = FIRST, second: str = SECOND) -> bytes:
    """Return the two-page fixture; evidence/document-chunks/compare.py reads it too."""
    pixels = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0])
    image = "/Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB /BitsPerComponent 8 "
    resources = "<< /Font << /F1 4 0 R >> /XObject << /Im1 5 0 R >> >>"
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: f"<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 /MediaBox [0 0 300 400] /Resources {resources} >>",
            3: "<< /Type /Page /Parent 2 0 R /Contents 7 0 R >>",
            4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            5: _stream(pixels, image),
            6: "<< /Type /Page /Parent 2 0 R /Contents 8 0 R >>",
            7: _stream(first),
            8: _stream(second),
        }
    )


def _document(first: str = FIRST, second: str = SECOND) -> pyropdf.Document:
    return pyropdf.open(fixture_bytes(first, second))


def test_json_lists_each_page_with_its_size_and_elements_in_reading_order() -> None:
    data = json.loads(_document().to_json())

    assert [page["index"] for page in data["pages"]] == [0, 1]
    assert (data["pages"][0]["width"], data["pages"][0]["height"]) == (300.0, 400.0)
    first = data["pages"][0]["elements"]
    assert [element["kind"] for element in first] == [
        "heading",
        "paragraph",
        "list_item",
        "list_item",
        "table",
        "image",
        "paragraph",
    ]
    assert [element["text"] for element in first] == [
        "Introduction",
        "First paragraph line one. Line two of it.",
        "- apples",
        "- pears",
        "| Name | Qty |\n| --- | --- |\n| Bolt | 12 |",
        "",
        "After the image.",
    ]
    assert all(element["page"] == 0 for element in first)
    assert first[0]["level"] == 1
    assert data["pages"][1]["elements"][0] == {**data["pages"][1]["elements"][0], "kind": "heading", "level": 2}


def test_json_boxes_are_in_display_space() -> None:
    elements = json.loads(_document().to_json())["pages"][0]["elements"]
    table = next(element for element in elements if element["kind"] == "table")
    image = next(element for element in elements if element["kind"] == "image")

    # Display space has its origin at the top left, so the table's lines at y 250 and 210 become 150 and 190.
    assert table["bbox"] == pytest.approx([40.0, 150.0, 240.0, 190.0])
    assert table["rows"] == [["Name", "Qty"], ["Bolt", "12"]]
    assert image["bbox"] == pytest.approx([40.0, 220.0, 90.0, 250.0])
    paragraph = elements[1]
    assert paragraph["bbox"][0] == pytest.approx(40.0)
    assert paragraph["bbox"][1] < paragraph["bbox"][3] < table["bbox"][1]


def test_json_selects_pages_and_is_stable() -> None:
    document = _document()

    selected = json.loads(document.to_json([1]))
    assert [page["index"] for page in selected["pages"]] == [1]
    assert document.to_json() == document.to_json()


def test_json_keeps_text_that_is_not_ascii() -> None:
    document = _document(second="BT /F1 14 Tf 40 360 Td (Caf\\351) Tj ET\n")

    assert '"Café"' in document.to_json()


def test_chunks_keep_headings_tables_and_sections() -> None:
    chunks = _document().to_chunks(max_chars=2000)

    assert [chunk.index for chunk in chunks] == [0, 1]
    assert chunks[0].text.startswith("# Introduction\n\nFirst paragraph line one. Line two of it.\n\n- apples\n- pears")
    assert "| Name | Qty |\n| --- | --- |\n| Bolt | 12 |\n\nAfter the image." in chunks[0].text
    assert chunks[0].section == ("Introduction",)
    assert chunks[0].pages == (0,)
    assert chunks[1].text.startswith("## Details\n\nword0 word1")
    assert chunks[1].section == ("Introduction", "Details")
    assert chunks[1].pages == (1,)
    assert not any(chunk.oversized for chunk in chunks)


def test_chunks_split_long_paragraphs_at_whitespace() -> None:
    document = _document()
    chunks = document.to_chunks([1], max_chars=60)

    assert all(len(chunk.text) <= 60 for chunk in chunks)
    # Heading levels come from the selected pages, as in markdown().
    assert chunks[0].text == document.markdown([1]).split("\n")[0] == "# Details"
    words = " ".join(chunk.text for chunk in chunks[1:]).split()
    assert words == WORDS
    assert all(chunk.section == ("Details",) for chunk in chunks)


def test_a_table_stays_whole_and_marks_its_chunk_oversized() -> None:
    chunks = _document().to_chunks([0], max_chars=30)

    tables = [chunk for chunk in chunks if any(element.kind == "table" for element in chunk.elements)]
    assert len(tables) == 1
    assert tables[0].text == "| Name | Qty |\n| --- | --- |\n| Bolt | 12 |"
    assert tables[0].oversized
    assert [chunk.oversized for chunk in chunks if chunk is not tables[0]] == [False] * (len(chunks) - 1)


def test_chunks_carry_their_elements_and_serialize_to_json() -> None:
    chunk = _document().to_chunks([0])[0]

    assert isinstance(chunk, pyropdf.Chunk)
    assert all(isinstance(element, pyropdf.Element) for element in chunk.elements)
    data = json.loads(json.dumps(chunk.to_dict()))
    assert data["section"] == ["Introduction"]
    assert data["elements"][0]["kind"] == "heading"
    assert data["text"] == chunk.text


def test_an_image_without_text_joins_a_chunk_without_changing_its_text() -> None:
    chunks = _document().to_chunks([0])

    kinds = [element.kind for element in chunks[0].elements]
    assert "image" in kinds
    assert "\n\n\n" not in chunks[0].text


def test_max_size_bounds_the_export() -> None:
    document = _document()

    with pytest.raises(pyropdf.LimitError) as raised:
        document.to_json(max_size=20)
    assert raised.value.code == "export_output_size"
    with pytest.raises(pyropdf.LimitError):
        document.to_chunks(max_size=20)


@pytest.mark.parametrize(("value", "error"), [(0, ValueError), (-1, ValueError), (True, TypeError), (1.5, TypeError)])
def test_max_chars_must_be_a_positive_integer(value: object, error: type[Exception]) -> None:
    with pytest.raises(error, match="max_chars"):
        _document().to_chunks(max_chars=value)  # ty: ignore[invalid-argument-type]


def test_table_strategy_is_checked() -> None:
    with pytest.raises(ValueError, match="table_strategy"):
        _document().to_json(table_strategy="grid")  # ty: ignore[invalid-argument-type]
