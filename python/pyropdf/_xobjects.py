"""Shared Form XObject, inline-image filter, page-resource, and appearance-editing helpers."""

from __future__ import annotations

import base64
import binascii
import zlib
from typing import TYPE_CHECKING

from pyropdf._errors import PdfError
from pyropdf._objects import Dictionary, Name, Stream
from pyropdf._operations import Operations

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pyropdf._document import Document
    from pyropdf._page import Page

#: Nesting of Form XObjects followed from a page or appearance (F6).
_MAX_FORM_DEPTH = 16
#: Drawings of Form XObjects that one call reads; the others are reported (F6).
_MAX_FORMS = 4096
#: Pixels of an inline image that a rewrite or recoloring reads (F6).
_MAX_INLINE_PIXELS = 1 << 20
#: Ancestors followed up the page tree when resolving inherited ``/Resources`` (F6).
_MAX_RESOURCE_ANCESTORS = 64
#: Decoded-byte cap per inline-image Flate stream (up to four bytes per pixel).
_MAX_INLINE_FLATE_BYTES = _MAX_INLINE_PIXELS * 4
#: Stream dictionary entries that a copied stream sets from its new data.
_STREAM_KEYS = frozenset({"Length", "Filter", "DecodeParms", "DL"})
#: Inline image filter names and abbreviations that recoloring and image rewriting decode.
_INLINE_FILTERS = {
    "AHx": "hex",
    "ASCIIHexDecode": "hex",
    "A85": "a85",
    "ASCII85Decode": "a85",
    "Fl": "flate",
    "FlateDecode": "flate",
}


def _inline_data(filters: object, data: bytes) -> bytes | None:
    """Decode an inline image's ``data`` through ``filters``, or return ``None`` when unsupported or invalid."""
    names = [filters] if isinstance(filters, Name) else list(filters) if isinstance(filters, tuple) else []
    if filters is not None and not names:
        return None
    try:
        for name in names:
            kind = _INLINE_FILTERS.get(str(name))
            if kind == "hex":
                text = bytes(byte for byte in data.split(b">", 1)[0] if byte not in b" \t\r\n\f\x00")
                data = binascii.unhexlify(text + b"0" * (len(text) % 2))
            elif kind == "a85":
                data = base64.a85decode(data.split(b"~>", 1)[0], ignorechars=b" \t\r\n\f\x00")
            elif kind == "flate":
                data = zlib.decompressobj().decompress(data, _MAX_INLINE_FLATE_BYTES)
            else:
                return None
    except (ValueError, zlib.error):
        return None
    return data


def _resources(page: Page) -> Dictionary | None:
    """Return ``page``'s ``/Resources`` dictionary, either its own or inherited up to 64 ``/Parent`` nodes."""
    node: object = page.obj
    for _ in range(_MAX_RESOURCE_ANCESTORS):
        if not isinstance(node, Dictionary):
            return None
        resources = node.get("Resources")
        if isinstance(resources, Dictionary):
            return resources
        node = node.get("Parent")
    return None


def _sub(resources: Dictionary | None, key: str) -> Dictionary | None:
    """Return dictionary entry ``key`` of ``resources``, or ``None`` when absent or not a dictionary."""
    value = resources.get(key) if resources is not None else None
    return value if isinstance(value, Dictionary) else None


def _parse_form(form: Stream) -> Operations | None:
    """Parse ``form``'s content stream into :class:`Operations`, or return ``None`` when unreadable."""
    try:
        return Operations.parse(form.read())
    except (PdfError, ValueError):
        return None


def _clone_form_resources(resources: Dictionary | None, drawn: Mapping[str, Stream]) -> dict[str, object]:
    """Return a copy-on-write ``/Resources`` mapping with ``drawn`` merged into ``/XObject``."""
    own: dict[str, object] = dict(resources.items()) if resources is not None else {}
    xobjects = own.get("XObject")
    own["XObject"] = {**(dict(xobjects.items()) if isinstance(xobjects, Dictionary) else {}), **drawn}
    return own


def _copy_form(
    document: Document,
    source: Stream,
    data: bytes,
    resources: Dictionary | None = None,
    drawn: Mapping[str, Stream] | None = None,
) -> int:
    """Add a new stream copying ``source``'s non-stream entries with ``data`` and copy-on-write ``drawn`` XObjects."""
    dictionary: dict[str, object] = {key: value for key, value in source.items() if key not in _STREAM_KEYS}
    if drawn:
        dictionary["Resources"] = _clone_form_resources(resources, drawn)
    return document.objects.add_stream(data, dictionary)


def _repoint_page(page: Page, drawn: Mapping[str, Stream]) -> None:
    """Update ``page``'s ``/Resources["XObject"]`` with ``drawn`` via copy-on-write dictionaries."""
    own = page.obj.get("Resources")
    if own is None or own.is_indirect:
        inherited = own if own is not None else _resources(page)
        page.obj["Resources"] = {} if inherited is None else dict(inherited.items())
    resources = page.obj["Resources"]
    xobjects = resources.get("XObject")
    if xobjects is None or xobjects.is_indirect:
        resources["XObject"] = {} if xobjects is None else dict(xobjects.items())
    for name, value in drawn.items():
        resources["XObject"][name] = value


def _appearance_streams(
    annotation: Dictionary,
    keys: tuple[str, ...] = ("N", "R", "D"),
) -> list[tuple[str, str | None, Stream]]:
    """Return ``(key, state, stream)`` for each appearance stream of ``annotation`` under ``keys``."""
    appearances = annotation.get("AP")
    if not isinstance(appearances, Dictionary):
        return []
    found: list[tuple[str, str | None, Stream]] = []
    for key in keys:
        entry = appearances.get(key)
        if isinstance(entry, Stream):
            found.append((key, None, entry))
        elif isinstance(entry, Dictionary):
            found.extend((key, str(state), value) for state, value in entry.items() if isinstance(value, Stream))
    return found


def _set_appearance(annotation: Dictionary, key: str, state: str | None, appearance: Stream) -> None:
    """Point ``annotation``'s ``/AP`` entry ``key`` (and optional ``state``) at ``appearance`` via copy-on-write."""
    appearances = annotation["AP"]
    if appearances.is_indirect:
        annotation["AP"] = dict(appearances.items())
        appearances = annotation["AP"]
    if state is None:
        appearances[key] = appearance
        return
    states = appearances[key]
    if states.is_indirect:
        appearances[key] = dict(states.items())
        states = appearances[key]
    states[state] = appearance
