"""Complete control of PDF as ISO 32000-2 defines it, implemented in Rust."""

from pyropdf._core import __version__
from pyropdf._document import Document, Probe, open, probe  # noqa: A004 - pyropdf.open is the entry point.
from pyropdf._encryption import Access, Encryption, Permissions
from pyropdf._errors import (
    DocumentClosedError,
    EncryptedDocumentError,
    LimitError,
    MalformedPdfError,
    PasswordError,
    PdfError,
    StaleObjectError,
    StalePageError,
)
from pyropdf._history import History, Transaction
from pyropdf._limits import DocumentLimits
from pyropdf._page import Page
from pyropdf._pages import Pages
from pyropdf._pixmap import Pixmap

for _public in (Document, Probe, DocumentLimits, History, Page, Pages, Transaction, open, probe):
    _public.__module__ = "pyropdf"

__all__ = [
    "Access",
    "Document",
    "DocumentClosedError",
    "DocumentLimits",
    "EncryptedDocumentError",
    "Encryption",
    "History",
    "LimitError",
    "MalformedPdfError",
    "Page",
    "Pages",
    "PasswordError",
    "PdfError",
    "Permissions",
    "Pixmap",
    "Probe",
    "StaleObjectError",
    "StalePageError",
    "Transaction",
    "__version__",
    "open",
    "probe",
]
