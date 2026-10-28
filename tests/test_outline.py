"""Tests for ``Document.outline``: item handles, entry records, and targets.

Ported from pylopdf's tests/test_outline.py and tests/test_outline_targets.py
at ebb01d3c. Not ported:

- ``test_item_objects_keep_the_handle_generation``: the store holds one
  generation of each object number, so two generations of one number do not
  load together.
- ``test_removed_outline_names_raise_migration_hints``: pyropdf never had
  ``get_toc`` or ``set_toc``.
- ``test_page_cleanup_shares_the_toc_node_limit``: page edits follow the
  references to the removed pages (DESIGN.md section 5), and do not walk the
  outline.
- The in-memory renumbering case: pyropdf renumbers only the saved output.
  A separate test checks that saving preserves current outline handles.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

import pyropdf
from pyropdf import (
    Color,
    Destination,
    Launch,
    LimitError,
    NamedAction,
    OutlineEntry,
    RemoteDestination,
    _core,
)

REAL_WORLD = Path(__file__).parent / "assets" / "real_world"
MAX_TOC_ENTRIES = 65_536
MAX_TOC_TEXT_BYTES = 16 * 1024 * 1024


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


def build_pdf(texts: list[str]) -> bytes:
    """Return one page per text, with MediaBox and Resources on the tree's root."""
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(texts)))
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {len(texts)} /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 3 0 R >> >> >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for i, text in enumerate(texts):
        content = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET"
        objects[4 + 2 * i] = f"<< /Type /Page /Parent 2 0 R /Contents {5 + 2 * i} 0 R >>"
        objects[5 + 2 * i] = f"<< /Length {len(content)} >>\nstream\n{content}\nendstream"
    return build_raw_pdf(objects)


@pytest.fixture(scope="module")
def three_page_pdf() -> bytes:
    return build_pdf(["Page one", "Page two", "Page three"])


def _build_outline_fixture(outline_objects: dict[int, str], *, pages: str = "[3 0 R]") -> bytes:
    """Build one page with a caller-provided outline graph rooted at object 4."""
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /Outlines 4 0 R >>",
        2: f"<< /Type /Pages /Kids {pages} /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
    }
    objects.update(outline_objects)
    return build_raw_pdf(objects)


def _simple(entries: list[OutlineEntry]) -> list[tuple[int, str, int | None]]:
    return [(entry.level, entry.title, entry.page) for entry in entries]


BOOK = [
    OutlineEntry(1, "第 1 章", Destination(0)),
    OutlineEntry(2, "1.1 節", Destination(1)),
    OutlineEntry(1, "第 2 章", Destination(2)),
]


# --- writing and reading whole outlines --------------------------------------


def test_outline_round_trips_through_saving(three_page_pdf: bytes) -> None:
    doc = pyropdf.open(three_page_pdf)
    assert list(doc.outline) == []
    assert doc.outline.entries() == []
    doc.outline = BOOK
    assert doc.outline.entries() == BOOK
    assert _simple(doc.outline.entries()) == [(1, "第 1 章", 0), (2, "1.1 節", 1), (1, "第 2 章", 2)]
    # CJK titles survive as UTF-16BE text strings.
    assert pyropdf.open(doc.to_bytes()).outline.entries() == BOOK


def test_outline_deep_nesting(three_page_pdf: bytes) -> None:
    doc = pyropdf.open(three_page_pdf)
    entries = [
        OutlineEntry(1, "A", Destination(0)),
        OutlineEntry(2, "B", Destination(0)),
        OutlineEntry(3, "C", Destination(1)),
        OutlineEntry(1, "D", Destination(2)),
    ]
    doc.outline = entries
    reloaded = pyropdf.open(doc.to_bytes())
    assert reloaded.outline.entries() == entries
    assert [item.level for item in reloaded.outline.walk()] == [1, 2, 3, 1]
    assert [item.title for item in reloaded.outline[0].children[0].children] == ["C"]


def test_assigning_replaces_and_deleting_removes(three_page_pdf: bytes) -> None:
    doc = pyropdf.open(three_page_pdf)
    doc.outline = [OutlineEntry(1, "old", Destination(0)), OutlineEntry(1, "old2", Destination(1))]
    doc.outline = (entry for entry in [OutlineEntry(1, "new", Destination(2))])
    assert _simple(doc.outline.entries()) == [(1, "new", 2)]
    assert b"old2" not in doc.to_bytes()
    del doc.outline
    assert doc.outline.entries() == []
    assert "Outlines" not in doc.catalog
    doc.outline = BOOK
    doc.outline = []
    assert pyropdf.open(doc.to_bytes()).outline.entries() == []
    del doc.outline  # Removing an absent outline changes nothing.


def test_replacing_frees_the_items_before(three_page_pdf: bytes) -> None:
    doc = pyropdf.open(three_page_pdf)
    before = set(doc.objects)
    doc.outline = BOOK
    doc.outline = [OutlineEntry(1, "new", Destination(2))]
    del doc.outline
    assert set(doc.objects) == before


@pytest.mark.parametrize(
    ("entries", "error", "match"),
    [
        ([OutlineEntry(2, "A")], ValueError, "level"),  # The first level must be 1.
        ([OutlineEntry(1, "A"), OutlineEntry(3, "B")], ValueError, "level"),
        ([OutlineEntry(1, "A", Destination(3))], ValueError, "out of range"),
        ([(1, "A", 0)], TypeError, "OutlineEntry"),
        ("A", TypeError, "iterable of OutlineEntry"),
    ],
)
def test_assigning_refuses_invalid_outlines(
    three_page_pdf: bytes, entries: object, error: type[Exception], match: str
) -> None:
    doc = pyropdf.open(three_page_pdf)
    doc.outline = BOOK
    with pytest.raises(error, match=match):
        doc.outline = entries  # ty: ignore[invalid-assignment]
    assert doc.outline.entries() == BOOK


@pytest.mark.parametrize(
    ("make", "error"),
    [
        (lambda: OutlineEntry(0, "A"), ValueError),
        (lambda: OutlineEntry(65, "A"), ValueError),
        (lambda: OutlineEntry(True, "A"), TypeError),  # noqa: FBT003 - a bool is no level.
        (lambda: OutlineEntry(1, b"A"), TypeError),  # ty: ignore[invalid-argument-type]
        (lambda: OutlineEntry(1, "A", 3), TypeError),  # ty: ignore[invalid-argument-type]
        (lambda: OutlineEntry(1, "A", color=(1, 0, 0)), TypeError),  # ty: ignore[invalid-argument-type]
        (lambda: OutlineEntry(1, "A", color=Color(1, 0, 0, 0.5)), ValueError),
        (lambda: OutlineEntry(1, "A", color=Color(2, 0, 0)), ValueError),
        (lambda: OutlineEntry(1, "A", bold=1), TypeError),  # ty: ignore[invalid-argument-type]
        (lambda: Destination(-1), ValueError),
        (lambda: Destination(0, zoom=-1), ValueError),
        (lambda: Destination(0, zoom=float("nan")), ValueError),
        # pyropdf's geometry refuses non-finite coordinates with PdfError.
        (lambda: Destination(0, (1, float("inf"))), pyropdf.PdfError),
        (lambda: RemoteDestination(""), ValueError),
        (lambda: Launch(b"a.pdf"), TypeError),  # ty: ignore[invalid-argument-type]
        (lambda: NamedAction(""), ValueError),
    ],
)
def test_records_validate_on_construction(make: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        make()  # ty: ignore[call-non-callable]


def test_outline_survives_page_ops(three_page_pdf: bytes) -> None:
    """Keep outline entries readable after select while target pages remain."""
    doc = pyropdf.open(three_page_pdf)
    doc.outline = [OutlineEntry(1, "first", Destination(0)), OutlineEntry(1, "third", Destination(2))]
    doc.pages.select([0, 2])
    assert _simple(doc.outline.entries()) == [(1, "first", 0), (1, "third", 1)]  # Page numbers are compacted.


@pytest.mark.parametrize("name", ["usrguide.pdf", "bill-hr815.pdf", "f1040.pdf"])
def test_real_world_outline_readable(name: str) -> None:
    doc = pyropdf.open(REAL_WORLD / name)
    entries = doc.outline.entries()
    items = list(doc.outline.walk())
    assert len(items) == len(entries)
    for entry, item in zip(entries, items, strict=True):
        assert entry.level >= 1
        assert isinstance(entry.title, str)
        assert entry.page is None or 0 <= entry.page < len(doc.pages)
        assert item.to_entry() == entry
    # A document's own outline writes back unchanged.
    doc.outline = entries
    assert doc.outline.entries() == entries


# --- targets ---------------------------------------------------------------------


def test_every_target_kind_round_trips(three_page_pdf: bytes) -> None:
    doc = pyropdf.open(three_page_pdf)
    targets = [
        None,
        Destination(1),
        Destination(2, (10, 20)),
        Destination(0, zoom=1.5),
        Destination(1, (30, 40), 2.0),
        "https://example.com/a?b=c",
        RemoteDestination("other.pdf", 4),
        RemoteDestination("other.pdf", name="chapter-2"),
        Launch("notes.txt"),
        NamedAction("NextPage"),
    ]
    doc.outline = [OutlineEntry(1, str(index), target) for index, target in enumerate(targets)]
    for document in (doc, pyropdf.open(doc.to_bytes())):
        assert [entry.destination for entry in document.outline.entries()] == targets
        assert [item.destination for item in document.outline] == targets


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_destination_points_use_display_coordinates(rotation: int) -> None:
    doc = pyropdf.open(build_pdf(["a"]))
    doc.pages[0].rotation = rotation
    doc.outline = [OutlineEntry(1, "spot", Destination(0, (12.5, 30)))]
    (entry,) = doc.outline.entries()
    assert entry.destination == Destination(0, (12.5, 30))


def test_destinations_read_from_explicit_arrays_and_names() -> None:
    doc = pyropdf.open(
        _build_outline_fixture(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /Outlines 4 0 R /Names << /Dests 8 0 R >> >>",
                4: "<< /First 5 0 R /Last 7 0 R >>",
                5: "<< /Title (xyz) /Dest [3 0 R /XYZ 10 90 3] /Next 6 0 R >>",
                6: "<< /Title (named) /Dest (intro) /Next 7 0 R >>",
                7: "<< /Title (fith) /A << /S /GoTo /D [3 0 R /FitH 50] >> >>",
                8: "<< /Names [(intro) [3 0 R /Fit]] >>",
            }
        )
    )
    assert not doc.is_repaired
    assert [entry.destination for entry in doc.outline.entries()] == [
        Destination(0, (10, 10), 3.0),
        Destination(0),
        Destination(0, (0, 50)),
    ]


@pytest.mark.parametrize("target", [RemoteDestination("日本語.pdf", 4), Launch("café.txt")])
def test_unicode_file_targets_round_trip(target: RemoteDestination | Launch) -> None:
    doc = pyropdf.open(build_pdf(["a"]))
    doc.outline = [OutlineEntry(1, "file", target)]
    assert doc.outline[0].destination == target
    assert pyropdf.open(doc.to_bytes()).outline[0].destination == target

    item = doc.outline[0]
    item.destination = None
    item.destination = target
    assert item.destination == target
    assert pyropdf.open(doc.to_bytes()).outline[0].destination == target


@pytest.mark.parametrize(
    "target", [Destination(0, (1e100, 0)), Destination(0, (0, -1e100)), Destination(0, zoom=1e100)]
)
def test_targets_reject_pdf_real_overflow_before_mutation(target: Destination) -> None:
    doc = pyropdf.open(build_pdf(["a"]))
    original = [OutlineEntry(1, "original", Destination(0))]
    doc.outline = original
    item = doc.outline[0]
    before = doc.to_bytes()

    with pytest.raises(pyropdf.PdfError, match="PDF real-number range"):
        doc.outline = [OutlineEntry(1, "invalid", target)]
    assert doc.to_bytes() == before
    assert doc.outline.entries() == original

    with pytest.raises(pyropdf.PdfError, match="PDF real-number range"):
        item.destination = target
    assert doc.to_bytes() == before
    assert item.destination == Destination(0)


def test_targets_reject_overflow_after_display_coordinate_conversion() -> None:
    doc = pyropdf.open(build_pdf(["a"]))
    doc.pages[0].obj["MediaBox"] = [2e38, 0, 3e38, 100]
    doc.outline = [OutlineEntry(1, "original", Destination(0))]
    item = doc.outline[0]
    before = doc.to_bytes()
    target = Destination(0, (2e38, 0))

    with pytest.raises(pyropdf.PdfError, match="PDF real-number range"):
        item.destination = target
    assert doc.to_bytes() == before
    assert item.destination == Destination(0)

    with pytest.raises(pyropdf.PdfError, match="PDF real-number range"):
        doc.outline = [OutlineEntry(1, "invalid", target)]
    assert doc.to_bytes() == before


# --- item handles ----------------------------------------------------------------


def test_items_are_handles_on_the_tree(three_page_pdf: bytes) -> None:
    doc = pyropdf.open(three_page_pdf)
    doc.outline = BOOK
    outline = doc.outline
    assert len(outline) == 2
    first, second = outline
    assert outline[0] == first
    assert outline[-1] == second
    assert outline[:1] == [first]
    assert first != second
    assert len({first, outline[0], second}) == 2
    assert (first.level, first.title, first.page) == (1, "第 1 章", 0)
    (child,) = first.children
    assert (child.level, child.title, child.destination) == (2, "1.1 節", Destination(1))
    assert child.children == ()
    assert second.children == ()
    assert list(outline.walk()) == [first, child, second]
    assert repr(first) == f"OutlineItem(xref={first.xref}, title='第 1 章')"
    assert first.obj["Title"].text == "第 1 章"
    assert repr(outline).startswith("Outline([OutlineItem(")


def test_editing_an_item_in_place(three_page_pdf: bytes) -> None:
    doc = pyropdf.open(three_page_pdf)
    doc.outline = BOOK
    page = doc.pages[0]
    item = doc.outline[0].children[0]
    item.title = "Renamed ✓"
    item.color = Color(0, 0.5, 1)
    item.bold = True
    item.italic = True
    item.italic = False
    item.destination = "https://example.com"
    assert item.to_entry() == OutlineEntry(2, "Renamed ✓", "https://example.com", color=Color(0, 0.5, 1), bold=True)
    item.color = None
    item.destination = Destination(2, (5, 5))
    item.destination = None
    assert item.to_entry() == OutlineEntry(2, "Renamed ✓", bold=True)
    reloaded = pyropdf.open(doc.to_bytes())
    assert reloaded.outline.entries()[1] == OutlineEntry(2, "Renamed ✓", bold=True)
    # In-place edits keep page views valid.
    assert page.text().strip() == "Page one"


def test_item_edits_validate_before_any_change(three_page_pdf: bytes) -> None:
    doc = pyropdf.open(three_page_pdf)
    doc.outline = BOOK
    item = doc.outline[0]
    with pytest.raises(TypeError):
        item.title = 5  # ty: ignore[invalid-assignment]
    with pytest.raises(ValueError, match="out of range"):
        item.destination = Destination(3)
    with pytest.raises(TypeError):
        item.destination = 2  # ty: ignore[invalid-assignment]
    with pytest.raises(ValueError, match="no transparency"):
        item.color = Color(1, 0, 0, 0)
    with pytest.raises(TypeError):
        item.bold = None  # ty: ignore[invalid-assignment]
    with pytest.raises(LimitError) as caught:
        item.title = "é" * (MAX_TOC_TEXT_BYTES // 2)
    assert caught.value.code == "toc_input_size"
    with pytest.raises(pyropdf.PdfError, match="no open state"):
        item.children[0].is_open = True
    assert doc.outline.entries() == BOOK


@pytest.mark.parametrize("property_name", ["title", "destination"])
def test_item_text_edits_preserve_the_aggregate_read_limit(three_page_pdf: bytes, property_name: str) -> None:
    doc = pyropdf.open(three_page_pdf)
    original = [OutlineEntry(1, "A" * (MAX_TOC_TEXT_BYTES // 2)), OutlineEntry(1, "B")]
    doc.outline = original
    item = doc.outline[1]
    with pytest.raises(LimitError) as caught:
        setattr(item, property_name, "C" * (MAX_TOC_TEXT_BYTES // 2 + 1))
    assert caught.value.code == "toc_input_size"
    assert doc.outline.entries() == original


@pytest.mark.parametrize("color", ["[2 0 0]", "[-1 0 0]"])
def test_invalid_source_colors_read_as_the_viewer_default(color: str) -> None:
    doc = pyropdf.open(
        _build_outline_fixture(
            {
                4: "<< /First 5 0 R >>",
                5: f"<< /Title (A) /Dest [3 0 R /Fit] /C {color} >>",
            }
        )
    )
    expected = OutlineEntry(1, "A", Destination(0))
    assert doc.outline[0].color is None
    assert doc.outline[0].to_entry() == expected
    assert doc.outline.entries() == [expected]
    doc.outline = doc.outline.entries()
    assert pyropdf.open(doc.to_bytes()).outline.entries() == [expected]


def test_open_states_keep_counts_consistent(three_page_pdf: bytes) -> None:
    doc = pyropdf.open(three_page_pdf)
    doc.outline = [
        OutlineEntry(1, "A", Destination(0), is_open=True),
        OutlineEntry(2, "A1", Destination(0), is_open=False),
        OutlineEntry(3, "A1a", Destination(0)),
        OutlineEntry(3, "A1b", Destination(0)),
        OutlineEntry(2, "A2", Destination(1)),
        OutlineEntry(1, "B", Destination(2), is_open=False),
        OutlineEntry(2, "B1", Destination(2)),
    ]

    def counts() -> list[object]:
        return [doc.catalog["Outlines"]["Count"], *(item.obj.get("Count") for item in doc.outline.walk())]

    # ISO 32000-2 Table 151: an open item counts its visible descendants, a
    # closed one the negative of those that opening it would show.
    assert counts() == [4, 2, -2, None, None, None, -1, None]
    a1 = doc.outline[0].children[0]
    a1.is_open = True
    assert a1.is_open
    assert counts() == [6, 4, 2, None, None, None, -1, None]
    doc.outline[0].is_open = False
    assert counts() == [2, -4, 2, None, None, None, -1, None]
    assert [entry.is_open for entry in doc.outline.entries()] == [False, True, False, False, False, False, False]
    assert pyropdf.open(doc.to_bytes()).outline.entries() == doc.outline.entries()


@pytest.mark.parametrize("direct_root", [True, False])
def test_open_state_recounts_direct_and_aliased_outline_roots(*, direct_root: bool) -> None:
    root = "<< /First 5 0 R /Count 1 >>" if direct_root else "4 0 R"
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: f"<< /Type /Catalog /Pages 2 0 R /Outlines {root} >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
                4: "7 0 R",
                5: "<< /Title (A) /First 6 0 R /Count -1 >>",
                6: "<< /Title (B) >>",
                7: "<< /First 5 0 R /Count 1 >>",
            }
        )
    )
    item = doc.outline[0]
    assert not item.is_open
    assert doc.catalog["Outlines"]["Count"] == 1

    item.is_open = True
    assert item.is_open
    assert doc.catalog["Outlines"]["Count"] == 2
    assert pyropdf.open(doc.to_bytes()).catalog["Outlines"]["Count"] == 2

    item.is_open = False
    assert not item.is_open
    assert doc.catalog["Outlines"]["Count"] == 1


@pytest.mark.parametrize("first", ["6 0 R", "99 0 R"])
def test_null_and_missing_children_have_no_open_state(first: str) -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /Outlines 4 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
                4: "<< /First 5 0 R /Count 2 >>",
                5: f"<< /Title (A) /First {first} /Count 1 >>",
                6: "null",
            }
        )
    )
    item = doc.outline[0]
    assert item.children == ()
    assert not item.is_open
    before = doc.to_bytes()

    for is_open in (False, True):
        with pytest.raises(pyropdf.PdfError, match="without children"):
            item.is_open = is_open
        assert doc.to_bytes() == before


def test_handles_go_stale_when_the_outline_is_replaced(three_page_pdf: bytes) -> None:
    doc = pyropdf.open(three_page_pdf)
    doc.outline = BOOK
    item = doc.outline[0]
    doc.outline = BOOK
    with pytest.raises(pyropdf.StaleObjectError):
        _ = item.title
    with pytest.raises(pyropdf.StaleObjectError):
        item.title = "x"
    assert repr(item).startswith("<stale OutlineItem")
    doc.history.undo()
    assert item.title == "第 1 章"
    doc.close()
    with pytest.raises(pyropdf.DocumentClosedError):
        _ = doc.outline


def test_direct_items_appear_in_entries_but_not_as_handles() -> None:
    doc = pyropdf.open(
        _build_outline_fixture(
            {
                4: "<< /First 5 0 R >>",
                5: "<< /Title (indirect) /Dest [3 0 R /Fit] /Next << /Title (direct) /Dest [3 0 R /Fit] >> >>",
            }
        )
    )
    assert _simple(doc.outline.entries()) == [(1, "indirect", 0), (1, "direct", 0)]
    assert [item.title for item in doc.outline.walk()] == ["indirect"]
    assert [item.title for item in doc.outline] == ["indirect"]


# --- the memoized walk ---------------------------------------------------------


def test_the_walk_is_kept_across_page_edits(three_page_pdf: bytes) -> None:
    doc = pyropdf.open(three_page_pdf)
    doc.outline = BOOK
    core = doc._core()  # noqa: SLF001 - the memo's counts are internal.
    hits, computations = _core.outline_walk_counts(core)
    assert doc.outline.entries() == BOOK
    assert doc.outline.entries() == BOOK
    assert _core.outline_walk_counts(core) == (hits + 1, computations + 1)
    # A page edit leaves the outline's objects alone, and the pages resolve
    # again in the new page index.
    doc.pages.select([2, 0, 1])
    assert _simple(doc.outline.entries()) == [(1, "第 1 章", 1), (2, "1.1 節", 2), (1, "第 2 章", 0)]
    assert _core.outline_walk_counts(core) == (hits + 2, computations + 1)
    doc.outline[0].title = "Renamed"
    assert doc.outline.entries()[0].title == "Renamed"
    assert _core.outline_walk_counts(core) == (hits + 2, computations + 2)


# --- read limits -----------------------------------------------------------------


def test_next_cycle_is_visited_once() -> None:
    doc = pyropdf.open(
        _build_outline_fixture(
            {
                4: "<< /First 5 0 R >>",
                5: "<< /Title (A) /Dest [3 0 R /Fit] /Next 5 0 R >>",
            }
        )
    )
    assert _simple(doc.outline.entries()) == [(1, "A", 0)]
    assert [item.title for item in doc.outline] == ["A"]


def test_unresolved_destinations_read_as_none() -> None:
    objects = {
        4: "<< /First 5 0 R >>",
        5: "<< /Title (cycle) /Dest 6 0 R /Next 7 0 R >>",
        6: "<< /D 6 0 R >>",
        7: "<< /Title (deep) /Dest 8 0 R /Next 41 0 R >>",
        41: "<< /Title (missing page) /Dest [99 0 R /Fit] >>",
    }
    for object_id in range(8, 40):
        objects[object_id] = f"<< /D {object_id + 1} 0 R >>"
    objects[40] = "[3 0 R /Fit]"
    doc = pyropdf.open(_build_outline_fixture(objects))
    assert _simple(doc.outline.entries()) == [(1, "cycle", None), (1, "deep", None), (1, "missing page", None)]


def test_reads_refuse_excessive_depth() -> None:
    objects = {4: "<< /First 5 0 R >>"}
    for object_id in range(5, 69):
        objects[object_id] = f"<< /First {object_id + 1} 0 R >>"
    objects[69] = "<< >>"
    doc = pyropdf.open(_build_outline_fixture(objects))
    with pytest.raises(pyropdf.PdfError, match="64-level safety limit"):
        doc.outline.entries()


def _outline_chain(count: int, entry: str = "") -> dict[int, str]:
    """Return ``count`` sibling outline items linked from object 5."""
    objects = {4: "<< /First 5 0 R >>"}
    last = 4 + count
    for object_id in range(5, last + 1):
        next_entry = f" /Next {object_id + 1} 0 R" if object_id < last else ""
        objects[object_id] = f"<<{entry}{next_entry} >>"
    return objects


def test_reads_entries_beyond_the_former_4096_limit() -> None:
    doc = pyropdf.open(_build_outline_fixture(_outline_chain(4_097, " /Title (A) /Dest [3 0 R /Fit]")))
    assert _simple(doc.outline.entries()) == [(1, "A", 0)] * 4_097
    assert len(doc.outline) == 4_097


def test_reads_refuse_excessive_nodes() -> None:
    doc = pyropdf.open(_build_outline_fixture(_outline_chain(MAX_TOC_ENTRIES + 1)))
    with pytest.raises(LimitError, match="65536-node safety limit") as caught:
        doc.outline.entries()
    assert caught.value.code == "toc_entry_count"
    with pytest.raises(LimitError, match="65536-entry safety limit") as caught:
        len(doc.outline)
    assert caught.value.code == "toc_entry_count"


def test_reads_refuse_excessive_edges() -> None:
    objects = {4: "<< /First 5 0 R >>"}
    last = 4 + MAX_TOC_ENTRIES
    for object_id in range(5, last + 1):
        next_id = object_id + 1 if object_id < last else 5
        objects[object_id] = f"<< /First 5 0 R /Next {next_id} 0 R >>"
    doc = pyropdf.open(_build_outline_fixture(objects))
    with pytest.raises(LimitError, match="131072-edge safety limit") as caught:
        doc.outline.entries()
    assert caught.value.code == "toc_entry_count"


def test_reads_refuse_excessive_title_bytes() -> None:
    title = "x" * (1024 * 1024)
    objects = {4: "<< /First 5 0 R >>"}
    for object_id in range(5, 22):
        next_entry = f" /Next {object_id + 1} 0 R" if object_id < 21 else ""
        objects[object_id] = f"<< /Title ({title}) /Dest [3 0 R /Fit]{next_entry} >>"
    doc = pyropdf.open(_build_outline_fixture(objects))
    with pytest.raises(LimitError, match="TOC source text exceeds the 16777216-byte safety limit") as caught:
        doc.outline.entries()
    assert caught.value.code == "toc_text_size"


# --- write limits ----------------------------------------------------------------


def test_assigning_refuses_excessive_entries(three_page_pdf: bytes) -> None:
    doc = pyropdf.open(three_page_pdf)
    with pytest.raises(LimitError, match="65536 items") as caught:
        doc.outline = [OutlineEntry(1, "A", Destination(0))] * (MAX_TOC_ENTRIES + 1)
    assert caught.value.code == "toc_entry_count"
    assert doc.outline.entries() == []

    core = doc._core()  # noqa: SLF001 - the core repeats every check.
    target = ("goto", 1, None, None, None, None)
    with pytest.raises(LimitError, match="65536 outline items") as core_caught:
        _core.outline_write(core, [(1, "A", target, None, 0, False)] * (MAX_TOC_ENTRIES + 1))
    assert core_caught.value.code == "toc_entry_count"
    assert doc.outline.entries() == []


def test_write_limits_are_atomic(three_page_pdf: bytes) -> None:
    doc = pyropdf.open(three_page_pdf)
    original = [OutlineEntry(1, "original", Destination(0))]
    doc.outline = original

    with pytest.raises(LimitError, match="16777216-byte encoded-text safety limit") as caught:
        doc.outline = [OutlineEntry(1, "é" * (MAX_TOC_TEXT_BYTES // 2), Destination(0))]
    assert caught.value.code == "toc_input_size"
    with pytest.raises(LimitError) as caught:
        doc.outline = [OutlineEntry(1, "uri", "x" * MAX_TOC_TEXT_BYTES)]
    assert caught.value.code == "toc_input_size"
    assert doc.outline.entries() == original

    core = doc._core()  # noqa: SLF001 - the core repeats every check.
    target = ("goto", 1, None, None, None, None)
    with pytest.raises(pyropdf.PdfError, match="64-level safety limit"):
        _core.outline_write(core, [(level, str(level), target, None, 0, False) for level in range(1, 66)])
    for bad in (
        ("goto", 4, None, None, None, None),
        ("goto", 1, (float("nan"), 0.0), None, None, None),
        ("uri", None, None, None, "", None),
        ("teleport", None, None, None, None, None),
    ):
        with pytest.raises(pyropdf.PdfError):
            _core.outline_write(core, [(1, "bad", bad, None, 0, False)])
    with pytest.raises(pyropdf.PdfError, match="from 0 to 1"):
        _core.outline_write(core, [(1, "bad", target, (2.0, 0.0, 0.0), 0, False)])
    assert doc.outline.entries() == original


@pytest.mark.parametrize("operation", ["replace", "target"])
def test_waiting_outline_target_uses_the_latest_page_order(three_page_pdf: bytes, operation: str) -> None:
    doc = pyropdf.open(three_page_pdf)
    doc.outline = [OutlineEntry(1, "before", Destination(0))]
    item = doc.outline[0]
    started = Event()

    def write() -> None:
        started.set()
        if operation == "replace":
            doc.outline = [OutlineEntry(1, "after", Destination(0))]
        else:
            item.destination = Destination(0)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with doc.transaction("Reorder pages before the waiting outline edit"):
            future = pool.submit(write)
            assert started.wait(timeout=10)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
            doc.pages.move(0, 2)
        future.result(timeout=10)
    assert doc.outline[0].destination == Destination(0)


@pytest.mark.parametrize("operation", ["bold", "color"])
def test_waiting_outline_style_preserves_the_first_writers_changes(three_page_pdf: bytes, operation: str) -> None:
    doc = pyropdf.open(three_page_pdf)
    doc.outline = [OutlineEntry(1, "item")]
    item = doc.outline[0]
    started = Event()

    def write() -> None:
        started.set()
        if operation == "bold":
            item.bold = True
        else:
            item.color = Color(1, 0, 0)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with doc.transaction("Set italic before the waiting style edit"):
            future = pool.submit(write)
            assert started.wait(timeout=10)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
            item.italic = True
        future.result(timeout=10)
    assert item.italic
    assert item.bold if operation == "bold" else item.color == Color(1, 0, 0)


@pytest.mark.parametrize("tail", ["null", "5 0 R", "999 0 R"])
def test_outline_depth_limit_ignores_absent_or_visited_children(tail: str) -> None:
    objects = {4: "<< /First 5 0 R >>"}
    for number in range(5, 69):
        child = f"{number + 1} 0 R" if number < 68 else tail
        objects[number] = f"<< /Title (item) /First {child} >>"
    doc = pyropdf.open(_build_outline_fixture(objects))
    assert len(doc.outline.entries()) == 64
    doc.outline[0].is_open = True
    assert len(doc.outline.entries()) == 64


@pytest.mark.parametrize("kind", ["direct", "alias"])
def test_replacing_a_direct_outline_frees_indirect_descendants(kind: str) -> None:
    first = "<< /Title (direct) /Next 5 0 R >>" if kind == "direct" else "6 0 R"
    objects = {
        1: f"<< /Type /Catalog /Pages 2 0 R /Outlines << /First {first} >> >>",
        2: "<< /Type /Pages /Kids [] /Count 0 >>",
        5: "<< /Title (indirect) >>",
    }
    if kind == "alias":
        objects[6] = "5 0 R"
    doc = pyropdf.open(build_raw_pdf(objects))
    doc.outline = []
    assert set(doc.objects) == {1, 2}


def test_failed_outline_cleanup_rolls_back_inside_an_open_transaction() -> None:
    doc = pyropdf.open(
        _build_outline_fixture({4: "<< /First 5 0 R >>", 5: "<< /Title (keep) >>", 6: "<< /Broken [ >>"})
    )
    with doc.transaction("Keep a failed replacement atomic"):
        with pytest.raises(pyropdf.MalformedPdfError):
            doc.outline = []
        assert [item.title for item in doc.outline] == ["keep"]
    assert [item.title for item in doc.outline] == ["keep"]


@pytest.mark.parametrize("kind", ["Page", "Pages", "Catalog"])
def test_open_state_refuses_an_outline_root_aliasing_another_identity(kind: str) -> None:
    doc = pyropdf.open(
        _build_outline_fixture(
            {
                4: f"<< /Type /{kind} /First 5 0 R /Count 99 >>",
                5: "<< /Title (parent) /First 6 0 R >>",
                6: "<< /Title (child) >>",
            }
        )
    )
    core = doc._core()  # noqa: SLF001
    opened = True
    with pytest.raises(pyropdf.MalformedPdfError, match="outline"):
        _core.outline_set_open(core, (5, 0), opened)
    assert doc.objects[4]["Count"] == 99


def test_outline_aliases_name_the_resolved_item_identity() -> None:
    doc = pyropdf.open(
        _build_outline_fixture({4: "<< /First 5 0 R >>", 5: "6 0 R", 6: "<< /Title (item) /Next 7 0 R >>", 7: "6 0 R"})
    )
    assert len(doc.outline.entries()) == 1
    assert [item.xref for item in doc.outline.walk()] == [6]
    assert [item.xref for item in doc.outline] == [6]
    doc.outline[0].title = "changed"
    assert doc.objects[6]["Title"] == b"changed"


def test_open_state_counts_direct_children_and_updates_their_ancestors() -> None:
    doc = pyropdf.open(
        _build_outline_fixture(
            {
                4: "<< /First 5 0 R >>",
                5: "<< /Title (top) /First << /Title (direct) /First 6 0 R /Count -1 >> /Count -1 >>",
                6: "<< /Title (nested) /First 7 0 R /Count -1 >>",
                7: "<< /Title (leaf) >>",
            }
        )
    )
    doc.outline[0].is_open = True
    assert doc.objects[5]["Count"] == 1
    nested = list(doc.outline.walk())[1]
    nested.is_open = True
    assert doc.objects[5]["First"]["Count"] == -2
    assert doc.objects[6]["Count"] == 1
    assert doc.objects[5]["Count"] == 1
    assert doc.catalog["Outlines"]["Count"] == 2


@pytest.mark.parametrize("field", ["title", "destination"])
def test_outline_rejects_excessive_characters_before_the_binding(field: str) -> None:
    text = "x" * (MAX_TOC_TEXT_BYTES + 1)
    title, destination = (text, None) if field == "title" else ("item", text)
    with pytest.raises(LimitError) as caught:
        OutlineEntry(1, title, destination)
    assert caught.value.code == "toc_input_size"


def test_outline_bounds_aggregate_characters_before_the_binding(
    three_page_pdf: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = pyropdf.open(three_page_pdf)
    entries = [OutlineEntry(1, "x" * (MAX_TOC_TEXT_BYTES // 2 + 1))] * 2

    def unexpected(*_args: object) -> None:
        pytest.fail("oversized text reached the binding")

    monkeypatch.setattr(_core, "outline_write", unexpected)
    with pytest.raises(LimitError) as caught:
        doc.outline = entries
    assert caught.value.code == "toc_input_size"


def test_renumbering_a_save_preserves_current_outline_handles(three_page_pdf: bytes) -> None:
    doc = pyropdf.open(three_page_pdf)
    doc.outline = BOOK
    item = doc.outline[0]
    identity = item.xref
    saved = doc.to_bytes(renumber=True)
    assert pyropdf.open(saved).outline.entries() == BOOK
    assert item.xref == identity
    assert item.title == BOOK[0].title
    item.title = "still live"
    assert doc.outline[0].title == "still live"


def test_outline_handle_refuses_a_parent_cycle() -> None:
    doc = pyropdf.open(_build_outline_fixture({4: "<< /First 5 0 R >>", 5: "<< /Title (cycle) /Parent 5 0 R >>"}))
    with pytest.raises(pyropdf.PdfError, match="parent cycle"):
        _ = doc.outline[0].level
