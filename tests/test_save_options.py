# ruff: noqa: S106 - the fixtures' passwords are test data.
"""Save options beyond full and incremental saves (DESIGN.md section 4.4)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from corpus import ASSETS, NAMES
from test_attachments import build_pdf
from test_consistency import QPDF, qpdf_findings

import pyropdf
from pyropdf import Encryption, LimitError, Name, PdfError, Stream

REFERENCE = re.compile(rb"(\d+) \d+ R")
#: The filters that hayro-syntax decodes, which a decompressed save removes.
DECODED = {"ASCIIHexDecode", "ASCII85Decode", "LZWDecode", "FlateDecode", "RunLengthDecode"}
ENCRYPTED = Path(__file__).resolve().parent / "assets" / "encrypted"


def _numbers(data: bytes) -> set[int]:
    return set(pyropdf.open(data).objects)


def test_a_pruned_save_leaves_out_what_an_edit_unlinked() -> None:
    doc = pyropdf.open(build_pdf(["Hello"]))
    doc.attachments.add("a.txt", b"data")
    doc = pyropdf.open(doc.to_bytes())
    filespec = doc.catalog["Names"]["EmbeddedFiles"]["Names"][1]
    unlinked = {filespec.xref, filespec["EF"]["F"].xref}
    del doc.attachments["a.txt"]

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
def test_a_save_without_edits_writes_the_identifier_as_read(name: str) -> None:
    data = (ASSETS / name).read_bytes()
    doc = pyropdf.open(data)
    assert doc.to_bytes(incremental=True) == data
    if "ID" in doc.trailer:
        assert _id(doc.to_bytes()) == [bytes(element) for element in doc.trailer["ID"]]


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
    with pytest.raises(ValueError, match="needs cross_reference='stream' or None"):
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
    # The document in memory keeps its numbers.
    assert list(doc.objects)[: len(numbers)] == numbers


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
