"""doc.destinations and the named destinations that links read.

Ported from pylopdf's tests/test_destinations.py and the link cases of
tests/test_named_destinations.py at ebb01d3. The outline, page cleanup, and
``insert_pdf`` cases of the latter wait for those features in pyropdf.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest

import pyropdf
from pyropdf import Destination, Point

if TYPE_CHECKING:
    from collections.abc import Callable

ASSETS = Path(__file__).parent / "assets" / "real_world"
MAX_NAMED_DEST_ENTRIES = 65_536
MAX_NAMED_DEST_NODES = 65_536
MAX_NAMED_DEST_EDGES = 131_072
MIB = 1024 * 1024
MANUAL_PAGES = 3
# pdfTeX keeps at most six names per leaf and six kids per intermediate node.
PDFTEX_FANOUT = 6

Shape = Literal["entries", "nodes", "edges", "text"]
SHAPE_CODES: dict[Shape, str] = {
    "entries": "named_destination_count",
    "nodes": "named_destination_count",
    "edges": "named_destination_edge_count",
    "text": "named_destination_text_size",
}


def build_raw_pdf(objects: dict[int, str]) -> bytes:
    """Return a PDF of ``objects`` by number, with a correct cross-reference table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode()
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 00000 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _named(names: str, legacy: str = "", kids: dict[int, str] | None = None) -> pyropdf.Document:
    catalog = "<< /Type /Catalog /Pages 2 0 R /Names << /Dests 5 0 R >> " + legacy + ">>"
    return pyropdf.open(
        build_raw_pdf(
            {
                1: catalog,
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] >>",
                4: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Rotate 90 >>",
                5: names,
                **(kids or {}),
            }
        )
    )


def test_named_destinations_resolve_to_display_space() -> None:
    doc = _named(
        "<< /Kids [6 0 R] >>",
        "/Dests << /legacy [3 0 R /FitH 50] /intro [4 0 R /Fit] >> ",
        {
            6: (
                "<< /Names [(intro) [3 0 R /XYZ 10 90 2] (later) << /D [4 0 R /Fit] >> "
                "(nowhere) [99 0 R /Fit] (intro) [4 0 R /Fit]] >>"
            )
        },
    )

    destinations = doc.destinations

    assert list(destinations) == ["intro", "later", "nowhere", "legacy"]
    assert destinations["intro"] == Destination(0, Point(10, 10), 2)
    assert destinations["later"] == Destination(1)
    assert destinations["nowhere"] is None
    assert destinations["legacy"] == Destination(0, Point(0, 50))
    assert len(destinations) == 4
    assert "missing" not in destinations
    with pytest.raises(KeyError):
        destinations["missing"]
    with pytest.raises(TypeError):
        destinations["intro"] = Destination(0)  # ty: ignore[invalid-assignment]


def test_a_document_without_names_has_none() -> None:
    doc = pyropdf.Document()
    doc.pages.new()

    assert dict(doc.destinations) == {}
    assert repr(doc.destinations) == "Destinations({})"


def test_links_and_named_destinations_agree() -> None:
    doc = pyropdf.open(ASSETS / "usrguide.pdf")

    destinations = doc.destinations

    assert len(destinations) == 61
    assert destinations["Doc-Start"] == Destination(0, Point(124.802, 125.798))
    assert destinations["Hfootnote.1"] is not None
    assert destinations["Hfootnote.1"].page == 25


def test_named_destination_reads_are_bounded() -> None:
    names = " ".join(f"(n{index}) [3 0 R /Fit]" for index in range(65537))
    doc = _named(f"<< /Names [{names}] >>")

    with pytest.raises(pyropdf.LimitError) as raised:
        len(doc.destinations)
    assert raised.value.code == "named_destination_count"


def test_closed_documents_refuse() -> None:
    doc = pyropdf.Document()
    doc.close()

    with pytest.raises(pyropdf.DocumentClosedError):
        _ = doc.destinations


def _name(index: int) -> str:
    """Return an 18-byte listings anchor; sorted names keep the tree ordered."""
    return f"lstnumber.-{index // 64:04d}.{index % 64 + 1:02d}"


def _deepest(count: int, page: int, pages: int = MANUAL_PAGES) -> int:
    """Return the last destination index that targets ``page``."""
    return count - 1 - (count - 1 - page) % pages


def _name_tree(count: int, first_id: int, pages: int = MANUAL_PAGES) -> tuple[dict[int, str], str]:
    """Build a pdfTeX-shaped name tree whose destination ``i`` targets page ``i % pages``."""
    objects: dict[int, str] = {}
    level: list[tuple[int, str, str]] = []
    for start in range(0, count, PDFTEX_FANOUT):
        indices = range(start, min(start + PDFTEX_FANOUT, count))
        pairs = " ".join(f"({_name(i)}) [{3 + i % pages} 0 R /XYZ 0 200 null]" for i in indices)
        limits = (_name(indices[0]), _name(indices[-1]))
        objects[first_id + len(objects)] = f"<< /Names [{pairs}] /Limits [({limits[0]}) ({limits[1]})] >>"
        level.append((first_id + len(objects) - 1, *limits))
    while len(level) > PDFTEX_FANOUT:
        parents: list[tuple[int, str, str]] = []
        for start in range(0, len(level), PDFTEX_FANOUT):
            group = level[start : start + PDFTEX_FANOUT]
            kids = " ".join(f"{node} 0 R" for node, _, _ in group)
            limits = (group[0][1], group[-1][2])
            objects[first_id + len(objects)] = f"<< /Kids [{kids}] /Limits [({limits[0]}) ({limits[1]})] >>"
            parents.append((first_id + len(objects) - 1, *limits))
        level = parents
    return objects, " ".join(f"{node} 0 R" for node, _, _ in level)


def _manual(count: int, pages: int = MANUAL_PAGES) -> bytes:
    """Build a manual whose links name the deepest destinations.

    Every page links to the deepest destination of each of the first three
    pages.
    """
    deepest = [_name(_deepest(count, page, pages)) for page in range(pages)]
    kids = " ".join(f"{3 + page} 0 R" for page in range(pages))
    objects: dict[int, str] = {2: f"<< /Type /Pages /Kids [{kids}] /Count {pages} /MediaBox [0 0 200 200] >>"}
    for page in range(pages):
        links = " ".join(
            f"<< /Type /Annot /Subtype /Link /Rect [{20 * other + 1} 1 {20 * other + 10} 10] "
            f"/A << /S /GoTo /D ({deepest[other]}) >> >>"
            for other in range(min(pages, MANUAL_PAGES))
        )
        objects[3 + page] = f"<< /Type /Page /Parent 2 0 R /Annots [{links}] >>"
    tree, root_kids = _name_tree(count, 3 + pages, pages)
    objects.update(tree)
    objects[1] = f"<< /Type /Catalog /Pages 2 0 R /Names << /Dests << /Kids [{root_kids}] >> >> >>"
    return build_raw_pdf(objects)


def _page_of(link: pyropdf.Link) -> int | None:
    target = link.target
    return target.page if isinstance(target, pyropdf.Destination) else None


def _links(doc: pyropdf.Document) -> list[list[tuple[pyropdf.Rect, pyropdf.Target | None]]]:
    return [[(link.rect, link.target) for link in page.links] for page in doc.pages]


def test_reading_every_page_of_a_large_manual_indexes_it_once() -> None:
    """Link reads reuse one name-tree index and one page index until an edit.

    Rebuilding both for every call made this loop take about 13 s in
    pyropdf; the generous bound only catches that regression.
    """
    pages = 1_000
    doc = pyropdf.open(_manual(MAX_NAMED_DEST_ENTRIES, pages))
    views = list(doc.pages)

    start = time.perf_counter()
    targets = [[_page_of(link) for link in page.links] for page in views]
    elapsed = time.perf_counter() - start

    assert elapsed < 2.0, f"reading the links of {pages} pages took {elapsed:.2f}s"
    assert targets == [list(range(MANUAL_PAGES))] * pages


def _delete_page(doc: pyropdf.Document, index: int) -> None:
    del doc.pages[index]


def _retarget(doc: pyropdf.Document) -> None:
    """Point the names of the last leaf of ``_manual(256)``, which links name, at the last page."""
    leaf = doc.objects[3 + MANUAL_PAGES + 255 // PDFTEX_FANOUT]
    names = leaf["Names"]
    for at in range(1, len(names), 2):
        names[at] = [doc.pages[2].obj, pyropdf.Name("Fit")]


def _retarget_in_one_step(doc: pyropdf.Document) -> None:
    with doc.transaction("retarget"):
        _retarget(doc)


EDITS: dict[str, Callable[[pyropdf.Document], object]] = {
    "delete_pages": lambda doc: _delete_page(doc, 0),
    "new_page": lambda doc: doc.pages.new((200, 200), index=0),
    "add_link_annot": lambda doc: doc.pages[1].links.add((1, 20, 10, 30), "https://example.com/"),
    "set_rotation": lambda doc: setattr(doc.pages[2], "rotation", 90),
    "name_tree": _retarget_in_one_step,
}


@pytest.mark.parametrize("edit", EDITS)
def test_cached_link_reads_follow_every_edit(edit: str) -> None:
    """Warm link reads after an edit match a document that never cached them."""
    doc = pyropdf.open(_manual(256))
    before = _links(doc)
    assert all(isinstance(target, pyropdf.Destination) for links in before for _, target in links)

    EDITS[edit](doc)

    assert _links(doc) == _links(pyropdf.open(doc.to_bytes()))
    doc.history.undo()
    assert _links(doc) == before


def test_named_destinations_follow_a_transaction() -> None:
    doc = pyropdf.open(_manual(256))
    first = _page_of(doc.pages[0].links[0])

    with doc.transaction("retarget"):
        _retarget(doc)
        inside = [_page_of(link) for link in doc.pages[0].links]

    assert inside == [2, 2, 2]
    assert inside == [_page_of(link) for link in doc.pages[0].links]
    assert dict(doc.destinations) == dict(pyropdf.open(doc.to_bytes()).destinations)
    doc.history.undo()
    assert _page_of(doc.pages[0].links[0]) == first


def test_cached_link_reads_follow_deleted_pages() -> None:
    """Links to a deleted page leave the pages that stay, and the deleted page's view is stale."""
    doc = pyropdf.open(_manual(256))
    first, last = doc.pages[0], doc.pages[2]
    assert [_page_of(link) for link in last.links] == [0, 1, 2]

    del doc.pages[0]

    with pytest.raises(pyropdf.StalePageError):
        list(first.links)
    assert [_page_of(link) for link in last.links] == [0, 1]


def _limited_tree(shape: Shape, *, over: bool) -> tuple[str, dict[int, str]]:
    """Return a tree exactly at, or one unit beyond, one limit with ``section`` last.

    Each tree approaches only the limit named by ``shape``; the others stay far
    below theirs, so every case isolates one boundary.
    """
    extra = int(over)
    section = "(section) [4 0 R /Fit]"
    if shape == "entries":
        return f"<< /Names [{'(k) 0 ' * (MAX_NAMED_DEST_ENTRIES - 1 + extra)}{section}] >>", {}
    if shape == "nodes":
        empty_kids = "<< >> " * (MAX_NAMED_DEST_NODES - 2 + extra)
        return f"<< /Kids [{empty_kids}<< /Names [{section}] >>] >>", {}
    if shape == "edges":
        return f"<< /Kids [{'8 0 R ' * (MAX_NAMED_DEST_EDGES + extra)}] >>", {8: f"<< /Names [{section}] >>"}
    # Fifteen reused 1 MiB keys, one shorter key, and `section` fill the budget.
    keys = "8 0 R 0 " * 15 + "9 0 R 0 "
    filler = {8: f"({'x' * MIB})", 9: f"({'y' * (MIB - len('section') + extra)})"}
    return f"<< /Names [{keys}{section}] >>", filler


def _limited_document(shape: Shape, *, over: bool) -> pyropdf.Document:
    """Open two pages whose name tree is at object 5 and whose first page links to ``section``."""
    tree, extra = _limited_tree(shape, over=over)
    link = "<< /Type /Annot /Subtype /Link /Rect [1 1 10 10] /Dest (section) >>"
    objects: dict[int, str] = {
        1: "<< /Type /Catalog /Pages 2 0 R /Names << /Dests 5 0 R >> >>",
        2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 200 200] >>",
        3: f"<< /Type /Page /Parent 2 0 R /Annots [{link}] >>",
        4: "<< /Type /Page /Parent 2 0 R >>",
        5: tree,
        **extra,
    }
    return pyropdf.open(build_raw_pdf(objects))


@pytest.mark.parametrize("shape", SHAPE_CODES)
def test_reads_accept_each_limit(shape: Shape) -> None:
    doc = _limited_document(shape, over=False)

    assert [_page_of(link) for link in doc.pages[0].links] == [1]
    assert doc.destinations["section"] == Destination(1)


@pytest.mark.parametrize("shape", SHAPE_CODES)
def test_refused_name_tree_index_is_never_cached(shape: Shape) -> None:
    """Every named lookup re-raises instead of trusting a partial index."""
    doc = _limited_document(shape, over=True)
    named = doc.pages[0]

    # A page without named links never walks the tree.
    assert len(doc.pages[1].links) == 0
    for _ in range(2):
        with pytest.raises(pyropdf.LimitError) as caught:
            list(named.links)
        assert caught.value.code == SHAPE_CODES[shape]
    with pytest.raises(pyropdf.LimitError):
        len(doc.destinations)


def test_name_tree_index_keeps_first_occurrences_of_unsorted_keys() -> None:
    """Unsorted and repeated keys resolve like a first-occurrence lookup.

    A key whose tree value is not a destination still shadows the legacy
    catalog ``/Dests`` entry of the same name.
    """
    names = ["zeta", "alpha", "", "twice", "legacy", "shadowed", "missing"]
    links = " ".join(
        f"<< /Type /Annot /Subtype /Link /Rect [{index} 1 {index + 1} 2] /Dest ({name}) >>"
        for index, name in enumerate(names)
    )
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: (
                    "<< /Type /Catalog /Pages 2 0 R /Names << /Dests << /Kids [5 0 R 6 0 R] >> >> "
                    "/Dests << /legacy [4 0 R /Fit] /shadowed [4 0 R /Fit] >> >>"
                ),
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 200 200] >>",
                3: f"<< /Type /Page /Parent 2 0 R /Annots [{links}] >>",
                4: "<< /Type /Page /Parent 2 0 R >>",
                5: "<< /Names [(zeta) [4 0 R /Fit] (alpha) [3 0 R /Fit] () [4 0 R /Fit] (twice) [4 0 R /Fit]] >>",
                6: "<< /Names [(twice) [3 0 R /Fit] () [3 0 R /Fit] (shadowed) << /S /GoTo >>] >>",
            }
        )
    )

    for _ in range(2):
        # zeta, alpha, "", twice, legacy, shadowed, and missing.
        assert [_page_of(link) for link in doc.pages[0].links] == [1, 0, 1, 1, 1, None, None]


def test_invalid_name_tree_pairs_still_count_with_legacy_entries() -> None:
    doc = _named(
        "<< /Names [" + "null [3 0 R /Fit] " * MAX_NAMED_DEST_ENTRIES + "] >>",
        "/Dests << /extra [3 0 R /Fit] >> ",
    )
    with pytest.raises(pyropdf.LimitError) as caught:
        list(doc.destinations)
    assert caught.value.code == "named_destination_count"
