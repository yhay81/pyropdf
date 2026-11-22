"""Write embedded PDF code-to-CID CMaps through the document store."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from pyropdf import _core
from pyropdf._objects import Name, Stream

if TYPE_CHECKING:
    from pyropdf._document import Document

_MAX_CODE_BYTES = 4
_MAX_CID = 65535
_BLOCK_SIZE = 100
_NAME_BYTES = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.+")


def _blocks(entries: list[bytes], section: bytes) -> bytes:
    """PostScript CMap blocks contain at most 100 entries."""
    chunks: list[bytes] = []
    for start in range(0, len(entries), _BLOCK_SIZE):
        rows = entries[start : start + _BLOCK_SIZE]
        block = [str(len(rows)).encode() + b" begin" + section, *rows, b"end" + section]
        chunks.append(b"\n".join(block))
    return b"\n".join(chunks)


def _hex(value: bytes) -> bytes:
    return b"<" + value.hex().encode("ascii") + b">"


def _entries(mapping: Mapping[bytes, int], operation: _core._OperationBudget) -> tuple[bytes, bytes]:
    pairs: list[tuple[bytes, int]] = []
    for code, cid in mapping.items():
        operation.charge("memory_size", 384)  # The sort entry, range, CID row, and encoded copies.
        if type(code) is not bytes or not 1 <= len(code) <= _MAX_CODE_BYTES:
            msg = "a CMap code must be one to four bytes"
            raise ValueError(msg)
        if type(cid) is not int or not 0 <= cid <= _MAX_CID:
            msg = "a CID must be an integer from 0 through 65535"
            raise ValueError(msg)
        pairs.append((code, cid))
    if not pairs:
        msg = "an encoding CMap needs at least one code"
        raise ValueError(msg)
    pairs.sort()
    operation.poll()
    ranges: list[bytes] = []
    chars: list[bytes] = []
    first = previous = pairs[0][0]
    for i, (code, cid) in enumerate(pairs):
        operation.poll()
        if i:
            if code.startswith(previous):
                msg = "a CMap code cannot be the prefix of another code"
                raise ValueError(msg)
            if code[:-1] != previous[:-1] or code[-1] != previous[-1] + 1:
                ranges.append(_hex(first) + b" " + _hex(previous))
                first = code
        chars.append(_hex(code) + b" " + str(cid).encode("ascii"))
        previous = code
    ranges.append(_hex(first) + b" " + _hex(previous))
    return _blocks(ranges, b"codespacerange"), _blocks(chars, b"cidchar")


def write_cmap(  # noqa: PLR0913 - identifiers correspond to the CMap dictionary.
    document: Document,
    mapping: Mapping[bytes, int],
    *,
    name: str,
    registry: bytes,
    ordering: bytes,
    supplement: int,
    vertical: bool,
    stream: Stream | None,
) -> Stream:
    """Generate metadata and PostScript together and commit both atomically."""
    if stream is not None and (
        not isinstance(stream, Stream)
        or stream._document is not document  # noqa: SLF001
        or not isinstance(stream.get("Type"), Name)
        or stream["Type"] != "CMap"
    ):
        msg = "stream must be a CMap stream in this document"
        raise ValueError(msg)
    if not isinstance(mapping, Mapping):
        msg = "mapping must map PDF character codes to CIDs"
        raise TypeError(msg)
    if not isinstance(name, str) or not name.isascii() or not name or any(ord(c) not in _NAME_BYTES for c in name):
        msg = "a CMap name must contain ASCII letters, digits, dots, underscores, plus or minus signs"
        raise ValueError(msg)
    if not isinstance(registry, bytes) or not isinstance(ordering, bytes) or not registry or not ordering:
        msg = "registry and ordering must be nonempty byte strings"
        raise ValueError(msg)
    if type(supplement) is not int or not 0 <= supplement <= 2**31 - 1 or type(vertical) is not bool:
        msg = "supplement must be a nonnegative PDF integer and vertical must be a boolean"
        raise ValueError(msg)
    core = document._core()  # noqa: SLF001
    operation = _core.begin_operation(core, "Write encoding CMap")
    try:
        operation.charge("memory_size", 1024 + 8 * (len(name) + len(registry) + len(ordering)))
        ranges, chars = _entries(mapping, operation)
        mode = int(vertical)
        info = {"Registry": registry, "Ordering": ordering, "Supplement": supplement}
        metadata = {"Type": Name("CMap"), "CMapName": Name(name), "CIDSystemInfo": info, "WMode": mode}
        body = b"\n".join(
            (
                b"/CIDInit /ProcSet findresource begin",
                b"12 dict begin",
                b"begincmap",
                b"/CIDSystemInfo << /Registry "
                + _hex(registry)
                + b" /Ordering "
                + _hex(ordering)
                + b" /Supplement "
                + str(supplement).encode()
                + b" >> def",
                b"/CMapName /" + name.encode("ascii") + b" def",
                b"/CMapType 1 def",
                b"/WMode " + str(mode).encode() + b" def",
                ranges,
                chars,
                b"endcmap",
                b"CMapName currentdict /CMap defineresource pop",
                b"end",
                b"end",
                b"",
            )
        )
        if stream is None:
            stream = document.objects[document.objects.add_stream(body, metadata)]
        else:
            stream.write(body)
            for key, value in metadata.items():
                stream[key] = value
            if "UseCMap" in stream:
                del stream["UseCMap"]
        _core.end_transaction(core, True)  # noqa: FBT003
    except BaseException:
        _core.abort_canvas(core, operation)
        raise
    return stream
