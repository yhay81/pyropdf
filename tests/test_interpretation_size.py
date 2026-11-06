"""DocumentLimits bounds the PDF input to hayro when a page is interpreted."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from test_text import build_text_pdf

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.mark.parametrize(
    "read", [pyropdf.Page.text, pyropdf.Page.words, pyropdf.Page.paths, pyropdf.Page.content, pyropdf.Page.pixmap]
)
@pytest.mark.parametrize("edited", [False, True], ids=["original", "edited"])
def test_interpretation_size_bounds_the_pdf_handed_to_hayro(
    read: Callable[[pyropdf.Page], object], *, edited: bool
) -> None:
    data = build_text_pdf("BT /F1 12 Tf 5 20 Td (hello) Tj ET")
    # The original fits, but the bridge also supplies a cross-reference update.
    limits = pyropdf.DocumentLimits(max_interpretation_size=len(data))
    with pyropdf.open(data, limits=limits) as doc:
        page = doc.pages[0]
        if edited:
            page.rotation = 90
        saved = doc.to_bytes(incremental=True)
        assert page.operations
        with pytest.raises(pyropdf.LimitError) as caught:
            read(page)
        assert caught.value.code == "interpretation_size"
        assert doc.to_bytes(incremental=True) == saved

    with pyropdf.open(data, limits=pyropdf.DocumentLimits(max_interpretation_size=64 * 1024)) as doc:
        page = doc.pages[0]
        if edited:
            page.rotation = 90
        read(page)
