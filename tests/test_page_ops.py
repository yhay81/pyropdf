"""Copying a page within a document with ``insert`` and ``extend``.

The copy cases of pylopdf's tests/test_page_ops.py at ebb01d3c, ported.
Intended differences:

- the inherited geometry is read through ``page.rect``, ``rotation``, and
  the copy's own dictionary, since pyropdf has no ``page.boxes``;
- a copy repairs a page whose ``/Parent`` names a cycle instead of
  refusing, as ``new`` does in tests/test_pages.py, since the walk of
  ``/Kids``, not ``/Parent``, decides the pages.

Its import cases wait on yhay81/pyropdf#176; its ``new`` and ``del``
cases are in tests/test_pages.py.
"""

from __future__ import annotations

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
