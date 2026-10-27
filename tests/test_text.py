"""page.text() and doc.text(): the text of a page in reading order, and the text options.

Ported from pylopdf's tests/test_text_options.py at
036be111cafe77755db4bb2be17bc70a8f2f9d82, the tests that read ``text()``.
The budgets are the call's ``max_glyphs`` and ``max_size`` rather than
pylopdf's ``DocumentLimits.max_text_glyphs`` and ``max_text_size``, and a
document with an unmapped font comes from the corpus instead of pylopdf's
canvas.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from corpus import ASSETS, NAMES
from test_content import build_raw_pdf

import pyropdf


def _to_unicode(mappings: dict[int, str]) -> str:
    """Return a ToUnicode CMap that maps one-byte codes to text."""
    entries = "\n".join(f"<{code:02X}> <{text.encode('utf-16-be').hex().upper()}>" for code, text in mappings.items())
    return (
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
        "/CMapName /Test def 1 begincodespacerange <00> <FF> endcodespacerange "
        f"{len(mappings)} beginbfchar\n{entries}\nendbfchar endcmap "
        "CMapName currentdict /CMap defineresource pop end end"
    )


def build_text_pdf(*streams: str, to_unicode: dict[int, str] | None = None) -> bytes:
    """Build 612 by 792 pages, one per stream, that draw with Helvetica as /F1."""
    font = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
    first = 5 + len(streams)
    kids = " ".join(f"{3 + 2 * index} 0 R" for index in range(len(streams)))
    objects: dict[int, str | bytes] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {len(streams)} >>",
    }
    for index, stream in enumerate(streams):
        page = 3 + 2 * index
        objects[page] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {page + 1} 0 R "
            f"/Resources << /Font << /F1 {first + len(streams)} 0 R >> >> >>"
        )
        objects[page + 1] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
    number = first + len(streams)
    if to_unicode is None:
        objects[number] = font + " >>"
    else:
        cmap = _to_unicode(to_unicode)
        objects[number] = font + f" /ToUnicode {number + 1} 0 R >>"
        objects[number + 1] = f"<< /Length {len(cmap)} >>\nstream\n{cmap}\nendstream"
    return build_raw_pdf(objects)


@pytest.mark.spec("text-extraction", "interpret")
def test_text_follows_reading_order_with_spaces_and_line_ends() -> None:
    page = pyropdf.open(
        build_text_pdf("BT /F1 20 Tf 72 600 Td (Second) Tj ET BT /F1 20 Tf 72 700 Td (First) Tj 60 0 Td (gap) Tj ET")
    ).pages[0]

    assert page.text() == "First gap\nSecond\n"


def test_clip_keeps_the_text_inside_a_rectangle_in_reading_order() -> None:
    page = pyropdf.open(
        build_text_pdf(
            "BT /F1 20 Tf 72 700 Td (Top left) Tj ET BT /F1 20 Tf 400 700 Td (Top right) Tj ET "
            "BT /F1 20 Tf 72 600 Td (Lower left) Tj ET"
        )
    ).pages[0]

    assert page.text(clip=(60, 70, 300, 200)) == "Top left\nLower left\n"
    assert page.text(clip=(380, 70, 612, 120)) == "Top right\n"
    # A glyph counts as inside when the center of its box is.
    assert page.text(clip=(60, 70, 100, 120)) == "To\n"
    assert page.text(clip=(0, 0, 10, 10)) == ""


def test_glyphs_that_the_page_or_the_clip_paths_hide_are_left_out_unless_asked_for() -> None:
    page = pyropdf.open(
        build_text_pdf(
            "BT /F1 20 Tf 72 700 Td (Seen) Tj ET "
            "BT /F1 20 Tf 700 650 Td (Off page) Tj ET "
            "q 0 0 10 10 re W n BT /F1 20 Tf 72 600 Td (Masked) Tj ET Q "
            "q 3 Tr BT /F1 20 Tf 72 500 Td (Invisible) Tj ET Q"
        )
    ).pages[0]

    assert page.text() == "Seen\nInvisible\n"
    assert page.text(clipped=True) == "Seen\nOff page\nMasked\nInvisible\n"
    assert page.text() == "Seen\nInvisible\n"


def test_ligatures_and_whitespace_are_kept_unless_asked_otherwise() -> None:
    page = pyropdf.open(
        build_text_pdf(
            "BT /F1 20 Tf 72 700 Td (AB) Tj ET",
            to_unicode={0x41: "ﬁ", 0x42: " x"},
        )
    ).pages[0]

    assert page.text() == "ﬁ x\n"
    assert page.text(ligatures=False) == "fi x\n"
    assert page.text(whitespace=False) == "ﬁ x\n"
    assert page.text(ligatures=False, whitespace=False) == "fi x\n"


def test_dehyphenation_joins_a_word_that_a_line_end_splits() -> None:
    page = pyropdf.open(
        build_text_pdf(
            "BT /F1 12 Tf 14 TL 72 700 Td (A long exam-) Tj T* (ple follows) Tj T* (Jean-) Tj T* (Pierre stays) Tj ET"
        )
    ).pages[0]

    assert page.text() == "A long exam-\nple follows\nJean-\nPierre stays\n"
    assert page.text(dehyphenate=True) == "A long example follows\nJean-\nPierre stays\n"


def test_dehyphenation_needs_the_next_line_in_the_same_block() -> None:
    page = pyropdf.open(build_text_pdf("BT /F1 12 Tf 72 700 Td (A long exam-) Tj 0 -200 Td (ple apart) Tj ET")).pages[0]

    assert page.text(dehyphenate=True) == page.text()


def _unmapped_document() -> pyropdf.Document:
    """Return the corpus's Japanese document without its ToUnicode CMaps."""
    document = pyropdf.open(ASSETS / "mhlw-doc.pdf")
    for xref in document.objects:
        value = document.objects[xref]
        if isinstance(value, pyropdf.Dictionary) and "ToUnicode" in value:
            del value["ToUnicode"]
    return document


@pytest.mark.spec("text-extraction", "read")
def test_unmapped_glyphs_are_skipped_or_stand_for_a_placeholder() -> None:
    page = _unmapped_document().pages[0]

    with pytest.warns(pyropdf.PyropdfWarning, match="no Unicode mapping"):
        skipped = page.text()
    assert skipped.strip() == ""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        replaced = page.text(unmapped="replacement")
        glyph_ids = page.text(unmapped="glyph_id")
    assert "�" in replaced
    assert set(replaced) <= {"�", " ", "\n"}
    assert all(0 < ord(char) < 0x10000 for char in glyph_ids.replace(" ", "").replace("\n", ""))


def test_type3_glyphs_without_unicode_have_no_glyph_id() -> None:
    page = pyropdf.open(ASSETS / "pdfium-type3.pdf").pages[0]

    with pytest.warns(pyropdf.PyropdfWarning, match="no Unicode mapping"):
        assert page.text() == ""
    assert page.text(unmapped="glyph_id") == page.text(unmapped="replacement")
    assert set(page.text(unmapped="replacement")) <= {"�", " ", "\n"}


def test_document_text_applies_the_options_to_every_page() -> None:
    stream = "BT /F1 20 Tf 72 700 Td (AB) Tj ET"
    document = pyropdf.open(build_text_pdf(stream, stream, to_unicode={0x41: "ﬁ", 0x42: "x"}))

    assert document.text(ligatures=False) == "fix\nfix\n"
    assert document.text(clip=(0, 0, 10, 10)) == ""
    assert document.text([1, -2, 1]) == "ﬁx\nﬁx\nﬁx\n"
    assert document.text([]) == ""


def test_document_text_joins_the_text_of_its_pages() -> None:
    document = pyropdf.open(build_text_pdf("BT /F1 20 Tf 72 700 Td (One) Tj ET", "BT /F1 20 Tf 72 700 Td (Two) Tj ET"))

    assert document.text() == "".join(page.text() for page in document.pages) == "One\nTwo\n"
    assert document.text([1, 0]) == "Two\nOne\n"
    with pytest.raises(IndexError, match="out of range"):
        document.text([2])


def test_the_glyph_budget_counts_the_glyphs_of_one_page() -> None:
    data = build_text_pdf("BT /F1 20 Tf 72 700 Td (Seen) Tj ET BT /F1 20 Tf 700 650 Td (Off) Tj ET")
    page = pyropdf.open(data).pages[0]

    # Hidden glyphs that the text leaves out are not collected.
    assert page.text(max_glyphs=4) == "Seen\n"
    assert page.text(clipped=True, max_glyphs=7) == "Seen\nOff\n"
    with pytest.raises(pyropdf.LimitError) as error:
        page.text(clipped=True, max_glyphs=6)
    assert error.value.code == "text_glyph_count"
    assert page.text(max_glyphs=None) == "Seen\n"
    # Each page of a document's text has the whole glyph budget.
    document = pyropdf.open(
        build_text_pdf("BT /F1 20 Tf 72 700 Td (Seen) Tj ET", "BT /F1 20 Tf 72 700 Td (Also) Tj ET")
    )
    assert document.text(max_glyphs=4) == "Seen\nAlso\n"


def test_the_size_budget_counts_the_text_of_the_whole_call() -> None:
    data = build_text_pdf("BT /F1 12 Tf 14 TL 72 700 Td (A long exam-) Tj T* (ple follows) Tj ET")
    document = pyropdf.open(data)
    page = document.pages[0]
    text = "A long example follows\n"

    # The assembled text fits its exact size.
    assert page.text(dehyphenate=True, max_size=len(text)) == text
    with pytest.raises(pyropdf.LimitError) as error:
        page.text(dehyphenate=True, max_size=len(text) - 1)
    assert error.value.code == "text_size"
    assert document.text(dehyphenate=True, ligatures=False, max_size=len(text)) == text
    with pytest.raises(pyropdf.LimitError) as error:
        document.text([0, 0], dehyphenate=True, max_size=2 * len(text) - 1)
    assert error.value.code == "text_size"
    assert document.text([0, 0], dehyphenate=True, max_size=2 * len(text)) == 2 * text


@pytest.mark.parametrize(
    ("options", "error", "message"),
    [
        ({"unmapped": "cid"}, ValueError, "unmapped must be"),
        ({"clipped": 1}, TypeError, "clipped must be True or False"),
        ({"ligatures": None}, TypeError, "ligatures must be True or False"),
        ({"whitespace": "no"}, TypeError, "whitespace must be True or False"),
        ({"dehyphenate": 0}, TypeError, "dehyphenate must be True or False"),
        ({"clip": (10, 10, 5, 20)}, ValueError, "clip must be a finite rect"),
        ({"max_glyphs": 0}, ValueError, "max_glyphs"),
        ({"max_size": -1}, ValueError, "max_size"),
    ],
)
def test_text_options_are_validated(options: dict[str, object], error: type[Exception], message: str) -> None:
    document = pyropdf.open(build_text_pdf("BT /F1 20 Tf 72 700 Td (A) Tj ET"))

    for view in (document.pages[0].text, document.text):
        with pytest.raises(error, match=message):
            view(**options)  # ty: ignore[invalid-argument-type]


def test_options_survive_a_save_and_reopen() -> None:
    document = pyropdf.open(build_text_pdf("BT /F1 12 Tf 14 TL 72 700 Td (exam-) Tj T* (ple) Tj ET"))

    reopened = pyropdf.open(document.to_bytes())

    assert reopened.pages[0].text(dehyphenate=True) == document.pages[0].text(dehyphenate=True) == "example\n"


def test_text_reads_the_current_version_and_refuses_a_deleted_page() -> None:
    document = pyropdf.open(build_text_pdf("BT /F1 20 Tf 72 700 Td (Before) Tj ET"))
    page = document.pages[0]
    assert page.text() == "Before\n"

    page.obj["Contents"].write(b"BT /F1 20 Tf 72 700 Td (After) Tj ET")
    assert page.text() == "After\n"
    document.history.undo()
    assert page.text() == "Before\n"
    del document.pages[0]
    with pytest.raises(pyropdf.StalePageError):
        page.text()


@pytest.mark.parametrize("name", NAMES)
def test_every_corpus_page_reads_within_the_default_limits(name: str) -> None:
    document = pyropdf.open(Path(ASSETS / name))

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", pyropdf.PyropdfWarning)
        texts = [page.text() for page in document.pages]
        assert document.text() == "".join(texts)
