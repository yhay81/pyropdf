"""The change report: what the edits since opening invalidate before a save."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyropdf import _core

if TYPE_CHECKING:
    from pyropdf._document import Document


@dataclass(frozen=True, slots=True)
class SignatureChange:
    """An opened signature affected by the save mode or edits to covered objects.

    ``field`` is the signature field's full name and ``xref`` its object.
    ``objects`` lists the edited objects that the signature signs, empty when
    the save mode alone invalidates it.
    """

    field: str
    xref: int
    reason: str
    objects: tuple[int, ...]

    @property
    def signed_bytes_preserved(self) -> bool:
        """Return whether the save preserves the bytes of the signed revision."""
        return bool(self.objects)


@dataclass(frozen=True, slots=True)
class SignaturePermission:
    """A signed DocMDP or FieldMDP transform's verdict for one proposed edit.

    ``signature`` and ``xref`` identify the signature field. ``object`` identifies
    the proposed edit (zero for the trailer). ``field`` is the affected form
    field when known. A verdict of ``None`` calls for further review.
    """

    signature: str
    xref: int
    transform: str
    edit: str
    object: int
    field: str | None
    permitted: bool | None
    reason: str


@dataclass(frozen=True, slots=True)
class ClaimChange:
    """A PDF/A or PDF/UA claim of the metadata that the edits break.

    ``claim`` names it, such as ``"PDF/A-1B"`` or ``"PDF/UA-1"``, and
    ``objects`` lists the edited objects that break it. The report checks
    what the edits wrote, not the whole document against the claim: added
    transparency and embedded files break PDF/A-1, an embedded file that is
    not PDF/A-1 or PDF/A-2 breaks PDF/A-2, one that is not PDF/A-1, -2, or -4
    breaks PDF/A-4 but not PDF/A-4f, and untagged annotations break PDF/UA.
    """

    claim: str
    reason: str
    objects: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ChangeReport:
    """What the edits since opening invalidate, for a save that is incremental or not."""

    signatures: tuple[SignatureChange, ...]
    claims: tuple[ClaimChange, ...]
    permissions: tuple[SignaturePermission, ...] = ()

    def __bool__(self) -> bool:
        """Return whether the report names anything."""
        return bool(self.signatures or self.claims or self.permissions)


def changes(document: Document, *, incremental: bool) -> ChangeReport:
    """Return the report of ``document``'s edits since opening; see :meth:`Document.changes`."""
    if not isinstance(incremental, bool):
        msg = f"incremental must be a bool: {incremental!r}"
        raise TypeError(msg)
    signatures, claims, permissions = _core.changes(document._core(), incremental)  # noqa: SLF001
    return ChangeReport(
        tuple(SignatureChange(name, xref, reason, tuple(objects)) for name, xref, reason, objects in signatures),
        tuple(ClaimChange(name, reason, tuple(objects)) for name, _, reason, objects in claims),
        tuple(SignaturePermission(*row) for row in permissions),
    )
