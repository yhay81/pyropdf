# ruff: noqa: S106 - the fixtures' passwords are test data.
"""Save options beyond full and incremental saves (DESIGN.md section 4.4)."""

from __future__ import annotations

import io
import os
import re
import subprocess
from pathlib import Path
from typing import Literal

import pikepdf
import pytest
from corpus import ASSETS, NAMES
from PIL import Image
from test_attachments import build_pdf, build_raw_pdf
from test_consistency import QPDF, qpdf_findings

import pyropdf
from pyropdf import Encryption, LimitError, Name, PdfError, Stream

REFERENCE = re.compile(rb"(\d+) \d+ R")
#: The filters that hayro-syntax decodes, which a decompressed save removes.
DECODED = {"ASCIIHexDecode", "ASCII85Decode", "LZWDecode", "FlateDecode", "RunLengthDecode"}
ENCRYPTED = Path(__file__).resolve().parent / "assets" / "encrypted"


def _numbers(data: bytes) -> set[int]:
    return set(pyropdf.open(data).objects)


@pytest.mark.spec("xref-table", "edit")
def test_a_pruned_save_leaves_out_what_an_edit_unlinked() -> None:
    doc = pyropdf.open(build_pdf(["Hello"]))
    data = doc.objects.add_stream(b"data")
    doc.catalog["PieceInfo"] = {"Example": doc.objects[doc.objects.add({"Data": doc.objects[data]})]}
    doc = pyropdf.open(doc.to_bytes())
    holder = doc.catalog["PieceInfo"]["Example"]
    unlinked = {holder.xref, holder["Data"].xref}
    # Deleting an entry unlinks its objects without freeing them.
    del doc.catalog["PieceInfo"]

    pruned = pyropdf.open(doc.to_bytes(prune=True))
    assert not unlinked & set(pruned.objects)
    assert unlinked <= _numbers(doc.to_bytes())
    assert unlinked <= set(doc.objects)
    assert len(pruned.pages) == 1
    assert pruned.pages[0].text().strip() == "Hello"


def test_a_pruned_save_keeps_what_the_trailer_reaches_through_any_object() -> None:
    doc = pyropdf.open(build_pdf(["Hello"]))
    leaf = doc.objects.add({"Leaf": 1})
    orphan = doc.objects.add({"Child": doc.objects[leaf]})
    kept = doc.objects.add({"Child": doc.objects[leaf]})
    doc.catalog["Extra"] = doc.objects[kept]

    numbers = _numbers(doc.to_bytes(prune=True))
    assert {leaf, kept} <= numbers
    assert orphan not in numbers


@pytest.mark.spec("crypt-filters", "edit")
@pytest.mark.spec("encryption-dictionary", "edit")
def test_a_pruned_save_writes_a_new_encryption_without_the_old_dictionary(tmp_path: Path) -> None:
    doc = pyropdf.open(ENCRYPTED / "user-aes-256.pdf", password="userpw")
    old = doc.trailer["Encrypt"].xref
    path = tmp_path / "pruned.pdf"
    doc.save(path, encryption=Encryption(user_password="new"), prune=True)
    locked = pyropdf.open(path)
    assert locked.needs_password
    locked.unlock("new")
    assert len(locked.pages) == len(doc.pages)
    assert locked.trailer["Encrypt"].xref != old
    assert old not in set(locked.objects)


@pytest.mark.parametrize("name", NAMES)
def test_a_pruned_corpus_save_keeps_every_reachable_object(name: str) -> None:
    source = pyropdf.open(ASSETS / name)
    values = {number: body for number, _, body in source._core()._objects()}  # noqa: SLF001
    pruned = pyropdf.open(source.to_bytes(prune=True))
    kept = {number: body for number, _, body in pruned._core()._objects()}  # noqa: SLF001

    assert source.catalog.xref in kept
    assert len(pruned.pages) == len(source.pages)
    # A file read with a cross-reference stream is saved with a new one.
    added = kept.keys() - values.keys()
    assert all(kept[number].startswith(b"<< /Type /XRef") for number in added)
    for number, body in kept.items():
        if number in added:
            continue
        assert body == values[number], number
        # What a kept object refers to is kept, unless the source lacks it too.
        head = body.split(b"stream\n", 1)[0]
        for match in REFERENCE.finditer(head):
            target = int(match[1])
            assert (target in kept) == (target in values), (number, target)


def test_prune_requires_a_full_save_and_a_bool() -> None:
    doc = pyropdf.open(build_pdf(["Hello"]))
    with pytest.raises(ValueError, match="full save"):
        doc.to_bytes(incremental=True, prune=True)
    with pytest.raises(TypeError, match="prune must be a bool"):
        doc.to_bytes(prune=1)  # ty: ignore[invalid-argument-type]


def _streams(doc: pyropdf.Document) -> dict[int, Stream]:
    streams = {}
    for number in doc.objects:
        value = doc.objects[number]
        if isinstance(value, Stream):
            streams[number] = value
    return streams


def _decoded(stream: Stream) -> bytes | None:
    """Return the stream's decoded data, or ``None`` when hayro-syntax does not decode it."""
    try:
        return stream.read(max_size=None)
    except PdfError:
        return None


def _filters(stream: Stream) -> list[str]:
    if "Filter" not in stream:
        return []
    value = stream["Filter"]
    return [str(value)] if isinstance(value, Name) else [str(name) for name in value]


@pytest.mark.spec("xmp-metadata", "preserve")
def test_a_compressed_save_flates_unfiltered_streams_where_that_shrinks_them() -> None:
    content = b"BT /F1 12 Tf 72 720 Td (Hello) Tj ET\n" * 40
    doc = pyropdf.open(build_pdf(["Hello"]))
    stream = doc.objects.add_stream(content, filter=None)
    metadata = doc.objects.add_stream(b"<x:xmpmeta>" * 40, {"Type": Name("Metadata")}, filter=None)
    doc.catalog["Extra"] = [doc.objects[stream], doc.objects[metadata]]

    compressed = pyropdf.open(doc.to_bytes(compress=True, compression_level=9))
    assert _filters(compressed.objects[stream]) == ["FlateDecode"]
    assert compressed.objects[stream].read() == content
    assert _filters(compressed.objects[metadata]) == []
    assert compressed.pages[0].text().strip() == "Hello"
    # Stored blocks at level 0 are longer than the data, which stays.
    stored = pyropdf.open(doc.to_bytes(compress=True, compression_level=0))
    assert _filters(stored.objects[stream]) == []
    # The document in memory keeps its streams.
    assert _filters(doc.objects[stream]) == []


@pytest.mark.parametrize("name", NAMES)
def test_a_compressed_corpus_save_keeps_every_stream_and_is_not_larger(name: str) -> None:
    source = pyropdf.open(ASSETS / name)
    data = source.to_bytes(compress=True)
    assert len(data) <= len(source.to_bytes())
    compressed = pyropdf.open(data)
    assert len(compressed.pages) == len(source.pages)
    for number, stream in _streams(source).items():
        expected = _decoded(stream)
        if expected is None:
            assert compressed.objects[number].read(decode=False) == stream.read(decode=False), number
        else:
            assert compressed.objects[number].read(max_size=None) == expected, number


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.spec("asciihex-filter", "read", "edit")
@pytest.mark.spec("ccitt-fax-filter", "preserve")
@pytest.mark.spec("dct-filter", "preserve")
@pytest.mark.spec("jbig2-filter", "preserve")
@pytest.mark.spec("jpx-filter", "preserve")
def test_a_decompressed_corpus_save_keeps_only_image_filters(name: str) -> None:
    source = pyropdf.open(ASSETS / name)
    decompressed = pyropdf.open(source.to_bytes(decompress=True, max_size=None))
    assert len(decompressed.pages) == len(source.pages)
    for number, stream in _streams(source).items():
        written = decompressed.objects[number]
        filters, before = _filters(written), _filters(stream)
        if filters and filters[0] in DECODED:
            # Only data that does not decode keeps a decoded filter.
            assert written.read(decode=False) == stream.read(decode=False), number
        elif filters:
            # An image filter and the filters after it stay.
            assert filters == before[len(before) - len(filters) :], number
            if filters == before:
                assert written.read(decode=False) == stream.read(decode=False), number
        else:
            assert written.read(decode=False) == stream.read(max_size=None), number


def test_an_incremental_save_compresses_only_what_it_writes() -> None:
    doc = pyropdf.open(build_pdf(["Hello"]))
    original = doc.to_bytes()
    doc = pyropdf.open(original)
    stream = doc.objects.add_stream(b"0 0 m 10 10 l S\n" * 50, filter=None)
    doc.catalog["Extra"] = doc.objects[stream]

    data = doc.to_bytes(incremental=True, compress=True)
    assert data.startswith(original)
    updated = pyropdf.open(data)
    assert _filters(updated.objects[stream]) == ["FlateDecode"]
    # The content stream of the opened file stays unfiltered.
    content = updated.pages[0].obj["Contents"]
    assert _filters(content) == []


def test_an_encrypted_save_compresses_before_it_encrypts(tmp_path: Path) -> None:
    content = b"0 0 m 10 10 l S\n" * 50
    doc = pyropdf.open(build_pdf(["Hello"]))
    stream = doc.objects.add_stream(content, filter=None)
    doc.catalog["Extra"] = doc.objects[stream]
    path = tmp_path / "compressed.pdf"
    doc.save(path, encryption=Encryption(user_password="pw"), compress=True)
    locked = pyropdf.open(path, password="pw")
    assert _filters(locked.objects[stream]) == ["FlateDecode"]
    assert locked.objects[stream].read() == content


def test_compression_options_are_checked() -> None:
    doc = pyropdf.open(build_pdf(["Hello"]))
    for level in (-1, 10):
        with pytest.raises(ValueError, match="from 0 through 9"):
            doc.to_bytes(compress=True, compression_level=level)
    with pytest.raises(TypeError, match="compression_level must be an integer"):
        doc.to_bytes(compression_level=True)
    with pytest.raises(TypeError, match="compress must be a bool"):
        doc.to_bytes(compress=1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="needs compress=False"):
        doc.to_bytes(compress=True, decompress=True)
    with pytest.raises(ValueError, match="decompress requires a full save"):
        doc.to_bytes(incremental=True, decompress=True)
    with pytest.raises(LimitError) as caught:
        doc.to_bytes(decompress=True, max_size=100)
    assert caught.value.code == "pdf_output_size"


def _id(data: bytes) -> list[bytes]:
    return [bytes(element) for element in pyropdf.open(data).trailer["ID"]]


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("file-identifiers", "edit", "preserve")
def test_a_save_of_edits_renews_the_second_identifier(incremental: bool) -> None:  # noqa: FBT001
    read = [b"\xaa" * 16, b"\xbb" * 16]
    trailer = f"/Root 1 0 R /ID [<{read[0].hex()}><{read[1].hex()}>] >>".encode()
    doc = pyropdf.open(build_pdf(["Hello"]).replace(b"/Root 1 0 R >>", trailer))
    assert _id(doc.to_bytes(incremental=incremental)) == read
    doc.catalog["Edited"] = True

    data = doc.to_bytes(incremental=incremental)
    renewed = _id(data)
    assert renewed[0] == read[0]
    assert len(renewed[1]) == 16
    assert renewed[1] != read[1]
    # The new element is a digest, so the same edits save to the same bytes.
    assert doc.to_bytes(incremental=incremental) == data
    assert _id(doc.to_bytes(incremental=incremental, keep_id=True)) == read


@pytest.mark.spec("file-identifiers", "create")
def test_a_document_without_an_identifier_gets_one() -> None:
    data = build_pdf(["Hello"])
    doc = pyropdf.open(data)
    assert "ID" not in doc.trailer
    first, second = _id(doc.to_bytes())
    assert len(first) == 16
    assert first == second
    assert "ID" not in pyropdf.open(doc.to_bytes(keep_id=True)).trailer
    # An incremental save without edits reproduces the file.
    assert doc.to_bytes(incremental=True) == data
    doc.catalog["Edited"] = True
    first, second = _id(doc.to_bytes(incremental=True))
    assert len(first) == 16
    assert first == second


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.spec("file-identifiers", "read", "preserve")
def test_a_save_without_edits_writes_the_identifier_as_read(name: str) -> None:
    data = (ASSETS / name).read_bytes()
    doc = pyropdf.open(data)
    assert doc.to_bytes(incremental=True) == data
    if "ID" in doc.trailer:
        assert _id(doc.to_bytes()) == [bytes(element) for element in doc.trailer["ID"]]


@pytest.mark.spec("file-identifiers", "interpret")
def test_an_encrypted_save_keeps_the_identifier_that_its_keys_derive_from() -> None:
    doc = pyropdf.open(ENCRYPTED / "user-r4-rc4-128.pdf", password="userpw")
    first = bytes(doc.trailer["ID"][0])
    doc.catalog["Edited"] = True
    for data in (doc.to_bytes(incremental=True), doc.to_bytes(encryption=Encryption.KEEP)):
        reopened = pyropdf.open(data, password="userpw")
        assert bytes(reopened.trailer["ID"][0]) == first
        assert bytes(reopened.trailer["ID"][1]) != first
        assert reopened.catalog["Edited"] is True
        assert len(reopened.pages) == len(doc.pages)


def test_keep_id_must_be_a_bool() -> None:
    doc = pyropdf.open(build_pdf(["Hello"]))
    with pytest.raises(TypeError, match="keep_id must be a bool"):
        doc.to_bytes(keep_id=1)  # ty: ignore[invalid-argument-type]


STARTXREF = re.compile(rb"startxref\s+(\d+)\s+%%EOF\s*$")


def _form(data: bytes) -> str:
    """Return the form of the cross-reference section that ``data``'s ``startxref`` names."""
    match = STARTXREF.search(data)
    assert match is not None
    return "table" if data[int(match[1]) :].startswith(b"xref") else "stream"


def _require_qpdf() -> str:
    if QPDF is None:
        if os.environ.get("PYROPDF_REQUIRE_QPDF") == "1":
            pytest.fail("PYROPDF_REQUIRE_QPDF=1, but qpdf is not on PATH")
        pytest.skip("qpdf is not on PATH")
    return QPDF


def _check_hybrid_sections(data: bytes, *, packed: bool) -> int:
    """Check both visible views of the file and return the latest table offset."""
    assert data.startswith(b"%PDF-1.5")
    latest = STARTXREF.search(data)
    assert latest
    table = int(latest[1])
    with pikepdf.open(io.BytesIO(data), attempt_recovery=False) as pdf:
        assert pdf.check_pdf_syntax() == []
        previous = int(pdf.trailer.Prev)
        supplement = int(pdf.trailer.XRefStm)
        assert previous < supplement < table
        assert data[previous:].startswith(b"xref\n")
        assert data[table:].startswith(b"xref\n")
        stream = pdf.get_object(int(data[supplement:].split(maxsplit=1)[0]), 0)
        assert str(stream.Type) == "/XRef"
        assert stream.Size == pdf.trailer.Size
        assert "/Prev" not in stream
        primary = data[: data.index(b"%%EOF", previous) + 5]
        with pikepdf.open(io.BytesIO(primary), attempt_recovery=False) as classic:
            assert "/XRefStm" not in classic.trailer
            assert list(classic.trailer.ID) == list(pdf.trailer.ID)
            assert len(classic.pages) == 1
            assert str(classic.Root.XXVisible.Text) == "legacy-visible"
            if packed:
                assert classic.get_object(7, 0) is None
            else:
                assert str(classic.get_object(7, 0).Text) == "hidden candidate"
        entries = pdf.get_xref_table()
        assert entries[(7, 0)].type == (2 if packed else 1)
        assert (5, 0) not in entries
        assert str(pdf.get_object(7, 0).Text) == "hidden candidate"
        assert str(pdf.Root.XXVisible.Text) == "legacy-visible"
    return table


@pytest.mark.parametrize("packed", [False, True])
def test_hybrid_sections_keep_the_document_visible_to_a_table_only_reader(tmp_path: Path, *, packed: bool) -> None:
    source = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /XXVisible 3 0 R >>",
            2: "<< /Type /Pages /Count 1 /Kids [4 0 R] >>",
            3: "<< /Text (legacy-visible) >>",
            4: "<< /Type /Page /Parent 2 0 R /Resources << >> /MediaBox [0 0 20 20] >>",
            7: "<< /Text (hidden candidate) >>",
        },
    ).replace(b"%PDF-1.7", b"%PDF-1.4", 1)
    with pyropdf.open(source) as doc:
        data = doc.to_bytes(cross_reference="hybrid", object_streams=packed, keep_id=False)
        out = tmp_path / "hybrid.pdf"
        doc.save(out, cross_reference="hybrid", object_streams=packed, keep_id=False)
        assert out.read_bytes() == data
        with pytest.raises(LimitError, match="PDF output"):
            doc.to_bytes(cross_reference="hybrid", object_streams=packed, keep_id=False, max_size=len(data) - 1)
    table = _check_hybrid_sections(data, packed=packed)
    qpdf = _require_qpdf()
    command = [qpdf, "--ignore-xref-streams", "--show-npages", str(out)]
    legacy = subprocess.run(command, capture_output=True, check=True)  # noqa: S603 - local qpdf on generated data.
    assert legacy.stdout.strip() == b"1"
    with pyropdf.open(data) as reopened:
        assert reopened.to_bytes(incremental=True) == data
        reopened.pages[0].rotation = 90
        updated = reopened.to_bytes(incremental=True)
    assert updated.startswith(data)
    with pikepdf.open(io.BytesIO(updated), attempt_recovery=False) as edited:
        assert edited.check_pdf_syntax() == []
        assert int(edited.trailer.Prev) == table
        assert int(edited.trailer.XRefStm) >= len(data)
        assert edited.pages[0].Rotate == 90
        assert str(edited.get_object(7, 0).Text) == "hidden candidate"


@pytest.mark.parametrize("renumber", [False, True])
def test_an_encrypted_hybrid_keeps_its_document_id_and_visible_page(*, renumber: bool) -> None:
    with pyropdf.Document() as doc:
        page = doc.pages.new()
        page.obj["Resources"] = {}
        doc.objects.add({"XXSecret": b"an unreferenced encrypted value"})
        data = doc.to_bytes(
            cross_reference="hybrid",
            object_streams=True,
            renumber=renumber,
            keep_id=False,
            encryption=Encryption(user_password="reader", owner_password="owner"),
        )
    assert b"an unreferenced encrypted value" not in data
    with pikepdf.open(io.BytesIO(data), password="reader", attempt_recovery=False) as pdf:
        assert pdf.check_pdf_syntax() == []
        previous = int(pdf.trailer.Prev)
        main = data[: data.index(b"%%EOF", previous) + 5]
        with pikepdf.open(io.BytesIO(main), password="reader", attempt_recovery=False) as classic:
            assert len(classic.pages) == len(pdf.pages) == 1
            assert list(classic.trailer.ID) == list(pdf.trailer.ID)
        hidden = [key for key, entry in pdf.get_xref_table().items() if entry.type == 2]
        assert any(bytes(pdf.get_object(*key).XXSecret) == b"an unreferenced encrypted value" for key in hidden)
    with pyropdf.open(data, password="owner") as opened:
        opened.pages[0].rotation = 90
        changed = opened.to_bytes(incremental=True, keep_id=False)
    with pikepdf.open(io.BytesIO(changed), password="reader", attempt_recovery=False) as updated:
        assert updated.pages[0].Rotate == 90
        assert updated.check_pdf_syntax() == []


def test_a_sparse_hybrid_main_table_respects_the_object_budget(tmp_path: Path) -> None:
    source = build_pdf(["Visible"])
    sparse = re.sub(rb"/Size \d+", b"/Size 70000", source)
    target = tmp_path / "existing.pdf"
    target.write_bytes(b"existing destination")
    with pyropdf.open(sparse, limits=pyropdf.DocumentLimits(max_objects=50)) as doc:
        with pytest.raises(LimitError) as caught:
            doc.save(target, cross_reference="hybrid")
        assert caught.value.code == "object_count"
    assert target.read_bytes() == b"existing destination"


def test_hybrid_creation_refuses_incompatible_layouts_before_replacing_a_file(tmp_path: Path) -> None:
    with pyropdf.open(build_pdf(["Visible"])) as doc:
        target = tmp_path / "existing.pdf"
        target.write_bytes(b"existing destination")
        for incremental in (True, False):
            with pytest.raises(ValueError, match="cross_reference"):
                doc.save(target, cross_reference="hybrid", incremental=incremental, linearize=not incremental)
            assert target.read_bytes() == b"existing destination"


def _new_qpdf_warnings(saved: Path, source: Path, password: str) -> set[str]:
    """Return the warnings of ``qpdf --check`` on the encrypted ``saved`` that ``source`` lacks."""
    qpdf = _require_qpdf()
    warnings = []
    for path in (saved, source):
        command = [qpdf, "--check", f"--password={password}", str(path)]
        result = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603 - a fixed command on test data.
        # qpdf exits 2 on errors and 3 on warnings.
        assert result.returncode in {0, 3}, result.stdout + result.stderr
        lines = (result.stdout + result.stderr).splitlines()
        warnings.append({line.rsplit(": ", 1)[-1] for line in lines if "WARNING" in line})
    return warnings[0] - warnings[1]


@pytest.mark.parametrize("form", ["stream", "object_streams"])
@pytest.mark.parametrize("name", NAMES)
@pytest.mark.spec("object-streams", "read", "create")
@pytest.mark.spec("xref-streams", "read", "create")
def test_a_corpus_save_with_a_cross_reference_stream_keeps_every_object(name: str, form: str, tmp_path: Path) -> None:
    source = pyropdf.open(ASSETS / name)
    values = {number: body for number, _, body in source._core()._objects()}  # noqa: SLF001
    data = source.to_bytes(cross_reference="stream") if form == "stream" else source.to_bytes(object_streams=True)
    assert _form(data) == "stream"
    assert data[5:8] >= b"1.5"
    saved = pyropdf.open(data)
    assert not saved.is_repaired
    kept = {number: body for number, _, body in saved._core()._objects()}  # noqa: SLF001
    for number, body in values.items():
        assert kept[number] == body, number
    path = tmp_path / "saved.pdf"
    path.write_bytes(data)
    _require_qpdf()
    assert qpdf_findings(path) - qpdf_findings(ASSETS / name) == set()


@pytest.mark.spec("file-header", "edit", "preserve")
@pytest.mark.spec("incremental-update", "read")
@pytest.mark.spec("xref-streams", "read", "edit")
@pytest.mark.spec("xref-table", "create")
def test_a_save_keeps_the_cross_reference_form_of_the_opened_file() -> None:
    table = build_pdf(["Hello"]).replace(b"%PDF-1.7", b"%PDF-1.4", 1)
    doc = pyropdf.open(table)
    assert _form(doc.to_bytes()) == "table"
    stream = doc.to_bytes(cross_reference="stream")
    assert stream.startswith(b"%PDF-1.5\n")
    reopened = pyropdf.open(stream)
    assert _form(reopened.to_bytes()) == "stream"
    assert _form(reopened.to_bytes(cross_reference="table")) == "table"
    reopened.catalog["Edited"] = True
    update = reopened.to_bytes(incremental=True)
    assert update.startswith(stream)
    assert _form(update) == "stream"
    assert pyropdf.open(update).catalog["Edited"] is True


def test_an_encrypted_save_encrypts_object_streams_and_not_their_members(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(build_pdf(["Hello"]))
    doc = pyropdf.open(source)
    doc.catalog["Marker"] = "object-stream-member"
    # Uncompressed object streams would show their members if unencrypted.
    data = doc.to_bytes(encryption=Encryption(user_password="pw"), object_streams=True, decompress=True)
    assert b"object-stream-member" in doc.to_bytes(object_streams=True, decompress=True)
    assert b"object-stream-member" not in data
    assert b"/ObjStm" in data
    reopened = pyropdf.open(data, password="pw")
    assert bytes(reopened.catalog["Marker"]) == b"object-stream-member"
    assert len(reopened.pages) == 1
    path = tmp_path / "encrypted.pdf"
    path.write_bytes(data)
    assert _new_qpdf_warnings(path, source, "pw") == set()


@pytest.mark.parametrize("name", ["user-r4-rc4-128.pdf", "user-aes-256-object-streams.pdf"])
@pytest.mark.spec("crypt-filters", "preserve")
@pytest.mark.spec("encryption-dictionary", "preserve")
@pytest.mark.spec("object-streams", "read", "preserve")
def test_a_save_that_keeps_the_encryption_packs_objects_into_object_streams(name: str, tmp_path: Path) -> None:
    doc = pyropdf.open(ENCRYPTED / name, password="userpw")
    values = {number: body for number, _, body in doc._core()._objects()}  # noqa: SLF001
    data = doc.to_bytes(encryption=Encryption.KEEP, object_streams=True)
    assert _form(data) == "stream"
    reopened = pyropdf.open(data, password="userpw")
    kept = {number: body for number, _, body in reopened._core()._objects()}  # noqa: SLF001
    for number, body in values.items():
        assert kept[number] == body, number
    assert reopened.pages[0].text() == doc.pages[0].text()
    path = tmp_path / "kept.pdf"
    path.write_bytes(data)
    assert _new_qpdf_warnings(path, ENCRYPTED / name, "userpw") == set()


def test_cross_reference_options_are_checked() -> None:
    doc = pyropdf.open(build_pdf(["Hello"]))
    with pytest.raises(ValueError, match="cross_reference must be"):
        doc.to_bytes(cross_reference="xref")  # ty: ignore[invalid-argument-type]
    for options in ({"cross_reference": "stream"}, {"cross_reference": "table"}, {"object_streams": True}):
        with pytest.raises(ValueError, match="full save"):
            doc.to_bytes(incremental=True, **options)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="needs cross_reference="):
        doc.to_bytes(object_streams=True, cross_reference="table")
    with pytest.raises(TypeError, match="object_streams must be a bool"):
        doc.to_bytes(object_streams=1)  # ty: ignore[invalid-argument-type]


def _content(doc: pyropdf.Document) -> tuple[int, list[str], dict[str, bytes]]:
    """Return the page count, the text of each page, and the attachments of ``doc``."""
    attachments = {name: attachment.read() for name, attachment in doc.attachments.items()}
    return len(doc.pages), [page.text(unmapped="glyph_id") for page in doc.pages], attachments


@pytest.mark.parametrize("name", NAMES)
def test_a_renumbered_corpus_save_numbers_objects_one_through_n(name: str, tmp_path: Path) -> None:
    source = pyropdf.open(ASSETS / name)
    count = len(source.objects)
    data = source.to_bytes(renumber=True)
    saved = pyropdf.open(data)
    assert not saved.is_repaired
    numbers = sorted(saved.objects)
    # A cross-reference stream follows the renumbered objects.
    assert numbers[:count] == list(range(1, count + 1))
    assert len(numbers) == count + (_form(data) == "stream")
    assert all(generation == 0 for _, generation, _ in saved._core()._objects())  # noqa: SLF001
    assert _content(saved) == _content(source)
    path = tmp_path / "renumbered.pdf"
    path.write_bytes(data)
    _require_qpdf()
    assert qpdf_findings(path) - qpdf_findings(ASSETS / name) == set()


@pytest.mark.spec("object-streams", "edit")
@pytest.mark.spec("xref-streams", "edit")
@pytest.mark.spec("xref-table", "edit")
def test_a_renumbered_save_follows_each_reference_with_the_other_options() -> None:
    doc = pyropdf.open(build_pdf(["Hello", "World"]))
    doc.attachments.add("a.txt", b"data")
    numbers = list(doc.objects)
    del doc.attachments["a.txt"]
    doc.attachments.add("b.txt", b"other")
    data = doc.to_bytes(renumber=True, prune=True, object_streams=True, compress=True)
    saved = pyropdf.open(data)
    # The objects that the edits unlinked leave, and the rest close up.
    assert len(saved.objects) < len(numbers) + 3
    assert sorted(saved.objects) == list(range(1, len(saved.objects) + 1))
    assert _content(saved) == _content(doc)
    # The document in memory keeps the numbers of what the delete did not free.
    kept = [number for number in numbers if number in doc.objects]
    assert list(doc.objects)[: len(kept)] == kept


@pytest.mark.parametrize("encryption", ["new", "kept"])
@pytest.mark.parametrize("name", ["user-r4-rc4-128.pdf", "user-aes-256-object-streams.pdf"])
def test_an_encrypted_renumbered_save_reopens_with_its_password(name: str, encryption: str, tmp_path: Path) -> None:
    doc = pyropdf.open(ENCRYPTED / name, password="userpw")
    # qpdf opens the source and the save with the same password.
    new = Encryption(user_password="userpw", owner_password="owner")
    data = doc.to_bytes(encryption=new if encryption == "new" else Encryption.KEEP, renumber=True)
    reopened = pyropdf.open(data, password="userpw")
    assert not reopened.is_repaired
    assert max(reopened.objects) == len(reopened.objects)
    assert _content(reopened) == _content(doc)
    path = tmp_path / "renumbered.pdf"
    path.write_bytes(data)
    assert _new_qpdf_warnings(path, ENCRYPTED / name, "userpw") == set()


def test_renumber_requires_a_full_save_and_a_bool() -> None:
    doc = pyropdf.open(build_pdf(["Hello"]))
    with pytest.raises(ValueError, match="renumber requires a full save"):
        doc.to_bytes(incremental=True, renumber=True)
    with pytest.raises(TypeError, match="renumber must be a bool"):
        doc.to_bytes(renumber=1)  # ty: ignore[invalid-argument-type]


def _identical_resources() -> bytes:
    """Return two pages, each with an annotation, a layer, and a font, all identical."""
    font = "<< /Type /Font /Subtype /TrueType /BaseFont /Same /FontDescriptor {} 0 R >>"
    descriptor = "<< /Type /FontDescriptor /FontName /Same /Flags 32 /FontFile2 {} 0 R >>"
    page = "<< /Type /Page /Parent 2 0 R /Annots [{}] /Resources << /Font << /F1 {} 0 R >> >> >>"
    objects: dict[int, str | bytes] = {
        1: "<< /Type /Catalog /Pages 2 0 R /OCProperties << /OCGs [7 0 R 8 0 R] /D << /ON [7 0 R 8 0 R] >> >> >>",
        2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 200 200] >>",
        3: page.format("5 0 R", 9),
        4: page.format("6 0 R", 10),
        5: "<< /Type /Annot /Subtype /Square /Rect [10 10 50 50] >>",
        6: "<< /Type /Annot /Subtype /Square /Rect [10 10 50 50] >>",
        7: "<< /Type /OCG /Name (Layer) >>",
        8: "<< /Type /OCG /Name (Layer) >>",
        9: font.format(11),
        10: font.format(12),
        11: descriptor.format(13),
        12: descriptor.format(14),
        13: "<< /Length 12 >>\nstream\nFONTPROGRAM!\nendstream",
        14: "<< /Length 12 >>\nstream\nFONTPROGRAM!\nendstream",
    }
    return build_raw_pdf(objects)


def test_dedupe_merges_identical_fonts_but_never_pages_annotations_or_layers() -> None:
    doc = pyropdf.open(_identical_resources())
    saved = doc.to_bytes(prune=True, dedupe=True, decompress=True)
    # The font, its descriptor, and its font program merge as one chain.
    assert saved.count(b"FONTPROGRAM!") == 1
    assert saved.count(b"/BaseFont /Same") == saved.count(b"/Type /FontDescriptor") == 1
    assert len(re.findall(rb"/Type\s*/Page\b(?!s)", saved)) == 2
    assert len(re.findall(rb"/Subtype\s*/Square", saved)) == 2
    assert len(re.findall(rb"/Type\s*/OCG", saved)) == 2
    reopened = pyropdf.open(saved)
    assert not reopened.is_repaired
    assert len(reopened.pages) == 2
    fonts = [page.obj["Resources"]["Font"]["F1"] for page in reopened.pages]
    assert fonts[0].xref == fonts[1].xref == 9
    assert [len(page.obj["Annots"]) for page in reopened.pages] == [1, 1]
    # The merged numbers are free; renumbering closes them up.
    assert sorted(reopened.objects) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 13]
    compact = pyropdf.open(doc.to_bytes(prune=True, dedupe=True, renumber=True))
    assert sorted(compact.objects) == list(range(1, 12))
    assert len(compact.pages) == 2
    # The document in memory keeps its objects.
    assert len(doc.objects) == 14


def test_dedupe_keeps_objects_in_cycles_of_references() -> None:
    objects: dict[int, str | bytes] = {
        1: "<< /Type /Catalog /Pages 2 0 R /Ring [4 0 R 6 0 R] >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 200] >>",
        3: "<< /Type /Page /Parent 2 0 R >>",
        4: "<< /Other 5 0 R /Tag (RING) >>",
        5: "<< /Other 4 0 R /Tag (RING) >>",
        6: "<< /Other 7 0 R /Tag (RING) >>",
        7: "<< /Other 6 0 R /Tag (RING) >>",
        8: "<< /Shared (EQUAL) >>",
        9: "<< /Shared (EQUAL) >>",
    }
    raw = build_raw_pdf(objects).replace(b"/Ring [4 0 R 6 0 R]", b"/Ring [4 0 R 6 0 R 8 0 R 9 0 R]", 1)
    saved = pyropdf.open(raw).to_bytes(prune=True, dedupe=True, renumber=True)
    assert saved.count(b"(RING)") == 4
    assert saved.count(b"(EQUAL)") == 1
    assert len(pyropdf.open(saved).pages) == 1


@pytest.mark.parametrize("name", NAMES)
def test_a_deduplicated_corpus_save_keeps_its_content_and_is_no_larger(name: str, tmp_path: Path) -> None:
    source = pyropdf.open(ASSETS / name)
    pruned = source.to_bytes(prune=True)
    data = source.to_bytes(prune=True, dedupe=True)
    assert len(data) <= len(pruned)
    saved = pyropdf.open(data)
    assert not saved.is_repaired
    assert _content(saved) == _content(source)
    path = tmp_path / "deduplicated.pdf"
    path.write_bytes(data)
    _require_qpdf()
    assert qpdf_findings(path) - qpdf_findings(ASSETS / name) == set()


def test_dedupe_requires_prune_a_full_save_and_a_bool() -> None:
    doc = pyropdf.open(build_pdf(["Hello"]))
    with pytest.raises(ValueError, match="dedupe=True needs prune=True"):
        doc.to_bytes(dedupe=True)
    with pytest.raises(ValueError, match="full save"):
        doc.to_bytes(incremental=True, dedupe=True)
    with pytest.raises(TypeError, match="dedupe must be a bool"):
        doc.to_bytes(dedupe=1, prune=True)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("mode", ["plain", "prune", "renumber", "dedupe", "object_streams", "encryption"])
def test_full_save_refuses_live_object_zero(mode: str, tmp_path: Path) -> None:
    # Repaired input from save-fuzz crash 48ddebe9596f7ddd421a47eb48cc7105a4d0f49c.
    source = b"%PDF-1.7\n0 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
    doc = pyropdf.open(source)
    path = tmp_path / "existing.pdf"
    path.write_bytes(b"keep the existing destination")
    with pytest.raises(pyropdf.MalformedPdfError, match="object number 0"):
        doc.to_bytes(
            prune=mode in {"prune", "dedupe"},
            renumber=mode == "renumber",
            dedupe=mode == "dedupe",
            object_streams=mode == "object_streams",
            encryption=Encryption(user_password="new") if mode == "encryption" else None,
        )
    with pytest.raises(pyropdf.MalformedPdfError, match="object number 0"):
        doc.save(
            path,
            prune=mode in {"prune", "dedupe"},
            renumber=mode == "renumber",
            dedupe=mode == "dedupe",
            object_streams=mode == "object_streams",
            encryption=Encryption(user_password="new") if mode == "encryption" else None,
        )
    assert path.read_bytes() == b"keep the existing destination"
    assert doc.trailer["Root"].xref == 0


def _is_printable_ascii(data: bytes) -> bool:
    return all(byte in (9, 10, 13) or 32 <= byte <= 126 for byte in data)


def _dirty_content_pdf() -> bytes:
    """Return a PDF with messy page content, unused resources, and a messy annotation appearance."""
    doc = pyropdf.Document()
    page = doc.pages.new(size=(300, 300))
    f1 = doc.objects.add({"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica")})
    unused_font = doc.objects.add({"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Courier")})
    dirty_page_content = (
        b"% comment to remove\n"
        b"Q\n"  # unmatched Q
        b"q Q\n"  # empty q Q pair
        b"BT ET\n"  # empty BT ET pair
        b"q\n"  # unclosed q
        b"BT   /F1   12   Tf   20   200   Td   (CleanMe)   Tj   ET\n"
    )
    content_xref = doc.objects.add_stream(dirty_page_content, filter=None)
    page.obj["Contents"] = doc.objects[content_xref]
    page.obj["Resources"] = {
        "Font": {"F1": doc.objects[f1], "FUnused": doc.objects[unused_font]},
    }
    ap_unused = doc.objects.add({"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Times-Roman")})
    ap_stream = doc.objects.add_stream(
        b"% annot comment\nq Q\nq 1 0 0 rg 0 0 20 20 re f\n",
        {
            "Type": Name("XObject"),
            "Subtype": Name("Form"),
            "BBox": [0, 0, 20, 20],
            "Resources": {"Font": {"FApUnused": doc.objects[ap_unused]}},
        },
        filter=None,
    )
    annot = doc.objects.add(
        {
            "Type": Name("Annot"),
            "Subtype": Name("Square"),
            "Rect": [10, 10, 30, 30],
            "AP": {"N": doc.objects[ap_stream]},
        }
    )
    page.obj["Annots"] = [doc.objects[annot]]
    return doc.to_bytes()


def test_clean_save_cleans_pages_and_annotations_without_mutating_live_document(tmp_path: Path) -> None:
    doc = pyropdf.open(_dirty_content_pdf())
    before_bytes = doc.to_bytes()
    before_history = (doc.history.steps, doc.history.position)
    before_objects = sorted(doc.objects)
    before_changes = doc.changes(incremental=False)
    before_page_raw = doc.pages[0].obj["Contents"].read()
    before_fonts = set(doc.pages[0].obj["Resources"]["Font"].keys())
    before_ap_raw = doc.pages[0].obj["Annots"][0]["AP"]["N"].read()

    cleaned_bytes = doc.to_bytes(clean=True)
    saved_path = tmp_path / "cleaned.pdf"
    doc.save(saved_path, clean=True)

    # Caller's live document, history, changes, and objects are completely untouched.
    assert (doc.history.steps, doc.history.position) == before_history
    assert sorted(doc.objects) == before_objects
    assert doc.changes(incremental=False) == before_changes
    assert doc.pages[0].obj["Contents"].read() == before_page_raw
    assert set(doc.pages[0].obj["Resources"]["Font"].keys()) == before_fonts
    assert doc.pages[0].obj["Annots"][0]["AP"]["N"].read() == before_ap_raw
    assert doc.to_bytes() == before_bytes

    for payload in (cleaned_bytes, saved_path.read_bytes()):
        reopened = pyropdf.open(payload)
        assert not reopened.is_repaired
        page = reopened.pages[0]
        assert page.text().strip() == "CleanMe"
        content = page.obj["Contents"].read()
        assert b"% comment" not in content
        assert content.startswith(b"q\n")
        assert content.endswith(b"Q")
        assert set(page.obj["Resources"]["Font"].keys()) == {"F1"}
        ap = page.obj["Annots"][0]["AP"]["N"]
        ap_data = ap.read()
        assert b"% annot comment" not in ap_data
        assert ap_data.endswith(b"Q")
        assert "Font" not in ap["Resources"]
        with pikepdf.open(io.BytesIO(payload)) as pike:
            assert len(pike.pages) == 1

    # Composing clean=True with prune=True drops the unreferenced fonts from output only.
    pruned = pyropdf.open(doc.to_bytes(clean=True, prune=True))
    assert len(pruned.objects) < len(doc.objects)
    assert sorted(doc.objects) == before_objects


def test_clean_save_failure_leaves_destination_and_document_unchanged(tmp_path: Path) -> None:
    broken = pyropdf.open(build_pdf(["Hello"]))
    bad_stream = broken.objects.add_stream(b"BT /F1 12 Tf (Hi) Tj ET\n(unterminated", filter=None)
    broken.pages[0].obj["Contents"] = broken.objects[bad_stream]
    broken_history = (broken.history.steps, broken.history.position)
    broken_objects = sorted(broken.objects)
    target = tmp_path / "untouched.pdf"
    target.write_bytes(b"keep me")
    with pytest.raises(ValueError, match="hayro stops reading"):
        broken.to_bytes(clean=True)
    with pytest.raises(ValueError, match="hayro stops reading"):
        broken.save(target, clean=True)
    assert target.read_bytes() == b"keep me"
    assert (broken.history.steps, broken.history.position) == broken_history
    assert sorted(broken.objects) == broken_objects


def test_clean_save_inside_transaction_sees_draft_without_mutating_it() -> None:
    doc = pyropdf.open(_dirty_content_pdf())
    with doc.transaction("rotate"):
        doc.pages[0].rotation = 90
        cleaned = doc.to_bytes(clean=True)
        # Live draft still has its uncleaned fonts and content inside the transaction.
        assert "FUnused" in doc.pages[0].obj["Resources"]["Font"]
    reopened = pyropdf.open(cleaned)
    assert reopened.pages[0].rotation == 90
    assert set(reopened.pages[0].obj["Resources"]["Font"].keys()) == {"F1"}
    assert doc.history.steps == ("rotate",)


def _valid_jpeg_bytes() -> bytes:
    """Return a minimal valid 1x1 grayscale JPEG accepted cleanly by qpdf."""
    buf = io.BytesIO()
    Image.new("L", (1, 1), 128).save(buf, format="JPEG")
    return buf.getvalue()


def test_ascii_and_pretty_save_output_is_printable_ascii_deterministic_and_valid(tmp_path: Path) -> None:
    jpeg_bytes = _valid_jpeg_bytes()
    raw = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /SampleImage 6 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 300 300] >>",
            3: "<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            4: b"<< /Length 36 >>\nstream\nBT /F1 12 Tf 20 200 Td (Hello) Tj ET\nendstream",
            5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            6: (
                f"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray "
                f"/BitsPerComponent 8 /Filter /DCTDecode /DecodeParms << /ColorTransform 0 >> "
                f"/Length {len(jpeg_bytes)} >>\nstream\n".encode("ascii")
                + jpeg_bytes
                + b"\nendstream"
            ),
        }
    )
    doc = pyropdf.open(raw)
    doc.catalog["BinaryNote"] = b"raw\x00\x01\xfe\xffbytes"
    doc.objects.add_stream(b"BT /F1 12 Tf (Readable) Tj ET\n", filter=None)
    flated_xref = doc.objects.add_stream(b"Flate stream payload " * 10, filter="FlateDecode")
    doc.catalog["Flated"] = doc.objects[flated_xref]

    modes: tuple[tuple[bool, Literal["table", "stream", "hybrid"] | None, bool], ...] = (
        (False, None, False),
        (True, None, False),
        (True, "stream", False),
        (True, None, True),
        (True, "hybrid", False),
    )
    for index, (pretty, cross_reference, object_streams) in enumerate(modes):
        first = doc.to_bytes(
            ascii=True,
            pretty=pretty,
            cross_reference=cross_reference,
            object_streams=object_streams,
        )
        second = doc.to_bytes(
            ascii=True,
            pretty=pretty,
            cross_reference=cross_reference,
            object_streams=object_streams,
        )
        assert first == second
        assert _is_printable_ascii(first)
        assert b"%\xe2\xe3\xcf\xd3" not in first
        if pretty:
            expected_key = b"\n  /Type /ObjStm" if object_streams else b"\n  /Type /Catalog"
            assert expected_key in first
            assert b"trailer\n<<" in first or b"\n  /Type /XRef" in first

        reopened = pyropdf.open(first)
        assert not reopened.is_repaired
        assert reopened.pages[0].text().strip() == "Hello"
        assert reopened.catalog["BinaryNote"] == b"raw\x00\x01\xfe\xffbytes"
        assert reopened.objects[flated_xref].read() == b"Flate stream payload " * 10
        # JPEG stream retains /DCTDecode under /ASCII85Decode without lossy re-encoding.
        assert _filters(reopened.objects[6]) == ["ASCII85Decode", "DCTDecode"]
        with pikepdf.open(io.BytesIO(first)) as pike:
            assert len(pike.pages) == 1
        path = tmp_path / f"ascii_{index}.pdf"
        path.write_bytes(first)
        _require_qpdf()
        assert qpdf_findings(path) == set()

    # pretty=True alone formats dicts deterministically while keeping binary header.
    pretty_only = doc.to_bytes(pretty=True)
    assert pretty_only == doc.to_bytes(pretty=True)
    assert b"\n  /Type /Catalog" in pretty_only
    assert b"%\xe2\xe3\xcf\xd3" in pretty_only
    assert pyropdf.open(pretty_only).pages[0].text().strip() == "Hello"


@pytest.mark.parametrize("name", NAMES)
def test_ascii_and_pretty_corpus_round_trip(name: str, tmp_path: Path) -> None:
    source = pyropdf.open(ASSETS / name)
    data = source.to_bytes(ascii=True, pretty=True)
    assert _is_printable_ascii(data)
    assert data == source.to_bytes(ascii=True, pretty=True)
    reopened = pyropdf.open(data)
    assert not reopened.is_repaired
    assert _content(reopened) == _content(source)
    with pikepdf.open(io.BytesIO(data)) as pike:
        assert len(pike.pages) == len(source.pages)
    path = tmp_path / "ascii_pretty.pdf"
    path.write_bytes(data)
    _require_qpdf()
    assert qpdf_findings(path) - qpdf_findings(ASSETS / name) == set()


def _stream_filter_fixture() -> tuple[pyropdf.Document, bytes, bytes, bytes, bytes, bytes, int, int, int, int]:
    jpeg_bytes = _valid_jpeg_bytes()
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /SampleImage 6 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 300 300] >>",
                3: "<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
                4: b"<< /Length 42 >>\nstream\nBT /F1 12 Tf 20 200 Td (Filter test) Tj ET\nendstream",
                5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                6: (
                    f"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray "
                    f"/BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg_bytes)} >>\nstream\n".encode("ascii")
                    + jpeg_bytes
                    + b"\nendstream"
                ),
            }
        )
    )
    data_a = b"AAAAAAABBBBBBBCCCCCCC Hello filter A!\n" * 8
    data_b = bytes(range(256)) * 2
    data_c = b"Unlisted stream stays untouched!\n"
    xmp = b"<?xpacket begin=''?><x:xmpmeta/>" * 4
    s_a = doc.objects.add_stream(data_a, filter=None)
    s_b = doc.objects.add_stream(data_b, filter="FlateDecode")
    s_c = doc.objects.add_stream(data_c, filter=None)
    s_xmp = doc.objects.add_stream(xmp, {"Type": Name("Metadata")}, filter=None)
    doc.catalog["Extra"] = [doc.objects[s_a], doc.objects[s_b], doc.objects[s_c], doc.objects[s_xmp]]
    return doc, jpeg_bytes, data_a, data_b, data_c, xmp, s_a, s_b, s_c, s_xmp


def test_stream_filter_document_wide_round_trip(tmp_path: Path) -> None:
    doc, jpeg_bytes, data_a, data_b, data_c, xmp, s_a, s_b, s_c, s_xmp = _stream_filter_fixture()
    choices: tuple[
        tuple[Literal["FlateDecode", "ASCII85Decode", "ASCIIHexDecode", "RunLengthDecode", "none"], list[str]], ...
    ] = (
        ("FlateDecode", ["FlateDecode"]),
        ("ASCII85Decode", ["ASCII85Decode"]),
        ("ASCIIHexDecode", ["ASCIIHexDecode"]),
        ("RunLengthDecode", ["RunLengthDecode"]),
        ("none", []),
    )
    for choice, expected in choices:
        saved = doc.to_bytes(stream_filter=choice, compression_level=9)
        reopened = pyropdf.open(saved)
        assert not reopened.is_repaired
        assert _filters(reopened.objects[s_a]) == expected
        assert _filters(reopened.objects[s_b]) == expected
        assert _filters(reopened.objects[s_c]) == expected
        # Document-wide filter selection preserves unfiltered XMP metadata and JPEG images.
        assert _filters(reopened.objects[s_xmp]) == []
        assert _filters(reopened.objects[6]) == ["DCTDecode"]
        assert reopened.objects[6].read(decode=False) == jpeg_bytes
        assert reopened.objects[s_a].read() == data_a
        assert reopened.objects[s_b].read() == data_b
        assert reopened.objects[s_c].read() == data_c
        assert reopened.objects[s_xmp].read() == xmp
        assert reopened.pages[0].text().strip() == "Filter test"
        with pikepdf.open(io.BytesIO(saved)) as pike:
            assert len(pike.pages) == 1
        path = tmp_path / f"filter_{choice}.pdf"
        path.write_bytes(saved)
        _require_qpdf()
        assert qpdf_findings(path) == set()


def test_stream_filter_per_stream_overrides_and_budget() -> None:
    doc, _, data_a, data_b, data_c, xmp, s_a, s_b, s_c, s_xmp = _stream_filter_fixture()
    # stream_filter="keep" preserves existing filters untouched.
    kept = pyropdf.open(doc.to_bytes(stream_filter="keep"))
    assert _filters(kept.objects[s_a]) == []
    assert _filters(kept.objects[s_b]) == ["FlateDecode"]

    # Per-stream mapping overrides only named objects and leaves others untouched (F3).
    per_stream = doc.to_bytes(
        stream_filter={
            s_a: "ASCIIHexDecode",
            s_b: None,
            s_xmp: "ASCII85Decode",
        }
    )
    reopened = pyropdf.open(per_stream)
    assert _filters(reopened.objects[s_a]) == ["ASCIIHexDecode"]
    assert _filters(reopened.objects[s_b]) == []
    assert _filters(reopened.objects[s_c]) == []
    assert _filters(reopened.objects[s_xmp]) == ["ASCII85Decode"]
    assert reopened.objects[s_a].read() == data_a
    assert reopened.objects[s_b].read() == data_b
    assert reopened.objects[s_c].read() == data_c
    assert reopened.objects[s_xmp].read() == xmp

    # Budget is charged during filter re-encoding (F6).
    big = pyropdf.open(build_pdf(["Budget"]))
    big_stream = big.objects.add_stream(b"X" * 40_000, filter=None)
    big.catalog["Big"] = big.objects[big_stream]
    tight = pyropdf.open(big.to_bytes(), limits=pyropdf.DocumentLimits(max_memory_size=40_000))
    with pytest.raises(LimitError) as exc_info:
        tight.to_bytes(stream_filter="ASCIIHexDecode")
    assert exc_info.value.code == "memory_size"


def test_new_full_save_options_validation_and_refusals() -> None:
    doc = pyropdf.open(build_pdf(["Validation"]))
    # Refused on incremental saves.
    for kwargs in (
        {"clean": True},
        {"ascii": True},
        {"pretty": True},
        {"stream_filter": "FlateDecode"},
        {"stream_filter": {1: "ASCII85Decode"}},
    ):
        with pytest.raises(ValueError, match="full save"):
            doc.to_bytes(incremental=True, **kwargs)  # ty: ignore[invalid-argument-type]

    # stream_filter excludes compress=True and decompress=True.
    with pytest.raises(ValueError, match="stream_filter"):
        doc.to_bytes(compress=True, stream_filter="FlateDecode")
    with pytest.raises(ValueError, match="stream_filter"):
        doc.to_bytes(decompress=True, stream_filter="none")

    # linearize=True refuses ascii and pretty.
    with pytest.raises(ValueError, match="linearize=True"):
        doc.to_bytes(linearize=True, ascii=True)
    with pytest.raises(ValueError, match="linearize=True"):
        doc.to_bytes(linearize=True, pretty=True)

    # ascii=True refuses active encryption, but allows decrypting an unlocked encrypted doc.
    with pytest.raises(ValueError, match="encrypted"):
        doc.to_bytes(ascii=True, encryption=Encryption(user_password="pw"))
    enc = pyropdf.open(ENCRYPTED / "user-aes-256.pdf", password="userpw")
    with pytest.raises(ValueError, match="encrypted"):
        enc.to_bytes(ascii=True, encryption=Encryption.KEEP)
    decrypted_ascii = enc.to_bytes(ascii=True)
    assert _is_printable_ascii(decrypted_ascii)
    assert len(pyropdf.open(decrypted_ascii).pages) == len(enc.pages)

    # Type and value validation.
    for flag in ("clean", "ascii", "pretty"):
        with pytest.raises(TypeError, match=f"{flag} must be a bool"):
            doc.to_bytes(**{flag: 1})  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="stream_filter"):
        doc.to_bytes(stream_filter="UnknownFilter")  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="stream_filter"):
        doc.to_bytes(stream_filter=123)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="keys"):
        doc.to_bytes(stream_filter={True: "FlateDecode"})
    with pytest.raises(ValueError, match="positive"):
        doc.to_bytes(stream_filter={0: "FlateDecode"})
    with pytest.raises(ValueError, match="unsupported stream_filter"):
        doc.to_bytes(stream_filter={1: "DCTDecode"})  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="values"):
        doc.to_bytes(stream_filter={1: 42})  # ty: ignore[invalid-argument-type]


def _load_recipient_credential(label: str) -> pyropdf.RecipientCredential:
    from cryptography import x509  # noqa: PLC0415
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ec, rsa  # noqa: PLC0415

    recipients_dir = ENCRYPTED / "recipients"
    key = serialization.load_der_private_key((recipients_dir / f"recipient-{label}.key").read_bytes(), None)
    assert isinstance(key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey))
    cert = x509.load_der_x509_certificate((recipients_dir / f"recipient-{label}.cer").read_bytes())
    return pyropdf.RecipientCredential(certificate=cert, private_key=key)


def test_pretty_save_with_encryption_preserves_multiline_formatting() -> None:
    doc = pyropdf.open(build_pdf(["Encrypted Pretty"]))
    doc.catalog["SecretNote"] = "hidden-pretty-secret"

    # Password Encryption (plain, renumber, object_streams).
    pwd_enc = Encryption(user_password="reader", owner_password="owner")
    for kwargs in (
        {"pretty": True},
        {"pretty": True, "renumber": True},
        {"pretty": True, "object_streams": True},
    ):
        saved = doc.to_bytes(encryption=pwd_enc, **kwargs)  # ty: ignore[invalid-argument-type]
        assert b"hidden-pretty-secret" not in saved
        expected_key = b"\n  /Type /ObjStm" if kwargs.get("object_streams") else b"\n  /Type /Catalog"
        assert expected_key in saved
        assert b"\n  /Filter /Standard" in saved
        reopened = pyropdf.open(saved, password="reader")
        assert not reopened.is_repaired
        assert bytes(reopened.catalog["SecretNote"]) == b"hidden-pretty-secret"
        assert reopened.pages[0].text().strip() == "Encrypted Pretty"

    # RecipientEncryption with pretty=True (new and Encryption.KEEP).
    owner = _load_recipient_credential("owner")
    recip_enc = pyropdf.RecipientEncryption(recipients={owner.certificate: None})
    for kwargs in ({"pretty": True}, {"pretty": True, "renumber": True}):
        saved = doc.to_bytes(encryption=recip_enc, **kwargs)  # ty: ignore[invalid-argument-type]
        assert b"hidden-pretty-secret" not in saved
        assert b"\n  /Type /Catalog" in saved
        assert b"\n  /Filter /Adobe.PubSec" in saved
        reopened = pyropdf.open(saved, credential=owner)
        assert not reopened.is_repaired
        assert bytes(reopened.catalog["SecretNote"]) == b"hidden-pretty-secret"
        assert reopened.pages[0].text().strip() == "Encrypted Pretty"

    recip_doc = pyropdf.open(ENCRYPTED / "recipients" / "pyhanko-recipients.pdf", credential=owner)
    recip_keep = recip_doc.to_bytes(encryption=Encryption.KEEP, pretty=True)
    assert b"\n  /Type /Catalog" in recip_keep
    assert b"\n  /Filter /Adobe.PubSec" in recip_keep
    recip_reopened = pyropdf.open(recip_keep, credential=owner)
    assert not recip_reopened.is_repaired
    assert len(recip_reopened.pages) == len(recip_doc.pages)

    # Encryption.KEEP with pretty=True (untouched, edited, renumbered, and object_streams).
    for fixture in ("user-aes-256.pdf", "user-r4-rc4-128.pdf", "user-aes-256-object-streams.pdf"):
        enc_doc = pyropdf.open(ENCRYPTED / fixture, password="userpw")
        expected_text = [page.text() for page in enc_doc.pages]
        for edit, kwargs in (
            (False, {"pretty": True}),
            (True, {"pretty": True}),
            (False, {"pretty": True, "renumber": True}),
            (False, {"pretty": True, "object_streams": True}),
        ):
            if edit:
                enc_doc.catalog["KeepSecret"] = "kept-pretty-secret"
            saved = enc_doc.to_bytes(encryption=Encryption.KEEP, **kwargs)  # ty: ignore[invalid-argument-type]
            if edit:
                assert b"kept-pretty-secret" not in saved
            expected_key = b"\n  /Type /ObjStm" if kwargs.get("object_streams") else b"\n  /Type /Catalog"
            assert expected_key in saved
            reopened = pyropdf.open(saved, password="userpw")
            assert not reopened.is_repaired
            assert [page.text() for page in reopened.pages] == expected_text
            if edit:
                assert bytes(reopened.catalog["KeepSecret"]) == b"kept-pretty-secret"


def test_ascii_save_edge_cases_external_stream_header_filters_and_crypt() -> None:
    binary_bytes = b"\x00\x01\x80\xfe\xffXYZ"
    # Build a raw PDF exercising external /F stream, /Filter null, /Filter [] + /DecodeParms, and /Filter /Crypt.
    tmp = pyropdf.Document()
    flate_xref = tmp.objects.add_stream(binary_bytes, filter="FlateDecode")
    flate_raw = tmp.objects[flate_xref].read(decode=False)

    raw = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /Streams [4 0 R 5 0 R 6 0 R 7 0 R 8 0 R] >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 100 100] >>",
            3: "<< /Type /Page /Parent 2 0 R >>",
            4: (
                b"<< /F (external.bin) /FFilter /FlateDecode /FDecodeParms << /Columns 4 >> /Length 4 >>\n"
                b"stream\n\xff\xfe\xfd\xfc\nendstream"
            ),
            5: (
                f"<< /Filter null /DecodeParms << /Columns 2 >> /Length {len(binary_bytes)} >>\nstream\n".encode()
                + binary_bytes
                + b"\nendstream"
            ),
            6: (
                f"<< /Filter [] /DecodeParms << /Columns 2 >> /Length {len(binary_bytes)} >>\nstream\n".encode()
                + binary_bytes
                + b"\nendstream"
            ),
            7: (
                b"<< /Filter /Crypt /DecodeParms << /Type /CryptFilterDecodeParms /Name /Identity >> "
                + f"/Length {len(binary_bytes)} >>\nstream\n".encode()
                + binary_bytes
                + b"\nendstream"
            ),
            8: (
                b"<< /Filter [/Crypt /FlateDecode] "
                b"/DecodeParms [<< /Type /CryptFilterDecodeParms /Name /Identity >> null] "
                + f"/Length {len(flate_raw)} >>\nstream\n".encode()
                + flate_raw
                + b"\nendstream"
            ),
        }
    )
    # Also verify malformed header %PDF-1.\xff falls back to %PDF-1.7.
    malformed_header = b"%PDF-1.\xff" + raw[8:]
    doc = pyropdf.open(malformed_header)
    ascii_pdf = doc.to_bytes(ascii=True)
    assert ascii_pdf.startswith(b"%PDF-1.7\n")
    assert _is_printable_ascii(ascii_pdf)

    reopened = pyropdf.open(ascii_pdf)
    assert not reopened.is_repaired
    # 4: external /F stream strips ignored binary inline bytes while keeping /F, /FFilter, /FDecodeParms.
    ext = reopened.objects[4]
    assert bytes(ext["F"]) == b"external.bin"
    assert str(ext["FFilter"]) == "FlateDecode"
    assert ext["FDecodeParms"]["Columns"] == 4
    assert "Filter" not in ext
    assert ext.read(decode=False) == b""

    # 5 & 6: /Filter null and /Filter [] become /ASCII85Decode without stale /DecodeParms.
    for xref in (5, 6):
        s = reopened.objects[xref]
        assert _filters(s) == ["ASCII85Decode"]
        assert "DecodeParms" not in s
        assert s.read() == binary_bytes

    # 7 & 8: /Crypt stays first ahead of /ASCII85Decode.
    s7 = reopened.objects[7]
    assert _filters(s7) == ["Crypt", "ASCII85Decode"]
    assert s7.read() == binary_bytes
    s8 = reopened.objects[8]
    assert _filters(s8) == ["Crypt", "ASCII85Decode", "FlateDecode"]
    assert s8.read() == binary_bytes

    # Non-Identity /Crypt on an unencrypted binary stream is refused cleanly.
    bad_crypt = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /S 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 100 100] >>",
            3: "<< /Type /Page /Parent 2 0 R >>",
            4: (
                b"<< /Filter /Crypt /DecodeParms << /Name /CustomCF >> "
                + f"/Length {len(binary_bytes)} >>\nstream\n".encode()
                + binary_bytes
                + b"\nendstream"
            ),
        }
    )
    with pytest.raises(pyropdf.MalformedPdfError, match="Crypt"):
        pyropdf.open(bad_crypt).to_bytes(ascii=True)


def test_streaming_save_releases_per_object_memory_budget(tmp_path: Path) -> None:
    builder = pyropdf.open(build_pdf(["Streaming"]))
    payload = bytes((i * 37 + 13) & 0xFF for i in range(16_384))
    stream_xrefs = [builder.objects.add_stream(payload, filter=None) for _ in range(24)]
    builder.catalog["Many"] = [builder.objects[x] for x in stream_xrefs]
    source_bytes = builder.to_bytes()

    # 24 streams * 16 KiB = ~393 KiB raw (~500-786 KiB ASCII85/ASCIIHex encoded),
    # while max_memory_size=200_000 only permits single-object peak memory.
    tight = pyropdf.open(source_bytes, limits=pyropdf.DocumentLimits(max_memory_size=200_000))
    for name, kwargs in (
        ("ascii.pdf", {"ascii": True}),
        ("ascii_pretty.pdf", {"ascii": True, "pretty": True}),
        ("hex.pdf", {"stream_filter": "ASCIIHexDecode"}),
        ("flate.pdf", {"stream_filter": "FlateDecode"}),
        ("renum.pdf", {"ascii": True, "renumber": True}),
    ):
        target = tmp_path / name
        tight.save(target, **kwargs)  # ty: ignore[invalid-argument-type]
        reopened = pyropdf.open(target)
        assert not reopened.is_repaired
        assert len(reopened.catalog["Many"]) == 24
        assert reopened.catalog["Many"][0].read() == payload
        assert reopened.catalog["Many"][-1].read() == payload


def test_empty_compressed_streams_and_blank_cleaned_pages_recode() -> None:
    doc = pyropdf.open(build_pdf(["Valid"]))
    s_flate = doc.objects.add_stream(b"", filter="FlateDecode")
    s_a85 = doc.objects.add_stream(b"", filter="ASCII85Decode")
    s_hex = doc.objects.add_stream(b"", filter="ASCIIHexDecode")
    s_rle = doc.objects.add_stream(b"", filter="RunLengthDecode")
    doc.catalog["EmptyStreams"] = [doc.objects[x] for x in (s_flate, s_a85, s_hex, s_rle)]

    decompressed = pyropdf.open(doc.to_bytes(decompress=True))
    for x in (s_flate, s_a85, s_hex, s_rle):
        assert _filters(decompressed.objects[x]) == []
        assert decompressed.objects[x].read(decode=False) == b""

    hexed = pyropdf.open(doc.to_bytes(stream_filter="ASCIIHexDecode"))
    for x in (s_flate, s_a85, s_hex, s_rle):
        assert _filters(hexed.objects[x]) == ["ASCIIHexDecode"]
        assert hexed.objects[x].read() == b""

    # Blank page cleaned by clean=True produces an empty Flate stream that stream_filter/decompress recodes.
    blank_doc = pyropdf.Document()
    page = blank_doc.pages.new(size=(200, 200))
    blank_stream = blank_doc.objects.add_stream(b"% empty page\nq Q\nBT ET\n", filter=None)
    page.obj["Contents"] = blank_doc.objects[blank_stream]

    cleaned_hex = pyropdf.open(blank_doc.to_bytes(clean=True, stream_filter="ASCIIHexDecode"))
    assert _filters(cleaned_hex.pages[0].obj["Contents"]) == ["ASCIIHexDecode"]
    assert cleaned_hex.pages[0].obj["Contents"].read() == b""

    cleaned_decomp = pyropdf.open(blank_doc.to_bytes(clean=True, decompress=True))
    assert _filters(cleaned_decomp.pages[0].obj["Contents"]) == []
    assert cleaned_decomp.pages[0].obj["Contents"].read(decode=False) == b""


def test_clean_preserves_and_remaps_per_stream_filters_and_complex_pages() -> None:
    # 1. Single unshared /Contents stream: rewritten in place so content_xref stays intact.
    doc = pyropdf.open(_dirty_content_pdf())
    content_xref = doc.pages[0].obj["Contents"].xref
    assert content_xref is not None
    before_objects = sorted(doc.objects)
    for prune in (False, True):
        saved = doc.to_bytes(clean=True, prune=prune, stream_filter={content_xref: "ASCIIHexDecode"})
        reopened = pyropdf.open(saved)
        assert not reopened.is_repaired
        reopened_contents = reopened.pages[0].obj["Contents"]
        assert reopened_contents.xref == content_xref
        assert _filters(reopened_contents) == ["ASCIIHexDecode"]
        assert reopened.pages[0].text().strip() == "CleanMe"
        assert b"% comment" not in reopened_contents.read()
    assert sorted(doc.objects) == before_objects

    # 2. Multi-stream /Contents array: collapsed stream receives the remapped stream_filter override.
    part1 = b"% part 1\nq\nBT /F1 12 Tf\n"
    part2 = (
        b"20 150 Td (MultiClean) Tj ET\n"
        b"/Span /MC1 BDC EMC\n"
        b"q 10 0 0 10 50 50 cm BI /W 1 /H 1 /CS /CS1 /BPC 8 ID \x00 EI Q\n"
    )
    multi = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 200] /Resources 8 0 R >>",
                3: "<< /Type /Page /Parent 2 0 R /Contents [4 0 R 5 0 R] >>",
                4: f"<< /Length {len(part1)} >>\nstream\n".encode() + part1 + b"\nendstream",
                5: f"<< /Length {len(part2)} >>\nstream\n".encode() + part2 + b"\nendstream",
                8: (
                    "<< /Font << /F1 9 0 R /FUnused 10 0 R >> "
                    "/ColorSpace << /CS1 [/Indexed /DeviceRGB 0 <ff0000>] /CSUnused /DeviceGray >> "
                    "/Properties << /MC1 << /MCID 0 >> /MCUnused << /MCID 1 >> >> >>"
                ),
                9: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                10: "<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
            }
        )
    )
    for prune in (False, True):
        saved = multi.to_bytes(clean=True, prune=prune, stream_filter={4: "ASCIIHexDecode"})
        reopened = pyropdf.open(saved)
        assert not reopened.is_repaired
        page = reopened.pages[0]
        assert _filters(page.obj["Contents"]) == ["ASCIIHexDecode"]
        assert page.text().strip() == "MultiClean"
        # Inherited resources pruned while keeping used Font, inline-image ColorSpace, and /MCID Properties.
        assert set(page.obj["Resources"]["Font"]) == {"F1"}
        assert set(page.obj["Resources"]["ColorSpace"]) == {"CS1"}
        assert set(page.obj["Resources"]["Properties"]) == {"MC1"}
    # Live document's inherited resources and multi-stream /Contents remain completely untouched.
    assert "Resources" not in multi.pages[0].obj
    assert len(multi.pages[0].obj["Contents"]) == 2

    # 3. Shared /Contents stream referenced by two pages: remapped to both new streams.
    shared_body = b"% shared\nq Q\nBT /F1 12 Tf 20 100 Td (Shared) Tj ET\n"
    shared = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 200 200] >>",
                3: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /Resources << /Font << /F1 6 0 R >> >> >>",
                4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /Resources << /Font << /F1 6 0 R >> >> >>",
                5: f"<< /Length {len(shared_body)} >>\nstream\n".encode() + shared_body + b"\nendstream",
                6: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            }
        )
    )
    shared_saved = pyropdf.open(shared.to_bytes(clean=True, prune=True, stream_filter={5: "ASCIIHexDecode"}))
    for page in shared_saved.pages:
        assert _filters(page.obj["Contents"]) == ["ASCIIHexDecode"]
        assert page.text().strip() == "Shared"


def test_dedupe_respects_per_stream_filter_overrides() -> None:
    doc = pyropdf.open(build_pdf(["DedupeFilter"]))
    payload = b"Duplicate stream payload for dedupe test!\n" * 6
    s1 = doc.objects.add_stream(payload, filter=None)
    s2 = doc.objects.add_stream(payload, filter=None)
    doc.catalog["Pair"] = [doc.objects[s1], doc.objects[s2]]

    # Override on higher xref only: must not merge or drop override.
    hi = pyropdf.open(doc.to_bytes(prune=True, dedupe=True, stream_filter={s2: "ASCIIHexDecode"}))
    a, b = hi.catalog["Pair"]
    assert a.xref != b.xref
    assert _filters(a) == []
    assert _filters(b) == ["ASCIIHexDecode"]
    assert a.read() == b.read() == payload

    # Override on lower xref only: must not leak onto higher xref.
    lo = pyropdf.open(doc.to_bytes(prune=True, dedupe=True, stream_filter={s1: "ASCIIHexDecode"}))
    a, b = lo.catalog["Pair"]
    assert a.xref != b.xref
    assert _filters(a) == ["ASCIIHexDecode"]
    assert _filters(b) == []

    # Same override on both: streams merge into lower xref with override intact.
    both = pyropdf.open(
        doc.to_bytes(prune=True, dedupe=True, stream_filter={s1: "ASCIIHexDecode", s2: "ASCIIHexDecode"})
    )
    a, b = both.catalog["Pair"]
    assert a.xref == b.xref
    assert _filters(a) == ["ASCIIHexDecode"]
    assert a.read() == payload


def test_compress_with_ascii_keeps_shorter_printable_ascii_stream() -> None:
    doc = pyropdf.open(build_pdf(["CompressAscii"]))
    short_ascii = b"0 0 m 10 10 l S\n" * 3
    long_ascii = b"0 0 m 10 10 l S\n" * 100
    incompressible_binary = bytes((i * 173 + 0x81) & 0xFF for i in range(64))
    s_short = doc.objects.add_stream(short_ascii, filter=None)
    s_long = doc.objects.add_stream(long_ascii, filter=None)
    s_bin = doc.objects.add_stream(incompressible_binary, filter=None)
    doc.catalog["Targets"] = [doc.objects[s_short], doc.objects[s_long], doc.objects[s_bin]]

    for renumber in (False, True):
        saved = doc.to_bytes(compress=True, ascii=True, renumber=renumber)
        assert _is_printable_ascii(saved)
        reopened = pyropdf.open(saved)
        out_short, out_long, out_bin = reopened.catalog["Targets"]
        assert _filters(out_short) == []
        assert out_short.read(decode=False) == short_ascii
        assert _filters(out_long) == ["ASCII85Decode", "FlateDecode"]
        assert out_long.read() == long_ascii
        assert _filters(out_bin) == ["ASCII85Decode"]
        assert out_bin.read() == incompressible_binary
