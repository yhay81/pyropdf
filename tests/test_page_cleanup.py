"""Keep navigation from retaining pages that structural edits remove.

pylopdf's tests/test_page_cleanup.py at ebb01d3c, ported. Intended
differences from pylopdf:

- The outline is read through raw objects, since pyropdf has no outline
  API yet.
- Selecting a page twice needs page copies (yhay81/pyropdf#175), so the
  duplicate-selection case selects each page once.
- The rules read the references to the removed pages through the
  reverse-reference index instead of walking every destination, so a
  name tree past the named-destination limit that leads nowhere near a
  removed page no longer refuses the edit: it stays as it was, and reading
  the links still refuses it.
- A named-destination tree keeps its shape: an entry to a removed page
  leaves the leaf that holds it, where pylopdf rewrote the whole tree as
  one flat leaf.
- The removed page and what only it referred to are freed, so that a full
  save no longer holds them; pylopdf's save dropped unreachable objects.
"""

from __future__ import annotations

from typing import Any, Literal

import pytest

import pyropdf

_EXCLUDED_TEXT = "EXCLUDED-PAGE-CONTENT-9f7c"


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


def _destination_page(doc: pyropdf.Document, destination: Any) -> int | None:  # noqa: ANN401 - a PDF value.
    if destination is None:
        return None
    if isinstance(destination, (str, bytes)):
        text = destination.text if isinstance(destination, pyropdf.String) else str(destination)
        target = doc.destinations.get(text)
        return None if target is None else target.page
    pages = {page.xref: at for at, page in enumerate(doc.pages)}
    return pages.get(destination[0].xref)


def _item_page(doc: pyropdf.Document, item: pyropdf.Dictionary) -> int | None:
    if "Dest" in item:
        return _destination_page(doc, item["Dest"])
    action = item.get("A")
    if action is not None and action.get("S") == "GoTo":
        return _destination_page(doc, action.get("D"))
    return None


def outline_tuples(doc: pyropdf.Document) -> list[tuple[int, str, int | None]]:
    """Return each outline item's level, title, and page, in reading order."""
    out: list[tuple[int, str, int | None]] = []
    root = doc.catalog.get("Outlines")
    pending = [] if root is None or "First" not in root else [(1, root["First"])]
    while pending:
        level, item = pending.pop()
        out.append((level, item["Title"].text, _item_page(doc, item)))
        if "Next" in item:
            pending.append((level, item["Next"]))
        if "First" in item:
            pending.append((level + 1, item["First"]))
    return out


def _stream(text: str) -> str:
    content = f"BT /F1 20 Tf 20 100 Td ({text}) Tj ET"
    return f"<< /Length {len(content)} >>\nstream\n{content}\nendstream"


def _linked_pdf(*, indirect_annots: bool = False, excessive_names: bool = False) -> bytes:
    """Combine direct, named, legacy, outline, and external navigation."""
    annots = "[6 0 R 7 0 R 8 0 R 9 0 R 22 0 R 23 0 R 24 0 R]"
    objects: dict[int, str] = {
        1: "<< /Type /Catalog /Pages 2 0 R /Names << /Dests 11 0 R >> "
        "/Dests << /legacygone [4 0 R /Fit] /legacykeep [5 0 R /Fit] >> /Outlines 12 0 R /OpenAction [4 0 R /Fit] >>",
        2: "<< /Type /Pages /Kids [3 0 R 4 0 R 5 0 R] /Count 3 /MediaBox [0 0 200 200] "
        "/Resources << /Font << /F1 10 0 R >> >> >>",
        3: f"<< /Type /Page /Parent 2 0 R /Contents 13 0 R /Annots {'25 0 R' if indirect_annots else annots} >>",
        4: "<< /Type /Page /Parent 2 0 R /Contents 14 0 R >>",
        5: "<< /Type /Page /Parent 2 0 R /Contents 15 0 R >>",
        6: "<< /Type /Annot /Subtype /Link /Rect [1 1 10 10] /Dest [4 0 R /Fit] >>",
        7: "<< /Type /Annot /Subtype /Link /Rect [20 1 30 10] /A << /S /GoTo /D (gone) >> >>",
        8: "<< /Type /Annot /Subtype /Link /Rect [40 1 50 10] /Dest /legacygone >>",
        9: "<< /Type /Annot /Subtype /Link /Rect [60 1 70 10] /Dest (keep) >>",
        10: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        11: "<< /Kids [16 0 R 17 0 R] >>",
        12: "<< /Type /Outlines /First 18 0 R /Last 20 0 R /Count 4 >>",
        13: _stream("Retained first page"),
        14: _stream(_EXCLUDED_TEXT),
        15: _stream("Retained last page"),
        16: "<< /Names [(gone) [4 0 R /Fit]] /Limits [(gone) (gone)] >>",
        17: "<< /Names [(keep) [5 0 R /Fit]] /Limits [(keep) (keep)] >>",
        18: "<< /Title (Removed) /Dest [4 0 R /Fit] /Next 19 0 R /First 21 0 R >>",
        19: "<< /Title (Named removed) /A << /S /GoTo /D (gone) >> /Prev 18 0 R /Next 20 0 R >>",
        20: "<< /Title (Retained) /Dest (keep) /Prev 19 0 R >>",
        21: "<< /Title (Retained child) /Dest [5 0 R /Fit] /Parent 18 0 R >>",
        22: "<< /Type /Annot /Subtype /Link /Rect [80 1 90 10] /A << /S /URI /URI (https://example.com) >> >>",
        23: "<< /Type /Annot /Subtype /Link /Rect [100 1 110 10] /A << /S /GoToR /F (other.pdf) /D (gone) >> >>",
        24: "<< /Type /Annot /Subtype /Link /Rect [120 1 130 10] /Dest /legacykeep >>",
    }
    if indirect_annots:
        objects[25] = annots
    if excessive_names:
        pairs = " ".join(f"(name{index}) [5 0 R /Fit]" for index in range(65_537))
        objects[17] = f"<< /Names [{pairs}] >>"
    return build_raw_pdf(objects)


def _remove(
    doc: pyropdf.Document, operation: Literal["delete", "select"], deleted: slice | int, kept: list[int]
) -> None:
    if operation == "delete":
        del doc.pages[deleted]
    else:
        doc.pages.select(kept)


@pytest.mark.parametrize("operation", ["delete", "select"])
@pytest.mark.parametrize("indirect_annots", [False, True])
def test_excluded_page_navigation_is_removed(operation: Literal["delete", "select"], *, indirect_annots: bool) -> None:
    """Remove excluded targets while preserving retained and external links."""
    doc = pyropdf.open(_linked_pdf(indirect_annots=indirect_annots))
    if operation == "delete":
        del doc.pages[1]
        source, target = 0, 1
    else:
        doc.pages.select([2, 0])
        source, target = 1, 0
    assert len(doc.pages) == 2
    saved = doc.to_bytes()
    assert _EXCLUDED_TEXT.encode() not in saved
    for current in (doc, pyropdf.open(saved)):
        links = list(current.pages[source].links)
        assert [link.target for link in links] == [
            pyropdf.Destination(target),
            "https://example.com",
            pyropdf.RemoteDestination("other.pdf", name="gone"),
            pyropdf.Destination(target),
        ]
        # The named links that stay keep their names.
        assert [links[0].obj["Dest"], links[3].obj["Dest"]] == [pyropdf.String(b"keep"), "legacykeep"]
        # As in pymupdf, an item that led to a removed page stays without a destination.
        assert outline_tuples(current) == [
            (1, "Removed", None),
            (2, "Retained child", target),
            (1, "Named removed", None),
            (1, "Retained", target),
        ]
        assert "OpenAction" not in current.catalog
        assert _EXCLUDED_TEXT not in current.text()


@pytest.mark.parametrize("indirect_annots", [False, True])
def test_selected_pages_keep_clean_navigation(*, indirect_annots: bool) -> None:
    """Clean direct and shared annotation arrays on the selected pages."""
    doc = pyropdf.open(_linked_pdf(indirect_annots=indirect_annots))
    doc.pages.select([0, 2])
    saved = doc.to_bytes()
    assert _EXCLUDED_TEXT.encode() not in saved
    reloaded = pyropdf.open(saved)
    targets = [link.target for link in reloaded.pages[0].links]
    assert len(targets) == 4
    assert [target.page for target in targets if isinstance(target, pyropdf.Destination)] == [1, 1]


@pytest.mark.parametrize("operation", ["delete", "select"])
def test_removing_every_page_drops_outline_and_destination_references(operation: Literal["delete", "select"]) -> None:
    doc = pyropdf.open(_linked_pdf())
    _remove(doc, operation, slice(0, 3), [])
    saved = doc.to_bytes()
    assert _EXCLUDED_TEXT.encode() not in saved
    for current in (doc, pyropdf.open(saved)):
        assert len(current.pages) == 0
        assert outline_tuples(current) == []


@pytest.mark.parametrize("operation", ["delete", "select"])
def test_a_name_tree_past_the_limit_away_from_the_removed_page_stays(operation: Literal["delete", "select"]) -> None:
    doc = pyropdf.open(_linked_pdf(excessive_names=True))
    _remove(doc, operation, 1, [0, 2])
    assert len(doc.pages) == 2
    assert len(doc.catalog["Names"]["Dests"]["Kids"][1]["Names"]) == 2 * 65_537
    with pytest.raises(pyropdf.LimitError, match="65536-entry safety limit") as caught:
        list(doc.pages[0].links)
    assert caught.value.code == "named_destination_count"


@pytest.mark.parametrize("operation", ["delete", "select"])
def test_cleanup_refuses_navigation_tree_aliasing_a_page(operation: Literal["delete", "select"]) -> None:
    """Do not edit a page when a malformed destination root aliases it."""
    data = _linked_pdf().replace(b"/Dests 11 0 R", b"/Dests 3 0 R ")
    doc = pyropdf.open(data)
    before = doc.to_bytes()
    with pytest.raises(pyropdf.PdfError, match="cannot alias a page"):
        _remove(doc, operation, 1, [0, 2])
    assert len(doc.pages) == 3
    assert doc.to_bytes() == before


@pytest.mark.parametrize("operation", ["delete", "select"])
def test_cleanup_refuses_annotation_aliasing_a_page(operation: Literal["delete", "select"]) -> None:
    """Do not treat navigation-bearing page dictionaries as annotations."""
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R 5 0 R] /Count 3 /MediaBox [0 0 200 200] >>",
                3: "<< /Type /Page /Parent 2 0 R /Annots [5 0 R] >>",
                4: "<< /Type /Page /Parent 2 0 R /Contents 6 0 R >>",
                5: "<< /Type /Page /Parent 2 0 R /Subtype /Link /Rect [1 1 10 10] /Dest [4 0 R /Fit] >>",
                6: _stream(_EXCLUDED_TEXT),
            }
        )
    )
    page = doc.pages[0]
    before = doc.to_bytes()
    with pytest.raises(pyropdf.PdfError, match="cannot alias a page"):
        _remove(doc, operation, 1, [0, 2])
    assert doc.to_bytes() == before
    assert len(doc.pages) == 3
    assert page.rect == pyropdf.Rect(0, 0, 200, 200)


@pytest.mark.parametrize("operation", ["delete", "select"])
def test_cleanup_refuses_outline_root_aliasing_a_page(operation: Literal["delete", "select"]) -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /Outlines 4 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 200 200] >>",
                3: "<< /Type /Page /Parent 2 0 R >>",
                4: "<< /Type /Page /Parent 2 0 R /Contents 6 0 R /First 5 0 R >>",
                5: "<< /Title (Retained) /Dest [3 0 R /Fit] >>",
                6: _stream(_EXCLUDED_TEXT),
            }
        )
    )
    page = doc.pages[0]
    before = doc.to_bytes()
    with pytest.raises(pyropdf.PdfError, match="cannot alias a page"):
        _remove(doc, operation, 1, [0])
    assert doc.to_bytes() == before
    assert len(doc.pages) == 2
    assert page.rect == pyropdf.Rect(0, 0, 200, 200)


@pytest.mark.parametrize("operation", ["delete", "select"])
def test_cleanup_preserves_indirect_name_tree_keys(operation: Literal["delete", "select"]) -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /Names << /Dests 5 0 R >> >>",
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 200 200] >>",
                3: "<< /Type /Page /Parent 2 0 R /Annots [7 0 R] >>",
                4: "<< /Type /Page /Parent 2 0 R >>",
                5: "<< /Names [6 0 R [3 0 R /Fit]] >>",
                6: "(keep)",
                7: "<< /Type /Annot /Subtype /Link /Rect [1 1 10 10] /Dest (keep) >>",
            }
        )
    )
    assert doc.pages[0].links[0].target == pyropdf.Destination(0)
    _remove(doc, operation, 1, [0])
    saved = doc.to_bytes()
    for current in (doc, pyropdf.open(saved)):
        (link,) = current.pages[0].links
        assert link.target == pyropdf.Destination(0)
        assert link.obj["Dest"] == pyropdf.String(b"keep")


@pytest.mark.parametrize("operation", ["delete", "select"])
def test_cleanup_refuses_names_container_aliasing_a_page(operation: Literal["delete", "select"]) -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /Names 4 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 200 200] >>",
                3: "<< /Type /Page /Parent 2 0 R >>",
                4: "<< /Type /Page /Parent 2 0 R /Contents 6 0 R /Dests 5 0 R >>",
                5: "<< /Names [(keep) [3 0 R /Fit]] >>",
                6: _stream(_EXCLUDED_TEXT),
            }
        )
    )
    page = doc.pages[0]
    before = doc.to_bytes()
    with pytest.raises(pyropdf.PdfError, match="cannot alias a page"):
        _remove(doc, operation, 1, [0])
    assert doc.to_bytes() == before
    assert len(doc.pages) == 2
    assert page.rect == pyropdf.Rect(0, 0, 200, 200)


@pytest.mark.parametrize("operation", ["delete", "select"])
@pytest.mark.parametrize(
    "catalog",
    [
        "/Outlines null",
        "/Outlines 7 0 R",
        "/Outlines 90 0 R",
        "/Names null",
        "/Names << /Dests null >>",
        "/Names << /Dests 90 0 R >>",
    ],
    ids=[
        "null-outlines",
        "indirect-null-outlines",
        "unallocated-outlines",
        "null-names",
        "null-dests",
        "unallocated-dests",
    ],
)
def test_cleanup_reads_null_navigation_roots_as_absent(operation: Literal["delete", "select"], catalog: str) -> None:
    """A null value, an indirect null, and an unallocated reference omit the entry (ISO 32000-2 §7.3.7, §7.3.10)."""
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: f"<< /Type /Catalog /Pages 2 0 R {catalog} >>",
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 200 200] "
                "/Resources << /Font << /F1 5 0 R >> >> >>",
                3: "<< /Type /Page /Parent 2 0 R /Contents 6 0 R >>",
                4: "<< /Type /Page /Parent 2 0 R /Contents 8 0 R >>",
                5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                6: _stream("Retained page"),
                7: "null",
                8: _stream(_EXCLUDED_TEXT),
            }
        )
    )
    _remove(doc, operation, 1, [0])
    saved = doc.to_bytes()
    assert _EXCLUDED_TEXT.encode() not in saved
    for current in (doc, pyropdf.open(saved)):
        assert len(current.pages) == 1
        assert current.pages[0].text().split() == ["Retained", "page"]


def test_a_shared_resource_stays_and_an_annotation_of_the_removed_page_goes() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 200 200] >>",
                3: "<< /Type /Page /Parent 2 0 R /Contents 6 0 R /Resources << /Font << /F1 5 0 R >> >> "
                "/Annots [8 0 R] >>",
                4: "<< /Type /Page /Parent 2 0 R /Contents 7 0 R /Resources << /Font << /F1 5 0 R >> >> "
                "/Annots [9 0 R] >>",
                5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                6: _stream("Retained page"),
                7: _stream(_EXCLUDED_TEXT),
                8: "<< /Type /Annot /Subtype /Text /Rect [0 0 10 10] /P 4 0 R /Contents (stays) >>",
                9: "<< /Type /Annot /Subtype /Text /Rect [0 0 10 10] /P 4 0 R /Contents (EXCLUDED-NOTE) >>",
            }
        )
    )
    del doc.pages[1]
    saved = doc.to_bytes()
    assert _EXCLUDED_TEXT.encode() not in saved
    assert b"EXCLUDED-NOTE" not in saved
    reloaded = pyropdf.open(saved)
    assert reloaded.pages[0].text().split() == ["Retained", "page"]
    (note,) = reloaded.pages[0].annotations
    assert "P" not in note.obj
    assert note.obj["Contents"] == pyropdf.String(b"stays")
