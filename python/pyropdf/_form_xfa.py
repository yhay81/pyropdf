"""The existing XFA resource of an AcroForm (ISO 32000-1 §12.7.8, PDF 2.0 Annex K)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from xml.parsers import expat

from pyropdf import _core
from pyropdf._errors import MalformedPdfError
from pyropdf._objects import Array, Dictionary, Stream, String, _unlocked

if TYPE_CHECKING:
    from pyropdf._document import Document

_XDP = "http://ns.adobe.com/xdp/ xdp"
_CHUNK = 65536


@dataclass(frozen=True, slots=True)
class _Packet:
    name: str
    begin: int
    end: int


def _xml_unit(data: bytes) -> tuple[int, int]:
    """Return the byte width and ASCII byte offset for UTF-8 or either UTF-16 byte order."""
    if data.startswith((b"\xfe\xff", b"\0<")):
        return 2, 1
    if data.startswith((b"\xff\xfe", b"<\0")):
        return 2, 0
    return 1, 0


def _tag_end(data: bytes, start: int, unit: tuple[int, int], budget: _core._OperationBudget) -> tuple[int, bool]:
    """Find the end of a parsed start/end tag without treating a quoted '>' as its delimiter."""
    width, offset = unit
    quote = 0
    last = 0
    for i in range(start + offset, len(data), width):
        if (i - start - offset) % _CHUNK == 0:
            budget.poll()
        char = data[i]
        if width > 1 and data[i + (1 if offset == 0 else -1)]:
            char = 0
        if quote:
            if char == quote:
                quote = 0
        elif char in (34, 39):
            quote = char
        elif char == ord(">"):
            return i - offset + width, last == ord("/")
        last = char
    msg = "unterminated XFA element"
    raise MalformedPdfError(msg)


def _packets(data: bytes, budget: _core._OperationBudget) -> list[_Packet]:
    budget.charge("memory_size", len(data) * 2)
    parser = expat.ParserCreate(namespace_separator=" ", intern=None)
    depth = begin = start_end = 0
    empty = False
    indexed: list[_Packet] = []
    unit = _xml_unit(data)

    def start(name: str, _attributes: dict[str, str]) -> None:
        nonlocal depth, begin, start_end, empty
        depth += 1
        budget.check("object_depth", depth)
        budget.poll()
        if depth == 1:
            if name != _XDP:
                msg = "the XFA stream must contain an xdp:xdp document"
                raise MalformedPdfError(msg)
        elif depth == 2:  # noqa: PLR2004 - direct children of the XDP root are packets.
            begin = parser.CurrentByteIndex
            start_end, empty = _tag_end(data, begin, unit, budget)

    def end(name: str) -> None:
        nonlocal depth
        if depth == 2:  # noqa: PLR2004
            budget.charge("memory_size", 160 + 4 * len(name))
            end = start_end if empty else _tag_end(data, parser.CurrentByteIndex, unit, budget)[0]
            indexed.append(_Packet(name, begin, end))
        depth -= 1

    def doctype(*_args: object) -> None:
        msg = "an XFA document type declaration is not supported"
        raise MalformedPdfError(msg)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.StartDoctypeDeclHandler = doctype
    try:
        for i in range(0, len(data), _CHUNK):
            budget.poll()
            parser.Parse(data[i : i + _CHUNK], False)  # noqa: FBT003
        parser.Parse(b"", True)  # noqa: FBT003
    except expat.ExpatError as error:
        msg = f"invalid XFA XML: {error}"
        raise MalformedPdfError(msg) from error
    return indexed


def _resource(doc: Document) -> tuple[Dictionary | None, Array | Stream | None]:
    form = doc.catalog.get("AcroForm")
    if form is None:
        return None, None
    if not isinstance(form, Dictionary) or isinstance(form, Stream):
        msg = "/AcroForm must be a dictionary"
        raise MalformedPdfError(msg)
    resource = form.get("XFA")
    if resource is not None and not isinstance(resource, (Array, Stream)):
        msg = "/AcroForm /XFA must be a stream or a packet array"
        raise MalformedPdfError(msg)
    return form, resource


def _pairs(resource: Array, budget: _core._OperationBudget) -> list[tuple[String, Stream]]:
    count = len(resource)
    if count % 2:
        msg = "a packet name in the /XFA array has no corresponding stream"
        raise MalformedPdfError(msg)
    budget.charge("memory_size", count * 80)
    pairs: list[tuple[String, Stream]] = []
    for i in range(0, count, 2):
        budget.poll()
        name, stream = resource[i], resource[i + 1]
        if not isinstance(name, String) or not isinstance(stream, Stream) or stream.xref is None:
            msg = "an /XFA packet must be a string and an indirect stream"
            raise MalformedPdfError(msg)
        budget.charge("memory_size", len(name) * 4)
        pairs.append((name, stream))
    return pairs


def _data(stream: Stream, budget: _core._OperationBudget) -> bytes:
    data = stream.read(max_size=budget.limit("stream_data_size"))
    budget.charge("memory_size", len(data))
    return data


def names(doc: Document) -> tuple[str, ...]:
    def read(budget: _core._OperationBudget) -> tuple[str, ...]:
        _, resource = _resource(doc)
        if resource is None:
            return ()
        if isinstance(resource, Array):
            return tuple(key.text for key, _ in _pairs(resource, budget))
        return tuple(p.name.rsplit(" ", 1)[-1] for p in _packets(_data(resource, budget), budget))

    return _core.read_operation(_unlocked(doc), read)


def _index(index: int) -> int:
    if isinstance(index, bool) or not isinstance(index, int):
        msg = "an XFA packet index must be an integer"
        raise TypeError(msg)
    return index


def read_packet(doc: Document, index: int) -> bytes:
    pos = _index(index)

    def read(budget: _core._OperationBudget) -> bytes:
        _, resource = _resource(doc)
        if resource is None:
            raise IndexError(pos)
        if isinstance(resource, Array):
            return _data(_pairs(resource, budget)[pos][1], budget)
        data = _data(resource, budget)
        packet = _packets(data, budget)[pos]
        budget.charge("memory_size", packet.end - packet.begin)
        return data[packet.begin : packet.end]

    return _core.read_operation(_unlocked(doc), read)


@dataclass(slots=True)
class _Edit:
    form: Dictionary
    old: Stream
    data: bytes
    pairs: list[tuple[String, Stream]] | None = None
    old_array: int | None = None
    index: int = 0


def _array_edit(
    form: Dictionary, resource: Array, pos: int, replacement: bytes | None, budget: _core._OperationBudget
) -> _Edit:
    pairs = _pairs(resource, budget)
    _, stream = pairs[pos]
    pos %= len(pairs)
    # The start/end wrapper streams supply namespaces used by the interior packets.
    # Validate each edit in the original context and retain the other streams verbatim.
    parts: list[bytes] = []
    total = 0
    for _, item in pairs:
        part = _data(item, budget)
        total += len(part)
        budget.check("stream_data_size", total)
        parts.append(part)
    budget.charge("memory_size", total)
    original = _packets(b"".join(parts), budget)
    changed = replacement or b""
    total += len(changed) - len(parts[pos])
    budget.check("stream_data_size", total)
    budget.charge("memory_size", total)
    parts[pos] = changed
    updated = _packets(b"".join(parts), budget)
    if replacement is not None and [p.name for p in original] != [p.name for p in updated]:
        msg = "the replacement must retain the original XFA packet names and order"
        raise ValueError(msg)
    return _Edit(form, stream, changed, pairs, resource.xref, pos)


def edit_packet(doc: Document, index: int, replacement: bytes | None) -> None:
    pos = _index(index)

    def prepare(budget: _core._OperationBudget) -> _Edit:
        form, resource = _resource(doc)
        if form is None or resource is None:
            raise IndexError(pos)
        if replacement is not None:
            budget.check("stream_data_size", len(replacement))
        if isinstance(resource, Array):
            return _array_edit(form, resource, pos, replacement, budget)
        original = _data(resource, budget)
        packets = _packets(original, budget)
        packet = packets[pos]
        data = replacement or b""
        total = len(original) - (packet.end - packet.begin) + len(data)
        budget.check("stream_data_size", total)
        budget.charge("memory_size", total * 2)
        updated = b"".join((original[: packet.begin], data, original[packet.end :]))
        check = _packets(updated, budget)
        if replacement is not None and (
            len(check) != len(packets) or check[pos] != _Packet(packet.name, packet.begin, packet.begin + len(data))
        ):
            msg = "the replacement must be one complete XFA packet with the original expanded element name"
            raise ValueError(msg)
        return _Edit(form, resource, updated)

    with doc.transaction("edit an existing XFA packet"):
        edit = _core.read_operation(_unlocked(doc), prepare)
        retired = [n for n in (edit.old_array, edit.old.xref) if n is not None]
        new = None
        if edit.pairs is None or replacement is not None:
            dictionary = {k: edit.old[k] for k in edit.old if k not in ("Length", "Filter", "DecodeParms")}
            new = doc.objects[doc.objects.add_stream(edit.data, dictionary)]
        if edit.pairs is None:
            edit.form["XFA"] = new
        else:
            entries: list[object] = []
            for i, (key, old) in enumerate(edit.pairs):
                if i != edit.index or new is not None:
                    entries.extend((key, new if i == edit.index else old))
            edit.form["XFA"] = doc.objects[doc.objects.add(entries)] if edit.old_array is not None else entries
        _core.object_free_nominated(_unlocked(doc), retired)


def remove(doc: Document) -> bool:
    def collect(budget: _core._OperationBudget) -> tuple[Dictionary | None, list[int]]:
        form = doc.catalog.get("AcroForm")
        if not isinstance(form, Dictionary) or isinstance(form, Stream) or "XFA" not in form:
            return None, []
        resource = form["XFA"]
        candidates: list[int] = []
        if isinstance(resource, (Array, Stream)) and resource.xref is not None:
            candidates.append(resource.xref)
        if isinstance(resource, Array):
            budget.charge("memory_size", len(resource) * 32)
            for item in resource:
                budget.poll()
                if isinstance(item, Stream) and item.xref is not None:
                    candidates.append(item.xref)
        return form, candidates

    with doc.transaction("remove the XFA resource"):
        form, candidates = _core.read_operation(_unlocked(doc), collect)
        if form is None:
            return False
        del form["XFA"]
        _core.object_free_nominated(_unlocked(doc), candidates)
        return True
