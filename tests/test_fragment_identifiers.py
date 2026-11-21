"""PDF resource selection and viewer parameters (ISO 32000-2 Annex O)."""

from __future__ import annotations

import io

import pikepdf
import pytest
from test_attachments import build_raw_pdf

import pyropdf
from pyropdf._fragments import PdfFragment


def _source() -> bytes:
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /Names << /Dests 8 0 R >> /StructTreeRoot 9 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
            3: "<< /Type /Page /Parent 2 0 R /Resources << >> /MediaBox [0 0 20 20] /Annots [5 0 R] >>",
            4: "<< /Type /Page /Parent 2 0 R /Resources << >> /MediaBox [0 0 20 20] /Annots [6 0 R] >>",
            5: "<< /Type /Annot /Subtype /Text /Rect [0 0 10 10] /NM (same) >>",
            6: "<< /Type /Annot /Subtype /Text /Rect [0 0 10 10] /NM (same) >>",
            8: "<< /Names [(Second) [4 0 R /Fit]] >>",
            9: "<< /Type /StructTreeRoot /K [10 0 R] >>",
            10: "<< /Type /StructElem /P 9 0 R /S /Document /K [10 0 R 11 0 R] >>",
            11: "<< /Type /StructElem /P 10 0 R /S /P /ID <00ff2b> /Pg 4 0 R >>",
        }
    )


@pytest.mark.spec("fragment-identifiers", "read", "interpret")
def test_annex_o_resolves_only_the_selected_pages_objects_and_raw_structure_id() -> None:
    with pyropdf.open(_source()) as doc:
        fields = PdfFragment.parse("#page=1&comment=same#nameddest=Second&comment=same&structelem=%00%ff+").resolve(doc)
        first, first_note, dest, second_note, elem = [part.target for part in fields]
        assert isinstance(first, pyropdf.Page)
        assert first.index == 0
        assert isinstance(first_note, pyropdf.Annotation)
        assert first_note.obj.xref == 5
        assert isinstance(dest, pyropdf.Destination)
        assert dest.page == 1
        assert isinstance(second_note, pyropdf.Annotation)
        assert second_note.obj.xref == 6
        assert isinstance(elem, pyropdf.Dictionary)
        assert elem.xref == 11
        assert fields[-1].value == b"\x00\xff+"
        missing = PdfFragment.parse("comment=same&page=33&comment=same&nameddest=Missing&structelem=%ff").resolve(doc)
        assert [part.outcome for part in missing] == ["unresolved"] * 5


@pytest.mark.parametrize("indexed", [True, False])
def test_structure_ids_use_exact_index_keys_and_allow_wide_shallow_trees(*, indexed: bool) -> None:
    with pyropdf.open(_source()) as doc:
        root = doc.catalog["StructTreeRoot"]
        element = doc.objects[11]
        if indexed:
            root["IDTree"] = {"Kids": [{"Names": [b"\x00\xff+", element], "Limits": [b"\x00\xff+", b"\x00\xff+"]}]}
        else:
            # A width of 300 is not a depth of 300. The preceding integers are MCIDs.
            root["K"] = [*range(300), element]
        selected = PdfFragment.parse("structelem=%00%FF%2B").resolve(doc)[0].target
        assert isinstance(selected, pyropdf.Dictionary)
        assert selected.xref == 11


@pytest.mark.spec("fragment-identifiers", "read", "interpret")
def test_annex_o_open_parameters_remain_ordered_data() -> None:
    text = "zoom=125,0,-4&view=FitH#viewrect=1,2,30,40&highlight=0,20,3,4&search=%22A%20B+C%22&fdf=form%3Fx%3D1%26y%3D2"
    with pyropdf.open(_source()) as doc:
        params = PdfFragment.parse(text).resolve(doc)
        assert all(p.outcome == "option" for p in params)
        assert [p.value for p in params] == [
            (125, 0, -4),
            ("FitH",),
            (1, 2, 30, 40),
            (0, 20, 3, 4),
            ("A", "B+C"),
            "form?x=1&y=2",
        ]
        assert PdfFragment.parse("view=FitR,0,0,10,10").resolve(doc)[0].value == ("FitR", 0, 0, 10, 10)


@pytest.mark.spec("fragment-identifiers", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_a_fragment_edit_preserves_other_uri_bytes_and_the_embedded_remainder(*, incremental: bool) -> None:
    initial = "unKnOwn=%fF%2b#page=1&ef=file%2b.bin#chapter-1&time=0%2C5"
    with pyropdf.open(_source()) as doc:
        doc.attachments.add("file+.bin", b"embedded bytes")
        fragment = PdfFragment.parse(initial)
        updated = fragment.with_parameter("page", "2").with_parameter("zoom", "150,0,7")
        assert updated.text == "unKnOwn=%fF%2b#page=2&zoom=150,0,7&ef=file%2b.bin#chapter-1&time=0%2C5"
        link = "https://example.invalid/file.pdf#" + updated.text
        doc.pages[0].links.add(pyropdf.Rect(0, 0, 10, 10), link)
        data = doc.to_bytes(incremental=incremental)
    with pikepdf.open(io.BytesIO(data), attempt_recovery=False) as independent:
        assert str(independent.pages[0].Annots[-1].A.URI) == link
    with pyropdf.open(data) as reopened:
        uri = reopened.pages[0].links[-1].target
        assert isinstance(uri, str)
        params = PdfFragment.parse(uri.partition("#")[2]).resolve(reopened)
        assert (params[0].name, params[0].outcome, params[0].value) == ("unKnOwn", "unknown", b"\xff+")
        attachment = params[-1].target
        assert isinstance(attachment, pyropdf.Attachment)
        assert attachment.read() == b"embedded bytes"
        assert params[-1].remainder == "chapter-1&time=0%2C5"
        absent = PdfFragment.parse("ef=missing#page=1").resolve(reopened)
        assert absent[0].outcome == "unresolved"
        assert absent[0].remainder == "page=1"


@pytest.mark.parametrize(
    "text",
    [
        "page=0",
        "page=1.2",
        "page=999999999999",
        "page",
        "page=2&",
        "zoom=10,0",
        "zoom=0",
        "view=FitH,NaN",
        "view=Fit,1",
        "view=Wrong",
        "viewrect=1,2,-1,4",
        "highlight=1,2",
        "search=unquoted",
        "search=%2",
        "structelem=%XY",
        "x=" + "a" * 65536,
        "&".join(["x=1"] * 1025),
    ],
    ids=lambda text: text[:30],
)
def test_malformed_pdf_fragments_and_excessive_input_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match=r"(?i)PDF (URI )?fragment|empty.*parameter"):
        PdfFragment.parse(text)
