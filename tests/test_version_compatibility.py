"""Public name, version and content behavior across PDF editions (Annexes E and I).

The registration controls ITXT, ISO_ and _Y4Y appear in the Adobe PDF Names
registry at 686327e66d95. Arlington's additional-key test for prefixes that
already contain the delimiter is tracked in #943.
"""

from __future__ import annotations

import io
import shutil
import subprocess
from typing import TYPE_CHECKING

import pikepdf
import pytest
from test_content import build_raw_pdf

import pyropdf
from pyropdf import Name, Operation

if TYPE_CHECKING:
    from pathlib import Path

KNOWN_PAINT = b"0 g 4 4 16 16 re f"
COMPAT = b"q BX /ITXT_View 17 ITXT_Op BX 3 XXHidden EX " + KNOWN_PAINT + b" EX Q"


def _document(header: str = "1.4", declaration: str = "", paint: bytes = COMPAT) -> bytes:
    """Create independent lexical fixtures with inherited resources and a shared legacy ProcSet."""
    objects: dict[int, str | bytes] = {
        1: f"<< /Type /Catalog /Pages 2 0 R {declaration} /ITXT#5FFlag /ITXT_One /ISO_Flag /ISO_One /XXFlag 1 >>",
        2: "<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 /Resources 6 0 R /MediaBox [0 0 40 40] >>",
        3: "<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>",
        4: f"<< /Length {len(paint)} >>\nstream\n".encode() + paint + b"\nendstream",
        5: "<< /Type /Page /Parent 2 0 R >>",
        6: "<< /ProcSet 7 0 R /XXResource /PrivateValue >>",
        7: "[/PDF /Text /ImageB]",
    }
    data = build_raw_pdf(objects).replace(b"%PDF-1.7", f"%PDF-{header}".encode(), 1)
    return data.replace(
        b"/Root 1 0 R >>", b"/Root 1 0 R /ID [<0123456789ABCDEF0123456789ABCDEF> <0123456789ABCDEF0123456789ABCDEF>] >>"
    )


@pytest.mark.spec("private-names", "read", "interpret")
def test_registered_spelling_is_distinct_from_registration_and_private_use() -> None:
    # ITXT and ISO_ are registered, with different positions for the delimiter.
    assert Name("ITXT_Entry").developer_prefix == "ITXT"
    assert Name("ITXT:Entry").developer_prefix == "ITXT"
    assert Name("ISO_Entry").developer_prefix == "ISO_"
    assert Name("_Y4YEntry").developer_prefix == "_Y4Y"
    assert Name("iTXT_Entry").developer_prefix == "iTXT"
    assert Name("XX_Private").developer_prefix == "XX"
    assert Name("XX").developer_prefix == "XX"
    assert Name("ITX_Entry").developer_prefix == "ITX_"
    assert Name("ITXTEntry").developer_prefix is None
    assert Name("Type").developer_prefix is None
    assert Name("X").developer_prefix is None
    with pyropdf.open(_document()) as doc:
        names = {str(key): key.developer_prefix for key in doc.catalog if isinstance(key, Name)}
        assert names["ITXT_Flag"] == "ITXT"
        assert names["ISO_Flag"] == "ISO_"
        assert names["XXFlag"] == "XX"
        assert doc.catalog["ITXT_Flag"].developer_prefix == "ITXT"


@pytest.mark.spec("private-names", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_editing_one_private_name_preserves_colliding_suffixes_and_case(*, incremental: bool) -> None:
    with pyropdf.open(_document()) as doc:
        doc.catalog["iTXT_Flag"] = Name("iTXT:Mixed")
        doc.catalog["ITXT_Flag"] = Name("ITXT:Two")
        doc.catalog["XXFlag"] = 2
        saved = doc.to_bytes(incremental=incremental)
    with pikepdf.open(io.BytesIO(saved), inherit_page_attributes=False) as pdf:
        assert str(pdf.Root["/ITXT_Flag"]) == "/ITXT:Two"
        assert str(pdf.Root["/iTXT_Flag"]) == "/iTXT:Mixed"
        assert str(pdf.Root["/ISO_Flag"]) == "/ISO_One"
        assert pdf.Root["/XXFlag"] == 2
        assert str(pdf.Root.Pages.Resources["/XXResource"]) == "/PrivateValue"
    with pyropdf.open(saved) as doc:
        del doc.catalog["ITXT_Flag"]
        assert "ITXT_Flag" not in doc.catalog
        assert doc.catalog["iTXT_Flag"] == Name("iTXT:Mixed")
        doc.history.undo()
        assert doc.catalog["ITXT_Flag"] == Name("ITXT:Two")


@pytest.mark.spec("version-compatibility", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.parametrize(
    ("header", "declaration", "effective"),
    [
        ("1.4", "/Version /1.7", "1.7"),
        ("1.7", "/Version /1.4", "1.7"),
        ("1.7", "/Version (2.0)", "1.7"),
        ("1.7", "/Version /Invalid", "1.7"),
        ("2.0", "", "2.0"),
    ],
)
def test_header_and_catalog_declarations_select_the_effective_version(
    header: str, declaration: str, effective: str
) -> None:
    original = _document(header, declaration)
    assert pyropdf.probe(original).version == header
    with pyropdf.open(original) as doc:
        assert doc.check().version == effective
        doc.catalog["Version"] = Name("2.0")
        assert doc.check().version == "2.0"
        for incremental in (False, True):
            data = doc.to_bytes(incremental=incremental)
            with pyropdf.open(data) as reopened, pikepdf.open(io.BytesIO(data)) as independent:
                assert reopened.check().version == "2.0"
                assert str(independent.Root.Version) == "/2.0"
                assert independent.pages[0].Contents.read_bytes() == COMPAT
                if incremental:
                    assert data.startswith(original)
        doc.history.undo()
        assert doc.check().version == effective


@pytest.mark.spec("version-compatibility", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_bx_ex_keeps_unknown_operands_and_paints_known_operators(*, incremental: bool) -> None:
    with pyropdf.open(_document()) as doc, pyropdf.open(_document(paint=b"q " + KNOWN_PAINT + b" Q")) as control:
        page = doc.pages[0]
        assert page.operations.to_bytes() == COMPAT
        original = list(page.operations)
        assert original[2] == Operation("ITXT_Op", (Name("ITXT_View"), 17))
        assert page.pixmap(dpi=72).to_bytes() == control.pages[0].pixmap(dpi=72).to_bytes()
        original[2] = Operation("XXOther", (Name("XXMode"), 19))
        page.operations = original
        saved = doc.to_bytes(incremental=incremental)
        with pikepdf.open(io.BytesIO(saved), inherit_page_attributes=False) as independent:
            operations = pikepdf.parse_content_stream(independent.pages[0])
            assert [str(x.operator) for x in operations] == [x.operator for x in original]
            assert str(operations[2].operands[0]) == "/XXMode"
            assert int(operations[2].operands[1]) == 19
        with pyropdf.open(saved) as reopened:
            assert list(reopened.pages[0].operations) == original
            assert reopened.pages[0].pixmap(dpi=72).to_bytes() == control.pages[0].pixmap(dpi=72).to_bytes()


@pytest.mark.spec("version-compatibility", "interpret")
def test_poppler_paints_the_same_known_operations_in_a_compatibility_section(tmp_path: Path) -> None:
    tool = shutil.which("pdftoppm")
    if tool is None:
        pytest.skip("pdftoppm is unavailable")
    renders = []
    for name, content in (("wrapped", COMPAT), ("known", KNOWN_PAINT)):
        source = tmp_path / f"{name}.pdf"
        source.write_bytes(_document(paint=content))
        output = tmp_path / name
        subprocess.run([tool, "-r", "72", "-f", "1", "-l", "1", "-singlefile", str(source), str(output)], check=True)  # noqa: S603
        renders.append(output.with_suffix(".ppm").read_bytes())
    assert renders[0] == renders[1]
    assert bytes(3) in renders[1]


@pytest.mark.spec("procedure-sets", "read", "interpret", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_existing_procsets_resolve_through_inherited_resources_and_are_editable(*, incremental: bool) -> None:
    with pyropdf.open(_document()) as doc:
        owner = doc.catalog["Pages"]["Resources"]
        sets = pyropdf.typed.Resource(owner).ProcSet
        assert sets is not None
        assert [sets[i] for i in range(3)] == ["PDF", "Text", "ImageB"]
        before = doc.pages[0].pixmap(dpi=72).to_bytes()
        sets[2] = "ImageC"
        saved = doc.to_bytes(incremental=incremental)
    with pikepdf.open(io.BytesIO(saved), inherit_page_attributes=False) as independent:
        resources = independent.Root.Pages.Resources
        assert [str(name) for name in resources.ProcSet] == ["/PDF", "/Text", "/ImageC"]
        assert resources.ProcSet.is_indirect
        assert str(resources["/XXResource"]) == "/PrivateValue"
        assert all("/Resources" not in page.obj for page in independent.pages)
    with pyropdf.open(saved) as reopened:
        assert reopened.pages[0].pixmap(dpi=72).to_bytes() == before
        assert reopened.pages[0].operations.to_bytes() == COMPAT


@pytest.mark.spec("version-compatibility", "read", "interpret")
def test_compatibility_operators_and_unknown_operators_each_consume_an_operation() -> None:
    count = len(pyropdf.Operations.parse(COMPAT))
    with pytest.raises(pyropdf.LimitError) as refused:
        pyropdf.Operations.parse(COMPAT, max_operations=count - 1)
    assert refused.value.code == "content_operation_count"
    assert pyropdf.Operations.parse(COMPAT, max_operations=count).to_bytes() == COMPAT


@pytest.mark.spec("procedure-sets", "read", "interpret", "edit")
def test_a_wrong_procset_type_is_reported_and_new_pages_omit_legacy_procsets() -> None:
    with pyropdf.open(_document()) as doc:
        resource = doc.catalog["Pages"]["Resources"]
        resource["ProcSet"] = Name("Text")
        with pytest.raises(pyropdf.PdfError, match="ProcSet"):
            _ = pyropdf.typed.Resource(resource).ProcSet
        doc.history.undo()
        assert pyropdf.typed.Resource(resource).ProcSet is not None
    with pyropdf.Document() as doc:
        page = doc.pages.new((40, 40))
        with page.draw() as canvas:
            canvas.text("Current", (2, 20))
        with pikepdf.open(io.BytesIO(doc.to_bytes())) as writer:
            assert "/ProcSet" not in writer.pages[0].Resources
