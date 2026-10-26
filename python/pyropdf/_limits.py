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
    """Optional budgets for opening and reading untrusted PDFs.

    ``None`` leaves a limit unset. Each limit refuses with :class:`LimitError`,
    whose ``code`` names it. Opening reads only the cross-reference data, so
    damage and most limits surface when a part is first read;
    ``check_at_open`` reads every object at open instead. :meth:`web` is a
    conservative profile for user uploads in bounded workers.
    """

    #: The size of the input, in bytes (``file_size``).
    max_file_size: int | None = None
    #: The number of pages (``page_count``).
    max_pages: int | None = None
    #: The number of objects (``object_count``).
    max_objects: int | None = None
    #: The decoded size of any one stream, in bytes (``decompressed_size``).
    max_decompressed_size: int | None = None
    #: The decoded size of one content stream of a page, in bytes (``page_content_size``).
    max_page_content_size: int | None = None
    #: The decoded size of all the streams of one operation, in bytes (``total_decompressed_size``).
    max_total_decompressed_size: int | None = None
    #: The nesting of arrays and dictionaries in one object (``object_depth``).
    max_object_depth: int | None = None
    #: The size of the PDF data handed to the interpreter, in bytes (``interpretation_size``).
    max_interpretation_size: int | None = None
    #: The time that one operation may take, in seconds (``deadline``).
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
        """Return pylopdf's profile for user uploads, which checks every object at open."""
        return cls(**web_limits(), check_at_open=True)


CancellationToken.__module__ = "pyropdf"

__all__ = ["CancellationToken", "DocumentLimits"]
