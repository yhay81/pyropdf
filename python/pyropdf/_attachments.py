"""Document attachments: the ``Document.attachments`` mapping and its handles.

The attachments are the entries of the catalog's ``/EmbeddedFiles`` name tree
(ISO 32000-2, 7.11.4). Every access walks the tree within its bounds: 32
levels and 4,096 entries. Adding and deleting write the tree again as one
node whose ``/Names`` holds the entries sorted, as pylopdf does.

A document that claims PDF/A-3 or PDF/A-4 requires each embedded file to be
an associated file with a MIME type (ISO 19005-3 section 6.8, ISO 19005-4
section 6.9): adding writes them, and deleting removes the file from the
catalog's ``/AF``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple

from pyropdf import _core
from pyropdf._errors import LimitError, PdfError, StaleObjectError
from pyropdf._limits import _validate_optional_positive_int
from pyropdf._objects import Array, Dictionary, Name, Stream, String, _encode_text_string
from pyropdf._profiles import pdfa_claim

if TYPE_CHECKING:
    from pyropdf._document import Document

_DEFAULT_MAX_EMBEDDED_FILE_SIZE = 64 * 1024 * 1024
_MAX_INPUT_TEXT_SIZE = 1024 * 1024
_MAX_TREE_DEPTH = 32
_MAX_TREE_ENTRIES = 4096
_MAX_INLINE_OBJECTS = 4096
#: The longest date string that a file's ``created`` and ``modified`` take.
_MAX_DATE_SIZE = 64


class _Entry(NamedTuple):
    """One entry of the name tree: its key as stored, as text, and its file specification."""

    key: bytes
    name: str
    filespec: Any


def _validate_text_size(texts: list[str], label: str) -> None:
    """Refuse texts that are longer together in UTF-8 than 1 MiB."""
    size = 0
    for text in texts:
        # Every character needs at least one UTF-8 byte. Reject an oversized
        # input before allocating its encoded copy.
        if str.__len__(text) > _MAX_INPUT_TEXT_SIZE - size:
            code = "embedded_file_input_size"
            msg = f"the {label} is longer than the {_MAX_INPUT_TEXT_SIZE}-byte limit in UTF-8"
            raise LimitError(code, msg)
        size += len(str.encode(text, "utf-8", "surrogatepass"))
        if size > _MAX_INPUT_TEXT_SIZE:
            code = "embedded_file_input_size"
            msg = f"the {label} is longer than the {_MAX_INPUT_TEXT_SIZE}-byte limit in UTF-8"
            raise LimitError(code, msg)


def _validate_name(name: object) -> str:
    """Check a lookup name's type and its 1 MiB bound before walking the tree."""
    if not isinstance(name, str):
        msg = f"name must be a string: {type(name).__name__}"
        raise TypeError(msg)
    _validate_text_size([name], "attachment lookup name")
    return str.__str__(name)


def _validate_data(data: object, max_size: int | None) -> bytes:
    """Check attachment data against ``max_size`` before copying it.

    The data is read through the buffer protocol, which a ``bytes`` subclass
    cannot override: its ``__len__`` and ``__bytes__`` decide neither the
    size checked nor the bytes written.
    """
    _validate_optional_positive_int("max_size", max_size)
    try:
        view = memoryview(data)  # ty: ignore[invalid-argument-type]
    except TypeError:
        msg = f"data must be a bytes-like object: {type(data).__name__}"
        raise TypeError(msg) from None
    with view:
        if max_size is not None and view.nbytes > max_size:
            code = "embedded_file_size"
            msg = f"attachment input is {view.nbytes} bytes, exceeding the {max_size}-byte limit"
            raise LimitError(code, msg)
        return view.tobytes()


#: The file fields that :meth:`Attachment._edit_file` takes.
_FILE_FIELDS = ("filename", "description", "created", "modified")


def _is_date(value: str) -> bool:
    """Return whether ``value`` is a PDF date string within its bound: ``D:``, then printable ASCII."""
    if not len("D:") < str.__len__(value) <= _MAX_DATE_SIZE:
        return False
    return str.startswith(value, "D:") and str.isascii(value) and str.isprintable(value)


def _validate_date(value: object, field: str) -> str:
    """Check a date that a file field takes, raising :class:`TypeError` or :class:`ValueError`."""
    if not isinstance(value, str):
        msg = f"{field} must be a PDF date string such as 'D:20260101120000Z': {type(value).__name__}"
        raise TypeError(msg)
    if not _is_date(value):
        msg = f"{field} must be a PDF date string of up to {_MAX_DATE_SIZE} printable ASCII characters"
        raise ValueError(msg)
    return value


def _validate_text(value: object, field: str) -> str:
    """Check a file name or description: a string within the 1 MiB bound."""
    if not isinstance(value, str):
        msg = f"{field} must be a str: {type(value).__name__}"
        raise TypeError(msg)
    _validate_text_size([value], f"attachment {field}")
    return str.__str__(value)


def _check_edit(edit: object) -> dict[str, str | None]:
    """Check an edit of file fields as a whole, raising :class:`PdfError`, before anything is written.

    The setters check each value first; this repeats the checks for any
    caller, as pylopdf's core ``embfile_edit`` does. ``None`` removes a
    field, except the file name, which a file specification keeps.
    """
    if not isinstance(edit, dict):
        msg = f"a file edit is a dict: {type(edit).__name__}"
        raise PdfError(msg)
    checked: dict[str, str | None] = {}
    texts: list[str] = []
    for key, value in edit.items():
        if key not in _FILE_FIELDS:
            msg = f"unknown file field: {key!r}"
            raise PdfError(msg)
        if value is None:
            if key == "filename":
                msg = "an embedded file keeps a file name"
                raise PdfError(msg)
        elif not isinstance(value, str):
            msg = f"the file field {key!r} must be a str or None: {type(value).__name__}"
            raise PdfError(msg)
        elif key in ("created", "modified"):
            if not _is_date(value):
                msg = f"{key} must be a PDF date string of up to {_MAX_DATE_SIZE} printable ASCII characters"
                raise PdfError(msg)
        elif key == "filename" and str.__len__(value) == 0:
            msg = "a file name cannot be empty"
            raise PdfError(msg)
        else:
            texts.append(value)
        checked[key] = value
    _validate_text_size(texts, "file name and description")
    # The object writer calls string methods while encoding.  Use exactly
    # the contents just checked, without invoking subclass overrides.
    return {key: str.__str__(value) if value is not None else None for key, value in checked.items()}


def _now() -> str:
    """Return the current time as a PDF date string in UTC."""
    return datetime.now(UTC).strftime("D:%Y%m%d%H%M%SZ")


def _text(value: object) -> str | None:
    """Return a text string's text, or ``None`` for another value."""
    return value.text if isinstance(value, String) else None


def _pairs(names: object) -> Iterator[_Entry]:
    """Return the entries of a node's ``/Names``, skipping a key that is not a string."""
    items = list(names) if isinstance(names, Array) else []
    for key, filespec in zip(items[::2], items[1::2], strict=False):
        if isinstance(key, String):
            yield _Entry(bytes(key), key.text, filespec)


def _walk(root: object) -> list[_Entry]:
    """Return the entries of the name tree at ``root`` in tree order, visiting each node once."""
    entries: list[_Entry] = []
    seen: set[int | None] = set()
    stack: list[tuple[object, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if not isinstance(node, Dictionary) or (node.is_indirect and node.xref in seen):
            continue
        if depth > _MAX_TREE_DEPTH:
            msg = f"the EmbeddedFiles name tree exceeds the {_MAX_TREE_DEPTH}-level safety limit"
            raise PdfError(msg)
        if node.is_indirect:
            seen.add(node.xref)
        for entry in _pairs(node.get("Names")):
            entries.append(entry)
            if len(entries) > _MAX_TREE_ENTRIES:
                msg = f"the EmbeddedFiles name tree exceeds the {_MAX_TREE_ENTRIES}-entry safety limit"
                raise PdfError(msg)
        kids = node.get("Kids")
        if isinstance(kids, Array):
            stack.extend((kid, depth + 1) for kid in reversed(list(kids)))
    return entries


def _check_inline(value: object) -> None:
    """Refuse a direct ``value`` whose copy would copy more than 4,096 values."""
    budget = _MAX_INLINE_OBJECTS
    pending = [value]
    while pending:
        item = pending.pop()
        budget -= 1
        if budget < 0:
            msg = f"an inline file specification exceeds the {_MAX_INLINE_OBJECTS}-object safety limit"
            raise PdfError(msg)
        if isinstance(item, Dictionary) and not item.is_indirect:
            pending.extend(item.values())
        elif isinstance(item, Array) and not item.is_indirect:
            pending.extend(item)


#: The version that a document without a header is held to, as pyropdf writes it.
_DEFAULT_VERSION = (1, 7)
#: The versions that add /Names/EmbeddedFiles, /Desc, and /UF.
_EMBEDDED_FILES_VERSION = (1, 4)
_DESCRIPTION_VERSION = (1, 6)
_UF_VERSION = (1, 7)
#: The PDF/A parts whose embedded files are associated files with a MIME type.
_ASSOCIATED_PARTS = (3, 4)
#: The MIME type of an embedded file whose type is not known.
_UNKNOWN_MIME = "application/octet-stream"


def _parse_version(text: str) -> tuple[int, int] | None:
    major, _, minor = text.partition(".")
    return (int(major), int(minor)) if major.isdigit() and minor.isdigit() else None


def _version(document: Document) -> tuple[int, int]:
    """Return the document's version: the later of its header and the catalog's ``/Version``."""
    header = _core.header_version(document._core())  # noqa: SLF001
    version = (_parse_version(header) if header is not None else None) or _DEFAULT_VERSION
    declared = document.catalog.get("Version")
    if isinstance(declared, Name):
        version = max(version, _parse_version(str(declared)) or version)
    return version


def _require(document: Document, minimum: tuple[int, int]) -> tuple[int, int]:
    """Raise the catalog's ``/Version`` to ``minimum`` when the document is older, and return its version."""
    version = _version(document)
    if version < minimum:
        document.catalog["Version"] = Name(f"{minimum[0]}.{minimum[1]}")
        return minimum
    return version


def _file(filespec: object) -> Stream | None:
    """Return the embedded file stream of a file specification, if it has one."""
    if not isinstance(filespec, Dictionary):
        return None
    files = filespec.get("EF")
    if not isinstance(files, Dictionary):
        return None
    for key in ("F", "UF"):
        stream = files.get(key)
        if isinstance(stream, Stream):
            return stream
    return None


def _objects(filespec: object) -> list[int]:
    """Return the objects of a file specification and of the embedded files that its ``/EF`` holds."""
    if not isinstance(filespec, Dictionary):
        return []
    numbers = [] if filespec.xref is None else [filespec.xref]
    files = filespec.get("EF")
    if isinstance(files, Dictionary):
        numbers.extend(stream.xref for key in files if isinstance(stream := files[key], Stream) and stream.xref)
    return numbers


def _edit_dates(stream: Stream, edit: dict[str, str | None]) -> None:
    """Write or remove the dates of a checked edit in the embedded file stream's ``/Params``."""
    for field, key in (("created", "CreationDate"), ("modified", "ModDate")):
        if field not in edit:
            continue
        if not isinstance(stream.get("Params"), Dictionary):
            if edit[field] is None:
                continue
            stream["Params"] = {}
        params = stream["Params"]
        if (date := edit[field]) is not None:
            params[key] = date
        elif key in params:
            del params[key]


class Attachment:
    """A handle on one embedded file of a document, as ``doc.attachments[name]`` returns it.

    Properties read the document on every access, and assigning
    ``filename``, ``description``, ``created``, or ``modified`` edits the
    file specification at once; ``del`` removes all but the file name. A
    handle raises
    :class:`StaleObjectError` once its attachment has been deleted.
    """

    __slots__ = ("_document", "_name")

    def __init__(self, document: Document, name: str) -> None:
        """Create a handle; :class:`Attachments` does this."""
        self._document = document
        self._name = name

    def _filespec(self) -> Any:  # noqa: ANN401 - a file specification is any PDF value.
        """Return the file specification of this attachment, or raise when it no longer exists."""
        self._document._ensure_open()  # noqa: SLF001
        found = _lookup(self._document, self._name)
        if found is None:
            msg = f"the attachment {self._name!r} no longer exists"
            raise StaleObjectError(msg)
        return found.filespec

    def _params(self) -> Dictionary | None:
        stream = _file(self._filespec())
        params = stream.get("Params") if stream is not None else None
        return params if isinstance(params, Dictionary) else None

    @property
    def name(self) -> str:
        """Return the key under which the document lists the attachment."""
        return self._name

    def _edit_file(self, edit: dict[str, str | None]) -> None:
        """Apply an edit of file fields in one transaction, after checking all of it.

        A date needs the embedded file stream, whose ``/Params`` hold it; the
        file name is written as ``/F``, and as ``/UF`` from PDF 1.7 or when
        the file specification has one; a description raises the document to
        PDF 1.6, as :meth:`Attachments.add` does.
        """
        checked = _check_edit(edit)
        with self._document.transaction("attachment edit"):
            filespec = self._filespec()
            if not isinstance(filespec, Dictionary):
                msg = f"the attachment {self._name!r} has a file specification that is not a dictionary"
                raise PdfError(msg)
            stream = _file(filespec)
            if stream is None and ("created" in checked or "modified" in checked):
                msg = f"the attachment {self._name!r} has no embedded file stream to hold its dates"
                raise PdfError(msg)
            if (filename := checked.get("filename")) is not None:
                filespec["F"] = filename
                if "UF" in filespec or _version(self._document) >= _UF_VERSION:
                    filespec["UF"] = filename
            if "description" in checked:
                if (description := checked["description"]) is not None:
                    _require(self._document, _DESCRIPTION_VERSION)
                    filespec["Desc"] = description
                elif "Desc" in filespec:
                    del filespec["Desc"]
            if stream is not None:
                _edit_dates(stream, checked)

    @property
    def filename(self) -> str:
        """Return the viewer-facing file name (``/UF``, else ``/F``), or ``""``; assignable, never empty."""
        filespec = self._filespec()
        if not isinstance(filespec, Dictionary):
            return _text(filespec) or ""
        return _text(filespec.get("UF")) or _text(filespec.get("F")) or ""

    @filename.setter
    def filename(self, filename: str) -> None:
        filename = _validate_text(filename, "filename")
        if str.__len__(filename) == 0:
            msg = "a file name cannot be empty"
            raise ValueError(msg)
        self._edit_file({"filename": filename})

    @property
    def description(self) -> str | None:
        """Return the description (``/Desc``), or ``None``; assignable, and ``del`` removes it."""
        filespec = self._filespec()
        return (_text(filespec.get("Desc")) or None) if isinstance(filespec, Dictionary) else None

    @description.setter
    def description(self, description: str) -> None:
        self._edit_file({"description": _validate_text(description, "description")})

    @description.deleter
    def description(self) -> None:
        self._edit_file({"description": None})

    @property
    def size(self) -> int:
        """Return the uncompressed size that the file declares (``/Params /Size``), or -1, as pymupdf does."""
        params = self._params()
        size = params.get("Size") if params is not None else None
        return size if isinstance(size, int) and not isinstance(size, bool) else -1

    @property
    def created(self) -> str | None:
        """Return the creation date (``/Params /CreationDate``) as stored, or ``None``; assignable and deletable.

        Dates are PDF date strings such as ``D:20260101120000Z``, of up to 64
        printable ASCII characters, and need an embedded file stream.
        """
        params = self._params()
        return (_text(params.get("CreationDate")) or None) if params is not None else None

    @created.setter
    def created(self, date: str) -> None:
        self._edit_file({"created": _validate_date(date, "created")})

    @created.deleter
    def created(self) -> None:
        self._edit_file({"created": None})

    @property
    def modified(self) -> str | None:
        """Return the modification date (``/Params /ModDate``) as stored, or ``None``; assignable and deletable.

        :meth:`write` sets it to the current time.
        """
        params = self._params()
        return (_text(params.get("ModDate")) or None) if params is not None else None

    @modified.setter
    def modified(self, date: str) -> None:
        self._edit_file({"modified": _validate_date(date, "modified")})

    @modified.deleter
    def modified(self) -> None:
        self._edit_file({"modified": None})

    def read(self, *, max_size: int | None = _DEFAULT_MAX_EMBEDDED_FILE_SIZE) -> bytes:
        """Return the attachment's contents, as pymupdf's ``embfile_get`` does.

        ``max_size`` bounds the decoded data and defaults to 64 MiB, and the
        document's limits bound the decoding. Pass a larger positive integer
        for a known large attachment, or ``None`` only when intentionally
        accepting unbounded materialization. Oversized content raises
        :class:`LimitError` with code ``embedded_file_size``.
        """
        stream = _file(self._filespec())
        _validate_optional_positive_int("max_size", max_size)
        if stream is None:
            msg = f"the attachment {self._name!r} has no embedded file stream"
            raise PdfError(msg)
        filters = stream.get("Filter")
        if filters is not None and not isinstance(filters, Name | Array):
            msg = f"the attachment {self._name!r} has an invalid Filter"
            raise PdfError(msg)
        try:
            return stream.read(max_size=max_size)
        except LimitError as error:
            if error.code != "stream_data_size":
                raise
            code = "embedded_file_size"
            msg = f"the attachment {self._name!r} is larger than the {max_size}-byte limit"
            raise LimitError(code, msg) from error
        except PdfError as error:
            if isinstance(error, LimitError):
                raise
            msg = f"failed to decode attachment {self._name!r}: {error}"
            raise PdfError(msg) from error

    def write(self, data: bytes, /, *, max_size: int | None = _DEFAULT_MAX_EMBEDDED_FILE_SIZE) -> None:
        """Replace the attachment's contents, as pymupdf's ``embfile_upd`` does with ``buffer_``.

        The name, file name, description, and creation date stay; ``size``
        becomes the new length, ``modified`` the current time, and a stale
        ``/CheckSum`` is dropped. ``max_size`` bounds ``data`` and defaults to
        64 MiB, checked before the data is copied.
        """
        self._document._ensure_open()  # noqa: SLF001
        payload = _validate_data(data, max_size)
        with self._document.transaction("attachment write"):
            # Resolve the stream under the transaction's lock, so that a
            # concurrent delete cannot leave this write on a stream that
            # another attachment shares.
            stream = _file(self._filespec())
            if stream is None:
                msg = f"the attachment {self._name!r} has no embedded file stream"
                raise PdfError(msg)
            stream.write(payload)
            if not isinstance(stream.get("Params"), Dictionary):
                stream["Params"] = {}
            params = stream["Params"]
            params["Size"] = len(payload)
            params["ModDate"] = _now()
            if "CheckSum" in params:
                del params["CheckSum"]

    def __eq__(self, other: object) -> bool:
        """Compare handles by document and name."""
        if not isinstance(other, Attachment):
            return NotImplemented
        return self._document is other._document and self._name == other._name

    def __hash__(self) -> int:
        """Hash consistently with equality."""
        return hash((id(self._document), self._name))

    def __repr__(self) -> str:
        """Return ``Attachment('name')``."""
        return f"Attachment({self._name!r})"


def _associated(document: Document) -> bool:
    """Return whether the document claims a PDF/A part whose embedded files are associated files."""
    claim = pdfa_claim(document)
    return claim is not None and claim[0] in _ASSOCIATED_PARTS


def _associate(document: Document, filespec: Dictionary, stream: Dictionary) -> None:
    """Make an added attachment an associated file of the document, as PDF/A-3 and PDF/A-4 require.

    The embedded file gets a MIME type and the modification date that an
    associated file requires (ISO 32000-2 table 45), the file specification
    its relationship to the document, and the catalog's ``/AF`` lists it.
    """
    stream["Subtype"] = Name(_UNKNOWN_MIME)
    stream["Params"]["ModDate"] = _now()
    filespec["AFRelationship"] = Name("Unspecified")
    files = document.catalog.get("AF")
    if isinstance(files, Array):
        files.append(filespec)
    else:
        document.catalog["AF"] = [filespec]


def _dissociate(document: Document, filespec: object) -> None:
    """Remove ``filespec`` from the catalog's ``/AF``, and the array once it is empty."""
    files = document.catalog.get("AF")
    if not isinstance(filespec, Dictionary) or filespec.xref is None or not isinstance(files, Array):
        return
    for index in reversed(range(len(files))):
        item = files[index]
        if isinstance(item, Dictionary) and item.xref == filespec.xref:
            del files[index]
    if not len(files):
        del document.catalog["AF"]


def _root(document: Document) -> Dictionary | None:
    """Return the root of the ``/EmbeddedFiles`` name tree, if the document has one."""
    names = document.catalog.get("Names")
    root = names.get("EmbeddedFiles") if isinstance(names, Dictionary) else None
    return root if isinstance(root, Dictionary) else None


def _entries(document: Document) -> list[_Entry]:
    """Return the entries of the document's name tree, each name once, sorted by key."""
    root = _root(document)
    unique: dict[bytes, _Entry] = {}
    for entry in _walk(root) if root is not None else []:
        unique.setdefault(entry.key, entry)
    return [unique[key] for key in sorted(unique)]


def _lookup(document: Document, name: str) -> _Entry | None:
    """Return the entry called ``name``, or ``None``."""
    return next((entry for entry in _entries(document) if entry.name == name), None)


def _writable(document: Document) -> list[_Entry]:
    """Return the entries of the name tree, refusing a tree that writing it again would copy beyond its bounds."""
    entries = _entries(document)
    for entry in entries:
        _check_inline(entry.filespec)
    return entries


def _rewrite(document: Document, entries: list[_Entry]) -> None:
    """Write the name tree as one node whose ``/Names`` holds ``entries``, sorted by key.

    Without entries the tree is removed, and so is a name dictionary that it
    leaves empty, since a name tree node has at least one entry.
    """
    if not entries:
        catalog = document.catalog
        names = catalog.get("Names")
        if isinstance(names, Dictionary) and "EmbeddedFiles" in names:
            del names["EmbeddedFiles"]
            if not len(names):
                del catalog["Names"]
        return
    pairs: list[object] = []
    for entry in sorted(entries, key=lambda entry: entry.key):
        pairs += [String(entry.key), entry.filespec]
    root = _root(document)
    if root is not None:
        root["Names"] = pairs
        for key in ("Kids", "Limits"):
            if key in root:
                del root[key]
        return
    catalog = document.catalog
    names = catalog.get("Names")
    if isinstance(names, Dictionary):
        names["EmbeddedFiles"] = {"Names": pairs}
    else:
        catalog["Names"] = {"EmbeddedFiles": {"Names": pairs}}


class Attachments(Mapping[str, Attachment]):
    """The embedded files of a document, keyed by name, as ``doc.attachments`` returns them.

    This live view replaces pymupdf's ``embfile_*`` methods: iterate it for
    the sorted names, index it for an :class:`Attachment`, ``add`` a file, and
    ``del`` one. The EmbeddedFiles name tree is read within its bounds on
    every access.
    """

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Create the view; :attr:`Document.attachments` does this."""
        self._document = document

    def _names(self) -> list[str]:
        self._document._ensure_open()  # noqa: SLF001
        return [entry.name for entry in _entries(self._document)]

    def __getitem__(self, name: str) -> Attachment:
        """Return the handle of the attachment called ``name``; raise :class:`KeyError` when absent."""
        _validate_name(name)
        self._document._ensure_open()  # noqa: SLF001
        if _lookup(self._document, name) is None:
            raise KeyError(name)
        return Attachment(self._document, name)

    def __iter__(self) -> Iterator[str]:
        """Iterate the attachment names in sorted order."""
        return iter(self._names())

    def __len__(self) -> int:
        """Return the number of attachments, as pymupdf's ``embfile_count`` does."""
        return len(self._names())

    def __contains__(self, name: object) -> bool:
        """Return whether an attachment is called ``name``."""
        if not isinstance(name, str):
            return False
        _validate_name(name)
        self._document._ensure_open()  # noqa: SLF001
        return _lookup(self._document, name) is not None

    def __delitem__(self, name: str) -> None:
        """Delete the attachment called ``name``; raise :class:`KeyError` when absent."""
        if not isinstance(name, str):
            raise KeyError(name)
        name = _validate_name(name)
        self._document._ensure_open()  # noqa: SLF001
        with self._document.transaction("attachment delete"):
            # Read the tree under the transaction's lock, so that a
            # concurrent edit is not overwritten by a stale snapshot.
            entries = _writable(self._document)
            removed = [entry.filespec for entry in entries if entry.name == name]
            if not removed:
                raise KeyError(name)
            candidates = sorted({number for filespec in removed for number in _objects(filespec)})
            _rewrite(self._document, [entry for entry in entries if entry.name != name])
            for filespec in removed:
                _dissociate(self._document, filespec)
            # The file specification and its embedded files go unless
            # something else, such as an annotation's /FS, refers to them.
            _core.object_free_unreferenced(self._document._core(), candidates)  # noqa: SLF001

    def add(
        self,
        name: str,
        data: bytes,
        /,
        *,
        filename: str | None = None,
        description: str | None = None,
        max_size: int | None = _DEFAULT_MAX_EMBEDDED_FILE_SIZE,
    ) -> Attachment:
        """Add an attachment and return its handle, as pymupdf's ``embfile_add`` does.

        ``name`` is the unique key; adding an existing name raises
        :class:`PdfError`. ``filename`` is the viewer-facing file name and
        defaults to ``name``; ``description`` is a description; both may hold
        any Unicode text. ``max_size`` bounds the data and defaults to 64 MiB,
        matching :meth:`Attachment.read`; ``None`` explicitly accepts
        unbounded input. The name, file name, and description together are
        bounded at 1 MiB in UTF-8 before the data is copied; refusals use
        ``embedded_file_input_size``.

        The file name is written as ``/UF`` only from PDF 1.7, the later of
        the header and the catalog's ``/Version``; a document older than PDF
        1.4, which has no embedded file tree, gets ``/Version /1.4``. A
        description requires at least PDF 1.6.
        Deleting the last attachment removes the tree.

        On a document that claims PDF/A-3 or PDF/A-4, the embedded file gets
        the MIME type ``application/octet-stream`` as ``/Subtype`` and a
        ``/ModDate``, the file specification ``/AFRelationship
        /Unspecified``, and the catalog's ``/AF`` lists it.
        """
        self._document._ensure_open()  # noqa: SLF001
        if not isinstance(name, str):
            msg = f"name must be a string: {type(name).__name__}"
            raise TypeError(msg)
        if not str.__len__(name):
            msg = "name must be at least 1 character"
            raise ValueError(msg)
        for field, value in (("filename", filename), ("description", description)):
            if value is not None and not isinstance(value, str):
                msg = f"{field} must be a string or None: {type(value).__name__}"
                raise TypeError(msg)
        filename = name if filename is None else filename
        texts = [name, filename] if description is None else [name, filename, description]
        _validate_text_size(texts, "attachment name, filename, and description")
        # Write exactly the text just checked, without the overrides of a
        # str subclass, which the object writer would otherwise call.
        name = str.__str__(name)
        filename = str.__str__(filename)
        description = None if description is None else str.__str__(description)
        payload = _validate_data(data, max_size)
        with self._document.transaction("attachment add"):
            # Read the tree under the transaction's lock, so that a
            # concurrent addition or deletion is not overwritten by a stale
            # snapshot.
            entries = _writable(self._document)
            if any(entry.name == name for entry in entries):
                msg = f"an attachment named {name!r} already exists"
                raise PdfError(msg)
            if len(entries) >= _MAX_TREE_ENTRIES:
                msg = f"the EmbeddedFiles name tree's {_MAX_TREE_ENTRIES}-entry safety limit is reached"
                raise PdfError(msg)
            objects = self._document.objects
            associated = _associated(self._document)
            dictionary = {"Type": Name("EmbeddedFile"), "Params": {"Size": len(payload)}}
            stream = objects[objects.add_stream(payload, dictionary)]
            # /Desc needs 1.6, /UF needs 1.7, and the tree needs 1.4.
            minimum = _DESCRIPTION_VERSION if description is not None else _EMBEDDED_FILES_VERSION
            unicode = _require(self._document, minimum) >= _UF_VERSION
            filespec: dict[str, object] = {"Type": Name("Filespec"), "F": filename}
            if unicode:
                filespec["UF"] = filename
            if description is not None:
                filespec["Desc"] = description
            filespec["EF"] = {"F": stream, "UF": stream} if unicode else {"F": stream}
            added = objects[objects.add(filespec)]
            _rewrite(self._document, [*entries, _Entry(_encode_text_string(name), name, added)])
            if associated:
                _associate(self._document, added, stream)
        return Attachment(self._document, name)

    def __repr__(self) -> str:
        """Return the names, such as ``Attachments(['a.xml', 'b.txt'])``."""
        return f"Attachments({self._names()!r})"
