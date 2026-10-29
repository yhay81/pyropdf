"""Optional content: listing groups and configurations, switching them, and what rendering and text then show."""

from __future__ import annotations

from pathlib import Path

import pytest

import pyropdf
from pyropdf import Layer, LayerConfig, Name

CORPUS = Path(__file__).resolve().parent / "assets" / "real_world"
WIDTH, HEIGHT = 200, 100
#: Content marked with group L1 (marked content), L2 (an XObject's /OC), and membership dictionary M.
CONTENT = (
    b"/OC /L1 BDC BT /F1 12 Tf 10 70 Td (Alpha) Tj ET EMC "
    b"BT /F1 12 Tf 10 40 Td (Always) Tj ET "
    b"q 50 0 0 20 100 10 cm /Box Do Q "
    b"/OC /M BDC BT /F1 12 Tf 10 10 Td (Either) Tj ET EMC"
)


def layer_document(*, base_state: str = "ON", off: tuple[int, ...] = ()) -> tuple[pyropdf.Document, list[int]]:
    """Return a page with three groups, ``Alpha``, ``Boxes``, and ``Unused``, and the groups' object numbers."""
    doc = pyropdf.Document()
    groups = [doc.objects.add({"Type": Name("OCG"), "Name": name}) for name in ("Alpha", "Boxes", "Unused")]
    first, second, third = groups
    membership = doc.objects.add(
        {"Type": Name("OCMD"), "OCGs": [doc.objects[first], doc.objects[second]], "P": Name("AnyOn")}
    )
    box = doc.objects.add_stream(
        b"0 0 1 rg 0 0 1 1 re f",
        {"Type": Name("XObject"), "Subtype": Name("Form"), "BBox": [0, 0, 1, 1], "OC": doc.objects[second]},
    )
    font = doc.objects.add({"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica")})
    doc.pages.new((WIDTH, HEIGHT))
    page = doc.pages[0].obj
    page["Resources"] = {
        "Font": {"F1": doc.objects[font]},
        "XObject": {"Box": doc.objects[box]},
        "Properties": {"L1": doc.objects[first], "M": doc.objects[membership]},
    }
    page["Contents"] = doc.objects[doc.objects.add_stream(CONTENT)]
    doc.catalog["OCProperties"] = {
        "OCGs": [doc.objects[number] for number in groups],
        "D": {
            "Name": "Default",
            "BaseState": Name(base_state),
            "OFF": [doc.objects[number] for number in off],
            "Locked": [doc.objects[third]],
        },
        "Configs": [
            {"Name": "Print", "Creator": "tests", "OFF": [doc.objects[first]]},
            {"Name": "Nothing", "BaseState": Name("OFF"), "ON": [doc.objects[third]]},
        ],
    }
    return doc, groups


def _text(doc: pyropdf.Document) -> str:
    return doc.pages[0].text()


def _blue(doc: pyropdf.Document) -> bool:
    """Return whether the box XObject shows: a blue pixel inside its rectangle."""
    pixmap = doc.pages[0].pixmap(dpi=72)
    data = bytes(pixmap)
    x, y = 125, HEIGHT - 20
    at = (y * pixmap.width + x) * 3
    return data[at : at + 3] == b"\x00\x00\xff"


def test_layers_list_names_and_states() -> None:
    doc, (first, second, third) = layer_document()
    assert list(doc.layers) == [
        Layer(first, "Alpha", ("View",), on=True, locked=False),
        Layer(second, "Boxes", ("View",), on=True, locked=False),
        Layer(third, "Unused", ("View",), on=True, locked=True),
    ]
    assert len(doc.layers) == 3
    assert doc.layers[1].name == "Boxes"
    assert [layer.name for layer in doc.layers[1:]] == ["Boxes", "Unused"]


def test_a_corpus_document_lists_its_layers_and_follows_a_switch() -> None:
    doc = pyropdf.open(CORPUS / "pdfium-ocg-invisible.pdf")
    assert list(doc.layers) == [Layer(8, "Show Text", ("View",), on=False, locked=False)]
    assert doc.layers.configs == [LayerConfig(0, "", "", "OFF", (), (), ())]
    hidden = bytes(doc.pages[0].pixmap(dpi=72))
    doc.layers.switch(8, on=True)
    assert doc.layers[0].on
    shown = bytes(doc.pages[0].pixmap(dpi=72))
    assert shown != hidden
    doc.layers.switch(8, on=False)
    assert bytes(doc.pages[0].pixmap(dpi=72)) == hidden


def test_a_document_without_optional_content_has_no_layers() -> None:
    doc = pyropdf.Document()
    doc.pages.new()
    assert list(doc.layers) == []
    assert doc.layers.configs == []
    with pytest.raises(IndexError, match="has 0"):
        doc.layers.use(0)


def test_configurations_list_in_order() -> None:
    doc, (first, _, third) = layer_document()
    assert doc.layers.configs == [
        LayerConfig(0, "Default", "", "ON", (), (), (third,)),
        LayerConfig(1, "Print", "tests", "ON", (), (first,), ()),
        LayerConfig(2, "Nothing", "", "OFF", (third,), (), ()),
    ]


def test_everything_shows_by_default() -> None:
    doc, _ = layer_document()
    assert _text(doc).split() == ["Alpha", "Always", "Either"]
    assert _blue(doc)


def test_switching_a_group_off_removes_its_marked_content_from_rendering_and_text() -> None:
    doc, (first, _, _) = layer_document()
    before = bytes(doc.pages[0].pixmap(dpi=72))
    steps = doc.history.position
    doc.layers.switch(first, on=False)
    assert doc.history.position == steps + 1
    assert doc.history.steps[-1] == "set layer"
    assert not doc.layers[0].on
    assert _text(doc).split() == ["Always", "Either"]
    assert bytes(doc.pages[0].pixmap(dpi=72)) != before
    doc.layers.switch(first, on=True)
    assert _text(doc).split() == ["Alpha", "Always", "Either"]
    doc.layers.switch(first, on=False)
    doc.history.undo()
    assert doc.layers[0].on


def test_switching_a_group_off_hides_its_xobject() -> None:
    doc, (_, second, _) = layer_document()
    doc.layers.switch(second, on=False)
    assert not _blue(doc)
    assert _text(doc).split() == ["Alpha", "Always", "Either"]  # The membership dictionary needs any one on.


def test_a_membership_dictionary_follows_its_policy() -> None:
    doc, (first, second, _) = layer_document()
    doc.layers.switch(first, on=False)
    doc.layers.switch(second, on=False)
    assert _text(doc).split() == ["Always"]


def test_switching_survives_saving() -> None:
    doc, (first, _, _) = layer_document()
    doc.layers.switch(first, on=False)
    reopened = pyropdf.open(doc.to_bytes())
    assert [layer.on for layer in reopened.layers] == [False, True, True]
    assert _text(reopened).split() == ["Always", "Either"]


def test_a_base_state_of_off_hides_unlisted_groups() -> None:
    doc, (first, second, third) = layer_document(base_state="OFF")
    assert [layer.on for layer in doc.layers] == [False, False, False]
    assert _text(doc).split() == ["Always"]
    doc.layers.switch(first, on=True)
    assert [layer.on for layer in doc.layers] == [True, False, False]
    assert doc.layers.configs[0].on == (first,)
    assert _text(doc).split() == ["Alpha", "Always", "Either"]
    assert (second, third) == (doc.layers[1].xref, doc.layers[2].xref)


def test_using_a_configuration_applies_it() -> None:
    doc, (first, _, third) = layer_document()
    doc.layers.use(1)
    assert doc.history.steps[-1] == "use layer configuration"
    assert [layer.on for layer in doc.layers] == [False, True, True]
    assert _text(doc).split() == ["Always", "Either"]
    assert doc.layers.configs[0].name == "Default"  # The default keeps its name.
    doc.layers.use(2)
    assert [layer.on for layer in doc.layers] == [False, False, True]
    assert doc.layers.configs[0].on == (third,)
    assert not _blue(doc)
    assert _text(doc).split() == ["Always"]
    assert first not in doc.layers.configs[0].off


def test_visibility_follows_groups_and_policies() -> None:
    doc, (first, second, _) = layer_document()
    layers = doc.layers
    membership = doc.pages[0].obj["Resources"]["Properties"]["M"]
    assert layers.visible(None)
    assert layers.visible(doc.objects[first])
    assert layers.visible(membership)
    layers.switch(first, on=False)
    assert not layers.visible(doc.objects[first])
    assert layers.visible(membership)
    for policy, expected in (("AllOn", False), ("AnyOn", True), ("AnyOff", True), ("AllOff", False)):
        membership["P"] = Name(policy)
        assert layers.visible(membership) is expected, policy
    layers.switch(second, on=False)
    membership["P"] = Name("AllOff")
    assert layers.visible(membership)


def _expression(doc: pyropdf.Document, expression: object) -> pyropdf.Dictionary:
    return doc.objects[doc.objects.add({"Type": Name("OCMD"), "VE": expression})]


def test_a_visibility_expression_overrides_the_groups() -> None:
    doc, (first, second, third) = layer_document()
    one, two, three = (doc.objects[number] for number in (first, second, third))
    doc.layers.switch(first, on=False)
    assert not doc.layers.visible(_expression(doc, [Name("And"), one, two]))
    assert doc.layers.visible(_expression(doc, [Name("Or"), one, two]))
    assert doc.layers.visible(_expression(doc, [Name("Not"), one]))
    nested = [Name("And"), [Name("Not"), one], [Name("Or"), two, [Name("And"), three, one]]]
    assert doc.layers.visible(_expression(doc, nested))
    expression = _expression(doc, [Name("And"), two])
    expression["OCGs"] = one
    expression["P"] = Name("AllOn")
    assert doc.layers.visible(expression)


def test_a_deep_visibility_expression_is_bounded() -> None:
    doc, (first, _, _) = layer_document()
    # Each level is an indirect array, which a written value's nesting limit does not count.
    expression = doc.objects[doc.objects.add([Name("Not"), doc.objects[first]])]
    for _ in range(31):
        expression = doc.objects[doc.objects.add([Name("Not"), expression])]
    assert doc.layers.visible(_expression(doc, expression))  # 32 negations of a shown group
    with pytest.raises(pyropdf.LimitError) as caught:
        doc.layers.visible(_expression(doc, [Name("Not"), expression]))
    assert caught.value.code == "optional_content_expression"


def test_a_cyclic_visibility_expression_is_bounded() -> None:
    doc, (first, _, _) = layer_document()
    number = doc.objects.add([Name("Or"), doc.objects[first]])
    cycle = doc.objects[number]
    cycle.append(cycle)
    with pytest.raises(pyropdf.LimitError) as caught:
        doc.layers.visible(_expression(doc, cycle))
    assert caught.value.code == "optional_content_expression"


def test_a_wide_visibility_expression_is_bounded() -> None:
    doc, (first, _, _) = layer_document()
    wide = [Name("Or"), *[doc.objects[first]] * 4097]
    with pytest.raises(pyropdf.LimitError) as caught:
        doc.layers.visible(_expression(doc, wide))
    assert caught.value.code == "optional_content_expression"


def test_switching_refuses_what_is_not_a_group() -> None:
    doc, _ = layer_document()
    page = doc.pages[0].obj.xref
    assert page is not None
    before = doc.to_bytes()
    with pytest.raises(ValueError, match="not an optional content group"):
        doc.layers.switch(page, on=False)
    with pytest.raises(TypeError, match="on"):
        doc.layers.switch(page, on=0)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="xref"):
        doc.layers.switch("1", on=False)  # ty: ignore[invalid-argument-type]
    with pytest.raises(IndexError, match="configuration 3"):
        doc.layers.use(3)
    assert doc.to_bytes() == before
