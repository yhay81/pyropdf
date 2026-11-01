"""``Canvas.layer``: drawing optional content on a page (DESIGN.md section 7).

Ported from pylopdf's tests/test_canvas_layer.py at ebb01d3c. The intended
differences:

- pyropdf's :class:`Layer` is a value, so ``doc.layers.switch(xref, on=...)``
  shows and hides a group where pylopdf assigns ``layer.is_visible``;
- the canvas writes its drawing into the page's content, so ``/Properties``
  joins the page's resources and the names are ``PyroL1`` and on;
- pyropdf has no ``create()`` builder, so the refusal of layers on a new
  document's canvas is not ported;
- a renumbering save leaves the numbers in memory as they are, so it cannot
  make a scope's group stale; the commit refuses a group that stopped being
  one instead.
"""

from __future__ import annotations

import copy
import pickle
from typing import TYPE_CHECKING

import pytest

import pyropdf
from pyropdf import Color, Dictionary, LayerMembership, Name, StaleObjectError, TagRole, VisibilityPolicy

if TYPE_CHECKING:
    from collections.abc import Iterator

RED = Color(1, 0, 0)


def _pixel(page: pyropdf.Page, x: float, y: float) -> tuple[int, int, int]:
    pixmap = page.pixmap(dpi=72)
    samples = memoryview(pixmap).cast("B")
    offset = int(y) * pixmap.stride + int(x) * pixmap.channels
    return tuple(samples[offset : offset + 3])  # ty: ignore[invalid-return-type]


def _drawn(*, visible: bool) -> tuple[pyropdf.Document, pyropdf.Layer]:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    layer = doc.layers.add("Overlay", visible=visible)
    with page.draw() as canvas:
        canvas.text("Base", (20, 40))
        with canvas.layer(layer):
            canvas.rect((100, 100, 150, 150), fill=RED)
            canvas.text("Layered", (20, 80))
    return doc, layer


def test_layers_add_creates_groups_in_the_default_configuration() -> None:
    doc = pyropdf.Document()
    shown = doc.layers.add("Shown")
    hidden = doc.layers.add("Hidden", visible=False)

    assert list(doc.layers) == [shown, hidden]
    assert (shown.name, shown.on, hidden.on) == ("Shown", True, False)
    config = doc.catalog["OCProperties"]["D"]
    assert [group.xref for group in config["Order"]] == [shown.xref, hidden.xref]
    assert [group.xref for group in config["OFF"]] == [hidden.xref]
    assert doc.history.steps[-2:] == ("add layer", "add layer")

    with pytest.raises(TypeError):
        doc.layers.add(1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        doc.layers.add("x", visible=1)  # ty: ignore[invalid-argument-type]


def test_layers_add_follows_a_base_state_of_off() -> None:
    doc = pyropdf.Document()
    doc.layers.add("First", visible=False)
    doc.catalog["OCProperties"]["D"]["BaseState"] = Name("OFF")
    doc.catalog["OCProperties"]["D"]["OFF"] = []
    added = doc.layers.add("Shown")
    assert [layer.on for layer in doc.layers] == [False, True]
    assert [group.xref for group in doc.catalog["OCProperties"]["D"]["ON"]] == [added.xref]


def test_a_layer_scope_hides_and_shows_what_it_draws() -> None:
    doc, layer = _drawn(visible=False)
    page = doc.pages[0]

    assert page.text().split() == ["Base"]
    assert _pixel(page, 125, 125) == (255, 255, 255)

    doc.layers.switch(layer.xref, on=True)
    assert page.text().split() == ["Base", "Layered"]
    assert _pixel(page, 125, 125) == (255, 0, 0)

    reopened = pyropdf.open(doc.to_bytes())
    assert reopened.pages[0].text().split() == ["Base", "Layered"]
    reopened.layers.switch(reopened.layers[0].xref, on=False)
    assert reopened.pages[0].text().split() == ["Base"]


def test_the_scope_names_the_group_in_the_page_resources() -> None:
    doc, layer = _drawn(visible=True)
    page = doc.pages[0]

    properties = page.obj["Resources"]["Properties"]
    assert [(name, value.xref) for name, value in properties.items()] == [("PyroL1", layer.xref)]
    assert b"/OC /PyroL1 BDC" in page.obj["Contents"][-1].read()


def test_a_colliding_property_name_is_renamed() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    first = doc.layers.add("First")
    second = doc.layers.add("Second", visible=False)
    with page.draw() as canvas, canvas.layer(first):
        canvas.text("One", (20, 40))
    with page.draw() as canvas, canvas.layer(second):
        canvas.text("Two", (20, 80))

    properties = page.obj["Resources"]["Properties"]
    assert [(name, value.xref) for name, value in properties.items()] == [
        ("PyroL1", first.xref),
        ("PyroL1_1", second.xref),
    ]
    assert page.text().split() == ["One"]
    doc.layers.switch(second.xref, on=True)
    assert page.text().split() == ["One", "Two"]


def test_membership_scopes_write_a_membership_dictionary() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    notes = doc.layers.add("Notes")
    draft = doc.layers.add("Draft", visible=False)
    with page.draw() as canvas:
        with canvas.layer(LayerMembership((notes, draft), policy=VisibilityPolicy.ALL_ON)):
            canvas.text("Both", (20, 40))
        with canvas.layer(LayerMembership((notes, draft), policy=VisibilityPolicy.ANY_ON)):
            canvas.text("Either", (20, 80))

    assert page.text().split() == ["Either"]
    membership = page.obj["Resources"]["Properties"]["PyroL1"]
    assert (membership["Type"], membership["P"]) == ("OCMD", "AllOn")
    assert not doc.layers.visible(membership)
    doc.layers.switch(draft.xref, on=True)
    assert page.text().split() == ["Both", "Either"]
    assert doc.layers.visible(membership)


def test_memberships_validate_their_fields() -> None:
    doc = pyropdf.Document()
    layer = doc.layers.add("Notes")
    assert LayerMembership([layer]).layers == (layer,)  # ty: ignore[invalid-argument-type]
    assert LayerMembership((layer,), policy="AllOff").policy is VisibilityPolicy.ALL_OFF  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        LayerMembership(layer)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        LayerMembership(("Notes",))  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="VisibilityPolicy"):
        LayerMembership((layer,), policy="Sometimes")  # ty: ignore[invalid-argument-type]


def test_membership_stops_reading_at_its_limit() -> None:
    doc = pyropdf.Document()
    layer = doc.layers.add("Notes")
    read = 0

    def layers() -> Iterator[pyropdf.Layer]:
        nonlocal read
        while True:
            read += 1
            yield layer

    with pytest.raises(pyropdf.LimitError, match="4096"):
        LayerMembership(layers())  # ty: ignore[invalid-argument-type]
    assert read == 4097


@pytest.mark.parametrize("array_name", ["OCGs", "Order", "OFF"])
def test_adding_a_layer_refuses_an_array_at_its_limit(array_name: str) -> None:
    doc = pyropdf.Document()
    first = doc.layers.add("First")
    properties = doc.catalog["OCProperties"]
    target = properties if array_name == "OCGs" else properties["D"]
    target[array_name] = [doc.objects[first.xref]] * 4096
    steps = doc.history.steps

    with pytest.raises(pyropdf.LimitError, match="4096"):
        doc.layers.add("Extra", visible=False)
    assert len(doc.catalog["OCProperties"]["OCGs"]) == (4096 if array_name == "OCGs" else 1)
    assert len(target[array_name]) == 4096
    assert doc.history.steps == steps


def test_layers_nest_with_other_scopes() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    outer = doc.layers.add("Outer")
    inner = doc.layers.add("Inner")
    with page.draw() as canvas:
        with canvas.layer(outer), canvas.opacity(0.5), canvas.layer(inner):
            canvas.text("Nested", (20, 40))
        with canvas.transform(pyropdf.Matrix(1, 0, 0, 1, 0, 50)), canvas.layer(inner):
            canvas.text("Moved", (20, 40))

    assert page.text().split() == ["Nested", "Moved"]
    doc.layers.switch(inner.xref, on=False)
    assert page.text() == ""
    doc.layers.switch(inner.xref, on=True)
    doc.layers.switch(outer.xref, on=False)
    assert page.text().split() == ["Moved"]


def test_layers_of_other_documents_and_other_values_are_refused() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    other = pyropdf.Document()
    foreign = other.layers.add("Foreign")

    with page.draw() as canvas:
        with pytest.raises(ValueError, match="this document"):
            canvas.layer(foreign)
        with pytest.raises(ValueError, match="this document"):
            canvas.layer(LayerMembership((foreign,)))
        with pytest.raises(TypeError):
            canvas.layer(None)  # ty: ignore[invalid-argument-type]
        with pytest.raises(TypeError):
            canvas.layer("Foreign")  # ty: ignore[invalid-argument-type]
    assert "Contents" not in page.obj


def test_a_same_named_layer_from_another_document_is_refused() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    local = doc.layers.add("Notes")
    other = pyropdf.Document()
    other.pages.new((200, 200))
    foreign = other.layers.add("Notes")
    assert local.xref == foreign.xref

    with page.draw() as canvas:
        for layer in (foreign, other.layers[0]):
            with pytest.raises(ValueError, match="this document"):
                canvas.layer(layer)
            with pytest.raises(ValueError, match="this document"):
                canvas.layer(LayerMembership((layer,)))
        with canvas.layer(doc.layers[0]):
            canvas.text("Local", (10, 20))
    assert page.text().strip() == "Local"


def test_layer_copy_and_pickle_keep_value_behavior() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    layer = doc.layers.add("Notes")
    restored = pickle.loads(pickle.dumps(layer))  # noqa: S301 - round trip of a value made in this test.
    assert restored == layer
    assert repr(restored) == repr(layer)

    with page.draw() as canvas:
        for saved in (restored, pyropdf.Layer(layer.xref, layer.name, layer.intent, layer.on, layer.locked)):
            with pytest.raises(ValueError, match="this document"):
                canvas.layer(saved)
        for duplicate in (copy.copy(layer), copy.deepcopy(layer)):
            with canvas.layer(duplicate):
                canvas.text("Copy", (10, 20))
    assert page.text().split() == ["Copy", "Copy"]


def test_a_renumbering_save_keeps_the_drawing() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    layer = doc.layers.add("Kept", visible=False)
    with page.draw() as canvas:
        with canvas.layer(layer):
            canvas.text("Kept", (20, 40))
        doc.to_bytes(renumber=True)

    assert page.text() == ""
    doc.layers.switch(layer.xref, on=True)
    assert page.text().split() == ["Kept"]


def test_a_group_that_stops_being_one_before_the_commit_refuses_it() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    layer = doc.layers.add("Moved")
    steps = doc.history.steps

    canvas = page.draw().__enter__()
    with canvas.layer(layer):
        canvas.rect((10, 10, 20, 20), fill=RED)
    doc.objects[layer.xref] = {"Type": Name("Font")}
    with pytest.raises(StaleObjectError):
        canvas.__exit__(None, None, None)
    assert isinstance(doc.objects[layer.xref], Dictionary)
    assert doc.objects[layer.xref]["Type"] == "OCG"
    assert "Contents" not in page.obj
    assert doc.history.steps == steps


def test_a_drawing_with_layers_and_tags_is_one_undo_step() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    layer = doc.layers.add("Notes")
    steps = doc.history.steps
    with page.draw() as canvas, canvas.tag(TagRole.PARAGRAPH), canvas.layer(LayerMembership((layer,))):
        canvas.text("Layered", (20, 40))

    assert doc.history.steps == (*steps, "draw")
    assert "StructTreeRoot" in doc.catalog
    doc.history.undo()
    assert "StructTreeRoot" not in doc.catalog
    assert "Contents" not in doc.pages[0].obj


def test_layers_and_tags_nest() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    layer = doc.layers.add("Notes")
    with page.draw() as canvas, canvas.tag(TagRole.PARAGRAPH), canvas.layer(layer):
        canvas.text("Layered", (20, 40))

    root = doc.catalog["StructTreeRoot"]
    paragraph = root["K"][0]
    form = paragraph["K"][0]["Stm"]
    content = form.read()
    assert content.index(b"/OC /PyroL1 BDC") < content.index(b"/P <</MCID 0>> BDC")
    assert form["Resources"]["Properties"]["PyroL1"].xref == layer.xref
    doc.layers.switch(layer.xref, on=False)
    assert page.text() == ""
