"""End-to-end tests for the five non-upstream-blocked canonical 1.0 workflows (#257, #1148).

Each workflow exercises full and incremental saves where applicable, checks that
untouched pages and objects survive unchanged, and validates every saved
deliverable independently with ``pikepdf`` as well as ``doc.check()``.

Full content-removing redaction (#362) and retrieval provenance (#853) remain
tracked separately while waiting on upstream engine releases.
"""

from __future__ import annotations

import io
import struct
from typing import TYPE_CHECKING

import pikepdf
import pytest

import pyropdf
from pyropdf import (
    AnnotationType,
    CheckBox,
    Color,
    ComboBox,
    Destination,
    Font,
    Matrix,
    Name,
    Note,
    OutlineEntry,
    Pixmap,
    Rect,
    RedactionMark,
    Stamp,
    StampIcon,
    Stroke,
    TextField,
)

if TYPE_CHECKING:
    from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SOI = b"\xff\xd8\xff"


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Validate a PNG byte stream and return its decoded ``(width, height)``."""
    assert data.startswith(PNG_SIGNATURE)
    assert data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    decoded = Pixmap.from_bytes(data)
    assert (decoded.width, decoded.height) == (width, height)
    return width, height


def _pixel_rgb(pixmap: Pixmap, x: int, y: int) -> tuple[int, int, int]:
    """Read the RGB triple at integer pixel ``(x, y)``."""
    offset = y * pixmap.stride + x * pixmap.channels
    r, g, b = bytes(pixmap)[offset : offset + 3]
    return r, g, b


def _region_diff(before: Pixmap, after: Pixmap, rect: tuple[int, int, int, int], tolerance: int = 8) -> int:
    """Count RGB samples inside ``rect`` that differ by more than ``tolerance``."""
    left = before.crop(rect)
    right = after.crop(rect)
    assert (left.width, left.height, left.channels) == (right.width, right.height, right.channels)
    return sum(abs(a - b) > tolerance for a, b in zip(bytes(left), bytes(right), strict=True))


def _page_raw_content(doc: pyropdf.Document, page_index: int) -> bytes:
    """Return the raw undecoded content bytes of ``doc.pages[page_index]``."""
    contents = doc.pages[page_index].obj.get("Contents")
    assert contents is not None
    if isinstance(contents, pyropdf.Stream):
        return contents.read(decode=False, max_size=None)
    assert isinstance(contents, pyropdf.Array)
    return b"".join(part.read(decode=False, max_size=None) for part in contents if isinstance(part, pyropdf.Stream))


def _pikepdf_page_raw_content(pdf: pikepdf.Pdf, page_index: int) -> bytes:
    """Return the raw undecoded content bytes of ``pdf.pages[page_index]`` via pikepdf."""
    contents = pdf.pages[page_index].Contents
    if isinstance(contents, pikepdf.Stream):
        return bytes(contents.read_raw_bytes())
    return b"".join(bytes(part.read_raw_bytes()) for part in contents)


def _defect_keys(data: bytes) -> set[tuple[str, str, str]]:
    """Return the ``(path, clause, kind)`` structural defects reported by ``doc.check()``."""
    with pyropdf.open(data) as doc:
        report = doc.check()
        assert not report.truncated
        return {(defect.path, defect.clause, defect.kind) for defect in report.defects}


def _assert_clean_pdf(
    data: bytes,
    expected_pages: int,
    *,
    baseline_defects: set[tuple[str, str, str]] | None = None,
) -> None:
    """Verify structural integrity via both ``pyropdf.Document.check`` and ``pikepdf``."""
    with pyropdf.open(data) as doc:
        assert len(doc.pages) == expected_pages
    found = _defect_keys(data)
    allowed = baseline_defects or set()
    assert found - allowed == set()
    with pikepdf.open(io.BytesIO(data)) as pdf:
        assert len(pdf.pages) == expected_pages
        assert pdf.check_pdf_syntax() == []


def _solid_image_bytes(width: int, height: int, color: Color, fmt: str) -> bytes:
    """Render a small solid-colour page and encode it as ``fmt`` (``png`` or ``jpeg``)."""
    with pyropdf.Document() as doc:
        page = doc.pages.new((width, height))
        with page.draw() as canvas:
            canvas.rect((0, 0, width, height), fill=color)
        return page.pixmap(dpi=72).to_bytes(fmt, quality=95) if fmt == "jpeg" else page.pixmap(dpi=72).to_bytes(fmt)


def _build_images_fixture() -> tuple[bytes, bytes]:
    """Build a 2-page PDF with landmarks, JPEG and PNG images, and a rotated untouched page."""
    jpeg_bytes = _solid_image_bytes(16, 12, Color(0.9, 0.1, 0.1), "jpeg")
    png_bytes = _solid_image_bytes(12, 10, Color(0.1, 0.8, 0.2), "png")
    with pyropdf.Document() as doc:
        p0 = doc.pages.new((300, 200))
        with p0.draw() as canvas:
            canvas.rect((20, 20, 100, 80), fill=Color(0.1, 0.2, 0.9))
            canvas.text("Cover landmark", (20, 120), size=14)
            canvas.image((140, 20, 220, 80), jpeg_bytes)
            canvas.image((140, 100, 212, 160), png_bytes)

        p1 = doc.pages.new((200, 300))
        p1.rotation = 90
        p1.duration = 12.375
        with p1.draw() as canvas:
            canvas.rect((20, 20, 80, 60), fill=Color(0.95, 0.85, 0.1))
            canvas.text("Untouched rotated page", (20, 110), size=12)
        return doc.to_bytes(), jpeg_bytes


def _assert_thumbnails_and_embedded_images(doc: pyropdf.Document, jpeg_bytes: bytes) -> None:
    """Verify 36 dpi / fitted thumbnails and both stored JPEG and decoded PNG image extraction."""
    thumbs = doc.render(dpi=36, format="png")
    assert len(thumbs) == 2
    assert _png_dimensions(thumbs[0]) == (150, 100)
    assert _png_dimensions(thumbs[1]) == (150, 100)  # 200x300 rotated 90 deg -> 300x200 display -> 150x100
    r0, g0, b0 = _pixel_rgb(Pixmap.from_bytes(thumbs[0]), 30, 25)
    assert b0 > 200
    assert r0 < 50
    assert g0 < 80
    r1, g1, b1 = _pixel_rgb(Pixmap.from_bytes(thumbs[1]), 25, 20)
    assert r1 > 210
    assert g1 > 190
    assert b1 < 60

    fitted = doc.pages[0].pixmap(fit=120)
    assert (fitted.width, fitted.height) == (120, 80)
    assert _png_dimensions(fitted.to_bytes("png")) == (120, 80)

    images = doc.pages[0].images()
    assert len(images) == 2
    jpeg_stored = images[0].stored()
    assert jpeg_stored is not None
    assert jpeg_stored.filter == "DCTDecode"
    assert jpeg_stored.extension == "jpg"
    assert jpeg_stored.data.startswith(JPEG_SOI)
    assert jpeg_stored.data == jpeg_bytes
    jpeg_pm = images[0].pixmap()
    assert (jpeg_pm.width, jpeg_pm.height) == (16, 12)
    jr, jg, jb = _pixel_rgb(jpeg_pm, 8, 6)
    assert jr > 200
    assert jg < 60
    assert jb < 60

    assert images[1].stored() is None
    png_pm = images[1].pixmap()
    assert (png_pm.width, png_pm.height) == (12, 10)
    pr, pg, pb = _pixel_rgb(png_pm, 6, 5)
    assert pg > 180
    assert pr < 50
    assert pb < 80
    assert _png_dimensions(png_pm.to_bytes("png")) == (12, 10)


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_workflow_page_images_and_thumbnails(tmp_path: Path, *, incremental: bool) -> None:
    """Workflow 1: render thumbnails, extract embedded images, embed /Thumb, and preserve page 2."""
    original, jpeg_bytes = _build_images_fixture()
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(original)

    with pyropdf.open(source_path) as doc:
        untouched_text = doc.pages[1].text()
        untouched_pixels = bytes(doc.pages[1].pixmap(dpi=72))
        untouched_stream = _page_raw_content(doc, 1)
        _assert_thumbnails_and_embedded_images(doc, jpeg_bytes)

        doc.pages[0].thumbnail = doc.pages[0].pixmap(fit=96)
        out_path = tmp_path / f"with-thumb-{'inc' if incremental else 'full'}.pdf"
        doc.save(out_path, incremental=incremental)

    saved = out_path.read_bytes()
    if incremental:
        assert saved.startswith(original)
    _assert_clean_pdf(saved, expected_pages=2)

    with pyropdf.open(saved) as reopened:
        embedded_thumb = reopened.pages[0].thumbnail
        assert embedded_thumb is not None
        assert (embedded_thumb.width, embedded_thumb.height) == (96, 64)
        assert reopened.pages[1].thumbnail is None
        assert reopened.pages[1].text() == untouched_text
        assert bytes(reopened.pages[1].pixmap(dpi=72)) == untouched_pixels
        assert _page_raw_content(reopened, 1) == untouched_stream
        assert reopened.pages[1].duration == pytest.approx(12.375)

    with pikepdf.open(io.BytesIO(saved)) as pdf:
        assert "/Thumb" in pdf.pages[0]
        assert int(pdf.pages[0].Thumb.Width) == 96
        assert int(pdf.pages[0].Thumb.Height) == 64
        assert "/Thumb" not in pdf.pages[1]
        assert _pikepdf_page_raw_content(pdf, 1) == untouched_stream
        xobjects = pdf.pages[0].Resources.XObject
        jpeg_streams = [
            bytes(obj.read_raw_bytes())
            for obj in xobjects.values()
            if obj.get("/Subtype") == "/Image" and obj.get("/Filter") == "/DCTDecode"
        ]
        assert jpeg_bytes in jpeg_streams


def _build_organizing_fixtures() -> tuple[bytes, bytes]:
    """Return a 3-page base PDF with an outline destination and a 2-page PDF to merge."""
    with pyropdf.Document() as primary:
        for label, extra in (("Alpha", False), ("Beta", False), ("Gamma", True)):
            page = primary.pages.new((300, 200))
            with page.draw() as canvas:
                canvas.text(label, (36, 60), size=16)
                if extra:
                    canvas.rect((36, 90, 136, 140), fill=Color(0.2, 0.4, 0.8))
            if extra:
                page.duration = 7.125
        primary.outline = [OutlineEntry(1, "Go to Gamma", Destination(2))]
        primary_bytes = primary.to_bytes()

    with pyropdf.Document() as secondary:
        for label, color in (("Delta", Color(0.1, 0.7, 0.3)), ("Epsilon", Color(0.8, 0.3, 0.1))):
            page = secondary.pages.new((300, 200))
            with page.draw() as canvas:
                canvas.text(label, (36, 60), size=16)
                canvas.rect((36, 90, 116, 130), fill=color)
        secondary_bytes = secondary.to_bytes()
    return primary_bytes, secondary_bytes


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_workflow_organizing_pages(tmp_path: Path, *, incremental: bool) -> None:
    """Workflow 2: merge, reorder, rotate, delete, and split with preservation and pikepdf checks."""
    primary_bytes, secondary_bytes = _build_organizing_fixtures()

    with pyropdf.open(primary_bytes) as doc, pyropdf.open(secondary_bytes) as other:
        gamma_pixels = bytes(doc.pages[2].pixmap(dpi=72))
        gamma_stream = _page_raw_content(doc, 2)

        with doc.transaction("organize pages"):
            doc.pages.insert(len(doc.pages), other.pages)  # Alpha, Beta, Gamma, Delta, Epsilon
            doc.pages.move(4, 1)  # Alpha, Epsilon, Beta, Gamma, Delta
            doc.pages[0].rotation = 90
            del doc.pages[2]  # Delete Beta -> Alpha, Epsilon, Gamma, Delta

        organized_path = tmp_path / f"organized-{'inc' if incremental else 'full'}.pdf"
        doc.save(organized_path, incremental=incremental)

    organized_bytes = organized_path.read_bytes()
    if incremental:
        assert organized_bytes.startswith(primary_bytes)
    _assert_clean_pdf(organized_bytes, expected_pages=4)

    with pyropdf.open(organized_bytes) as reopened:
        assert [page.text().strip() for page in reopened.pages] == ["Alpha", "Epsilon", "Gamma", "Delta"]
        assert reopened.pages[0].rotation == 90
        assert (reopened.pages[0].rect.width, reopened.pages[0].rect.height) == (200.0, 300.0)
        assert (reopened.pages[0].pixmap(dpi=72).width, reopened.pages[0].pixmap(dpi=72).height) == (200, 300)
        assert len(reopened.outline) == 1
        assert reopened.outline[0].title == "Go to Gamma"
        dest = reopened.outline[0].destination
        assert isinstance(dest, Destination)
        assert dest.page == 2
        assert bytes(reopened.pages[2].pixmap(dpi=72)) == gamma_pixels
        assert _page_raw_content(reopened, 2) == gamma_stream
        assert reopened.pages[2].duration == pytest.approx(7.125)

        split_paths: list[Path] = []
        for number, page in enumerate(reopened.pages, start=1):
            part_path = tmp_path / f"split-{number}.pdf"
            with pyropdf.Document() as part:
                part.pages.insert(0, page)
                part.save(part_path)
            split_paths.append(part_path)

    with pikepdf.open(io.BytesIO(organized_bytes)) as pdf:
        assert int(pdf.pages[0].Rotate) == 90
        assert "/Rotate" not in pdf.pages[2]
        assert _pikepdf_page_raw_content(pdf, 2) == gamma_stream
        assert float(pdf.pages[2].Dur) == pytest.approx(7.125)

    for index, (part_path, expected_label) in enumerate(
        zip(split_paths, ("Alpha", "Epsilon", "Gamma", "Delta"), strict=True)
    ):
        part_bytes = part_path.read_bytes()
        _assert_clean_pdf(part_bytes, expected_pages=1)
        with pyropdf.open(part_bytes) as part_doc:
            assert part_doc.pages[0].text().strip() == expected_label
            assert part_doc.pages[0].rotation == (90 if index == 0 else 0)
        with pikepdf.open(io.BytesIO(part_bytes)) as part_pdf:
            if index == 0:
                assert int(part_pdf.pages[0].Rotate) == 90
            else:
                assert "/Rotate" not in part_pdf.pages[0]


def _build_stamp_source() -> bytes:
    """Return a 2-page PDF with body text on page 0 and an untouched page 1."""
    with pyropdf.Document() as source:
        p0 = source.pages.new((400, 300))
        with p0.draw() as canvas:
            canvas.text("Invoice body stays readable", (36, 260), size=14)
        p1 = source.pages.new((400, 300))
        p1.duration = 4.25
        with p1.draw() as canvas:
            canvas.text("Second page untouched", (36, 60), size=14)
            canvas.rect((36, 90, 120, 130), fill=Color(0.2, 0.2, 0.2))
        return source.to_bytes()


def _assert_stamped_pikepdf(saved: bytes, untouched_stream: bytes) -> None:
    """Independently check the stamp annotation, watermark resources, and untouched page 1 via pikepdf."""
    with pikepdf.open(io.BytesIO(saved)) as pdf:
        annots = pdf.pages[0].Annots
        assert len(annots) == 1
        assert str(annots[0].Subtype) == "/Stamp"
        assert str(annots[0].Name) == "/Confidential"
        assert isinstance(annots[0].AP.N, pikepdf.Stream)
        resources = pdf.pages[0].Resources
        assert "/ExtGState" in resources or "/XObject" in resources
        assert "/Annots" not in pdf.pages[1]
        assert _pikepdf_page_raw_content(pdf, 1) == untouched_stream


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_workflow_stamps_and_watermarks(tmp_path: Path, *, incremental: bool) -> None:
    """Workflow 3: add a Stamp annotation and semi-transparent diagonal watermark preserving body text and page 2."""
    base_bytes = _build_stamp_source()
    stamp_rect = (36, 24, 216, 74)
    watermark_region = (110, 90, 290, 210)
    font = Font.standard("Helvetica-Bold")

    with pyropdf.open(base_bytes) as doc:
        before_p0 = doc.pages[0].pixmap(dpi=72)
        before_p1 = doc.pages[1].pixmap(dpi=72)
        untouched_stream = _page_raw_content(doc, 1)

        with doc.transaction("stamp and watermark"):
            page = doc.pages[0]
            page.annotations.add(
                Stamp(stamp_rect, icon=StampIcon.CONFIDENTIAL, color=Color(0.8, 0.1, 0.1), opacity=0.9)
            )
            label = "DRAFT"
            width = font.text_width(label, 52)
            cx, cy = page.rect.width / 2, page.rect.height / 2
            place = Matrix(-30) * Matrix(1, 0, 0, 1, cx, cy)
            with page.draw() as canvas, canvas.opacity(0.3), canvas.transform(place):
                canvas.text(label, (-width / 2, 16), font=font, size=52, fill=Color(0.75, 0.1, 0.1))
                canvas.line((-width / 2, 26), (width / 2, 26), stroke=Stroke(paint=Color(0.75, 0.1, 0.1), width=3))

        out_path = tmp_path / f"stamped-{'inc' if incremental else 'full'}.pdf"
        doc.save(out_path, incremental=incremental)

    saved = out_path.read_bytes()
    if incremental:
        assert saved.startswith(base_bytes)
    _assert_clean_pdf(saved, expected_pages=2)

    with pyropdf.open(saved) as reopened:
        p0_after = reopened.pages[0]
        after_p0 = p0_after.pixmap(dpi=72)
        assert len(p0_after.annotations) == 1
        stamp = p0_after.annotations[0]
        assert stamp.type is AnnotationType.STAMP
        assert stamp.icon == "Confidential"
        assert stamp.rect == Rect(*stamp_rect)
        assert _region_diff(before_p0, after_p0, stamp_rect) > 100
        assert _region_diff(before_p0, after_p0, watermark_region) > 100
        extracted = p0_after.text()
        assert "Invoice body stays readable" in extracted
        assert "DRAFT" in extracted
        assert "CONFIDENTIAL" in extracted
        assert bytes(reopened.pages[1].pixmap(dpi=72)) == bytes(before_p1)
        assert reopened.pages[1].text().strip() == "Second page untouched"
        assert _page_raw_content(reopened, 1) == untouched_stream
        assert reopened.pages[1].duration == pytest.approx(4.25)

    _assert_stamped_pikepdf(saved, untouched_stream)


def _build_form_fixture() -> tuple[bytes, tuple[tuple[int, int, int, int], ...]]:
    """Build a 2-page PDF with text, checkbox, and combo box fields on page 0 and an untouched page 1."""
    name_rect = (40, 80, 260, 104)
    agree_rect = (40, 124, 58, 142)
    tier_rect = (40, 164, 220, 188)
    with pyropdf.Document() as form_doc:
        p0 = form_doc.pages.new((420, 320))
        with p0.draw() as canvas:
            canvas.text("Registration Form", (40, 48), size=16)
        p0.widgets.add(TextField("applicant", name_rect))
        p0.widgets.add(CheckBox("agree", agree_rect))
        p0.widgets.add(ComboBox("tier", tier_rect, choices=["Standard", "Priority", "Enterprise"]))

        p1 = form_doc.pages.new((420, 320))
        p1.duration = 9.5
        with p1.draw() as canvas:
            canvas.text("Terms page stays intact", (40, 48), size=14)
        return form_doc.to_bytes(), (name_rect, agree_rect, tier_rect)


def _assert_filled_form_pikepdf(filled_bytes: bytes, untouched_stream: bytes) -> None:
    """Independently check filled AcroForm field values, widget appearances, and untouched page 1."""
    with pikepdf.open(io.BytesIO(filled_bytes)) as pdf:
        assert "/AcroForm" in pdf.Root
        by_name = {str(field.T): field for field in pdf.Root.AcroForm.Fields}
        assert str(by_name["applicant"].V) == "Ada Lovelace"
        assert str(by_name["agree"].V) == "/Yes"
        assert str(by_name["tier"].V) == "Priority"
        assert len(pdf.pages[0].Annots) == 3
        for widget in pdf.pages[0].Annots:
            assert "/AP" in widget
            assert "/N" in widget.AP
        assert _pikepdf_page_raw_content(pdf, 1) == untouched_stream


def _assert_flattened_form_pikepdf(flattened_bytes: bytes, untouched_stream: bytes) -> None:
    """Independently verify that flattening removed AcroForm and widgets without altering page 1."""
    with pikepdf.open(io.BytesIO(flattened_bytes)) as pdf:
        assert "/AcroForm" not in pdf.Root
        assert "/Annots" not in pdf.pages[0]
        assert _pikepdf_page_raw_content(pdf, 1) == untouched_stream


@pytest.mark.parametrize("flatten_incremental", [False, True], ids=["full", "incremental"])
def test_workflow_fill_and_flatten_forms(tmp_path: Path, *, flatten_incremental: bool) -> None:
    """Workflow 4: fill text/checkbox/choice fields, save incrementally, then flatten cleanly."""
    form_bytes, widget_rects = _build_form_fixture()
    form_defects = _defect_keys(form_bytes)

    with pyropdf.open(form_bytes) as doc:
        empty_p0 = doc.pages[0].pixmap(dpi=72)
        before_p1 = doc.pages[1].pixmap(dpi=72)
        untouched_stream = _page_raw_content(doc, 1)

        with doc.transaction("fill form"):
            doc.form["applicant"].value = "Ada Lovelace"
            doc.form["agree"].value = True
            doc.form["tier"].value = "Priority"
        filled_path = tmp_path / "filled-incremental.pdf"
        doc.save(filled_path, incremental=True)

    filled_bytes = filled_path.read_bytes()
    assert filled_bytes.startswith(form_bytes)
    _assert_clean_pdf(filled_bytes, expected_pages=2, baseline_defects=form_defects)
    _assert_filled_form_pikepdf(filled_bytes, untouched_stream)

    with pyropdf.open(filled_bytes) as filled_doc:
        assert filled_doc.form["applicant"].value == "Ada Lovelace"
        assert filled_doc.form["agree"].value == "Yes"
        assert filled_doc.form["tier"].value == "Priority"
        filled_p0 = filled_doc.pages[0].pixmap(dpi=72)
        for rect in widget_rects:
            assert _region_diff(empty_p0, filled_p0, rect) > 20
        assert bytes(filled_doc.pages[1].pixmap(dpi=72)) == bytes(before_p1)
        assert _page_raw_content(filled_doc, 1) == untouched_stream

        filled_doc.flatten()
        flattened_path = tmp_path / f"flattened-{'inc' if flatten_incremental else 'full'}.pdf"
        filled_doc.save(flattened_path, incremental=flatten_incremental)

    flattened_bytes = flattened_path.read_bytes()
    if flatten_incremental:
        assert flattened_bytes.startswith(filled_bytes)
    _assert_clean_pdf(flattened_bytes, expected_pages=2)

    with pyropdf.open(flattened_bytes) as flattened_doc:
        assert len(flattened_doc.form) == 0
        assert len(flattened_doc.pages[0].widgets) == 0
        assert "AcroForm" not in flattened_doc.catalog
        flattened_p0 = flattened_doc.pages[0].pixmap(dpi=72)
        assert _region_diff(filled_p0, flattened_p0, (0, 0, 420, 320)) < 60
        for rect in widget_rects:
            assert _region_diff(empty_p0, flattened_p0, rect) > 20
        page_text = flattened_doc.pages[0].text()
        assert "Registration Form" in page_text
        assert "Ada Lovelace" in page_text
        assert "Priority" in page_text
        assert bytes(flattened_doc.pages[1].pixmap(dpi=72)) == bytes(before_p1)
        assert flattened_doc.pages[1].text().strip() == "Terms page stays intact"
        assert _page_raw_content(flattened_doc, 1) == untouched_stream
        assert flattened_doc.pages[1].duration == pytest.approx(9.5)

    _assert_flattened_form_pikepdf(flattened_bytes, untouched_stream)


def _build_dirty_document(tmp_path: Path) -> bytes:
    """Create a 2-revision PDF carrying scripts, metadata, attachments, comments, hidden text, and an orphan object."""
    with pyropdf.Document() as doc:
        p0 = doc.pages.new((400, 240))
        font_xref = doc.objects.add({"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica")})
        p0.obj["Resources"] = {"Font": {"F1": doc.objects[font_xref]}}
        p0.obj["Contents"] = doc.objects[
            doc.objects.add_stream(
                b"BT /F1 12 Tf 36 190 Td (Account holder: Ada Lovelace) Tj "
                b"0 -24 Td (Secret code: TOP-9981) Tj "
                b"3 Tr 0 -24 Td (Hidden secret leak) Tj ET"
            )
        ]

        p1 = doc.pages.new((400, 240))
        with p1.draw() as canvas:
            canvas.text("Public summary stays intact", (36, 60), size=14)
        p1.annotations.add(Note((36, 100), contents="Internal reviewer comment"))

        script_xref = doc.objects.add({"S": Name("JavaScript"), "JS": "app.alert('leak')"})
        doc.catalog["OpenAction"] = doc.objects[script_xref]
        doc.catalog["Names"] = {"JavaScript": {"Names": ["startup", doc.objects[script_xref]]}}
        p0.obj["AA"] = {"O": {"S": Name("JavaScript"), "JS": "1"}, "C": {"S": Name("Named"), "N": Name("NextPage")}}
        doc.attachments.add("audit.txt", b"internal attachment payload")
        doc.metadata = {"title": "Confidential Draft", "author": "Internal Bot"}
        p0.obj["LastModified"] = "D:20260101000000Z"
        p0.obj["PieceInfo"] = {"InternalApp": {"LastModified": "D:20260101000000Z"}}
        doc.objects.add("orphan-leak-sentinel")

        base_path = tmp_path / "dirty-base.pdf"
        doc.save(base_path)

    with pyropdf.open(base_path) as rev_doc:
        rev_doc.metadata = {"title": "Confidential Draft Rev 2", "author": "Internal Bot"}
        return rev_doc.to_bytes(incremental=True)


def _assert_sanitized_with_marks(sanitized_bytes: bytes) -> None:
    """Verify sanitized output keeps visible text and /Redact marks while stripping active/hidden content."""
    assert sanitized_bytes.count(b"startxref") == 1
    assert b"orphan-leak-sentinel" not in sanitized_bytes
    _assert_clean_pdf(sanitized_bytes, expected_pages=2)

    with pyropdf.open(sanitized_bytes) as reopened:
        assert len(reopened.attachments) == 0
        assert dict(reopened.metadata) == {}
        assert "Hidden secret leak" not in reopened.pages[0].text()
        assert "Account holder: Ada Lovelace" in reopened.pages[0].text()
        assert "Public summary stays intact" in reopened.pages[1].text()
        # Underlying marked text stays until full content-removing redaction (#362) lands.
        assert "TOP-9981" in reopened.pages[0].text()
        redactions = [a for a in reopened.pages[0].annotations if a.type is AnnotationType.REDACT]
        assert len(redactions) == 1
        info = redactions[0].redaction
        assert info is not None
        assert info.overlay_text == "REDACTED"
        assert info.interior_color == Color(0, 0, 0)

    with pikepdf.open(io.BytesIO(sanitized_bytes)) as pdf:
        assert "/OpenAction" not in pdf.Root
        assert "/AF" not in pdf.Root
        assert "/Info" not in pdf.trailer
        assert "/PieceInfo" not in pdf.pages[0]
        assert "/AA" not in pdf.pages[0]
        if "/Names" in pdf.Root:
            assert "/JavaScript" not in pdf.Root.Names
            assert "/EmbeddedFiles" not in pdf.Root.Names
        annots = pdf.pages[0].Annots
        assert [str(a.Subtype) for a in annots] == ["/Redact"]
        assert str(annots[0].OverlayText) == "REDACTED"
        assert bytes(annots[0].DA) == b"/Helv 10 Tf 1 1 1 rg"
        assert [float(v) for v in annots[0].IC] == pytest.approx([0.0, 0.0, 0.0])
        assert isinstance(annots[0].AP.N, pikepdf.Stream)


@pytest.mark.parametrize("mark_incremental", [False, True], ids=["full", "incremental"])
def test_workflow_redaction_marks_and_sanitization(tmp_path: Path, *, mark_incremental: bool) -> None:
    """Workflow 5: place RedactionMark annotations and sanitize active/hidden content cleanly."""
    input_bytes = _build_dirty_document(tmp_path)
    assert input_bytes.count(b"startxref") == 2

    with pyropdf.open(input_bytes) as doc:
        before_p0 = doc.pages[0].pixmap(dpi=72)
        hits = doc.pages[0].search("TOP-9981", quads=True)
        assert len(hits) == 1
        quad = hits[0]
        doc.pages[0].annotations.add(
            RedactionMark(
                quad.rect,
                quads=(quad,),
                overlay_text="REDACTED",
                default_appearance=b"/Helv 10 Tf 1 1 1 rg",
                interior_color=Color(0, 0, 0),
            )
        )
        marked_path = tmp_path / f"marked-{'inc' if mark_incremental else 'full'}.pdf"
        doc.save(marked_path, incremental=mark_incremental)

    marked_bytes = marked_path.read_bytes()
    if mark_incremental:
        assert marked_bytes.startswith(input_bytes)
    _assert_clean_pdf(marked_bytes, expected_pages=2)

    with pyropdf.open(marked_bytes) as marked_doc:
        after_p0 = marked_doc.pages[0].pixmap(dpi=72)
        clip_box = (
            max(0, int(quad.rect.x0) - 2),
            max(0, int(quad.rect.y0) - 2),
            min(400, int(quad.rect.x1) + 2),
            min(240, int(quad.rect.y1) + 2),
        )
        assert _region_diff(before_p0, after_p0, clip_box) > 20

        kept_marks_path = tmp_path / "sanitized-with-marks.pdf"
        report = marked_doc.sanitize(kept_marks_path, comments=False)

    assert report.removed["javascript"] > 0
    assert report.removed["actions"] > 0
    assert report.removed["attachments"] > 0
    assert report.removed["metadata"] > 0
    assert report.removed["hidden_text"] > 0
    assert "comments" not in report.removed
    _assert_sanitized_with_marks(kept_marks_path.read_bytes())

    with pyropdf.open(marked_bytes) as marked_doc:
        full_clean_path = tmp_path / "sanitized-all.pdf"
        full_report = marked_doc.sanitize(full_clean_path)

    for key in ("javascript", "actions", "attachments", "metadata", "comments", "hidden_text"):
        assert full_report.removed[key] > 0

    full_bytes = full_clean_path.read_bytes()
    assert full_bytes.count(b"startxref") == 1
    assert b"orphan-leak-sentinel" not in full_bytes
    _assert_clean_pdf(full_bytes, expected_pages=2)

    with pyropdf.open(full_bytes) as clean_doc:
        assert "Account holder: Ada Lovelace" in clean_doc.pages[0].text()
        assert "Public summary stays intact" in clean_doc.pages[1].text()
        assert "Hidden secret leak" not in clean_doc.pages[0].text()
        assert len(clean_doc.pages[0].annotations) == 0
        assert len(clean_doc.pages[1].annotations) == 0

    with pikepdf.open(io.BytesIO(full_bytes)) as pdf:
        assert "/OpenAction" not in pdf.Root
        assert "/Info" not in pdf.trailer
        assert "/Annots" not in pdf.pages[0]
        assert "/Annots" not in pdf.pages[1]
