"""The structural check: the document against the Arlington PDF Model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pyropdf import _core

if TYPE_CHECKING:
    from pyropdf._document import Document

DefectKind = Literal["type", "required", "version", "indirect", "value"]


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


def structural_check(document: Document) -> StructuralReport:
    """Return the structural check of ``document``; see :meth:`Document.check`."""
    defects, truncated, objects, unchecked, version = _core.structural_check(document._core())  # noqa: SLF001
    return StructuralReport(
        tuple(StructuralDefect(*row) for row in defects),
        version,
        objects,
        unchecked,
        truncated,
    )
