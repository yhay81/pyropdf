"""Linearized saves and the detection of linearized files (ISO 32000-2 Annex F)."""

from __future__ import annotations

import io
import re
import shutil
import subprocess
from pathlib import Path

import pikepdf
import pytest

import pyropdf
from pyropdf import Encryption

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "assets" / "real_world"


def build_raw_pdf(objects: dict[int, str]) -> bytes:
    """Return a PDF of ``objects`` by number; absent numbers below the highest are free entries."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode("latin-1")
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 65535 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def qpdf_accepts(data: bytes, password: str = "") -> None:
    """Assert that qpdf, through pikepdf, finds no linearization error or warning in ``data``."""
    with pikepdf.open(io.BytesIO(data), password=password) as pdf:
        assert pdf.is_linearized
        report = io.StringIO()
        assert pdf.check_linearization(report), report.getvalue()
        assert report.getvalue() == ""


def page_texts(doc: pyropdf.Document) -> list[str]:
    """Return the text of every page."""
    return [page.text() for page in doc.pages]


@pytest.mark.parametrize(
    "name",
    [
        # Four pages with thumbnails and inherited boxes.
        "patent-us223898.pdf",
        # Outlines that the document opens showing, so they go with the first page.
        "usrguide.pdf",
        # A form whose fields the catalog names.
        "f1040.pdf",
        "wdl6812-manuscript.pdf",
    ],
)
@pytest.mark.spec("linearization", "create", "edit")
def test_a_linearized_corpus_file_passes_qpdf_and_reads_the_same(name: str) -> None:
    with pyropdf.open(CORPUS / name) as doc:
        data = doc.to_bytes(linearize=True)
        qpdf_accepts(data)
        with pyropdf.open(data) as saved:
            assert saved.is_linearized
            assert [page.rect for page in saved.pages] == [page.rect for page in doc.pages]
            assert page_texts(saved) == page_texts(doc)
            assert dict(saved.metadata) == dict(doc.metadata)
            assert saved.outline.entries() == doc.outline.entries()


def test_the_first_page_comes_first_and_the_hint_stream_follows_the_catalog() -> None:
    data = pyropdf.open(CORPUS / "patent-us223898.pdf").to_bytes(linearize=True)
    with pikepdf.open(io.BytesIO(data)) as pdf:
        first = pdf.pages[0].obj
        # The first page is the object that the linearization dictionary names.
        head = data[: data.index(b"endobj")]
        assert f"/O {first.objgen[0]} ".encode() in head
        assert data.index(f"\n{first.objgen[0]} 0 obj".encode()) < data.index(
            f"\n{pdf.pages[1].obj.objgen[0]} 0 obj".encode()
        )


def test_a_linearized_save_to_a_path_is_linearized(tmp_path: Path) -> None:
    out = tmp_path / "linear.pdf"
    pyropdf.open(CORPUS / "patent-us223898.pdf").save(out, linearize=True)
    qpdf_accepts(out.read_bytes())
    assert pyropdf.open(out).is_linearized


@pytest.mark.spec("incremental-update", "interpret")
@pytest.mark.spec("linearization", "read", "interpret")
def test_detection_reads_the_linearization_dictionary_and_the_file_length(tmp_path: Path) -> None:
    source = CORPUS / "patent-us223898.pdf"
    assert not pyropdf.open(source).is_linearized
    linear = tmp_path / "qpdf.pdf"
    with pikepdf.open(source) as pdf:
        pdf.save(linear, linearize=True)
    doc = pyropdf.open(linear)
    assert doc.is_linearized
    # Edits in memory do not change what the opened file is.
    doc.metadata = {"title": "Edited"}
    assert doc.is_linearized
    # An update after the linearized file changes its length.
    updated = pyropdf.open(doc.to_bytes(incremental=True))
    assert not updated.is_linearized
    new = pyropdf.Document()
    new.pages.new()
    assert not new.is_linearized
    assert pyropdf.open(new.to_bytes(linearize=True)).is_linearized


def test_inherited_attributes_move_into_the_pages() -> None:
    data = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 3 /MediaBox [0 0 200 300] /Resources 7 0 R /Rotate 90 >>",
            3: "<< /Type /Pages /Parent 2 0 R /Kids [4 0 R 6 0 R] /Count 2 /CropBox [0 0 100 100] >>",
            4: "<< /Type /Page /Parent 3 0 R >>",
            5: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 50 60] /Rotate 0 >>",
            6: "<< /Parent 3 0 R >>",
            7: "<< /ProcSet [/PDF] >>",
        }
    )
    doc = pyropdf.open(data)
    saved_bytes = doc.to_bytes(linearize=True)
    qpdf_accepts(saved_bytes)
    saved = pyropdf.open(saved_bytes)
    assert [page.rect for page in saved.pages] == [page.rect for page in doc.pages]
    assert [page.rotation for page in saved.pages] == [page.rotation for page in doc.pages]
    with pikepdf.open(io.BytesIO(saved_bytes)) as pdf:
        tree = pdf.Root.Pages
        assert all(key not in tree for key in ("/MediaBox", "/Resources", "/Rotate"))
        assert "/CropBox" not in tree.Kids[0]
        pages = pdf.pages
        assert [list(page.obj.MediaBox) for page in pages] == [[0, 0, 200, 300], [0, 0, 200, 300], [0, 0, 50, 60]]
        assert [page.obj.get("/CropBox") is not None for page in pages] == [True, True, False]
        # The pages share the node's resources, and a page without /Type gets one.
        assert pages[0].obj.Resources.objgen == pages[1].obj.Resources.objgen
        assert pages[1].obj.Type == "/Page"
    # The document in memory keeps its page tree.
    assert "MediaBox" in doc.catalog["Pages"]


@pytest.mark.parametrize("method", ["aes-256", "aes-128"])
def test_a_linearized_save_encrypts_every_object_but_the_encryption_dictionary(method: str) -> None:
    doc = pyropdf.open(CORPUS / "f1040.pdf")
    data = doc.to_bytes(linearize=True, encryption=Encryption(user_password="secret", method=method))  # ty: ignore[invalid-argument-type]  # noqa: S106
    qpdf_accepts(data, password="secret")  # noqa: S106 - public test fixture.
    locked = pyropdf.open(data)
    assert locked.needs_password
    locked.unlock("secret")
    assert locked.is_linearized
    assert page_texts(locked) == page_texts(doc)
    # Keeping the encryption re-encrypts each object with its new number.
    kept = locked.to_bytes(linearize=True, encryption=Encryption.KEEP)
    qpdf_accepts(kept, password="secret")  # noqa: S106 - public test fixture.
    assert page_texts(pyropdf.open(kept, password="secret")) == page_texts(doc)  # noqa: S106 - public test fixture.


@pytest.mark.parametrize("options", [{"decompress": True}, {"compress": True}, {"keep_id": True}, {"prune": True}])
def test_stream_and_identifier_options_combine_with_linearization(options: dict[str, bool]) -> None:
    doc = pyropdf.open(CORPUS / "bill-hr815.pdf")
    data = doc.to_bytes(linearize=True, **options)  # ty: ignore[invalid-argument-type]
    qpdf_accepts(data)
    assert page_texts(pyropdf.open(data)) == page_texts(doc)
    if options == {"keep_id": True}:
        with pikepdf.open(io.BytesIO(data)) as saved, pikepdf.open(CORPUS / "bill-hr815.pdf") as source:
            assert [bytes(item) for item in saved.trailer.ID] == [bytes(item) for item in source.trailer.ID]


def test_the_same_document_linearizes_to_the_same_bytes() -> None:
    doc = pyropdf.open(CORPUS / "patent-us223898.pdf")
    assert doc.to_bytes(linearize=True) == doc.to_bytes(linearize=True)


@pytest.mark.parametrize(
    "options",
    [{"object_streams": True}, {"cross_reference": "stream"}, {"dedupe": True, "prune": True}],
)
def test_options_that_a_linearized_file_cannot_hold_are_refused(options: dict[str, object]) -> None:
    doc = pyropdf.open(CORPUS / "patent-us223898.pdf")
    with pytest.raises(ValueError, match="linearize=True writes a cross-reference table"):
        doc.to_bytes(linearize=True, **options)  # ty: ignore[invalid-argument-type]


def test_an_incremental_save_refuses_linearization(tmp_path: Path) -> None:
    doc = pyropdf.open(CORPUS / "patent-us223898.pdf")
    with pytest.raises(ValueError, match="linearize requires a full save"):
        doc.to_bytes(incremental=True, linearize=True)
    with pytest.raises(ValueError, match="linearize requires a full save"):
        doc.save(tmp_path / "out.pdf", incremental=True, linearize=True)
    with pytest.raises(TypeError, match="linearize must be a bool"):
        doc.to_bytes(linearize=1)  # ty: ignore[invalid-argument-type]


def test_a_linearized_save_is_bounded_by_max_size() -> None:
    doc = pyropdf.open(CORPUS / "patent-us223898.pdf")
    size = len(doc.to_bytes(linearize=True))
    # The measuring pass does not count against the output.
    assert len(doc.to_bytes(linearize=True, max_size=size)) == size
    with pytest.raises(pyropdf.LimitError) as caught:
        doc.to_bytes(linearize=True, max_size=size - 1)
    assert caught.value.code == "pdf_output_size"


def test_a_document_without_pages_is_refused() -> None:
    doc = pyropdf.Document()
    with pytest.raises(pyropdf.PdfError, match="cannot linearize a document without pages"):
        doc.to_bytes(linearize=True)


def test_a_page_written_directly_in_kids_is_refused() -> None:
    data = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [<< /Type /Page /MediaBox [0 0 10 10] >>] /Count 1 >>",
        }
    )
    with pytest.raises(pyropdf.PdfError, match="cannot linearize a page or page-tree node written directly"):
        pyropdf.open(data).to_bytes(linearize=True)


def test_a_page_outside_the_page_tree_is_not_written() -> None:
    data = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /Extra 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 10 10] >>",
            4: "<< /Type /Page /MediaBox [0 0 20 20] >>",
        }
    )
    saved = pyropdf.open(data).to_bytes(linearize=True)
    qpdf_accepts(saved)
    reopened = pyropdf.open(saved)
    assert reopened.catalog["Extra"] is None
    assert len(reopened.pages) == 1


@pytest.mark.parametrize("page_mode", ["", " /PageMode /UseOutlines"], ids=["default-mode", "use-outlines"])
def test_direct_catalog_outlines_linearize_cleanly_in_pdf_2_0(page_mode: str) -> None:
    empty_direct = build_raw_pdf(
        {
            1: f"<< /Type /Catalog /Version /2.0 /Pages 2 0 R{page_mode} /Outlines << /Type /Outlines >> >>",
            2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << >> >>",
            4: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << >> >>",
        }
    ).replace(b"%PDF-1.7", b"%PDF-2.0", 1)
    saved_empty = pyropdf.open(empty_direct).to_bytes(linearize=True)
    qpdf_accepts(saved_empty)
    reopened_empty = pyropdf.open(saved_empty)
    assert reopened_empty.is_linearized
    assert reopened_empty.check().defects == ()

    with_items = build_raw_pdf(
        {
            1: f"<< /Type /Catalog /Version /2.0 /Pages 2 0 R{page_mode} "
            "/Outlines << /Type /Outlines /First 4 0 R /Last 4 0 R /Count 1 >> >>",
            2: "<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << >> >>",
            4: "<< /Title (Chapter 1) /Dest [3 0 R /Fit] >>",
            5: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << >> >>",
        }
    ).replace(b"%PDF-1.7", b"%PDF-2.0", 1)
    original = pyropdf.open(with_items)
    saved = original.to_bytes(linearize=True)
    qpdf_accepts(saved)
    reopened = pyropdf.open(saved)
    assert reopened.is_linearized
    assert reopened.outline.entries() == original.outline.entries()
    assert all(defect.path != "Outlines" for defect in reopened.check().defects)


def assert_hint_offsets(data: bytes, *, page_count: int) -> None:
    """Check the Annex F byte ranges as well as qpdf's independent computed hints."""
    with pikepdf.open(io.BytesIO(data), attempt_recovery=False) as pdf:
        assert pdf.check_linearization()
        params = next(o for o in pdf.objects if isinstance(o, pikepdf.Dictionary) and "/Linearized" in o)
        assert len(data) == params.L
        assert page_count == params.N
        assert pdf.pages[0].objgen[0] == params.O
        hint, size = map(int, params.H)
        assert 0 < hint < hint + size < params.E < params.T < params.L
        hint_number = int(data[hint:].split(maxsplit=1)[0])
        assert data[hint : hint + size].rstrip().endswith(b"endobj")
        entry = pdf.get_xref_table()[hint_number, 0]
        assert entry.offset == hint
        assert isinstance(pdf.get_object(hint_number, 0), pikepdf.Stream)
        assert re.match(rb"\s*\d+ \d+ obj\b", data[params.E :])
        assert re.match(rb"\s*\d{10} \d{5} f", data[params.T :])


@pytest.mark.spec("linearization", "preserve")
def test_edits_rebuild_fast_web_view_offsets_or_preserve_the_original_revision() -> None:
    out = io.BytesIO()
    with pikepdf.open(CORPUS / "patent-us223898.pdf") as independent:
        independent.save(out, linearize=True)
    original = out.getvalue()
    with pyropdf.open(original) as doc:
        assert doc.to_bytes(incremental=True) == original
        texts = page_texts(doc)
        count = len(doc.pages)
        doc.pages[-1].rotation = 90
        doc.metadata["subject"] = "A later revision"
        expected_boxes = [p.rect for p in doc.pages]
        for mode, data in (
            ("updated", doc.to_bytes(incremental=True)),
            ("ordinary", doc.to_bytes()),
            ("linearized", doc.to_bytes(linearize=True)),
        ):
            with pikepdf.open(io.BytesIO(data), attempt_recovery=False) as pdf:
                assert pdf.is_linearized == (mode == "linearized")
                assert pdf.check_pdf_syntax() == []
                assert str(pdf.docinfo.Subject) == "A later revision"
                assert pdf.pages[-1].Rotate == 90
            with pyropdf.open(data) as reopened:
                assert page_texts(reopened) == texts
                assert [page.rect for page in reopened.pages] == expected_boxes
            if mode == "linearized":
                assert_hint_offsets(data, page_count=count)
            elif mode == "updated":
                assert data.startswith(original)
                with pikepdf.open(io.BytesIO(data[: len(original)])) as base:
                    assert base.check_linearization()
        # Incremental output cannot replace the existing hint tables.
        with pytest.raises(ValueError, match="linear"):
            doc.to_bytes(incremental=True, linearize=True)


@pytest.mark.spec("xref-streams", "create", "edit")
@pytest.mark.parametrize("packed", [False, True], ids=["plain", "packed"])
def test_hybrid_revisions_resolve_the_same_page_for_legacy_and_modern_readers(tmp_path: Path, *, packed: bool) -> None:
    qpdf = shutil.which("qpdf")
    if qpdf is None:
        pytest.skip("qpdf is not on PATH")
    with pyropdf.open(CORPUS / "patent-us223898.pdf") as doc:
        original = doc.to_bytes(cross_reference="hybrid", object_streams=packed)
    with pyropdf.open(original) as doc:
        texts = page_texts(doc)
        doc.pages[1].rotation = 90
        updated = doc.to_bytes(incremental=True)
        rebuilt = doc.to_bytes(cross_reference="hybrid", object_streams=packed)
    assert updated.startswith(original)
    for mode, data in (("original", original), ("incremental", updated), ("full", rebuilt)):
        latest = int(data.rsplit(b"startxref", 1)[1].split()[0])
        assert data[latest:].startswith(b"xref\n")
        with pikepdf.open(io.BytesIO(data), attempt_recovery=False) as pdf:
            assert pdf.check_pdf_syntax() == []
            position = int(pdf.trailer.XRefStm)
            assert 0 < position < latest
            number = int(data[position:].split(maxsplit=1)[0])
            assert pdf.get_object(number, 0).Type == pikepdf.Name.XRef
            ref = pdf.pages[1].objgen
            rotate = int(pdf.pages[1].get("/Rotate", 0))
            if mode == "incremental":
                assert pdf.trailer.Prev == int(original.rsplit(b"startxref", 1)[1].split()[0])
                assert position >= len(original)
            if mode != "original":
                assert rotate == 90
        file = tmp_path / f"{mode}.pdf"
        file.write_bytes(data)
        args = [qpdf, "--ignore-xref-streams", f"--show-object={ref[0]},{ref[1]}", str(file)]
        legacy = subprocess.run(args, capture_output=True, check=True).stdout  # noqa: S603 - fixed local reader
        assert b"/Type /Page " in legacy
        if rotate:
            assert f"/Rotate {rotate}".encode() in legacy
        with pyropdf.open(data) as reopened:
            assert page_texts(reopened) == texts
