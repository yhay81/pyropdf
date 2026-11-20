"""Ordered /RF relationships and shared embedded streams (ISO 32000-2, 7.11.4.2)."""

from __future__ import annotations

import io

import pikepdf
import pytest
from test_attachments import build_raw_pdf

import pyropdf


def _document() -> pyropdf.Document:
    data = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 120 100] /Resources <<>> >>",
        }
    )
    return pyropdf.open(b"%PDF-1.7" + data[8:])


def _stream(doc: pyropdf.Document, data: bytes) -> pyropdf.Stream:
    return doc.objects[doc.objects.add_stream(data, {"Type": pyropdf.Name("EmbeddedFile")})]


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("related-files", "read", "interpret", "create", "edit", "preserve")
def test_related_streams_keep_name_bytes_order_and_object_identity(*, incremental: bool) -> None:
    with _document() as doc:
        a = doc.attachments.add("main.dat", b"primary")
        other = doc.attachments.add("other.dat", b"other")
        one = pyropdf.RelatedFile(filename=b"\xff.raw", stream=_stream(doc, b"one"))
        two = pyropdf.RelatedFile(filename="図面", stream=_stream(doc, b"two"))
        a.related_files = {"F": [two, one, two], "UF": [one]}
        other.related_files = {"F": [two]}
        assert a.related_files["F"] == (two, one, two)
        assert a.related_files["F"][0].filename.text == "図面"
        # Removing only one relationship keeps the stream and the second name key.
        a.related_files = {"F": a.related_files["F"][1:], "UF": a.related_files["UF"]}
        saved = doc.to_bytes(incremental=incremental)
    with pyropdf.open(saved) as reopened:
        assert not reopened.check()
        assert reopened.attachments["main.dat"].read() == b"primary"
        files = reopened.attachments["main.dat"].related_files
        assert [item.filename for item in files["F"]] == [b"\xff.raw", two.filename]
        assert files["UF"][0].stream.read() == b"one"
    with pikepdf.open(io.BytesIO(saved)) as independent:
        names = independent.Root.Names.EmbeddedFiles.Names
        first, second = names[1].RF, names[3].RF
        assert first.F.is_indirect
        assert first.UF.is_indirect
        assert [bytes(first.F[i]) for i in (0, 2)] == [b"\xff.raw", two.filename]
        assert first.F[1].objgen == first.UF[1].objgen
        assert first.F[3].objgen == second.F[1].objgen
        assert [first.F[i].read_bytes() for i in (1, 3)] == [b"one", b"two"]


def test_editing_shared_rf_retains_unknown_entries_and_live_referrers() -> None:
    with _document() as doc:
        a = doc.attachments.add("a", b"a")
        b = doc.attachments.add("b", b"b")
        shared = pyropdf.RelatedFile(filename="common", stream=_stream(doc, b"shared"))
        a.related_files = {"F": [shared]}
        names = doc.catalog["Names"]["EmbeddedFiles"]["Names"]
        first, second = names[1], names[3]
        first["RF"]["Private"] = {"Label": b"untouched"}
        second["RF"] = first["RF"]
        old_rf = second["RF"].xref
        del a.related_files
        assert not a.related_files
        assert first["RF"]["Private"]["Label"] == b"untouched"
        assert first["RF"].xref != old_rf
        assert second["RF"].xref == old_rf
        assert b.related_files["F"][0].stream.read() == b"shared"
        del doc.attachments["a"]
        assert b.related_files["F"] == (shared,)
        del b.related_files
        assert shared.stream.xref not in doc.objects


def test_invalid_related_collection_refuses_atomically() -> None:
    with _document() as doc, _document() as foreign:
        a = doc.attachments.add("a", b"a")
        item = pyropdf.RelatedFile(filename=b"ok", stream=_stream(doc, b"here"))
        elsewhere = pyropdf.RelatedFile(filename=b"foreign", stream=_stream(foreign, b"there"))
        a.related_files = {"F": [item]}
        before = doc.to_bytes(incremental=True)
        history = doc.history.steps
        with pytest.raises(ValueError, match="another document"):
            a.related_files = {"F": [item], "UF": [elsewhere]}
        with pytest.raises(pyropdf.LimitError) as refusal:
            a.related_files = {"F": [item] * 4097}
        assert refusal.value.code == "embedded_file_input_size"
        with pytest.raises(ValueError, match="only F and UF"):
            a.related_files = {"Mac": [item]}
        assert a.related_files == {"F": (item,)}
        assert doc.history.steps == history
        assert doc.to_bytes(incremental=True) == before


def test_annotated_files_share_the_same_related_file_contract() -> None:
    with _document() as doc:
        annot = doc.pages[0].annotations.add(pyropdf.FileAttachment(at=(20, 30), filename="drawing", data=b"main"))
        file = annot.file
        assert file is not None
        item = pyropdf.RelatedFile(filename="legend", stream=_stream(doc, b"legend"))
        file.related_files = {"F": [item]}
        with pyropdf.open(doc.to_bytes()) as reopened:
            attached = reopened.pages[0].annotations[0].file
            assert attached is not None
            assert attached.related_files["F"][0].stream.read() == b"legend"
        del file.related_files
        assert file.related_files == {}


def test_keys_must_match_the_embedded_file_and_stream_kind() -> None:
    with _document() as doc:
        a = doc.attachments.add("owner", b"data")
        fs = doc.catalog["Names"]["EmbeddedFiles"]["Names"][1]
        del fs["EF"]["UF"]
        stream = _stream(doc, b"raw")
        item = pyropdf.RelatedFile(filename="a", stream=stream)
        with pytest.raises(pyropdf.PdfError, match="/EF/UF"):
            a.related_files = {"UF": [item]}
        stream["Type"] = pyropdf.Name("XObject")
        with pytest.raises(ValueError, match="not an embedded file"):
            a.related_files = {"F": [item]}
        assert "RF" not in fs
        fs["RF"] = {"F": doc.objects[doc.objects.add([b"dangling name"])]}
        with pytest.raises(pyropdf.PdfError, match="filename/stream pairs"):
            _ = a.related_files
        del doc.attachments["owner"]
        assert "owner" not in doc.attachments
