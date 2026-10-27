"""The interactive form: ``Document.form``, its ``Field`` handles, and their enums (DESIGN.md §10).

One walk of the field tree reads every field, and it is memoized with the
objects that it read, so reading every field of a form costs about one walk.
"""

from __future__ import annotations

import enum
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyropdf import _core
from pyropdf._errors import PdfError, StaleObjectError
from pyropdf._objects import Dictionary
from pyropdf._page import Page
from pyropdf._widgets import Widget

if TYPE_CHECKING:
    from pyropdf._core import _Document
    from pyropdf._document import Document

#: The deepest field tree that the core's walk reads.
_MAX_FIELD_DEPTH = 64


class FieldType(enum.StrEnum):
    """The kind of a form field, from its ``/FT`` and the flags that refine it."""

    TEXT = "text"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    PUSHBUTTON = "pushbutton"
    COMBOBOX = "combobox"
    LISTBOX = "listbox"
    SIGNATURE = "signature"
    UNKNOWN = "unknown"
    """A field whose ``/FT`` is missing or not one that ISO 32000-2 defines."""


class SignatureFlags(enum.IntFlag):
    """The signature flags of the AcroForm's ``/SigFlags`` (ISO 32000-2 Table 225), which signing tools set."""

    SIGNATURES_EXIST = 1
    """The document holds at least one signature field."""
    APPEND_ONLY = 2
    """The document holds signatures that a full rewrite would invalidate; save edits incrementally."""


class FieldFlags(enum.IntFlag):
    """The field flags of ``/Ff`` (ISO 32000-2 Tables 227, 229, 231, and 233).

    Bits 26 and 27 mean different things for buttons and for text and choice
    fields, so ``RADIOS_IN_UNISON`` is the same bit as ``RICH_TEXT``.
    """

    READ_ONLY = 1 << 0
    REQUIRED = 1 << 1
    NO_EXPORT = 1 << 2
    MULTILINE = 1 << 12
    PASSWORD = 1 << 13
    NO_TOGGLE_TO_OFF = 1 << 14
    RADIO = 1 << 15
    PUSHBUTTON = 1 << 16
    COMBO = 1 << 17
    EDIT = 1 << 18
    SORT = 1 << 19
    FILE_SELECT = 1 << 20
    MULTI_SELECT = 1 << 21
    DO_NOT_SPELL_CHECK = 1 << 22
    DO_NOT_SCROLL = 1 << 23
    COMB = 1 << 24
    RICH_TEXT = 1 << 25
    RADIOS_IN_UNISON = 1 << 25
    COMMIT_ON_SEL_CHANGE = 1 << 26


_DEFINED_FIELD_FLAGS = sum(flag.value for flag in FieldFlags)


@dataclass(frozen=True, slots=True)
class FieldChoice:
    """One option of a choice field's ``/Opt``."""

    value: str
    """The export value, which :attr:`Field.value` holds when the option is selected."""
    label: str
    """The text that a viewer shows, the export value unless the option names another."""


class Field:
    """A handle on one terminal form field, as ``doc.form[name]`` returns it.

    Properties read the document on every access. A handle names its field's
    object and raises :class:`StaleObjectError` while that object is no
    terminal field of the form; it works again if undo restores it.
    """

    __slots__ = ("_document", "_id")

    def __init__(self, document: Document, xref: int, generation: int) -> None:
        """Create a handle; :class:`Form` does this."""
        self._document = document
        self._id = (xref, generation)

    def _core(self) -> _Document:
        return self._document._core()  # noqa: SLF001

    def _read(self) -> tuple[str, str, int, str | None]:
        return _core.form_field_read(self._core(), self._id)

    @property
    def xref(self) -> int:
        """Return the field dictionary's object number."""
        return self._id[0]

    @property
    def obj(self) -> Dictionary:
        """Return the field dictionary in the low-level object model."""
        self._read()
        return Dictionary(self._document, self._id)

    @property
    def name(self) -> str:
        """Return the fully qualified name, the partial names of the field and its ancestors joined by dots."""
        return self._read()[0]

    @property
    def type(self) -> FieldType:
        """Return the field's kind."""
        return FieldType(self._read()[1])

    @property
    def flags(self) -> FieldFlags:
        """Return the field flags, inherited from ancestors that set ``/Ff``; undefined bits are dropped."""
        return FieldFlags(self._read()[2] & _DEFINED_FIELD_FLAGS)

    @property
    def choices(self) -> tuple[FieldChoice, ...]:
        """Return a choice field's options in ``/Opt`` order, or ``()`` without them.

        ``/Opt`` is inherited from the fields above. Reads stop at 4,096
        options or 1 MiB of option text.
        """
        return tuple(
            FieldChoice(value=value, label=label) for value, label in _core.form_field_choices(self._core(), self._id)
        )

    @property
    def is_signed(self) -> bool:
        """Return whether this is a signature field whose value is a signature dictionary.

        pyropdf does not verify signatures; a signing tool such as pyHanko
        signs and validates them.
        """
        if self.type is not FieldType.SIGNATURE:
            return False
        node: Any = self.obj
        # /V is inheritable (ISO 32000-2 Table 226); the walk ends at the
        # depth that the field-tree walk accepts.
        for _ in range(_MAX_FIELD_DEPTH):
            if not isinstance(node, Dictionary):
                return False
            value = node.get("V")
            if value is not None:
                return isinstance(value, Dictionary)
            node = node.get("Parent")
        return False

    @property
    def widgets(self) -> list[Widget]:
        """Return the widgets that show the field, in page order, each a :class:`Widget` handle."""
        document = self._document
        return [
            Widget(Page(document, *page), widget, index)
            for page, widget, index in _core.form_field_widgets(self._core(), self._id)
        ]

    @property
    def value(self) -> str | None:
        """Return the field's value.

        A text or choice field holds its text, a multiple selection joined
        by commas; a check box or radio button holds the appearance state of
        the selected widget, such as ``"Yes"``, or ``"Off"``. ``None`` means
        the field has no ``/V``.

        Setting a text or choice field writes its value and draws the
        appearances of its widgets in a Standard 14 font, with those of the
        other text and choice fields that have none yet; the form's
        ``/NeedAppearances`` stays true while a widget is left without one.
        A value that WinAnsiEncoding cannot show leaves the widgets for the
        viewer to draw. Setting a check box or radio button to a state name
        selects that state in each widget that has it, or else in the first
        widget, and turns the others ``Off``; ``True`` names the first state
        that is not ``Off``, or ``"Yes"``, and ``False`` names ``"Off"``. The
        widgets get the ``Off`` and selected states that they lack, as a
        check mark or a dot, and the button states that other fields lack
        are drawn too.
        """
        return self._read()[3]

    @value.setter
    def value(self, value: str | bool) -> None:
        if not isinstance(value, (str, bool)):
            msg = f"a field value is a str or bool: {value!r}"
            raise TypeError(msg)
        _core.form_field_fill(self._core(), self._id, value)

    def __eq__(self, other: object) -> bool:
        """Compare document and object."""
        if not isinstance(other, Field):
            return NotImplemented
        return self._document is other._document and self._id == other._id

    def __hash__(self) -> int:
        """Hash document and object."""
        return hash((id(self._document), self._id))

    def __repr__(self) -> str:
        """Return ``<Field 'name' 12>``, or ``<Field 12>`` once stale."""
        try:
            name = self.name
        except (PdfError, StaleObjectError):
            return f"<Field {self._id[0]}>"
        return f"<Field {name!r} {self._id[0]}>"


class Form(Mapping[str, Field]):
    """The terminal fields of a document's interactive form, as ``doc.form`` returns them.

    Keys are fully qualified field names in sorted order; when malformed
    files give two fields one name, the mapping holds the first by object
    number. A document without a form is an empty mapping. Reads refuse
    more than 4,096 fields or tree nodes, 8,192 edges, 64 levels, 4,096
    widgets, 1 MiB of names or values, or 4,096 value items with
    :class:`PdfError` rather than return part of the form.
    """

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Wrap a document; use ``Document.form``."""
        self._document = document

    def _core(self) -> _Document:
        return self._document._core()  # noqa: SLF001

    def _names(self) -> list[str]:
        """Return the distinct field names in sorted order."""
        names: list[str] = []
        for name, _ in _core.form_fields(self._core()):
            if not names or names[-1] != name:
                names.append(name)
        return names

    def repair_radio_groups(self) -> int:
        """Gather top-level radio buttons that share a name under one field, and return how many groups it made.

        Some producers write each button of a radio group as a field of its
        own with the group's name, so the form lists only one of them and
        selecting a button does not clear the others. Each such set becomes
        the widgets of one new radio field of that name, which takes the
        group's value.
        """
        return _core.form_repair_radio_groups(self._core())

    @property
    def signature_flags(self) -> SignatureFlags:
        """Return the AcroForm's ``/SigFlags``, without undefined bits; a document without a form reads none."""
        form = self._document.catalog.get("AcroForm")
        flags = form.get("SigFlags") if isinstance(form, Dictionary) else None
        if not isinstance(flags, int) or isinstance(flags, bool):
            return SignatureFlags(0)
        return SignatureFlags(flags & (SignatureFlags.SIGNATURES_EXIST | SignatureFlags.APPEND_ONLY))

    @property
    def need_appearances(self) -> bool:
        """Return the AcroForm's ``/NeedAppearances``, which asks viewers to draw the widgets again.

        A document without fields reads ``False``.
        """
        if not _core.form_fields(self._core()):
            return False
        form = self._document.catalog.get("AcroForm")
        return isinstance(form, Dictionary) and form.get("NeedAppearances") is True

    def __getitem__(self, name: str) -> Field:
        """Return the field named ``name``; another name raises :class:`KeyError`."""
        if not isinstance(name, str):
            raise KeyError(name)
        found = _core.form_field(self._core(), name)
        if found is None:
            raise KeyError(name)
        return Field(self._document, *found)

    def __iter__(self) -> Iterator[str]:
        """Iterate over the field names in sorted order."""
        return iter(self._names())

    def __len__(self) -> int:
        """Return the number of distinct field names."""
        return len(self._names())

    def __repr__(self) -> str:
        """Return ``Form([...])`` with the field names."""
        return f"Form({self._names()!r})"


# The package re-exports these names; reprs, pickles, and documentation name them.
for _public in (Field, FieldChoice, FieldFlags, FieldType, Form, SignatureFlags):
    _public.__module__ = "pyropdf"
