"""A placed page's Form is reused exactly while what it was made from is unchanged.

``Canvas.page`` keeps, for each Form it writes, the stamps of every source
object that the page's content and resources reach (#503). These tests
check reuse, invalidation by relevant and only relevant edits, one source
version under concurrent edits, and the operation budget of the cache.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

import pyropdf
from pyropdf import Name, _core
from pyropdf._canvas import _cache

if TYPE_CHECKING:
    from collections.abc import Callable

# ruff: noqa: SLF001, FBT003 - the private cache and core are the interfaces under test.


def _source(document: pyropdf.Document | None = None) -> tuple[pyropdf.Document, pyropdf.Page, dict[str, int]]:
    """Return a page with a font, an image and a nested Form, and their numbers."""
    document = document or pyropdf.Document()
    page = document.pages.new((200, 100))
    font = document.objects.add({"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica")})
    image = document.objects.add_stream(
        b"\xff\x00\x00",
        {
            "Type": Name("XObject"),
            "Subtype": Name("Image"),
            "Width": 1,
            "Height": 1,
            "ColorSpace": Name("DeviceRGB"),
            "BitsPerComponent": 8,
        },
    )
    inner = document.objects.add_stream(
        b"0 0 1 rg 0 0 5 5 re f", {"Type": Name("XObject"), "Subtype": Name("Form"), "BBox": [0, 0, 5, 5]}
    )
    page.obj["Resources"] = {
        "Font": {"F1": document.objects[font]},
        "XObject": {"Im1": document.objects[image], "Fm1": document.objects[inner]},
    }
    page.obj["Contents"] = document.objects[
        document.objects.add_stream(b"BT /F1 12 Tf 10 50 Td (Placed) Tj ET q 10 0 0 10 0 0 cm /Im1 Do Q /Fm1 Do")
    ]
    return document, page, {"font": font, "image": image, "inner": inner}


def _place(target_document: pyropdf.Document, source: pyropdf.Page, *, times: int = 1) -> list[int]:
    """Place ``source`` on a new page of ``target_document`` in one canvas, and return the Forms it drew."""
    target = target_document.pages.new((200, 100 * times))
    with target.draw() as canvas:
        for at in range(times):
            canvas.page((0, 100 * at, 200, 100 * at + 100), source)
    return _forms(target)


def _forms(target: pyropdf.Page) -> list[int]:
    """Return the Forms that the target page's last drawing names, in name order."""
    xobjects = target.obj["Resources"]["XObject"]
    return [xobjects[name].xref for name in sorted(xobjects) if name.startswith("PyroX")]


# Reuse.


def test_an_unchanged_page_of_the_same_document_reuses_one_form() -> None:
    document, source, _ = _source()
    first = _place(document, source, times=2)
    assert len(set(first)) == 1
    assert document.pages[-1].text().count("Placed") == 2
    # Another canvas, on another page of the same document, reuses it too.
    assert _place(document, source) == first
    assert len(_cache(document).forms) == 1


def test_an_unchanged_page_of_another_document_reuses_one_form() -> None:
    _, source, _ = _source()
    target_document = pyropdf.Document()
    first = _place(target_document, source, times=2)
    assert len(set(first)) == 1
    count = len(target_document.objects)
    assert _place(target_document, source) == first
    # Only the new page and its content stream are new.
    assert len(target_document.objects) == count + 2


def test_a_reused_form_draws_the_source_as_it_was() -> None:
    document, source, _ = _source()
    _place(document, source)
    _place(document, source)
    assert "Placed" in document.pages[-1].text()


# Invalidation.


def _change_content(document: pyropdf.Document, page: pyropdf.Page, _numbers: dict[str, int]) -> None:
    page.obj["Contents"] = document.objects[document.objects.add_stream(b"BT /F1 12 Tf 10 50 Td (Other) Tj ET")]


def _change_content_in_place(_document: pyropdf.Document, page: pyropdf.Page, _numbers: dict[str, int]) -> None:
    page.obj["Contents"].write(b"BT /F1 12 Tf 10 50 Td (Other) Tj ET")


def _change_inherited_box(_document: pyropdf.Document, page: pyropdf.Page, _numbers: dict[str, int]) -> None:
    page.obj["CropBox"] = [0, 0, 150, 100]


def _change_resources(_document: pyropdf.Document, page: pyropdf.Page, _numbers: dict[str, int]) -> None:
    page.obj["Resources"]["ExtGState"] = {"G1": {"CA": 0.5}}


def _change_font(document: pyropdf.Document, _page: pyropdf.Page, numbers: dict[str, int]) -> None:
    document.objects[numbers["font"]]["BaseFont"] = Name("Courier")


def _change_image(document: pyropdf.Document, _page: pyropdf.Page, numbers: dict[str, int]) -> None:
    document.objects[numbers["image"]].write(b"\x00\xff\x00", filter=None)


def _change_nested_form(document: pyropdf.Document, _page: pyropdf.Page, numbers: dict[str, int]) -> None:
    document.objects[numbers["inner"]].write(b"1 0 0 rg 0 0 5 5 re f")


RELEVANT = [
    _change_content,
    _change_content_in_place,
    _change_inherited_box,
    _change_resources,
    _change_font,
    _change_image,
    _change_nested_form,
]


@pytest.mark.parametrize("same", [True, False], ids=["same-document", "other-document"])
@pytest.mark.parametrize("change", RELEVANT, ids=[change.__name__ for change in RELEVANT])
def test_a_relevant_source_change_makes_a_fresh_form(
    change: Callable[[pyropdf.Document, pyropdf.Page, dict[str, int]], None],
    same: bool,  # noqa: FBT001
) -> None:
    document, source, numbers = _source()
    target_document = document if same else pyropdf.Document()
    (first,) = _place(target_document, source)
    change(document, source, numbers)
    (second,) = _place(target_document, source)
    assert second != first
    # The fresh Form is reused in turn.
    assert _place(target_document, source) == [second]


def test_an_inherited_geometry_change_makes_a_fresh_form() -> None:
    document, source, _ = _source()
    del source.obj["MediaBox"]
    root = document.catalog["Pages"]
    root["MediaBox"] = [0, 0, 200, 100]
    (first,) = _place(document, source)
    assert _place(document, source) == [first]
    root["MediaBox"] = [0, 0, 180, 100]
    (second,) = _place(document, source)
    assert second != first


def test_an_inherited_resources_change_makes_a_fresh_form() -> None:
    document, source, _ = _source()
    resources = source.obj["Resources"]
    root = document.catalog["Pages"]
    root["Resources"] = {"Font": resources["Font"], "XObject": resources["XObject"]}
    del source.obj["Resources"]
    (first,) = _place(document, source)
    assert _place(document, source) == [first]
    root["Resources"]["ExtGState"] = {"G1": {"CA": 0.5}}
    (second,) = _place(document, source)
    assert second != first


def _unrelated_object(document: pyropdf.Document, _page: pyropdf.Page) -> None:
    number = document.objects.add({"Unrelated": 1})
    document.objects[number]["Unrelated"] = 2


def _new_page(document: pyropdf.Document, _page: pyropdf.Page) -> None:
    document.pages.new()


def _page_entry(_document: pyropdf.Document, page: pyropdf.Page) -> None:
    page.obj["Annots"] = []
    page.obj["Rotate"] = 90


def _another_page(document: pyropdf.Document, _page: pyropdf.Page) -> None:
    other = document.pages.new()
    other.obj["Resources"] = {"Font": {}}


UNRELATED = [
    _unrelated_object,
    _new_page,
    _page_entry,
    _another_page,
]


@pytest.mark.parametrize("change", UNRELATED, ids=[change.__name__ for change in UNRELATED])
def test_an_unrelated_edit_keeps_the_form(change: Callable[[pyropdf.Document, pyropdf.Page], None]) -> None:
    document, source, _ = _source()
    (first,) = _place(document, source)
    change(document, source)
    assert _place(document, source) == [first]


def test_an_edited_or_undone_form_is_not_reused() -> None:
    document, source, _ = _source()
    (first,) = _place(document, source)
    document.objects[first]["BBox"] = [0, 0, 1, 1]
    (second,) = _place(document, source)
    assert second != first
    # Undoing the placement and the edit gives back a Form as it was written.
    document.history.undo()
    document.history.undo()
    (third,) = _place(document, source)
    assert list(document.objects[third]["BBox"]) == [0, 0, 200, 100]


def test_a_discarded_drawing_leaves_no_reusable_form() -> None:
    document, source, _ = _source()
    target = document.pages.new((200, 100))

    def discard() -> None:
        with target.draw() as canvas:
            canvas.page((0, 0, 200, 100), source)
            msg = "discard"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="discard"):
        discard()
    assert "Placed" not in target.text()
    (form,) = _place(document, source)
    assert document.objects[form]["Subtype"] == "Form"
    assert "Placed" in document.pages[-1].text()


# One source version.


def test_a_form_reads_one_version_while_the_source_changes() -> None:
    document, source, numbers = _source()
    target_document = pyropdf.Document()
    target_document.pages.new((200, 100))
    core, source_core = target_document._core(), document._core()
    _core.begin_transaction(core, "draw")
    operation = _core.begin_canvas(core)
    read = _core.canvas_source_page(source_core, source.xref, source._generation, operation)
    assert read is not None
    snapshot = read[-1]

    def edit() -> None:
        document.objects[numbers["font"]]["BaseFont"] = Name("Courier")
        _change_content(document, source, numbers)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(edit).result(timeout=30)
    forms = _cache(target_document).forms
    form = _core.canvas_page_form(forms, core, source_core, source.xref, source._generation, operation, snapshot)
    assert form is not None
    _core.end_transaction(core, True)
    _core.end_transaction(core, True)
    placed = target_document.objects[form]
    assert b"Placed" in placed.read()
    assert placed["Resources"]["Font"]["F1"]["BaseFont"] == "Helvetica"
    assert not snapshot.held
    # The Form recorded the version it read: the edited source is new.
    (fresh,) = _place(target_document, source)
    assert fresh != form
    assert b"Other" in target_document.objects[fresh].read()
    assert target_document.objects[fresh]["Resources"]["Font"]["F1"]["BaseFont"] == "Courier"


def test_a_same_document_form_is_independent_of_later_source_edits() -> None:
    document, source, numbers = _source()
    (form,) = _place(document, source)
    _change_font(document, source, numbers)
    assert document.objects[form]["Resources"]["Font"]["F1"]["BaseFont"] == "Helvetica"


# The operation budget.


def test_a_placement_charges_its_operation() -> None:
    _, source, _ = _source()
    target_document = pyropdf.Document(limits=pyropdf.DocumentLimits(max_memory_size=6_000))
    target = target_document.pages.new((200, 100))
    count = len(target_document.objects)
    with pytest.raises(pyropdf.LimitError) as error, target.draw() as canvas:
        canvas.page((0, 0, 200, 100), source)
    assert error.value.code == "memory_size"
    assert len(target_document.objects) == count
    assert len(_cache(target_document).forms) == 0


def test_a_placement_observes_the_source_limits() -> None:
    document, _, _ = _source()
    limited = pyropdf.open(document.to_bytes(), limits=pyropdf.DocumentLimits(max_page_content_size=8))
    target_document = pyropdf.Document()
    target = target_document.pages.new((200, 100))
    with pytest.raises(pyropdf.LimitError) as error, target.draw() as canvas:
        canvas.page((0, 0, 200, 100), limited.pages[0])
    assert error.value.code == "page_content_size"


def test_a_cancelled_placement_writes_nothing() -> None:
    document, _, _ = _source()
    token = pyropdf.CancellationToken()
    source = pyropdf.open(document.to_bytes(), limits=pyropdf.DocumentLimits(cancellation=token))
    target_document = pyropdf.Document()
    target = target_document.pages.new((200, 100))
    page = source.pages[0]
    count = len(target_document.objects)
    token.cancel()
    with pytest.raises(pyropdf.LimitError) as error, target.draw() as canvas:
        canvas.page((0, 0, 200, 100), page)
    assert error.value.code == "cancelled"
    assert len(target_document.objects) == count


def test_a_refused_placement_keeps_the_rest_of_the_drawing() -> None:
    document, _, _ = _source()
    limited = pyropdf.open(document.to_bytes(), limits=pyropdf.DocumentLimits(max_page_content_size=8))
    target_document = pyropdf.Document()
    target = target_document.pages.new((200, 100))
    with target.draw() as canvas:
        canvas.rect((0, 0, 10, 10), fill=pyropdf.Color(1, 0, 0))
        with pytest.raises(pyropdf.LimitError):
            canvas.page((0, 0, 200, 100), limited.pages[0])
    assert len(target.content().paths) == 1


def test_the_cache_keeps_a_bounded_number_of_forms() -> None:
    document = pyropdf.Document()
    pages = [_source(document)[1] for _ in range(70)]
    target = document.pages.new((200, 100))
    with target.draw() as canvas:
        for page in pages:
            canvas.page((0, 0, 20, 10), page)
    drawn = _forms(target)
    assert len(set(drawn)) == 70
    assert len(_cache(document).forms) == 64
    # The newest page is still reused; the oldest was evicted.
    (newest,) = _place(document, pages[-1])
    assert newest in drawn
    (oldest,) = _place(document, pages[0])
    assert oldest not in drawn
