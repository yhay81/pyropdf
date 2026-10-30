"""Transactions, which group edits into one step, and the history that undoes and redoes steps."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyropdf import _core

if TYPE_CHECKING:
    from types import TracebackType
    from typing import Self

    from pyropdf._document import Document


class Transaction:
    """A group of edits that commits as one version and one undo step.

    ``with doc.transaction(name):`` opens it. Leaving the block commits its
    edits; an exception discards them, without copying the document, and
    propagates. A transaction opened inside another joins it: its edits
    commit with the outer one, and its exception discards only its own
    edits. Reads inside the block see its edits.

    A transaction belongs to the thread that opened it. Other threads read
    the version before it until it commits, and their edits, transactions,
    undo, and redo wait until it ends.
    """

    __slots__ = ("_document", "_name")

    def __init__(self, document: Document, name: str) -> None:
        """Name a transaction of ``document``; use ``doc.transaction()`` instead."""
        if not isinstance(name, str):
            msg = f"name must be a str: {name!r}"
            raise TypeError(msg)
        self._document = document
        self._name = name

    def __enter__(self) -> Self:
        """Open the transaction, or nest it in the open one."""
        _core.begin_transaction(self._document._core(), self._name)  # noqa: SLF001
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Commit the edits, or discard them when the block raised."""
        _core.end_transaction(self._document._core(), exc_type is None)  # noqa: SLF001


class History:
    """The line of steps that undo and redo walk (DESIGN.md section 4.1).

    Every committed edit is a step: a transaction is one step with its name,
    and an edit outside a transaction is one step named ``""``. A new step
    after undo discards the redo steps. :attr:`max_size` bounds the bytes
    that the steps keep, 256 MiB by default: the farthest redo steps go
    first while redo alone is over it, then the oldest undo steps.
    """

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Wrap the history of ``document``; use ``doc.history`` instead."""
        self._document = document

    def _state(self) -> tuple[list[str], int, int | None, int]:
        return _core.history_state(self._document._core())  # noqa: SLF001

    @property
    def steps(self) -> tuple[str, ...]:
        """Return the names of the steps, oldest first: those that undo and then redo can take."""
        return tuple(self._state()[0])

    @property
    def position(self) -> int:
        """Return how many steps undo can take."""
        return self._state()[1]

    @property
    def can_undo(self) -> bool:
        """Return whether a step can be undone."""
        return self._state()[1] > 0

    @property
    def can_redo(self) -> bool:
        """Return whether a step can be redone."""
        names, position, _, _ = self._state()
        return position < len(names)

    @property
    def size(self) -> int:
        """Return the bytes that the steps keep, as the budget counts them."""
        return self._state()[3]

    @property
    def max_size(self) -> int | None:
        """Return the budget of the steps in bytes, or ``None`` for no limit; 0 keeps no step."""
        return self._state()[2]

    @max_size.setter
    def max_size(self, value: int | None) -> None:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            msg = f"max_size must be a non-negative integer or None: {value!r}"
            raise ValueError(msg)
        _core.set_history_limit(self._document._core(), value)  # noqa: SLF001

    def undo(self) -> None:
        """Undo one step; raises :class:`PdfError` with nothing to undo or inside a transaction."""
        _core.history_undo(self._document._core())  # noqa: SLF001

    def redo(self) -> None:
        """Redo one step; raises :class:`PdfError` with nothing to redo or inside a transaction."""
        _core.history_redo(self._document._core())  # noqa: SLF001

    def save(self) -> bytes:
        """Return the steps, their names, and the position among them as bytes that :meth:`restore` reads.

        The bytes hold what each step wrote, not the document: restoring
        them needs the document they were saved from. They hold the objects
        that the steps wrote unencrypted, even for an encrypted document.
        Raises :class:`PdfError` inside a transaction.
        """
        return _core.history_save(self._document._core())  # noqa: SLF001

    def restore(self, data: bytes) -> None:
        """Restore the steps that :meth:`save` returned, and make the document as it was then.

        The document must be the one that the steps were saved from, opened
        again and not edited since: raises :class:`ValueError` for another
        document and :class:`PdfError` for an edited one or inside a
        transaction. Every byte is checked before the document changes, so
        :class:`MalformedPdfError` for data that is not a saved history and
        :class:`LimitError` for what the document's limits refuse leave the
        document and its history as they were. :attr:`max_size` still
        applies: the steps that do not fit it are dropped, as after an edit.
        """
        if not isinstance(data, bytes):
            msg = f"data must be bytes, not {type(data).__name__}"
            raise TypeError(msg)
        _core.history_restore(self._document._core(), data)  # noqa: SLF001

    def clear(self) -> None:
        """Forget every step, keeping the document as it is."""
        _core.history_clear(self._document._core())  # noqa: SLF001

    def __repr__(self) -> str:
        """Return the position among the steps."""
        names, position, _, _ = self._state()
        return f"<History {position}/{len(names)}>"


__all__ = ["History", "Transaction"]
