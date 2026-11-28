"""Document.to_json and Document.to_chunks: layout elements as JSON, and retrieval chunks built from them."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from test_content import build_raw_pdf
from test_markdown import _rich_tagged_pdf

import pyropdf
from pyropdf import _markdown, _structure_reading

_PDFUA_ASSETS = Path(__file__).parent / "assets" / "pdfua"

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from pyropdf._markdown import TextPageDict


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


_EXPORTS = (pyropdf.Document.markdown, pyropdf.Document.to_json, pyropdf.Document.to_chunks)


@pytest.mark.parametrize("export", _EXPORTS)
def test_export_keeps_one_version_while_a_writer_commits(
    export: Callable[[pyropdf.Document], object], monkeypatch: pytest.MonkeyPatch
) -> None:
    old = "BT /F1 12 Tf 40 300 Td (old) Tj ET"
    new = old.replace("old", "new")
    document = _document(first=old)
    document.pages[1].obj["Contents"] = document.pages[0].obj["Contents"]
    before = export(document)
    layout = _markdown._page_layout  # noqa: SLF001 - schedule the commit between actual page reads.
    reads = 0

    with ThreadPoolExecutor(max_workers=1) as writer:

        def layout_during_commit(page: pyropdf.Page) -> tuple[TextPageDict, list[pyropdf.Word]]:
            nonlocal reads
            result = layout(page)
            reads += 1
            # After the heading pass and the first output page, replace the shared stream.
            if reads == 3:
                writer.submit(document.objects[7].write, new.encode()).result(timeout=10)
            return result

        with monkeypatch.context() as patch:
            patch.setattr(_markdown, "_page_layout", layout_during_commit)
            during = export(document)

    assert during == before
    after = export(document)
    assert after != before
    assert after == export(_document(first=new, second=new))


@pytest.mark.parametrize("export", _EXPORTS)
def test_export_selects_pages_from_its_starting_version(
    export: Callable[[pyropdf.Document, Iterable[int]], object],
) -> None:
    document = _document()
    expected = export(document, [-1, 0, 1])

    def selection() -> Iterator[int]:
        yield -1
        del document.pages[0]
        yield 0
        yield 1

    assert export(document, selection()) == expected
    assert len(document.pages) == 1


@pytest.mark.parametrize("export", _EXPORTS)
def test_export_reads_the_callers_uncommitted_edits(export: Callable[[pyropdf.Document], object]) -> None:
    document = _document()
    before = export(document)
    with document.transaction():
        document.objects[7].write(b"BT /F1 12 Tf 40 300 Td (draft) Tj ET")
        draft = export(document)
        assert draft != before
    assert export(document) == draft


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


@pytest.mark.parametrize(
    ("filename", "expected_alt"),
    [
        ("pdfua-1.pdf", "A blue rectangle crossed by a red line"),
        ("pdfua-2.pdf", "Water level rose at each of three readings; the final level was three times the first."),
        (
            "pdfua-2-pdfa-4.pdf",
            "Water level rose at each of three readings; the final level was three times the first.",
        ),
    ],
)
@pytest.mark.spec("tagged-pdf", "interpret")
def test_json_and_chunks_read_pdfua_figures_with_alt_bbox_and_language(filename: str, expected_alt: str) -> None:
    with pyropdf.open(_PDFUA_ASSETS / filename) as doc:
        data = json.loads(doc.to_json())
        elements = data["pages"][0]["elements"]
        assert len(elements) == 1
        fig = elements[0]
        assert fig["kind"] == "image"
        assert fig["text"] == expected_alt
        assert fig["alt_text"] == expected_alt
        assert fig["language"] == "en-US"
        assert fig["bbox"][2] > fig["bbox"][0]
        assert fig["bbox"][3] > fig["bbox"][1]

        chunks = doc.to_chunks()
        assert len(chunks) == 1
        assert chunks[0].text == f"![{expected_alt}]()"
        assert chunks[0].language == "en-US"
        assert chunks[0].elements[0].alt_text == expected_alt
        assert chunks[0].to_dict()["language"] == "en-US"


@pytest.mark.spec("tagged-pdf", "interpret")
def test_json_and_chunks_preserve_structure_semantics_and_language_metadata() -> None:
    with pyropdf.open(_rich_tagged_pdf()) as doc:
        data = json.loads(doc.to_json(expand_abbreviations=True))
        elements = data["pages"][0]["elements"]
        kinds = [element["kind"] for element in elements]
        assert kinds == [
            "heading",
            "paragraph",
            "heading",
            "paragraph",
            "list_item",
            "list_item",
            "list_item",
            "blockquote",
            "code",
            "table",
            "paragraph",
            "image",
        ]
        assert elements[0]["level"] == 1
        assert elements[2]["level"] == 2
        assert elements[0]["language"] == "en-US"
        quote = next(e for e in elements if e["kind"] == "blockquote")
        assert quote["language"] == "fr"
        assert quote["text"] == "Quoted line"
        table = next(e for e in elements if e["kind"] == "table")
        assert table["rows"] == [["Metric", None], ["100", "OK"]]
        assert "| Metric | Metric |" in table["text"]
        assert elements[-2]["text"] == "Application Programming Interface"
        assert elements[-1]["alt_text"] == "Blue bar chart"

        chunks = doc.to_chunks(max_chars=2000)
        assert len(chunks) == 2
        assert chunks[0].section == ("Main Title",)
        assert chunks[0].language == "en-US"
        assert chunks[1].section == ("Main Title", "Nested Heading")
        assert "> Quoted line" in chunks[1].text
        assert "```\nprint(1)\n```" in chunks[1].text
        assert "![Blue bar chart]()" in chunks[1].text

        # Oversized code block stays whole and marks oversized=True.
        tiny_chunks = doc.to_chunks(max_chars=12)
        code_chunks = [c for c in tiny_chunks if any(e.kind == "code" for e in c.elements)]
        assert len(code_chunks) == 1
        assert code_chunks[0].oversized

        # max_size enforces export_output_size on tagged documents too.
        with pytest.raises(pyropdf.LimitError) as caught:
            doc.to_json(max_size=10)
        assert caught.value.code == "export_output_size"


@pytest.mark.parametrize("export", _EXPORTS)
def test_tagged_export_keeps_one_version_while_writer_mutates_structure_and_content(
    export: Callable[[pyropdf.Document], object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = pyropdf.open(_rich_tagged_pdf())
    before = export(document)
    index_cls = _structure_reading._PageMcidIndex  # noqa: SLF001
    orig_scan = index_cls._scan_operations  # noqa: SLF001
    mutated = False

    with ThreadPoolExecutor(max_workers=1) as writer:

        def scan_during_commit(
            self_idx: _structure_reading._PageMcidIndex,
            page: pyropdf.Page,
            budget: pyropdf._core._OperationBudget,
            *,
            expand: bool,
        ) -> tuple[dict[int, int], set[int], dict[int, list[int]]]:
            nonlocal mutated
            result = orig_scan(self_idx, page, budget, expand=expand)
            if not mutated:
                mutated = True

                def mutate_doc() -> None:
                    with document.transaction("concurrent edit"):
                        document.structure[0].children[0].actual_text = "Mutated Heading"
                        stream = document.objects[5]
                        assert isinstance(stream, pyropdf.Stream)
                        stream.write(stream.read().replace(b"Second logically", b"Mutated body text"))

                writer.submit(mutate_doc).result(timeout=10)
            return result

        with monkeypatch.context() as patch:
            patch.setattr(index_cls, "_scan_operations", scan_during_commit)
            during = export(document)

    assert during == before
    after = export(document)
    assert after != before
    document.close()


def test_tagged_export_enforces_cancellation_and_memory_limits() -> None:
    pdf = _rich_tagged_pdf()
    token = pyropdf.CancellationToken()
    with pyropdf.open(pdf, limits=pyropdf.DocumentLimits(cancellation=token)) as doc:
        token.cancel()
        for call in (doc.markdown, doc.to_json, doc.to_chunks):
            with pytest.raises(pyropdf.LimitError) as caught:
                call()
            assert caught.value.code == "cancelled"

    with pyropdf.open(pdf, limits=pyropdf.DocumentLimits(max_memory_size=8192)) as doc:
        with pytest.raises(pyropdf.LimitError) as caught:
            doc.to_chunks()
        assert caught.value.code == "memory_size"


def test_json_and_chunks_apply_margins_on_upright_and_rotated_pages() -> None:
    pixels = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0])
    img_hdr = "/Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB /BitsPerComponent 8 "
    stream = (
        "BT /F1 10 Tf 120 380 Td (TopMarginHeader) Tj ET\n"
        "q 20 0 0 20 100 370 cm /Im1 Do Q\n"
        "BT /F1 10 Tf 10 200 Td (SideBarText) Tj ET\n"
        "BT /F1 16 Tf 100 260 Td (Main Heading) Tj ET\n"
        "BT /F1 10 Tf 100 220 Td (Body content here.) Tj ET\n"
        "q 30 0 0 30 100 120 cm /Im1 Do Q\n"
        "BT /F1 10 Tf 120 15 Td (BottomMarginFooter) Tj ET\n"
    )
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: (
                "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 400 400] "
                "/Resources << /Font << /F1 4 0 R >> /XObject << /Im1 5 0 R >> >> >>"
            ),
            3: "<< /Type /Page /Parent 2 0 R /Contents 6 0 R >>",
            4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            5: _stream(pixels, img_hdr),
            6: _stream(stream),
        }
    )
    with pyropdf.open(pdf) as doc:
        data = json.loads(doc.to_json(margins=(65, 45, 65, 45)))
        elems = data["pages"][0]["elements"]
        texts = [e["text"] for e in elems]
        assert texts == ["Main Heading", "Body content here.", ""]
        assert [e["kind"] for e in elems] == ["heading", "paragraph", "image"]

        chunks = doc.to_chunks(margins=(65, 45, 65, 45))
        assert len(chunks) == 1
        assert "TopMarginHeader" not in chunks[0].text
        assert "SideBarText" not in chunks[0].text
        assert "BottomMarginFooter" not in chunks[0].text
        assert "# Main Heading" in chunks[0].text
        assert "Body content here." in chunks[0].text

        # Rotate 90 degrees: SideBarText moves to display top (y ~ 10..20) and is clipped by top margin.
        doc.pages[0].rotation = 90
        rotated_chunks = doc.to_chunks(margins=(0, 55, 0, 0))
        combined = "\n".join(c.text for c in rotated_chunks)
        assert "SideBarText" not in combined
        assert "Main Heading" in combined


def test_json_and_chunks_ignore_headers_footers() -> None:
    pages = [
        (
            "BT /F1 16 Tf 40 375 Td (First Page Title) Tj ET\n"
            "BT /F1 10 Tf 40 200 Td (Alpha body text.) Tj ET\n"
            "BT /F1 9 Tf 150 20 Td (Page 1 of 3) Tj ET\n"
        ),
        (
            "BT /F1 9 Tf 40 380 Td (Repeated Header) Tj ET\n"
            "BT /F1 10 Tf 40 200 Td (Beta body text.) Tj ET\n"
            "BT /F1 9 Tf 150 20 Td (Page 2 of 3) Tj ET\n"
        ),
        (
            "BT /F1 9 Tf 40 380 Td (Repeated Header) Tj ET\n"
            "BT /F1 10 Tf 40 200 Td (Gamma body text.) Tj ET\n"
            "BT /F1 9 Tf 150 20 Td (Page 3 of 3) Tj ET\n"
        ),
    ]
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: (
                "<< /Type /Pages /Kids [3 0 R 5 0 R 7 0 R] /Count 3 /MediaBox [0 0 400 400] "
                "/Resources << /Font << /F1 4 0 R >> >> >>"
            ),
            3: "<< /Type /Page /Parent 2 0 R /Contents 6 0 R >>",
            4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            5: "<< /Type /Page /Parent 2 0 R /Contents 8 0 R >>",
            6: _stream(pages[0]),
            7: "<< /Type /Page /Parent 2 0 R /Contents 9 0 R >>",
            8: _stream(pages[1]),
            9: _stream(pages[2]),
        }
    )
    with pyropdf.open(pdf) as doc:
        data = json.loads(doc.to_json(ignore_headers_footers=True))
        all_texts = [elem["text"] for page in data["pages"] for elem in page["elements"]]
        assert all_texts == ["First Page Title", "Alpha body text.", "Beta body text.", "Gamma body text."]

        chunks = doc.to_chunks(ignore_headers_footers=True)
        assert len(chunks) == 1
        assert chunks[0].section == ("First Page Title",)
        assert "Repeated Header" not in chunks[0].text
        assert "Page 1 of 3" not in chunks[0].text
        assert "Gamma body text." in chunks[0].text


def test_json_and_chunks_image_export_and_snapshot_consistency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    document = _document()
    try:
        # Default images=None writes nothing and omits "image" field from JSON elements.
        default_data = json.loads(document.to_json([0]))
        default_img = next(e for e in default_data["pages"][0]["elements"] if e["kind"] == "image")
        assert "image" not in default_img
        assert list(tmp_path.iterdir()) == []

        # Directory export writes stable PNG and sets Element.image in JSON and Chunk.text.
        json_dir = tmp_path / "json_imgs"
        exported_data = json.loads(document.to_json([0], images=json_dir))
        exported_img = next(e for e in exported_data["pages"][0]["elements"] if e["kind"] == "image")
        assert exported_img["image"] == "page-0-image-0.png"
        assert (json_dir / "page-0-image-0.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

        chunk_dir = tmp_path / "chunk_imgs"
        chunks = document.to_chunks([0], images=chunk_dir)
        assert "![](page-0-image-0.png)" in chunks[0].text
        img_elem = next(e for e in chunks[0].elements if e.kind == "image")
        assert img_elem.image == "page-0-image-0.png"
        assert (chunk_dir / "page-0-image-0.png").exists()

        # Callback export emits custom link with zero disk writes.
        cb_chunks = document.to_chunks([0], images=lambda img: f"s3://bucket/{img.filename}")
        assert "![](s3://bucket/page-0-image-0.png)" in cb_chunks[0].text

        # Atomic failure: max_size breach creates no files or directories.
        fail_dir = tmp_path / "fail_imgs"
        with pytest.raises(pyropdf.LimitError) as caught:
            document.to_json([0], images=fail_dir, max_size=10)
        assert caught.value.code == "export_output_size"
        assert not fail_dir.exists()

        # Snapshot consistency (#731): replacing image bytes concurrently during export keeps snapshot pixels.
        before_bytes: list[bytes] = []
        document.to_chunks([0], images=lambda img: (before_bytes.append(img.data), img.filename)[1])

        orig_layout = _markdown._page_layout  # noqa: SLF001
        mutated = False
        new_pixels = bytes([0, 0, 0] * 4)
        with ThreadPoolExecutor(max_workers=1) as writer:

            def mutate_during_export(page: pyropdf.Page) -> tuple[TextPageDict, list[pyropdf.Word]]:
                nonlocal mutated
                res = orig_layout(page)
                if not mutated:
                    mutated = True
                    writer.submit(document.objects[5].write, new_pixels).result(timeout=10)
                return res

            during_bytes: list[bytes] = []
            with monkeypatch.context() as patch:
                patch.setattr(_markdown, "_page_layout", mutate_during_export)
                document.to_chunks([0], images=lambda img: (during_bytes.append(img.data), img.filename)[1])

        after_bytes: list[bytes] = []
        document.to_chunks([0], images=lambda img: (after_bytes.append(img.data), img.filename)[1])
        assert during_bytes == before_bytes
        assert after_bytes != before_bytes
    finally:
        document.close()


def test_json_and_chunks_enforce_max_graphics() -> None:
    lines = "\n".join(f"10 {20 + i} m 290 {20 + i} l S" for i in range(120))
    stream = f"BT /F1 12 Tf 40 350 Td (Vector Map) Tj ET\n0.5 w\n{lines}\n"
    with _document(first=stream) as doc:
        for call in (doc.to_json, doc.to_chunks):
            with pytest.raises(pyropdf.LimitError) as caught:
                call([0], max_graphics=50)
            assert caught.value.code == "content_element_count"

        data = json.loads(doc.to_json([0], max_graphics=None))
        assert [e["text"] for e in data["pages"][0]["elements"]] == ["Vector Map"]
        chunks = doc.to_chunks([0], max_graphics=None)
        assert [c.text for c in chunks] == ["Vector Map"]


def test_json_and_chunks_preflight_table_before_element_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_table_events(*_args: object, **_kwargs: object) -> object:
        raise AssertionError

    with _document() as doc:
        monkeypatch.setattr(_markdown, "_table_events", fail_table_events)
        for call in (doc.to_json, doc.to_chunks):
            with pytest.raises(pyropdf.LimitError) as caught:
                call([0], max_size=16)
            assert caught.value.code == "export_output_size"


def test_exports_cache_tables_and_structure_content_per_page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    orig_page_tables = _markdown._page_tables  # noqa: SLF001
    table_calls = 0

    def counting_tables(*args: object, **kwargs: object) -> object:
        nonlocal table_calls
        table_calls += 1
        return orig_page_tables(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    with _document() as doc, monkeypatch.context() as patch:
        patch.setattr(_markdown, "_page_tables", counting_tables)
        doc.markdown(ignore_headers_footers=True)
        # Page 0 has paths (1 table extraction), page 1 has no paths (1 empty call cached); no 3x repeats.
        assert table_calls == 2
        table_calls = 0
        doc.to_chunks(ignore_headers_footers=True)
        assert table_calls == 2

    # Tagged Figure page reuses Page.content(text_runs=False) from graphic_bbox instead of calling twice.
    pixels = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0])
    img_hdr = "/Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB /BitsPerComponent 8 "
    tagged = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /MarkInfo << /Marked true >> /StructTreeRoot 10 0 R >>",
            2: (
                "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 300 300] "
                "/Resources << /Font << /F1 4 0 R >> /XObject << /Im1 6 0 R >> >> >>"
            ),
            3: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /StructParents 0 >>",
            4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            5: _stream(
                "BT /F1 12 Tf 40 260 Td /P << /MCID 0 >> BDC (Caption) Tj EMC ET\n"
                "/Figure << /MCID 1 >> BDC q 50 0 0 50 40 160 cm /Im1 Do Q EMC\n"
            ),
            6: _stream(pixels, img_hdr),
            10: "<< /Type /StructTreeRoot /K 11 0 R >>",
            11: "<< /Type /StructElem /S /Document /P 10 0 R /Pg 3 0 R /K [12 0 R 13 0 R] >>",
            12: "<< /Type /StructElem /S /P /P 11 0 R /Pg 3 0 R /K 0 >>",
            13: "<< /Type /StructElem /S /Figure /P 11 0 R /Pg 3 0 R /Alt (Chart) /K 1 >>",
        }
    )
    orig_content = pyropdf.Page.content
    content_calls = 0

    def counting_content(self: pyropdf.Page, *args: object, **kwargs: object) -> pyropdf.Content:
        nonlocal content_calls
        content_calls += 1
        return orig_content(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    with pyropdf.open(tagged) as doc, monkeypatch.context() as patch:
        patch.setattr(pyropdf.Page, "content", counting_content)
        md = doc.markdown(images=tmp_path / "tagged")
        assert "![Chart](page-0-image-0.png)" in md
        assert content_calls == 1
        content_calls = 0
        chunks = doc.to_chunks(images=tmp_path / "tagged_chunks")
        assert "![Chart](page-0-image-0.png)" in chunks[0].text
        assert content_calls == 1
