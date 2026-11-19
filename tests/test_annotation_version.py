"""PDF version legality and conformance claim reporting for annotations (#797).

Covers old-version (PDF 1.3 and 1.4) ``Square`` creation and appearance edits,
version-gated automatic entries (``/CreationDate``, ``/LE``, ``/Q``, ``/UF``,
``/BS`` versus ``/Border``), PDF/A-1 Arlington 1.4 conformance, explicit PDF/A
claim breakage reporting, and preservation of unrelated objects and pages.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_preservation import snapshot as _snapshot

import pyropdf
from pyropdf import (
    Array,
    BlendMode,
    Border,
    Color,
    Dictionary,
    FileAttachment,
    FreeText,
    LineAnnotation,
    Name,
    Polygon,
    Square,
)

ASSETS = Path(__file__).resolve().parent / "assets"
PDFA = ASSETS / "pdfa"
REAL_WORLD = ASSETS / "real_world"

OLD_CORPUS = (
    "nics-background-checks-2015-11.pdf",
    "patent-us223898.pdf",
    "senate-expenditures.pdf",
    "wdl6812-manuscript.pdf",
)


def _minimal_pdf(header: str) -> bytes:
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << >> >>",
        4: (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            "/Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>"
        ),
        5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        6: "<< /Length 36 >>\nstream\nBT /F1 12 Tf 20 150 Td (Keep me) Tj ET\nendstream",
    }
    out = bytearray(f"%PDF-{header}\n".encode())
    offsets: dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n{objects[num]}\nendobj\n".encode()
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for num in range(1, size):
        out += f"{offsets[num]:010} 00000 n \n".encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _defects(data: bytes) -> set[tuple[int | None, str, str, str]]:
    with pyropdf.open(data) as doc:
        return {(d.xref, d.path, d.clause, d.kind) for d in doc.check().defects}


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.parametrize(
    ("header", "expected_catalog_version", "expects_creation_date"),
    [
        ("1.3", "1.4", False),
        ("1.4", None, False),
        ("1.5", None, True),
        ("1.7", None, True),
    ],
)
def test_square_creation_and_appearance_edits_emit_version_legal_entries(
    header: str,
    expected_catalog_version: str | None,
    *,
    expects_creation_date: bool,
    incremental: bool,
) -> None:
    source = _minimal_pdf(header)
    assert _defects(source) == set()

    with pyropdf.open(source) as doc:
        before_objects = _snapshot(doc)
        untouched_page = (doc.pages[1].text(), bytes(doc.pages[1].pixmap(dpi=72)))
        touched = {doc.pages[0].obj.xref}
        if expected_catalog_version is not None:
            touched.add(doc.catalog.xref)

        annot = doc.pages[0].annotations.add(
            Square((10, 10, 40, 40), interior_color=Color(0.25, 0.5, 0.75), contents="initial")
        )
        annot.name = "version-regression"
        created_bytes = doc.to_bytes(incremental=incremental)

    assert _defects(created_bytes) == set()
    with pyropdf.open(created_bytes) as reopened:
        assert reopened.catalog.get("Version") == expected_catalog_version
        item = reopened.pages[0].annotations[0]
        assert item.name == "version-regression"
        assert item.contents == "initial"
        assert item.interior_color == Color(0.25, 0.5, 0.75)
        assert ("CreationDate" in item.obj) is expects_creation_date
        after_objects = _snapshot(reopened)
        assert [
            xref for xref, val in before_objects.items() if xref not in touched and after_objects.get(xref) != val
        ] == []
        assert (reopened.pages[1].text(), bytes(reopened.pages[1].pixmap(dpi=72))) == untouched_page

        # Appearance-redrawing property edits on the reopened document.
        edit_baseline = reopened.to_bytes(incremental=True)

    with pyropdf.open(edit_baseline) as doc2:
        before2 = _snapshot(doc2)
        target = doc2.pages[0].annotations[0]
        ap_xref = target.obj["AP"]["N"].xref
        assert ap_xref is not None
        edit_touched = {target.xref, ap_xref}
        with doc2.transaction():
            target.contents = "initial edited"
            target.interior_color = Color(0.75, 0.5, 0.25)
            target.border = Border(2.0)
        edited_bytes = doc2.to_bytes(incremental=incremental)

    assert _defects(edited_bytes) == set()
    with pyropdf.open(edited_bytes) as reopened2:
        edited = reopened2.pages[0].annotations[0]
        assert edited.contents == "initial edited"
        assert edited.interior_color == Color(0.75, 0.5, 0.25)
        assert edited.border is not None
        assert edited.border.width == pytest.approx(2.0)
        assert ("CreationDate" in edited.obj) is expects_creation_date
        after2 = _snapshot(reopened2)
        assert [xref for xref, val in before2.items() if xref not in edit_touched and after2.get(xref) != val] == []
        assert (reopened2.pages[1].text(), bytes(reopened2.pages[1].pixmap(dpi=72))) == untouched_page


def test_plain_pdf13_square_keeps_catalog_untouched() -> None:
    source = _minimal_pdf("1.3")
    with pyropdf.open(source) as doc:
        before = _snapshot(doc)
        page_xref = doc.pages[0].obj.xref
        annot = doc.pages[0].annotations.add(Square((10, 10, 30, 30), contents="plain"))
        assert "CreationDate" not in annot.obj
        assert "IC" not in annot.obj
        assert "NM" not in annot.obj
        assert doc.catalog.get("Version") is None
        saved = doc.to_bytes(incremental=True)

    assert _defects(saved) == set()
    with pyropdf.open(saved) as reopened:
        after = _snapshot(reopened)
        assert [xref for xref, val in before.items() if xref != page_xref and after.get(xref) != val] == []


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.parametrize("name", OLD_CORPUS)
def test_corpus_old_version_square_add_and_edit_add_no_structural_defects(name: str, *, incremental: bool) -> None:
    source = (REAL_WORLD / name).read_bytes()
    baseline_defects = _defects(source)

    with pyropdf.open(source) as doc:
        annot = doc.pages[0].annotations.add(
            Square((10, 10, 30, 30), interior_color=Color(0.25, 0.5, 0.75), contents="pyropdf-preservation")
        )
        annot.name = "pyropdf-preservation"
        added = doc.to_bytes(incremental=incremental)

    assert _defects(added) - baseline_defects == set()

    prepared = pyropdf.open(source)
    added_handle = prepared.pages[0].annotations.add(
        Square((10, 10, 30, 30), interior_color=Color(0.25, 0.5, 0.75), contents="pyropdf-preservation")
    )
    added_handle.name = "pyropdf-preservation"
    prepared_bytes = prepared.to_bytes(incremental=True)
    prepared.close()

    with pyropdf.open(prepared_bytes) as doc2:
        item = next(entry for entry in doc2.pages[0].annotations if entry.name == "pyropdf-preservation")
        with doc2.transaction():
            item.contents = "pyropdf-preservation edited"
            item.interior_color = Color(0.75, 0.5, 0.25)
        edited = doc2.to_bytes(incremental=incremental)

    assert _defects(edited) - _defects(prepared_bytes) == set()


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_pdfa1_square_preserves_conformance_and_version(*, incremental: bool) -> None:
    source = (PDFA / "pdfa-1b.pdf").read_bytes()
    with pyropdf.open(source) as doc:
        before = _snapshot(doc)
        page_xref = doc.pages[0].obj.xref
        items = doc.pages[0].obj.get("Annots")
        touched = {page_xref}
        if isinstance(items, Array) and items.xref is not None:
            touched.add(items.xref)

        annot = doc.pages[0].annotations.add(
            Square((10, 10, 30, 30), interior_color=Color(0.25, 0.5, 0.75), contents="pdfa1-ok")
        )
        annot.name = "pdfa1-square"
        assert doc.catalog.get("Version") is None
        assert "CreationDate" not in annot.obj
        assert doc.changes(incremental=incremental).claims == ()
        saved = doc.to_bytes(incremental=incremental)

    assert _defects(saved) == set()
    with pyropdf.open(saved) as reopened:
        assert reopened.catalog.get("Version") is None
        after = _snapshot(reopened)
        assert [xref for xref, val in before.items() if xref not in touched and after.get(xref) != val] == []


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_pdfa1_and_pdfa2_report_explicit_version_or_subtype_conformance_breaks(*, incremental: bool) -> None:
    # Explicit PDF 1.5 entry (/Subj) on PDF/A-1 elevates /Version to 1.5 and reports PDF/A-1B broken.
    with pyropdf.open(PDFA / "pdfa-1b.pdf") as doc:
        annot = doc.pages[0].annotations.add(Square((10, 10, 30, 30)))
        annot.subject = "Needs PDF 1.5"
        assert doc.catalog.get("Version") == Name("1.5")
        claims = doc.changes(incremental=incremental).claims
        assert len(claims) == 1
        assert claims[0].claim == "PDF/A-1B"
        assert "PDF 1.4" in claims[0].reason
        assert set(claims[0].objects) >= {doc.catalog.xref, annot.xref}
        assert _defects(doc.to_bytes(incremental=incremental)) == set()

    # Forbidden subtype (Polygon) on PDF/A-1 reports both forbidden subtype and >1.4 catalog version.
    with pyropdf.open(PDFA / "pdfa-1b.pdf") as doc:
        poly = doc.pages[0].annotations.add(Polygon([(10, 10), (40, 10), (25, 35)]))
        claims = doc.changes(incremental=incremental).claims
        assert {c.claim for c in claims} == {"PDF/A-1B"}
        reasons = {c.reason: set(c.objects) for c in claims}
        assert any("annotation type that PDF/A-1 forbids" in r and poly.xref in objs for r, objs in reasons.items())
        assert _defects(doc.to_bytes(incremental=incremental)) == set()

    # Explicit PDF 2.0 entry (/Lang or /BM) on PDF/A-2 elevates /Version to 2.0 and reports PDF/A-2B broken.
    with pyropdf.open(PDFA / "pdfa-2b.pdf") as doc:
        annot2 = doc.pages[0].annotations.add(Square((10, 10, 30, 30), blend_mode=BlendMode.MULTIPLY))
        annot2.language = "en"
        assert doc.catalog.get("Version") == Name("2.0")
        claims2 = doc.changes(incremental=incremental).claims
        assert len(claims2) == 1
        assert claims2[0].claim == "PDF/A-2B"
        assert "PDF 1.7" in claims2[0].reason
        assert set(claims2[0].objects) == {doc.catalog.xref, annot2.xref}
        assert _defects(doc.to_bytes(incremental=incremental)) == set()


def test_version_sensitive_optional_entries_across_other_annotation_types() -> None:
    # FreeText on PDF 1.4 uses /Border instead of /BS (PDF 1.7) and omits default /Q when PDF 1.3.
    with pyropdf.open(_minimal_pdf("1.3")) as doc:
        ft = doc.pages[0].annotations.add(FreeText((10, 10, 100, 40), "hello", border=Border(1.0)))
        assert "BS" not in ft.obj
        assert isinstance(ft.obj.get("Border"), Array)
        assert "Q" not in ft.obj
        assert "CreationDate" not in ft.obj
        assert _defects(doc.to_bytes()) == set()

    # LineAnnotation with default ("None", "None") endings omits /LE on PDF 1.3.
    with pyropdf.open(_minimal_pdf("1.3")) as doc:
        line = doc.pages[0].annotations.add(LineAnnotation((10, 10), (50, 50)))
        assert "LE" not in line.obj
        assert _defects(doc.to_bytes()) == set()

    # FileAttachment on PDF 1.4 omits /UF (PDF 1.7) on creation and file edits.
    with pyropdf.open(_minimal_pdf("1.4")) as doc:
        att = doc.pages[0].annotations.add(FileAttachment((10, 10), b"abc", filename="note.txt"))
        fs = att.obj["FS"]
        assert isinstance(fs, Dictionary)
        assert "UF" not in fs
        assert "UF" not in fs["EF"]
        assert att.file is not None
        att.file.filename = "renamed.txt"
        att.file.write(b"xyz")
        assert "UF" not in att.obj["FS"]
        assert "UF" not in att.obj["FS"]["EF"]
        assert doc.catalog.get("Version") is None
        assert _defects(doc.to_bytes()) == set()
