"""ISO 32000-2 §7.4 general-purpose filter round trips through both PDF save paths."""

from __future__ import annotations

import base64
import io
import zlib
from typing import TYPE_CHECKING

import pikepdf
import pytest

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Callable


def _literal_lzw(data: bytes) -> bytes:
    """Build an independent MSB-first LZW stream, resetting before each literal."""
    codes = [code for value in data for code in (256, value)] + [257]
    bits = "".join(f"{code:09b}" for code in codes)
    return int(bits.ljust((len(bits) + 7) // 8 * 8, "0"), 2).to_bytes((len(bits) + 7) // 8)


def _literal_runs(data: bytes) -> bytes:
    """Use only literal runs, leaving the encoder free to choose another representation."""
    return b"".join(bytes([len(data[i : i + 128]) - 1]) + data[i : i + 128] for i in range(0, len(data), 128)) + b"\x80"


def _exercise(name: str, encode: Callable[[bytes], bytes], *, incremental: bool) -> None:
    plain = b"0 g 0 0 16 16 re f\n"
    edited = b"1 0 0 rg 0 0 16 16 re f\n"
    # White space, black, zero and repeated byte boundaries are all observable.
    unrelated = bytes(range(129)) + b"\0" * 130 + b"abc"
    doc = pyropdf.Document()
    page = doc.pages.new((16, 16))
    for key, data in (("Keep", unrelated), ("Contents", plain)):
        ref = doc.objects.add_stream(encode(data), filter=None)
        stream = doc.objects[ref]
        stream["Filter"] = pyropdf.Name(name)
        page.obj[key] = stream
    original = doc.to_bytes()
    doc = pyropdf.open(original)
    page = doc.pages[0]
    assert page.obj["Keep"].read() == unrelated
    assert page.obj["Contents"].read() == plain
    assert page.pixmap(dpi=72).pixel(8, 8)[:3] == (0, 0, 0)

    created = doc.objects.add_stream(unrelated[::-1], filter=name)
    page.obj["Created"] = doc.objects[created]
    page.obj["Contents"].write(edited, filter=name)
    result = doc.to_bytes(incremental=incremental)
    if incremental:
        assert result.startswith(original)
    saved = pyropdf.open(result).pages[0]
    assert saved.obj["Contents"]["Filter"] == name
    assert saved.obj["Contents"].read() == edited
    assert saved.obj["Created"].read() == unrelated[::-1]
    assert saved.pixmap(dpi=72).pixel(8, 8)[:3] == (255, 0, 0)
    with pikepdf.Pdf.open(io.BytesIO(result)) as independent:
        objects = independent.pages[0].obj
        assert objects.Keep.read_raw_bytes() == encode(unrelated)
        assert objects.Keep.read_bytes(pikepdf.StreamDecodeLevel.specialized) == unrelated
        assert objects.Created.read_bytes(pikepdf.StreamDecodeLevel.specialized) == unrelated[::-1]
        assert objects.Contents.read_bytes(pikepdf.StreamDecodeLevel.specialized) == edited


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("flate-filter", "edit")
def test_flate_stream_replacement(incremental: bool) -> None:  # noqa: FBT001
    _exercise("FlateDecode", zlib.compress, incremental=incremental)


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("asciihex-filter", "create", "edit")
def test_asciihex_reencodes_and_keeps_source_spelling(incremental: bool) -> None:  # noqa: FBT001
    # A reader may ignore PDF whitespace and mix lower- and uppercase digits.
    _exercise(
        "ASCIIHexDecode", lambda data: b" \n".join(f"{b:02x}".encode() for b in data) + b">", incremental=incremental
    )


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("ascii85-filter", "create", "edit", "preserve")
def test_ascii85_groups_and_source_preservation(incremental: bool) -> None:  # noqa: FBT001
    _exercise("ASCII85Decode", lambda data: base64.a85encode(data, wrapcol=17) + b"~>", incremental=incremental)


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("lzw-filter", "read", "interpret", "create", "edit", "preserve")
def test_lzw_stream_content_and_original_codes(incremental: bool) -> None:  # noqa: FBT001
    _exercise("LZWDecode", _literal_lzw, incremental=incremental)


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("run-length-filter", "read", "create", "edit", "preserve")
def test_run_length_packets_and_source_preservation(incremental: bool) -> None:  # noqa: FBT001
    _exercise("RunLengthDecode", _literal_runs, incremental=incremental)
