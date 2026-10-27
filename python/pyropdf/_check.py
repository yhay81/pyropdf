"""The structural check: the document against the Arlington PDF Model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pyropdf import _core
from pyropdf._objects import Array, Dictionary, _catalog, _trailer
from pyropdf._profiles import profiles

if TYPE_CHECKING:
    from pyropdf._document import Document

DefectKind = Literal["type", "required", "version", "indirect", "value", "invariant", "profile"]


@dataclass(frozen=True, slots=True)
class StructuralDefect:
    """One departure of the document from the model.

    ``xref`` is the indirect object, or ``None`` for the trailer, and ``path``
    the keys and indexes from that object to the value, joined by ``/``.
    ``clause`` is the Arlington clause, the object type and key such as
    ``"Catalog/Pages"``, and ``spec`` the clause of ISO 32000 that the model
    names, such as ``"Table 29"``, or an empty string.
    """

    xref: int | None
    path: str
    clause: str
    spec: str
    kind: DefectKind
    message: str


@dataclass(frozen=True, slots=True)
class StructuralReport:
    """What the structural check found.

    ``version`` is the PDF version that the check held the document to: the
    later of its header and the catalog's ``/Version``. ``objects`` counts
    the indirect objects that the walk reached, and ``unchecked`` the
    predicates of the model that it met but cannot decide. ``truncated`` is
    whether defects past the report's limit were dropped.
    """

    defects: tuple[StructuralDefect, ...]
    version: str
    objects: int
    unchecked: int
    truncated: bool

    def __bool__(self) -> bool:
        """Return whether the report names a defect."""
        return bool(self.defects)


def _javascript(catalog: Dictionary) -> str | None:
    """Return the path of the catalog's document-level JavaScript, or ``None``."""
    names = catalog.get("Names")
    if isinstance(names, Dictionary) and "JavaScript" in names:
        return "Names/JavaScript"
    action = catalog.get("OpenAction")
    if isinstance(action, Dictionary) and action.get("S") == "JavaScript":
        return "OpenAction"
    return None


def _defect(claim: str, xref: int | None, path: str, spec: str, message: str) -> StructuralDefect:
    """Return a ``profile`` defect of ``claim`` at ``path``, whose first key names the clause."""
    return StructuralDefect(xref, path, f"{claim}/{path.split('/', maxsplit=1)[0]}", spec, "profile", message)


def _pdfa_defects(document: Document, claim: str, catalog: Dictionary) -> list[StructuralDefect]:
    """Return the document-level PDF/A rules that ``document`` breaks."""
    out = []
    if "Encrypt" in _trailer(document):
        out.append(_defect(claim, None, "Encrypt", "ISO 19005 6.1.3", f"{claim} forbids encryption"))
    intents = catalog.get("OutputIntents")
    entries = list(intents) if isinstance(intents, Array) else []
    if not any(isinstance(entry, Dictionary) and entry.get("S") == "GTS_PDFA1" for entry in entries):
        message = f"{claim} needs a /GTS_PDFA1 output intent"
        out.append(_defect(claim, catalog.xref, "OutputIntents", "ISO 19005 6.2.2", message))
    script = _javascript(catalog)
    if script is not None:
        out.append(_defect(claim, catalog.xref, script, "ISO 19005 6.6.1", f"{claim} forbids JavaScript"))
    return out


def _pdfua_defects(claim: str, catalog: Dictionary) -> list[StructuralDefect]:
    """Return the document-level PDF/UA rules that ``catalog`` breaks."""
    mark_info = catalog.get("MarkInfo")
    preferences = catalog.get("ViewerPreferences")
    rules = (
        (
            isinstance(mark_info, Dictionary) and mark_info.get("Marked") is True,
            "MarkInfo/Marked",
            "7.1",
            "/MarkInfo /Marked true",
        ),
        (isinstance(catalog.get("StructTreeRoot"), Dictionary), "StructTreeRoot", "7.1", "a structure tree"),
        ("Lang" in catalog, "Lang", "7.2", "the document's /Lang"),
        (
            isinstance(preferences, Dictionary) and preferences.get("DisplayDocTitle") is True,
            "ViewerPreferences/DisplayDocTitle",
            "7.1",
            "/ViewerPreferences /DisplayDocTitle true",
        ),
    )
    return [
        _defect(claim, catalog.xref, path, f"ISO 14289-1 {spec}", f"{claim} needs {needed}")
        for holds, path, spec, needed in rules
        if not holds
    ]


def _profile_defects(document: Document) -> list[StructuralDefect]:
    """Return the document-level rules of the claimed PDF/A and PDF/UA profiles that it breaks."""
    claimed = profiles(document)
    catalog = _catalog(document)
    out = []
    if claimed.pdfa is not None:
        out += _pdfa_defects(document, claimed.pdfa, catalog)
    if claimed.pdfua is not None:
        out += _pdfua_defects(claimed.pdfua, catalog)
    return out


def structural_check(document: Document) -> StructuralReport:
    """Return the structural check of ``document``; see :meth:`Document.check`."""
    defects, truncated, objects, unchecked, version = _core.structural_check(document._core())  # noqa: SLF001
    return StructuralReport(
        (*(StructuralDefect(*row) for row in defects), *_profile_defects(document)),
        version,
        objects,
        unchecked,
        truncated,
    )
