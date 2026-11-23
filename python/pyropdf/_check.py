"""The structural check: the document against the Arlington PDF Model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

from pyropdf import _core
from pyropdf._archival_colour import archival_colour
from pyropdf._objects import Array, Dictionary, Name, Stream, String, _catalog, _trailer
from pyropdf._profiles import profiles

if TYPE_CHECKING:
    from pyropdf._document import Document

#: The category of a :class:`StructuralDefect`.
DefectKind: TypeAlias = Literal["type", "required", "version", "indirect", "value", "invariant", "profile"]
DefectKind.__doc__ = "The category of a :class:`StructuralDefect`."


@dataclass(frozen=True, slots=True, kw_only=True)
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


@dataclass(frozen=True, slots=True, kw_only=True)
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
    return StructuralDefect(
        xref=xref,
        path=path,
        clause=f"{claim}/{path.split('/', maxsplit=1)[0]}",
        spec=spec,
        kind="profile",
        message=message,
    )


def _pdfa_defects(document: Document, claim: str, catalog: Dictionary) -> tuple[list[StructuralDefect], bool]:
    """Return the document-level PDF/A rules that ``document`` breaks."""
    out = []
    if "Encrypt" in _trailer(document):
        out.append(_defect(claim, None, "Encrypt", "ISO 19005 6.1.3", f"{claim} forbids encryption"))
    device, unsolved = archival_colour(document, claim)
    if device is not None:
        space = {"gray": "DeviceGray", "rgb": "DeviceRGB", "cmyk": "DeviceCMYK"}[device]
        message = f"{claim} uses {space} without an applicable colour characterization"
        spec = "ISO 19005-1 6.2.3" if claim.startswith("PDF/A-1") else "ISO 19005 6.2.4.3"
        out.append(_defect(claim, catalog.xref, "OutputIntents", spec, message))
    script = _javascript(catalog)
    if script is not None:
        out.append(_defect(claim, catalog.xref, script, "ISO 19005 6.6.1", f"{claim} forbids JavaScript"))
    return out, unsolved


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


def _pdfx4_defects(catalog: Dictionary) -> list[StructuralDefect]:
    """Check the single embedded PDF/X output intent shared by PDF/X-4 and PDF/VT-1."""
    spec = "ISO 15930-7:2010 6.4.2.1"
    intents = catalog.get("OutputIntents")
    match = None
    if isinstance(intents, Array):
        for index, entry in enumerate(intents):
            if isinstance(entry, Dictionary) and entry.get("S") == Name("GTS_PDFX"):
                if match is not None:
                    message = "PDF/X-4 requires exactly one /GTS_PDFX output intent"
                    return [_defect("PDF/X-4", catalog.xref, "OutputIntents", spec, message)]
                match = (index, entry)
    if match is None:
        return [_defect("PDF/X-4", catalog.xref, "OutputIntents", spec, "PDF/X-4 requires a /GTS_PDFX output intent")]

    index, entry = match
    rules = (
        (isinstance(entry.get("OutputConditionIdentifier"), String), "OutputConditionIdentifier", "a text identifier"),
        (isinstance(entry.get("DestOutputProfile"), Stream), "DestOutputProfile", "an embedded output profile stream"),
        ("DestOutputProfileRef" not in entry, "DestOutputProfileRef", "an output intent without an external profile"),
    )
    return [
        _defect("PDF/X-4", catalog.xref, f"OutputIntents/{index}/{key}", spec, f"PDF/X-4 requires {needed}")
        for holds, key, needed in rules
        if not holds
    ]


def _vt_page_index(document: Document, obj: object) -> int | None:
    """Resolve a page boundary through the existing page index."""
    if not isinstance(obj, Dictionary) or obj.xref is None or obj.generation is None:
        return None
    return _core.page_index(document._core(), obj.xref, obj.generation)  # noqa: SLF001


def _vt_leaf_covers(document: Document, leaf: object, index: int) -> bool:
    """Check that the page points to a leaf whose inclusive range covers the page."""
    if not isinstance(leaf, Dictionary) or leaf.xref is None or "DParts" in leaf:
        return False
    start = _vt_page_index(document, leaf.get("Start"))
    end = _vt_page_index(document, leaf["End"]) if "End" in leaf else start
    return start is not None and end is not None and start <= index <= end


def _pdfvt1_defects(document: Document, pdfx: str | None, catalog: Dictionary) -> list[StructuralDefect]:
    """Check the PDF/X-4 basis, hierarchy root, and page-to-leaf links of a PDF/VT-1 file."""
    claim = "PDF/VT-1"
    structure = "ISO 16612-2:2010 6.5"
    out = []
    if pdfx != "PDF/X-4":
        message = "PDF/VT-1 requires a PDF/X-4 claim"
        out.append(_defect(claim, catalog.xref, "Metadata", "ISO 16612-2:2010 6.2.1", message))
    root = catalog.get("DPartRoot")
    if not isinstance(root, Dictionary) or root.xref is None:
        out.append(_defect(claim, catalog.xref, "DPartRoot", structure, "PDF/VT-1 requires an indirect /DPartRoot"))
        return out
    node = root.get("DPartRootNode")
    if not isinstance(node, Dictionary) or node.xref is None:
        out.append(_defect(claim, root.xref, "DPartRootNode", structure, "PDF/VT-1 requires an indirect root part"))
        return out
    children = node.get("DParts")
    if "DParts" in node and (
        not isinstance(children, Array) or not children or not all(isinstance(chunk, Array) for chunk in children)
    ):
        message = "A PDF/VT-1 part stores its children in an array of arrays"
        out.append(_defect(claim, node.xref, "DParts", structure, message))
    try:
        for index, page in enumerate(document.pages):
            if not _vt_leaf_covers(document, page.obj.get("DPart"), index):
                message = "Each PDF/VT-1 page must refer to its leaf document part"
                out.append(_defect(claim, page.xref, "DPart", structure, message))
                break
    except _core.MalformedPdfError:
        # The core walk already reported the malformed page tree. Leaf ranges
        # cannot be interpreted until there is a page order.
        return out
    return out


def _profile_defects(document: Document) -> tuple[list[StructuralDefect], bool]:
    """Return the structural rules of the declared profiles that the document breaks."""
    claimed = profiles(document)
    catalog = _catalog(document)
    out = []
    unsolved = False
    if claimed.pdfa is not None:
        out, unsolved = _pdfa_defects(document, claimed.pdfa, catalog)
    if claimed.pdfua is not None:
        out += _pdfua_defects(claimed.pdfua, catalog)
    if claimed.pdfx == "PDF/X-4" or claimed.pdfvt == "PDF/VT-1":
        out += _pdfx4_defects(catalog)
    if claimed.pdfvt == "PDF/VT-1":
        out += _pdfvt1_defects(document, claimed.pdfx, catalog)
    return out, unsolved


def structural_check(document: Document) -> StructuralReport:
    """Return the structural check of ``document``; see :meth:`Document.check`."""
    defects, truncated, objects, unchecked, version = _core.structural_check(document._core())  # noqa: SLF001
    findings = [
        StructuralDefect(xref=xref, path=path, clause=clause, spec=spec, kind=kind, message=message)
        for xref, path, clause, spec, kind, message in defects
    ]
    profile_findings, unsolved = _profile_defects(document)
    findings.extend(profile_findings)
    return StructuralReport(
        defects=tuple(findings),
        version=version,
        objects=objects,
        unchecked=unchecked + int(unsolved),
        truncated=truncated,
    )
