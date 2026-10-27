"""Copying and importing pages with ``insert`` and ``extend``.

The copy and import cases of pylopdf's tests/test_page_ops.py at ebb01d3c,
ported. Intended differences:

- the inherited geometry is read through ``page.rect``, ``rotation``, and
  the page's own dictionary, since pyropdf has no ``page.boxes``;
- a copy or an import repairs a page whose ``/Parent`` names a cycle
  instead of refusing, as ``new`` does in tests/test_pages.py, since the
  walk of ``/Kids``, not ``/Parent``, decides the pages;
- the unreachable source data is an embedded file of the source's catalog,
  since pyropdf has no ``attachments`` yet.

Its ``new`` and ``del`` cases are in tests/test_pages.py.
"""

from __future__ import annotations

import pytest

import pyropdf


def build_raw_pdf(objects: dict[int, str]) -> bytes:
    """Return a PDF of ``objects`` by number, with a correct cross-reference table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode()
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 00000 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def build_pdf(texts: list[str]) -> bytes:
    """Return one page per text, with MediaBox and Resources on the tree's root."""
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(texts)))
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {len(texts)} /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 3 0 R >> >> >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for i, text in enumerate(texts):
        content = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET"
        objects[4 + 2 * i] = f"<< /Type /Page /Parent 2 0 R /Contents {5 + 2 * i} 0 R >>"
        objects[5 + 2 * i] = f"<< /Length {len(content)} >>\nstream\n{content}\nendstream"
    return build_raw_pdf(objects)


def test_copy_page_append_and_position() -> None:
    doc = pyropdf.open(build_pdf(["Page one", "Page two", "Page three"]))
    doc.pages.extend(doc.pages[0])
    assert len(doc.pages) == 4
    assert "Page one" in doc.pages[3].text()
    doc.pages.insert(0, doc.pages[2])  # Copy the current Page three to the front.
    assert len(doc.pages) == 5
    assert "Page three" in doc.pages[0].text()
    reloaded = pyropdf.open(doc.to_bytes())
    assert len(reloaded.pages) == 5
    assert "Page three" in reloaded.pages[0].text()
    assert reloaded.pages[0].pixmap(alpha=True).to_bytes() == reloaded.pages[3].pixmap(alpha=True).to_bytes()


def test_copies_inherit_past_attributes_that_name_missing_objects() -> None:
    """A copy reads a missing-object attribute as omitted and keeps what its original inherits."""
    attributes = " ".join(f"/{key} 99 0 R" for key in ("MediaBox", "CropBox", "Rotate", "Resources"))
    content = "BT /F1 20 Tf 40 80 Td (MISSING-REFERENCES) Tj ET"
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 /MediaBox [0 0 612 792] >>",
                3: "<< /Type /Pages /Parent 2 0 R /Kids [4 0 R] /Count 1 /CropBox [0 0 300 400] /Rotate 90 "
                "/Resources << /Font << /F1 5 0 R >> >> >>",
                4: f"<< /Type /Page /Parent 3 0 R /Contents 7 0 R {attributes} >>",
                5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                6: "<< /Type /Page /Parent 2 0 R >>",
                7: f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            }
        )
    )
    geometry = (pyropdf.Rect(0, 0, 400, 300), 90)
    assert (doc.pages[0].rect, doc.pages[0].rotation) == geometry
    expected_png = doc.pages[0].pixmap(alpha=True).to_bytes()

    doc.pages.extend(doc.pages[0])

    for current in (doc, pyropdf.open(doc.to_bytes())):
        page = current.pages[2]
        assert (page.rect, page.rotation) == geometry
        assert list(page.obj["CropBox"]) == [0, 0, 300, 400]
        assert page.pixmap(alpha=True).to_bytes() == expected_png


def test_a_copy_repairs_a_page_whose_parent_is_a_cycle() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
                4: "<< /Type /Page /Parent 5 0 R /MediaBox [0 0 100 110] >>",
                5: "<< /Type /Pages /Parent 5 0 R >>",
            }
        )
    )
    existing = doc.pages[0]

    doc.pages.extend(doc.pages[1])

    assert existing.index == 0
    reopened = pyropdf.open(doc.to_bytes())
    assert [page.rect for page in reopened.pages] == [pyropdf.Rect(0, 0, 100, 100), *[pyropdf.Rect(0, 0, 100, 110)] * 2]
    assert reopened.pages[2].obj["Parent"].xref == 2


def test_insert_pdf_negative_range_follows_pymupdf() -> None:
    doc = pyropdf.Document()
    doc.pages.extend(pyropdf.open(build_pdf(["Page one", "Page two", "Page three"])).pages)
    assert len(doc.pages) == 3
    assert "Page one" in doc.pages[0].text()
    # A descending slice reverses.
    clamped = pyropdf.Document()
    clamped.pages.extend(pyropdf.open(build_pdf(["Page one", "Page two", "Page three"])).pages[1:100][::-1])
    assert [clamped.pages[index].text().strip() for index in range(2)] == ["Page three", "Page two"]


def test_insert_pdf_start_at() -> None:
    doc = pyropdf.open(build_pdf(["Page one", "Page two", "Page three"]))
    src = pyropdf.open(build_pdf(["Hello PDF"]))
    doc.pages.insert(1, src.pages)
    assert len(doc.pages) == 4
    assert "Page one" in doc.pages[0].text()
    assert "Hello PDF" in doc.pages[1].text()
    assert "Page two" in doc.pages[2].text()
    # Preserve page order after save and reload.
    reloaded = pyropdf.open(doc.to_bytes())
    assert "Hello PDF" in reloaded.pages[1].text()
    assert len(src.pages) == 1


def test_insert_pdf_empty_source_noop() -> None:
    doc = pyropdf.open(build_pdf(["Page one", "Page two", "Page three"]))
    before = doc.to_bytes()
    doc.pages.extend(pyropdf.Document().pages)
    assert len(doc.pages) == 3
    assert doc.to_bytes() == before


def test_insert_pdf_does_not_keep_unreachable_source_data() -> None:
    """Do not copy what the pages do not reach, such as the source's embedded files."""
    hidden = "SECRET-UNREFERENCED-ATTACHMENT-7c3f"
    source = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << /Names [(secret.txt) 4 0 R] >> >> >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
                4: "<< /Type /Filespec /F (secret.txt) /EF << /F 5 0 R >> >>",
                5: f"<< /Type /EmbeddedFile /Length {len(hidden)} >>\nstream\n{hidden}\nendstream",
            }
        )
    )

    target = pyropdf.Document()
    target.pages.extend(source.pages)

    assert len(target.pages) == 1
    assert hidden.encode() not in target.to_bytes()
    assert "Names" not in target.catalog


def test_insert_pdf_native_argument_refusal_keeps_existing_pages() -> None:
    doc = pyropdf.open(build_pdf(["Page one", "Page two", "Page three"]))
    source = pyropdf.open(build_pdf(["Hello PDF"]))
    page = doc.pages[0]
    before = doc.to_bytes()

    with pytest.raises(TypeError):
        doc.pages.insert(0.5, source.pages)  # ty: ignore[invalid-argument-type]

    assert doc.to_bytes() == before
    assert "Page one" in page.text()


def _build_cropped_parent_pdf() -> bytes:
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: ("<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 612 792] /CropBox [0 0 80 100] /Rotate 90 >>"),
            3: "<< /Type /Page /Parent 2 0 R >>",
        }
    )


@pytest.mark.parametrize("source_attributes", ["", "/CropBox [5 7 305 407] /Rotate 180"])
def test_insert_pdf_preserves_geometry_under_cropped_rotated_parent(source_attributes: str) -> None:
    source = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 612 792] >>",
                3: f"<< /Type /Page /Parent 2 0 R {source_attributes} >>",
            }
        )
    )
    expected = (source.pages[0].rect, source.pages[0].rotation)
    expected_png = source.pages[0].pixmap(alpha=True).to_bytes()
    source_bytes = source.to_bytes()
    target = pyropdf.open(_build_cropped_parent_pdf())
    original_png = target.pages[0].pixmap(alpha=True).to_bytes()

    target.pages.extend(source.pages)

    assert (target.pages[1].rect, target.pages[1].rotation) == expected
    assert target.pages[1].pixmap(alpha=True).to_bytes() == expected_png
    assert target.pages[0].pixmap(alpha=True).to_bytes() == original_png
    assert source.to_bytes() == source_bytes
    reopened = pyropdf.open(target.to_bytes())
    assert (reopened.pages[1].rect, reopened.pages[1].rotation) == expected
    assert reopened.pages[1].pixmap(alpha=True).to_bytes() == expected_png


@pytest.mark.parametrize("null_value", ["null", "5 0 R", "99 0 R"], ids=["null", "indirect-null", "missing-object"])
@pytest.mark.parametrize("inherited_values", [False, True])
def test_insert_pdf_treats_null_page_and_parent_attributes_as_absent(
    *, null_value: str, inherited_values: bool
) -> None:
    """A null attribute is omitted, so the page inherits from its source ancestors.

    ISO 32000-2 section 7.3.10 reads a reference to a missing object as null.
    """
    attributes = " ".join(f"/{key} {null_value}" for key in ("MediaBox", "CropBox", "Rotate", "Resources"))
    inherited = "/CropBox [0 0 300 400] /Rotate 180 /Resources << /Font << /F1 6 0 R >> >>" if inherited_values else ""
    content = "BT /F1 20 Tf 40 80 Td (NULL-INHERITANCE) Tj ET" if inherited_values else ""
    source = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: f"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 612 792] {inherited} >>",
                3: f"<< /Type /Pages /Parent 2 0 R /Kids [4 0 R] /Count 1 {attributes} >>",
                4: f"<< /Type /Page /Parent 3 0 R /Contents 7 0 R {attributes} >>",
                5: "null",
                6: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                7: f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            }
        )
    )
    expected_rect = pyropdf.Rect(0, 0, 300, 400) if inherited_values else pyropdf.Rect(0, 0, 612, 792)
    expected_rotation = 180 if inherited_values else 0
    assert (source.pages[0].rect, source.pages[0].rotation) == (expected_rect, expected_rotation)
    expected_png = source.pages[0].pixmap(alpha=True).to_bytes()
    source_bytes = source.to_bytes()
    target = pyropdf.open(_build_cropped_parent_pdf())

    target.pages.extend(source.pages)

    for current in (target, pyropdf.open(target.to_bytes())):
        assert (current.pages[1].rect, current.pages[1].rotation) == (expected_rect, expected_rotation)
        assert current.pages[1].pixmap(alpha=True).to_bytes() == expected_png
        if inherited_values:
            assert current.pages[1].text().split() == ["NULL-INHERITANCE"]
    assert source.to_bytes() == source_bytes


def test_an_import_repairs_a_page_whose_parent_is_a_cycle() -> None:
    target = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
                4: "<< /Type /Page /Parent 5 0 R /MediaBox [0 0 100 110] >>",
                5: "<< /Type /Pages /Parent 5 0 R >>",
            }
        )
    )
    source = pyropdf.open(build_pdf(["IMPORTED"]))

    target.pages.extend(source.pages)

    reopened = pyropdf.open(target.to_bytes())
    assert len(reopened.pages) == 3
    assert reopened.pages[2].text().split() == ["IMPORTED"]


def _linked(count: int) -> bytes:
    """Return ``count`` pages, page ``i`` linking to page ``i + 1``, and the last to a named destination of page 0."""
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(count))
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /Dests << /home [3 0 R /Fit] >> >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {count} /MediaBox [0 0 200 200] >>",
    }
    for i in range(count):
        target = f"/Dest [{5 + 2 * i} 0 R /XYZ 10 20 null]" if i < count - 1 else "/A << /S /GoTo /D /home >>"
        objects[3 + 2 * i] = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {100 + i} 200] /Annots [{4 + 2 * i} 0 R] >>"
        objects[4 + 2 * i] = f"<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] {target} >>"
    return build_raw_pdf(objects)


def test_imported_links_follow_the_pages_imported_with_them() -> None:
    source = pyropdf.open(_linked(3))
    target = pyropdf.open(build_pdf(["Page one"]))

    target.pages.extend([source.pages[2], source.pages[1], source.pages[0]])

    widths = [page.rect.x1 for page in target.pages[1:]]
    assert widths == [102, 101, 100]
    last, middle, first = target.pages[1:]
    assert last.links[0].target == pyropdf.Destination(3)
    assert middle.links[0].target == pyropdf.Destination(1, (10, 180))
    assert first.links[0].target == pyropdf.Destination(2, (10, 180))
    assert last.links[0].obj["A"]["D"][0].xref == first.xref

    partial = pyropdf.Document()
    partial.pages.extend(source.pages[1])
    assert len(partial.pages[0].links) == 0
    assert b"/Link" not in partial.to_bytes()
    assert len(source.pages[1].links) == 1


def test_imported_pages_leave_the_source_structure_behind() -> None:
    """Until their elements are grafted (yhay81/pyropdf#189), imported pages carry no structure keys."""
    source = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 5 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /StructParents 0 /Annots [4 0 R] >>",
                4: "<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] /StructParent 1 /P 3 0 R "
                "/A << /S /URI /URI (https://example.test/) >> >>",
                5: "<< /Type /StructTreeRoot /K 6 0 R /ParentTree << /Nums [0 [6 0 R] 1 6 0 R] >> >>",
                6: "<< /Type /StructElem /S /Link /P 5 0 R /Pg 3 0 R /K [0 << /Type /OBJR /Obj 4 0 R >>] >>",
            }
        )
    )
    target = pyropdf.Document()

    target.pages.extend(source.pages)

    (page,) = target.pages
    assert "StructParents" not in page.obj
    (link,) = page.links
    assert "StructParent" not in link.obj
    assert link.obj["P"].xref == page.xref
    assert "StructTreeRoot" not in target.catalog
    assert b"StructElem" not in target.to_bytes()
