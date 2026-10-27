"""Underline, strike-out, and superscript flags of spans.

Ported from pylopdf 036be111 ``tests/test_text_styles.py``; the ``to_dict``
assertions belong to #100.
"""

from __future__ import annotations

from test_text import build_text_pdf

import pyropdf


def _spans(page: pyropdf.Page, *, styles: bool = False) -> list[pyropdf.Span]:
    return [span for block in page.layout(styles=styles).blocks for line in block.lines for span in line.spans]


def test_a_raised_smaller_span_is_a_superscript_on_its_line() -> None:
    page = pyropdf.open(
        build_text_pdf("BT /F1 20 Tf 72 700 Td (E = mc) Tj 8 Ts /F1 12 Tf (2) Tj 0 Ts /F1 20 Tf ( is famous) Tj ET")
    ).pages[0]

    (line,) = page.layout().blocks[0].lines

    assert [(span.text, span.is_superscript) for span in line.spans] == [
        ("E = mc", False),
        ("2", True),
        (" is famous", False),
    ]
    assert page.text() == "E = mc2 is famous\n"


def test_rules_under_and_through_text_underline_and_strike_it_out() -> None:
    page = pyropdf.open(
        build_text_pdf(
            "BT /F1 20 Tf 72 700 Td (under) Tj ET 72 696 m 125 696 l S "
            "BT /F1 20 Tf 72 600 Td (struck) Tj ET 72 606 60 1.5 re f "
            "BT /F1 20 Tf 72 500 Td (plain) Tj ET 72 470 m 125 470 l S"
        )
    ).pages[0]

    spans = {span.text: span for span in _spans(page, styles=True)}

    assert (spans["under"].is_underlined, spans["under"].is_struck) == (True, False)
    assert (spans["struck"].is_underlined, spans["struck"].is_struck) == (False, True)
    assert (spans["plain"].is_underlined, spans["plain"].is_struck) == (False, False)
    # Without styles=True the rules are not read.
    assert not any(span.is_underlined or span.is_struck for span in _spans(page))


def test_a_rule_under_part_of_a_span_splits_it() -> None:
    page = pyropdf.open(build_text_pdf("BT /F1 20 Tf 72 700 Td (half way) Tj ET 72 696 m 110 696 l S")).pages[0]

    spans = _spans(page, styles=True)

    assert [(span.text, span.is_underlined) for span in spans] == [("half ", True), ("way", False)]
    assert page.layout(styles=True).blocks[0].lines[0].text == page.layout().blocks[0].lines[0].text
