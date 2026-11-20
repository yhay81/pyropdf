"""Interactive actions, document scripts, and additional-action triggers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import pyropdf
from pyropdf import (
    ActionTriggers,
    AnnotationTrigger,
    Destination,
    DocumentScripts,
    DocumentTrigger,
    DocumentTriggers,
    EmbeddedDestination,
    EmbeddedTarget,
    HideAction,
    ImportDataAction,
    JavaScriptAction,
    Launch,
    LayerStateChange,
    LayerStateTransition,
    Name,
    NamedAction,
    NavigationStep,
    Note,
    OutlineEntry,
    PageTrigger,
    PageTriggers,
    Point,
    PushButton,
    ResetFormAction,
    ScriptTrigger,
    SetLayerStateAction,
    SubmitFormAction,
    SubmitFormFlags,
    TextField,
    Transition,
    TransitionAction,
    TransitionStyle,
)

if TYPE_CHECKING:
    from pathlib import Path


def _build_raw_pdf(objects: dict[int, str], *, version: str = "1.7") -> bytes:
    """Build a minimal valid PDF with explicit object dictionaries and header version."""
    out = bytearray(f"%PDF-{version}\n".encode("ascii"))
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode("latin-1")
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode("ascii")
    for number in range(1, size):
        if number in offsets:
            out += f"{offsets[number]:010} 00000 n \n".encode("ascii")
        else:
            out += b"0000000000 65535 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    return bytes(out)


def _doc_with_layer_and_annot() -> tuple[pyropdf.Document, int, int]:
    """Create a 2-page PDF with an OCG and a note annotation, returning ``(doc, layer_xref, annot_xref)``."""
    doc = pyropdf.Document()
    page0 = doc.pages.new((400, 300))
    doc.pages.new((400, 300))
    ocg_xref = doc.objects.add({"Type": Name("OCG"), "Name": "DraftLayer"})
    ocg_obj = doc.objects[ocg_xref]
    doc.catalog["OCProperties"] = {
        "OCGs": [ocg_obj],
        "D": {"BaseState": Name("ON"), "ON": [ocg_obj]},
    }
    note = page0.annotations.add(Note((30, 30), contents="Margin note"))
    return doc, ocg_xref, note.xref


def _assert_valid(doc: pyropdf.Document) -> None:
    """Assert zero structural defects in memory (aside from unsaved trailer /Size) and after saving."""
    assert [d for d in doc.check().defects if d.clause != "FileTrailer/Size"] == []
    with pyropdf.open(doc.to_bytes()) as saved:
        assert not saved.check()


def _add_screen_annotation(
    page: pyropdf.Page,
    rect: tuple[float, float, float, float] = (50, 50, 150, 110),
) -> pyropdf.Annotation:
    """Append an indirect ``/Subtype /Screen`` annotation with a valid normal appearance stream."""
    doc = page.document
    ap_xref = doc.objects.add_stream(
        b"",
        dictionary={
            "Type": Name("XObject"),
            "Subtype": Name("Form"),
            "BBox": [0, 0, 100, 60],
            "Resources": {},
        },
    )
    annot_xref = doc.objects.add(
        {
            "Type": Name("Annot"),
            "Subtype": Name("Screen"),
            "Rect": list(rect),
            "P": page.obj,
            "F": 4,
            "AP": {"N": doc.objects[ap_xref]},
        }
    )
    existing = list(page.obj["Annots"]) if "Annots" in page.obj else []
    existing.append(doc.objects[annot_xref])
    page.obj["Annots"] = existing
    return next(a for a in page.annotations if a.xref == annot_xref)


def test_javascript_and_embedded_destination_validation() -> None:
    bad_int: Any = 42
    bad_bool: Any = True
    assert JavaScriptAction("app.beep(0);").script == "app.beep(0);"
    assert JavaScriptAction("").script == ""
    with pytest.raises(TypeError, match="script must be a str"):
        JavaScriptAction(bad_int)
    with pytest.raises(pyropdf.LimitError) as js_exc:
        JavaScriptAction("x" * (1024 * 1024 + 1))
    assert js_exc.value.code == "script_size"

    parent_hop = EmbeddedTarget(relationship="parent")
    assert parent_hop.relationship == "parent"
    with pytest.raises(ValueError, match="parent EmbeddedTarget"):
        EmbeddedTarget(relationship="parent", embedded_file="a.pdf")
    with pytest.raises(ValueError, match="child EmbeddedTarget"):
        EmbeddedTarget(relationship="child")
    with pytest.raises(ValueError, match="child EmbeddedTarget"):
        EmbeddedTarget(relationship="child", embedded_file="a.pdf", annotation_page=0, annotation=0)
    with pytest.raises(ValueError, match="both be set or both be None"):
        EmbeddedTarget(relationship="child", annotation_page=0)
    with pytest.raises(ValueError, match="not be negative"):
        EmbeddedTarget(relationship="child", annotation_page=-1, annotation=0)
    with pytest.raises(TypeError, match="non-negative int"):
        EmbeddedTarget(relationship="child", annotation_page=bad_bool, annotation=0)

    chain = EmbeddedTarget(embedded_file="leaf.pdf")
    for idx in range(31):
        chain = EmbeddedTarget(embedded_file=f"hop-{idx}.pdf", next_target=chain)
    assert chain.embedded_file == "hop-30.pdf"
    with pytest.raises(ValueError, match="32-hop"):
        EmbeddedTarget(embedded_file="overflow.pdf", next_target=chain)

    with pytest.raises(ValueError, match="at least one of target or file"):
        EmbeddedDestination(page=0)
    with pytest.raises(ValueError, match="cannot be combined with a named destination"):
        EmbeddedDestination(name="sec1", point=(10, 20), file="child.pdf")
    with pytest.raises(ValueError, match="zoom"):
        EmbeddedDestination(page=0, zoom=-1.0, file="child.pdf")
    bad_one: Any = 1
    with pytest.raises(TypeError, match="new_window"):
        EmbeddedDestination(page=0, file="child.pdf", new_window=bad_one)


def test_hide_layer_and_form_action_validation() -> None:
    bad_list_bool: Any = [True]
    bad_changes: Any = ["ON"]
    reserved_13: Any = 1 << 12
    reserved_15: Any = 1 << 14
    bad_charset: Any = "latin-1"
    bad_exclude: Any = 1

    assert HideAction("field_a").targets == ("field_a",)
    assert HideAction(7, hide=False) == HideAction((7,), hide=False)
    with pytest.raises(ValueError, match="between 1 and 4096"):
        HideAction(())
    with pytest.raises(ValueError, match="positive"):
        HideAction(0)
    with pytest.raises(TypeError, match="str or positive int"):
        HideAction(bad_list_bool)

    change = LayerStateChange(LayerStateTransition.TOGGLE, 5)
    assert change.layers == (5,)
    with pytest.raises(ValueError, match="positive"):
        LayerStateChange("ON", 0)
    with pytest.raises(ValueError, match="between 1 and 4096"):
        SetLayerStateAction(())
    with pytest.raises(TypeError, match="LayerStateChange"):
        SetLayerStateAction(bad_changes)

    submit = SubmitFormAction(
        "https://example.test/post",
        fields=["a", "b"],
        flags=SubmitFormFlags.EXPORT_FORMAT | SubmitFormFlags.GET_METHOD | SubmitFormFlags.EMBED_FORM,
        charset="utf-8",
    )
    assert submit.fields == ("a", "b")
    with pytest.raises(ValueError, match="reserved"):
        SubmitFormAction("https://example.test/post", flags=reserved_13)
    with pytest.raises(ValueError, match="reserved"):
        SubmitFormAction("https://example.test/post", flags=reserved_15)
    with pytest.raises(ValueError, match="charset"):
        SubmitFormAction("https://example.test/post", charset=bad_charset)
    with pytest.raises(TypeError, match="exclude must be a bool"):
        ResetFormAction(("a",), exclude=bad_exclude)
    with pytest.raises(ValueError, match="must not be empty"):
        ImportDataAction("")


@pytest.mark.spec("ecmascript-actions", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.spec("embedded-goto-actions", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.spec("hide-actions", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.spec("set-ocg-state-actions", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.spec("form-actions", "read", "interpret", "create", "edit", "preserve")
def test_all_new_actions_round_trip_across_target_sites_and_saves(tmp_path: Path) -> None:
    doc, layer_xref, annot_xref = _doc_with_layer_and_annot()
    page0 = doc.pages[0]
    page0.widgets.add(TextField("email", (20, 200, 180, 224)))
    button = page0.widgets.add(PushButton("go", (200, 200, 280, 224), caption="Go"))
    screen = _add_screen_annotation(page0)

    actions: list[pyropdf.Target] = [
        JavaScriptAction("var x = 1 + 2;"),
        EmbeddedDestination(
            page=1,
            point=Point(12.0, 24.0),
            zoom=1.5,
            target=EmbeddedTarget(
                relationship="parent",
                next_target=EmbeddedTarget(
                    relationship="child",
                    embedded_file="nested.pdf",
                    next_target=EmbeddedTarget(
                        relationship="child",
                        annotation_page=0,
                        annotation="att-1",
                    ),
                ),
            ),
            new_window=True,
        ),
        EmbeddedDestination(name="summary", file="packet.pdf", new_window=False),
        HideAction(("email", annot_xref), hide=False),
        SetLayerStateAction(
            (
                LayerStateChange(LayerStateTransition.OFF, (layer_xref,)),
                LayerStateChange(LayerStateTransition.TOGGLE, (layer_xref,)),
            ),
            preserve_radio_groups=False,
        ),
        SubmitFormAction(
            "https://example.com/collect",
            fields=("email",),
            flags=SubmitFormFlags.XFDF | SubmitFormFlags.INCLUDE_NO_VALUE_FIELDS,
            charset="utf-8",
        ),
        ResetFormAction(("email",), exclude=True),
        ImportDataAction("values.fdf"),
    ]

    # 1. Links: add, read, edit, save/reopen (full and incremental)
    for idx, act in enumerate(actions):
        y0 = 10 + idx * 18
        page0.links.add((20, y0, 140, y0 + 14), act)
    assert [lnk.target for lnk in page0.links] == actions

    # 2. Outline: write all, read entries & items, mutate one item's destination
    doc.outline = [OutlineEntry(1, f"Item {idx}", act) for idx, act in enumerate(actions)]
    assert [item.destination for item in doc.outline] == actions
    assert [entry.destination for entry in doc.outline.entries()] == actions
    doc.outline[0].destination = actions[-1]
    assert doc.outline[0].destination == actions[-1]
    doc.outline[0].destination = actions[0]

    # 3. Presentation NavigationStep
    page0.navigation_steps = [
        NavigationStep(next_action=actions[1], previous_action=actions[4], duration=1.5),
        NavigationStep(next_action=actions[5], previous_action=actions[6]),
    ]
    assert page0.navigation_steps[0].next_action == actions[1]
    assert page0.navigation_steps[0].previous_action == actions[4]

    # 4. OpenAction, Page triggers, Widget triggers, Screen action & triggers
    doc.open_action = actions[1]
    page0.triggers[PageTrigger.OPEN] = actions[3]
    page0.triggers[PageTrigger.CLOSE] = actions[7]
    button.triggers[AnnotationTrigger.ACTIVATE] = actions[5]
    button.triggers[AnnotationTrigger.PAGE_OPEN] = actions[4]
    screen.action = actions[0]
    screen.triggers[AnnotationTrigger.ENTER] = actions[2]
    screen.triggers[AnnotationTrigger.PAGE_VISIBLE] = actions[6]

    _assert_valid(doc)

    # Full save round-trip
    base_path = tmp_path / "actions.pdf"
    doc.save(base_path)
    with pyropdf.open(base_path) as reopened:
        assert not reopened.check()
        assert [lnk.target for lnk in reopened.pages[0].links] == actions
        assert [item.destination for item in reopened.outline] == actions
        assert reopened.pages[0].navigation_steps[0].next_action == actions[1]
        assert reopened.open_action == actions[1]
        assert dict(reopened.pages[0].triggers) == {
            PageTrigger.OPEN: actions[3],
            PageTrigger.CLOSE: actions[7],
        }
        re_btn = reopened.pages[0].widgets[1]
        assert re_btn.triggers[AnnotationTrigger.ACTIVATE] == actions[5]
        assert re_btn.triggers[AnnotationTrigger.PAGE_OPEN] == actions[4]
        re_screen = next(a for a in reopened.pages[0].annotations if a.type is pyropdf.AnnotationType.SCREEN)
        assert re_screen.action == actions[0]
        assert dict(re_screen.triggers) == {
            AnnotationTrigger.ENTER: actions[2],
            AnnotationTrigger.PAGE_VISIBLE: actions[6],
        }

        # Edit in place and incremental save
        reopened.pages[0].links[0].target = ResetFormAction()
        reopened.open_action = Destination(1, Point(20.0, 30.0), 2.0)
        reopened.save(base_path, incremental=True)

    with pyropdf.open(base_path) as inc_doc:
        assert not inc_doc.check()
        assert len(inc_doc.revisions) == 2
        assert inc_doc.pages[0].links[0].target == ResetFormAction()
        assert inc_doc.open_action == Destination(1, Point(20.0, 30.0), 2.0)


def test_hide_and_layer_actions_reject_missing_or_wrong_objects() -> None:
    doc, layer_xref, annot_xref = _doc_with_layer_and_annot()
    page = doc.pages[0]
    plain_xref = doc.objects.add({"NotAnAnnot": 1})

    with pytest.raises(ValueError, match="not an annotation dictionary"):
        page.links.add((10, 10, 60, 30), HideAction(plain_xref))
    with pytest.raises(ValueError, match="not an indirect annotation"):
        page.links.add((10, 10, 60, 30), HideAction(99999))
    with pytest.raises(ValueError, match="not an optional content group"):
        page.links.add((10, 10, 60, 30), SetLayerStateAction([LayerStateChange("ON", annot_xref)]))
    with pytest.raises(ValueError, match="not an optional content group"):
        page.links.add((10, 10, 60, 30), SetLayerStateAction([LayerStateChange("ON", 99999)]))

    # Valid layer_xref and annot_xref pass and promote version on a PDF 1.2 document
    old_pdf = _build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 200] >>",
            3: "<< /Type /Page /Parent 2 0 R /Resources << >> >>",
        },
        version="1.2",
    )
    with pyropdf.open(old_pdf) as doc12:
        sq = doc12.pages[0].annotations.add(pyropdf.Square((20, 20, 80, 80)))
        doc12.pages[0].links.add((10, 100, 80, 120), HideAction(sq.xref, hide=True))
        assert doc12.pages[0].links[0].target == HideAction(sq.xref, hide=True)
        _assert_valid(doc12)
        assert layer_xref > 0


def test_form_actions_resolve_indirect_field_references_and_streams() -> None:
    raw = _build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /OpenAction 8 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 300 200] >>",
            3: "<< /Type /Page /Parent 2 0 R /Resources << >> /Annots [6 0 R 7 0 R] >>",
            4: "<< /T (user) /Kids [5 0 R] >>",
            5: "<< /Parent 4 0 R /FT /Tx /T (name) >>",
            6: (
                "<< /Type /Annot /Subtype /Link /Rect [10 10 80 30] /Border [0 0 0] "
                "/A << /S /SubmitForm /F (https://api.example/form) /Fields [5 0 R (extra)] "
                "/Flags 12 /CharSet (Shift-JIS) >> >>"
            ),
            7: (
                "<< /Type /Annot /Subtype /Link /Rect [10 40 80 60] /Border [0 0 0] "
                "/A << /S /ResetForm /Fields [5 0 R] /Flags 1 >> >>"
            ),
            8: "<< /S /JavaScript /JS 9 0 R >>",
            9: "<< /Length 14 >>\nstream\napp.alert(42);\nendstream",
        }
    )
    with pyropdf.open(raw) as doc:
        assert doc.open_action == JavaScriptAction("app.alert(42);")
        assert doc.pages[0].links[0].target == SubmitFormAction(
            "https://api.example/form",
            fields=("user.name", "extra"),
            flags=SubmitFormFlags.EXPORT_FORMAT | SubmitFormFlags.GET_METHOD,
            charset="Shift-JIS",
        )
        assert doc.pages[0].links[1].target == ResetFormAction(("user.name",), exclude=True)


@pytest.mark.spec("trigger-events", "read", "interpret", "create", "edit", "preserve")
def test_document_and_page_triggers_and_open_action_undo_redo() -> None:
    seed = pyropdf.Document()
    seed.pages.new((300, 300))
    seed.pages.new((300, 300))
    doc = pyropdf.open(seed.to_bytes())
    page0 = doc.pages[0]

    assert doc.open_action is None
    pos = doc.history.position
    doc.open_action = Destination(1, Point(10.0, 20.0), 1.25)
    assert doc.history.position == pos + 1
    assert isinstance(doc.catalog["OpenAction"], pyropdf.Array)
    assert doc.open_action == Destination(1, Point(10.0, 20.0), 1.25)

    doc.open_action = Launch("manual.pdf")
    assert doc.history.position == pos + 2
    assert doc.open_action == Launch("manual.pdf")
    doc.history.undo()
    assert doc.open_action == Destination(1, Point(10.0, 20.0), 1.25)
    doc.history.redo()
    assert doc.open_action == Launch("manual.pdf")
    del doc.open_action
    assert doc.open_action is None
    assert "OpenAction" not in doc.catalog

    assert isinstance(doc.triggers, DocumentTriggers)
    pos = doc.history.position
    doc.triggers.update(
        {
            DocumentTrigger.CLOSE: "var closing = 1;",
            DocumentTrigger.WILL_SAVE: "var saving = 1;",
            DocumentTrigger.DID_SAVE: "var saved = 1;",
            DocumentTrigger.WILL_PRINT: "var printing = 1;",
            DocumentTrigger.DID_PRINT: "var printed = 1;",
        }
    )
    assert doc.history.position == pos + 1
    assert len(doc.triggers) == 5
    assert DocumentTrigger.WILL_PRINT in doc.triggers
    del doc.triggers[DocumentTrigger.DID_PRINT]
    assert len(doc.triggers) == 4
    doc.triggers = {DocumentTrigger.CLOSE: "var only_close = 1;"}
    assert dict(doc.triggers) == {DocumentTrigger.CLOSE: "var only_close = 1;"}
    assert not doc.check()
    del doc.triggers
    assert dict(doc.triggers) == {}
    assert "AA" not in doc.catalog

    assert isinstance(page0.triggers, PageTriggers)
    pos = doc.history.position
    page0.triggers[PageTrigger.OPEN] = TransitionAction(Transition(style=TransitionStyle.FADE))
    assert doc.history.position == pos + 1
    page0.triggers = {
        PageTrigger.OPEN: NamedAction("FirstPage"),
        PageTrigger.CLOSE: "https://example.com/bye",
    }
    assert len(page0.triggers) == 2
    del page0.triggers[PageTrigger.OPEN]
    assert dict(page0.triggers) == {PageTrigger.CLOSE: "https://example.com/bye"}
    del page0.triggers
    assert "AA" not in page0.obj

    saved_triggers = doc.pages[1].triggers
    del doc.pages[1]
    with pytest.raises(pyropdf.StalePageError):
        _ = len(saved_triggers)
    assert repr(saved_triggers) == "<stale PageTriggers>"


def test_widget_triggers_coexist_with_field_scripts() -> None:
    seed = pyropdf.Document()
    seed_page = seed.pages.new((300, 300))
    seed_page.widgets.add(TextField("amount", (20, 20, 160, 44)))
    seed_page.widgets.add(PushButton("run", (180, 20, 250, 44), caption="Run"))
    doc = pyropdf.open(seed.to_bytes())
    page0 = doc.pages[0]
    btn = page0.widgets[1]

    assert isinstance(btn.triggers, ActionTriggers)
    amount_field = doc.form["amount"]
    amount_widget = page0.widgets[0]
    amount_field.scripts[ScriptTrigger.FORMAT] = "AFNumber_Format(2, 0, 0, 0, '', true);"
    amount_widget.triggers = {
        AnnotationTrigger.FOCUS: JavaScriptAction("var focused = true;"),
        AnnotationTrigger.BLUR: ResetFormAction(("amount",)),
        AnnotationTrigger.PAGE_OPEN: NamedAction("NextPage"),
        AnnotationTrigger.ACTIVATE: "https://example.com/help",
    }
    assert dict(amount_field.scripts) == {ScriptTrigger.FORMAT: "AFNumber_Format(2, 0, 0, 0, '', true);"}
    assert len(amount_widget.triggers) == 4
    del amount_widget.triggers[AnnotationTrigger.FOCUS]
    assert AnnotationTrigger.FOCUS not in amount_widget.triggers
    del amount_widget.triggers
    assert dict(amount_widget.triggers) == {}
    assert dict(amount_field.scripts) == {ScriptTrigger.FORMAT: "AFNumber_Format(2, 0, 0, 0, '', true);"}
    assert not doc.check()


def test_screen_annotation_action_and_triggers_and_non_screen_guards() -> None:
    seed = pyropdf.Document()
    seed_page = seed.pages.new((300, 300))
    _add_screen_annotation(seed_page)
    seed_page.annotations.add(Note((30, 150), contents="Plain note"))
    doc = pyropdf.open(seed.to_bytes())
    page0 = doc.pages[0]
    screen = next(a for a in page0.annotations if a.type is pyropdf.AnnotationType.SCREEN)
    note = next(a for a in page0.annotations if a.type is pyropdf.AnnotationType.TEXT)

    assert note.action is None
    assert dict(note.triggers) == {}
    with pytest.raises(ValueError, match="only Screen annotations"):
        note.action = NamedAction("NextPage")
    with pytest.raises(ValueError, match="only Screen annotations"):
        del note.action
    with pytest.raises(ValueError, match="only Screen annotations"):
        note.triggers[AnnotationTrigger.ENTER] = NamedAction("NextPage")
    with pytest.raises(ValueError, match="only Screen annotations"):
        note.triggers = {AnnotationTrigger.ENTER: NamedAction("NextPage")}
    with pytest.raises(ValueError, match="only Screen annotations"):
        del note.triggers

    for unsupported in (AnnotationTrigger.FOCUS, AnnotationTrigger.BLUR, AnnotationTrigger.ACTIVATE):
        with pytest.raises(ValueError, match="not a supported trigger"):
            screen.triggers[unsupported] = NamedAction("NextPage")

    pos = doc.history.position
    screen.action = "https://example.com/play"
    assert doc.history.position == pos + 1
    screen.triggers = {
        AnnotationTrigger.ENTER: NamedAction("NextPage"),
        AnnotationTrigger.EXIT: NamedAction("PrevPage"),
        AnnotationTrigger.PAGE_INVISIBLE: ResetFormAction(),
    }
    assert doc.history.position == pos + 2
    assert not doc.check()
    doc.history.undo()
    assert dict(screen.triggers) == {}
    assert screen.action == "https://example.com/play"
    doc.history.redo()
    assert len(screen.triggers) == 3
    del screen.triggers[AnnotationTrigger.EXIT]
    assert len(screen.triggers) == 2
    del screen.action
    assert screen.action is None
    del screen.triggers
    assert "AA" not in screen.obj
    assert not doc.check()


def test_document_scripts_name_tree_scaling_cleanup_and_limits() -> None:
    doc = pyropdf.Document()
    doc.pages.new((200, 200))
    doc.pages.new((200, 200))
    doc.named_pages["keep-page"] = 0

    assert isinstance(doc.scripts, DocumentScripts)
    assert dict(doc.scripts) == {}

    # Write > 64 entries so _build_canonical_js_tree creates intermediate /Kids + /Limits nodes
    many = {f"script-{i:03d}": f"var v{i} = {i};" for i in range(135)}
    pos = doc.history.position
    doc.scripts = many
    assert doc.history.position == pos + 1
    assert len(doc.scripts) == 135
    assert doc.scripts["script-000"] == "var v0 = 0;"
    assert doc.scripts["script-134"] == "var v134 = 134;"
    assert "Kids" in doc.catalog["Names"]["JavaScript"]
    _assert_valid(doc)

    # Reopen and verify sorted order and preservation
    with pyropdf.open(doc.to_bytes()) as reopened:
        assert not reopened.check()
        assert list(reopened.scripts) == sorted(many)
        assert dict(reopened.scripts) == many

    # Single-key delete, update, undo/redo, and sibling /Names preservation on del doc.scripts
    del doc.scripts["script-064"]
    assert "script-064" not in doc.scripts
    assert len(doc.scripts) == 134
    with pytest.raises(KeyError):
        del doc.scripts["missing"]

    doc.scripts[" unicode-key-日本語 "] = "var jp = '東京';"
    assert doc.scripts[" unicode-key-日本語 "] == "var jp = '東京';"
    _assert_valid(doc)

    del doc.scripts
    assert dict(doc.scripts) == {}
    assert "JavaScript" not in doc.catalog["Names"]
    assert doc.named_pages["keep-page"].index == 0
    _assert_valid(doc)

    del doc.named_pages
    doc.scripts["alpha"] = "var a = 1;"
    assert "Names" in doc.catalog
    doc.scripts.clear()
    assert "Names" not in doc.catalog

    # Aggregate 1 MiB script_size limit on doc.scripts and doc.triggers
    doc.scripts["part1"] = "a" * 600_000
    with pytest.raises(pyropdf.LimitError) as script_lim:
        doc.scripts["part2"] = "b" * 600_000
    assert script_lim.value.code == "script_size"
    assert list(doc.scripts) == ["part1"]

    doc.triggers[DocumentTrigger.CLOSE] = "a" * 600_000
    with pytest.raises(pyropdf.LimitError) as trig_lim:
        doc.triggers[DocumentTrigger.WILL_SAVE] = "b" * 600_000
    assert trig_lim.value.code == "script_size"
    assert list(doc.triggers) == [DocumentTrigger.CLOSE]


def test_malformed_and_deep_trees_or_targets_are_safely_bounded() -> None:
    # 33-level /Names /JavaScript tree raises LimitError("object_depth")
    objs: dict[int, str] = {
        1: "<< /Type /Catalog /Pages 2 0 R /Names << /JavaScript 10 0 R >> >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 100 100] >>",
        3: "<< /Type /Page /Parent 2 0 R /Resources << >> >>",
    }
    for depth in range(33):
        node_num = 10 + depth
        objs[node_num] = f"<< /Kids [{node_num + 1} 0 R] >>"
    objs[43] = "<< /Names [(leaf) << /S /JavaScript /JS (1;) >>] >>"
    with pyropdf.open(_build_raw_pdf(objs)) as deep_doc:
        with pytest.raises(pyropdf.LimitError) as exc_info:
            _ = dict(deep_doc.scripts)
        assert exc_info.value.code == "object_depth"

    # 33-hop /GoToE /T chain raises LimitError("object_depth")
    gotoe_objs: dict[int, str] = {
        1: "<< /Type /Catalog /Pages 2 0 R /OpenAction << /S /GoToE /D [0 /Fit] /T 10 0 R >> >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 100 100] >>",
        3: "<< /Type /Page /Parent 2 0 R /Resources << >> >>",
    }
    for hop in range(32):
        hop_num = 10 + hop
        gotoe_objs[hop_num] = f"<< /R /C /N (f{hop}.pdf) /T {hop_num + 1} 0 R >>"
    gotoe_objs[42] = "<< /R /C /N (last.pdf) >>"
    with pyropdf.open(_build_raw_pdf(gotoe_objs)) as deep_gotoe:
        with pytest.raises(pyropdf.LimitError) as gotoe_exc:
            _ = deep_gotoe.open_action
        assert gotoe_exc.value.code == "object_depth"
