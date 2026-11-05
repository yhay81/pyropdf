"""The structural check against the Arlington PDF Model (DESIGN.md section 8)."""

from __future__ import annotations

import runpy
from pathlib import Path

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
    assert report == StructuralReport((), "1.7", report.objects, report.unchecked, truncated=False)
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
        1,
        "PageLayout",
        "Catalog/PageLayout",
        "Table 29",
        "value",
        defect.message,
    )
    assert "/Sideways" in defect.message


def test_the_check_holds_a_file_to_its_declared_version() -> None:
    report = pyropdf.open(BROKEN / "version.pdf").check()
    assert report.version == "1.3"
    assert "PDF 1.4" in report.defects[0].message


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


@pytest.mark.spec("pdfa", "read")
def test_a_pdfa_claim_without_an_output_intent_is_found() -> None:
    assert profile_defects(claiming(PDFA)) == {(1, "OutputIntents", "PDF/A-2B/OutputIntents")}


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


@pytest.mark.spec("pdfua", "read")
def test_a_pdfua_claim_breaking_each_rule_is_found() -> None:
    assert profile_defects(claiming(PDFUA)) == {
        (1, "MarkInfo/Marked", "PDF/UA-1/MarkInfo"),
        (1, "StructTreeRoot", "PDF/UA-1/StructTreeRoot"),
        (1, "Lang", "PDF/UA-1/Lang"),
        (1, "ViewerPreferences/DisplayDocTitle", "PDF/UA-1/ViewerPreferences"),
    }


def test_no_claim_means_no_profile_defect() -> None:
    assert profile_defects(claiming('dc:title="x"')) == set()
