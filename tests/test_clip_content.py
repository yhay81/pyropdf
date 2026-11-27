"""Clip independent direct text and reject ambiguous painting without changing the live PDF."""

from __future__ import annotations

import io
import shutil
from dataclasses import replace
from typing import TYPE_CHECKING

import pikepdf
import pytest
from test_canvas_round_trip import _poppler_page
from test_replace_text import HELVETICA, build_raw_pdf, content_pdf, stream

import pyropdf

if TYPE_CHECKING:
    from pathlib import Path


KEEP = "BT /F1 18 Tf 1 0 0 1 40 720 Tm [(Ke) 15 (ep)] TJ ET"
DROP = "BT /F1 18 Tf 80 520 Td (Gone) Tj ET"
BOTH = f"q 0 g {DROP} {KEEP} Q"


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_direct_clip_detaches_shared_content_and_is_one_undo_step(rotation: int) -> None:
    with pyropdf.open(content_pdf(BOTH, KEEP)) as doc:
        page, sibling = doc.pages
        sibling.obj["Contents"] = page.obj["Contents"]
        page.rotation = rotation
        words = {word.text: word.bbox for word in page.words()}
        before = doc.history.steps
        saved_bounds = page.rect

        assert page.clip_to_rect(words["Keep"]) == 4
        assert doc.history.steps == (*before, "clip page content")
        assert page.clip_to_rect(words["Keep"]) == 0
        assert doc.history.steps == (*before, "clip page content")
        assert page.rect == saved_bounds
        assert [(word.text, word.bbox) for word in page.words()] == [("Keep", words["Keep"])]
        assert {word.text for word in sibling.words()} == {"Gone", "Keep"}
        output = doc.to_bytes()
        with pyropdf.open(output) as reopened, pikepdf.open(io.BytesIO(output)) as parsed:
            assert reopened.pages[0].words()[0].bbox == words["Keep"]
            shows = [op for op in pikepdf.parse_content_stream(parsed.pages[0]) if str(op.operator) in {"Tj", "TJ"}]
            assert len(shows) == 1
            assert list(shows[0].operands[0]) == [pikepdf.String("Ke"), 15, pikepdf.String("ep")]
            assert parsed.pages[0].Contents.objgen != parsed.pages[1].Contents.objgen
        doc.history.undo()
        assert {word.text for word in page.words()} == {"Gone", "Keep"}
        assert page.obj["Contents"].xref == sibling.obj["Contents"].xref
        doc.history.redo()
        assert page.text().strip() == "Keep"


@pytest.mark.parametrize("tail", ["\n% End of text", "\r% first\r% second\n\x00\t% last\r\n"])
def test_trailing_pdf_comments_survive_the_rewrite(tail: str) -> None:
    with pyropdf.open(content_pdf(BOTH + tail)) as doc:
        page = doc.pages[0]
        keep = next(word for word in page.words() if word.text == "Keep")
        assert page.clip_to_rect(keep.bbox) == 4
        with pyropdf.open(doc.to_bytes()) as saved:
            assert saved.pages[0].words() == [keep]
            assert saved.pages[0].operations.tail == tail.encode()


def test_annotation_text_is_independent_of_an_identical_removed_page_show() -> None:
    with pyropdf.open(content_pdf(BOTH)) as doc:
        page = doc.pages[0]
        keep = next(word for word in page.words() if word.text == "Keep")
        note = page.annotations.add(pyropdf.FreeText((30, 30, 115, 92), "Gone"))
        appearance = note.obj["AP"]["N"].read()
        assert [word.text for word in page.words()].count("Gone") == 2
        assert page.clip_to_rect(keep.bbox) == 4
        data = doc.to_bytes()
        with pyropdf.open(data) as saved, pikepdf.open(io.BytesIO(data)) as parsed:
            assert [word.text for word in saved.pages[0].words()] == ["Gone", "Keep"]
            assert parsed.pages[0].Annots[0].AP.N.read_bytes() == appearance
            assert b"Gone" not in parsed.pages[0].Contents.read_bytes()


def test_unlocated_direct_glyph_still_refuses_atomically(monkeypatch: pytest.MonkeyPatch) -> None:
    with pyropdf.open(content_pdf(BOTH)) as doc:
        page = doc.pages[0]
        keep = next(word for word in page.words() if word.text == "Keep")
        chars = [replace(c, stream=None, operation=None) if c.text == "G" else c for c in page.chars()]
        monkeypatch.setattr(pyropdf.Page, "chars", lambda _page, **_options: chars)
        original = doc.to_bytes()
        with pytest.raises(pyropdf.PdfError, match="located glyphs"):
            page.clip_to_rect(keep.bbox)
        assert not doc.history.steps
        assert doc.to_bytes() == original


@pytest.mark.skipif(not shutil.which("pdftoppm") or not shutil.which("pdftotext"), reason="Poppler is unavailable")
@pytest.mark.parametrize("rotation", [0, 90])
def test_poppler_full_save_matches_separate_inside_only_control(tmp_path: Path, rotation: int) -> None:
    source, output, control = (tmp_path / name for name in ("source.pdf", "result.pdf", "control.pdf"))
    with pyropdf.open(content_pdf(BOTH)) as doc, pyropdf.open(content_pdf(KEEP)) as expected:
        page = doc.pages[0]
        page.rotation = expected.pages[0].rotation = rotation
        doc.save(source)
        expected.save(control)
        assert page.clip_to_rect(expected.pages[0].words()[0].bbox) == 4
        doc.save(output)
    original_text, original_pixels = _poppler_page(source, 1)
    text, pixels = _poppler_page(output, 1)
    assert "Gone" in original_text
    assert "Keep" in original_text
    assert text == "Keep"
    assert (text, pixels) == _poppler_page(control, 1)
    assert pixels != original_pixels


def test_reset_from_the_line_matrix_and_invisible_text_are_supported() -> None:
    program = "BT /F1 18 Tf 20 550 Td 3 Tr (Invisible) Tj 0 Tr 30 150 Td (Keep) Tj ET"
    with pyropdf.open(content_pdf(program)) as doc:
        page = doc.pages[0]
        keep = next(word for word in page.words() if word.text == "Keep")
        assert page.clip_to_rect(keep.bbox) == len("Invisible")
        assert page.words() == [keep]
        assert b"Invisible" not in page.operations.to_bytes()


@pytest.mark.parametrize("intervening", ["", "q q 0 g Q Q"])
def test_selected_font_survives_between_positioned_text_objects(intervening: str) -> None:
    program = f"BT /F1 18 Tf 40 450 Td (Away) Tj ET {intervening} BT 1 0 0 1 65 700 Tm (Keep) Tj ET"
    with pyropdf.open(content_pdf(program)) as doc:
        page = doc.pages[0]
        keep = next(word for word in page.words() if word.text == "Keep")
        assert page.clip_to_rect(keep.bbox) == 4
        with pyropdf.open(doc.to_bytes()) as saved:
            assert saved.pages[0].words() == [keep]


def test_restoring_graphics_state_discards_a_font_selected_only_inside_it() -> None:
    program = "q BT /F1 18 Tf 40 450 Td (Away) Tj ET Q BT 1 0 0 1 65 700 Tm (Keep) Tj ET"
    with pyropdf.open(content_pdf(program)) as doc:
        page = doc.pages[0]
        before = doc.to_bytes()
        with pytest.raises(pyropdf.PdfError, match="text operation"):
            page.clip_to_rect(page.rect)
        assert not doc.history.steps
        assert doc.to_bytes() == before


@pytest.mark.parametrize("incremental", [False, True])
def test_clip_uses_the_joined_operation_index_across_shared_streams(*, incremental: bool) -> None:
    with pyropdf.open(content_pdf(DROP, KEEP)) as doc:
        page, sibling = doc.pages
        page.obj["Contents"] = [page.obj["Contents"], sibling.obj["Contents"]]
        keep = next(word for word in page.words() if word.text == "Keep")
        assert page.clip_to_rect(keep.bbox) == 4
        data = doc.to_bytes(incremental=incremental)
        with pyropdf.open(data) as saved, pikepdf.open(io.BytesIO(data)) as parsed:
            assert saved.pages[0].words() == saved.pages[1].words() == [keep]
            assert parsed.pages[0].Contents.objgen != parsed.pages[1].Contents.objgen


def test_clip_reserves_the_retained_stream_before_editing() -> None:
    data = content_pdf(DROP + " " * 32768 + KEEP)
    with pyropdf.open(data) as reference:
        keep = next(word for word in reference.pages[0].words() if word.text == "Keep")
    with pyropdf.open(data, limits=pyropdf.DocumentLimits(max_memory_size=300_000)) as doc:
        page = doc.pages[0]
        before = doc.to_bytes()
        assert page.clip_to_rect(page.rect) == 0
        with pytest.raises(pyropdf.LimitError) as exc:
            page.clip_to_rect(keep.bbox)
        assert exc.value.code == "memory_size"
        assert not doc.history.steps
        assert doc.to_bytes() == before
    with pyropdf.open(data) as doc:
        assert doc.pages[0].clip_to_rect(keep.bbox) == 4
        assert doc.pages[0].words() == [keep]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("0 0 50 50 re f", "graphics operation"),
        ("q BI /W 1 /H 1 /BPC 1 /IM true ID x EI Q", "graphics operation"),
        ("BT /F1 18 Tf 50 700 Td (First) Tj (Second) Tj ET", "dependent text operation"),
        ("BT /F1 18 Tf 7 Tr (Clip) Tj ET", "text operation"),
        ("q", "unbalanced"),
        (" ".join(["1"] * 34) + " cm", "unparsed content"),
        ("% ignored\r34", "unparsed content"),
    ],
)
def test_unsupported_paint_refuses_every_edit_including_earlier_outside_text(extra: str, message: str) -> None:
    with pyropdf.open(content_pdf(f"{DROP} {KEEP} {extra}")) as doc:
        page = doc.pages[0]
        saved = doc.to_bytes()
        with doc.transaction("outer"):
            doc.catalog["OuterMarker"] = 5
            with pytest.raises(pyropdf.PdfError, match=message):
                page.clip_to_rect((0, 0, 300, 120))
            assert page.obj["Contents"].xref == 5
            assert doc.catalog["OuterMarker"] == 5
        doc.history.undo()
        assert doc.to_bytes() == saved


def test_partial_show_and_composite_font_refuse_without_history() -> None:
    with pyropdf.open(content_pdf(BOTH)) as doc:
        page = doc.pages[0]
        char = next(char for char in page.chars() if char.text == "K")
        with pytest.raises(pyropdf.PdfError, match="splits"):
            page.clip_to_rect(char.bbox)
        assert not doc.history.steps
        with doc.transaction():
            doc.objects[3]["Subtype"] = pyropdf.Name("Type0")
            with pytest.raises(pyropdf.PdfError, match="single byte"):
                page.clip_to_rect(page.rect)
            assert page.obj["Contents"].xref == 5


def test_nested_shared_form_is_preserved_at_both_placements_on_refusal() -> None:
    data = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [4 0 R 8 0 R] /Count 2 /MediaBox [0 0 300 300] >>",
            3: HELVETICA,
            4: "<< /Type /Page /Parent 2 0 R /Resources << /XObject << /Outer 6 0 R >> >> /Contents 5 0 R >>",
            5: stream("q 1 0 0 1 20 30 cm /Outer Do Q q 1 0 0 1 150 180 cm /Outer Do Q"),
            6: stream(
                "/Inner Do",
                "/Type /XObject /Subtype /Form /BBox [0 0 80 80] /Resources << /XObject << /Inner 7 0 R >> >>",
            ),
            7: stream(
                "BT /F1 14 Tf 3 24 Td (Echo) Tj ET",
                "/Type /XObject /Subtype /Form /BBox [0 0 80 80] /Resources << /Font << /F1 3 0 R >> >>",
            ),
            8: "<< /Type /Page /Parent 2 0 R /Resources << /XObject << /Outer 6 0 R >> >> /Contents 5 0 R >>",
        }
    )
    with pyropdf.open(data) as doc, pikepdf.open(io.BytesIO(data)) as other:
        before = doc.to_bytes()
        assert other.pages[0].Resources.XObject.Outer.Resources.XObject.Inner.objgen == (7, 0)
        assert [str(op.operator) for op in pikepdf.parse_content_stream(other.pages[0])].count("Do") == 2
        page = doc.pages[0]
        first, second = page.words()
        assert first.text == second.text == "Echo"
        assert first.bbox != second.bbox
        with pytest.raises(pyropdf.PdfError, match="Do"):
            page.clip_to_rect(first.bbox)
        assert not doc.history.steps
        assert doc.pages[1].words() == page.words() == [first, second]
        assert doc.to_bytes() == before


def test_limit_or_invalid_rect_cannot_publish_a_partial_rewrite() -> None:
    data = content_pdf(BOTH)
    limits = pyropdf.DocumentLimits(max_memory_size=10_000)
    with pyropdf.open(data, limits=limits) as doc:
        before = doc.to_bytes()
        with pytest.raises(pyropdf.LimitError) as exc:
            doc.pages[0].clip_to_rect((0, 0, 300, 120))
        assert exc.value.code == "memory_size"
        assert not doc.history.steps
        assert doc.to_bytes() == before
    with pyropdf.open(data) as doc:
        for rect in ((float("nan"), 0, 1, 1), (0, 0, 0, 1), (1000, 1000, 1010, 1010)):
            with pytest.raises(ValueError, match="rect"):
                doc.pages[0].clip_to_rect(rect)
        assert not doc.history.steps
