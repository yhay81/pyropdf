"""The exceptions of pyropdf, which the core raises, and ``LimitError.code``."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pyropdf._core import (
    DocumentClosedError,
    EncryptedDocumentError,
    LimitError,
    MalformedPdfError,
    PasswordError,
    PdfError,
    StaleObjectError,
)

if TYPE_CHECKING:
    from typing import Any


class StalePageError(StaleObjectError):
    """Use of a :class:`Page` whose page object the page tree no longer lists.

    Other pages' edits never make a page stale; deleting it does, and undo
    can make it valid again. Fetch a current page with ``doc.pages[i]``.
    """


_LIMIT_ERROR_STR = LimitError.__str__
_LIMIT_ERROR_ARG_COUNT = 2


def _limit_error_code(error: LimitError) -> str:
    """Return the stable code of the limit that refused the operation."""
    if len(error.args) >= _LIMIT_ERROR_ARG_COUNT and isinstance(error.args[0], str):
        return error.args[0]
    return "unknown"


def _limit_error_str(error: LimitError) -> str:
    """Return only the message of a refusal, without its code."""
    if len(error.args) >= _LIMIT_ERROR_ARG_COUNT and isinstance(error.args[1], str):
        return error.args[1]
    return _LIMIT_ERROR_STR(error)


_limit_error_type = cast("Any", LimitError)
_limit_error_type.code = property(_limit_error_code, doc="The stable code of the limit, from ``LIMIT_CODES``.")
_limit_error_type.__str__ = _limit_error_str


# The package re-exports these names; reprs, pickles, and documentation name it.
for _public in (
    PdfError,
    MalformedPdfError,
    PasswordError,
    EncryptedDocumentError,
    LimitError,
    StaleObjectError,
    StalePageError,
    DocumentClosedError,
):
    _public.__module__ = "pyropdf"

__all__ = [
    "DocumentClosedError",
    "EncryptedDocumentError",
    "LimitError",
    "MalformedPdfError",
    "PasswordError",
    "PdfError",
    "StaleObjectError",
    "StalePageError",
]
