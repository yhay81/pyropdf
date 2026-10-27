"""The interactive form: ``Document.form``, its ``Field`` handles, and their enums (DESIGN.md §10).

One walk of the field tree reads every field, and it is memoized with the
objects that it read, so reading every field of a form costs about one walk.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeAlias

from pyropdf import _core
from pyropdf._annotations import _BORDER_STYLE_NAMES, Border, _opaque_color, _rect
from pyropdf._content import Color
from pyropdf._errors import LimitError, PdfError, StaleObjectError
from pyropdf._geometry import Rect
from pyropdf._objects import Dictionary
from pyropdf._page import Page
from pyropdf._widgets import Widget

if TYPE_CHECKING:
    from pyropdf._core import _Document
    from pyropdf._document import Document

#: The deepest field tree that the core's walk reads.
_MAX_FIELD_DEPTH = 64
#: The most options that a choice field takes.
_MAX_FIELD_CHOICES = 4096
#: The longest maximum length that a text field takes.
_MAX_FIELD_LENGTH = 1_000_000
#: The most UTF-8 bytes of text that a field property or specification takes.
_MAX_FIELD_INPUT_SIZE = 1024 * 1024


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

    def _edit(self, *, clear: list[str] | None = None, **entries: object) -> None:
        """Change the field in one edit and draw its widgets again."""
        _core.form_field_edit(self._core(), self._id, {"clear": clear or [], **entries})

    @property
    def flags(self) -> FieldFlags:
        """Return or set the field flags, inherited from ancestors that set ``/Ff``; undefined bits are dropped.

        Assigning writes the field's own ``/Ff``; the bits that decide its kind
        (``RADIO``, ``PUSHBUTTON``, and ``COMBO``) cannot change.
        """
        return FieldFlags(self._read()[2] & _DEFINED_FIELD_FLAGS)

    @flags.setter
    def flags(self, flags: FieldFlags) -> None:
        if isinstance(flags, bool) or not isinstance(flags, int):
            msg = f"flags is a FieldFlags value: {flags!r}"
            raise TypeError(msg)
        self._edit(flags=int(flags))

    @property
    def choices(self) -> tuple[FieldChoice, ...]:
        """Return or set a choice field's options in ``/Opt`` order, or ``()`` without them.

        ``/Opt`` is inherited from the fields above. Reads stop at 4,096
        options or 1 MiB of option text. Assigning takes ``FieldChoice``
        records or strings, at most 4,096, and writes the field's own
        ``/Opt``.
        """
        return tuple(
            FieldChoice(value=value, label=label) for value, label in _core.form_field_choices(self._core(), self._id)
        )

    @choices.setter
    def choices(self, choices: Iterable[FieldChoice | str]) -> None:
        if isinstance(choices, (str, bytes)) or not isinstance(choices, Iterable):
            msg = f"choices is an iterable of FieldChoice records or strings: {choices!r}"
            raise TypeError(msg)
        pairs: list[tuple[str, str]] = []
        for choice in choices:
            if len(pairs) >= _MAX_FIELD_CHOICES:
                msg = f"a choice field has at most {_MAX_FIELD_CHOICES} choices"
                raise ValueError(msg)
            if isinstance(choice, str):
                pairs.append((choice, choice))
            elif isinstance(choice, FieldChoice):
                pairs.append((choice.value, choice.label))
            else:
                msg = f"a choice is a FieldChoice or a str: {choice!r}"
                raise TypeError(msg)
        self._edit(choices=pairs)

    def _details(self) -> tuple[str | None, int | None, str | None]:
        return _core.form_field_details(self._core(), self._id)

    @property
    def label(self) -> str | None:
        """Return or set the field's description for users, ``/TU``; ``del`` removes it."""
        return self._details()[0]

    @label.setter
    def label(self, label: str) -> None:
        self._edit(label=_field_text(label, "label"))

    @label.deleter
    def label(self) -> None:
        self._edit(clear=["label"])

    @property
    def max_length(self) -> int | None:
        """Return or set the most characters that a text field holds, ``/MaxLen``; ``del`` removes it.

        ``/MaxLen`` is inherited from the fields above.
        """
        return self._details()[1]

    @max_length.setter
    def max_length(self, length: int) -> None:
        if isinstance(length, bool) or not isinstance(length, int) or not 0 <= length <= _MAX_FIELD_LENGTH:
            msg = f"max_length is an int from 0 to {_MAX_FIELD_LENGTH}: {length!r}"
            raise ValueError(msg)
        self._edit(max_length=length)

    @max_length.deleter
    def max_length(self) -> None:
        self._edit(clear=["max_length"])

    @property
    def default_value(self) -> str | None:
        """Return or set the value that :meth:`reset` restores, ``/DV``; ``del`` removes it."""
        return self._details()[2]

    @default_value.setter
    def default_value(self, value: str) -> None:
        self._edit(default_value=_field_text(value, "default_value"))

    @default_value.deleter
    def default_value(self) -> None:
        self._edit(clear=["default_value"])

    def reset(self) -> None:
        """Restore the field's default value, or clear its value, and draw its widgets again.

        A check box or radio button without a default value turns off.
        """
        self._edit(reset=True)

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


def _field_text(value: object, name: str) -> str:
    """Check a field text property before the core copies it."""
    if not isinstance(value, str):
        msg = f"{name} must be a str: {type(value).__name__}"
        raise TypeError(msg)
    if len(value) * 4 > _MAX_FIELD_INPUT_SIZE and len(value.encode()) > _MAX_FIELD_INPUT_SIZE:
        code = "form_field_input_size"
        msg = f"field {name} exceeds the {_MAX_FIELD_INPUT_SIZE}-byte UTF-8 limit"
        raise LimitError(code, msg)
    return value


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
        """Return or set the AcroForm's ``/NeedAppearances``, which asks viewers to draw the widgets again.

        Filling a field draws its widgets and clears the flag once every
        widget that shows a value has an appearance. A document without
        fields reads ``False``, and one without a form refuses the
        assignment.
        """
        if not _core.form_fields(self._core()):
            return False
        form = self._document.catalog.get("AcroForm")
        return isinstance(form, Dictionary) and form.get("NeedAppearances") is True

    @need_appearances.setter
    def need_appearances(self, value: bool) -> None:
        if not isinstance(value, bool):
            msg = f"need_appearances is a bool: {value!r}"
            raise TypeError(msg)
        _core.form_set_need_appearances(self._core(), value)

    def __getitem__(self, name: str) -> Field:
        """Return the field named ``name``; another name raises :class:`KeyError`."""
        if not isinstance(name, str):
            raise KeyError(name)
        found = _core.form_field(self._core(), name)
        if found is None:
            raise KeyError(name)
        return Field(self._document, *found)

    def __delitem__(self, name: str) -> None:
        """Remove the field named ``name`` with its widgets, in one transaction; another name raises :class:`KeyError`.

        The widgets leave every page that lists them, and the field leaves
        the field tree and the calculation order with each named parent
        that it leaves without kids.
        """
        field = self[name]
        _core.form_field_remove(self._core(), field._id)  # noqa: SLF001

    def __iter__(self) -> Iterator[str]:
        """Iterate over the field names in sorted order."""
        return iter(self._names())

    def __len__(self) -> int:
        """Return the number of distinct field names."""
        return len(self._names())

    def __repr__(self) -> str:
        """Return ``Form([...])`` with the field names."""
        return f"Form({self._names()!r})"


#: The ``/Q`` code of each field text alignment.
_FIELD_ALIGNMENTS = ("left", "center", "right")


class _FieldSpecification:
    """The fields and validation that every field specification shares."""

    __slots__ = ()

    _kind: ClassVar[str]
    name: str
    rect: Rect
    label: str | None
    flags: FieldFlags
    border: Border
    border_color: Color | None
    background_color: Color | None

    def _common(  # noqa: PLR0913, PLR0917 - the shared fields, as each __init__ received them.
        self,
        name: object,
        rect: Sequence[float],
        label: object,
        flags: object,
        border: object,
        border_color: object,
        background_color: object,
    ) -> None:
        _field_text(name, "name")
        if label is not None:
            _field_text(label, "label")
        if isinstance(flags, bool) or not isinstance(flags, int):
            msg = f"flags is a FieldFlags value: {flags!r}"
            raise TypeError(msg)
        if not isinstance(border, Border):
            msg = f"border must be a Border: {border!r}"
            raise TypeError(msg)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "rect", Rect(*_rect(rect)))
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "flags", FieldFlags(flags))
        object.__setattr__(self, "border", border)
        object.__setattr__(
            self, "border_color", None if border_color is None else _opaque_color(border_color, "border_color")
        )
        object.__setattr__(
            self,
            "background_color",
            None if background_color is None else _opaque_color(background_color, "background_color"),
        )

    def _text_style(self, font_size: object, text_color: object, align: object) -> None:
        if isinstance(font_size, bool) or not isinstance(font_size, (int, float)) or not 0 <= font_size <= 10000:  # noqa: PLR2004
            msg = f"font_size is a number from 0, which fits the text, to 10000: {font_size!r}"
            raise ValueError(msg)
        if align not in _FIELD_ALIGNMENTS:
            msg = f"align must be one of {', '.join(_FIELD_ALIGNMENTS)}: {align!r}"
            raise ValueError(msg)
        object.__setattr__(self, "font_size", float(font_size))
        object.__setattr__(self, "text_color", _opaque_color(text_color, "text_color"))
        object.__setattr__(self, "align", align)

    def _payload(self) -> dict[str, Any]:
        border = self.border
        spec: dict[str, Any] = {
            "kind": self._kind,
            "name": self.name,
            "rect": tuple(self.rect),
            "label": self.label,
            "flags": int(self.flags),
            "border": (border.width, _BORDER_STYLE_NAMES[border.style], list(border.dashes)),
            "border_color": None if self.border_color is None else self.border_color.rgb,
            "background_color": None if self.background_color is None else self.background_color.rgb,
        }
        text_color: Color | None = getattr(self, "text_color", None)
        if text_color is not None:
            spec["text_color"] = text_color.rgb
            spec["font_size"] = getattr(self, "font_size")  # noqa: B009
            spec["align"] = _FIELD_ALIGNMENTS.index(getattr(self, "align"))  # noqa: B009
        return spec


@dataclass(frozen=True, slots=True, init=False)
class TextField(_FieldSpecification):
    """A text field, or another widget of the text field of that name, as ``page.widgets.add`` takes it.

    ``rect`` is in display coordinates. ``value`` and ``default_value`` are
    the field's text, drawn in Helvetica at ``font_size`` (0 fits the text),
    in ``text_color``, aligned by ``align``. ``flags`` adds
    :class:`FieldFlags` such as ``MULTILINE``, ``PASSWORD``, ``COMB``,
    ``REQUIRED``, or ``READ_ONLY``; ``max_length`` bounds the text.
    """

    _kind: ClassVar[str] = "text"
    name: str
    rect: Rect
    value: str | None
    default_value: str | None
    max_length: int | None
    font_size: float
    text_color: Color
    align: Literal["left", "center", "right"]
    label: str | None
    flags: FieldFlags
    border: Border
    border_color: Color | None
    background_color: Color | None

    def __init__(  # noqa: PLR0913 - the field's options.
        self,
        name: str,
        rect: Sequence[float],
        *,
        value: str | None = None,
        default_value: str | None = None,
        max_length: int | None = None,
        font_size: float = 0,
        text_color: Color = Color(0.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        align: Literal["left", "center", "right"] = "left",
        label: str | None = None,
        flags: FieldFlags = FieldFlags(0),  # noqa: B008 - FieldFlags is immutable.
        border: Border = Border(1),  # noqa: B008 - Border is immutable.
        border_color: Color | None = Color(0.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        background_color: Color | None = None,
    ) -> None:
        """Validate the specification."""
        self._common(name, rect, label, flags, border, border_color, background_color)
        self._text_style(font_size, text_color, align)
        for text, field in ((value, "value"), (default_value, "default_value")):
            if text is not None:
                _field_text(text, field)
        if max_length is not None and (
            isinstance(max_length, bool) or not isinstance(max_length, int) or not 0 <= max_length <= _MAX_FIELD_LENGTH
        ):
            msg = f"max_length is an int from 0 to {_MAX_FIELD_LENGTH}: {max_length!r}"
            raise ValueError(msg)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "default_value", default_value)
        object.__setattr__(self, "max_length", max_length)

    def _payload(self) -> dict[str, Any]:
        spec = _FieldSpecification._payload(self)  # noqa: SLF001 - slotted dataclasses cannot call super().
        spec.update(value=self.value, default_value=self.default_value, max_length=self.max_length)
        return spec


@dataclass(frozen=True, slots=True, init=False)
class CheckBox(_FieldSpecification):
    """A check box, as ``page.widgets.add`` takes it.

    ``on_state`` names the state that ``checked`` selects, ``"Yes"`` by
    default; the box draws a check mark in its border.
    """

    _kind: ClassVar[str] = "checkbox"
    name: str
    rect: Rect
    checked: bool
    on_state: str
    label: str | None
    flags: FieldFlags
    border: Border
    border_color: Color | None
    background_color: Color | None

    def __init__(  # noqa: PLR0913 - the field's options.
        self,
        name: str,
        rect: Sequence[float],
        *,
        checked: bool = False,
        on_state: str = "Yes",
        label: str | None = None,
        flags: FieldFlags = FieldFlags(0),  # noqa: B008 - FieldFlags is immutable.
        border: Border = Border(1),  # noqa: B008 - Border is immutable.
        border_color: Color | None = Color(0.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        background_color: Color | None = None,
    ) -> None:
        """Validate the specification."""
        self._common(name, rect, label, flags, border, border_color, background_color)
        if not isinstance(checked, bool):
            msg = f"checked is a bool: {checked!r}"
            raise TypeError(msg)
        _button_state(on_state)
        object.__setattr__(self, "checked", checked)
        object.__setattr__(self, "on_state", on_state)

    def _payload(self) -> dict[str, Any]:
        spec = _FieldSpecification._payload(self)  # noqa: SLF001 - slotted dataclasses cannot call super().
        spec.update(on_state=self.on_state, value=self.on_state if self.checked else None)
        return spec


@dataclass(frozen=True, slots=True, init=False)
class RadioButton(_FieldSpecification):
    """One button of a radio group, as ``page.widgets.add`` takes it.

    Buttons that share ``name`` form one group, each selecting its own
    ``on_state``; ``selected`` selects this one.
    """

    _kind: ClassVar[str] = "radio"
    name: str
    rect: Rect
    on_state: str
    selected: bool
    label: str | None
    flags: FieldFlags
    border: Border
    border_color: Color | None
    background_color: Color | None

    def __init__(  # noqa: PLR0913 - the field's options.
        self,
        name: str,
        rect: Sequence[float],
        *,
        on_state: str,
        selected: bool = False,
        label: str | None = None,
        flags: FieldFlags = FieldFlags(0),  # noqa: B008 - FieldFlags is immutable.
        border: Border = Border(1),  # noqa: B008 - Border is immutable.
        border_color: Color | None = Color(0.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        background_color: Color | None = None,
    ) -> None:
        """Validate the specification."""
        self._common(name, rect, label, flags, border, border_color, background_color)
        if not isinstance(selected, bool):
            msg = f"selected is a bool: {selected!r}"
            raise TypeError(msg)
        _button_state(on_state)
        object.__setattr__(self, "on_state", on_state)
        object.__setattr__(self, "selected", selected)

    def _payload(self) -> dict[str, Any]:
        spec = _FieldSpecification._payload(self)  # noqa: SLF001 - slotted dataclasses cannot call super().
        spec.update(on_state=self.on_state, value=self.on_state if self.selected else None)
        return spec


class _ChoiceSpecification(_FieldSpecification):
    """The fields of combo and list boxes."""

    __slots__ = ()

    def _choices(self, choices: object, value: object, default_value: object) -> None:
        if isinstance(choices, (str, bytes)) or not isinstance(choices, Iterable):
            msg = f"choices is an iterable of FieldChoice records or strings: {choices!r}"
            raise TypeError(msg)
        options: list[FieldChoice] = []
        for choice in choices:
            if len(options) >= _MAX_FIELD_CHOICES:
                msg = f"a choice field has at most {_MAX_FIELD_CHOICES} choices"
                raise ValueError(msg)
            if isinstance(choice, str):
                options.append(FieldChoice(value=choice, label=choice))
            elif isinstance(choice, FieldChoice):
                options.append(choice)
            else:
                msg = f"a choice is a FieldChoice or a str: {choice!r}"
                raise TypeError(msg)
        _field_text("".join(text for option in options for text in (option.value, option.label)), "choices")
        for text, field in ((value, "value"), (default_value, "default_value")):
            if text is not None:
                _field_text(text, field)
        object.__setattr__(self, "choices", tuple(options))
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "default_value", default_value)

    def _payload(self) -> dict[str, Any]:
        spec = _FieldSpecification._payload(self)  # noqa: SLF001 - slotted dataclasses cannot call super().
        spec.update(
            choices=[(choice.value, choice.label) for choice in getattr(self, "choices")],  # noqa: B009
            value=getattr(self, "value"),  # noqa: B009
            default_value=getattr(self, "default_value"),  # noqa: B009
        )
        return spec


@dataclass(frozen=True, slots=True, init=False)
class ComboBox(_ChoiceSpecification):
    """A drop-down choice field, as ``page.widgets.add`` takes it.

    ``choices`` are :class:`FieldChoice` records or strings; ``flags`` with
    ``FieldFlags.EDIT`` also takes typed text. The value is drawn as in a
    :class:`TextField`.
    """

    _kind: ClassVar[str] = "combobox"
    name: str
    rect: Rect
    choices: tuple[FieldChoice, ...]
    value: str | None
    default_value: str | None
    font_size: float
    text_color: Color
    align: Literal["left", "center", "right"]
    label: str | None
    flags: FieldFlags
    border: Border
    border_color: Color | None
    background_color: Color | None

    def __init__(  # noqa: PLR0913 - the field's options.
        self,
        name: str,
        rect: Sequence[float],
        *,
        choices: Iterable[FieldChoice | str],
        value: str | None = None,
        default_value: str | None = None,
        font_size: float = 0,
        text_color: Color = Color(0.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        align: Literal["left", "center", "right"] = "left",
        label: str | None = None,
        flags: FieldFlags = FieldFlags(0),  # noqa: B008 - FieldFlags is immutable.
        border: Border = Border(1),  # noqa: B008 - Border is immutable.
        border_color: Color | None = Color(0.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        background_color: Color | None = None,
    ) -> None:
        """Validate the specification."""
        self._common(name, rect, label, flags, border, border_color, background_color)
        self._text_style(font_size, text_color, align)
        self._choices(choices, value, default_value)


@dataclass(frozen=True, slots=True, init=False)
class ListBox(_ChoiceSpecification):
    """A list of choices, as ``page.widgets.add`` takes it.

    The fields are those of :class:`ComboBox`; ``FieldFlags.MULTI_SELECT``
    lets the user select several.
    """

    _kind: ClassVar[str] = "listbox"
    name: str
    rect: Rect
    choices: tuple[FieldChoice, ...]
    value: str | None
    default_value: str | None
    font_size: float
    text_color: Color
    align: Literal["left", "center", "right"]
    label: str | None
    flags: FieldFlags
    border: Border
    border_color: Color | None
    background_color: Color | None

    def __init__(  # noqa: PLR0913 - the field's options.
        self,
        name: str,
        rect: Sequence[float],
        *,
        choices: Iterable[FieldChoice | str],
        value: str | None = None,
        default_value: str | None = None,
        font_size: float = 0,
        text_color: Color = Color(0.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        align: Literal["left", "center", "right"] = "left",
        label: str | None = None,
        flags: FieldFlags = FieldFlags(0),  # noqa: B008 - FieldFlags is immutable.
        border: Border = Border(1),  # noqa: B008 - Border is immutable.
        border_color: Color | None = Color(0.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        background_color: Color | None = None,
    ) -> None:
        """Validate the specification."""
        self._common(name, rect, label, flags, border, border_color, background_color)
        self._text_style(font_size, text_color, align)
        self._choices(choices, value, default_value)


@dataclass(frozen=True, slots=True, init=False)
class PushButton(_FieldSpecification):
    """A pushbutton, which holds no value, as ``page.widgets.add`` takes it.

    ``caption`` is drawn centred in Helvetica at ``font_size`` in
    ``text_color``.
    """

    _kind: ClassVar[str] = "pushbutton"
    name: str
    rect: Rect
    caption: str | None
    font_size: float
    text_color: Color
    align: Literal["left", "center", "right"]
    label: str | None
    flags: FieldFlags
    border: Border
    border_color: Color | None
    background_color: Color | None

    def __init__(  # noqa: PLR0913 - the field's options.
        self,
        name: str,
        rect: Sequence[float],
        *,
        caption: str | None = None,
        font_size: float = 0,
        text_color: Color = Color(0.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        label: str | None = None,
        flags: FieldFlags = FieldFlags(0),  # noqa: B008 - FieldFlags is immutable.
        border: Border = Border(1),  # noqa: B008 - Border is immutable.
        border_color: Color | None = Color(0.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        background_color: Color | None = Color(0.75, 0.75, 0.75),  # noqa: B008 - Color is immutable.
    ) -> None:
        """Validate the specification."""
        self._common(name, rect, label, flags, border, border_color, background_color)
        self._text_style(font_size, text_color, "center")
        if caption is not None:
            _field_text(caption, "caption")
        object.__setattr__(self, "caption", caption)

    def _payload(self) -> dict[str, Any]:
        spec = _FieldSpecification._payload(self)  # noqa: SLF001 - slotted dataclasses cannot call super().
        spec["caption"] = self.caption
        return spec


@dataclass(frozen=True, slots=True, init=False)
class SignatureField(_FieldSpecification):
    """An unsigned signature field, which a signing tool such as pyHanko fills, as ``page.widgets.add`` takes it."""

    _kind: ClassVar[str] = "signature"
    name: str
    rect: Rect
    label: str | None
    flags: FieldFlags
    border: Border
    border_color: Color | None
    background_color: Color | None

    def __init__(  # noqa: PLR0913 - the field's options.
        self,
        name: str,
        rect: Sequence[float],
        *,
        label: str | None = None,
        flags: FieldFlags = FieldFlags(0),  # noqa: B008 - FieldFlags is immutable.
        border: Border = Border(1),  # noqa: B008 - Border is immutable.
        border_color: Color | None = Color(0.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        background_color: Color | None = None,
    ) -> None:
        """Validate the specification."""
        self._common(name, rect, label, flags, border, border_color, background_color)


#: The specifications that ``page.widgets.add`` takes.
FieldSpecification: TypeAlias = TextField | CheckBox | RadioButton | ComboBox | ListBox | PushButton | SignatureField
_FIELD_SPECIFICATIONS = (TextField, CheckBox, RadioButton, ComboBox, ListBox, PushButton, SignatureField)


def _button_state(state: object) -> None:
    if not isinstance(state, str) or not state or state == "Off":
        msg = f"a button's on state is a name other than Off: {state!r}"
        raise ValueError(msg)
    _field_text(state, "on_state")


# The package re-exports these names; reprs, pickles, and documentation name them.
for _public in (
    CheckBox,
    ComboBox,
    Field,
    FieldChoice,
    FieldFlags,
    FieldType,
    Form,
    ListBox,
    PushButton,
    RadioButton,
    SignatureField,
    SignatureFlags,
    TextField,
):
    _public.__module__ = "pyropdf"
