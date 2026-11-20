"""Tests for viewports, rectilinear and geospatial measurements, and document requirements (§12.9, §12.10)."""

from __future__ import annotations

import io
from decimal import Decimal
from typing import TYPE_CHECKING

import pikepdf
import pytest

import pyropdf
from pyropdf import (
    CoordinateSystem,
    DocumentClosedError,
    DocumentRequirement,
    GeospatialMeasure,
    LimitError,
    Name,
    NumberFormat,
    NumberFormatFraction,
    PointData,
    Real,
    Rect,
    RectilinearMeasure,
    RequirementHandler,
    RequirementType,
    StalePageError,
    Viewport,
)

if TYPE_CHECKING:
    from pathlib import Path


def _pdf_14() -> bytes:
    """Build a valid minimal PDF 1.4 one-page document for version-promotion checks."""
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 400] /Resources <<>> >>",
    }
    body = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number, content in objects.items():
        offsets[number] = len(body)
        body.extend(f"{number} 0 obj\n".encode("ascii") + content + b"\nendobj\n")
    xref_start = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for number in range(1, len(objects) + 1):
        body.extend(f"{offsets[number]:010d} 00000 n \n".encode("ascii"))
    body.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n".encode("ascii")
    )
    return bytes(body)


def _assert_valid(doc: pyropdf.Document) -> None:
    """Assert zero structural defects in memory (aside from unsaved trailer /Size) and after saving."""
    assert [d for d in doc.check().defects if d.clause != "FileTrailer/Size"] == []
    with pyropdf.open(doc.to_bytes()) as saved:
        assert not saved.check()


def test_public_exports_and_immutability() -> None:
    exported = (
        CoordinateSystem,
        DocumentRequirement,
        GeospatialMeasure,
        NumberFormat,
        NumberFormatFraction,
        PointData,
        RectilinearMeasure,
        RequirementHandler,
        RequirementType,
        Viewport,
    )
    for symbol in exported:
        assert symbol.__name__ in pyropdf.__all__
        assert symbol.__module__ == "pyropdf"

    nf = NumberFormat("ft", Decimal(10))
    assert hash(nf) == hash(NumberFormat("ft", 10))
    with pytest.raises(AttributeError):
        nf.unit = "m"  # ty: ignore[invalid-assignment]

    vp = Viewport((10, 10, 50, 50), name="Detail")
    assert hash(vp) == hash(Viewport(Rect(10, 10, 50, 50), name="Detail"))
    with pytest.raises(AttributeError):
        vp.name = "Changed"  # ty: ignore[invalid-assignment]


@pytest.mark.spec("measurement", "read", "interpret", "create")
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_viewport_display_space_conversion_across_rotation_crop_and_user_unit(rotation: int) -> None:
    with pyropdf.Document() as doc:
        page = doc.pages.new((300, 400))
        page.obj["CropBox"] = [50.0, 40.0, 250.0, 340.0]
        page.obj["UserUnit"] = 2.5
        if rotation:
            page.rotation = rotation

        target_bbox = Rect(12.5, 18.0, 96.5, 140.0)
        vp = Viewport(
            target_bbox,
            name="CroppedRotated",
            measure=RectilinearMeasure(
                "1 in = 20 ft",
                x=(NumberFormat("ft", Decimal("0.277777777777778")),),
                distance=(NumberFormat("ft", Decimal("0.277777777777778")),),
                area=(NumberFormat("sq ft", Decimal("0.07716049382716")),),
            ),
        )
        page.viewports = (vp,)
        assert "Version" not in doc.catalog
        _assert_valid(doc)

        [read_back] = page.viewports
        assert read_back.name == "CroppedRotated"
        assert read_back.bbox.x0 == pytest.approx(target_bbox.x0)
        assert read_back.bbox.y0 == pytest.approx(target_bbox.y0)
        assert read_back.bbox.x1 == pytest.approx(target_bbox.x1)
        assert read_back.bbox.y1 == pytest.approx(target_bbox.y1)

        with pyropdf.open(doc.to_bytes()) as reopened:
            assert not reopened.check()
            [reopened_vp] = reopened.pages[0].viewports
            assert reopened_vp.bbox.x0 == pytest.approx(target_bbox.x0)
            assert reopened_vp.bbox.y0 == pytest.approx(target_bbox.y0)
            assert reopened_vp.bbox.x1 == pytest.approx(target_bbox.x1)
            assert reopened_vp.bbox.y1 == pytest.approx(target_bbox.y1)


@pytest.mark.spec("measurement", "read", "interpret")
def test_reading_legacy_and_malformed_viewport_dictionaries() -> None:
    with pyropdf.Document() as doc:
        page = doc.pages.new((200, 300))
        # Omitted /Subtype defaults to /RL (Table 266); non-final /F,/D,/FD and
        # orphan /CYX without /Y are normalized on read so re-writing is valid.
        page.obj["VP"] = [
            {
                "Type": Name("Viewport"),
                "BBox": [20.0, 50.0, 120.0, 250.0],
                "Measure": {
                    "Type": Name("Measure"),
                    "R": "1:100",
                    "X": [
                        {"U": "m", "C": 1, "F": Name("F"), "D": 8, "FD": True},
                        {"U": "cm", "C": 100, "F": Name("D")},
                    ],
                    "D": [{"U": "m", "C": 1}],
                    "A": [{"U": "sq m", "C": 1}],
                    "CYX": 2.0,
                },
            },
            # Degenerate BBox is skipped cleanly.
            {"Type": Name("Viewport"), "BBox": [10.0, 10.0, 10.0, 50.0]},
        ]
        [vp] = page.viewports
        assert vp.bbox == Rect(20.0, 50.0, 120.0, 250.0)
        assert isinstance(vp.measure, RectilinearMeasure)
        assert vp.measure.x[0].fraction is None
        assert vp.measure.x[0].denominator is None
        assert vp.measure.x[0].force_denominator is None
        assert vp.measure.x[1].fraction is NumberFormatFraction.DECIMAL
        assert vp.measure.cyx is None

        # Re-writing the normalized viewport produces zero Arlington defects.
        page.viewports = (vp,)
        _assert_valid(doc)


@pytest.mark.spec("measurement", "create", "edit")
def test_rectilinear_measure_chains_indirect_cleanup_and_undo() -> None:
    with pyropdf.open(_pdf_14()) as doc:
        page = doc.pages[0]
        indirect_measure = doc.objects.add(
            {
                "Type": Name("Measure"),
                "Subtype": Name("RL"),
                "R": "1 in = 1 ft",
                "X": [{"Type": Name("NumberFormat"), "U": "ft", "C": 1}],
                "D": [{"Type": Name("NumberFormat"), "U": "ft", "C": 1}],
                "A": [{"Type": Name("NumberFormat"), "U": "sq ft", "C": 1}],
            }
        )
        indirect_vp = doc.objects.add(
            {
                "Type": Name("Viewport"),
                "BBox": [0.0, 0.0, 100.0, 100.0],
                "Measure": doc.objects[indirect_measure],
            }
        )
        page.obj["VP"] = [doc.objects[indirect_vp]]
        assert indirect_measure in doc.objects
        assert indirect_vp in doc.objects

        rl = RectilinearMeasure(
            "1 in = 10 ft",
            x=(
                NumberFormat("mi", Decimal("0.001893939393939")),
                NumberFormat(
                    "ft",
                    Decimal(5280),
                    fraction=NumberFormatFraction.FRACTION,
                    denominator=16,
                    force_denominator=False,
                    thousands_separator=",",
                    decimal_separator=".",
                    prefix_spacing=" ",
                    suffix_spacing="",
                    label_position="S",
                ),
            ),
            y=(NumberFormat("ft", Decimal(10), fraction=NumberFormatFraction.ROUND, label_position="P"),),
            distance=(NumberFormat("ft", Decimal(10), fraction=NumberFormatFraction.TRUNCATE),),
            area=(NumberFormat("sq ft", Decimal(100), fraction=NumberFormatFraction.DECIMAL),),
            angles=(NumberFormat("deg", 1),),
            slope=(NumberFormat("%", "1.0"),),
            origin=(Decimal("12.5"), Decimal("-7.25")),
            cyx=Real("1.5"),
        )
        steps_before = doc.history.position
        page.viewports = (Viewport((25, 30, 300, 320), name="CAD Plan", measure=rl),)
        assert doc.history.position == steps_before + 1
        assert doc.history.steps[-1] == "page viewports"
        assert indirect_vp not in doc.objects
        assert indirect_measure not in doc.objects
        assert doc.catalog["Version"] == "1.6"
        assert "ID" not in doc.trailer
        _assert_valid(doc)

        [loaded] = page.viewports
        assert loaded == Viewport((25, 30, 300, 320), name="CAD Plan", measure=rl)

        doc.history.undo()
        assert indirect_vp in doc.objects
        assert len(page.viewports) == 1
        assert page.viewports[0].name is None

        doc.history.redo()
        assert page.viewports == (Viewport((25, 30, 300, 320), name="CAD Plan", measure=rl),)

        del page.viewports
        assert page.viewports == ()
        assert "VP" not in page.obj
        del page.viewports  # idempotent when absent


def test_measurement_validation_errors() -> None:
    nf = NumberFormat("ft", 1)
    nf_frac = NumberFormat("in", 12, fraction=NumberFormatFraction.FRACTION, denominator=16, force_denominator=True)

    with pytest.raises(ValueError, match="final NumberFormat"):
        RectilinearMeasure("1:1", x=(nf_frac, nf), distance=(nf,), area=(nf,))
    with pytest.raises(ValueError, match="cyx"):
        RectilinearMeasure("1:1", x=(nf,), distance=(nf,), area=(nf,), cyx=1)
    with pytest.raises(ValueError, match="at least one"):
        RectilinearMeasure("1:1", x=(), distance=(nf,), area=(nf,))
    with pytest.raises(ValueError, match="origin"):
        RectilinearMeasure("1:1", x=(nf,), distance=(nf,), area=(nf,), origin=(1,))

    with pytest.raises(TypeError, match="bool"):
        NumberFormat("ft", True)  # noqa: FBT003
    with pytest.raises(ValueError, match="finite"):
        NumberFormat("ft", Decimal("NaN"))
    with pytest.raises(ValueError, match="finite"):
        NumberFormat("ft", float("inf"))
    with pytest.raises(ValueError, match="limit"):
        NumberFormat("ft", Decimal("1E1000"))
    with pytest.raises(ValueError, match="positive"):
        NumberFormat("ft", 1, denominator=0)
    with pytest.raises(TypeError):
        NumberFormat("ft", 1, denominator=True)
    with pytest.raises(ValueError, match="label_position"):
        NumberFormat("ft", 1, label_position="X")  # ty: ignore[invalid-argument-type]

    with pytest.raises(ValueError, match="both epsg"):
        CoordinateSystem("geographic", epsg=4326, wkt='GEOGCS["WGS 84"]')
    with pytest.raises(ValueError, match="requires either epsg"):
        CoordinateSystem("projected")
    with pytest.raises(ValueError, match="printable ASCII"):
        CoordinateSystem("geographic", wkt="GEOGCS[\u6771\u4eac]")
    with pytest.raises(ValueError, match="positive"):
        CoordinateSystem("geographic", epsg=0)

    gcs = CoordinateSystem("geographic", epsg=4326)
    with pytest.raises(ValueError, match="even"):
        GeospatialMeasure(gcs, (Decimal("35.0"), Decimal("139.0"), Decimal("36.0")))
    with pytest.raises(ValueError, match="match gpts"):
        GeospatialMeasure(gcs, (1, 2, 3, 4), lpts=(0, 0))
    with pytest.raises(ValueError, match="pdu"):
        GeospatialMeasure(gcs, (1, 2), pdu=("M", "SQM", "BAD"))
    with pytest.raises(ValueError, match="12 numbers"):
        GeospatialMeasure(gcs, (1, 2), pcsm=(1, 2, 3))

    with pytest.raises(ValueError, match="LAT"):
        PointData(("BAD",), ((1,),))  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="match names length"):
        PointData(("LAT", "LON"), ((Decimal("35.0"),),))
    with pytest.raises(TypeError, match="bool"):
        PointData(("LAT",), ((True,),))
    with pytest.raises(ValueError, match="valid decimal"):
        PointData(("LAT",), (("not-a-number",),))


@pytest.mark.spec("measurement", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.spec("geospatial", "read", "create", "preserve")
def test_geospatial_measure_full_decimal_precision_and_point_data_preservation(tmp_path: Path) -> None:
    lat0 = Decimal("35.6812361234567890123456789")
    lon0 = Decimal("139.7671249876543210987654321")
    lat1 = Decimal("35.6894871234567890987654321")
    lon1 = Decimal("139.6917069876543210123456789")
    alt = Decimal("42.1250000000000000000000001")

    geo = GeospatialMeasure(
        gcs=CoordinateSystem("geographic", epsg=4326),
        dcs=CoordinateSystem("projected", wkt='PROJCS["WGS 84 / UTM zone 54N"]'),
        gpts=(lat0, lon0, lat1, lon1),
        lpts=(Decimal(0), Decimal(0), Decimal(1), Decimal(1)),
        bounds=(Decimal(0), Decimal(0), Decimal(1), Decimal(1)),
        pdu=("M", "SQM", "DEG"),
        pcsm=(
            Decimal(1),
            Decimal(0),
            Decimal(0),
            Decimal(0),
            Decimal(1),
            Decimal(0),
            Decimal(0),
            Decimal(0),
            Decimal(1),
            Decimal("500000.1234567890123456789"),
            Decimal("3949000.9876543210987654321"),
            Decimal(0),
        ),
    )
    pt_data = PointData(
        names=("LAT", "LON", "ALT"),
        points=((lat0, lon0, alt), (lat1, lon1, None)),
    )
    assert geo.gpts == (lat0, lon0, lat1, lon1)
    assert pt_data.points == ((lat0, lon0, alt), (lat1, lon1, None))

    base_path = tmp_path / "geo-full.pdf"
    inc_path = tmp_path / "geo-inc.pdf"

    with pyropdf.Document() as doc:
        page0 = doc.pages.new((500, 500))
        doc.pages.new((500, 500))
        page0.viewports = (
            Viewport(
                (50, 60, 450, 460),
                name="Tokyo Survey",
                measure=geo,
                point_data=pt_data,
            ),
        )
        assert doc.catalog["Version"] == "2.0"
        assert "ID" in doc.trailer
        _assert_valid(doc)
        doc.save(base_path)

    full_bytes = base_path.read_bytes()
    for exact_token in (
        b"35.6812361234567890123456789",
        b"139.7671249876543210987654321",
        b"35.6894871234567890987654321",
        b"139.6917069876543210123456789",
        b"42.1250000000000000000000001",
        b"500000.1234567890123456789",
        b"3949000.9876543210987654321",
    ):
        assert exact_token in full_bytes

    with pyropdf.open(base_path) as reopened:
        assert not reopened.check()
        [vp] = reopened.pages[0].viewports
        assert vp.bbox == Rect(50, 60, 450, 460)
        assert vp.name == "Tokyo Survey"
        assert isinstance(vp.measure, GeospatialMeasure)
        assert vp.measure.gcs == CoordinateSystem("geographic", epsg=4326)
        assert vp.measure.dcs == CoordinateSystem("projected", wkt='PROJCS["WGS 84 / UTM zone 54N"]')
        assert vp.measure.pdu == ("M", "SQM", "DEG")
        assert vp.measure.lpts == (Decimal("0.0"), Decimal("0.0"), Decimal("1.0"), Decimal("1.0"))
        assert vp.measure.bounds == (Decimal("0.0"), Decimal("0.0"), Decimal("1.0"), Decimal("1.0"))
        assert vp.point_data is not None
        assert vp.point_data.names == ("LAT", "LON", "ALT")
        assert vp.point_data.points[1][2] is None

        # Edit an unrelated page and save both incrementally and in full:
        # untouched geospatial objects keep their exact 28-digit tokens (F3).
        reopened.pages[1].rotation = 90
        reopened.save(inc_path, incremental=True)
        resaved_full = reopened.to_bytes(incremental=False)

    inc_bytes = inc_path.read_bytes()
    for exact_token in (
        b"35.6812361234567890123456789",
        b"139.7671249876543210987654321",
        b"42.1250000000000000000000001",
    ):
        assert exact_token in inc_bytes
        assert exact_token in resaved_full

    with pyropdf.open(inc_path) as after_inc:
        assert not after_inc.check()
        assert len(after_inc.pages[0].viewports) == 1
        after_inc.pages[0].viewports = ()
        assert after_inc.pages[0].viewports == ()
        assert "VP" not in after_inc.pages[0].obj


@pytest.mark.spec("geospatial", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_projected_viewport_registration_keeps_matching_local_and_ground_points(*, incremental: bool) -> None:
    # West/east are eastings; south/north are northings, in EPSG:32654.
    local = ("0", "0", "1", "0", "1", "1", "0", "1")
    ground = ("400000", "3950000", "400100", "3950000", "400100", "3950050", "400000", "3950050")
    projection = CoordinateSystem("projected", epsg=32654)
    with pyropdf.open(_pdf_14()) as source:
        page = source.pages[0]
        page.viewports = [Viewport((10, 20, 110, 70), measure=GeospatialMeasure(projection, ground, lpts=local))]
        source.catalog["PrivateSurvey"] = b"keep"
        base = source.to_bytes()

    corrected = (*ground[:4], "400101.125", *ground[5:])
    with pyropdf.open(base) as doc:
        [viewport] = doc.pages[0].viewports
        assert isinstance(viewport.measure, GeospatialMeasure)
        assert viewport.measure.gcs == projection
        assert viewport.measure.lpts == tuple(map(Decimal, local))
        assert viewport.measure.gpts == tuple(map(Decimal, ground))
        with pytest.raises(ValueError, match="must match"):
            GeospatialMeasure(projection, corrected, lpts=local[:-2])
        assert doc.to_bytes(incremental=True) == base
        doc.pages[0].viewports = [Viewport(viewport.bbox, measure=GeospatialMeasure(projection, corrected, lpts=local))]
        saved = doc.to_bytes(incremental=incremental)

    with pikepdf.open(io.BytesIO(saved)) as independent:
        [vp] = independent.pages[0].obj.VP
        assert list(vp.BBox) == [10, 330, 110, 380]
        assert str(vp.Measure.GCS.Type) == "/PROJCS"
        assert vp.Measure.GCS.EPSG == 32654
        assert list(vp.Measure.LPTS) == list(map(Decimal, local))
        assert list(vp.Measure.GPTS) == list(map(Decimal, corrected))
        assert bytes(independent.Root.PrivateSurvey) == b"keep"


@pytest.mark.spec("document-requirements", "read", "interpret", "create", "edit", "preserve")
def test_document_requirements_lifecycle_versions_and_preservation(tmp_path: Path) -> None:
    # EnableJavaScripts without Penalty/V is PDF 1.7 (Table 273 / RequirementsEnableJavaScripts).
    with pyropdf.open(_pdf_14()) as doc17:
        js_req = DocumentRequirement(
            RequirementType.ENABLE_JAVASCRIPTS,
            handlers=(
                RequirementHandler("JS", script="app.alert('Enable JS');"),
                RequirementHandler("NoOp"),
            ),
        )
        steps_before = doc17.history.position
        doc17.requirements = (js_req,)
        assert doc17.history.position == steps_before + 1
        assert doc17.history.steps[-1] == "document requirements"
        assert doc17.catalog["Version"] == "1.7"
        assert "ID" not in doc17.trailer
        assert doc17.requirements == (js_req,)
        _assert_valid(doc17)

        doc17.history.undo()
        assert doc17.requirements == ()
        doc17.history.redo()
        assert doc17.requirements == (js_req,)

    # All 26 standard RequirementType values, single-dict /RH read support,
    # indirect object retirement, glTF default /V /2.0, and incremental save.
    all_reqs = tuple(
        DocumentRequirement(
            req_type,
            handlers=(RequirementHandler("NoOp"),) if index % 2 == 0 else (),
            penalty=(index * 3) % 101,
            version="2.0" if req_type is RequirementType.GLTF else None,
        )
        for index, req_type in enumerate(RequirementType)
    )
    assert DocumentRequirement(RequirementType.GLTF).version == "2.0"

    full_path = tmp_path / "reqs-full.pdf"
    inc_path = tmp_path / "reqs-inc.pdf"

    with pyropdf.Document() as doc20:
        doc20.pages.new((200, 200))
        # Seed an indirect requirement + single-dictionary /RH to test reading and retirement.
        indirect_rh = doc20.objects.add({"Type": Name("ReqHandler"), "S": Name("NoOp")})
        indirect_req = doc20.objects.add(
            {
                "Type": Name("Requirement"),
                "S": Name("EnableJavaScripts"),
                "RH": doc20.objects[indirect_rh],
            }
        )
        doc20.catalog["Requirements"] = [doc20.objects[indirect_req]]
        assert doc20.requirements == (
            DocumentRequirement(
                RequirementType.ENABLE_JAVASCRIPTS,
                handlers=(RequirementHandler("NoOp"),),
            ),
        )

        doc20.requirements = all_reqs
        assert indirect_rh not in doc20.objects
        assert indirect_req not in doc20.objects
        assert doc20.catalog["Version"] == "2.0"
        assert "ID" in doc20.trailer
        _assert_valid(doc20)
        doc20.save(full_path)

    with pyropdf.open(full_path) as reopened:
        assert not reopened.check()
        assert reopened.requirements == all_reqs

        edited = (
            DocumentRequirement(
                RequirementType.GEOSPATIAL_2D,
                handlers=(RequirementHandler("JS", script="app.alert('2D geo');"),),
                penalty=100,
            ),
        )
        reopened.requirements = edited
        reopened.save(inc_path, incremental=True)

    with pyropdf.open(inc_path) as after_inc:
        assert not after_inc.check()
        assert after_inc.requirements == edited
        del after_inc.requirements
        assert after_inc.requirements == ()
        assert "Requirements" not in after_inc.catalog
        del after_inc.requirements  # idempotent when absent


def test_requirement_and_handle_error_cases() -> None:
    with pytest.raises(ValueError, match="only when kind is 'JS'"):
        RequirementHandler("NoOp", script="app.alert(1);")
    with pytest.raises(ValueError, match="between 0 and 100"):
        DocumentRequirement(RequirementType.MARKUP, penalty=101)
    with pytest.raises(TypeError):
        DocumentRequirement(RequirementType.MARKUP, penalty=False)
    with pytest.raises(ValueError, match="glTF"):
        DocumentRequirement(RequirementType.GLTF, version="1.0")
    with pytest.raises(ValueError, match="leading slash"):
        DocumentRequirement(RequirementType.MARKUP, version="/Bad")
    with pytest.raises(LimitError) as exc_info:
        RequirementHandler("JS", script="x" * (1024 * 1024 + 1))
    assert exc_info.value.code == "script_size"

    doc = pyropdf.Document()
    page = doc.pages.new((100, 100))
    del doc.pages[0]
    with pytest.raises(StalePageError):
        _ = page.viewports
    with pytest.raises(StalePageError):
        page.viewports = ()
    with pytest.raises(StalePageError):
        del page.viewports

    doc.close()
    with pytest.raises(DocumentClosedError):
        _ = doc.requirements
    with pytest.raises(DocumentClosedError):
        doc.requirements = ()
    with pytest.raises(DocumentClosedError):
        del doc.requirements
