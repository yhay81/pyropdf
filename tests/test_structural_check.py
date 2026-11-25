"""The structural check against the Arlington PDF Model (DESIGN.md section 8)."""

from __future__ import annotations

import io
import runpy
from pathlib import Path

import pikepdf
import pytest

import pyropdf
from pyropdf import CancellationToken, DocumentLimits, StructuralDefect, StructuralReport

ASSETS = Path(__file__).parent / "assets"
BROKEN = ASSETS / "broken"
CORPUS = runpy.run_path(str(BROKEN / "generate.py"))

#: The files of the test assets that depart from the model, with the clauses
#: and kinds of their real defects. Every other file passes.
#: The keys of crypt filters (PDF 1.5) and AES (PDF 1.6) that a 1.3 header
#: lacks.
CRYPT_FILTERS = frozenset(
    (clause, "version")
    for clause in (
        "CryptFilter/AuthEvent",
        "CryptFilter/CFM",
        "CryptFilter/Length",
        "CryptFilterMap/StdCF",
        "EncryptionStandard/CF",
        "EncryptionStandard/Length",
        "EncryptionStandard/StmF",
        "EncryptionStandard/StrF",
        "EncryptionStandard/V",
    )
)

KNOWN: dict[str, frozenset[tuple[str, str]]] = {
    # This negative really uses uncalibrated device RGB: PDF/A-4 6.2.4.3-2.
    "profile-sources/4-fail.pdf": frozenset({("PDF/A-4/OutputIntents", "profile")}),
    # veraPDF's EmbeddedFiles control uses /Undefined in a separate
    # AFRelationship. The base schema enumerates /Unspecified.
    "profile-sources/4f-pass.pdf": frozenset(
        {("AFFileSpecification/AFRelationship", "value"), ("FileSpecification/AFRelationship", "value")}
    ),
    # A 1.3 header with AES encryption.
    "encrypted/owneronly-aes-256.pdf": CRYPT_FILTERS,
    "encrypted/user-aes-128.pdf": CRYPT_FILTERS,
    "encrypted/user-aes-256.pdf": CRYPT_FILTERS,
    "encrypted/user-aes-256-r5.pdf": CRYPT_FILTERS | {("EncryptionStandard/R", "version")},
    # A 1.3 header with 128-bit RC4 (/V 2, /Length), which came with PDF 1.4.
    "encrypted/user-rc4-128.pdf": frozenset(
        {("EncryptionStandard/Length", "version"), ("EncryptionStandard/V", "version")}
    ),
    "encrypted/user-rc4-40.pdf": frozenset({("EncryptionStandard/Length", "version")}),
    # A signature's /REx is a name where the model wants a text string, and
    # PDF 1.6 added the key to a 1.5 file.
    "real_world/bill-hr815.pdf": frozenset(
        {
            ("SignatureBuildDataAppDict/REx", "type"),
            ("SignatureBuildDataAppDict/REx", "version"),
        }
    ),
    # Two form XObjects without /Resources.
    "real_world/pdfium-smask-blend.pdf": frozenset({("XObjectFormType1/Resources", "required")}),
}


def _open(path: Path) -> pyropdf.Document:
    doc = pyropdf.open(path)
    if doc.needs_password:
        doc.unlock("userpw")
    return doc


@pytest.mark.parametrize("name", sorted(CORPUS["DEFECTS"]))
def test_the_check_finds_each_defect_of_the_broken_corpus(name: str) -> None:
    data, (xref, path, clause, kind) = CORPUS["DEFECTS"][name]
    assert (BROKEN / name).read_bytes() == data, "run tests/assets/broken/generate.py"
    report = pyropdf.open(BROKEN / name).check()
    assert [(d.xref, d.path, d.clause, d.kind) for d in report.defects] == [(xref, path, clause, kind)]
    [defect] = report.defects
    assert defect.spec
    assert defect.message
    assert report


@pytest.mark.parametrize(
    "path",
    sorted(path.relative_to(ASSETS).as_posix() for path in ASSETS.glob("*/*.pdf") if path.parent != BROKEN),
)
def test_every_valid_asset_passes_or_lists_its_real_defects(path: str) -> None:
    report = _open(ASSETS / path).check()
    assert {(d.clause, d.kind) for d in report.defects} == KNOWN.get(path, frozenset())
    assert not report.truncated
    assert report.objects > 0


def test_a_new_document_passes() -> None:
    report = pyropdf.Document().check()
    assert report == StructuralReport(
        defects=(), version="1.7", objects=report.objects, unchecked=report.unchecked, truncated=False
    )
    assert not report


def _resource_document(resource: bytes, objects: list[bytes]) -> pyropdf.Document:
    """Read resource objects starting at object 4 through a page's resource dictionary."""
    return pyropdf.open(
        CORPUS["pdf"](
            [
                b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << " + resource + b" >> >>",
                *objects,
            ]
        )
    )


def _shading_document(shading_type: int, route: str, omit: bytes = b"", replace: bytes = b"") -> pyropdf.Document:
    """Reach a shading directly or through a pattern, with one optional malformed entry."""
    entries = f"/ShadingType {shading_type} /ColorSpace /DeviceGray ".encode()
    if shading_type <= 3:
        if shading_type == 1:
            # A two-input PostScript function for a function-based shading.
            function = b"<< /FunctionType 4 /Domain [0 1 0 1] /Range [0 1] /Length 5 >>\nstream\n{pop}\nendstream"
        else:
            coordinates = b"[0 0 200 200]" if shading_type == 2 else b"[0 0 0 100 100 100]"
            entries += b"/Coords " + coordinates + b" "
            function = b"<< /FunctionType 2 /Domain [0 1] /N 1 >>"
        entries += b"/Function 6 0 R "
    else:
        entries += b"/BitsPerCoordinate 8 /BitsPerComponent 8 /Decode [0 200 0 200 0 1] "
        entries += b"/VerticesPerRow 2 " if shading_type == 5 else b"/BitsPerFlag 2 "
        function = b"null"
    if omit:
        assert omit in entries
        entries = entries.replace(omit, replace)
    shading = b"<< " + entries + b" >>"
    if shading_type >= 4:
        shading = b"<< " + entries + b" /Length 0 >>\nstream\n\nendstream"
    resource = b"/Shading << /S 4 0 R >>" if route == "shading" else b"/Pattern << /P 5 0 R >>"
    return _resource_document(resource, [shading, b"<< /Type /Pattern /PatternType 2 /Shading 4 0 R >>", function])


@pytest.mark.parametrize("route", ["shading", "pattern"])
@pytest.mark.parametrize("shading_type", range(1, 8))
def test_shading_types_check_clean_through_both_resource_paths(shading_type: int, route: str) -> None:
    doc = _shading_document(shading_type, route)
    assert doc.check().defects == ()


@pytest.mark.parametrize("route", ["shading", "pattern"])
@pytest.mark.parametrize(
    ("shading_type", "entry", "key"),
    [
        (1, b"/Function 6 0 R ", "Function"),
        (2, b"/Coords [0 0 200 200] ", "Coords"),
        (3, b"/Coords [0 0 0 100 100 100] ", "Coords"),
        (4, b"/BitsPerFlag 2 ", "BitsPerFlag"),
        (5, b"/VerticesPerRow 2 ", "VerticesPerRow"),
        (6, b"/BitsPerFlag 2 ", "BitsPerFlag"),
        (7, b"/BitsPerFlag 2 ", "BitsPerFlag"),
    ],
)
def test_missing_shading_keys_are_reported_against_the_declared_type(
    shading_type: int, entry: bytes, key: str, route: str
) -> None:
    report = _shading_document(shading_type, route, entry).check()
    assert [(d.xref, d.clause, d.kind) for d in report.defects] == [(4, f"ShadingType{shading_type}/{key}", "required")]


@pytest.mark.parametrize("shading_type", range(4, 8))
def test_invalid_mesh_bit_depth_does_not_prevent_selecting_its_type(shading_type: int) -> None:
    report = _shading_document(shading_type, "shading", b"/BitsPerComponent 8 ", b"/BitsPerComponent 3 ").check()
    assert [(d.xref, d.clause, d.kind) for d in report.defects] == [
        (4, f"ShadingType{shading_type}/BitsPerComponent", "value")
    ]


@pytest.mark.parametrize(
    ("function_type", "function", "required"),
    [
        (
            0,
            (
                b"<< /FunctionType 0 /Domain [0 1] /Range [0 1] /Size [2] /BitsPerSample 8 /Length 2 >>"
                b"\nstream\n\x00\xff\nendstream"
            ),
            b"/Size [2] ",
        ),
        (2, b"<< /FunctionType 2 /Domain [0 1] /N 1 >>", b"/N 1 "),
        (
            3,
            b"<< /FunctionType 3 /Domain [0 1] /Functions [5 0 R] /Bounds [] /Encode [0 1] >>",
            b"/Encode [0 1] ",
        ),
        (
            4,
            b"<< /FunctionType 4 /Domain [0 1] /Range [0 1] /Length 2 >>\nstream\n{}\nendstream",
            b"/Range [0 1] ",
        ),
    ],
)
def test_function_types_validate_their_own_required_keys(function_type: int, function: bytes, required: bytes) -> None:
    resource = b"/ExtGState << /G << /BG 4 0 R >> >>"
    nested = b"<< /FunctionType 2 /Domain [0 1] /N 1 >>"
    assert _resource_document(resource, [function, nested]).check().defects == ()
    report = _resource_document(resource, [function.replace(required, b""), nested]).check()
    key = required.split()[0][1:].decode()
    assert [(d.xref, d.clause, d.kind) for d in report.defects] == [
        (4, f"FunctionType{function_type}/{key}", "required")
    ]


def test_tiling_pattern_integer_constraints_are_still_validated() -> None:
    pattern = (
        b"<< /Type /Pattern /PatternType 1 /PaintType 1 /TilingType 1 /BBox [0 0 10 10] "
        b"/XStep 10 /YStep 10 /Resources << >> /Length 0 >>\nstream\n\nendstream"
    )
    resource = b"/Pattern << /P 4 0 R >>"
    assert _resource_document(resource, [pattern]).check().defects == ()
    report = _resource_document(resource, [pattern.replace(b"/TilingType 1", b"/TilingType 9")]).check()
    assert [(d.xref, d.clause, d.kind) for d in report.defects] == [(4, "PatternType1/TilingType", "value")]


def test_the_report_names_object_path_and_both_clauses() -> None:
    [defect] = pyropdf.open(BROKEN / "value.pdf").check().defects
    assert defect == StructuralDefect(
        xref=1,
        path="PageLayout",
        clause="Catalog/PageLayout",
        spec="Table 29",
        kind="value",
        message=defect.message,
    )
    assert "/Sideways" in defect.message


def test_the_check_holds_a_file_to_its_declared_version() -> None:
    report = pyropdf.open(BROKEN / "version.pdf").check()
    assert report.version == "1.3"
    assert "PDF 1.4" in report.defects[0].message


def _geo_viewport(version: str, extension: bytes = b"", *, pcsm: bool = False) -> bytes:
    catalog = b"/Extensions << /ADBE << " + extension + b" >> >>" if extension else b""
    matrix = b"/PCSM [1 0 0 0 1 0 0 0 1 0 0 0]" if pcsm else b""
    data: bytes = CORPUS["pdf"](
        [
            b"<< /Type /Catalog /Pages 2 0 R " + catalog + b" >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << >> /VP [4 0 R] >>",
            b"<< /Type /Viewport /BBox [0 0 100 100] /Measure << /Type /Measure /Subtype /GEO "
            b"/GCS << /Type /GEOGCS /EPSG 4326 >> /GPTS [37 -122] /LPTS [1 0] " + matrix + b" >> >>",
        ],
        version,
    )
    # PDF 2.0 requires a file identifier. The trailer follows all indexed objects.
    return data.replace(b"/Root 1 0 R >>", b"/Root 1 0 R /ID [(0123456789abcdef) (0123456789abcdef)] >>")


@pytest.mark.parametrize("level", [3, 11])
@pytest.mark.parametrize("incremental", [False, True])
def test_legacy_geo_viewport_survives_a_save(level: int, *, incremental: bool) -> None:
    source = _geo_viewport("1.7", f"/BaseVersion /1.7 /ExtensionLevel {level}".encode())
    with pyropdf.open(source) as doc:
        assert not doc.check().defects
        doc.catalog["Lang"] = "en-US"
        saved = doc.to_bytes(incremental=incremental)
    if incremental:
        assert saved.startswith(source)
    with pyropdf.open(saved) as reopened:
        assert not reopened.check().defects
    with pikepdf.open(io.BytesIO(saved)) as independent:
        assert not independent.check_pdf_syntax()
        assert independent.pdf_version == "1.7"
        assert independent.Root.Extensions.ADBE.BaseVersion == pikepdf.Name("/1.7")
        assert independent.Root.Extensions.ADBE.ExtensionLevel == level
        assert str(independent.Root.Lang) == "en-US"
        [viewport] = independent.pages[0].obj.VP
        assert list(viewport.BBox) == [0, 0, 100, 100]
        measure = viewport.Measure
        assert (measure.Type, measure.Subtype) == (pikepdf.Name.Measure, pikepdf.Name.GEO)
        assert (measure.GCS.Type, measure.GCS.EPSG) == (pikepdf.Name.GEOGCS, 4326)
        assert list(measure.GPTS) == [37, -122]
        assert list(measure.LPTS) == [1, 0]


@pytest.mark.parametrize(
    ("version", "extension"),
    [
        ("1.7", b""),
        ("1.7", b"/BaseVersion /1.7 /ExtensionLevel 2"),
        ("1.7", b"/BaseVersion /1.6 /ExtensionLevel 3"),
        ("1.6", b"/BaseVersion /1.7 /ExtensionLevel 3"),
    ],
)
def test_legacy_geo_requires_the_matching_adobe_extension(version: str, extension: bytes) -> None:
    with pyropdf.open(_geo_viewport(version, extension)) as doc:
        defects = {(d.clause, d.kind) for d in doc.check().defects}
    assert ("MeasureGEO/Type", "version") in defects
    assert ("MeasureGEO/Subtype", "version") in defects


@pytest.mark.parametrize(
    ("version", "extension", "expected"),
    [
        ("1.7", b"/BaseVersion /1.7 /ExtensionLevel 3", [("MeasureGEO/PCSM", "version")]),
        ("2.0", b"", []),
    ],
)
def test_geo_matrix_requires_pdf_20(version: str, extension: bytes, expected: list[tuple[str, str]]) -> None:
    with pyropdf.open(_geo_viewport(version, extension, pcsm=True)) as doc:
        assert [(d.clause, d.kind) for d in doc.check().defects] == expected


def test_legacy_geo_checks_its_viewport_and_coordinate_system() -> None:
    source = _geo_viewport("1.7", b"/BaseVersion /1.7 /ExtensionLevel 3")
    # Equal-length substitutions retain the literal file's cross-reference offsets.
    source = source.replace(b"/BBox ", b"/Test ").replace(b"/EPSG 4326", b"/EPSG /bad")
    with pyropdf.open(source) as doc:
        assert {(d.clause, d.kind) for d in doc.check().defects} == {
            ("Viewport/BBox", "required"),
            ("GeographicCoordinateSystem/EPSG", "type"),
        }


def test_a_cyclic_page_tree_is_walked_once() -> None:
    report = pyropdf.open(BROKEN / "cycle.pdf").check()
    assert report.objects <= 4
    # The root reached again as a kid lacks the /Parent a kid needs, and it
    # counts its own page twice.
    assert sorted((d.xref, d.clause) for d in report.defects) == [
        (2, "PageTreeNode/Count"),
        (2, "PageTreeNode/Kids"),
        (2, "PageTreeNode/Parent"),
    ]


def test_a_deep_page_tree_is_walked_without_recursion() -> None:
    depth = 20_000
    report = pyropdf.open(CORPUS["chain"](depth)).check()
    assert report.defects == ()
    # The catalog, the nodes, the page, and its content stream.
    assert report.objects == depth + 3


def test_the_check_runs_under_the_documents_budget() -> None:
    token = CancellationToken()
    doc = pyropdf.open(CORPUS["chain"](100), limits=DocumentLimits(cancellation=token))
    token.cancel()
    with pytest.raises(pyropdf.LimitError) as caught:
        doc.check()
    assert caught.value.code == "cancelled"


def test_a_closed_document_refuses_the_check() -> None:
    doc = pyropdf.Document()
    doc.close()
    with pytest.raises(pyropdf.DocumentClosedError):
        doc.check()


def claiming(claim: str, catalog: str = "") -> pyropdf.Document:
    """Return a one-page document whose XMP metadata declares ``claim``, plus ``catalog`` entries."""
    xmp = f"<rdf:Description {claim}/>".encode()
    data = CORPUS["pdf"](
        [
            b"<< /Type /Catalog /Pages 2 0 R /Metadata 5 0 R " + catalog.encode() + b" >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> >>",
            b"<< /Type /StructTreeRoot >>",
            b"<< /Type /Metadata /Subtype /XML /Length %d >>\nstream\n%s\nendstream" % (len(xmp), xmp),
        ]
    )
    return pyropdf.open(data)


def profile_defects(doc: pyropdf.Document) -> set[tuple[int | None, str, str]]:
    return {(d.xref, d.path, d.clause) for d in doc.check().defects if d.kind == "profile"}


PDFA = 'pdfaid:part="2" pdfaid:conformance="B"'
INTENT = "/OutputIntents [<< /Type /OutputIntent /S /GTS_PDFA1 /OutputConditionIdentifier (sRGB) >>]"
PDFUA = 'pdfuaid:part="1"'
UA_KEEPS = (
    "/MarkInfo << /Marked true >> /StructTreeRoot 4 0 R /Lang (en) /ViewerPreferences << /DisplayDocTitle true >>"
)


def test_a_pdfa_claim_that_holds_has_no_profile_defect() -> None:
    assert profile_defects(claiming(PDFA, INTENT)) == set()


@pytest.mark.spec("output-intents", "interpret")
@pytest.mark.spec("pdfa", "read")
def test_a_pdfa_claim_without_an_output_intent_is_found() -> None:
    doc = claiming(PDFA)
    assert profile_defects(doc) == set()  # The empty page paints no colours.
    with doc.pages[0].draw() as canvas:
        canvas.rect((0, 0, 20, 20), fill=pyropdf.Color(0.2, 0.5, 0.8))
    assert profile_defects(doc) == {(1, "OutputIntents", "PDF/A-2B/OutputIntents")}


def test_a_pdfa_claim_with_javascript_is_found() -> None:
    names = "/Names << /JavaScript << /Names [] >> >>"
    assert profile_defects(claiming(PDFA, f"{INTENT} {names}")) == {
        (1, "Names/JavaScript", "PDF/A-2B/Names"),
    }
    action = "/OpenAction << /S /JavaScript /JS (app.alert(1)) >>"
    assert profile_defects(claiming(PDFA, f"{INTENT} {action}")) == {(1, "OpenAction", "PDF/A-2B/OpenAction")}


def test_a_pdfa_claim_with_encryption_is_found() -> None:
    data = claiming(PDFA, INTENT).to_bytes(encryption=pyropdf.Encryption(owner_password="owner"))  # noqa: S106 - test data
    assert profile_defects(pyropdf.open(data)) == {(None, "Encrypt", "PDF/A-2B/Encrypt")}


def test_a_pdfua_claim_that_holds_has_no_profile_defect() -> None:
    assert profile_defects(claiming(PDFUA, UA_KEEPS)) == set()


@pytest.mark.spec("accessibility", "read", "interpret")
@pytest.mark.spec("document-catalog", "interpret")
@pytest.mark.spec("pdfua", "read")
@pytest.mark.spec("tagged-pdf", "read", "interpret")
def test_a_pdfua_claim_breaking_each_rule_is_found() -> None:
    assert profile_defects(claiming(PDFUA)) == {
        (1, "MarkInfo/Marked", "PDF/UA-1/MarkInfo"),
        (1, "StructTreeRoot", "PDF/UA-1/StructTreeRoot"),
        (1, "Lang", "PDF/UA-1/Lang"),
        (1, "ViewerPreferences/DisplayDocTitle", "PDF/UA-1/ViewerPreferences"),
    }


def test_no_claim_means_no_profile_defect() -> None:
    assert profile_defects(claiming('dc:title="x"')) == set()


@pytest.mark.parametrize("key", ["A", "C"])
@pytest.mark.parametrize("with_revisions", [False, True])
def test_structure_array_revision_numbers_are_optional_for_every_payload(key: str, *, with_revisions: bool) -> None:
    doc = pyropdf.Document()
    doc.pages.new((100, 100))
    elem = doc.structure.add("Document")
    payloads = (
        [
            doc.objects[doc.objects.add({"O": pyropdf.Name("Layout")})],
            {"O": pyropdf.Name("Table")},
            {"O": pyropdf.Name("List")},
        ]
        if key == "A"
        else [pyropdf.Name(name) for name in ("First", "Second", "Third")]
    )
    if key == "C":
        root = doc.structure.obj
        assert root is not None
        root["ClassMap"] = {str(name): {"O": pyropdf.Name("Layout")} for name in payloads}
    elem.obj[key] = [payloads[0], 1, payloads[1], payloads[2], 0] if with_revisions else payloads
    for current in (doc, pyropdf.open(doc.to_bytes())):
        assert not [
            d
            for d in current.check().defects
            if d.clause.startswith(("ArrayOfAttributeRevisions/", "ArrayOfClassNamesRevisions/"))
        ]


@pytest.mark.parametrize(
    ("key", "descriptor"), [("A", "ArrayOfAttributeRevisions"), ("C", "ArrayOfClassNamesRevisions")]
)
@pytest.mark.parametrize(
    ("case", "index", "slot"), [("leading", 0, "0*"), ("double", 2, "0*"), ("negative", 1, "1*"), ("bad", 1, "0*")]
)
def test_invalid_structure_array_members_retain_their_own_path(
    key: str, descriptor: str, case: str, index: int, slot: str
) -> None:
    doc = pyropdf.Document()
    doc.pages.new((100, 100))
    elem = doc.structure.add("Document")
    first = {"O": pyropdf.Name("Layout")} if key == "A" else pyropdf.Name("First")
    elem.obj[key] = {"leading": [1, first], "double": [first, 0, 2], "negative": [first, -1], "bad": [first, False]}[
        case
    ]
    for current in (doc, pyropdf.open(doc.to_bytes())):
        assert (f"{key}/{index}", f"{descriptor}/{slot}") in {(d.path, d.clause) for d in current.check().defects}


def test_xfa_packet_arrays_still_require_alternating_names_and_indirect_streams() -> None:
    with pyropdf.Document() as doc:
        doc.pages.new((100, 100))
        doc.catalog["Version"] = pyropdf.Name("1.7")
        packet = doc.objects[doc.objects.add_stream(b"<packet/>")]
        doc.catalog["AcroForm"] = {"Fields": [], "XFA": ["template", packet, "datasets", packet]}
        assert not [d for d in doc.check().defects if d.clause.startswith("ArrayOfXFA/")]
        doc.catalog["AcroForm"]["XFA"] = ["template", "datasets", packet]
        assert {
            (d.path.split("/")[-1], d.clause) for d in doc.check().defects if d.clause.startswith("ArrayOfXFA/")
        } == {("1", "ArrayOfXFA/1*"), ("2", "ArrayOfXFA/0*")}


@pytest.mark.parametrize("filter_entry", [b"", b"/Filter /FlateDecode ", b"/Filter [/FlateDecode] "])
def test_image_missing_colorspace_and_bpc_reported_for_absent_single_and_array_filter(filter_entry: bytes) -> None:
    stream = (
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 " + filter_entry + b"/Length 1 >>\nstream\nx\nendstream"
    )
    report = _resource_document(b"/XObject << /Im0 4 0 R >>", [stream]).check()
    required = {(d.xref, d.clause) for d in report.defects if d.kind == "required"}
    assert (4, "XObjectImage/ColorSpace") in required
    assert (4, "XObjectImage/BitsPerComponent") in required


@pytest.mark.parametrize("extra", [b"/Filter /JPXDecode ", b"/Filter [/FlateDecode /JPXDecode] ", b"/ImageMask true "])
def test_image_exempt_filters_and_imagemask_do_not_require_colorspace_or_bpc(extra: bytes) -> None:
    stream = b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 " + extra + b"/Length 1 >>\nstream\nx\nendstream"
    report = _resource_document(b"/XObject << /Im0 4 0 R >>", [stream]).check()
    required = {d.clause for d in report.defects if d.kind == "required"}
    assert "XObjectImage/ColorSpace" not in required
    assert "XObjectImage/BitsPerComponent" not in required


def test_soft_mask_bpc_required_value_enforced_for_single_name_filter() -> None:
    image = (
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceRGB "
        b"/BitsPerComponent 8 /SMask 5 0 R /Length 3 >>\nstream\nabc\nendstream"
    )
    smask = (
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray "
        b"/BitsPerComponent 4 /Filter /DCTDecode /Length 1 >>\nstream\nx\nendstream"
    )
    report = _resource_document(b"/XObject << /Im0 4 0 R >>", [image, smask]).check()
    assert (5, "XObjectImageSoftMask/BitsPerComponent", "value") in {(d.xref, d.clause, d.kind) for d in report.defects}


def test_multi_level_parent_tree_next_key_is_validated() -> None:
    def make_doc(next_key: int) -> pyropdf.Document:
        return pyropdf.open(
            CORPUS["pdf"](
                [
                    b"<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 4 0 R >>",
                    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << >> >>",
                    f"<< /Type /StructTreeRoot /K 5 0 R /ParentTree 6 0 R /ParentTreeNextKey {next_key} >>".encode(),
                    b"<< /Type /StructElem /S /P /P 4 0 R >>",
                    b"<< /Kids [7 0 R 8 0 R] >>",
                    b"<< /Limits [0 1] /Nums [0 5 0 R 1 5 0 R] >>",
                    b"<< /Limits [2 5] /Kids [9 0 R] >>",
                    b"<< /Limits [2 5] /Nums [2 5 0 R 5 5 0 R] >>",
                ]
            )
        )

    assert not [d for d in make_doc(6).check().defects if d.clause == "StructTreeRoot/ParentTreeNextKey"]
    defects = [(d.xref, d.path, d.clause, d.kind) for d in make_doc(5).check().defects if d.kind == "invariant"]
    assert defects == [(4, "ParentTreeNextKey", "StructTreeRoot/ParentTreeNextKey", "invariant")]


@pytest.mark.parametrize(
    ("case", "expected_xref", "expected_path"),
    [
        ("indirect_ap_and_n", 4, "AS"),
        ("as_null", 4, "AS"),
        ("rollover_states", 4, "AS"),
        ("down_states", 4, "AS"),
        ("direct_annot", 3, "Annots/0/AS"),
    ],
)
def test_appearance_state_defects_cover_indirect_direct_null_and_r_d(
    case: str, expected_xref: int, expected_path: str
) -> None:
    form = b"<< /Type /XObject /Subtype /Form /BBox [0 0 10 10] /Resources << >> /Length 0 >>\nstream\n\nendstream"
    if case == "indirect_ap_and_n":
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << >> /Annots [4 0 R] >>",
            b"<< /Type /Annot /Subtype /Square /Rect [0 0 10 10] /AP 5 0 R >>",
            b"<< /N 6 0 R >>",
            b"<< /On 7 0 R >>",
            form,
        ]
    elif case == "as_null":
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << >> /Annots [4 0 R] >>",
            b"<< /Type /Annot /Subtype /Square /Rect [0 0 10 10] /AP << /N << /On 5 0 R >> >> /AS null >>",
            form,
        ]
    elif case in ("rollover_states", "down_states"):
        key = b"R" if case == "rollover_states" else b"D"
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << >> /Annots [4 0 R] >>",
            b"<< /Type /Annot /Subtype /Square /Rect [0 0 10 10] /AP << /N 5 0 R /" + key + b" 6 0 R >> >>",
            form,
            b"<< /Over 5 0 R >>",
        ]
    else:
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << >> "
                b"/Annots [<< /Type /Annot /Subtype /Square /Rect [0 0 10 10] /AP << /N << /On 4 0 R >> >> >>] >>"
            ),
            form,
        ]
    report = pyropdf.open(CORPUS["pdf"](objects)).check()
    invariants = [(d.xref, d.path, d.clause, d.kind) for d in report.defects if d.kind == "invariant"]
    assert invariants == [(expected_xref, expected_path, "AnnotSquare/AS", "invariant")]


def _blank_version_doc(version: str) -> pyropdf.Document:
    data: bytes = CORPUS["pdf"](
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << >> >>",
        ],
        version,
    )
    if version == "2.0":
        data = data.replace(b"/Root 1 0 R >>", b"/Root 1 0 R /ID [(0123456789abcdef) (0123456789abcdef)] >>")
    return pyropdf.open(data)


@pytest.mark.parametrize("filter_cls", [pyropdf.typed.FilterFlateDecode, pyropdf.typed.FilterLZWDecode])
@pytest.mark.parametrize("version", ["1.3", "1.7", "2.0"])
def test_filter_colors_accepts_valid_values_on_pdf13_plus_and_rejects_nonpositive(
    filter_cls: type[pyropdf.typed.FilterFlateDecode | pyropdf.typed.FilterLZWDecode], version: str
) -> None:
    with _blank_version_doc(version) as doc:
        for colors in (1, 3, 5):
            created = filter_cls.create(doc, {"Colors": colors})
            assert created.interpret("Colors") == colors
        for colors in (0, -1):
            with pytest.raises(pyropdf.MalformedPdfError, match="Colors"):
                filter_cls.create(doc, {"Colors": colors})
            raw = doc.objects[doc.objects.add({"Colors": colors})]
            with pytest.raises(pyropdf.PdfError, match="admitted types or values"):
                filter_cls(raw).interpret("Colors")


@pytest.mark.parametrize("filter_cls", [pyropdf.typed.FilterFlateDecode, pyropdf.typed.FilterLZWDecode])
def test_filter_colors_enforces_upper_bound_on_pre13_pdfs(
    filter_cls: type[pyropdf.typed.FilterFlateDecode | pyropdf.typed.FilterLZWDecode],
) -> None:
    with _blank_version_doc("1.2") as doc:
        for colors in (1, 4):
            created = filter_cls.create(doc, {"Colors": colors})
            assert created.interpret("Colors") == colors
        for colors in (0, 5):
            with pytest.raises(pyropdf.MalformedPdfError, match="Colors"):
                filter_cls.create(doc, {"Colors": colors})
            raw = doc.objects[doc.objects.add({"Colors": colors})]
            with pytest.raises(pyropdf.PdfError, match="admitted types or values"):
                filter_cls(raw).interpret("Colors")


def test_before_version_is_required_rules_stay_enforced() -> None:
    form = b"<< /Type /XObject /Subtype /Form /BBox [0 0 10 10] /Resources << >> /Length 0 >>\nstream\n\nendstream"
    for version, expected in (
        ("1.2", {"XObjectFormType1/FormType", "XObjectFormType1/Matrix", "XObjectFormType1/Name"}),
        ("1.3", set()),
    ):
        data = CORPUS["pdf"](
            [
                b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << /XObject << /Fm0 4 0 R >> >> >>",
                form,
            ],
            version,
        )
        required = {d.clause for d in pyropdf.open(data).check().defects if d.kind == "required"}
        assert required == expected


@pytest.mark.parametrize(
    ("zoom", "expected"),
    [
        (b"/min 2.0", []),
        (b"/max 5.0", []),
        (b"/min 0.0", []),
        (b"/max 0.0", []),
        (b"/min 1.0 /max 5.0", []),
        (b"/min 2.0 /max 2.0", []),
        (b"/min -1.0", [(4, "Usage/Zoom/min", "OptContentZoom/min", "value")]),
        (b"/max -0.5", [(4, "Usage/Zoom/max", "OptContentZoom/max", "value")]),
        (
            b"/min 5.0 /max 2.0",
            [
                (4, "Usage/Zoom/max", "OptContentZoom/max", "value"),
                (4, "Usage/Zoom/min", "OptContentZoom/min", "value"),
            ],
        ),
        (
            b"",
            [
                (4, "Usage/Zoom/max", "OptContentZoom/max", "required"),
                (4, "Usage/Zoom/min", "OptContentZoom/min", "required"),
            ],
        ),
    ],
)
def test_opt_content_zoom_allows_single_bound_and_enforces_constraints(
    zoom: bytes, expected: list[tuple[int, str, str, str]]
) -> None:
    data = CORPUS["pdf"](
        [
            b"<< /Type /Catalog /Pages 2 0 R /OCProperties << /OCGs [4 0 R] /D << >> >> >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << >> >>",
            b"<< /Type /OCG /Name (Layer) /Usage << /Zoom << " + zoom + b" >> >> >>",
        ]
    )
    defects = sorted((d.xref, d.path, d.clause, d.kind) for d in pyropdf.open(data).check().defects)
    assert defects == expected


def test_opt_content_zoom_typed_create_and_interpret_accept_single_bound() -> None:
    with pyropdf.Document() as doc:
        min_only = pyropdf.typed.OptContentZoom.create(doc, {"min": 2.0})
        assert min_only.interpret("min") == 2.0
        max_only = pyropdf.typed.OptContentZoom.create(doc, {"max": 5.0})
        assert max_only.interpret("max") == 5.0
        with pytest.raises(pyropdf.MalformedPdfError, match="OptContentZoom/min"):
            pyropdf.typed.OptContentZoom.create(doc, {"min": -1.0})
        with pytest.raises(pyropdf.MalformedPdfError, match="OptContentZoom/"):
            pyropdf.typed.OptContentZoom.create(doc, {"min": 5.0, "max": 2.0})


def test_indirect_structure_k_arrays_validate_parent_backlinks() -> None:
    def root_indirect_k(second_parent: int) -> pyropdf.Document:
        return pyropdf.open(
            CORPUS["pdf"](
                [
                    b"<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 4 0 R >>",
                    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << >> >>",
                    b"<< /Type /StructTreeRoot /K 7 0 R >>",
                    b"<< /Type /StructElem /S /Document /P 4 0 R >>",
                    f"<< /Type /StructElem /S /P /P {second_parent} 0 R >>".encode(),
                    b"[5 0 R 6 0 R]",
                ]
            )
        )

    assert [d for d in root_indirect_k(4).check().defects if d.kind == "invariant"] == []
    assert [
        (d.xref, d.path, d.clause, d.kind) for d in root_indirect_k(1).check().defects if d.kind == "invariant"
    ] == [(6, "P", "StructTreeRoot/K", "invariant")]

    def elem_indirect_k(leaf_parent: int) -> pyropdf.Document:
        return pyropdf.open(
            CORPUS["pdf"](
                [
                    b"<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 4 0 R >>",
                    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << >> >>",
                    b"<< /Type /StructTreeRoot /K 5 0 R >>",
                    b"<< /Type /StructElem /S /Document /P 4 0 R /K 6 0 R >>",
                    b"[0 7 0 R 8 0 R]",
                    b"<< /Type /StructElem /S /P /P 5 0 R >>",
                    f"<< /Type /StructElem /S /Span /P {leaf_parent} 0 R >>".encode(),
                ]
            )
        )

    assert [d for d in elem_indirect_k(5).check().defects if d.kind == "invariant"] == []
    assert [
        (d.xref, d.path, d.clause, d.kind) for d in elem_indirect_k(4).check().defects if d.kind == "invariant"
    ] == [(8, "P", "StructElem/K", "invariant")]

    cyclic = pyropdf.open(
        CORPUS["pdf"](
            [
                b"<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 4 0 R >>",
                b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << >> >>",
                b"<< /Type /StructTreeRoot /K 5 0 R >>",
                b"[5 0 R]",
            ]
        )
    )
    assert not cyclic.check().truncated


def test_etsi_pades_cades_requires_direct_extensions_dictionary() -> None:
    def check_cades(extensions_entry: bytes, extra: list[bytes]) -> set[tuple[str, str]]:
        data = CORPUS["pdf"](
            [
                b"<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R /Extensions " + extensions_entry + b" >>",
                b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << >> >>",
                b"<< /Fields [5 0 R] >>",
                b"<< /FT /Sig /T (Approval) /V 6 0 R >>",
                b"<< /Type /Sig /Filter /Adobe.PPKLite /SubFilter /ETSI.CAdES.detached /Contents <00> >>",
                *extra,
            ]
        )
        return {(d.clause, d.kind) for d in pyropdf.open(data).check().defects}

    direct = check_cades(b"<< /ESIC << /BaseVersion /1.7 /ExtensionLevel 2 >> >>", [])
    assert ("Signature/SubFilter", "version") not in direct
    assert ("Catalog/Extensions", "indirect") not in direct

    # ISO 32000-1 7.12.1 requires /Extensions and developer extension dictionaries to be direct.
    indirect_ext = check_cades(b"7 0 R", [b"<< /ESIC << /BaseVersion /1.7 /ExtensionLevel 2 >> >>"])
    assert ("Catalog/Extensions", "indirect") in indirect_ext
    assert ("Signature/SubFilter", "version") in indirect_ext

    indirect_vendor = check_cades(b"<< /ESIC 7 0 R >>", [b"<< /BaseVersion /1.7 /ExtensionLevel 2 >>"])
    assert ("Extensions/*", "indirect") in indirect_vendor
    assert ("Signature/SubFilter", "version") in indirect_vendor
