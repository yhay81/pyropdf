"""``Document.page_labels`` and ``Page.label``, ported from pylopdf's tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_pages import build_raw_pdf, numbered

import pyropdf
from pyropdf import PageLabelRange, PageLabelStyle

ROOT = Path(__file__).resolve().parents[1]
#: PyMuPDF's ``get_label`` of every corpus page, from ``evidence/pymupdf-comparison/labels.py``.
ORACLE = ROOT / "evidence" / "pymupdf-comparison" / "results" / "page-labels.json"
ONE_PAGE = {
    2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
}


def _six_page_doc() -> pyropdf.Document:
    return pyropdf.open(numbered(6))


def _labels(doc: pyropdf.Document) -> list[str]:
    return [page.label for page in doc.pages]


def _tree(tree: str, **objects: str) -> pyropdf.Document:
    """Return one page whose catalog's ``/PageLabels`` is ``4 0 R``, object 4 being ``tree``."""
    extra = {int(key[1:]): value for key, value in objects.items()}
    return pyropdf.open(
        build_raw_pdf({1: "<< /Type /Catalog /Pages 2 0 R /PageLabels 4 0 R >>", **ONE_PAGE, 4: tree, **extra})
    )


@pytest.mark.parametrize("name", sorted(json.loads(ORACLE.read_text(encoding="utf-8"))["labels"]))
def test_labels_of_the_corpus_match_pymupdf(name: str) -> None:
    expected = json.loads(ORACLE.read_text(encoding="utf-8"))["labels"][name]
    with pyropdf.open(ROOT / "tests" / "assets" / "real_world" / name) as doc:
        assert _labels(doc) == expected


def test_assign_and_compute_labels() -> None:
    doc = _six_page_doc()
    doc.page_labels = [PageLabelRange(0, PageLabelStyle.LOWER_ROMAN), PageLabelRange(3, prefix="A-")]
    assert _labels(doc) == ["i", "ii", "iii", "A-1", "A-2", "A-3"]
    assert doc.pages.labeled("A-2") == [4]
    assert doc.pages.labeled("") == []


def test_assigning_is_one_undoable_step() -> None:
    doc = _six_page_doc()
    doc.page_labels = [PageLabelRange(0, PageLabelStyle.LOWER_ROMAN), PageLabelRange(3, prefix="A-")]
    doc.history.undo()
    assert doc.page_labels == []


def test_labels_roundtrip_through_save() -> None:
    doc = _six_page_doc()
    ranges = [
        PageLabelRange(0, PageLabelStyle.UPPER_ROMAN, first=5),
        PageLabelRange(2, PageLabelStyle.LOWER_LETTERS, "付-"),
    ]
    doc.page_labels = reversed(ranges)  # Ranges are written in page order.
    reopened = pyropdf.open(doc.to_bytes())
    assert reopened.page_labels == ranges
    assert _labels(reopened)[:3] == ["V", "VI", "付-a"]


def test_styles_take_members_or_their_values() -> None:
    assert PageLabelRange(0, "r") == PageLabelRange(0, PageLabelStyle.LOWER_ROMAN)
    assert PageLabelRange(0).style is PageLabelStyle.DECIMAL
    assert repr(PageLabelRange(2, None, "x")) == "PageLabelRange(start=2, style=None, prefix='x', first=1)"
    with pytest.raises(ValueError, match="style"):
        PageLabelRange(0, "X")


def test_prefix_only_style() -> None:
    doc = _six_page_doc()
    doc.page_labels = [PageLabelRange(0, None, "表紙")]
    assert doc.page_labels == [PageLabelRange(0, None, "表紙")]
    assert doc.pages[0].label == doc.pages[5].label == "表紙"


def test_letters_style_wraps_past_z() -> None:
    doc = _six_page_doc()
    doc.page_labels = [PageLabelRange(0, PageLabelStyle.UPPER_LETTERS, first=26)]
    assert _labels(doc)[:3] == ["Z", "AA", "BB"]


def test_absent_and_removed_labels() -> None:
    doc = _six_page_doc()
    assert doc.page_labels == []
    assert doc.pages[0].label == ""
    doc.page_labels = [PageLabelRange(0)]
    doc.page_labels = []  # An empty list removes labels.
    assert doc.page_labels == []
    assert "PageLabels" not in doc.catalog
    doc.page_labels = [PageLabelRange(0)]
    del doc.page_labels
    assert doc.page_labels == []
    assert doc.pages[0].label == ""
    del doc.page_labels  # Removing absent labels changes nothing.


@pytest.mark.parametrize(
    ("make", "error", "match"),
    [
        (lambda: PageLabelRange(-1), ValueError, "start"),
        (lambda: PageLabelRange(0, first=0), ValueError, "first"),
        (lambda: PageLabelRange(True), TypeError, "start"),  # noqa: FBT003 - the wrong type on purpose
        (lambda: PageLabelRange(0, prefix=5), TypeError, "prefix"),  # ty: ignore[invalid-argument-type]
    ],
)
def test_ranges_validate_on_construction(make: object, error: type[Exception], match: str) -> None:
    with pytest.raises(error, match=match):
        make()  # ty: ignore[call-non-callable]


def test_assigning_validates_the_ranges_together() -> None:
    doc = _six_page_doc()
    with pytest.raises(ValueError, match="page 0"):
        doc.page_labels = [PageLabelRange(2)]
    with pytest.raises(ValueError, match="distinct"):
        doc.page_labels = [PageLabelRange(0, None), PageLabelRange(0)]
    with pytest.raises(TypeError, match="PageLabelRange"):
        doc.page_labels = [{"startpage": 0}]  # ty: ignore[invalid-assignment]
    assert doc.page_labels == []


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (lambda pages: pages.move(0, 5), ["ii", "iii", "A-1", "A-2", "A-3", "i"]),
        (lambda pages: pages.__delitem__(1), ["i", "iii", "A-1", "A-2", "A-3"]),
        (lambda pages: pages.select([3, 0]), ["A-1", "i"]),
        (lambda pages: pages.new((100, 100)), None),
        (lambda pages: pages.insert(0, pages[5]), None),
    ],
)
def test_page_operations_keep_each_pages_label(edit: object, expected: list[str] | None) -> None:
    doc = _six_page_doc()
    doc.page_labels = [PageLabelRange(0, PageLabelStyle.LOWER_ROMAN), PageLabelRange(3, prefix="A-")]
    before = {page.obj.xref: page.label for page in doc.pages}
    edit(doc.pages)  # ty: ignore[call-non-callable]
    kept = [(before[page.obj.xref], page.label) for page in doc.pages if page.obj.xref in before]
    assert kept
    assert all(old == new for old, new in kept)
    if expected is not None:
        assert _labels(doc) == expected


def test_page_label_tree_with_kids_is_read_in_order() -> None:
    doc = _tree(
        "<< /Kids [5 0 R 6 0 R] >>",
        o5="<< /Limits [0 0] /Nums [0 << /S /r >>] >>",
        o6="<< /Limits [3 3] /Nums [3 << /S /D /P (A-) /St 7 >>] >>",
    )
    assert doc.page_labels == [PageLabelRange(0, "r"), PageLabelRange(3, "D", "A-", 7)]


def test_page_label_tree_reference_cycle_is_visited_once() -> None:
    doc = _tree("<< /Nums [0 << /S /D /P (A-) >>] /Kids [4 0 R] >>")
    assert doc.page_labels == [PageLabelRange(0, PageLabelStyle.DECIMAL, "A-")]
    assert doc.pages[0].label == "A-1"


@pytest.mark.parametrize("source", ["raw_pdf", "catalog"])
def test_unsupported_stored_style_reads_as_prefix_only(source: str) -> None:
    if source == "raw_pdf":
        doc = _tree("<< /Nums [0 << /S /Unknown /P (Appendix-) >>] >>")
    else:
        doc = _six_page_doc()
        doc.catalog["PageLabels"] = {"Nums": [0, {"S": pyropdf.Name("Unknown"), "P": "Appendix-"}]}
    expected = [PageLabelRange(0, None, "Appendix-")]
    assert doc.page_labels == expected
    assert _labels(doc) == ["Appendix-"] * len(doc.pages)
    assert doc.pages.labeled("Appendix-") == list(range(len(doc.pages)))
    reopened = pyropdf.open(doc.to_bytes())
    assert reopened.page_labels == expected
    reopened.page_labels = reopened.page_labels
    assert "S" not in reopened.catalog["PageLabels"]["Nums"][1]
    assert pyropdf.open(reopened.to_bytes()).page_labels == expected


def test_page_label_tree_rejects_excessive_depth() -> None:
    objects = {f"o{number}": f"<< /Kids [{number + 1} 0 R] >>" for number in range(5, 37)}
    doc = _tree("<< /Kids [5 0 R] >>", **objects, o37="<< /Nums [0 << /S /D >>] >>")
    with pytest.raises(pyropdf.LimitError, match="32 levels") as caught:
        _ = doc.page_labels
    assert caught.value.code == "page_label_tree_depth"
    with pytest.raises(pyropdf.LimitError, match="32 levels"):
        _ = doc.pages[0].label


def test_page_label_tree_rejects_excessive_edges() -> None:
    doc = _tree(f"<< /Nums [0 << /S /D >>] /Kids [{'5 0 R ' * 4097}] >>", o5="<< /Nums [1 << /S /D >>] >>")
    with pytest.raises(pyropdf.LimitError, match="4096 kids") as caught:
        _ = doc.page_labels
    assert caught.value.code == "page_label_tree_edges"


def test_page_label_tree_rejects_excessive_entries() -> None:
    doc = _tree(f"<< /Nums [{' '.join(f'{index} << /S /D >>' for index in range(4097))}] >>")
    with pytest.raises(pyropdf.LimitError, match="4096 ranges") as caught:
        _ = doc.page_labels
    assert caught.value.code == "page_label_ranges"


def test_page_label_tree_rejects_excessive_text() -> None:
    doc = _tree(f"<< /Nums [0 << /P ({'x' * (1024 * 1024 + 1)}) >>] >>")
    with pytest.raises(pyropdf.LimitError, match="1048576 bytes") as caught:
        _ = doc.page_labels
    assert caught.value.code == "page_label_text_size"


def test_assigning_refuses_over_limit_ranges_atomically() -> None:
    doc = _six_page_doc()
    before = doc.to_bytes()
    with pytest.raises(ValueError, match="4096 ranges"):
        doc.page_labels = [PageLabelRange(index) for index in range(4097)]
    assert doc.to_bytes() == before
    with pytest.raises(pyropdf.LimitError, match="1048576 bytes") as caught:
        doc.page_labels = [PageLabelRange(0, prefix="x" * (1024 * 1024 + 1))]
    assert caught.value.code == "page_label_input_size"
    assert doc.to_bytes() == before
