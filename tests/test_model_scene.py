"""ISO 32000-2 clause 13.6 scene settings and dimensional geometry as stored data."""

from __future__ import annotations

import hashlib
import io
import os
from dataclasses import replace
from pathlib import Path

import pikepdf
import pytest
from test_actions import _build_raw_pdf
from test_multimedia import _RECT, _U3D

import pyropdf as pdf
from pyropdf import typed

_KIND = pdf.ModelMeasurementType
_X = (1, 0, 0)
_Y = (0, 1, 0)
_Z = (0, 0, 1)
_CAMERA = (*_X, *_Y, *_Z, 4, 5, 6)
_POINT = pdf.ModelMeasurement(kind=_KIND.POINT_COMMENT, anchor=(0, 0, 0), text_position=(1, 1, 2))
_DISTANCE = replace(
    _POINT, kind=_KIND.DISTANCE, annotation_plane=_Z, second_anchor=(3, 4, 0), text_y=_Y, value=5, unit="mm"
)
_GEOMETRY = (
    _POINT,
    _DISTANCE,
    replace(_DISTANCE, kind=_KIND.PERPENDICULAR_DISTANCE, first_direction=_X),
    replace(_DISTANCE, kind=_KIND.ANGLE, first_direction=_X, second_direction=_Y, text_x=_X, value=90, unit=None),
    replace(_DISTANCE, kind=_KIND.RADIUS, third_anchor=_X, fourth_anchor=_Y, text_x=_X),
)


def _base(version: str = "2.0") -> bytes:
    data = _build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /Resources <<>> /MediaBox [0 0 120 100] >>",
        },
        version=version,
    )
    return data.replace(b"/Root 1 0 R >>", b"/Root 1 0 R /ID [(0123456789ABCDEF) (0123456789ABCDEF)] >>")


def scene_pdf() -> bytes:
    """Create all five variants, each with a projection, and share the scene resources."""
    with pdf.open(_base()) as doc:
        page = doc.pages[0]
        node = typed.ThreeDNode(doc.objects[doc.objects.add({"N": "Part", "M": _CAMERA, "V": True})])
        section = typed.ThreeDCrossSection(doc.objects[doc.objects.add({"O": [None, 0, 0], "C": [0, 0, 1]})])
        activation = typed.ThreeDActivation(doc.objects[doc.objects.add({"A": pdf.Name("XA"), "D": pdf.Name("PI")})])
        view = pdf.ModelView(
            name="Front",
            measurements=_GEOMETRY,
            camera_to_world=_CAMERA,
            lighting="CAD",
            background=(0.2, 0.4, 0.6),
            render_mode="SolidOutline",
            nodes=(node,),
            cross_sections=(section,),
        )
        model = page.annotations.add(
            pdf.ThreeD(rect=_RECT, format=pdf.ModelFormat.U3D, data=_U3D, views=(view,), activation=activation)
        )
        assert isinstance(model.media, typed.Annot3D)
        stored = model.media["3DD"].VA[0].MA
        measures = tuple(stored[i] for i in range(len(_GEOMETRY)))
        page.annotations.add(
            pdf.ThreeD(
                rect=_RECT,
                format=pdf.ModelFormat.U3D,
                data=_U3D,
                activation=activation,
                views=(replace(view, name="Rear", measurements=measures),),
            )
        )
        for measure in measures:
            page.annotations.add(pdf.ProjectionAnnotation(rect=_RECT, measurement=measure))
            measure.TRL = "inspection"
        return doc.to_bytes()


def edit_scene(source: bytes, *, incremental: bool) -> bytes:
    with pdf.open(source) as doc:
        model, shared, *projections = doc.pages[0].annotations
        assert isinstance(model.media, typed.Annot3D)
        assert isinstance(shared.media, typed.Annot3D)
        view = model.media["3DD"].VA[0]
        assert tuple(view.C2W.obj) == _CAMERA
        assert (view.MS, view.LS.Subtype, view.RM.Subtype) == ("M", "CAD", "SolidOutline")
        assert tuple(view.BG.C.obj) == (0.2, 0.4, 0.6)
        assert view.NA[0] == shared.media["3DD"].VA[0].NA[0]
        assert view.SA[0] == shared.media["3DD"].VA[0].SA[0]
        assert model.media["3DA"] == shared.media["3DA"]
        view.C2W[9] = 12
        view.LS.Subtype = "Day"
        view.RM.Subtype = "Wireframe"
        view.BG.C[1] = 0.75
        view.NA[0].V = False
        view.SA[0].PO = 0.25
        model.media["3DA"].A = "PO"
        for index, (geometry, projection) in enumerate(zip(_GEOMETRY, projections, strict=True)):
            measure = view.MA[index]
            before_read = doc.history.position
            assert pdf.ModelMeasurement.from_view(measure) == geometry
            assert doc.history.position == before_read
            assert measure.Subtype == geometry.kind
            if geometry.kind is _KIND.RADIUS:
                assert tuple(measure.A3.obj) == _X
                assert tuple(measure.A4.obj) == _Y
            assert isinstance(projection.media, typed.AnnotProjection)
            assert isinstance(projection.media.ExData, typed.ExDataProjection)
            assert measure == projection.media.ExData.M3DREF
            assert projection.media == measure.S
            pos = doc.history.position
            replace(geometry, anchor=(7, 8, index), third_anchor=None, fourth_anchor=None).apply_to(measure)
            assert doc.history.position == pos + 1
        return doc.to_bytes(incremental=incremental)


@pytest.mark.spec("3d-artwork", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_measurements_and_scene_resources_roundtrip(*, incremental: bool) -> None:
    source = scene_pdf()
    saved = edit_scene(source, incremental=incremental)
    if incremental:
        assert saved.startswith(source)
    with pdf.open(saved) as reopened:
        assert not reopened.check()
    with pikepdf.open(io.BytesIO(saved)) as independent:
        model, shared, *projections = independent.pages[0].Annots
        stream = model["/3DD"]
        assert stream.read_bytes() == _U3D
        with pikepdf.open(io.BytesIO(source)) as baseline:
            assert stream.read_raw_bytes() == baseline.pages[0].Annots[0]["/3DD"].read_raw_bytes()
        view = stream.VA[0]
        other = shared["/3DD"].VA[0]
        assert list(view.C2W) == [*_CAMERA[:9], 12, 5, 6]
        assert (str(view.LS.Subtype), str(view.RM.Subtype)) == ("/Day", "/Wireframe")
        assert list(map(float, view.BG.C)) == [0.2, 0.75, 0.6]
        assert view.NA[0].objgen == other.NA[0].objgen
        assert not other.NA[0].V
        assert list(other.NA[0].M) == list(_CAMERA)
        assert view.SA[0].objgen == other.SA[0].objgen
        assert list(other.SA[0].O) == [None, 0, 0]
        assert float(other.SA[0].PO) == 0.25
        assert model["/3DA"].objgen == shared["/3DA"].objgen
        assert str(shared["/3DA"].A) == "/PO"
        for i, (measure, projection, geometry) in enumerate(zip(view.MA, projections, _GEOMETRY, strict=True)):
            assert measure.objgen == other.MA[i].objgen == projection.ExData.M3DREF.objgen
            assert measure.S.objgen == projection.objgen
            assert str(measure.Subtype) == f"/{geometry.kind}"
            assert list(measure.A1) == [7, 8, i]
            assert list(measure.TP) == [1, 1, 2]
            assert str(measure.TRL) == "inspection"
            if geometry.kind != _KIND.POINT_COMMENT:
                assert list(measure.AP) == list(_Z)
                assert list(measure.A2) == [3, 4, 0]
                assert list(measure.TY) == list(_Y)
                assert geometry.value == measure.V
            if geometry.unit:
                assert str(measure.U) == geometry.unit
            if geometry.first_direction:
                assert list(measure.D1) == list(_X)
            if geometry.second_direction:
                assert list(measure.D2) == list(_Y)
            if geometry.text_x:
                assert list(measure.TX) == list(_X)
            assert "/A3" not in measure
            assert "/A4" not in measure


def test_scene_rejects_invalid_geometry_and_links_before_publishing() -> None:
    for changes in (
        {"anchor": (1, 2)},
        {"text_position": (1, True, 3)},
        {"anchor": (float("inf"), 2, 3)},
        {"first_direction": _Y},
        {"unit": None},
        {"annotation_plane": (0, 0, 0)},
    ):
        with pytest.raises(ValueError, match=r"requires|not a geometry field"):
            replace(_DISTANCE, **changes)
    with pytest.raises(ValueError, match="C2W"):
        pdf.ModelView(name="broken", camera_to_world=(*_X, *_Y))
    doc = pdf.open(scene_pdf())
    model, shared, *projections = doc.pages[0].annotations
    point, distance = (model.obj["3DD"]["VA"][0]["MA"][idx] for idx in (0, 1))
    typed_distance = typed.ThreeDMeasureLD3(distance)
    before = doc.to_bytes()
    pos = doc.history.position
    with pytest.raises(ValueError, match="subtype"):
        _POINT.apply_to(typed_distance)
    for key, wrong, error in (
        ("A1", [0, 1], "3 finite"),
        ("S", shared.obj, "reciprocal"),
        ("S", projections[0].obj, "reciprocal"),
    ):
        distance[key] = wrong
        malformed = doc.to_bytes()
        with pytest.raises(ValueError, match=error):
            _DISTANCE.apply_to(typed_distance)
        assert doc.to_bytes() == malformed
        doc.history.undo()
    with pytest.raises(ValueError, match="already has"):
        doc.pages[0].annotations.add(pdf.ProjectionAnnotation(rect=_RECT, measurement=typed.ThreeDMeasure3DC(point)))
    foreign = pdf.open(scene_pdf())
    node = foreign.pages[0].annotations[0].obj["3DD"]["VA"][0]["NA"][0]
    with pytest.raises(ValueError, match="another document"):
        doc.pages[0].annotations.add(
            pdf.ThreeD(
                rect=_RECT,
                format=pdf.ModelFormat.U3D,
                data=_U3D,
                views=(pdf.ModelView(name="foreign", nodes=(typed.ThreeDNode(node),)),),
            )
        )
    assert doc.to_bytes() == before
    assert doc.history.position == pos


def test_changed_scene_resource_is_checked_inside_the_annotation_transaction() -> None:
    with pdf.open(_base()) as doc:
        section = typed.ThreeDCrossSection(doc.objects[doc.objects.add({"O": [None, 0, 0]})])
        view = pdf.ModelView(name="section", cross_sections=(section,))
        spec = pdf.ThreeD(rect=_RECT, format=pdf.ModelFormat.U3D, data=_U3D, views=(view,))
        section.obj["O"] = [0, 0, 0]
        before = doc.to_bytes()
        pos = doc.history.position
        with pytest.raises(ValueError, match="one null axis"):
            doc.pages[0].annotations.add(spec)
        assert doc.to_bytes() == before
        assert doc.history.position == pos

        activation = typed.ThreeDActivation(doc.objects[doc.objects.add({"A": pdf.Name("Bad")})])
        spec = replace(spec, activation=activation, views=())
        before = doc.to_bytes()
        with pytest.raises(pdf.PdfError, match="3DActivation/A"):
            doc.pages[0].annotations.add(spec)
        assert doc.to_bytes() == before


def test_scene_selects_the_version_required_by_its_settings() -> None:
    with pdf.open(_base("1.5")) as doc:
        spec = pdf.ThreeD(
            rect=_RECT,
            format=pdf.ModelFormat.U3D,
            data=_U3D,
            views=(pdf.ModelView(name="camera", camera_to_world=_CAMERA),),
        )
        doc.pages[0].annotations.add(spec)
        assert doc.catalog["Version"] == "1.6"
        doc.pages[0].annotations.add(replace(spec, views=(pdf.ModelView(name="lit", lighting="Night"),)))
        assert doc.catalog["Version"] == "1.7"
        doc.pages[0].annotations.add(replace(spec, views=(pdf.ModelView(name="point", measurements=(_POINT,)),)))
        assert doc.catalog["Version"] == "2.0"
        with pdf.open(doc.to_bytes()) as saved:
            assert not saved.check()


def test_rich_media_named_scene_uses_the_pinned_view_edition() -> None:
    with pdf.open(_base("1.7")) as doc:
        doc.catalog["Extensions"] = {"ADBE": {"BaseVersion": pdf.Name("1.7"), "ExtensionLevel": 3}}
        asset = pdf.MediaAsset(filename="model.u3d", data=_U3D, content_type="model/u3d")
        view = pdf.ModelView(name="front", measurements=(_POINT,), camera_to_world=_CAMERA)
        doc.pages[0].annotations.add(pdf.RichMedia(rect=_RECT, kind=pdf.MediaKind.THREE_D, asset=asset, views=(view,)))
        with pdf.open(doc.to_bytes()) as saved:
            assert saved.catalog["Version"] == "2.0"
            assert not saved.check()


def test_pinned_arlington_scene_saves(tmp_path: Path) -> None:
    from test_verapdf import VERAPDF  # noqa: PLC0415

    jar = Path(os.environ.get("ARLINGTON_JAR", "target/verapdf/greenfield-apps-arlington.jar"))
    if not jar.is_file():
        pytest.skip("set ARLINGTON_JAR to the CI-pinned greenfield-apps-arlington jar")
    with jar.open("rb") as archive:
        assert hashlib.file_digest(archive, "sha256").hexdigest() == (
            "a780142be33c9ce00a0fd075c6d7b706074a01e87aa5c7d7328db4ff345a4464"
        )
    source = scene_pdf()
    paths = []
    for mode in ("full", "incremental"):
        dest = tmp_path / f"3d-scene-{mode}.pdf"
        dest.write_bytes(edit_scene(source, incremental=mode == "incremental"))
        paths.append(dest)
    for path, passed in VERAPDF.validate(jar.resolve(), "arlington2.0", paths).items():
        assert passed, VERAPDF.details(jar.resolve(), "arlington2.0", tmp_path / path)
