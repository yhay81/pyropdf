"""Tagged PDF enums, marked-content scopes, and structure-tree attachment."""

from __future__ import annotations

import enum
import re
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, cast

from pyropdf import _core
from pyropdf._objects import Array, Dictionary, Name, _encode

if TYPE_CHECKING:
    from types import TracebackType

    from pyropdf._canvas._canvas import Canvas
    from pyropdf._document import Document
    from pyropdf._geometry import Matrix
    from pyropdf._page import Page

#: The shape of a BCP 47 language tag, and its longest form.
_LANGUAGE_TAG = re.compile(r"[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*", re.ASCII)
_MAX_LANGUAGE_TAG = 256


class TagRole(enum.StrEnum):
    """A structure type (ISO 32000-1 section 14.8.4), by its PDF name.

    :attr:`ARTIFACT` marks content that is not part of the document's
    logical structure, such as page headers, footers, and decoration.
    """

    PART = "Part"
    ARTICLE = "Art"
    SECTION = "Sect"
    DIV = "Div"
    BLOCK_QUOTE = "BlockQuote"
    CAPTION = "Caption"
    TOC = "TOC"
    TOC_ITEM = "TOCI"
    INDEX = "Index"
    NON_STRUCT = "NonStruct"
    PARAGRAPH = "P"
    HEADING_1 = "H1"
    HEADING_2 = "H2"
    HEADING_3 = "H3"
    HEADING_4 = "H4"
    HEADING_5 = "H5"
    HEADING_6 = "H6"
    LIST = "L"
    LIST_ITEM = "LI"
    LABEL = "Lbl"
    LIST_BODY = "LBody"
    TABLE = "Table"
    TABLE_ROW = "TR"
    TABLE_HEADER = "TH"
    TABLE_DATA = "TD"
    TABLE_HEAD = "THead"
    TABLE_BODY = "TBody"
    TABLE_FOOT = "TFoot"
    SPAN = "Span"
    QUOTE = "Quote"
    NOTE = "Note"
    REFERENCE = "Reference"
    BIB_ENTRY = "BibEntry"
    CODE = "Code"
    FIGURE = "Figure"
    FORMULA = "Formula"
    ARTIFACT = "Artifact"
    LINK = "Link"


class TableHeaderScope(enum.StrEnum):
    """What a table header cell heads (ISO 32000-1 section 14.8.5.7), by its PDF name."""

    ROW = "Row"
    COLUMN = "Column"
    BOTH = "Both"


class ListNumbering(enum.StrEnum):
    """How a list numbers its items (ISO 32000-1 section 14.8.5.5), by its PDF name."""

    NONE = "None"
    DISC = "Disc"
    CIRCLE = "Circle"
    SQUARE = "Square"
    DECIMAL = "Decimal"
    LOWER_ROMAN = "LowerRoman"
    UPPER_ROMAN = "UpperRoman"
    LOWER_ALPHA = "LowerAlpha"
    UPPER_ALPHA = "UpperAlpha"


def _tag_attribute(
    value: object,
    kind: type[enum.StrEnum],
    name: str,
    role: TagRole,
    takes: TagRole,
) -> str | None:
    """Return the PDF name of a role-specific tag attribute, refusing it on other roles."""
    if value is None:
        return None
    if role is not takes:
        msg = f"only {takes.name} tags take {name}: {role.name}"
        raise ValueError(msg)
    try:
        return kind(value).value
    except ValueError as exc:
        msg = f"{name} must be a {kind.__name__}: {value!r}"
        raise ValueError(msg) from exc


class _Element:
    """A structure element that a tag scope opened: its role, its entries, and its kids in order.

    A kid is a marked-content identifier of the canvas's content or a child element.
    """

    __slots__ = ("entries", "kids", "role")

    def __init__(self, role: str, entries: dict[str, object]) -> None:
        self.role = role
        self.entries = entries
        self.kids: list[int | _Element] = []


class _Scope(AbstractContextManager[None]):
    """One clip, transform, opacity, blend, or layer scope of a canvas."""

    def __init__(
        self,
        canvas: Canvas,
        opening: bytes,
        closing: bytes,
        *,
        group: float | None = None,
        matrix: Matrix | None = None,
    ) -> None:
        self._canvas = canvas
        self._opening = opening
        self._closing = closing
        self._group = group
        self._matrix = matrix

    def __enter__(self) -> None:
        self._canvas._push(self._opening, self._closing, group=self._group, matrix=self._matrix)  # noqa: SLF001

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._canvas._pop()  # noqa: SLF001


class _TagScope(AbstractContextManager[None]):
    """One tag scope of a canvas: a structure element, or ``None`` for an artifact."""

    def __init__(self, canvas: Canvas, element: _Element | None) -> None:
        self._canvas = canvas
        self._element = element

    def __enter__(self) -> None:
        self._canvas._push_tag(self._element)  # noqa: SLF001

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._canvas._pop_tag()  # noqa: SLF001


def _tag_scope(  # noqa: PLR0913 - the entries of a structure element.
    canvas: Canvas,
    role: TagRole | str,
    *,
    alt: str | None,
    lang: str | None,
    title: str | None,
    actual_text: str | None,
    scope: TableHeaderScope | str | None,
    numbering: ListNumbering | str | None,
) -> _TagScope:
    """Validate a structure tag's role and attributes and return its scope."""
    try:
        tag_role = TagRole(role)
    except ValueError as exc:
        msg = f"role must be a TagRole: {role!r}"
        raise ValueError(msg) from exc
    texts = {"Alt": alt, "Lang": lang, "T": title, "ActualText": actual_text}
    for key, value in texts.items():
        if value is not None and not isinstance(value, str):
            msg = f"{key} must be a string or None: {value!r}"
            raise TypeError(msg)
    if lang is not None and (len(lang) > _MAX_LANGUAGE_TAG or _LANGUAGE_TAG.fullmatch(lang) is None):
        msg = f"lang must be a BCP 47 language tag: {lang!r}"
        raise ValueError(msg)
    header_scope = _tag_attribute(scope, TableHeaderScope, "scope", tag_role, TagRole.TABLE_HEADER)
    list_numbering = _tag_attribute(numbering, ListNumbering, "numbering", tag_role, TagRole.LIST)
    if tag_role is TagRole.ARTIFACT:
        return _TagScope(canvas, None)
    entries: dict[str, object] = {key: value for key, value in texts.items() if value is not None}
    if header_scope is not None:
        entries["A"] = {"O": Name("Table"), "Scope": Name(header_scope)}
    if list_numbering is not None:
        entries["A"] = {"O": Name("List"), "ListNumbering": Name(list_numbering)}
    return _TagScope(canvas, _Element(tag_role.value, entries))


def _top_element(root: Dictionary) -> Dictionary:
    """Return the element that new elements join: the root's lone ``Document`` element, or the root."""
    kids = root.get("K")
    if isinstance(kids, Array) and len(kids) == 1:
        kids = kids[0]
    if isinstance(kids, Dictionary) and kids.is_indirect and kids.get("S") == "Document":
        return kids
    return root


def _add_parent_tree_entry(document: Document, owners: list[Dictionary | None]) -> int:
    """Add the elements that own a Form's MCIDs to the root's parent tree under a new key; return the key."""
    return _core.parent_tree_append(document._core(), _encode(document, owners, 1))  # noqa: SLF001


def _write_structure(page: Page, elements: list[_Element], mcids: int, form: int) -> int:
    """Add ``elements`` to the document's structure tree and return the Form's parent tree key.

    A document without a structure tree gets one. The elements join its
    ``Document`` element when the root holds that one element only, and
    the root otherwise.
    """
    document = page.document
    objects = document.objects
    catalog = document.catalog
    root = catalog.get("StructTreeRoot")
    if not isinstance(root, Dictionary):
        catalog["StructTreeRoot"] = objects[objects.add({"Type": Name("StructTreeRoot"), "K": []})]
        root = cast("Dictionary", catalog["StructTreeRoot"])
        if not isinstance(catalog.get("MarkInfo"), Dictionary):
            catalog["MarkInfo"] = {}
        catalog["MarkInfo"]["Marked"] = True
    holder = _top_element(root)
    form_ref = objects[form]
    # Write each element, parents first, then their kids: MCIDs as marked-content references.
    nodes: dict[int, Dictionary] = {}
    owners: list[Dictionary | None] = [None] * mcids
    pending: list[tuple[_Element, Dictionary]] = [(element, holder) for element in reversed(elements)]
    written: list[_Element] = []
    while pending:
        element, parent = pending.pop()
        value: dict[str, object] = {"Type": Name("StructElem"), "S": Name(element.role), "P": parent}
        value["Pg"] = page.obj
        value.update(element.entries)
        node = objects[objects.add(value)]
        nodes[id(element)] = node
        written.append(element)
        pending.extend((kid, node) for kid in reversed(element.kids) if isinstance(kid, _Element))
    for element in written:
        node = nodes[id(element)]
        values: list[object] = []
        for kid in element.kids:
            if isinstance(kid, _Element):
                values.append(nodes[id(kid)])
            else:
                owners[kid] = node
                values.append({"Type": Name("MCR"), "Pg": page.obj, "Stm": form_ref, "MCID": kid})
        node["K"] = values
    held = holder.get("K")
    if not isinstance(held, Array):
        holder["K"] = [] if held is None else [held]
        held = cast("Array", holder["K"])
    held.extend(nodes[id(element)] for element in elements)
    return _add_parent_tree_entry(document, owners)


for _public in (ListNumbering, TableHeaderScope, TagRole):
    _public.__module__ = "pyropdf"
