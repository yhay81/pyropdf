"""Interaction appearances, line geometry, popup inheritance and redaction marks."""

from __future__ import annotations

import io
import shutil
import subprocess
from dataclasses import replace
from typing import TYPE_CHECKING

import pikepdf
import pytest

import pyropdf as p

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Literal


def source() -> bytes:
    doc = p.Document()
    page = doc.pages.new((200, 200))
    with page.draw() as canvas:
        canvas.text("original text", (24, 42), size=10)
    return doc.to_bytes()


def form(doc: p.Document, color: str = "0 1 0") -> p.Stream:
    ref = doc.objects.add_stream(
        f"{color} rg 2 5 20 10 re f".encode("ascii"),
        {
            "Type": p.Name("XObject"),
            "Subtype": p.Name("Form"),
            "BBox": [2, 5, 22, 15],
            "Resources": {},
            "Matrix": [1, 0, 0, 1, 3, 7],
        },
    )
    return doc.objects[ref]


def saved(doc: p.Document, original: bytes, *, incremental: bool) -> bytes:
    data = doc.to_bytes(incremental=incremental)
    assert not incremental or data.startswith(original)
    return data


def pixel(path: Path, x: int, y: int) -> tuple[int, ...]:
    executable = shutil.which("pdftoppm")
    if executable is None:
        pytest.skip("Poppler is unavailable")
    ppm = subprocess.run(  # noqa: S603 - the command and switches are fixed.
        [executable, "-r", "72", "-singlefile", str(path)], check=True, capture_output=True
    ).stdout
    header, size, maximum, raw = ppm.split(b"\n", 3)
    assert (header, size, maximum) == (b"P6", b"200 200", b"255")
    start = (y * 200 + x) * 3
    return tuple(raw[start : start + 3])


@pytest.mark.spec("appearance-streams", "create", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_interaction_modes_copy_shared_state_dictionaries(*, incremental: bool) -> None:
    original = source()
    doc = p.open(original)
    page = doc.pages[0]
    one = page.annotations.add(p.Note((20, 50), contents="first"))
    two = page.annotations.add(p.Note((80, 50), contents="second"))
    normal = one.appearance_stream()
    assert normal is not None
    assert one.appearance_stream("rollover") == normal
    down = one.set_appearance("down", b"0 1 0 rg 0 0 10 10 re f", state="Yes", bbox=(0, 0, 10, 10))
    one.obj["AS"] = p.Name("Yes")
    one.obj["AP"]["Private"] = 123
    shared = doc.objects[doc.objects.add(dict(one.obj["AP"]))]
    one.obj["AP"] = shared
    two.obj["AP"] = shared
    one.set_appearance("down", form(doc), state="Off")
    rollover = one.set_appearance("rollover", b"0 0 1 rg 0 0 10 10 re f")
    assert one.appearance_stream("down") == down
    assert one.appearance_stream("rollover") == rollover
    assert two.appearance_stream("down", state="Off") is None
    one.color = p.Color(1, 0, 1)
    assert one.appearance_stream("down") == down
    assert one.appearance_stream("rollover") == rollover
    button = page.widgets.add(p.PushButton("state", (20, 160, 100, 180)))
    button.set_appearance("rollover", rollover)
    assert button.appearance_stream("rollover") == rollover
    data = saved(doc, original, incremental=incremental)
    with pikepdf.open(io.BytesIO(data)) as check:
        a, b, w = check.pages[0].Annots
        assert a.AP.R.objgen == w.AP.R.objgen
        assert a.AP.D.Yes.objgen == b.AP.D.Yes.objgen
        assert "/Off" not in b.AP.D
        assert "/R" not in b.AP
        assert int(a.AP.Private) == 123
        assert b"0 0 1 rg" in a.AP.R.read_bytes()
        assert a.AP.N.objgen != b.AP.N.objgen


@pytest.mark.spec("appearance-streams", "create", "edit")
def test_interaction_appearance_rejects_bad_forms_and_malformed_dictionary() -> None:
    doc = p.open(source())
    note = doc.pages[0].annotations.add(p.Note((5, 5)))
    with pytest.raises(ValueError, match="this document"):
        note.set_appearance("rollover", form(p.open(source())))
    assert list(note.set_appearance("rollover", b"", bbox=range(4))["BBox"]) == [0, 1, 2, 3]
    note.set_appearance("down", form(doc))
    with pytest.raises(ValueError, match="single-state"):
        note.set_appearance("down", b"x", state="Off")
    with pytest.raises(ValueError, match="positive area"):
        note.set_appearance("rollover", b"x", bbox=(0, 0, 0, 2))
    note.obj["AP"] = {"N": 4}
    with pytest.raises(p.MalformedPdfError, match="Form"):
        note.appearance_stream()


@pytest.mark.spec("line-annotations", "read", "interpret", "create", "edit")
@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("direction", [1, -1])
def test_leader_offsets_and_caption_are_visible_after_edit(
    tmp_path: Path, *, incremental: bool, direction: int
) -> None:
    original = source()
    doc = p.open(original)
    page = doc.pages[0]
    details = p.LineDetails(
        leader_length=20 * direction,
        leader_extension=4,
        leader_offset=6,
        caption=True,
        position="Top",
        caption_offset=(0, 1),
    )
    line = page.annotations.add(
        p.LineAnnotation((20, 110), (160, 110), contents="Old", details=details, border=p.Border(2))
    )
    line.obj["Private"] = 55
    assert line.line_details == details
    line.contents = "36 mm"
    line.line_details = replace(details, position="Inline")
    data = saved(doc, original, incremental=incremental)
    reopened = p.open(data).pages[0].annotations[0]
    assert reopened.line_details == replace(details, position="Inline")
    with pikepdf.open(io.BytesIO(data)) as pdf:
        annot = pdf.pages[0].Annots[0]
        assert list(annot.L) == [20, 90, 160, 90]
        assert float(annot.LL) == 20 * direction
        assert float(annot.LLO) == 6
        assert annot.Cap
        assert int(annot.Private) == 55
        assert annot.AP.N.Resources.Font.Helv.BaseFont == "/Helvetica"
        assert b"<3336206D6D> Tj" in annot.AP.N.read_bytes()
    path = tmp_path / "dimension.pdf"
    path.write_bytes(data)
    assert pixel(path, 35, 110 - direction * 26)[0] > 200
    assert pixel(path, 35, 110 - direction * 26)[1] < 90
    assert pixel(path, 35, 110) == (255, 255, 255)
    assert pixel(path, 20, 110 - direction * 14)[1] < 100
    assert pixel(path, 20, 110 - direction * 2) == (255, 255, 255)


@pytest.mark.spec("line-annotations", "read", "interpret", "create", "edit")
def test_line_details_refuse_malformed_leaders_and_keep_interaction_forms() -> None:
    doc = p.open(source())
    line = doc.pages[0].annotations.add(p.LineAnnotation((1, 20), (100, 20)))
    other = line.set_appearance("down", form(doc))
    line.line_details = p.LineDetails(leader_length=2, leader_extension=1)
    line.vertices = ((3, 5), (100, 50))
    assert line.appearance_stream("down") == other
    assert line.line_details == p.LineDetails(leader_length=2, leader_extension=1)
    with pytest.raises(ValueError, match="nonnegative"):
        p.LineDetails(leader_length=4, leader_extension=-1)
    del line.obj["LL"]
    with pytest.raises(p.MalformedPdfError, match="requires /LL"):
        _ = line.line_details


@pytest.mark.spec("popup-annotations", "interpret")
@pytest.mark.parametrize("incremental", [False, True])
def test_popup_parent_overrides_local_values_and_conflict_is_reported(*, incremental: bool) -> None:
    original = source()
    doc = p.open(original)
    page = doc.pages[0]
    note = page.annotations.add(p.Note((20, 50), contents="owner"))
    note.author = "Ada"
    note.color = p.Color(0, 1, 0)
    note.popup_rect = (40, 50, 85, 80)
    note.is_open = True
    popup = note.obj["Popup"]
    popup["Contents"] = "child"
    popup["T"] = "other"
    popup["C"] = [1, 0, 0]
    data = saved(doc, original, incremental=incremental)
    owner = p.open(data).pages[0].annotations[0]
    info = owner.popup_info
    assert info
    assert (info.contents, info.author, info.color, info.is_open) == ("owner", "Ada", p.Color(0, 1, 0), True)
    assert info.rect == p.Rect(40, 50, 85, 80)
    assert info.modified == owner.modified
    with pikepdf.open(io.BytesIO(data)) as pdf:
        a, b = pdf.pages[0].Annots
        assert a.Popup.objgen == b.objgen
        assert b.Parent.objgen == a.objgen
        assert str(b.Contents) == "child"
    del owner.obj["Popup"]["Parent"]
    assert owner.popup_info
    assert owner.popup_info.contents == "child"
    assert owner.popup_info.color == p.Color(1, 0, 0)
    owner.obj["Popup"]["Parent"] = owner.page.obj
    with pytest.raises(p.MalformedPdfError, match="Parent"):
        _ = owner.popup_info


@pytest.mark.spec("redaction-annotations", "read", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_redaction_marks_reopen_without_removing_the_marked_text(tmp_path: Path, *, incremental: bool) -> None:
    original = source()
    doc = p.open(original)
    page = doc.pages[0]
    rect = p.Rect(20, 25, 105, 55)
    quad = p.Quad((24, 27), (100, 29), (21, 48), (99, 51))
    mark = page.annotations.add(p.RedactionMark(rect, quads=(quad,), interior_color=p.Color(0.2, 0.3, 0.4)))
    mark.obj["Private"] = "preserved"
    overlay = form(doc)
    mark.redaction = p.RedactionMark(
        rect,
        quads=(quad,),
        overlay_text="REMOVED",
        default_appearance=b"/Helv 10 Tf 0 g",
        repeat=True,
        alignment=2,
        interior_color=p.Color(0.1, 0.5, 0.8),
        overlay=overlay,
    )
    data = saved(doc, original, incremental=incremental)
    reopened = p.open(data)
    info = reopened.pages[0].annotations[0].redaction
    assert info is not None
    assert info.quads == (quad,)
    assert info.overlay_text == "REMOVED"
    assert info.repeat
    assert info.overlay is not None
    assert info.overlay.read() == overlay.read()
    assert "original text" in reopened.pages[0].text()
    with pikepdf.open(io.BytesIO(data)) as pdf:
        annot = pdf.pages[0].Annots[0]
        assert list(annot.QuadPoints) == [24, 173, 100, 171, 21, 152, 99, 149]
        assert [float(v) for v in annot.IC] == pytest.approx([0.1, 0.5, 0.8])
        assert annot.RO.Subtype == "/Form"
        assert annot.RO.objgen != annot.AP.N.objgen
        assert bytes(annot.DA) == b"/Helv 10 Tf 0 g"
        assert int(annot.Q) == 2
        assert str(annot.Private) == "preserved"
    path = tmp_path / "redaction.pdf"
    path.write_bytes(data)
    assert pixel(path, 60, 28)[1] < 245
    mark.redaction = p.RedactionMark(rect)
    assert all(k not in mark.obj for k in ("QuadPoints", "OverlayText", "DA", "RO", "IC"))


@pytest.mark.spec("redaction-annotations", "read", "create", "edit", "preserve")
def test_redaction_marks_validate_page_space_and_refuse_invalid_edits_atomically() -> None:
    doc = p.open(source())
    page = doc.pages[0]
    page.rotation = 90
    page.obj["UserUnit"] = 2
    rect = p.Rect(20, 24, 70, 66)
    mark = page.annotations.add(p.RedactionMark(rect, quads=(rect.quad,)))
    assert mark.redaction == p.RedactionMark(rect, quads=(rect.quad,))
    with pytest.raises(ValueError, match="default_appearance"):
        p.RedactionMark(rect, overlay_text="hidden")
    with pytest.raises(ValueError, match="this document"):
        mark.redaction = p.RedactionMark(rect, overlay=form(p.open(source())))
    assert mark.redaction == p.RedactionMark(rect, quads=(rect.quad,))
    mark.obj["QuadPoints"] = [1, 2]
    with pytest.raises(p.MalformedPdfError, match="eight"):
        _ = mark.redaction


@pytest.mark.spec("widget-annotations", "read", "create", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_pushbutton_icons_share_the_fit_and_keep_other_modes(tmp_path: Path, *, incremental: bool) -> None:
    original = source()
    doc = p.open(original)
    page = doc.pages[0]
    button = page.widgets.add(p.PushButton("go", (25, 70, 125, 120), caption="Go"))
    green = form(doc)
    blue = form(doc, "0 0 1")
    button.obj["MK"]["Private"] = 42
    button.set_icon("normal", green, fit=p.IconFit(when="never", anchor=(1, 0), fit_bounds=True))
    button.set_icon("rollover", blue)
    button.set_icon("down", green)
    button.obj["MK"]["IF"]["PrivateFit"] = 7
    button.icon_fit = p.IconFit(when="never", anchor=(1, 0), fit_bounds=True)
    button.background_color = p.Color(1, 1, 1)
    data = saved(doc, original, incremental=incremental)
    again = p.open(data).pages[0].widgets[0]
    assert again.icon_fit == p.IconFit(when="never", anchor=(1, 0), fit_bounds=True)
    again_rollover = again.icon("rollover")
    assert again_rollover is not None
    assert again_rollover.read() == blue.read()
    with pikepdf.open(io.BytesIO(data)) as pdf:
        w = pdf.pages[0].Annots[0]
        assert w.MK.TP == 1
        assert w.MK.Private == 42
        assert w.MK.IF.PrivateFit == 7
        assert str(w.MK.CA) == "Go"
        assert w.MK.I.objgen == w.MK.IX.objgen
        assert w.MK.I.objgen != w.MK.RI.objgen
        for mode, icon in ((w.AP.N, w.MK.I), (w.AP.R, w.MK.RI), (w.AP.D, w.MK.IX)):
            assert mode.Resources.XObject.Icon.objgen == icon.objgen
            assert b"1 0 0 1 75 -12 cm /Icon Do" in mode.read_bytes()
    path = tmp_path / "button.pdf"
    path.write_bytes(data)
    assert pixel(path, 115, 115) == (0, 255, 0)
    assert pixel(path, 65, 95) == (255, 255, 255)


@pytest.mark.spec("widget-annotations", "read", "create", "edit")
@pytest.mark.parametrize(
    ("when", "proportional", "width", "height", "scale"),
    [
        ("always", True, 100, 60, b"5 0 0 5"),
        ("always", False, 100, 60, b"5 0 0 6"),
        ("larger", True, 10, 20, b"0.5 0 0 0.5"),
        ("larger", True, 100, 60, b"1 0 0 1"),
        ("smaller", True, 100, 60, b"5 0 0 5"),
        ("smaller", True, 10, 20, b"1 0 0 1"),
    ],
)
def test_icon_scaling_policies_use_the_transformed_form_box(
    *, when: Literal["always", "larger", "smaller", "never"], proportional: bool, width: int, height: int, scale: bytes
) -> None:
    doc = p.open(source())
    page = doc.pages[0]
    w = page.widgets.add(p.PushButton("go", (10, 60, 10 + width, 60 + height)))
    w.set_icon("normal", form(doc), fit=p.IconFit(when=when, proportional=proportional, fit_bounds=True))
    stream = w.appearance_stream()
    assert stream
    assert scale + b" " in stream.read().split(b"re W n ")[1]
    w.icon_fit = replace(w.icon_fit, fit_bounds=False)
    inset_stream = w.appearance_stream()
    assert inset_stream is not None
    assert b"q 1 1 " in inset_stream.read()


@pytest.mark.spec("widget-annotations", "read", "create", "edit")
@pytest.mark.parametrize("position", range(7))
def test_caption_positions_keep_fitted_icons_and_caption_resources(position: int) -> None:
    doc = p.open(source())
    w = doc.pages[0].widgets.add(p.PushButton("go", (20, 50, 160, 120), caption="Buy"))
    icon = form(doc)
    w.set_icon("normal", icon, position=position)
    ap = w.appearance_stream()
    assert ap is not None
    assert (b"/Icon Do" in ap.read()) == (position != 0)
    assert (b"BT" in ap.read()) == (position != 1)
    assert (ap["Resources"].get("Font") is not None) == (position != 1)
    w.rect = (30, 30, 140, 130)
    assert w.obj["MK"]["TP"] == position
    assert w.icon() == icon


@pytest.mark.spec("widget-annotations", "read", "create", "edit")
def test_widget_icons_refuse_foreign_images_and_invalid_fit_without_changing_the_button() -> None:
    doc = p.open(source())
    page = doc.pages[0]
    w = page.widgets.add(p.PushButton("go", (20, 50, 120, 100)))
    with pytest.raises(ValueError, match="this document"):
        w.set_icon("down", form(p.open(source())))
    ref = doc.objects.add_stream(b"x", {"Subtype": p.Name("Image"), "Width": 1, "Height": 1})
    with pytest.raises(ValueError, match="/Form"):
        w.set_icon("normal", doc.objects[ref])
    with pytest.raises(ValueError, match="fractions"):
        p.IconFit(anchor=(0, 2))
    text = page.widgets.add(p.TextField("text", (10, 130, 80, 150)))
    with pytest.raises(ValueError, match="pushbutton"):
        text.set_icon("normal", form(doc))
    assert w.icon() is None
    w.obj["MK"]["IF"] = {"A": [0, 2]}
    with pytest.raises(p.MalformedPdfError, match="fractions"):
        _ = w.icon_fit
