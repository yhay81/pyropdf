# ruff: noqa: S106 - the fixtures' passwords are test data.
"""Save options beyond full and incremental saves (DESIGN.md section 4.4)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from corpus import ASSETS, NAMES
from test_attachments import build_pdf

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
    for number, body in kept.items():
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
