"""Sanitizing: remove hidden or active content and write the document as one revision.

``doc.sanitize(path)`` removes each chosen kind of content in one
transaction, reports the signatures and PDF/A or PDF/UA claims that the
result breaks, and saves the document in full, so the file has no
incremental history; with ``unused=True`` the save writes only the objects
that the trailer reaches (ISO 32000-2 section 7.5.6).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from pyropdf._errors import LimitError
from pyropdf._objects import Array, Dictionary, Name, Stream
from pyropdf._operations import Operation, Operations

if TYPE_CHECKING:
    import os
    from collections.abc import Iterator, Mapping

    from pyropdf._changes import ChangeReport
    from pyropdf._document import Document
    from pyropdf._operations import InlineImage

#: Actions of one ``/Next`` chain that a check follows.
_MAX_ACTION_CHAIN = 256
#: Annotation subtypes that are comments: the markup annotations (ISO 32000-2 12.5.6.2) and their pop-ups.
_COMMENTS = frozenset(
    {
        "Text",
        "FreeText",
        "Line",
        "Square",
        "Circle",
        "Polygon",
        "PolyLine",
        "Highlight",
        "Underline",
        "Squiggly",
        "StrikeOut",
        "Caret",
        "Ink",
        "Stamp",
        "FileAttachment",
        "Sound",
        "Redact",
        "Projection",
        "Popup",
    }
)
#: Text rendering mode 3 neither fills nor strokes (ISO 32000-2 table 104).
_INVISIBLE = 3
#: Text-showing operators; ``'`` and ``"`` also move to the next line.
_SHOW = frozenset({"Tj", "TJ", "'", '"'})
#: Form XObjects, appearance streams included, that hidden text removal visits.
_MAX_FORMS = 4096
#: Levels of the page tree that a page's inherited resources are looked up through.
_MAX_TREE_DEPTH = 64
#: A form's entry mode before any use of it is seen.
_UNSEEN = -1
#: The order in which the kinds are removed and reported.
KINDS = ("javascript", "actions", "attachments", "metadata", "comments", "hidden_text")


@dataclass(frozen=True, slots=True)
class SanitizeReport:
    """What :meth:`Document.sanitize` removed, and what the result invalidates."""

    removed: Mapping[str, int]
    """For each chosen kind, the entries, annotations, or text operations removed."""
    changes: ChangeReport
    """The signatures and profile claims that the sanitized, fully saved document breaks."""
    dropped_claims: tuple[str, ...]
    """The PDF/A, PDF/UA, PDF/X, and PDF/VT claims that the document no longer makes, such as ``"PDF/A-2B"``."""


def _dictionaries(document: Document) -> Iterator[Dictionary]:
    """Yield every indirect dictionary and stream of the document's current version, then each direct annotation."""
    objects = document.objects
    for number in objects:
        value = objects.get(number)
        if isinstance(value, Dictionary):
            yield value
    for page in document.pages:
        annotations = page.obj.get("Annots")
        if isinstance(annotations, Array):
            yield from (item for item in annotations if isinstance(item, Dictionary) and item.xref is None)


def _claims(document: Document) -> tuple[str, ...]:
    profiles = document.profiles
    return tuple(claim for claim in (profiles.pdfa, profiles.pdfua, profiles.pdfx, profiles.pdfvt) if claim)


def _chain(action: object) -> Iterator[Dictionary]:
    """Yield action ``action`` and the actions of its ``/Next`` entries, bounded and without repeats."""
    pending = [action]
    seen: set[int] = set()
    count = 0
    while pending:
        item = pending.pop()
        if isinstance(item, Array):
            pending.extend(item)
            continue
        if not isinstance(item, Dictionary):
            continue
        if item.xref is not None:
            if item.xref in seen:
                continue
            seen.add(item.xref)
        count += 1
        if count > _MAX_ACTION_CHAIN:
            msg = f"an action chain has more than {_MAX_ACTION_CHAIN} actions"
            code = "sanitize_action_chain"
            raise LimitError(code, msg)
        yield item
        if "Next" in item:
            pending.append(item["Next"])


def _has_javascript(action: object) -> bool:
    return any(item.get("S") == "JavaScript" for item in _chain(action))


def _remove_actions(document: Document, *, javascript_only: bool) -> int:
    """Remove ``/A``, ``/OpenAction``, and ``/AA`` entries, or only those whose chain runs JavaScript."""

    def doomed(action: object) -> bool:
        if javascript_only:
            return _has_javascript(action)
        # A plain destination, an array, is no action.
        return isinstance(action, Dictionary)

    removed = 0
    for dictionary in _dictionaries(document):
        for key in ("A", "OpenAction"):
            if key in dictionary and doomed(dictionary[key]):
                del dictionary[key]
                removed += 1
        triggers = dictionary.get("AA")
        if isinstance(triggers, Dictionary):
            for trigger in [key for key in triggers if doomed(triggers[key])]:
                del triggers[trigger]
                removed += 1
            if not len(triggers):
                del dictionary["AA"]
    names = document.catalog.get("Names")
    if isinstance(names, Dictionary) and "JavaScript" in names:
        del names["JavaScript"]
        removed += 1
    return removed


def _remove_attachments(document: Document) -> int:
    removed = 0
    catalog = document.catalog
    names = catalog.get("Names")
    if isinstance(names, Dictionary) and "EmbeddedFiles" in names:
        del names["EmbeddedFiles"]
        removed += 1
    if "Collection" in catalog:
        del catalog["Collection"]
        removed += 1
    for dictionary in _dictionaries(document):
        if "AF" in dictionary:
            del dictionary["AF"]
            removed += 1
    return removed + _remove_annotations(document, frozenset({"FileAttachment"}))


def _remove_metadata(document: Document) -> int:
    removed = 0
    trailer = document.trailer
    if "Info" in trailer:
        del trailer["Info"]
        removed += 1
    for dictionary in _dictionaries(document):
        for key in ("Metadata", "PieceInfo"):
            if key in dictionary:
                del dictionary[key]
                removed += 1
    return removed


def _remove_annotations(document: Document, subtypes: frozenset[str]) -> int:
    removed = 0
    for page in document.pages:
        annotations = page.obj.get("Annots")
        if not isinstance(annotations, Array):
            continue
        kept = [item for item in annotations if not (isinstance(item, Dictionary) and item.get("Subtype") in subtypes)]
        if len(kept) == len(annotations):
            continue
        removed += len(annotations) - len(kept)
        if kept:
            page.obj["Annots"] = kept
        else:
            del page.obj["Annots"]
    return removed


def _visible_operations(
    operations: Operations, mode: int | None
) -> tuple[list[Operation | InlineImage], int, list[tuple[str, int | None]]]:
    """Return ``operations`` without those that show text in rendering mode 3, how many went, and each ``Do``.

    ``mode`` is the rendering mode where the stream starts, ``None`` when
    it is painted in different modes; text shown before the stream sets a
    mode of its own then stays. Each ``Do`` comes with the mode it paints in.
    """
    kept: list[Operation | InlineImage] = []
    removed = 0
    painted: list[tuple[str, int | None]] = []
    saved: list[int | None] = []
    for operation in operations:
        operator = operation.operator
        if operator == "q":
            saved.append(mode)
        elif operator == "Q" and saved:
            mode = saved.pop()
        elif operator == "Tr" and isinstance(operation, Operation) and operation.operands:
            value = operation.operands[0]
            mode = value if isinstance(value, int) and not isinstance(value, bool) else mode
        elif operator == "Do" and isinstance(operation, Operation) and operation.operands:
            name = operation.operands[0]
            if isinstance(name, Name):
                painted.append((name, mode))
        elif operator in _SHOW and mode == _INVISIBLE:
            removed += 1
            # Keep the line move and spacing of ' and ".
            if operator == '"' and len(operation.operands) >= 2:  # noqa: PLR2004 - aw ac string
                kept += [Operation("Tw", (operation.operands[0],)), Operation("Tc", (operation.operands[1],))]
            if operator in ("'", '"'):
                kept.append(Operation("T*", ()))
            continue
        kept.append(operation)
    return kept, removed, painted


def _page_resources(page: Dictionary) -> Dictionary | None:
    """Return the resources of ``page``, inherited through the page tree."""
    node: object = page
    for _ in range(_MAX_TREE_DEPTH):
        if not isinstance(node, Dictionary):
            return None
        resources = node.get("Resources")
        if isinstance(resources, Dictionary):
            return resources
        node = node.get("Parent")
    return None


class _Forms:
    """The form XObjects that hidden text removal visits, and the rendering mode each is painted in.

    A form's entry mode only moves from unseen to one mode to mixed
    (``None``), so a form is read at most three times and cycles end.
    """

    def __init__(self) -> None:
        self.streams: dict[int, Stream] = {}
        self.entries: dict[int, int | None] = {}
        # A form without resources of its own uses those of the stream that paints it.
        self.resources: dict[int, Dictionary | None] = {}
        self.pending: list[int] = []

    def enter(self, form: Stream, mode: int | None, resources: Dictionary | None) -> None:
        xref = form.xref
        if xref is None:
            return
        if xref not in self.streams:
            if len(self.streams) >= _MAX_FORMS:
                msg = f"hidden text removal reaches more than {_MAX_FORMS} form XObjects"
                code = "sanitize_forms"
                raise LimitError(code, msg)
            self.streams[xref] = form
            self.entries[xref] = _UNSEEN
            own = form.get("Resources")
            self.resources[xref] = own if isinstance(own, Dictionary) else resources
        entry = self.entries[xref]
        merged = mode if entry == _UNSEEN else (entry if entry == mode else None)
        if merged != entry:
            self.entries[xref] = merged
            self.pending.append(xref)

    def paint(self, painted: list[tuple[str, int | None]], resources: Dictionary | None) -> None:
        """Enter the forms that ``Do`` operations paint from ``resources``."""
        forms = resources.get("XObject") if isinstance(resources, Dictionary) else None
        if not isinstance(forms, Dictionary):
            return
        for name, mode in painted:
            form = forms.get(name)
            if isinstance(form, Stream) and form.get("Subtype") == "Form":
                self.enter(form, mode, resources)


def _appearances(annotation: object) -> Iterator[Stream]:
    """Yield the appearance streams of ``annotation``, every state of each kind."""
    appearance = annotation.get("AP") if isinstance(annotation, Dictionary) else None
    if not isinstance(appearance, Dictionary):
        return
    for kind in appearance:
        value = appearance[kind]
        if isinstance(value, Stream):
            yield value
        elif isinstance(value, Dictionary):
            yield from (state for key in value if isinstance(state := value[key], Stream))


def _remove_hidden_text(document: Document) -> int:
    """Remove text shown in rendering mode 3 from pages and from the forms that pages and appearances paint."""
    removed = 0
    forms = _Forms()
    for page in document.pages:
        kept, count, painted = _visible_operations(page.operations, 0)
        if count:
            page.operations = kept
            removed += count
        forms.paint(painted, _page_resources(page.obj))
        annotations = page.obj.get("Annots")
        for annotation in annotations if isinstance(annotations, Array) else ():
            for stream in _appearances(annotation):
                forms.enter(stream, 0, None)
    while forms.pending:
        xref = forms.pending.pop()
        operations = Operations.parse(forms.streams[xref].read())
        _, _, painted = _visible_operations(operations, forms.entries[xref])
        forms.paint(painted, forms.resources[xref])
    # Every entry mode is final now: rewrite each form once.
    for xref, stream in forms.streams.items():
        kept, count, _ = _visible_operations(Operations.parse(stream.read()), forms.entries[xref])
        if count:
            stream.write(Operations(kept).to_bytes())
            removed += count
    return removed


def sanitize(  # noqa: PLR0913 - one switch per kind
    document: Document,
    path: str | os.PathLike[str],
    *,
    javascript: bool,
    actions: bool,
    attachments: bool,
    metadata: bool,
    comments: bool,
    hidden_text: bool,
    unused: bool,
) -> SanitizeReport:
    """Remove the chosen kinds in one transaction, then save the document in full to ``path``."""
    chosen = {
        "javascript": javascript,
        "actions": actions,
        "attachments": attachments,
        "metadata": metadata,
        "comments": comments,
        "hidden_text": hidden_text,
        "unused": unused,
    }
    for name, value in chosen.items():
        if not isinstance(value, bool):
            msg = f"{name} must be a bool: {value!r}"
            raise TypeError(msg)
    removed: dict[str, int] = {}
    claims = _claims(document)
    with document.transaction("sanitize"):
        if javascript:
            removed["javascript"] = _remove_actions(document, javascript_only=True)
        if actions:
            removed["actions"] = _remove_actions(document, javascript_only=False)
        if attachments:
            removed["attachments"] = _remove_attachments(document)
        if metadata:
            removed["metadata"] = _remove_metadata(document)
        if comments:
            removed["comments"] = _remove_annotations(document, _COMMENTS)
        if hidden_text:
            removed["hidden_text"] = _remove_hidden_text(document)
    kept = set(_claims(document))
    report = SanitizeReport(
        MappingProxyType(removed),
        document.changes(incremental=False),
        tuple(claim for claim in claims if claim not in kept),
    )
    document.save(path, prune=unused)
    return report


# The package re-exports this name; reprs, pickles, and documentation name it.
SanitizeReport.__module__ = "pyropdf"
