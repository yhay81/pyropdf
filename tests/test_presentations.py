"""Tests for page transitions, display durations, sub-page navigation, transition actions, and thumbnails."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import pyropdf
from pyropdf import (
    Colorspace,
    Destination,
    DocumentLimits,
    Launch,
    LimitError,
    Name,
    NamedAction,
    NavigationStep,
    OutlineEntry,
    Pixmap,
    Point,
    RemoteDestination,
    StalePageError,
    Transition,
    TransitionAction,
    TransitionDimension,
    TransitionMotion,
    TransitionStyle,
)

if TYPE_CHECKING:
    from pathlib import Path


def _pdf14_bytes() -> bytes:
    """Build a minimal valid PDF 1.4 two-page document."""
    body = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Resources << >> >>\nendobj\n"
        b"4 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Resources << >> >>\nendobj\n"
    )
    offsets = []
    pos = 0
    for marker in (b"1 0 obj", b"2 0 obj", b"3 0 obj", b"4 0 obj"):
        idx = body.index(marker, pos)
        offsets.append(idx)
        pos = idx + 1
    xref_offset = len(body)
    xref = [b"xref\n0 5\n0000000000 65535 f \n"]
    xref.extend(f"{off:010d} 00000 n \n".encode("ascii") for off in offsets)
    trailer = f"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    return body + b"".join(xref) + trailer


def _solid_pixmap(
    width: int,
    height: int,
    value: tuple[int, ...],
    *,
    gray: bool = False,
    alpha: bool = False,
) -> Pixmap:
    with pyropdf.Document() as tmp:
        page = tmp.pages.new((width, height))
        cs = Colorspace.GRAY if gray else Colorspace.RGB
        base = page.pixmap(dpi=72, colorspace=cs, alpha=alpha)
    return base.fill(value)


def _assert_valid(doc: pyropdf.Document) -> None:
    """Assert zero structural defects in memory (aside from unsaved trailer /Size) and after saving."""
    assert [d for d in doc.check().defects if d.clause != "FileTrailer/Size"] == []
    with pyropdf.open(doc.to_bytes()) as saved:
        assert not saved.check()


@pytest.mark.spec("presentations", "read", "interpret", "create", "edit", "preserve")
def test_page_duration_crud_undo_and_save_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "slides.pdf"
    path.write_bytes(_pdf14_bytes())

    with pyropdf.open(path) as doc:
        page = doc.pages[0]
        assert page.duration is None

        page.duration = 4.5
        assert page.duration == 4.5
        assert doc.pages[1].duration is None
        assert not doc.check()

        doc.history.undo()
        assert page.duration is None
        doc.history.redo()
        assert page.duration == 4.5

        page.duration = 0
        assert page.duration == 0.0
        assert not doc.check()
        doc.save(path, incremental=True)

    with pyropdf.open(path) as reopened:
        assert reopened.pages[0].duration == 0.0
        del reopened.pages[0].duration
        assert reopened.pages[0].duration is None
        del reopened.pages[0].duration  # Idempotent when absent.
        assert not reopened.check()
        full_path = tmp_path / "full.pdf"
        reopened.save(full_path)

    with pyropdf.open(full_path) as final:
        assert final.pages[0].duration is None
        assert not final.check()


def test_page_duration_validation_and_stale_page() -> None:
    with pyropdf.Document() as doc:
        page = doc.pages.new((200, 100))
        doc.pages.new((200, 100))

        for bad_type in (True, False, None, "5"):
            with pytest.raises(TypeError):
                page.duration = bad_type  # ty: ignore[invalid-assignment]

        for bad_val in (-0.1, float("nan"), float("inf"), 1e39):
            with pytest.raises(ValueError, match="duration"):
                page.duration = bad_val

        del doc.pages[0]
        with pytest.raises(StalePageError):
            _ = page.duration
        with pytest.raises(StalePageError):
            page.duration = 2.0
        with pytest.raises(StalePageError):
            del page.duration


@pytest.mark.spec("presentations", "read", "interpret", "create", "edit", "preserve")
def test_all_transition_styles_check_clean_and_round_trip(tmp_path: Path) -> None:
    valid_transitions = [
        Transition(),
        Transition(
            style=TransitionStyle.SPLIT,
            duration=1.25,
            dimension=TransitionDimension.VERTICAL,
            motion=TransitionMotion.OUTWARD,
        ),
        Transition(style=TransitionStyle.BLINDS, dimension=TransitionDimension.HORIZONTAL),
        Transition(style=TransitionStyle.BOX, motion=TransitionMotion.INWARD),
        Transition(style=TransitionStyle.WIPE, direction=180, duration=0.5),
        Transition(style=TransitionStyle.DISSOLVE, duration=2.0),
        Transition(style=TransitionStyle.GLITTER, direction=315),
        Transition(
            style=TransitionStyle.FLY,
            duration=1.5,
            motion=TransitionMotion.INWARD,
            direction="none",
            scale=0.5,
            rectangular=True,
        ),
        Transition(style=TransitionStyle.FLY, direction=270, scale=1.0, rectangular=False),
        Transition(style=TransitionStyle.PUSH, direction=0),
        Transition(style=TransitionStyle.COVER, direction=270),
        Transition(style=TransitionStyle.UNCOVER, direction=0),
        Transition(style=TransitionStyle.FADE, duration=0.75),
    ]

    with pyropdf.open(_pdf14_bytes()) as doc:
        page = doc.pages[0]
        assert page.transition is None

        # PDF 1.1 transition keeps PDF 1.4 without forcing /Version 1.5.
        page.transition = valid_transitions[1]
        assert page.transition == valid_transitions[1]
        assert "Version" not in doc.catalog
        assert not doc.check()

        # PDF 1.5 transitions promote catalog /Version to 1.5 cleanly.
        for expected in valid_transitions:
            page.transition = expected
            assert page.transition == expected
            assert not doc.check()

        assert doc.catalog["Version"] == "1.5"
        out = tmp_path / "transitions.pdf"
        doc.save(out)

    with pyropdf.open(out) as reopened:
        assert reopened.pages[0].transition == valid_transitions[-1]
        reopened.pages[0].transition = Transition(style=TransitionStyle.WIPE, direction=90)
        reopened.history.undo()
        assert reopened.pages[0].transition == valid_transitions[-1]
        reopened.history.redo()
        assert reopened.pages[0].transition == Transition(style=TransitionStyle.WIPE, direction=90)

        del reopened.pages[0].transition
        assert reopened.pages[0].transition is None
        assert not reopened.check()


def test_transition_validation_and_malformed_dict_tolerated() -> None:
    with pytest.raises(TypeError):
        Transition(style=123)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="NotAStyle"):
        Transition(style="NotAStyle")  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        Transition(duration=True)
    with pytest.raises(ValueError, match="duration"):
        Transition(duration=-1.0)
    with pytest.raises(ValueError, match="dimension"):
        Transition(style=TransitionStyle.FADE, dimension=TransitionDimension.HORIZONTAL)
    with pytest.raises(ValueError, match="motion"):
        Transition(style=TransitionStyle.WIPE, motion=TransitionMotion.INWARD)
    with pytest.raises(ValueError, match="scale"):
        Transition(style=TransitionStyle.WIPE, scale=0.5)
    with pytest.raises(ValueError, match="rectangular"):
        Transition(style=TransitionStyle.BOX, rectangular=True)
    with pytest.raises(TypeError):
        Transition(style=TransitionStyle.FLY, rectangular=1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        Transition(style=TransitionStyle.WIPE, direction=True)
    with pytest.raises(ValueError, match="direction"):
        Transition(style=TransitionStyle.SPLIT, direction=0)
    with pytest.raises(ValueError, match="direction"):
        Transition(style=TransitionStyle.GLITTER, direction=90)
    with pytest.raises(ValueError, match="direction"):
        Transition(style=TransitionStyle.PUSH, direction=180)
    with pytest.raises(ValueError, match="direction='none'"):
        Transition(style=TransitionStyle.FLY, direction="none")
    with pytest.raises(ValueError, match="direction='none'"):
        Transition(style=TransitionStyle.FLY, direction="none", scale=1.0)

    # Reading an external PDF with non-meaningful keys strips them cleanly;
    # replacing an indirect /Trans frees it when unreferenced.
    with pyropdf.Document() as doc:
        page = doc.pages.new((200, 100))
        trans_xref = doc.objects.add(
            {
                "Type": Name("Trans"),
                "Dm": Name("V"),  # Not meaningful on default /S /R
                "Di": Name("None"),  # Not meaningful on /S /R
                "D": 2,
            }
        )
        page.obj["Trans"] = doc.objects[trans_xref]
        assert page.transition == Transition(style=TransitionStyle.REPLACE, duration=2.0)

        page.transition = Transition(style=TransitionStyle.DISSOLVE)
        assert trans_xref not in doc.objects

        unknown_xref = doc.objects.add({"S": Name("VendorCustom")})
        page.obj["Trans"] = doc.objects[unknown_xref]
        assert page.transition is None
        del page.transition
        assert unknown_xref not in doc.objects


@pytest.mark.spec("presentations", "read", "interpret", "create", "edit", "preserve")
def test_navigation_steps_all_targets_coordinates_freeing_and_bounds(tmp_path: Path) -> None:
    path = tmp_path / "pres_steps.pdf"
    path.write_bytes(_pdf14_bytes())

    with pyropdf.open(path) as doc:
        doc.pages[1].rotation = 90
        slide = doc.pages[0]
        assert slide.navigation_steps == ()

        steps = (
            NavigationStep(
                next_action=TransitionAction(Transition(style=TransitionStyle.FADE, duration=0.5)),
                previous_action=NamedAction("PrevPage"),
                duration=3.0,
            ),
            NavigationStep(
                next_action=Destination(1, Point(25.0, 40.0), zoom=1.5),
                previous_action="https://example.com/prev",
            ),
            NavigationStep(
                next_action=RemoteDestination("deck2.pdf", page=2),
                previous_action=RemoteDestination("deck2.pdf", name="intro"),
            ),
            NavigationStep(
                next_action=Launch("notes.txt"),
                previous_action=Destination(0),
                duration=0.0,
            ),
        )
        slide.navigation_steps = steps
        assert slide.navigation_steps == steps
        assert doc.catalog["Version"] == "1.5"
        _assert_valid(doc)

        # Verify doubly-linked NavNode chain and indirectness.
        first_node = slide.obj["PresSteps"]
        assert first_node.is_indirect
        first_xref = first_node.xref
        assert first_xref is not None
        second_node = first_node["Next"]
        assert second_node.is_indirect
        assert second_node["Prev"] == first_node

        # Inserting a cover page shifts Destination page indexes automatically (F7).
        doc.pages.new((200, 100), index=0)
        shifted = slide.navigation_steps
        assert shifted[1].next_action == Destination(2, Point(25.0, 40.0), zoom=1.5)
        assert shifted[3].previous_action == Destination(1)

        doc.history.undo()
        assert slide.navigation_steps == steps

        doc.save(path, incremental=True)

    with pyropdf.open(path) as reopened:
        slide = reopened.pages[0]
        assert slide.navigation_steps == steps
        old_first_xref = slide.obj["PresSteps"].xref
        assert old_first_xref is not None

        # Replacing steps frees retired NavNode objects.
        replacement = (NavigationStep(next_action=NamedAction("NextPage"), duration=1.0),)
        slide.navigation_steps = replacement
        assert old_first_xref not in reopened.objects
        assert slide.navigation_steps == replacement
        _assert_valid(reopened)

        replacement_xref = slide.obj["PresSteps"].xref
        assert replacement_xref is not None
        del slide.navigation_steps
        assert slide.navigation_steps == ()
        assert "PresSteps" not in slide.obj
        assert replacement_xref not in reopened.objects
        _assert_valid(reopened)


def test_navigation_steps_cycle_limit_and_named_destinations() -> None:
    with pyropdf.Document() as doc:
        p0 = doc.pages.new((200, 100))
        p1 = doc.pages.new((200, 100))

        # Out-of-range destination page is refused without mutating the document.
        with pytest.raises(ValueError, match="out of range"):
            p0.navigation_steps = [NavigationStep(next_action=Destination(5))]
        assert "PresSteps" not in p0.obj

        # Legacy /Dests named destination resolves on /GoTo action in /NA.
        doc.catalog["Dests"] = {"target_slide": [p1.obj, Name("Fit")]}
        n1 = doc.objects.add(
            {
                "Type": Name("NavNode"),
                "NA": {"Type": Name("Action"), "S": Name("GoTo"), "D": Name("target_slide")},
            }
        )
        n2 = doc.objects.add({"Type": Name("NavNode"), "Dur": 2})
        # Introduce a cycle n1 -> n2 -> n1: iterative walk stops at seen node without recursion.
        doc.objects[n1]["Next"] = doc.objects[n2]
        doc.objects[n2]["Next"] = doc.objects[n1]
        p0.obj["PresSteps"] = doc.objects[n1]

        assert p0.navigation_steps == (
            NavigationStep(next_action=Destination(1)),
            NavigationStep(duration=2.0),
        )

    # Chain longer than 4,096 nodes raises LimitError rather than truncating silently (F5/F6).
    with pyropdf.Document() as doc:
        page = doc.pages.new((200, 100))
        with pytest.raises(ValueError, match="4096"):
            page.navigation_steps = [NavigationStep() for _ in range(4097)]

        with doc.transaction():
            xrefs = [doc.objects.add({"Type": Name("NavNode")}) for _ in range(4097)]
            for idx in range(4096):
                doc.objects[xrefs[idx]] = {"Type": Name("NavNode"), "Next": doc.objects[xrefs[idx + 1]]}
            page.obj["PresSteps"] = doc.objects[xrefs[0]]

        with pytest.raises(LimitError) as exc_info:
            _ = page.navigation_steps
        assert exc_info.value.code == "page_entry_count"


@pytest.mark.spec("transition-actions", "read", "interpret", "create", "edit", "preserve")
def test_transition_actions_on_links_outline_and_steps(tmp_path: Path) -> None:
    path = tmp_path / "trans_actions.pdf"
    path.write_bytes(_pdf14_bytes())

    wipe_action = TransitionAction(Transition(style=TransitionStyle.WIPE, duration=0.6, direction=0))
    fly_action = TransitionAction(
        Transition(
            style=TransitionStyle.FLY,
            duration=1.0,
            motion=TransitionMotion.OUTWARD,
            direction="none",
            scale=0.25,
            rectangular=True,
        )
    )

    with pytest.raises(TypeError):
        TransitionAction("not a transition")  # ty: ignore[invalid-argument-type]

    with pyropdf.open(path) as doc:
        page = doc.pages[0]

        # 1. Link.add, Link.target getter/setter, repr.
        link = page.links.add((10, 10, 80, 30), wipe_action)
        assert link.target == wipe_action
        assert "TransitionAction" in repr(link)
        assert doc.pages[0].links[0].target == wipe_action
        assert doc.catalog["Version"] == "1.5"
        _assert_valid(doc)

        doc.history.undo()
        assert len(page.links) == 0
        doc.history.redo()
        link = page.links[0]
        assert link.target == wipe_action

        link.target = fly_action
        assert link.target == fly_action
        _assert_valid(doc)

        # 2. OutlineEntry, doc.outline, OutlineItem.destination, to_entry, entries().
        entries = [
            OutlineEntry(1, "Intro", Destination(0), is_open=True),
            OutlineEntry(2, "Reveal", wipe_action, bold=True),
        ]
        doc.outline = entries
        assert doc.outline.entries() == entries
        child = doc.outline[0].children[0]
        assert child.destination == wipe_action
        assert child.page is None
        assert child.to_entry() == entries[1]
        _assert_valid(doc)

        child.destination = fly_action
        assert child.destination == fly_action
        assert doc.outline.entries()[1].destination == fly_action
        _assert_valid(doc)

        # 3. NavigationStep with TransitionAction.
        page.navigation_steps = [NavigationStep(next_action=wipe_action, previous_action=fly_action)]
        _assert_valid(doc)

        doc.save(path, incremental=True)

    full_path = tmp_path / "trans_actions_full.pdf"
    with pyropdf.open(path) as reopened:
        assert reopened.pages[0].links[0].target == fly_action
        assert reopened.outline[0].children[0].destination == fly_action
        assert reopened.pages[0].navigation_steps == (
            NavigationStep(next_action=wipe_action, previous_action=fly_action),
        )
        # Switch link and outline targets back to normal targets cleanly.
        reopened.pages[0].links[0].target = "https://example.com"
        reopened.outline[0].children[0].destination = Destination(1)
        _assert_valid(reopened)
        reopened.save(full_path)

    with pyropdf.open(full_path) as final:
        assert final.pages[0].links[0].target == "https://example.com"
        assert final.outline[0].children[0].destination == Destination(1)
        assert not final.check()


@pytest.mark.spec("presentations", "read", "interpret", "create", "edit", "preserve")
def test_page_thumbnail_write_flatten_and_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "thumbs.pdf"
    path.write_bytes(_pdf14_bytes())

    with pyropdf.open(path) as doc:
        page = doc.pages[0]
        assert page.thumbnail is None
        with pytest.raises(TypeError):
            page.thumbnail = "not a pixmap"  # ty: ignore[invalid-assignment]

        page.thumbnail = _solid_pixmap(16, 12, (20, 120, 220))
        decoded_rgb = page.thumbnail
        assert decoded_rgb is not None
        assert (decoded_rgb.width, decoded_rgb.height) == (16, 12)
        assert decoded_rgb.colorspace is Colorspace.RGB
        assert not decoded_rgb.alpha
        assert decoded_rgb.pixel(0, 0) == (20, 120, 220)
        first_thumb_xref = page.obj["Thumb"].xref
        assert first_thumb_xref is not None
        _assert_valid(doc)

        doc.history.undo()
        assert page.thumbnail is None
        doc.history.redo()
        assert page.thumbnail is not None

        # 50% alpha red onto white flattens alpha and frees the unreferenced previous stream.
        page.thumbnail = _solid_pixmap(8, 8, (255, 0, 0, 128), alpha=True)
        assert first_thumb_xref not in doc.objects
        decoded_flat = page.thumbnail
        assert decoded_flat is not None
        assert not decoded_flat.alpha
        r, g, b = decoded_flat.pixel(0, 0)
        assert (r, 126 <= g <= 128, 126 <= b <= 128) == (255, True, True)
        assert {"SMask", "Mask"}.isdisjoint(page.obj["Thumb"])

        doc.pages[1].thumbnail = _solid_pixmap(10, 8, (90,), gray=True)
        decoded_gray = doc.pages[1].thumbnail
        assert decoded_gray is not None
        assert decoded_gray.colorspace is Colorspace.GRAY
        assert decoded_gray.pixel(0, 0) == (90,)
        _assert_valid(doc)
        doc.save(path, incremental=True)

    with pyropdf.open(path) as reopened:
        t0, t1 = reopened.pages[0].thumbnail, reopened.pages[1].thumbnail
        assert t0 is not None
        assert t1 is not None
        assert (t0.width, t0.height, t1.pixel(0, 0)) == (8, 8, (90,))
        old_xref = reopened.pages[0].obj["Thumb"].xref
        assert old_xref is not None
        del reopened.pages[0].thumbnail
        assert reopened.pages[0].thumbnail is None
        assert old_xref not in reopened.objects
        assert not reopened.check()


def test_page_thumbnail_decodes_indexed_jpeg_subtype_omitted_and_enforces_limits() -> None:
    with pyropdf.Document() as doc:
        p_indexed = doc.pages.new((100, 100))
        p_jpeg = doc.pages.new((100, 100))
        p_no_subtype = doc.pages.new((100, 100))

        palette = bytes([10, 20, 30, 200, 210, 220])
        indexed_xref = doc.objects.add_stream(
            bytes([0, 1, 1, 0]),
            dictionary={
                "Type": Name("XObject"),
                "Subtype": Name("Image"),
                "Width": 2,
                "Height": 2,
                "ColorSpace": [Name("Indexed"), Name("DeviceRGB"), 1, palette],
                "BitsPerComponent": 8,
            },
        )
        p_indexed.obj["Thumb"] = doc.objects[indexed_xref]

        jpeg_bytes = _solid_pixmap(8, 8, (40, 140, 240)).to_bytes("jpeg", quality=100)
        jpeg_xref = doc.objects.add_stream(
            jpeg_bytes,
            dictionary={
                "Type": Name("XObject"),
                "Subtype": Name("Image"),
                "Width": 8,
                "Height": 8,
                "ColorSpace": Name("DeviceRGB"),
                "BitsPerComponent": 8,
            },
            filter=None,
        )
        doc.objects[jpeg_xref]["Filter"] = Name("DCTDecode")
        p_jpeg.obj["Thumb"] = doc.objects[jpeg_xref]

        # ISO 32000-2 12.3.4 makes /Subtype optional on /Thumb streams.
        no_sub_xref = doc.objects.add_stream(
            bytes([64, 192, 192, 64]),
            dictionary={
                "Width": 2,
                "Height": 2,
                "ColorSpace": Name("DeviceGray"),
                "BitsPerComponent": 8,
            },
        )
        p_no_subtype.obj["Thumb"] = doc.objects[no_sub_xref]

        _assert_valid(doc)
        decoded_idx = p_indexed.thumbnail
        assert decoded_idx is not None
        assert decoded_idx.pixel(0, 0) == (10, 20, 30)
        assert decoded_idx.pixel(1, 0) == (200, 210, 220)

        decoded_jpg = p_jpeg.thumbnail
        assert decoded_jpg is not None
        jr, jg, jb = decoded_jpg.pixel(0, 0)
        assert abs(jr - 40) <= 3
        assert abs(jg - 140) <= 3
        assert abs(jb - 240) <= 3

        decoded_nosub = p_no_subtype.thumbnail
        assert decoded_nosub is not None
        assert decoded_nosub.colorspace is Colorspace.GRAY
        assert decoded_nosub.pixel(0, 0) == (64,)
        assert decoded_nosub.pixel(1, 0) == (192,)

        raw_pdf = doc.to_bytes()

    with pyropdf.open(raw_pdf, limits=DocumentLimits(max_total_decompressed_size=32)) as limited:
        with pytest.raises(LimitError) as exc_info:
            _ = limited.pages[1].thumbnail
        assert exc_info.value.code == "total_decompressed_size"
