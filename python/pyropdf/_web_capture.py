"""Stored Web Capture content sets, source locations and name trees (ISO 32000-1, 14.10)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit, urlunsplit

from pyropdf import _core, typed
from pyropdf._attachments import _version
from pyropdf._collections import _MAX_TREE_DEPTH, _MAX_TREE_ENTRIES, _build_name_tree, _is_dict
from pyropdf._errors import LimitError, PdfError
from pyropdf._metadata import _require_version
from pyropdf._objects import Array, Dictionary, Stream, String

if TYPE_CHECKING:
    from pyropdf._document import Document
    from pyropdf._limit_codes import LimitCode

_Set = typed.WebCapturePageSet | typed.WebCaptureImageSet
_ID_BYTES = 16
_ENTRY_BYTES = 96  # A working entry holds its object handle and a collection slot.
_MAX_KEY_BYTES = 1_048_576
_COUNT: LimitCode = "object_count"
_DEPTH: LimitCode = "object_depth"
_SIZE: LimitCode = "object_parse_size"


@dataclass
class _Work:
    operation: _core._OperationBudget
    remaining: int = _MAX_TREE_ENTRIES
    key_bytes: int = _MAX_KEY_BYTES

    def key(self, value: bytes) -> None:
        self.key_bytes -= len(value)
        self.operation.charge("memory_size", len(value))
        if self.key_bytes < 0:
            msg = "Web Capture index keys and source URLs exceed one MiB"
            raise LimitError(_SIZE, msg)

    def charge(self, count: int = 1) -> None:
        self.remaining -= count
        self.operation.charge("memory_size", count * _ENTRY_BYTES)
        if self.remaining < 0:
            msg = "the Web Capture database exceeds the bounded number of nodes, entries and members"
            raise LimitError(_COUNT, msg)


def _set(value: object) -> _Set:
    if isinstance(value, Dictionary) and _is_dict(value):
        if value.get("S") == "SPS":
            return typed.WebCapturePageSet(value)
        if value.get("S") == "SIS":
            return typed.WebCaptureImageSet(value)
    msg = "a Web Capture index must contain an SPS or SIS content set"
    raise PdfError(msg)


def _items(value: object, work: _Work) -> tuple[object, ...]:
    if isinstance(value, Array):
        work.charge(len(value))
        return tuple(value)
    work.charge()
    return (value,)


def _sets(value: object, work: _Work) -> tuple[_Set, ...]:
    return tuple(_set(item) for item in _items(value, work))


@dataclass
class _Index:
    entries: dict[bytes, object] = field(default_factory=dict)
    root: Dictionary | None = None
    retired: list[int] = field(default_factory=list)
    private_children: bool = False


def _read_node(node: Dictionary, key: str, index: _Index, work: _Work) -> tuple[object, ...]:
    children = "Kids" in node
    values = node.get("Kids" if children else "Names")
    if not isinstance(values, Array) or (children and "Names" in node) or (not children and len(values) % 2):
        msg = f"the {key} name tree needs Kids or paired Names"
        raise PdfError(msg)
    if values.xref is not None:
        index.retired.append(values.xref)
    if children:
        return _items(values, work)
    work.charge(len(values) // 2)
    for offset in range(0, len(values), 2):
        name = values[offset]
        if not isinstance(name, String) or name in index.entries:
            msg = f"the {key} name tree has an invalid or repeated key"
            raise PdfError(msg)
        work.key(name)
        index.entries[bytes(name)] = values[offset + 1]
    return ()


def _read_index(names: object, key: str, work: _Work) -> _Index:
    index = _Index()
    if names is None:
        return index
    if not isinstance(names, Dictionary) or not _is_dict(names):
        msg = "the catalog Names entry must be a dictionary"
        raise PdfError(msg)
    if key not in names:
        return index
    root = names[key]
    pending = [(root, 0)]
    seen: set[Dictionary] = set()
    while pending:
        node, depth = pending.pop()
        work.charge()
        if not isinstance(node, Dictionary) or not _is_dict(node) or node in seen:
            msg = f"the {key} name tree contains a repeated or invalid node"
            raise PdfError(msg)
        if depth > _MAX_TREE_DEPTH:
            msg = f"the {key} name tree exceeds {_MAX_TREE_DEPTH} levels"
            raise LimitError(_DEPTH, msg)
        seen.add(node)
        if depth == 0:
            index.root = node
        else:
            work.charge(len(node))
            index.private_children |= any(name not in {"Kids", "Names", "Limits"} for name in node)
        if node.xref is not None:
            index.retired.append(node.xref)
        kids = _read_node(node, key, index, work)
        pending.extend((kid, depth + 1) for kid in kids)
    return index


def _canonical(url: str) -> bytes:
    if not isinstance(url, str) or not url.isascii() or not url.isprintable() or " " in url:
        msg = "a Web Capture source URL must be an absolute ASCII URL without spaces"
        raise ValueError(msg)
    if len(url) > _MAX_KEY_BYTES:
        msg = "a Web Capture URL exceeds one MiB"
        raise LimitError(_SIZE, msg)
    parts = urlsplit(url)
    if not parts.scheme:
        msg = "a Web Capture source URL must be absolute"
        raise ValueError(msg)
    if parts.scheme not in {"http", "ftp", "file"}:
        return url.encode("ascii")
    user, separator, host = parts.netloc.rpartition("@")
    if not parts.hostname and parts.scheme != "file":
        msg = "an HTTP or FTP Web Capture URL must have a host"
        raise ValueError(msg)
    authority = host.lower()
    if parts.scheme == "file" and authority == "localhost":
        authority = ""
    elif parts.port is not None and parts.port == {"http": 80, "ftp": 21}.get(parts.scheme):
        authority = authority.rsplit(":", 1)[0]
    return urlunsplit((parts.scheme, user + separator + authority, _url_path(parts.path), parts.query, "")).encode(
        "ascii"
    )


def _url_path(path: str) -> str:
    """Remove dot segments while retaining empty segments and the query's separate spelling."""
    segments: list[str] = []
    for segment in path.split("/"):
        if segment == "..":
            if len(segments) > 1:
                segments.pop()
        elif segment != ".":
            segments.append(segment)
    if path.endswith(("/.", "/..")):
        segments.append("")
    return "/".join(segments)


def _aliases(aliases: object, work: _Work) -> list[object]:
    urls: list[object] = []
    if isinstance(aliases, Dictionary) and _is_dict(aliases):
        urls.append(aliases.get("U"))
        chains = aliases.get("C")
        if chains is not None:
            if not isinstance(chains, Array):
                msg = "URL alias chains must be an array of arrays"
                raise ValueError(msg)
            for chain in _items(chains, work):
                if not isinstance(chain, Array):
                    msg = "a URL alias chain must be an array"
                    raise PdfError(msg)
                urls.extend(_items(chain, work))
    else:
        urls.append(aliases)
    return urls


def _source_keys(raw: object, work: _Work) -> set[bytes]:
    keys: set[bytes] = set()
    for source in _items(raw, work):
        if not isinstance(source, Dictionary) or not _is_dict(source):
            msg = "a content set needs one or more source information dictionaries"
            raise ValueError(msg)
        aliases = source.get("AU")
        urls = _aliases(aliases, work)
        for item in urls:
            if not isinstance(item, String):
                msg = "source URLs must be ASCII strings"
                raise PdfError(msg)
            work.key(item)
            keys.add(_canonical(item.decode("ascii")))
    if not keys:
        msg = "a content set needs at least one source URL"
        raise ValueError(msg)
    return keys


def _members(content: _Set, work: _Work) -> tuple[Dictionary, ...]:
    raw = content.obj.get("O")
    if not isinstance(raw, Array):
        msg = "the content set's O entry must be an array"
        raise PdfError(msg)
    image = isinstance(content, typed.WebCaptureImageSet)
    result: list[Dictionary] = []
    for member in _items(raw, work):
        if (
            not isinstance(member, Dictionary)
            or not member.is_indirect
            or (
                member.get("Subtype") != "Image" or not isinstance(member, Stream)
                if image
                else member.get("Type") != "Page" or not _is_dict(member)
            )
        ):
            msg = "the content set references a member of the wrong kind"
            raise PdfError(msg)
        result.append(member)
    if not result or len(set(result)) != len(result):
        msg = "a content set needs distinct member objects"
        raise PdfError(msg)
    if image:
        counts = content.obj.get("R")
        valid = (
            isinstance(counts, int) and not isinstance(counts, bool) and counts > 0
            if len(result) == 1
            else (
                isinstance(counts, Array)
                and len(counts) == len(result)
                and all(type(count) is int and count > 0 for count in _items(counts, work))
            )
        )
        if not valid:
            msg = "an image set needs one positive reference count per member"
            raise PdfError(msg)
    return tuple(result)


def _write_index(document: Document, names: Dictionary, key: str, index: _Index, work: _Work) -> None:
    if index.private_children:
        msg = f"editing the {key} index would discard private entries on a child name-tree node"
        raise PdfError(msg)
    extras: dict[str, object] = {}
    if index.root is not None:
        work.charge(len(index.root))
        extras = {name: value for name, value in index.root.items() if name not in {"Names", "Kids", "Limits"}}
    if index.entries or extras:
        root = _build_name_tree(document, sorted(index.entries.items()))
        root.update(extras)
        names[key] = root
    elif key in names:
        del names[key]


def _store_members(
    document: Document, content: _Set, members: tuple[Dictionary, ...], identifier: bytes, *, add: bool
) -> None:
    for member in members:
        if add:
            if "ID" in member and member["ID"] != identifier:
                msg = "the member already names another capture identifier"
                raise ValueError(msg)
            if isinstance(content, typed.WebCapturePageSet):
                number, generation = member.xref, member.generation
                assert number is not None  # noqa: S101 - _members requires an indirect object.
                assert generation is not None  # noqa: S101
                if _core.page_index(document._core(), number, generation) is None:  # noqa: SLF001
                    msg = "the set references a page outside the document's page tree"
                    raise ValueError(msg)
            member["ID"] = identifier
        elif member.get("ID") == identifier:
            del member["ID"]


def _plan_keys(
    index: _Index, keys: set[bytes], content: _Set, siblings: set[Dictionary], work: _Work
) -> tuple[bool, bool]:
    """Keep unmodified entries, including shared indirect arrays, exactly as stored."""
    found = changed = False
    for key, raw in list(index.entries.items()):
        values = [item.obj for item in _sets(raw, work)]
        siblings.update(value for value in values if value != content.obj)
        present = content.obj in values
        found |= present
        if present == (key in keys):
            continue
        changed = True
        kept = [value for value in values if value != content.obj] if present else [*values, content.obj]
        if kept:
            index.entries[key] = kept[0] if len(kept) == 1 else kept
        else:
            del index.entries[key]
    for key in keys.difference(index.entries):
        changed = True
        index.entries[key] = content.obj
    return found, changed


def _store_indexes(document: Document, names: object, changed: dict[str, _Index], work: _Work) -> None:
    if not changed:
        return
    catalog = document.catalog
    # Detach even an indirect Names dictionary shared by an unrelated catalog entry.
    if isinstance(names, Dictionary):
        work.charge(len(names))
        detached = document.objects[document.objects.add(dict(names))]
    else:
        detached = document.objects[document.objects.add({})]
    for key, index in changed.items():
        _write_index(document, detached, key, index, work)
    if len(detached):
        catalog["Names"] = detached
    else:
        del catalog["Names"]
    retired = [number for index in changed.values() for number in index.retired]
    if isinstance(names, Dictionary) and names.xref is not None:
        retired.append(names.xref)
    if not len(detached) and detached.xref is not None:
        retired.append(detached.xref)
    if retired:
        _core.object_free_unreferenced(document._core(), retired)  # noqa: SLF001


class WebCapture:
    """The stored Web Capture database. Index lookups return writable Arlington content-set views."""

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        self._document = document

    @property
    def info(self) -> typed.WebCaptureInfo | None:
        """Return the catalog's capture information, including its stored command dictionaries."""
        obj = self._document.catalog.get("SpiderInfo")
        return typed.WebCaptureInfo(obj) if isinstance(obj, Dictionary) and _is_dict(obj) else None

    def _lookup(self, index: str, key: bytes) -> tuple[_Set, ...]:
        def read(operation: _core._OperationBudget) -> tuple[_Set, ...]:
            work = _Work(operation)
            values = _read_index(self._document.catalog.get("Names"), index, work).entries
            return _sets(values[key], work) if key in values else ()

        return _core.read_operation(self._document._core(), read)  # noqa: SLF001

    def by_id(self, identifier: bytes, /) -> tuple[typed.WebCapturePageSet | typed.WebCaptureImageSet, ...]:
        """Resolve a binary source identifier, keeping the order of sets sharing the key."""
        if not isinstance(identifier, bytes):
            msg = "a capture identifier must be bytes"
            raise TypeError(msg)
        return self._lookup("IDS", identifier)

    def by_url(self, url: str, /) -> tuple[typed.WebCapturePageSet | typed.WebCaptureImageSet, ...]:
        """Resolve an absolute source URL through the stored URLS index."""
        return self._lookup("URLS", _canonical(url))

    def add(self, content: typed.WebCapturePageSet | typed.WebCaptureImageSet, /) -> None:
        """Register an indirect page or image set, its sources and its members in a PDF before 2.0.

        Construct the set and its source dictionaries with the object API. Its ID is the 16-byte
        source digest defined in ISO 32000-1, 14.10.3.3. Image sets supply their page reference counts.
        """
        self._change(content, "add")

    def remove(self, content: typed.WebCapturePageSet | typed.WebCaptureImageSet, /) -> None:
        """Remove the set from both indexes and clear its members' matching parent identifiers."""
        self._change(content, "remove")

    def set_sources(
        self,
        content: typed.WebCapturePageSet | typed.WebCaptureImageSet,
        sources: typed.SourceInformation | tuple[typed.SourceInformation, ...],
        /,
    ) -> None:
        """Replace a registered set's source information and update every URL index entry atomically.

        Pass a source dictionary's Arlington view or a tuple of those views. Command references,
        dates, URL aliases and private entries are retained as supplied.
        """
        if isinstance(sources, typed.SourceInformation):
            raw: object = sources.obj
        elif (
            isinstance(sources, tuple)
            and len(sources) <= _MAX_TREE_ENTRIES
            and all(isinstance(source, typed.SourceInformation) for source in sources)
        ):
            raw = [source.obj for source in sources]
        else:
            msg = "sources must be a SourceInformation view or a bounded tuple of those views"
            raise TypeError(msg)
        self._change(content, "sources", raw)

    def _change(self, content: _Set, action: Literal["add", "remove", "sources"], source: object = None) -> None:
        document = self._document
        if not isinstance(content, (typed.WebCapturePageSet, typed.WebCaptureImageSet)) or (
            not content.obj.is_indirect
            or content.obj._document is not document  # noqa: SLF001 - compare document identity.
            or type(_set(content.obj)) is not type(content)
        ):
            msg = "the set must be an indirect content set of this document"
            raise ValueError(msg)
        with document.transaction("edit Web Capture"):
            if action == "add" and _version(document) >= (2, 0):
                msg = "creating Web Capture is deprecated in PDF 2.0"
                raise ValueError(msg)
            if action == "sources":
                content.obj["SI"] = source
            _core.read_operation(
                document._core(),  # noqa: SLF001
                lambda operation: self._update_indexes(content, action, _Work(operation)),
            )

    def _update_indexes(self, content: _Set, action: Literal["add", "remove", "sources"], work: _Work) -> None:
        document = self._document
        catalog = document.catalog
        names = catalog.get("Names")
        identifier = content.obj.get("ID")
        if not isinstance(identifier, String) or len(identifier) != _ID_BYTES:
            msg = "a Web Capture source identifier must contain 16 bytes"
            raise ValueError(msg)
        desired = (
            {}
            if action == "remove"
            else {"IDS": {bytes(identifier)}, "URLS": _source_keys(content.obj.get("SI"), work)}
        )
        changed: dict[str, _Index] = {}
        siblings: set[Dictionary] = set()
        registered = False
        for key in ("IDS", "URLS"):
            index = _read_index(names, key, work)
            present, rewrite = _plan_keys(index, desired.get(key, set()), content, siblings, work)
            registered |= present
            if rewrite:
                changed[key] = index
        if (action == "add") == registered:
            msg = "the content set is already registered" if registered else "the content set is not registered"
            raise ValueError(msg)
        if action != "sources":
            members = _members(content, work)
            owned = set(members)
            if any(owned.intersection(_members(_set(sibling), work)) for sibling in siblings):
                msg = "a page or image cannot belong to more than one capture set"
                raise ValueError(msg)
            _store_members(document, content, members, identifier, add=action == "add")
        if action == "add":
            _require_version(document, catalog, (1, 3))
            if "SpiderInfo" not in catalog:
                catalog["SpiderInfo"] = {"V": 1}
        _store_indexes(document, names, changed, work)
        if action == "remove":
            _prune_empty_info(document)


def _prune_empty_info(document: Document) -> None:
    if _has_capture_indexes(document):
        return
    catalog = document.catalog
    info = catalog.get("SpiderInfo")
    if isinstance(info, Dictionary) and len(info) == 1 and info.get("V") == 1:
        del catalog["SpiderInfo"]
        if info.xref is not None:
            _core.object_free_unreferenced(document._core(), [info.xref])  # noqa: SLF001


def _references_image(page: Dictionary, images: set[Dictionary], work: _Work) -> bool:
    """Follow stored page resources (including inherited ones) and annotation appearances."""
    pending: list[object] = [page.get("Annots"), page.get("Thumb")]
    parent = page
    ancestors: set[Dictionary] = set()
    while "Resources" not in parent and isinstance(parent.get("Parent"), Dictionary):
        work.charge()
        if parent in ancestors:
            msg = "a page with captured images has cyclic resource inheritance"
            raise PdfError(msg)
        ancestors.add(parent)
        parent = parent["Parent"]
    pending.append(parent.get("Resources"))
    seen: set[Dictionary | Array] = set()
    while pending:
        value = pending.pop()
        if not isinstance(value, (Dictionary, Array)) or value in seen:
            continue
        seen.add(value)
        if isinstance(value, Dictionary):
            if value in images:
                return True
            if value.get("Type") in ("Page", "Pages", "Catalog"):
                continue
            work.charge(len(value))
            pending.extend(value.values())
        else:
            work.charge(len(value))
            pending.extend(value)
    return False


def _has_capture_indexes(document: Document) -> bool:
    names = document.catalog.get("Names")
    return isinstance(names, Dictionary) and ("IDS" in names or "URLS" in names)


def _guard_capture_pages(document: Document, pages: set[tuple[int, int]]) -> None:
    """Refuse to change indexed memberships or image page counts before a structural edit."""
    if pages:
        _core.read_operation(
            document._core(),  # noqa: SLF001
            lambda operation: _check_page_memberships(document, pages, _Work(operation)),
        )


def _check_page_memberships(document: Document, pages: set[tuple[int, int]], work: _Work) -> None:
    names = document.catalog.get("Names")
    seen: set[Dictionary] = set()
    images: set[Dictionary] = set()
    for key in ("IDS", "URLS"):
        for raw in _read_index(names, key, work).entries.values():
            for content in _sets(raw, work):
                if content.obj in seen:
                    continue
                seen.add(content.obj)
                for member in _members(content, work):
                    if isinstance(content, typed.WebCaptureImageSet):
                        images.add(member)
                    elif (member.xref, member.generation) in pages:
                        msg = "remove the Web Capture page set before deleting or copying one of its pages"
                        raise PdfError(msg)
    if images and any(_references_image(Dictionary(document, pair), images, work) for pair in pages):
        msg = "remove the Web Capture image set before changing a page that references its images"
        raise PdfError(msg)
