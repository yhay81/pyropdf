"""The change report: what the edits since opening invalidate before a save."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyropdf import _core

if TYPE_CHECKING:
    from pyropdf._document import Document


@dataclass(frozen=True, slots=True)
class SignatureChange:
    """A signature of the opened file that the edits or the save mode invalidate.

    ``field`` is the signature field's full name and ``xref`` its object.
    ``objects`` lists the edited objects that the signature signs, empty when
    the save mode alone invalidates it.
    """

    field: str
    xref: int
    reason: str
    objects: tuple[int, ...]


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

    def __bool__(self) -> bool:
        """Return whether the report names anything."""
        return bool(self.signatures or self.claims)


def changes(document: Document, *, incremental: bool) -> ChangeReport:
    """Return the report of ``document``'s edits since opening; see :meth:`Document.changes`."""
    if not isinstance(incremental, bool):
        msg = f"incremental must be a bool: {incremental!r}"
        raise TypeError(msg)
    signatures, claims = _core.changes(document._core(), incremental)  # noqa: SLF001
    return ChangeReport(
        tuple(SignatureChange(name, xref, reason, tuple(objects)) for name, xref, reason, objects in signatures),
        tuple(ClaimChange(name, reason, tuple(objects)) for name, _, reason, objects in claims),
    )
