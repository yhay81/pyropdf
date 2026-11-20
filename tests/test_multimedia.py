"""Base PDF multimedia as data. The reference PDF is generated independently of pyropdf."""

from __future__ import annotations

import io
import struct
from typing import TYPE_CHECKING

import pikepdf
import pytest
from test_actions import _build_raw_pdf

import pyropdf as pdf
from pyropdf import typed

if TYPE_CHECKING:
    from pathlib import Path

# ECMA-363 section 9.4.1. An empty U3D scene needs only the file header block.
# Negative major version for an unvalidated encoder, base profile, UTF-8.
_U3D = struct.pack("<IIIhhIIQI", 0x00443355, 24, 0, -1, 0, 0, 36, 36, 106)
_RECT = pdf.Rect(10, 15, 80, 60)


def media_pdf() -> bytes:
    """Write the PDF syntax directly, including a shared file, 3D view, measurement and stream."""
    annot = "/Type /Annot /Rect [10 40 80 85] /P 3 0 R /AP << /N 4 0 R >>"
    source = _build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100]"
                " /Resources <<>> /Annots [5 0 R 6 0 R 7 0 R 8 0 R 9 0 R] >>"
            ),
            4: "<< /Type /XObject /Subtype /Form /BBox [0 0 70 45] /Resources <<>> /Length 0 >>\nstream\n\nendstream",
            5: f"<< {annot} /Subtype /Screen /A << /S /Rendition /AN 5 0 R /R 10 0 R /OP 0 >> >>",
            6: f"<< {annot} /Subtype /3D /3DD 11 0 R /3DV (front) >>",
            7: (
                f"<< {annot} /Subtype /RichMedia /RichMediaContent <<"
                " /Assets << /Names [(empty.u3d) 12 0 R] >> /Configurations [13 0 R] /Views [14 0 R] >> >>"
            ),
            8: f"<< {annot} /Subtype /Projection /ExData << /Type /ExData /Subtype /3DM /M3DREF 15 0 R >> >>",
            9: f"<< {annot} /Subtype /3D /3DD << /Type /3DRef /3DD 11 0 R >> >>",
            10: "<< /S /MR /N (Empty scene) /C << /S /MCD /CT (model/u3d) /D 12 0 R >> >>",
            11: (
                "<< /Type /3D /Subtype /U3D /VA [14 0 R] /DV /F"
                f" /Length {len(_U3D)} >>\nstream\n{_U3D.decode('latin-1')}\nendstream"
            ),
            12: "<< /Type /Filespec /F (empty.u3d) /EF << /F 16 0 R >> >>",
            13: "<< /Type /RichMediaConfiguration /Subtype /3D /Instances [17 0 R] >>",
            14: "<< /Type /3DView /XN (Front) /IN (front) /MA [15 0 R] /P << /Subtype /O /OS 2 >> >>",
            15: "<< /Type /3DMeasure /Subtype /3DC /A1 [0 0 0] /TP [0 1 0] /S 8 0 R >>",
            16: (
                f"<< /Type /EmbeddedFile /Subtype /model#2Fu3d /Length {len(_U3D)}"
                f" >>\nstream\n{_U3D.decode('latin-1')}\nendstream"
            ),
            17: "<< /Type /RichMediaInstance /Subtype /3D /Asset 12 0 R >>",
        },
        version="2.0",
    )
    return source.replace(b"/Root 1 0 R >>", b"/Root 1 0 R /ID [<1234567890abcdef> <1234567890abcdef>] >>")


def _edit_fixture(doc: pdf.Document) -> int:
    screen, model, rich, projection, shared = doc.pages[0].annotations
    assert isinstance(screen.media, typed.AnnotScreen)
    assert isinstance(model.media, typed.Annot3D)
    assert isinstance(rich.media, typed.AnnotRichMedia)
    assert isinstance(projection.media, typed.AnnotProjection)
    assert isinstance(shared.media, typed.Annot3D)
    rendition = screen.action
    assert isinstance(rendition, pdf.RenditionAction)
    assert isinstance(rendition.rendition, typed.RenditionMedia)
    assert rendition.annotation == screen.xref
    assert rendition.operation is pdf.RenditionOperation.PLAY
    clip = rendition.rendition.C
    assert isinstance(clip, typed.MediaClipData)
    file = clip.D
    assert isinstance(file, typed.FileSpecification)
    assert file.EF
    assert file.EF.F
    assert file.EF.F.obj.read() == _U3D
    config = rich.media.RichMediaContent.Configurations[0]
    instance = config.Instances[0]
    assert instance.Asset == file
    mstream = model.media["3DD"]
    assert isinstance(mstream, typed.ThreeDStream)
    assert mstream.obj.read() == _U3D
    assert shared.media["3DD"]["3DD"].obj == mstream.obj
    view = mstream.VA[0]
    measure = view.MA[0]
    assert isinstance(projection.media.ExData, typed.ExDataProjection)
    assert measure == projection.media.ExData.M3DREF
    assert projection.media == measure.S
    assert view.P.OS == 2
    view.P.OS = 3
    view.XN = "Profile"
    measure.TRL = "Distance"
    projection.contents = "Dimension"
    rendition.rendition.N = "Edited scene"
    config.Name = "Edited configuration"
    screen.action = pdf.RenditionAction(
        annotation=screen.xref, operation=pdf.RenditionOperation.PAUSE, rendition=rendition.rendition
    )
    doc.open_action = pdf.GoTo3DView(annotation=model.xref, view="front")
    screen.triggers[pdf.AnnotationTrigger.PAGE_OPEN] = pdf.RichMediaExecute(
        annotation=rich.xref, command="pause", instance=instance, arguments="track"
    )
    return model.xref


@pytest.mark.spec("screen-annotations", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("renditions", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("rich-annotations", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("3d-artwork", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("rich-media", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("projection-annotations", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("rendition-actions", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("goto-3d-view-actions", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.spec("rich-media-execute-actions", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_media_relationships_roundtrip(*, incremental: bool, tmp_path: Path) -> None:
    source = media_pdf()
    doc = pdf.open(source)
    assert not doc.check()
    model_xref = _edit_fixture(doc)
    dest = tmp_path / "saved.pdf"
    doc.save(dest, incremental=incremental)
    if incremental:
        assert dest.read_bytes().startswith(source)
    with pdf.open(dest) as reopened:
        assert not reopened.check()
        assert reopened.open_action == pdf.GoTo3DView(annotation=model_xref, view="front")
        rs = reopened.pages[0].annotations[0]
        assert isinstance(rs.action, pdf.RenditionAction)
        assert rs.action.operation is pdf.RenditionOperation.PAUSE
        cmd = rs.triggers[pdf.AnnotationTrigger.PAGE_OPEN]
        assert isinstance(cmd, pdf.RichMediaExecute)
        assert cmd.command == "pause"
        assert cmd.arguments == "track"
    with pikepdf.open(dest) as parsed:
        s, m, r, p, other = parsed.pages[0].Annots
        assert s.A.AN.objgen == s.objgen
        assert s.A.R.N == "Edited scene"
        assert s.A.R.C.D.EF.F.read_raw_bytes() == _U3D
        instance = r.RichMediaContent.Configurations[0].Instances[0]
        assert instance.Asset.objgen == r.RichMediaContent.Assets.Names[1].objgen == s.A.R.C.D.objgen
        assert s.AA.PO.TI.objgen == instance.objgen
        assert s.AA.PO.TA.objgen == r.objgen
        assert parsed.Root.OpenAction.TA.objgen == m.objgen
        assert parsed.Root.OpenAction.V == "front"
        stream = m["/3DD"]
        assert other["/3DD"]["/3DD"].objgen == stream.objgen
        assert stream.read_raw_bytes() == _U3D
        assert stream.VA[0].objgen == r.RichMediaContent.Views[0].objgen
        assert stream.VA[0].XN == "Profile"
        assert stream.VA[0].P.OS == 3
        assert p.Contents == "Dimension"
        assert stream.VA[0].MA[0].objgen == p.ExData.M3DREF.objgen
        assert p.ExData.M3DREF.TRL == "Distance"


@pytest.mark.spec("screen-annotations", "create")
@pytest.mark.spec("renditions", "create")
@pytest.mark.spec("rendition-actions", "create")
@pytest.mark.spec("rich-annotations", "create")
@pytest.mark.spec("projection-annotations", "create")
@pytest.mark.spec("3d-artwork", "create")
@pytest.mark.spec("rich-media", "create")
def test_create_media_in_display_space_and_import_its_graph() -> None:
    doc = pdf.Document()
    p = doc.pages.new((200, 150))
    p.rotation = 90
    asset = pdf.MediaAsset(filename="scene.u3d", content_type="model/u3d", data=_U3D)
    mref = doc.objects.add(
        {"Type": pdf.Name("3DMeasure"), "Subtype": pdf.Name("3DC"), "A1": [0, 0, 0], "TP": [1, 0, 0]}
    )
    measure = typed.ThreeDMeasure3DC(doc.objects[mref])
    view = pdf.ModelView(name="Side", measurements=(measure,))
    screen = p.annotations.add(pdf.Screen(rect=_RECT, asset=asset))
    model = p.annotations.add(pdf.ThreeD(rect=_RECT, format=pdf.ModelFormat.U3D, data=_U3D, views=(view,)))
    rich = p.annotations.add(pdf.RichMedia(rect=_RECT, kind=pdf.MediaKind.THREE_D, asset=asset, views=(view,)))
    proj = p.annotations.add(pdf.ProjectionAnnotation(rect=_RECT, measurement=measure))
    assert all(a.rect == _RECT for a in (model, screen, rich, proj))
    assert proj.media == measure.S
    assert not pdf.open(doc.to_bytes()).check()
    other = pdf.Document()
    other.pages.new((50, 50))
    other.pages.extend(p)
    assert not pdf.open(other.to_bytes()).check()
    with pikepdf.open(io.BytesIO(other.to_bytes())) as parsed:
        s, m, r, pr = parsed.pages[-1].Annots
        assert s.P.objgen == m.P.objgen == r.P.objgen == pr.P.objgen == parsed.pages[-1].obj.objgen
        assert s.A.AN.objgen == s.objgen
        assert m["/3DD"].VA[0].MA[0].objgen == pr.ExData.M3DREF.objgen
        assert pr.ExData.M3DREF.S.objgen == pr.objgen
        assert m["/3DD"].read_bytes() == _U3D
        asset_ref = r.RichMediaContent.Assets.Names[1]
        assert asset_ref.objgen == r.RichMediaContent.Configurations[0].Instances[0].Asset.objgen
        assert asset_ref.EF.F.read_bytes() == _U3D


def test_invalid_media_relationships_and_limits_are_atomic() -> None:
    doc = pdf.open(media_pdf())
    screen, model, rich, _, _ = doc.pages[0].annotations
    foreign = pdf.open(media_pdf())
    instance = typed.RichMediaInstance(foreign.objects[17])
    before = doc.to_bytes()
    pos = doc.history.position
    for action in (
        pdf.GoTo3DView(annotation=model.xref, view=1),
        pdf.GoTo3DView(annotation=model.xref, view="absent"),
        pdf.GoTo3DView(annotation=screen.xref, view=0),
        pdf.RichMediaExecute(annotation=rich.xref, command="noop", instance=instance),
    ):
        with pytest.raises(ValueError, match=r"target|instance|annotation"):
            doc.open_action = action
    with pytest.raises(ValueError, match="another document"):
        doc.pages[0].annotations.add(
            pdf.ThreeD(
                rect=_RECT,
                format=pdf.ModelFormat.PRC,
                data=b"opaque",
                views=(pdf.ModelView(name="foreign", measurements=(typed.ThreeDMeasure3DC(foreign.objects[15]),)),),
            )
        )
    with pytest.raises(pdf.LimitError):
        pdf.ThreeD(rect=_RECT, format=pdf.ModelFormat.U3D, data=_U3D, views=(pdf.ModelView(name="a"),) * 4097)
    assert doc.history.position == pos
    assert doc.to_bytes() == before


def test_referenced_media_deletion_refuses_and_shared_data_survives() -> None:
    doc = pdf.open(media_pdf())
    page = doc.pages[0]
    doc.open_action = pdf.GoTo3DView(annotation=6, view=0)
    pos = doc.history.position
    with pytest.raises(ValueError, match="still referenced"):
        del page.annotations[1]
    assert doc.history.position == pos
    assert page.annotations[1].xref == 6
    del doc.open_action
    del page.annotations[1]
    assert 6 not in doc.objects
    assert doc.objects[11].read() == _U3D
    del page.annotations[0]
    assert 5 not in doc.objects
    assert doc.objects[16].read() == _U3D
    assert not pdf.open(doc.to_bytes()).check()


@pytest.mark.spec("sound-annotations", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("sounds", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("sound-actions", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("movie-annotations", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("movies", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("movie-actions", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("alternate-presentations", "read", "interpret", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_existing_legacy_media_as_data(*, incremental: bool) -> None:
    doc = pdf.open(
        _build_raw_pdf(
            {
                1: (
                    "<< /Type /Catalog /Pages 2 0 R /Names << /AlternatePresentations <<"
                    " /Names [(show) 8 0 R] >> >> /OpenAction 7 0 R >>"
                ),
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources <<>> /Annots [4 0 R 5 0 R] >>",
                4: "<< /Type /Annot /Subtype /Sound /Rect [1 1 8 8] /Sound 6 0 R >>",
                5: "<< /Type /Annot /Subtype /Movie /Rect [10 10 20 20] /Movie << /F (movie.mov) >> >>",
                6: "<< /Type /Sound /R 8000 /Length 2 >>\nstream\nab\nendstream",
                7: "<< /S /Sound /Sound 6 0 R /Next << /S /Movie /Annotation 5 0 R >> >>",
                8: (
                    "<< /Type /SlideShow /Subtype /Embedded /Resources <<"
                    " /Names [(a) 9 0 R (b) 9 0 R] >> /StartResource (a) >>"
                ),
                9: "<< /Type /Filespec /F (slide.svg) >>",
            }
        )
    )
    sound, movie = doc.pages[0].annotations
    assert isinstance(sound.media, typed.AnnotSound)
    assert isinstance(movie.media, typed.AnnotMovie)
    audio = sound.sound()
    assert audio
    assert audio.data == b"ab"
    sound.media.Sound.obj.write(b"cd")
    assert movie.media.Movie.F == b"movie.mov"
    movie.media.Movie.Rotate = 90
    sound_action = typed.Catalog(doc.catalog).OpenAction
    assert isinstance(sound_action, typed.ActionSound)
    assert sound_action.Sound == sound.media.Sound
    sound_action.Volume = 0.5
    movie_action = sound_action.Next
    assert isinstance(movie_action, typed.ActionMovie)
    assert movie_action.Annotation == movie.media
    movie_action.Operation = "Stop"
    names = typed.Catalog(doc.catalog).Names
    assert names
    assert names.AlternatePresentations
    show = typed.SlideShow(names.AlternatePresentations["Names"][1])
    assert show.StartResource == b"a"
    show.StartResource = pdf.String(b"b")
    saved = doc.to_bytes(incremental=incremental)
    assert not pdf.open(saved).check()
    with pikepdf.open(io.BytesIO(saved)) as parsed:
        first, second = parsed.pages[0].Annots
        action = parsed.Root.OpenAction
        assert first.Sound.objgen == action.Sound.objgen
        assert action.Sound.read_bytes() == b"cd"
        assert action.Volume == 0.5
        assert second.objgen == action.Next.Annotation.objgen
        assert second.Movie.Rotate == 90
        assert action.Next.Operation == "/Stop"
        slide_show = parsed.Root.Names.AlternatePresentations.Names[1]
        assert slide_show.StartResource == "b"
        assert slide_show.Resources.Names[3].F == "slide.svg"
    del doc.open_action
    del doc.pages[0].annotations[:]
    del names.AlternatePresentations
    assert not pdf.open(doc.to_bytes()).check()


def test_cross_page_media_target_must_follow_the_import_or_deletion() -> None:
    doc = pdf.Document()
    first = doc.pages.new((100, 100))
    second = doc.pages.new((100, 100))
    screen = first.annotations.add(pdf.Screen(rect=_RECT))
    model = second.annotations.add(pdf.ThreeD(rect=_RECT, format=pdf.ModelFormat.U3D, data=_U3D))
    screen.action = pdf.GoTo3DView(annotation=model.xref, view=pdf.Name("D"))
    dest = pdf.Document()
    before = dest.history.position
    with pytest.raises(pdf.PdfError, match="omits a referenced multimedia"):
        dest.pages.extend(first)
    assert dest.history.position == before
    assert not dest.pages
    before = doc.history.position
    with pytest.raises(ValueError, match="retained object still references"):
        del doc.pages[1]
    assert doc.history.position == before
    dest.pages.extend(doc.pages)
    a = dest.pages[0].annotations[0].action
    assert isinstance(a, pdf.GoTo3DView)
    assert a.annotation == dest.pages[1].annotations[0].xref
    assert not pdf.open(dest.to_bytes()).check()
    del doc.pages[:]
    assert not doc.pages


@pytest.mark.parametrize(
    "selector", [0, "front", pdf.Name("F"), pdf.Name("L"), pdf.Name("N"), pdf.Name("P"), pdf.Name("D")]
)
def test_3d_view_selection_preserves_its_pdf_type(selector: str | int) -> None:
    doc = pdf.open(media_pdf())
    action = pdf.GoTo3DView(annotation=9, view=selector)
    doc.open_action = action
    with pdf.open(doc.to_bytes()) as saved:
        got = saved.open_action
        assert isinstance(got, pdf.GoTo3DView)
        assert got == action
        assert type(got.view) is type(selector)


def test_rendition_script_and_rich_media_arguments_remain_inert_data() -> None:
    doc = pdf.open(media_pdf())
    code = b"/* inert marker */"
    script = doc.objects[doc.objects.add_stream(code)]
    doc.open_action = pdf.RenditionAction(script=script)
    got = doc.open_action
    assert isinstance(got, pdf.RenditionAction)
    assert isinstance(got.script, pdf.Stream)
    assert got.script.read() == code
    arguments = doc.objects[doc.objects.add(["track", 4, True])]
    doc.open_action = pdf.RichMediaExecute(annotation=7, command="select", arguments=arguments)
    with pdf.open(doc.to_bytes()) as saved:
        rich = saved.open_action
        assert isinstance(rich, pdf.RichMediaExecute)
        assert isinstance(rich.arguments, pdf.Array)
        assert list(rich.arguments) == [b"track", 4, True]
        assert not saved.check()


def test_media_uses_declared_extension_or_checks_base_version_promotion() -> None:
    old = _build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources <<>> /Annots [4 0 R] >>",
            4: "<< /Type /Annot /Subtype /Text /Rect [3 4 12 13] >>",
        }
    )
    spec = pdf.RichMedia(
        rect=_RECT,
        kind=pdf.MediaKind.THREE_D,
        asset=pdf.MediaAsset(filename="scene.u3d", content_type="model/u3d", data=_U3D),
    )
    doc = pdf.open(old)
    assert not doc.check()
    with pytest.raises(pdf.PdfError, match=r"PDF 2\.0 promotion"):
        doc.pages[0].annotations.add(spec)
    assert doc.history.position == 0
    assert "Version" not in doc.catalog
    assert "ID" not in doc.trailer
    del doc.pages[0].annotations[0]
    media = doc.pages[0].annotations.add(spec)
    assert doc.catalog["Version"] == "2.0"
    assert "ID" in doc.trailer
    doc.open_action = pdf.RichMediaExecute(annotation=media.xref, command="noop")
    assert not pdf.open(doc.to_bytes()).check()

    ext = pdf.open(old)
    ext.catalog["Extensions"] = {"ADBE": {"BaseVersion": pdf.Name("1.7"), "ExtensionLevel": 3}}
    media = ext.pages[0].annotations.add(spec)
    ext.open_action = pdf.RichMediaExecute(annotation=media.xref, command="noop")
    assert "Version" not in ext.catalog
    assert not pdf.open(ext.to_bytes()).check()


def test_imported_prc_requires_its_base_version_or_developer_extension() -> None:
    src = pdf.Document()
    src.catalog["Extensions"] = {"ADBE": {"BaseVersion": pdf.Name("1.7"), "ExtensionLevel": 1}}
    page = src.pages.new((100, 100))
    page.annotations.add(pdf.ThreeD(rect=_RECT, format=pdf.ModelFormat.PRC, data=b"opaque PRC payload"))
    assert "Version" not in src.catalog
    for declared in (False, True):
        dest = pdf.Document()
        if declared:
            dest.catalog["Extensions"] = {"ADBE": {"BaseVersion": pdf.Name("1.7"), "ExtensionLevel": 1}}
        dest.pages.extend(page)
        assert dest.catalog.get("Version") == (None if declared else "2.0")
        with pdf.open(dest.to_bytes()) as saved:
            assert not saved.check()
            annot = saved.pages[0].annotations[0].media
            assert isinstance(annot, typed.Annot3D)
            assert annot["3DD"].obj.read() == b"opaque PRC payload"


@pytest.mark.spec("geospatial", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_declared_geospatial3d_uses_world_triples_and_keeps_shared_media(*, incremental: bool) -> None:
    source = media_pdf()
    matrix = (2, 3, 4, 5, 7, 11, 13, 17, 19, 100, 200, 300)
    with pdf.open(source) as doc:
        _, model, rich, _, _ = doc.pages[0].annotations
        projected = pdf.GeospatialMeasure(
            pdf.CoordinateSystem("projected", epsg=32611),
            (400001, 4800002, 234),
            lpts=(-2, 3, 5),
            dimensions=3,
            pcsm=matrix,
        )
        assert model.obj.geospatial_measure is None
        with pytest.raises(ValueError, match="Geospatial3D"):
            model.obj.geospatial_measure = projected
        assert doc.history.position == 0
        assert doc.to_bytes(incremental=True) == source
        doc.requirements = [pdf.DocumentRequirement(pdf.RequirementType.GEOSPATIAL_3D)]
        model.obj.geospatial_measure = projected
        rich.obj.geospatial_measure = pdf.GeospatialMeasure(
            pdf.CoordinateSystem(epsg=4326),
            ("35.25", "-117.5", "243.125"),
            lpts=(13, -5, 7),
            dimensions=3,
            pcsm=matrix,
        )
        assert model.obj.geospatial_measure == projected
        assert model.obj.geospatial_point(0) == ((-2, 3, 5), (176, 300, 420))
        assert rich.obj.geospatial_point(0) == ((13, -5, 7), (35.25, -117.5, 243.125))
        saved = doc.to_bytes(incremental=incremental)
    if incremental:
        assert saved.startswith(source)
    with pikepdf.open(io.BytesIO(saved)) as independent:
        _, model, rich, _, shared = independent.pages[0].Annots
        assert independent.Root.Requirements[0].S == "/Geospatial3D"
        assert list(model.GEO.GPTS) == [400001, 4800002, 234]
        assert list(model.GEO.LPTS) == [-2, 3, 5]
        assert list(model.GEO.PCSM) == list(matrix)
        assert list(rich.GEO.GPTS) == [35.25, -117.5, 243.125]
        assert list(rich.GEO.LPTS) == [13, -5, 7]
        stream = model["/3DD"]
        assert stream.objgen == shared["/3DD"]["/3DD"].objgen
        assert stream.VA[0].objgen == rich.RichMediaContent.Views[0].objgen
        assert stream.read_raw_bytes() == _U3D
