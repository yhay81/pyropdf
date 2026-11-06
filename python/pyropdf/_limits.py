"""The limits that every operation on a document starts from."""

from __future__ import annotations

import math
from dataclasses import dataclass

from pyropdf._core import CancellationToken, web_limits

_COUNTED_FIELDS = (
    "max_file_size",
    "max_pages",
    "max_objects",
    "max_decompressed_size",
    "max_page_content_size",
    "max_total_decompressed_size",
    "max_object_depth",
    "max_interpretation_size",
    "max_memory_size",
)


def _validate_optional_positive_int(name: str, value: object) -> None:
    """Refuse a value that is neither ``None`` nor a positive integer."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{name} must be a positive integer or None: {value!r}"
        raise TypeError(msg)
    if value <= 0:
        msg = f"{name} must be a positive integer or None: {value!r}"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class DocumentLimits:
    """Optional budgets for opening and reading PDFs.

    ``None`` leaves a limit unset. Each limit refuses with :class:`LimitError`,
    whose ``code`` names it. Opening normally reads only the cross-reference
    data. ``check_at_open`` also reads every object, so that damage and object
    limits can surface at open.

    :meth:`Page.text`, :meth:`Page.words`, :meth:`Page.paths`,
    :meth:`Page.content`, and :meth:`Page.pixmap` all call hayro.
    When the store decodes a filtered stream, ``max_decompressed_size``
    checks its decoded size. ``max_page_content_size`` also applies when
    the store decodes a page content stream for :attr:`Page.operations`.
    Page content and images that hayro decodes directly bypass these checks.

    ``max_memory_size`` counts allocations charged by pyropdf. Memory used
    internally by hayro can exceed it. ``max_duration`` is checked when
    pyropdf polls the budget. A call into hayro can run past that deadline.
    For documents from outside your application, see
    [Reading a PDF in a worker](../limits.md#reading-a-pdf-in-a-worker).
    """

    #: The size of the input, in bytes (``file_size``).
    max_file_size: int | None = None
    #: The number of pages (``page_count``).
    max_pages: int | None = None
    #: The number of objects (``object_count``).
    max_objects: int | None = None
    #: The decoded bytes the store checks for one stream (``decompressed_size``).
    max_decompressed_size: int | None = None
    #: The decoded bytes the store checks for one page content stream (``page_content_size``).
    max_page_content_size: int | None = None
    #: The decoded stream bytes charged by the store in one operation (``total_decompressed_size``).
    max_total_decompressed_size: int | None = None
    #: The nesting of arrays and dictionaries in one object (``object_depth``).
    max_object_depth: int | None = None
    #: The size of the PDF data handed to the interpreter, in bytes (``interpretation_size``).
    max_interpretation_size: int | None = None
    #: The bytes charged to pyropdf's memory budget for one operation (``memory_size``).
    max_memory_size: int | None = None
    #: The deadline in seconds from the start of an operation, checked when pyropdf polls (``deadline``).
    max_duration: float | None = None
    #: A token that cancels every operation of the documents whose limits
    #: carry it (``cancelled``), from any thread.
    cancellation: CancellationToken | None = None
    #: Whether opening reads every object, so that damage and the limits of
    #: objects surface at open.
    check_at_open: bool = False

    def __post_init__(self) -> None:
        """Refuse booleans, non-integers, zero, and negative limits."""
        for name in _COUNTED_FIELDS:
            _validate_optional_positive_int(name, getattr(self, name))
        duration = self.max_duration
        if duration is not None:
            if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                msg = f"max_duration must be a positive number of seconds or None: {duration!r}"
                raise TypeError(msg)
            if not (math.isfinite(duration) and duration > 0):
                msg = f"max_duration must be a positive number of seconds or None: {duration!r}"
                raise ValueError(msg)
        if self.cancellation is not None and not isinstance(self.cancellation, CancellationToken):
            msg = f"cancellation must be a CancellationToken or None: {self.cancellation!r}"
            raise TypeError(msg)
        if not isinstance(self.check_at_open, bool):
            msg = f"check_at_open must be a bool: {self.check_at_open!r}"
            raise TypeError(msg)

    @classmethod
    def web(cls) -> DocumentLimits:
        """Return the web profile, which checks every object at open.

        Its decoded-size limits apply to reads through the store. hayro
        decodes page content and images directly during interpretation and
        rendering. The profile leaves ``max_memory_size`` and ``max_duration``
        unset. Give the worker its own memory and wall-clock limits.
        """
        return cls(**web_limits(), check_at_open=True)


CancellationToken.__module__ = "pyropdf"

__all__ = ["CancellationToken", "DocumentLimits"]
