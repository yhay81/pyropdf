"""Write the corpus broken on purpose that the structural check must catch.

Each file is one valid page with one defect, named in ``DEFECTS`` with the
object, key path, Arlington clause, and kind that the check reports. The
script needs no library, and its output is the same on every run:

    uv run python tests/assets/broken/generate.py
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent

CATALOG = b"<< /Type /Catalog /Pages 2 0 R >>"
PAGES = b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
PAGE = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> /Contents 4 0 R >>"
CONTENT = b"<< /Length 0 >>\nstream\n\nendstream"


def pdf(objects: list[bytes], version: str = "1.7") -> bytes:
    """Return a file whose objects are numbered from 1 in order, with object 1 as its root."""
    out = bytearray(f"%PDF-{version}\n%\xe2\xe3\xcf\xd3\n".encode("latin-1"))
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, start)
    return bytes(out)


def page(objects: dict[int, bytes] | None = None, version: str = "1.7") -> bytes:
    """Return the valid page with the objects of ``objects`` replaced."""
    base = {1: CATALOG, 2: PAGES, 3: PAGE, 4: CONTENT} | (objects or {})
    return pdf([base[number] for number in sorted(base)], version)


#: Each file, its bytes, and the defect it holds: object, path, clause, kind.
DEFECTS: dict[str, tuple[bytes, tuple[int | None, str, str, str]]] = {
    "type.pdf": (
        page({2: b"<< /Type /Pages /Kids [3 0 R] /Count (one) >>"}),
        (2, "Count", "PageTreeNodeRoot/Count", "type"),
    ),
    "required.pdf": (
        page({3: b"<< /Type /Page /Parent 2 0 R /Resources << >> /Contents 4 0 R >>"}),
        (3, "MediaBox", "PageObject/MediaBox", "required"),
    ),
    "version.pdf": (
        page({1: b"<< /Type /Catalog /Pages 2 0 R /Lang (en) >>"}, version="1.3"),
        (1, "Lang", "Catalog/Lang", "version"),
    ),
    "indirect.pdf": (
        page({1: b"<< /Type /Catalog /Pages << /Type /Pages /Kids [3 0 R] /Count 1 >> >>"}),
        (1, "Pages", "Catalog/Pages", "indirect"),
    ),
    "value.pdf": (
        page({1: b"<< /Type /Catalog /Pages 2 0 R /PageLayout /Sideways >>"}),
        (1, "PageLayout", "Catalog/PageLayout", "value"),
    ),
    "trailer.pdf": (
        pdf([CATALOG, PAGES, PAGE, CONTENT]).replace(b"/Size 5 /Root", b"/Size 0 /Root"),
        (None, "Size", "FileTrailer/Size", "value"),
    ),
    "count.pdf": (
        page({2: b"<< /Type /Pages /Kids [3 0 R] /Count 2 >>"}),
        (2, "Count", "PageTreeNode/Count", "invariant"),
    ),
    "parent.pdf": (
        page({3: PAGE.replace(b"/Parent 2 0 R", b"/Parent 1 0 R")}),
        (3, "Parent", "PageTreeNode/Kids", "invariant"),
    ),
    "order.pdf": (
        page(
            {
                1: b"<< /Type /Catalog /Pages 2 0 R"
                b" /Names << /Dests << /Names [(b) [3 0 R /Fit] (a) [3 0 R /Fit]] >> >> >>"
            }
        ),
        (1, "Names/Dests/Names/2", "NameTree/Names", "invariant"),
    ),
    "struct-parent.pdf": (
        page(
            {
                1: b"<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 5 0 R >>",
                5: b"<< /Type /StructTreeRoot /K 6 0 R >>",
                6: b"<< /Type /StructElem /S /P /P 1 0 R >>",
            }
        ),
        (6, "P", "StructTreeRoot/K", "invariant"),
    ),
    "next-key.pdf": (
        page(
            {
                1: b"<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 5 0 R >>",
                5: b"<< /Type /StructTreeRoot /K 6 0 R /ParentTree << /Nums [0 6 0 R] >> /ParentTreeNextKey 0 >>",
                6: b"<< /Type /StructElem /S /P /P 5 0 R >>",
            }
        ),
        (5, "ParentTreeNextKey", "StructTreeRoot/ParentTreeNextKey", "invariant"),
    ),
    "limits.pdf": (
        page(
            {
                1: b"<< /Type /Catalog /Pages 2 0 R"
                b" /PageLabels << /Nums [0 << /S /D >> 2 << /S /r >>] /Limits [0 3] >> >>"
            }
        ),
        (1, "PageLabels/Limits", "NumberTree/Limits", "invariant"),
    ),
}

#: A page tree whose root lists itself among its kids, which the walk must
#: visit once.
CYCLE = page({2: b"<< /Type /Pages /Kids [3 0 R 2 0 R] /Count 1 >>"})


def chain(depth: int) -> bytes:
    """Return a page tree ``depth`` nodes deep, one node per object."""
    objects = [CATALOG]
    for number in range(2, depth + 2):
        parent = b"" if number == 2 else b"/Parent %d 0 R " % (number - 1)
        objects.append(b"<< /Type /Pages %s/Kids [%d 0 R] /Count 1 >>" % (parent, number + 1))
    last = depth + 1
    objects.append(PAGE.replace(b"/Parent 2 0 R", b"/Parent %d 0 R" % last).replace(b"4 0 R", b"%d 0 R" % (last + 2)))
    objects.append(CONTENT)
    return pdf(objects)


def main() -> None:
    for name, (data, _) in DEFECTS.items():
        (HERE / name).write_bytes(data)
    (HERE / "cycle.pdf").write_bytes(CYCLE)


if __name__ == "__main__":
    main()
