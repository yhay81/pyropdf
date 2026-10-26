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
from pyropdf._limits import CancellationToken, DocumentLimits
from pyropdf._objects import Array, Dictionary, Name, Objects, Real, Stream, String
from pyropdf._page import Page
from pyropdf._pages import Pages
from pyropdf._pixmap import Pixmap

for _public in (
    Array,
    Dictionary,
    Name,
    Objects,
    Real,
    Stream,
    String,
    Document,
    Probe,
    DocumentLimits,
    CancellationToken,
    History,
    Page,
    Pages,
    Transaction,
    open,
    probe,
):
    _public.__module__ = "pyropdf"

__all__ = [
    "Access",
    "Array",
    "CancellationToken",
    "Dictionary",
    "Document",
    "DocumentClosedError",
    "DocumentLimits",
    "EncryptedDocumentError",
    "Encryption",
    "History",
    "LimitError",
    "MalformedPdfError",
    "Name",
    "Objects",
    "Page",
    "Pages",
    "PasswordError",
    "PdfError",
    "Permissions",
    "Pixmap",
    "Probe",
    "Real",
    "StaleObjectError",
    "StalePageError",
    "Stream",
    "String",
    "Transaction",
    "__version__",
    "open",
    "probe",
]
