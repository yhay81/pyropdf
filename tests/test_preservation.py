"""The preservation harness: an edit and a save leave everything else as it was.

Each edit of ``EDITS`` is applied to each corpus document, which is then saved
in full and incrementally and opened again. Every object that the edit did
not touch must read back equal, with reals compared by the bytes they are
written with and streams by their raw data, and an untouched page must keep
its text and its rendering. A later editing operation adds its row to
``EDITS``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from corpus import ASSETS, NAMES
from test_pages import build_raw_pdf

import pyropdf
from pyropdf import Array, Dictionary, Name, Real, Stream

if TYPE_CHECKING:
    from collections.abc import Callable

#: Plain values are compared through this, which nests no deeper.
_MAX_DEPTH = 64


def plain(value: object, depth: int = 0) -> object:
    """Return ``value`` as plain data: indirect objects inside it as references, reals as exact decimals."""
    assert depth < _MAX_DEPTH
    if isinstance(value, (Dictionary, Array)) and depth and value.xref is not None:
        return ("ref", value.xref)
    if isinstance(value, Stream):
        entries = {key: plain(item, depth + 1) for key, item in value.items()}
        return ("stream", entries, value.read(decode=False, max_size=None))
    if isinstance(value, Dictionary):
        return {key: plain(item, depth + 1) for key, item in value.items()}
    if isinstance(value, Array):
        return [plain(item, depth + 1) for item in value]
    if isinstance(value, Real):
        # The exact decimal of the real's lexeme, or of its float when it
        # has none; test_exact_reals_keep_their_tokens_through_every_edit
        # checks digits beyond a float in the saved bytes.
        return ("real", value.to_decimal())
    return (type(value).__name__, value)


def number(value: Dictionary) -> int:
    """Return the object number of the indirect ``value``."""
    assert value.xref is not None
    return value.xref


def snapshot(doc: pyropdf.Document) -> dict[int, object]:
    """Return every indirect object of ``doc`` as plain data, by number."""
    return {number: plain(doc.objects[number]) for number in doc.objects}


def rotate(doc: pyropdf.Document) -> set[int]:
    page = doc.pages[0]
    page.rotation = (page.rotation + 90) % 360
    return {number(page.obj)}


def add_object(doc: pyropdf.Document) -> set[int]:
    doc.objects.add({"Preserved": Real("0.000001")})
    return set()


def catalog_entry(doc: pyropdf.Document) -> set[int]:
    doc.catalog["PageMode"] = Name("UseNone")
    return {number(doc.catalog)}


def new_page(doc: pyropdf.Document) -> set[int]:
    # Adding a page flattens the page tree: it writes the root and every page
    # that is not already the root's kid (#272 narrows what those pages keep).
    root = doc.catalog["Pages"]
    touched = {number(root)}
    touched |= {number(page.obj) for page in doc.pages if page.obj.get("Parent") != root}
    doc.pages.new((200, 300))
    return touched


def replace_object(doc: pyropdf.Document) -> set[int]:
    number = doc.objects.add([1, 2])
    doc.objects[number] = [Real("3.14159265358979323846264338327950288")]
    return set()


def rewrite_stream(doc: pyropdf.Document) -> set[int]:
    number = doc.objects.add_stream(b"q Q")
    doc.objects[number].write(b"q 1 0 0 1 0 0 cm Q")
    return set()


#: Each edit, which returns the object numbers it touched among those that existed before it.
EDITS: dict[str, Callable[[pyropdf.Document], set[int]]] = {
    "none": lambda _: set(),
    "rotate": rotate,
    "add_object": add_object,
    "catalog_entry": catalog_entry,
    "new_page": new_page,
    "replace_object": replace_object,
    "rewrite_stream": rewrite_stream,
}


def page_state(doc: pyropdf.Document, index: int) -> tuple[str, bytes]:
    """Return the text and a low-resolution rendering of page ``index``."""
    page = doc.pages[index]
    return page.text(), page.pixmap(dpi=18).to_bytes()


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.parametrize("edit", sorted(EDITS))
@pytest.mark.parametrize("name", NAMES)
def test_an_edit_and_a_save_keep_every_untouched_object(name: str, edit: str, *, incremental: bool) -> None:
    doc = pyropdf.open(ASSETS / name)
    before = snapshot(doc)
    # The last page, which no edit of the table touches unless it is the first.
    untouched = len(doc.pages) - 1
    state = page_state(doc, untouched) if untouched else None
    touched = EDITS[edit](doc)
    reopened = pyropdf.open(doc.to_bytes(incremental=incremental))
    after = snapshot(reopened)
    changed = [number for number, value in before.items() if number not in touched and after.get(number) != value]
    assert changed == []
    if state is not None:
        assert page_state(reopened, untouched) == state


#: Reals that a float cannot hold exactly, as the synthetic document writes them.
EXACT = (
    b"35.68123456789012345678",
    b"0.000000000000000000000000000001",
    b"123456789012345678901234567890.5",
    b"-.5",
    b"-0.30000000000000004",
    b"99999999999999999999.9",
    b"792.000000000000000001",
)


def synthetic() -> bytes:
    """Return a one-page document whose catalog and page hold the reals of ``EXACT``."""
    reals = " ".join(token.decode() for token in EXACT[:-1])
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /Exact 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1000 {EXACT[-1].decode()}] >>",
            4: f"[{reals}]",
        }
    )


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.parametrize("edit", sorted(EDITS))
def test_exact_reals_keep_their_tokens_through_every_edit(edit: str, *, incremental: bool) -> None:
    doc = pyropdf.open(synthetic())
    touched = EDITS[edit](doc)
    data = doc.to_bytes(incremental=incremental)
    # The saved bytes are searched for the source tokens, independently of
    # the parser: a real read from bytes has no lexeme until I7 (#16).
    for token in EXACT if 3 not in touched else EXACT[:-1]:
        assert b" " + token + b" " in data.replace(b"[", b" ").replace(b"]", b" ")
    assert len(pyropdf.open(data).objects[4]) == len(EXACT) - 1
