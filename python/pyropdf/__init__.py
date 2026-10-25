"""Complete control of PDF as ISO 32000-2 defines it, implemented in Rust."""

from pyropdf._core import __version__
from pyropdf._document import Document, Probe, open, probe  # noqa: A004 - pyropdf.open is the entry point.
from pyropdf._errors import (
    DocumentClosedError,
    EncryptedDocumentError,
    LimitError,
    MalformedPdfError,
    PasswordError,
    PdfError,
    StaleObjectError,
)
from pyropdf._limits import DocumentLimits

for _public in (Document, Probe, DocumentLimits, open, probe):
    _public.__module__ = "pyropdf"

__all__ = [
    "Document",
    "DocumentClosedError",
    "DocumentLimits",
    "EncryptedDocumentError",
    "LimitError",
    "MalformedPdfError",
    "PasswordError",
    "PdfError",
    "Probe",
    "StaleObjectError",
    "__version__",
    "open",
    "probe",
]
