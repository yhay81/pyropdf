"""``Document.viewer_preferences``: the catalog's ``/ViewerPreferences``.

Ported from pylopdf's tests/test_viewer_preferences.py at ebb01d3c, extended
with undo/redo, indirect-object preservation, PDF/UA-1 and PDF-version
structural checks, and incremental and full save preservation.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from test_pages import build_raw_pdf
from test_preservation import snapshot as _snapshot

import pyropdf
from pyropdf import Duplex, PageBoundary, PageMode, PrintScaling, ReadingDirection, ViewerPreferences

ASSETS = Path(__file__).parent / "assets"
ENCRYPTED = ASSETS / "encrypted"
PDFUA = ASSETS / "pdfua"


def _pdf(preferences: str, *extra: str, header: str = "1.7") -> bytes:
    """Build a two-page PDF whose catalog holds ``preferences``; extra objects start at 5."""
    objects = {
        1: f"<< /Type /Catalog /Pages 2 0 R /ViewerPreferences {preferences} >>",
        2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << >> >>",
        4: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Resources << >> >>",
        **dict(enumerate(extra, start=5)),
    }
    raw = build_raw_pdf(objects)
    if header != "1.7":
        raw = raw.replace(b"%PDF-1.7\n", f"%PDF-{header}\n".encode(), 1)
    return raw


@pytest.mark.spec("viewer-preferences", "read", "interpret")
def test_every_entry_reads_as_a_typed_field() -> None:
    preferences = (
        "<< /HideToolbar true /HideMenubar false /HideWindowUI true /FitWindow true /CenterWindow false "
        "/DisplayDocTitle true /NonFullScreenPageMode /UseOutlines /Direction /R2L /ViewArea /CropBox "
        "/ViewClip /MediaBox /PrintArea /BleedBox /PrintClip /ArtBox /PrintScaling /None "
        "/Duplex /DuplexFlipLongEdge /PickTrayByPDFSize true /PrintPageRange [1 3 5 5] /NumCopies 2 "
        "/Enforce [/PrintScaling] >>"
    )
    doc = pyropdf.open(_pdf(preferences))

    assert doc.viewer_preferences == ViewerPreferences(
        hide_toolbar=True,
        hide_menubar=False,
        hide_window_ui=True,
        fit_window=True,
        center_window=False,
        display_title=True,
        non_full_screen_page_mode=PageMode.USE_OUTLINES,
        direction=ReadingDirection.R2L,
        view_area=PageBoundary.CROP,
        view_clip=PageBoundary.MEDIA,
        print_area=PageBoundary.BLEED,
        print_clip=PageBoundary.ART,
        print_scaling=PrintScaling.NONE,
        duplex=Duplex.FLIP_LONG_EDGE,
        pick_tray_by_pdf_size=True,
        print_page_ranges=((0, 2), (4, 4)),
        copies=2,
        enforce=("PrintScaling",),
    )
    assert not doc.check()


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.spec("viewer-preferences", "create", "edit", "preserve")
def test_assigning_a_record_replaces_the_dictionary_and_round_trips(*, incremental: bool) -> None:
    doc = pyropdf.open(_pdf("<< /HideToolbar true /Unknown 5 >>"))
    before = _snapshot(doc)
    catalog_xref = doc.catalog.xref
    preferences = ViewerPreferences(
        display_title=True,
        direction=ReadingDirection.R2L,
        duplex=Duplex.SIMPLEX,
        print_page_ranges=((0, 0), (2, 9)),
        copies=3,
        enforce=("PrintScaling",),
    )

    doc.viewer_preferences = preferences
    assert not doc.check()

    stored = doc.catalog["ViewerPreferences"]
    assert not stored.is_indirect
    assert sorted(stored) == ["Direction", "DisplayDocTitle", "Duplex", "Enforce", "NumCopies", "PrintPageRange"]
    assert isinstance(stored["Direction"], pyropdf.Name)
    assert isinstance(stored["Duplex"], pyropdf.Name)
    assert list(stored["PrintPageRange"]) == [1, 1, 3, 10]
    assert list(stored["Enforce"]) == [pyropdf.Name("PrintScaling")]
    assert all(isinstance(item, pyropdf.Name) for item in stored["Enforce"])

    saved = doc.to_bytes(incremental=incremental)
    reopened = pyropdf.open(saved)
    assert not reopened.check()
    assert reopened.viewer_preferences == preferences
    assert reopened.viewer_preferences is not None
    assert reopened.viewer_preferences.direction is ReadingDirection.R2L
    after = _snapshot(reopened)
    assert [xref for xref, value in before.items() if xref != catalog_xref and after.get(xref) != value] == []

    updated = dataclasses.replace(preferences, copies=None, print_page_ranges=())
    reopened.viewer_preferences = updated
    assert reopened.viewer_preferences == updated
    assert not reopened.check()
    del reopened.viewer_preferences
    assert reopened.viewer_preferences is None
    assert "ViewerPreferences" not in reopened.catalog
    assert not reopened.check()
    del reopened.viewer_preferences
    assert reopened.viewer_preferences is None


def test_indirect_viewer_preferences_keep_their_object_number_and_undo_atomically() -> None:
    doc = pyropdf.open(_pdf("5 0 R", "<< /HideToolbar true /Extra 9 >>"))
    before = _snapshot(doc)
    assert doc.viewer_preferences == ViewerPreferences(hide_toolbar=True)
    assert doc.catalog["ViewerPreferences"].xref == 5

    replacement = ViewerPreferences(
        display_title=True,
        print_scaling="None",  # ty: ignore[invalid-argument-type]
        print_page_ranges=[(0, 1)],  # ty: ignore[invalid-argument-type]
        enforce=["PrintScaling"],  # ty: ignore[invalid-argument-type]
    )
    assert replacement.print_scaling is PrintScaling.NONE
    assert replacement.print_page_ranges == ((0, 1),)
    assert replacement.enforce == ("PrintScaling",)

    doc.viewer_preferences = replacement
    stored = doc.catalog["ViewerPreferences"]
    assert stored.is_indirect
    assert stored.xref == 5
    assert "Extra" not in stored
    assert not doc.check()

    reopened = pyropdf.open(doc.to_bytes(incremental=True))
    after = _snapshot(reopened)
    assert [xref for xref, value in before.items() if xref != 5 and after.get(xref) != value] == []
    assert reopened.viewer_preferences == replacement
    assert reopened.catalog["ViewerPreferences"].xref == 5

    doc.history.undo()
    assert doc.viewer_preferences == ViewerPreferences(hide_toolbar=True)
    assert doc.catalog["ViewerPreferences"]["Extra"] == 9
    doc.history.redo()
    assert doc.viewer_preferences == replacement

    del doc.viewer_preferences
    assert doc.viewer_preferences is None
    doc.history.undo()
    assert doc.viewer_preferences == replacement


def test_pdfua_display_doc_title_and_old_pdf_versions_check_cleanly() -> None:
    ua = pyropdf.open(PDFUA / "pdfua-1.pdf")
    assert ua.viewer_preferences == ViewerPreferences(display_title=True)
    assert not ua.check()

    ua.viewer_preferences = ViewerPreferences(display_title=False)
    clauses = {defect.clause for defect in ua.check().defects}
    assert "PDF/UA-1/ViewerPreferences" in clauses

    ua.viewer_preferences = ViewerPreferences(display_title=True, fit_window=True)
    assert not ua.check()

    old = pyropdf.open(_pdf("<< >>", header="1.3"))
    old.viewer_preferences = ViewerPreferences(hide_toolbar=True)
    assert old.catalog.get("Version") is None
    assert not old.check()

    old.viewer_preferences = ViewerPreferences(duplex=Duplex.FLIP_SHORT_EDGE)
    assert old.catalog["Version"] == pyropdf.Name("1.7")
    assert not old.check()
    old.history.undo()
    assert old.catalog.get("Version") is None
    assert old.viewer_preferences == ViewerPreferences(hide_toolbar=True)


def test_an_empty_record_writes_an_empty_dictionary() -> None:
    doc = pyropdf.Document()

    assert doc.viewer_preferences is None
    doc.viewer_preferences = ViewerPreferences()

    assert doc.viewer_preferences == ViewerPreferences()
    assert dict(doc.catalog["ViewerPreferences"]) == {}
    assert not doc.check()


def test_malformed_entries_read_as_absent() -> None:
    preferences = (
        "<< /HideToolbar 1 /Direction /Up /NonFullScreenPageMode /FullScreen /PrintScaling (None) "
        "/PrintPageRange [1 2 3] /NumCopies 0 /Enforce [(PrintScaling)] /FitWindow true >>"
    )
    doc = pyropdf.open(_pdf(preferences))

    assert doc.viewer_preferences == ViewerPreferences(fit_window=True)
    assert pyropdf.open(_pdf("<< /Enforce [/PrintScaling 42] >>")).viewer_preferences == ViewerPreferences()
    assert pyropdf.open(_pdf("<< /Enforce [] >>")).viewer_preferences == ViewerPreferences(enforce=())
    assert pyropdf.open(_pdf("[/FitWindow]")).viewer_preferences is None
    assert pyropdf.open(_pdf("<< /PrintPageRange [3 1] >>")).viewer_preferences == ViewerPreferences()


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"hide_toolbar": 1}, TypeError),
        ({"direction": "up"}, ValueError),
        ({"non_full_screen_page_mode": PageMode.FULL_SCREEN}, ValueError),
        ({"print_page_ranges": [(2, 1)]}, ValueError),
        ({"print_page_ranges": [(-1, 1)]}, ValueError),
        ({"print_page_ranges": [(0, 1, 2)]}, ValueError),
        ({"print_page_ranges": [(0, 0)] * 4097}, ValueError),
        ({"print_page_ranges": "12"}, TypeError),
        ({"print_page_ranges": ["12"]}, TypeError),
        ({"copies": 0}, ValueError),
        ({"copies": True}, ValueError),
        ({"enforce": "PrintScaling"}, TypeError),
        ({"enforce": ("Print Scaling",)}, ValueError),
        ({"enforce": ("PrintScaling",) * 65}, ValueError),
    ],
)
def test_fields_are_validated(fields: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        ViewerPreferences(**fields)  # ty: ignore[invalid-argument-type]


def test_assignments_need_a_record_and_respect_closed_and_locked_state() -> None:
    doc = pyropdf.open(_pdf("<< >>"))
    with pytest.raises(TypeError, match="ViewerPreferences"):
        doc.viewer_preferences = {"fit_window": True}  # ty: ignore[invalid-assignment]
    assert doc.viewer_preferences == ViewerPreferences()
    assert not doc.history.can_undo

    locked = pyropdf.open(ENCRYPTED / "user-aes-256.pdf")
    with pytest.raises(pyropdf.EncryptedDocumentError):
        _ = locked.viewer_preferences
    with pytest.raises(pyropdf.EncryptedDocumentError):
        locked.viewer_preferences = ViewerPreferences(fit_window=True)
    with pytest.raises(pyropdf.EncryptedDocumentError):
        del locked.viewer_preferences

    doc.close()
    with pytest.raises(pyropdf.DocumentClosedError):
        _ = doc.viewer_preferences
    with pytest.raises(pyropdf.DocumentClosedError):
        doc.viewer_preferences = ViewerPreferences()
    with pytest.raises(pyropdf.DocumentClosedError):
        del doc.viewer_preferences
