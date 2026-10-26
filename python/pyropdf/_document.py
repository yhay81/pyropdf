"""Documents: opening, probing, closing, and saving."""

from __future__ import annotations

import os
import secrets
import stat
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from pyropdf import _core
from pyropdf._core import _Document
from pyropdf._core import probe as _probe
from pyropdf._encryption import Access, Encryption, Permissions, _encryption_arg, _password_bytes
from pyropdf._errors import DocumentClosedError, PdfError
from pyropdf._history import History, Transaction
from pyropdf._limits import DocumentLimits, _validate_optional_positive_int
from pyropdf._pages import Pages

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import TracebackType
    from typing import Self

_DEFAULT_MAX_PDF_OUTPUT_SIZE = 512 * 1024 * 1024
_TEMPORARY_FILE_ATTEMPTS = 100

#: The Info dictionary keys that :class:`Probe` reports, by their names in pyropdf.
_PROBE_METADATA_KEYS = (
    ("title", "Title"),
    ("author", "Author"),
    ("subject", "Subject"),
    ("keywords", "Keywords"),
    ("creator", "Creator"),
    ("producer", "Producer"),
    ("created", "CreationDate"),
    ("modified", "ModDate"),
)


class Document:
    """A PDF document.

    :func:`open` opens an existing document, and ``Document()`` creates an
    empty one. Documents are context managers that close on exit.
    """

    __slots__ = ("_access", "_closed", "_doc", "_limits")

    def __init__(self, *, limits: DocumentLimits | None = None) -> None:
        """Create an empty document without pages; :func:`open` opens an existing one."""
        self._setup(_Document.empty, _resolve_limits(limits))

    def _setup(self, load: Callable[[DocumentLimits], _Document], limits: DocumentLimits) -> None:
        """Set the state of a document that ``load`` makes."""
        self._doc = load(limits)
        self._limits = limits
        self._closed = False
        self._access: Access | None = None

    @property
    def limits(self) -> DocumentLimits:
        """Return the limits that the document's operations start from."""
        return self._limits

    @property
    def pages(self) -> Pages:
        """Return the pages, a sequence of :class:`Page` handles that edits keep valid."""
        self._ensure_open()
        return Pages(self)

    def transaction(self, name: str = "") -> Transaction:
        """Return a context manager that groups the edits of its block into one step named ``name``."""
        self._ensure_open()
        return Transaction(self, name)

    @property
    def history(self) -> History:
        """Return the steps that undo and redo walk."""
        self._ensure_open()
        return History(self)

    def _core(self) -> _Document:
        """Return the core document, refusing a closed one."""
        self._ensure_open()
        return self._doc

    @property
    def is_closed(self) -> bool:
        """Return whether :meth:`close` was called."""
        return self._closed

    @property
    def is_repaired(self) -> bool:
        """Return whether opening recovered damaged cross-reference data."""
        self._ensure_open()
        return self._doc.is_repaired()

    @property
    def needs_password(self) -> bool:
        """Return whether the document is locked until :meth:`unlock` receives its password."""
        self._ensure_open()
        encrypted, unlocked, _, _, _ = _core.security_state(self._doc)
        return encrypted and not unlocked

    @property
    def access(self) -> Access | None:
        """Return what opened an unlocked encrypted document, or ``None`` for a locked one.

        An unencrypted document grants :attr:`Access.OWNER`.
        """
        self._ensure_open()
        encrypted, unlocked, opened, _, _ = _core.security_state(self._doc)
        if not encrypted:
            return Access.OWNER
        if not unlocked:
            return None
        access = Access.OWNER if opened == "owner" else Access.USER
        return max(access, self._access) if self._access is not None else access

    @property
    def encryption_method(self) -> str | None:
        """Return the security handler of an unlocked encrypted document, such as ``"Standard V5 R6 256-bit AES"``.

        ``None`` for an unencrypted or locked document.
        """
        self._ensure_open()
        return _core.security_state(self._doc)[4]

    @property
    def permissions(self) -> Permissions:
        """Return what the document permits, as :class:`Permissions` flags.

        A locked document permits nothing. An unencrypted document, or one
        unlocked with its owner password, permits everything. Otherwise the
        flags are those that the encryption grants the user password.
        """
        access = self.access
        if access is None:
            return Permissions(0)
        if access is Access.OWNER:
            return Permissions.ALL
        _, _, _, flags, _ = _core.security_state(self._doc)
        return Permissions((flags or 0) & Permissions.ALL)

    def unlock(self, password: str) -> Access:
        """Unlock the document with a password and return the access that it grants.

        The owner password grants :attr:`Access.OWNER` and the user password
        :attr:`Access.USER`; a password that matches neither raises
        :class:`PasswordError`. An unencrypted document grants
        :attr:`Access.OWNER` to any password. Unlocking a locked document
        reads it again, which starts its history over; unlocking an unlocked
        one only checks the password, and keeps the history. Passwords are
        capped at 127 UTF-8 bytes.
        """
        self._ensure_open()
        opened = _core.unlock(self._doc, _password_bytes(password))
        access = Access.OWNER if opened == "owner" else Access.USER
        if self._access is None or access > self._access:
            self._access = access
        return access

    def save(
        self,
        path: str | os.PathLike[str],
        /,
        *,
        incremental: bool = False,
        encryption: Encryption | None = None,
    ) -> None:
        """Write the document to a file.

        The document is written completely to a new file beside ``path``,
        which then replaces ``path`` atomically, keeping an existing file's
        permissions; a final symlink is followed and kept. A failed save
        leaves ``path`` as it was. ``incremental=True`` appends only what
        changed to the bytes of the opened file, so that a save without
        edits reproduces it byte for byte; a full save rewrites every object
        and keeps the values of untouched ones.

        Without ``encryption``, a full save writes an encrypted document
        decrypted and an incremental save keeps its encryption;
        :attr:`Encryption.KEEP` keeps it in a full save too, copying its
        untouched encrypted bytes, and an :class:`Encryption` encrypts a full
        save anew.
        """
        self._ensure_open()
        _validate_bool("incremental", incremental)
        arg = _encryption_arg(encryption)
        _atomic_save_file(path, lambda temporary: self._doc.save_path(temporary, incremental, arg))

    def to_bytes(
        self,
        *,
        incremental: bool = False,
        encryption: Encryption | None = None,
        max_size: int | None = _DEFAULT_MAX_PDF_OUTPUT_SIZE,
    ) -> bytes:
        """Return the document as PDF bytes, as :meth:`save` writes them, with its ``encryption``.

        ``max_size`` bounds the output, 512 MiB by default, and refuses the
        write that would pass it with :class:`LimitError` code
        ``pdf_output_size``; ``None`` leaves it unbounded for trusted input.
        """
        self._ensure_open()
        _validate_bool("incremental", incremental)
        _validate_optional_positive_int("max_size", max_size)
        return self._doc.to_bytes(incremental, max_size, _encryption_arg(encryption))

    def close(self) -> None:
        """Close the document and release its data; later operations raise :class:`DocumentClosedError`."""
        self._closed = True
        self._doc.close()

    def _ensure_open(self) -> None:
        """Refuse operations on a closed document."""
        if self._closed:
            msg = "document closed"
            raise DocumentClosedError(msg)

    def __enter__(self) -> Self:
        """Enter a context manager and return this document."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the document when leaving a context manager."""
        self.close()

    def __repr__(self) -> str:
        """Return a representation containing the open or closed state."""
        state = "closed " if self._closed else ""
        return f"<{state}pyropdf.Document>"


def _validate_bool(name: str, value: object) -> None:
    """Refuse a value that is not a bool."""
    if not isinstance(value, bool):
        msg = f"{name} must be a bool: {value!r}"
        raise TypeError(msg)


def _resolve_limits(limits: object) -> DocumentLimits:
    """Return ``limits``, or the default limits for ``None``."""
    if limits is None:
        return DocumentLimits()
    if not isinstance(limits, DocumentLimits):
        msg = f"limits must be a DocumentLimits instance or None: {limits!r}"
        raise TypeError(msg)
    return limits


def _open_source(source: object) -> tuple[str | None, bytes | bytearray | memoryview | None]:
    """Split a source into a file path or a buffer holding the document."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        return None, source
    if isinstance(source, (str, os.PathLike)):
        path = os.fspath(source)
        if isinstance(path, str):
            return path, None
    if hasattr(source, "read"):
        msg = f"source must be a path or bytes; read the file object first: {type(source).__name__}"
        raise TypeError(msg)
    msg = f"source must be a path (str or os.PathLike[str]) or bytes, bytearray, or memoryview: {type(source).__name__}"
    raise TypeError(msg)


def open(  # noqa: A001 - the package's entry point, pyropdf.open.
    source: str | os.PathLike[str] | bytes | bytearray | memoryview,
    /,
    *,
    password: str | None = None,
    limits: DocumentLimits | None = None,
) -> Document:
    """Open a PDF from a file path or from the bytes of a document.

    A document with an empty user password decrypts on its own; any other
    encrypted document needs ``password``, or :meth:`Document.unlock` after
    opening, and refuses to read its objects until then. A wrong password
    raises :class:`PasswordError`. Passwords are capped at 127 UTF-8 bytes.

    Opening reads the cross-reference data only; ``bytes`` are read in place,
    without a copy, and other buffers are copied. ``limits`` bounds the work,
    and :meth:`DocumentLimits.web` also reads every object at open, for
    untrusted uploads. Input that is not a readable PDF, including empty
    input, raises :class:`MalformedPdfError`, and a missing file raises
    :class:`FileNotFoundError`.
    """
    path, data = _open_source(source)
    secret = None if password is None else _password_bytes(password)
    resolved = _resolve_limits(limits)
    document = Document.__new__(Document)
    if data is not None:
        buffer = data
        document._setup(lambda lim: _Document.open_buffer(buffer, lim, secret), resolved)  # noqa: SLF001
    elif path is not None:
        file = path
        document._setup(lambda lim: _Document.open_path(file, lim, secret), resolved)  # noqa: SLF001
    return document


@dataclass(frozen=True, slots=True, kw_only=True)
class Probe:
    """What :func:`probe` reads without loading the whole document."""

    #: The number of pages, as the page tree's root counts them.
    page_count: int
    #: The PDF version of the header, such as ``"1.7"``.
    version: str
    #: Whether the document is encrypted and the password given, if any, did not unlock it.
    needs_password: bool
    #: Whether reading it needed cross-reference recovery.
    is_repaired: bool
    #: The Info dictionary's text entries that are present: ``title``,
    #: ``author``, ``subject``, ``keywords``, ``creator``, ``producer``,
    #: ``created``, and ``modified``, the last two as PDF date strings.
    #: A locked document reports none.
    metadata: Mapping[str, str]


def probe(
    source: str | os.PathLike[str] | bytes | bytearray | memoryview,
    /,
    *,
    password: str | None = None,
    max_file_size: int | None = None,
) -> Probe:
    """Read the page count, version, encryption, and metadata without loading the whole document.

    This suits scanning many files. ``max_file_size`` refuses larger input
    before parsing with :class:`LimitError` code ``file_size``; ``None``, the
    default, leaves it unbounded. The metadata text is capped at 1 MiB, and
    passwords at 127 UTF-8 bytes; a wrong password raises :class:`PasswordError`.
    """
    path, data = _open_source(source)
    _validate_optional_positive_int("max_file_size", max_file_size)
    secret = None if password is None else _password_bytes(password)
    page_count, version, encrypted, repaired, raw = _probe(path, data, max_file_size, secret)
    return Probe(
        page_count=page_count,
        version=version,
        needs_password=encrypted,
        is_repaired=repaired,
        metadata=MappingProxyType({name: raw[key] for name, key in _PROBE_METADATA_KEYS if key in raw}),
    )


def _temporary_sibling_path(target: Path) -> Path:
    """Create one same-directory output path with normal umask permissions."""
    for _ in range(_TEMPORARY_FILE_ATTEMPTS):
        candidate = target.parent / f".pyropdf-{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        except FileExistsError:
            continue
        os.close(descriptor)
        return candidate
    msg = f"failed to create a unique temporary output beside {target}"
    raise FileExistsError(msg)


def _remove_temporary_output(temporary: Path) -> None:
    """Remove a temporary output, best effort, including a Windows file made read-only."""
    if sys.platform == "win32":
        with suppress(OSError):
            temporary.chmod(stat.S_IWRITE)
    with suppress(OSError):
        temporary.unlink()


def _atomic_save_file(filename: str | os.PathLike[str], writer: Callable[[str], None]) -> None:
    """Write a sibling file completely before atomically replacing the target."""
    requested = Path(filename)
    try:
        target = requested.resolve(strict=False) if requested.is_symlink() else requested
        try:
            target_metadata = target.stat()
        except FileNotFoundError:
            target_mode = None
        else:
            target_mode = stat.S_IMODE(target_metadata.st_mode) if stat.S_ISREG(target_metadata.st_mode) else None
        temporary = _temporary_sibling_path(target)
    except (OSError, RuntimeError) as exc:
        msg = f"failed to save {requested}: {exc}"
        raise PdfError(msg) from exc
    replaced = False
    try:
        try:
            writer(str(temporary))
        except PdfError as exc:
            # I/O errors name the output; name the caller's path instead.
            if str(temporary) not in str(exc):
                raise
            message = str(exc).replace(str(temporary), str(requested))
            raise PdfError(message) from exc
        try:
            if target_mode is not None:
                temporary.chmod(target_mode)
            temporary.replace(target)
        except OSError as exc:
            msg = f"failed to save {requested}: {exc}"
            raise PdfError(msg) from exc
        replaced = True
    finally:
        if not replaced:
            _remove_temporary_output(temporary)


__all__ = ["Document", "Probe", "open", "probe"]
