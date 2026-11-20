"""Optional content: listing groups and configurations, switching them, and what rendering and text then show."""

from __future__ import annotations

import io
from pathlib import Path

import pikepdf
import pytest

import pyropdf
from pyropdf import Layer, LayerConfig, Name, _layers, typed

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


@pytest.mark.spec("optional-content", "read")
def test_layers_list_names_and_states() -> None:
    doc, (first, second, third) = layer_document()
    assert list(doc.layers) == [
        Layer(xref=first, name="Alpha", intent=("View",), on=True, locked=False),
        Layer(xref=second, name="Boxes", intent=("View",), on=True, locked=False),
        Layer(xref=third, name="Unused", intent=("View",), on=True, locked=True),
    ]
    assert len(doc.layers) == 3
    assert doc.layers[1].name == "Boxes"
    assert [layer.name for layer in doc.layers[1:]] == ["Boxes", "Unused"]


def test_a_corpus_document_lists_its_layers_and_follows_a_switch() -> None:
    doc = pyropdf.open(CORPUS / "pdfium-ocg-invisible.pdf")
    assert list(doc.layers) == [Layer(xref=8, name="Show Text", intent=("View",), on=False, locked=False)]
    assert doc.layers.configs == [LayerConfig(index=0, name="", creator="", base_state="OFF", on=(), off=(), locked=())]
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


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.parametrize("draw", [False, True], ids=["group", "drawing"])
def test_adding_a_layer_to_a_pdf_13_document(*, draw: bool, incremental: bool) -> None:
    doc = pyropdf.open(CORPUS / "patent-us223898.pdf")
    layer = doc.layers.add("Overlay")
    assert doc.history.steps == ("add layer",)
    if draw:
        with doc.pages[0].draw() as canvas, canvas.layer(layer):
            canvas.rect((10, 10, 20, 20), fill=pyropdf.Color(1, 0, 0))

    saved = pyropdf.open(doc.to_bytes(incremental=incremental))
    assert saved.catalog["Version"] == Name("1.5")
    assert [group.xref for group in saved.layers] == [layer.xref]
    assert not saved.check()
    if draw:
        assert saved.pages[0].obj["Resources"]["Properties"]["PyroL1"].xref == layer.xref
        doc.history.undo()
    doc.history.undo()
    assert "OCProperties" not in doc.catalog
    assert "Version" not in doc.catalog
    assert not doc.history.can_undo


def test_adding_a_layer_keeps_a_sufficient_header_or_catalog_version() -> None:
    recent = pyropdf.Document()
    recent.layers.add("Overlay")
    assert "Version" not in recent.catalog

    older = pyropdf.open(CORPUS / "patent-us223898.pdf")
    older.catalog["Version"] = Name("1.7")
    older.layers.add("Overlay")
    assert older.catalog["Version"] == Name("1.7")


def test_configurations_list_in_order() -> None:
    doc, (first, _, third) = layer_document()
    assert doc.layers.configs == [
        LayerConfig(index=0, name="Default", creator="", base_state="ON", on=(), off=(), locked=(third,)),
        LayerConfig(index=1, name="Print", creator="tests", base_state="ON", on=(), off=(first,), locked=()),
        LayerConfig(index=2, name="Nothing", creator="", base_state="OFF", on=(third,), off=(), locked=()),
    ]


def test_everything_shows_by_default() -> None:
    doc, _ = layer_document()
    assert _text(doc).split() == ["Alpha", "Always", "Either"]
    assert _blue(doc)


@pytest.mark.spec("optional-content", "interpret", "edit")
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


def _usage_document() -> bytes:
    """Independent groups with identical names, shared application entries and two alternate configurations."""
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page()
    first, second = [pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.OCG, Name="Overlay")) for _ in range(2)]
    first.Usage = pdf.make_indirect(
        pikepdf.Dictionary(
            Print=pikepdf.Dictionary(PrintState=pikepdf.Name.OFF, Subtype=pikepdf.Name.Watermark),
            Zoom=pikepdf.Dictionary(min=2),
            XXNote="usage",
        )
    )
    second.Usage = pikepdf.Dictionary(View=pikepdf.Dictionary(ViewState=pikepdf.Name.ON))
    view = pikepdf.Dictionary(Event=pikepdf.Name.View, OCGs=[second], Category=[pikepdf.Name.View])
    first_print = pdf.make_indirect(
        pikepdf.Dictionary(Event=pikepdf.Name.Print, OCGs=[first], Category=[pikepdf.Name.Print], XXNote="application")
    )
    zoom = pikepdf.Dictionary(Event=pikepdf.Name.Print, OCGs=[first, second], Category=[pikepdf.Name.Zoom])
    missing = pikepdf.Dictionary(Event=pikepdf.Name.Print, Category=[pikepdf.Name.Language])
    empty = pikepdf.Dictionary(Event=pikepdf.Name.Print, OCGs=[], Category=[pikepdf.Name.Export])
    pdf.Root.OCProperties = pikepdf.Dictionary(
        OCGs=[first, second],
        D=pikepdf.Dictionary(
            Name="Default", AS=pdf.make_indirect(pikepdf.Array([view, first_print, zoom, missing, empty]))
        ),
        Configs=[
            pikepdf.Dictionary(Name="Alternate", AS=[view]),
            pikepdf.Dictionary(Name="Manual"),
        ],
    )
    out = io.BytesIO()
    pdf.save(out, min_version="1.7")
    pdf.close()
    return out.getvalue()


@pytest.mark.spec("optional-content", "read")
def test_usage_applications_resolve_group_event_and_configuration() -> None:
    doc = pyropdf.open(_usage_document())
    first, second = doc.layers
    assert first.name == second.name
    assert first.xref != second.xref
    printed, zoom = doc.layers.usage_applications(first, event="Print")
    assert (printed.Category[0], zoom.Category[0]) == ("Print", "Zoom")
    group = typed.OptContentGroup(doc.objects[first.xref])
    assert group.Usage is not None
    assert group.Usage.Print is not None
    assert group.Usage.Zoom is not None
    assert group.Usage.Print.PrintState == "OFF"
    assert group.Usage.Zoom.min == 2
    assert doc.layers.usage_applications(second, event="Print") == (zoom,)
    (view,) = doc.layers.usage_applications(second, event="View", config=1)
    assert view.Category[0] == "View"
    assert doc.layers.usage_applications(first, event="View") == ()
    assert doc.layers.usage_applications(first, event="Print", config=1) == ()
    assert doc.layers.usage_applications(second, event="View", config=2) == ()
    assert doc.layers.usage_applications(first, event="Export") == ()
    assert not doc.history.can_undo


@pytest.mark.spec("optional-content", "create")
@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_create_group_usage_and_export_application(*, incremental: bool) -> None:
    base, _ = layer_document()
    doc = pyropdf.open(base.to_bytes())
    first, second, _ = doc.layers
    group = typed.OptContentGroup(doc.objects[first.xref])
    config = typed.OptContentConfig(doc.catalog["OCProperties"]["Configs"][0])
    assert group.Usage is None
    assert config.AS is None
    with doc.transaction():
        group.Usage = typed.OptContentUsage(doc.objects[doc.objects.add({"Export": {"ExportState": Name("OFF")}})])
        config.AS = typed.ArrayOfOCUsage(
            doc.objects[doc.objects.add([{"Event": Name("Export"), "OCGs": [group.obj], "Category": [Name("Export")]}])]
        )
    saved = doc.to_bytes(incremental=incremental)
    reopened = pyropdf.open(saved)
    (application,) = reopened.layers.usage_applications(reopened.layers[0], event="Export", config=1)
    assert application.Category[0] == "Export"
    assert reopened.layers.usage_applications(reopened.layers[1], event="Export", config=1) == ()
    assert reopened.layers.usage_applications(reopened.layers[0], event="Export") == ()
    with pikepdf.open(io.BytesIO(saved)) as pdf:
        assert not pdf.check_pdf_syntax()
        groups = pdf.Root.OCProperties.OCGs
        alternate = pdf.Root.OCProperties.Configs[0]
        assert groups[0].Usage.Export.ExportState == pikepdf.Name.OFF
        assert alternate.AS[0].Event == pikepdf.Name.Export
        assert alternate.AS[0].OCGs[0].objgen == groups[0].objgen
        assert alternate.OFF[0].objgen == groups[0].objgen
        assert groups[1].objgen[0] == second.xref
        assert "/AS" not in pdf.Root.OCProperties.D


@pytest.mark.spec("optional-content", "edit")
@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_edit_usage_and_retarget_an_indirect_application(*, incremental: bool) -> None:
    doc = pyropdf.open(_usage_document())
    first, second = doc.layers
    application, zoom = doc.layers.usage_applications(first, event="Print")
    group = typed.OptContentGroup(doc.objects[first.xref])
    assert group.Usage is not None
    assert group.Usage.Print is not None
    assert application.OCGs is not None
    with doc.transaction():
        group.Usage.Print.PrintState = "ON"
        application.Event = "Export"
        application.OCGs[0] = doc.objects[second.xref]
    assert doc.layers.usage_applications(first, event="Print") == (zoom,)
    saved = doc.to_bytes(incremental=incremental)
    reopened = pyropdf.open(saved)
    (target,) = reopened.layers.usage_applications(reopened.layers[1], event="Export")
    assert target.obj.xref == application.obj.xref
    assert target.Category[0] == "Print"
    assert reopened.layers.usage_applications(reopened.layers[0], event="Export") == ()
    with pikepdf.open(io.BytesIO(saved)) as pdf:
        assert not pdf.check_pdf_syntax()
        properties = pdf.Root.OCProperties
        first_raw, second_raw = properties.OCGs
        assert first_raw.Usage.Print.PrintState == pikepdf.Name.ON
        assert first_raw.Usage.Print.Subtype == pikepdf.Name.Watermark
        assert first_raw.Usage.Zoom.min == 2
        assert first_raw.Usage.XXNote == "usage"
        assert second_raw.Usage.View.ViewState == pikepdf.Name.ON
        changed = properties.D.AS[1]
        assert (changed.Event, list(changed.Category), str(changed.XXNote)) == (
            pikepdf.Name.Export,
            [pikepdf.Name.Print],
            "application",
        )
        assert changed.OCGs[0].objgen == second_raw.objgen
        assert properties.Configs[0].AS[0].Event == pikepdf.Name.View
        assert "/OCGs" not in properties.D.AS[3]
        assert len(properties.D.AS[4].OCGs) == 0


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_using_a_configuration_replaces_or_clears_its_usage_applications(*, incremental: bool) -> None:
    doc = pyropdf.open(_usage_document())
    first, second = doc.layers
    doc.layers.use(1)
    assert doc.layers.usage_applications(first, event="Print") == ()
    (view,) = doc.layers.usage_applications(second, event="View")
    assert view.Category[0] == "View"
    with pikepdf.open(io.BytesIO(doc.to_bytes(incremental=incremental))) as pdf:
        assert [entry.Event for entry in pdf.Root.OCProperties.D.AS] == [pikepdf.Name.View]
        assert pdf.Root.OCProperties.D.Name == "Default"
    doc.layers.use(2)
    with pikepdf.open(io.BytesIO(doc.to_bytes(incremental=incremental))) as pdf:
        assert "/AS" not in pdf.Root.OCProperties.D
        assert pdf.Root.OCProperties.Configs[0].AS[0].Event == pikepdf.Name.View
    doc.history.undo()
    assert doc.layers.usage_applications(second, event="View") == (view,)
    doc.layers.use(0)
    assert doc.history.can_redo


def test_usage_follows_a_renamed_group_and_rejects_an_object_that_stopped_being_an_ocg() -> None:
    doc = pyropdf.open(_usage_document())
    first = doc.layers[0]
    original = doc.layers.usage_applications(first, event="Print")
    group = doc.objects[first.xref]
    group["Name"] = "Renamed overlay"
    assert doc.layers.usage_applications(first, event="Print") == original
    group["Type"] = Name("OCMD")
    with pytest.raises(ValueError, match="optional content group"):
        doc.layers.usage_applications(first, event="Print")


def test_usage_refuses_invalid_targets_and_malformed_settings() -> None:
    doc = pyropdf.open(_usage_document())
    first, _ = doc.layers
    foreign = pyropdf.open(_usage_document())
    with pytest.raises(ValueError, match="of this document"):
        doc.layers.usage_applications(foreign.layers[0], event="Print")
    with pytest.raises(ValueError, match="Print"):
        doc.layers.usage_applications(first, event="Preview")  # ty: ignore[invalid-argument-type]
    properties = doc.catalog["OCProperties"]
    properties["Configs"][0] = 1
    assert [config.index for config in doc.layers.configs] == [0, 2]
    with pytest.raises(IndexError, match="configuration 1"):
        doc.layers.usage_applications(first, event="View", config=1)
    assert doc.layers.usage_applications(first, event="View", config=2) == ()
    application = doc.layers.usage_applications(first, event="Print")[0]
    with pytest.raises(ValueError, match="Event"):
        application.Event = "Preview"
    application.obj["OCGs"] = 2
    with pytest.raises(pyropdf.PdfError, match="OCGs"):
        doc.layers.usage_applications(first, event="Print")
    properties["Configs"][1]["AS"] = "broken"
    before = doc.to_bytes()
    with pytest.raises(pyropdf.PdfError, match="AS"):
        doc.layers.use(2)
    assert doc.to_bytes() == before


def test_usage_lookup_limits_group_entries_across_applications(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = pyropdf.open(_usage_document())
    first = doc.layers[0]
    monkeypatch.setattr(_layers, "_MAX_LAYERS", 7)
    with pytest.raises(pyropdf.LimitError) as raised:
        doc.layers.usage_applications(first, event="Print")
    assert raised.value.code == "optional_content_count"
    assert len(doc.layers.usage_applications(doc.layers[1], event="View")) == 1


def test_matching_usage_views_share_the_document_read_budget() -> None:
    source = pyropdf.open(_usage_document())
    config = source.catalog["OCProperties"]["D"]
    config["AS"] = [config["AS"][1]] * 80
    data = source.to_bytes()
    limited = pyropdf.open(data, limits=pyropdf.DocumentLimits(max_memory_size=32_768))
    group = limited.layers[0]
    assert limited.layers.usage_applications(group, event="View") == ()
    with pytest.raises(pyropdf.LimitError) as error:
        limited.layers.usage_applications(group, event="Print")
    assert error.value.code == "memory_size"
    assert not limited.history.can_undo
    assert limited.layers.usage_applications(group, event="View") == ()
    regular = pyropdf.open(data)
    assert len(regular.layers.usage_applications(regular.layers[0], event="Print")) == 80
