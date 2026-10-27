"""Profiles: the PDF/A, PDF/UA, PDF/X, and PDF/VT claims of a document, with its output intents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyropdf._objects import Array, Dictionary, Name, Stream, String, _catalog

if TYPE_CHECKING:
    from pyropdf._document import Document

#: The XMP metadata that a claim is read from is capped at 16 MiB by default.
_DEFAULT_MAX_XMP_SIZE = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class OutputIntent:
    """One entry of the catalog's ``/OutputIntents``.

    ``subtype`` is ``/S`` without its slash, such as ``"GTS_PDFA1"``, and
    ``profile`` the xref of ``/DestOutputProfile``, or ``None``. A text entry
    that is missing, or neither a string nor a name, is ``None``.
    """

    subtype: str | None
    identifier: str | None
    condition: str | None
    registry: str | None
    info: str | None
    profile: int | None


@dataclass(frozen=True, slots=True)
class Profiles:
    """The profile claims of a document's XMP metadata, and its output intents.

    Each claim is a name such as ``"PDF/A-2B"``, ``"PDF/UA-1"``,
    ``"PDF/X-4"``, or ``"PDF/VT-1"``, or ``None`` when the metadata does not
    declare it. A claim is what the document says of itself; whether it
    holds is for a validator such as veraPDF to decide.
    """

    pdfa: str | None
    pdfua: str | None
    pdfx: str | None
    pdfvt: str | None
    output_intents: tuple[OutputIntent, ...]

    def __bool__(self) -> bool:
        """Return whether the document claims a profile or has an output intent."""
        return any((self.pdfa, self.pdfua, self.pdfx, self.pdfvt, self.output_intents))


def _property(xmp: str, name: str) -> str | None:
    """Return an XMP property written as ``name="value"`` or ``<name>value</name>``, trimmed.

    The name must not continue a longer name, such as ``notpdfaid:part``.
    """
    escaped = re.escape(name)
    match = re.search(
        rf"(?<![\w:])(?:{escaped}\s*=\s*(?:\"([^\"]*)\"|'([^']*)')|<{escaped}\s*>([^<]*)<)",
        xmp,
    )
    if match is None:
        return None
    return next(group for group in match.groups() if group is not None).strip()


def _metadata(document: Document, max_size: int | None) -> str | None:
    """Return the catalog's XMP metadata as text, or ``None`` without a metadata stream."""
    metadata = _catalog(document).get("Metadata")
    if not isinstance(metadata, Stream):
        return None
    return metadata.read(max_size=max_size).decode("utf-8", "replace")


def pdfa_claim(document: Document, max_size: int | None = _DEFAULT_MAX_XMP_SIZE) -> tuple[int, str] | None:
    """Return the PDF/A part and conformance level that the metadata declares, as pylopdf does."""
    xmp = _metadata(document, max_size)
    if xmp is None:
        return None
    part = _property(xmp, "pdfaid:part")
    if part is None or not part.isdigit():
        return None
    return int(part), _property(xmp, "pdfaid:conformance") or ""


def _text(entry: Dictionary, key: str) -> str | None:
    """Return a text string or name entry of ``entry`` as text, or ``None``."""
    value = entry.get(key)
    if isinstance(value, String):
        return value.text
    return str(value) if isinstance(value, Name) else None


def _output_intent(entry: Dictionary) -> OutputIntent:
    """Return the output intent of one ``/OutputIntents`` entry."""
    profile = entry.get("DestOutputProfile")
    return OutputIntent(
        subtype=_text(entry, "S"),
        identifier=_text(entry, "OutputConditionIdentifier"),
        condition=_text(entry, "OutputCondition"),
        registry=_text(entry, "RegistryName"),
        info=_text(entry, "Info"),
        profile=profile.xref if isinstance(profile, Stream) else None,
    )


def profiles(document: Document, max_size: int | None = _DEFAULT_MAX_XMP_SIZE) -> Profiles:
    """Return the profiles of ``document``; see :attr:`Document.profiles`."""
    xmp = _metadata(document, max_size) or ""
    claim = pdfa_claim(document, max_size) if xmp else None
    pdfa = f"PDF/A-{claim[0]}{claim[1].upper()}" if claim else None
    ua = _property(xmp, "pdfuaid:part")
    pdfx = _property(xmp, "pdfxid:GTS_PDFXVersion")
    pdfvt = _property(xmp, "pdfvtid:GTS_PDFVTVersion")
    intents = _catalog(document).get("OutputIntents")
    entries = tuple(intents) if isinstance(intents, Array) else ()
    return Profiles(
        pdfa=pdfa,
        pdfua=f"PDF/UA-{ua}" if ua and ua.isdigit() else None,
        pdfx=pdfx or None,
        pdfvt=pdfvt or None,
        output_intents=tuple(_output_intent(entry) for entry in entries if isinstance(entry, Dictionary)),
    )
