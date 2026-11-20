"""Stored prepress semantics, including hand-built independent plate and appearance graphs."""

from __future__ import annotations

import io

import pikepdf
import pytest
from test_attachments import build_raw_pdf

import pyropdf

N = pyropdf.Name


def prepress_source() -> bytes:
    """Serialize two plates, an unrelated page, and shared print resources independently."""

    def stream(entries: str, content: bytes) -> bytes:
        return f"<< {entries} /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream"

    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Count 3 /Kids [3 0 R 4 0 R 5 0 R] /MediaBox [0 0 220 200] /Resources 6 0 R >>",
            3: "<< /Type /Page /Parent 2 0 R /CropBox [1 2 210 195] /BoxColorInfo 7 0 R /SeparationInfo "
            "<< /Pages 8 0 R /DeviceColorant /Cyan /ColorSpace 16 0 R >> /Annots [9 0 R 10 0 R 11 0 R] "
            "/LastModified (D:20231011000000Z) >>",
            4: "<< /Type /Page /Parent 2 0 R /CropBox [1 2 210 195] /BoxColorInfo 7 0 R /SeparationInfo "
            "<< /Pages 8 0 R /DeviceColorant (Black) >> >>",
            5: "<< /Type /Page /Parent 2 0 R /Contents 19 0 R >>",
            6: "<< /XObject << /Proxy 18 0 R >> >>",
            7: "<< /CropBox << /C [1 0 0] /S /D /D [2 3] >> /ArtBox <<>> >>",
            8: "[3 0 R 4 0 R]",
            9: "<< /Type /Annot /Subtype /PrinterMark /F 68 /P 3 0 R /Rect [0 0 10 10] /MN /ColorBar "
            "/AP << /N << /Press 12 0 R >> >> /AS /Press >>",
            10: "<< /Type /Annot /Subtype /Watermark /F 4 /P 3 0 R /Rect [50 50 80 80] /AP << /N 15 0 R >> "
            "/FixedPrint << /Type /FixedPrint /H 0.25 /V 1 /Matrix [1 0 0 1 10 -12] >> >>",
            11: "<< /Type /Annot /Subtype /TrapNet /F 68 /P 3 0 R /Rect [0 0 220 200] /AS /Original "
            "/AP << /N << /Original 13 0 R >> >> /LastModified (D:20231011000000Z) >>",
            12: stream(
                "/Type /XObject /Subtype /Form /BBox [0 0 10 10] /Resources 6 0 R /MarkStyle (Press mark) "
                "/Colorants << /Cyan 16 0 R >>",
                b"0 g 1 1 3 3 re f",
            ),
            13: stream(
                "/Type /XObject /Subtype /Form /BBox [0 0 220 200] /Resources 6 0 R /PCM /DeviceCMYK "
                "/TrapStyles (Original) /SeparationColorNames [/Cyan /Black] /TrapRegions [14 0 R]",
                b"",
            ),
            14: "<< /TP /Standard /TZ [[0 0 100 0 100 100 0 100]] >>",
            15: stream(
                "/Type /XObject /Subtype /Form /BBox [0 0 10 10] /Resources 6 0 R "
                "/OPI << /2.0 << /Type /OPI /Version 2.0 /F (press.tif) /Size [8 8] "
                "/CropRect [0 0 8 8] /Inks /full_color >> >>",
                b"",
            ),
            16: "[/Separation /Cyan /DeviceCMYK << /FunctionType 2 /Domain [0 1] /N 1 /C0 [0 0 0 0] /C1 [1 0 0 0] >>]",
            18: stream(
                "/Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 "
                "/OPI << /1.3 << /Type /OPI /Version 1.3 /F (old.tif) /Size [1 1] /CropRect [0 0 1 1] "
                "/Position [0 0 0 1 1 1 1 0] >> >>",
                b"\x80",
            ),
            19: stream("", b"q Q"),
        }
    )


@pytest.mark.spec("page-boundaries", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.spec("separation-dictionaries", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_group_edits_preserve_plate_identity_colorants_and_shared_styles(*, incremental: bool) -> None:
    source = prepress_source()
    with pyropdf.open(source) as doc:
        a, b, c = doc.pages
        assert a.box_styles["ArtBox"] == pyropdf.BoxStyle()
        assert a.box_styles["CropBox"] == pyropdf.BoxStyle(color=(1, 0, 0), dashed=True, dashes=(2, 3))
        plates = a.separations
        assert tuple(plates) == (a, b)
        assert plates[a].color_space == doc.objects[16]
        assert isinstance(plates[b].colorant, pyropdf.String)
        a.box_styles = {"CropBox": pyropdf.BoxStyle(width=2.5)}
        a.separations = {b: plates[b], a: plates[a], c: pyropdf.SeparationPlate(colorant=N("Yellow"))}
        assert tuple(c.separations) == (b, a, c)
        a.separations = {a: plates[a], c: c.separations[c]}
        assert not b.separations
        doc.pages.move(2, 0)
        saved = doc.to_bytes(incremental=incremental)
    if incremental:
        assert saved.startswith(source)
    with pyropdf.open(saved) as doc:
        c, a, b = doc.pages
        assert tuple(a.separations) == (a, c)
        assert a.separations == c.separations
        assert not b.separations
        assert a.box_styles["CropBox"].width == 2.5
        assert b.box_styles["CropBox"].dashes == (2, 3)
        assert not doc.check()
    with pikepdf.open(io.BytesIO(saved)) as pdf:
        c, a, b = pdf.pages
        assert not pdf.check_pdf_syntax()
        assert a.SeparationInfo.Pages.objgen == c.SeparationInfo.Pages.objgen
        assert [p.objgen for p in c.SeparationInfo.Pages] == [a.objgen, c.objgen]
        assert a.SeparationInfo.ColorSpace.objgen == a.Annots[0].AP.N.Press.Colorants.Cyan.objgen
        assert "/SeparationInfo" not in b
        assert c.Contents.read_bytes() == b"q Q"


@pytest.mark.spec("printer-marks", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.spec("printer-mark-annotations", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.spec("trap-network-annotations", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("trapping", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("opi", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("watermark-annotations", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_print_forms_traps_opi_and_fixed_placement_reopen(*, incremental: bool) -> None:
    saved, spec = _edit_print_graph(incremental=incremental)
    with pyropdf.open(saved) as doc:
        water, new, mark, trap = doc.pages[0].annotations
        assert new.fixed_print == spec
        assert water.fixed_print is None
        assert isinstance(mark.print_appearance, pyropdf.typed.XObjectFormPrinterMark)
        assert mark.print_appearance.MarkStyle == "Proof mark"
        assert isinstance(trap.print_appearance, pyropdf.typed.XObjectFormTrapNet)
        assert trap.print_appearance.TrapStyles == "Fine"
        assert not doc.check()
    _check_print_graph(saved, spec)


def _check_print_graph(saved: bytes, spec: pyropdf.FixedPrint) -> None:
    with pikepdf.open(io.BytesIO(saved)) as pdf:
        assert not pdf.check_pdf_syntax()
        page = pdf.pages[0]
        water, new, mark, trap = page.Annots
        assert new.P.objgen == page.objgen
        assert mark.P.objgen == page.objgen
        assert [x.Subtype for x in page.Annots] == ["/Watermark", "/Watermark", "/PrinterMark", "/TrapNet"]
        assert mark.MN == "/RegistrationTarget"
        assert mark.F == 68
        assert "/FixedPrint" not in water
        assert water.AP.N.objgen == new.AP.N.objgen
        assert new.AP.N.Resources.objgen == mark.AP.N.Resources.objgen
        assert tuple(new.FixedPrint.Matrix) == tuple(spec.matrix)
        assert float(new.FixedPrint.H) == spec.horizontal
        assert float(new.FixedPrint.V) == spec.vertical
        assert trap.AP.N.Original.Resources.objgen == mark.AP.N.Resources.objgen
        opi = mark.AP.N.Resources.XObject.Proxy.OPI
        assert str(opi["/1.3"].F) == "old.tif"
        assert str(water.AP.N.OPI["/2.0"].F) == "updated.tif"
        assert mark.AP.N.read_bytes() == b"0 g 1 1 3 3 re f"


@pytest.mark.spec("separation-dictionaries", "edit")
def test_incomplete_groups_and_invalid_annotation_edits_refuse_atomically() -> None:
    with pyropdf.open(prepress_source()) as doc, pyropdf.Document() as receiver:
        a, b, c = doc.pages
        receiver.pages.new()
        before = doc.to_bytes(incremental=True)
        steps = doc.history.steps
        with pytest.raises(ValueError, match="split a separation group"):
            del doc.pages[1]
        with pytest.raises(ValueError, match="separation group"):
            doc.pages.extend(a)
        with pytest.raises(ValueError, match="split a separation group"):
            receiver.pages.extend(b)
        with pytest.raises(ValueError, match="existing separation group"):
            c.separations = {a: a.separations[a], c: pyropdf.SeparationPlate(colorant=N("Yellow"))}
        with pytest.raises(pyropdf.PdfError, match="flags"):
            a.annotations[0].flags = pyropdf.AnnotationFlags.PRINT
        assert doc.history.steps == steps
        assert doc.to_bytes(incremental=True) == before
        assert len(receiver.pages) == 1
        receiver.pages.extend([b, a])
        first, second = receiver.pages[1:]
        assert tuple(first.separations) == (second, first)
        del a.separations
        del doc.pages[0]
        assert not b.separations


def _edit_print_graph(*, incremental: bool) -> tuple[bytes, pyropdf.FixedPrint]:
    with pyropdf.open(prepress_source()) as doc:
        page = doc.pages[0]
        mark, water, trap = page.annotations
        form = mark.print_appearance
        assert isinstance(form, pyropdf.typed.XObjectFormPrinterMark)
        assert form.MarkStyle == "Press mark"
        assert form.Colorants is not None
        assert form.Colorants.obj["Cyan"] == doc.objects[16]
        tf = trap.print_appearance
        assert isinstance(tf, pyropdf.typed.XObjectFormTrapNet)
        assert tf.PCM == "DeviceCMYK"
        assert tf.TrapRegions is not None
        assert tf.TrapRegions.obj[0]["TP"] == "Standard"
        assert list(tf.TrapRegions.obj[0]["TZ"][0]) == [0, 0, 100, 0, 100, 100, 0, 100]
        tf.TrapStyles = "Fine"
        assert water.fixed_print == pyropdf.FixedPrint(
            matrix=pyropdf.Matrix(1, 0, 0, 1, 10, -12), horizontal=0.25, vertical=1
        )
        proxy = doc.objects[18]
        assert isinstance(proxy.opi, pyropdf.typed.OPIVersion13Dict)
        assert proxy.opi.F == b"old.tif"
        high = doc.objects[15].opi
        assert isinstance(high, pyropdf.typed.OPIVersion20Dict)
        assert high.F == b"press.tif"
        high.F = b"updated.tif"
        form.MarkStyle = "Proof mark"
        spec = pyropdf.FixedPrint(matrix=pyropdf.Matrix(0, 1, -1, 0, 2, 3), horizontal=0.5, vertical=0.75)
        new = page.annotations.add(pyropdf.Watermark(rect=water.rect, appearance=doc.objects[15], fixed_print=spec))
        water.fixed_print = spec
        del water.fixed_print
        assert water.fixed_print is None
        page.annotations.add(pyropdf.PrinterMark(rect=mark.rect, appearance=form.obj, name=N("RegistrationTarget")))
        del page.annotations[0]
        assert new.print_appearance is not None
        assert new.print_appearance.obj == doc.objects[15]
        return doc.to_bytes(incremental=incremental), spec


def test_print_creation_checks_version_fallout_before_committing() -> None:
    objects: dict[int, str | bytes] = {
        1: "<< /Type /Catalog /Pages 2 0 R /PieceInfo <<>> >>",
        2: "<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        3: "<< /Type /Page /Parent 2 0 R /Resources <<>> /MediaBox [0 0 100 100] >>",
    }
    # The older producer wrote a later catalog key. Promoting from 1.3 also
    # makes trailer /Info compulsory in the presence of that key.
    with pyropdf.open(build_raw_pdf(objects).replace(b"1.7", b"1.3", 1)) as doc:
        before = doc.to_bytes(incremental=True)
        steps = doc.history.steps
        with pytest.raises(pyropdf.PdfError, match=r"promotion.*FileTrailer/Info"):
            doc.pages[0].box_styles = {"CropBox": pyropdf.BoxStyle()}
        assert doc.to_bytes(incremental=True) == before
        assert doc.history.steps == steps


@pytest.mark.spec("trap-network-annotations", "edit", "preserve")
def test_trap_stays_last_through_ordinary_annotation_and_link_addition() -> None:
    with pyropdf.open(prepress_source()) as doc:
        page = doc.pages[0]
        trap = page.annotations[-1]
        page.annotations.add(pyropdf.Note((15, 15), contents="press notes"))
        page.links.add((0, 0, 10, 10), pyropdf.Destination(0))
        assert page.obj["Annots"][-1] == trap.obj
        with pyropdf.open(doc.to_bytes()) as reopened:
            assert not reopened.check()


@pytest.mark.spec("opi", "interpret")
def test_opi_rejects_wrong_version_and_ignores_masks() -> None:
    with pyropdf.open(prepress_source()) as doc:
        image = doc.objects[18]
        image["OPI"]["1.3"]["Version"] = 2
        with pytest.raises(pyropdf.PdfError, match="Version"):
            _ = image.opi
        image["ImageMask"] = True
        assert image.opi is None
        with pytest.raises(pyropdf.LimitError):
            pyropdf.BoxStyle(dashes=(1,) * 4097)


@pytest.mark.spec("printer-mark-annotations", "edit")
def test_a_referenced_print_annotation_cannot_be_detached() -> None:
    with pyropdf.open(prepress_source()) as doc:
        page = doc.pages[0]
        del page.separations
        mark = page.annotations[0]
        doc.catalog["OpenAction"] = {"S": N("Hide"), "T": mark.obj}
        before = doc.to_bytes(incremental=True)
        steps = doc.history.steps
        with pytest.raises(ValueError, match="still referenced"):
            del page.annotations[0]
        with pytest.raises(ValueError, match="retained object still references"):
            del doc.pages[0]
        assert doc.history.steps == steps
        assert doc.to_bytes(incremental=True) == before
        assert mark.obj == doc.catalog["OpenAction"]["T"]


def test_plate_write_charges_one_budget_and_keeps_the_original_group_on_refusal() -> None:
    with pyropdf.open(prepress_source(), limits=pyropdf.DocumentLimits(max_memory_size=16_000)) as doc:
        first, second = doc.pages[:2]
        plates = first.separations
        history = doc.history.steps
        with pytest.raises(pyropdf.LimitError) as refusal:
            first.separations = {second: plates[second], first: plates[first]}
        assert refusal.value.code == "memory_size"
        assert first.separations == plates
        assert doc.history.steps == history


@pytest.mark.parametrize("incremental", [False, True])
def test_box_guideline_edits_keep_unrelated_indirect_values(*, incremental: bool) -> None:
    # An indirect scalar cannot be reconstructed from its resolved Python value.
    with pikepdf.open(io.BytesIO(prepress_source())) as pdf:
        private = pdf.make_indirect(pikepdf.String("shared"))
        pdf.pages[0].BoxColorInfo.CropBox["/XXprivate"] = private
        pdf.pages[0].BoxColorInfo["/XXprivate"] = private
        baseline = io.BytesIO()
        pdf.save(baseline)
    with pyropdf.open(baseline.getvalue()) as doc:
        page = doc.pages[0]
        page.box_styles = {"CropBox": pyropdf.BoxStyle(width=5)}
        output = doc.to_bytes(incremental=incremental)
    with pikepdf.open(io.BytesIO(output)) as pdf:
        changed, sibling = (page.BoxColorInfo for page in pdf.pages[:2])
        assert changed.CropBox.W == 5
        assert "/W" not in sibling.CropBox
        assert changed.XXprivate.is_indirect
        assert changed.XXprivate.objgen == changed.CropBox.XXprivate.objgen == sibling.XXprivate.objgen
