"""Memoized page results (DESIGN.md section 5 and F4).

A page's text, words, layout, and content are memoized with the objects
that interpreting the page may read. An edit that misses those objects
reuses the results, and an edit to any of them recomputes. The oracle for
a reused result is a fresh document opened from the same version's bytes,
which has nothing memoized (F9).
"""

from __future__ import annotations

import random
from functools import partial
from typing import TYPE_CHECKING

import pytest
from test_content import build_raw_pdf

import pyropdf
from pyropdf import Name, _core

if TYPE_CHECKING:
    from collections.abc import Callable

# The objects of build_memo_pdf by number.
PAGES, CONTENTS, APPEARANCE, FONT, OTHER_CONTENTS = 2, 4, 8, 9, 7


def _stream(data: str, entries: str = "") -> str:
    return f"<< /Length {len(data)}{entries} >>\nstream\n{data}\nendstream"


def build_memo_pdf() -> bytes:
    """Build two pages that inherit their font and media box from /Pages.

    The first page has an annotation with an appearance stream.
    """
    font = f"<< /Font << /F1 {FONT} 0 R >> >>"
    return build_raw_pdf({
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        PAGES: f"<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 /MediaBox [0 0 300 300] /Resources {font} >>",
        3: f"<< /Type /Page /Parent 2 0 R /Contents {CONTENTS} 0 R /Annots [5 0 R] >>",
        CONTENTS: _stream("BT /F1 20 Tf 20 250 Td (Alpha) Tj ET"),
        5: f"<< /Type /Annot /Subtype /Square /Rect [20 20 120 120] /P 3 0 R /AP << /N {APPEARANCE} 0 R >> >>",
        6: f"<< /Type /Page /Parent 2 0 R /Contents {OTHER_CONTENTS} 0 R >>",
        OTHER_CONTENTS: _stream("BT /F1 20 Tf 20 250 Td (Beta) Tj ET"),
        APPEARANCE: _stream(
            "BT /F1 10 Tf 5 50 Td (Note) Tj ET",
            f" /Type /XObject /Subtype /Form /BBox [0 0 100 100] /Resources {font}",
        ),
        FONT: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    })  # fmt: skip


def _views(page: pyropdf.Page) -> tuple[object, ...]:
    """Return every memoized result of ``page``, one lookup each."""
    return (page.text(), page.words(), page.layout(chars=True), page.content())


VIEWS = 4


def _counts(document: pyropdf.Document) -> tuple[int, int]:
    """Return the hits and the computations of the document's page results."""
    return _core.page_result_counts(document._core())  # noqa: SLF001


def _fresh(document: pyropdf.Document, index: int = 0) -> pyropdf.Page:
    return pyropdf.open(document.to_bytes()).pages[index]


def test_an_edit_that_misses_a_page_reuses_its_results() -> None:
    document = pyropdf.open(build_memo_pdf())
    first, second = document.pages
    before = _views(first)
    hits, computations = _counts(document)
    assert computations == VIEWS

    second.obj["Contents"].write(b"BT /F1 20 Tf 20 250 Td (Gamma) Tj ET")
    document.objects.add({"Unrelated": 1})

    assert _views(first) == before
    assert _counts(document) == (hits + VIEWS, computations)
    assert second.text() == "Gamma\n"
    # Undo returns to a version whose results the memo still holds.
    document.history.undo()
    document.history.undo()
    assert _views(first) == before
    assert _counts(document)[1] == computations + 1


def _font(document: pyropdf.Document) -> None:
    document.objects[FONT]["BaseFont"] = Name("Courier")


def _inherited_font(document: pyropdf.Document) -> None:
    number = document.objects.add({"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Times-Roman")})
    document.objects[PAGES]["Resources"]["Font"]["F1"] = document.objects[number]


def _inherited_media_box(document: pyropdf.Document) -> None:
    document.objects[PAGES]["MediaBox"] = [0, 0, 400, 300]


def _content(document: pyropdf.Document) -> None:
    document.objects[CONTENTS].write(b"BT /F1 20 Tf 20 250 Td (Omega) Tj ET")


def _appearance(document: pyropdf.Document) -> None:
    document.objects[APPEARANCE].write(b"BT /F1 10 Tf 5 50 Td (Memo) Tj ET")


@pytest.mark.parametrize("edit", [_content, _font, _inherited_font, _inherited_media_box, _appearance])
def test_an_edit_to_what_a_page_reads_recomputes_its_results(edit: Callable[[pyropdf.Document], None]) -> None:
    document = pyropdf.open(build_memo_pdf())
    page = document.pages[0]
    before = _views(page)
    _hits, computations = _counts(document)

    edit(document)

    after = _views(page)
    assert after != before
    assert after == _views(_fresh(document))
    assert _counts(document)[1] == computations + VIEWS


def _random_edit(document: pyropdf.Document, rng: random.Random) -> None:
    word = rng.choice(["Alpha", "Beta", "fi ne", "A-", "x"])
    match rng.randrange(7):
        case 0:
            number = rng.choice([CONTENTS, OTHER_CONTENTS])
            document.objects[number].write(f"BT /F1 {rng.randint(8, 30)} Tf 20 250 Td ({word}) Tj ET".encode())
        case 1:
            document.objects[FONT]["BaseFont"] = Name(rng.choice(["Helvetica", "Courier", "Times-Roman"]))
        case 2:
            document.objects[PAGES]["MediaBox"] = [0, 0, rng.choice([200, 300, 400]), 300]
        case 3:
            rng.choice(document.pages).rotation = rng.choice([0, 90, 180, 270])
        case 4:
            document.objects[APPEARANCE].write(f"BT /F1 10 Tf 5 50 Td ({word}) Tj ET".encode())
        case 5 if document.history.can_undo:
            document.history.undo()
        case _:
            with document.transaction():
                document.objects.add({"Unrelated": rng.random()})
                document.pages[1].obj["Contents"].write(f"BT /F1 12 Tf 20 200 Td ({word}) Tj ET".encode())


@pytest.mark.parametrize("seed", range(6))
def test_memoized_results_equal_fresh_ones_across_random_edits(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - reproducible edits, not a secret
    document = pyropdf.open(build_memo_pdf())

    for _ in range(10):
        _random_edit(document, rng)
        fresh = pyropdf.open(document.to_bytes())
        for page, oracle in zip(document.pages, fresh.pages, strict=True):
            assert _views(page) == _views(oracle)
    assert _counts(document)[0] > 0


def test_a_transaction_reads_its_own_edits_and_a_discarded_one_restores_them() -> None:
    document = pyropdf.open(build_memo_pdf())
    page = document.pages[0]
    before = _views(page)

    inside = []

    def edit_and_fail() -> None:
        with document.transaction():
            _content(document)
            inside.append(page.text())
            raise RuntimeError

    with pytest.raises(RuntimeError):
        edit_and_fail()
    assert "Omega" in inside[0]

    assert _views(page) == before


def _outcome(call: Callable[[], object]) -> object:
    try:
        return call()
    except pyropdf.LimitError as error:
        return error.code, str(error)


LIMITED: list[tuple[str, str, range]] = [
    ("text", "max_glyphs", range(1, 12)),
    ("text", "max_size", range(1, 16)),
    ("words", "max_glyphs", range(1, 12)),
    ("words", "max_size", range(1, 40)),
    ("layout", "max_size", range(1, 160)),
    ("content", "max_elements", range(1, 80)),
]


@pytest.mark.parametrize(("view", "limit", "values"), LIMITED)
def test_a_reused_result_refuses_a_smaller_limit_as_a_fresh_one_does(view: str, limit: str, values: range) -> None:
    document = pyropdf.open(build_memo_pdf())
    page = document.pages[0]
    _views(page)
    _hits, computations = _counts(document)
    data = document.to_bytes()

    outcomes = set()
    for value in values:
        fresh = pyropdf.open(data).pages[0]
        reused = _outcome(partial(getattr(page, view), **{limit: value}))
        assert reused == _outcome(partial(getattr(fresh, view), **{limit: value})), value
        outcomes.add(isinstance(reused, tuple))

    # The range covers both a refusal and a result, and every call reused.
    assert outcomes == {True, False}
    assert _counts(document)[1] == computations
