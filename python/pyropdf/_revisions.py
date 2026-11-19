"""Incremental file revisions and per-revision object changes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pyropdf import _core
from pyropdf._fallback_fonts import _set_fallback

if TYPE_CHECKING:
    from pyropdf._document import Document
    from pyropdf._limits import DocumentLimits


@dataclass(frozen=True, slots=True)
class Revision:
    """One incremental file revision of an opened PDF document.

    ``index`` counts revisions from ``0``, oldest first, matching
    :attr:`Signature.revision`. ``end`` is the byte offset after the
    revision's ``%%EOF`` marker (and any line ending that immediately follows
    it), or ``None`` when the revision's ``startxref`` has no ``%%EOF``.
    ``added``, ``updated``, and ``freed`` list the indirect object numbers
    that this revision added, updated, or freed, each sorted ascending.
    """

    index: int
    end: int | None
    added: tuple[int, ...]
    updated: tuple[int, ...]
    freed: tuple[int, ...]
    _document: Document | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def objects(self) -> tuple[int, ...]:
        """Return the indirect object numbers that this revision added, updated, or freed, ascending."""
        return tuple(sorted({*self.added, *self.updated, *self.freed}))

    def open(self, *, limits: DocumentLimits | None = None) -> Document:
        """Open this revision as an independent :class:`Document` sharing the file's bytes.

        ``limits`` bounds the opened revision, defaulting to the parent
        document's :attr:`Document.limits`; :meth:`DocumentLimits.web` checks
        every object of the revision at open. An unlocked encrypted parent
        opens an unlocked revision with the same access, while a locked parent
        opens a locked revision that :meth:`Document.unlock` can unlock.
        Closing the parent document afterward leaves an already-opened
        revision usable, while calling ``open`` after the parent is closed
        raises :class:`DocumentClosedError`.
        """
        from pyropdf._document import Document, _resolve_limits  # noqa: PLC0415 - the document module imports this one.

        if self._document is None:
            msg = "Revision is not bound to a Document"
            raise ValueError(msg)
        parent = self._document
        parent_core = parent._core()  # noqa: SLF001
        resolved = parent.limits if limits is None else _resolve_limits(limits)
        if self.index < 0:
            msg = f"revision index out of range: {self.index}"
            raise IndexError(msg)
        opened = Document.__new__(Document)
        try:
            opened._setup(lambda lim: _core.open_revision(parent_core, self.index, lim), resolved)  # noqa: SLF001
        except OverflowError as exc:
            msg = f"revision index out of range: {self.index}"
            raise IndexError(msg) from exc
        if parent._access is not None and opened.access is not None:  # noqa: SLF001
            opened._access = max(opened.access, parent._access)  # noqa: SLF001
        if parent._fallback is not None:  # noqa: SLF001
            opened._fallback = dict(parent._fallback)  # noqa: SLF001
            _set_fallback(opened, opened._fallback)  # noqa: SLF001
        return opened


def revisions(document: Document) -> tuple[Revision, ...]:
    """Return the incremental revisions of ``document``'s opened file, oldest first."""
    items = _core.document_revisions(document._core())  # noqa: SLF001
    out: list[Revision] = []
    for index, (end, added, updated, freed) in enumerate(items):
        rev = Revision(index, end, tuple(added), tuple(updated), tuple(freed))
        object.__setattr__(rev, "_document", document)
        out.append(rev)
    return tuple(out)
