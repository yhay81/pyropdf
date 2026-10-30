"""Text editing: replace the text that simple fonts draw.

``page.replace_text(old, new)`` finds ``old`` in the strings of the
text-showing operations (``Tj``, ``TJ``, ``'``, and ``"``) of the page and
of the Form XObjects that it draws, decodes them through each font as hayro
reads it, and writes ``new`` encoded through the same font. A kerning number
after each replacement makes up the difference between the old and the new
advance, so the text that follows keeps its position (ISO 32000-2 9.4.4).

The code of each character and its advance come from hayro itself: a
transaction that is discarded shows every code of each font on the page and
reads the chars back, so that the encoding of a replacement is the one that
text extraction and rendering read. Ported in behavior from pylopdf's
``Page.replace_text`` at ebb01d3c, which applied lopdf's replacement.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyropdf._errors import LimitError
from pyropdf._limits import _validate_optional_positive_int
from pyropdf._objects import Array, Dictionary, Name, Real, Stream, String
from pyropdf._operations import InlineImage, Operation, Operations

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pyropdf._page import Page

#: Nesting of Form XObjects that a replacement follows from the page.
_MAX_FORM_DEPTH = 16
#: Drawings of Form XObjects that one replacement reads.
_MAX_FORMS = 4096
#: Fonts that one discarded transaction shows every code of.
_PROBE_FONTS = 256
#: The size that the probe shows each code at, so that an advance in
#: display space is in thousandths of text space (ISO 32000-2 9.2.4).
_PROBE_SIZE = 1000
#: The space between two probed codes, beyond any glyph's advance. Each
#: font's codes form a 16 by 16 grid, so that the coordinates stay small and
#: hayro's single-precision advances stay exact.
_PROBE_STEP = 2000
#: The font subtypes whose codes are one byte each (ISO 32000-2 9.6).
_SIMPLE = frozenset({"Type1", "MMType1", "TrueType", "Type3"})
#: The operators that show text.
_SHOWS_TEXT = frozenset({"Tj", "TJ", "'", '"'})
#: The code that word spacing applies to (ISO 32000-2 9.3.3).
_SPACE = 32


class _Rollback(Exception):  # noqa: N818 - control flow, never raised to a caller.
    """Ends the transaction that shows every code of the fonts."""


@dataclass(slots=True)
class _Font:
    """A font that a text-showing operation uses, and what hayro reads of its codes."""

    dictionary: Dictionary
    simple: bool
    texts: dict[int, str] = field(default_factory=dict)
    """The text of each code that hayro maps to Unicode."""
    advances: dict[int, float] = field(default_factory=dict)
    """The advance of each code, in thousandths of text space."""

    def code(self, character: str) -> int | None:
        """Return the code that shows ``character``, preferring the space for a space."""
        if character == " " and self.texts.get(_SPACE) == " ":
            return _SPACE
        codes = [code for code, text in self.texts.items() if text == character]
        return min(codes) if codes else None


@dataclass(slots=True)
class _State:
    """The text state that a replacement needs (ISO 32000-2 9.3)."""

    font: _Font | None = None
    size: float = 0.0
    char_spacing: float = 0.0
    word_spacing: float = 0.0

    def copy(self) -> _State:
        return _State(self.font, self.size, self.char_spacing, self.word_spacing)

    def apply(self, operation: Operation, resources: Dictionary | None, font_of: Any) -> None:  # noqa: ANN401 - _Editor._font_of.
        """Set what a text state operator, or a graphics state that sets the font, sets."""
        operator, operands = operation.operator, operation.operands
        found = font_of(resources, operation)
        if found is not None:
            self.font, self.size = found
        elif operator == "Tc" and operands:
            self.char_spacing = _number(operands[0])
        elif operator == "Tw" and operands:
            self.word_spacing = _number(operands[0])
        elif operator == '"' and len(operands) == 3:  # noqa: PLR2004 - aw, ac, and the string.
            self.word_spacing = _number(operands[0])
            self.char_spacing = _number(operands[1])


@dataclass(slots=True)
class _Scope:
    """A content stream as one drawing reads it: the page's, or a Form XObject's where a ``Do`` draws it."""

    operations: Operations
    resources: Dictionary | None
    stream: Stream | None
    """The Form XObject, or ``None`` for the page's content."""
    depth: int
    forms: dict[int, _Scope] = field(default_factory=dict)
    """The drawing of the Form XObject that each ``Do`` draws, by the operation's index."""


@dataclass(slots=True)
class _Copy:
    """A drawing of a Form XObject that changed, which becomes a new Form."""

    scope: _Scope
    operations: list[Operation | InlineImage]
    drawn: dict[str, int]
    """The copies that it draws, by the name that it draws them by."""


@dataclass(slots=True)
class _Glyph:
    code: int
    text: str


@dataclass(slots=True)
class _Kern:
    amount: float


def replace_text(page: Page, old: str, new: str, *, count: int | None) -> int:
    """Implement :meth:`Page.replace_text`."""
    if not isinstance(old, str):
        msg = f"old must be a string: {type(old).__name__}"
        raise TypeError(msg)
    if not isinstance(new, str):
        msg = f"new must be a string: {type(new).__name__}"
        raise TypeError(msg)
    if not old:
        msg = "old must be at least 1 character"
        raise ValueError(msg)
    _validate_optional_positive_int("count", count)
    editor = _Editor(page, old, new, count)
    editor.read()
    if editor.replaced:
        editor.write()
    return editor.replaced


class _Editor:
    """One replacement: reads the drawings of the page, then writes the ones that changed."""

    def __init__(self, page: Page, old: str, new: str, count: int | None) -> None:
        self.page = page
        self.old = old
        self.new = new
        self.count = count
        self.replaced = 0
        self.fonts: dict[object, _Font] = {}
        self.forms = 0
        #: The operations of each Form XObject, parsed once for all its drawings.
        self.parsed: dict[object, Operations] = {}
        self.root = _Scope(page.operations, _resources(page), None, 0)
        #: The new operations of the page's content, when they changed.
        self.operations: list[Operation | InlineImage] | None = None
        #: The copies that the page draws, by the name that it draws them by.
        self.drawn: dict[str, int] = {}
        #: The drawings that changed, each after the copies that it draws.
        self.copies: list[_Copy] = []

    def read(self) -> None:
        """Find the drawings and fonts, read the fonts' codes, and plan the replacements in drawing order."""
        self._collect(self.root)
        self._probe([font for font in self.fonts.values() if font.simple])
        self._refuse_composite()
        edited = self._edit(self.root, _State())
        if edited is not None:
            self.operations, self.drawn = edited

    def _collect(self, scope: _Scope) -> None:
        """Add the drawings of the Form XObjects that ``scope`` draws, and the fonts they use."""
        xobjects = _sub(scope.resources, "XObject")
        for index, operation in enumerate(scope.operations):
            if not isinstance(operation, Operation):
                continue
            if operation.operator in ("Tf", "gs"):
                self._font_of(scope.resources, operation)
            elif operation.operator == "Do" and operation.operands and isinstance(operation.operands[0], Name):
                form = xobjects.get(operation.operands[0]) if xobjects is not None else None
                if not isinstance(form, Stream) or form.get("Subtype") != "Form":
                    continue
                if scope.depth + 1 > _MAX_FORM_DEPTH:
                    msg = f"replace_text reaches Form XObjects nested deeper than {_MAX_FORM_DEPTH}"
                    code = "text_edit_forms"
                    raise LimitError(code, msg)
                if self.forms >= _MAX_FORMS:
                    msg = f"replace_text reaches more than {_MAX_FORMS} drawings of Form XObjects"
                    code = "text_edit_forms"
                    raise LimitError(code, msg)
                self.forms += 1
                resources = form.get("Resources")
                if not isinstance(resources, Dictionary):
                    resources = scope.resources
                key = _key(form)
                if key not in self.parsed:
                    self.parsed[key] = Operations.parse(form.read())
                child = _Scope(self.parsed[key], resources, form, scope.depth + 1)
                scope.forms[index] = child
                self._collect(child)

    def _font_of(self, resources: Dictionary | None, operation: Operation) -> tuple[_Font | None, float] | None:
        """Return the font and size that a ``Tf``, or a ``gs`` whose graphics state has a ``Font``, sets."""
        operator, operands = operation.operator, operation.operands
        if operator == "Tf" and len(operands) == 2 and isinstance(operands[0], Name):  # noqa: PLR2004 - name and size.
            fonts = _sub(resources, "Font")
            return self._font(fonts.get(operands[0]) if fonts is not None else None), _number(operands[1])
        if operator == "gs" and operands and isinstance(operands[0], Name):
            states = _sub(resources, "ExtGState")
            state = states.get(operands[0]) if states is not None else None
            entry = state.get("Font") if isinstance(state, Dictionary) else None
            # A font and a size (ISO 32000-2 8.4.5).
            if isinstance(entry, Array) and len(entry) == 2:  # noqa: PLR2004
                return self._font(entry[0]), _number(entry[1])
        return None

    def _font(self, dictionary: object) -> _Font | None:
        """Return the font of a font dictionary, recording it on first use."""
        if not isinstance(dictionary, Dictionary):
            return None
        key = _key(dictionary)
        font = self.fonts.get(key)
        if font is None:
            font = _Font(dictionary, dictionary.get("Subtype") in _SIMPLE)
            self.fonts[key] = font
        return font

    def _probe(self, fonts: list[_Font]) -> None:
        """Read the text and the advance of every code of ``fonts`` from hayro."""
        for start in range(0, len(fonts), _PROBE_FONTS):
            _probe(self.page, fonts[start : start + _PROBE_FONTS])

    def _refuse_composite(self) -> None:
        """Refuse when ``old`` is in the text of a composite font of the page's content."""
        composite = {index for index, font in _shown(self.root, self._font_of) if font is not None and not font.simple}
        if not composite:
            return
        texts: dict[int, list[str]] = {}
        for char in self.page.chars(clipped=True, unmapped="replacement"):
            if char.operation in composite and not char.is_synthetic:
                texts.setdefault(char.operation, []).append(char.text)
        for parts in texts.values():
            if self.old in "".join(parts):
                msg = f"{self.old!r} is drawn in a composite font, which replace_text does not edit"
                raise ValueError(msg)

    def _edit(self, scope: _Scope, state: _State) -> tuple[list[Operation | InlineImage], dict[str, int]] | None:
        """Return the drawing's operations with the replacements and the copies they draw, or ``None``.

        A Form XObject starts from the state where it is drawn (ISO 32000-2
        8.10.1). A drawing of one that changes draws its copy under a new
        name, so the other drawings keep the Form.
        """
        # The names that a copy must not take, when the drawing draws Forms.
        taken = _taken_names(scope) if scope.forms else set()
        states: list[_State] = []
        out: list[Operation | InlineImage] = []
        drawn: dict[str, int] = {}
        changed = False
        for index, operation in enumerate(scope.operations):
            if self.count is not None and self.replaced >= self.count:
                out.extend(scope.operations[index:])
                break
            if not isinstance(operation, Operation):
                out.append(operation)
                continue
            operator = operation.operator
            if operator == "q":
                states.append(state.copy())
            elif operator == "Q":
                if states:
                    state = states.pop()
            else:
                state.apply(operation, scope.resources, self._font_of)
            if operator in _SHOWS_TEXT and state.font is not None and state.font.simple:
                replacement = self._replace(operation, state)
                if replacement is not None:
                    out.extend(replacement)
                    changed = True
                    continue
            form = scope.forms.get(index)
            copy = None if form is None else self._copy(form, operation, state, taken, drawn)
            if copy is not None:
                out.append(copy)
                changed = True
                continue
            out.append(operation)
        return (out, drawn) if changed else None

    def _copy(
        self, form: _Scope, operation: Operation, state: _State, taken: set[str], drawn: dict[str, int]
    ) -> Operation | None:
        """Return the ``Do`` that draws a copy of the drawing ``form`` with its replacements, or ``None``."""
        edited = self._edit(form, state.copy())
        if edited is None:
            return None
        name = _fresh(str(operation.operands[0]), taken)
        taken.add(name)
        drawn[name] = len(self.copies)
        self.copies.append(_Copy(form, *edited))
        return Operation("Do", (Name(name),))

    def _replace(self, operation: Operation, state: _State) -> list[Operation] | None:
        """Return the operations that replace ``operation``, or ``None`` when ``old`` is not in it."""
        tokens = _tokens(operation, state.font)
        if tokens is None:
            return None
        edited = self._replace_tokens(tokens, state)
        if edited is None:
            return None
        array = tuple(_group(edited))
        operator, operands = operation.operator, operation.operands
        shown = Operation("TJ", (array,))
        if operator == "'":
            return [Operation("T*"), shown]
        if operator == '"':
            return [Operation("Tw", (operands[0],)), Operation("Tc", (operands[1],)), Operation("T*"), shown]
        return [shown]

    def _replace_tokens(self, tokens: list[_Glyph | _Kern], state: _State) -> list[_Glyph | _Kern] | None:
        """Return ``tokens`` with the occurrences of ``old`` replaced, or ``None`` when there is none."""
        font = state.font
        assert font is not None  # noqa: S101 - _edit checks it.
        glyphs = [index for index, token in enumerate(tokens) if isinstance(token, _Glyph)]
        starts: dict[int, int] = {}
        ends: dict[int, int] = {}
        text = ""
        for place, index in enumerate(glyphs):
            starts[len(text)] = place
            glyph = tokens[index]
            assert isinstance(glyph, _Glyph)  # noqa: S101 - glyphs lists them.
            text += glyph.text
            ends[len(text)] = place
        found: list[tuple[int, int]] = []
        position = text.find(self.old)
        while position >= 0:
            if self.count is not None and self.replaced + len(found) >= self.count:
                break
            end = position + len(self.old)
            if position in starts and end in ends:
                found.append((glyphs[starts[position]], glyphs[ends[end]]))
                position = text.find(self.old, end)
            else:
                position = text.find(self.old, position + 1)
        if not found:
            return None
        new = [_Glyph(_encode(font, character), character) for character in self.new]
        out: list[_Glyph | _Kern] = []
        last = 0
        for first, final in found:
            out.extend(tokens[last:first])
            out.extend(new)
            kern = _compensation(tokens[first : final + 1], new, font, state)
            if kern:
                out.append(_Kern(kern))
            last = final + 1
        out.extend(tokens[last:])
        self.replaced += len(found)
        return out

    def write(self) -> None:
        """Write the copies and the page's content, in one transaction."""
        document = self.page.document
        numbers: list[int] = []
        with document.transaction():
            for copy in self.copies:
                stream = copy.scope.stream
                assert stream is not None  # noqa: S101 - only Forms are copied.
                dictionary = {key: value for key, value in stream.items() if key not in _STREAM_KEYS}
                drawn = {name: numbers[place] for name, place in copy.drawn.items()}
                if drawn:
                    dictionary["Resources"] = self._own_resources(copy.scope, drawn)
                data = Operations(copy.operations, tail=copy.scope.operations.tail).to_bytes()
                numbers.append(document.objects.add_stream(data, dictionary))
            if self.operations is not None:
                self.page.operations = Operations(self.operations, tail=self.root.operations.tail)
            if self.drawn:
                _repoint_page(self.page, {name: numbers[place] for name, place in self.drawn.items()})

    def _own_resources(self, scope: _Scope, drawn: dict[str, int]) -> dict[str, Any]:
        """Return the resources of a Form's copy that draws the copies ``drawn`` too."""
        assert scope.stream is not None  # noqa: S101 - only Forms are copied.
        resources = scope.stream.get("Resources")
        if not isinstance(resources, Dictionary):
            # A Form without resources uses its parent's, which name its Forms.
            resources = scope.resources
        own = dict(resources.items()) if isinstance(resources, Dictionary) else {}
        xobjects = own.get("XObject")
        own["XObject"] = dict(xobjects.items()) if isinstance(xobjects, Dictionary) else {}
        document = self.page.document
        for name, number in drawn.items():
            own["XObject"][name] = document.objects[number]
        return own


def _taken_names(scope: _Scope) -> set[str]:
    """Return the names of the scope's XObjects and the names that its ``Do`` operations draw, defined or not.

    A name that the content draws without defining it draws nothing, and
    must keep drawing nothing, so a copy does not take it. A Form without
    resources of its own draws by the scope's names too, so its names are
    taken as well.
    """
    xobjects = _sub(scope.resources, "XObject")
    names = set(xobjects) if xobjects is not None else set()
    # The Forms read the same resources, and _collect bounds their number and depth.
    pending = [scope]
    while pending:
        current = pending.pop()
        names.update(
            str(operation.operands[0])
            for operation in current.operations
            if isinstance(operation, Operation) and operation.operator == "Do" and operation.operands
        )
        pending.extend(form for form in current.forms.values() if form.resources is scope.resources)
    return names


def _fresh(name: str, taken: set[str]) -> str:
    """Return a name for a copy of the Form ``name`` that ``taken`` lacks."""
    number = 1
    while f"{name}_{number}" in taken:
        number += 1
    return f"{name}_{number}"


#: The stream entries that a copy of a Form sets from its new data.
_STREAM_KEYS = frozenset({"Length", "Filter", "DecodeParms", "DL"})


def _repoint_page(page: Page, drawn: dict[str, int]) -> None:
    """Make the page draw the copies of its Forms, through resources of its own."""
    own = page.obj.get("Resources")
    if own is None or own.is_indirect:
        inherited = own if own is not None else _resources(page)
        page.obj["Resources"] = {} if inherited is None else dict(inherited.items())
    resources = page.obj["Resources"]
    xobjects = resources.get("XObject")
    if xobjects is None or xobjects.is_indirect:
        resources["XObject"] = {} if xobjects is None else dict(xobjects.items())
    document = page.document
    for name, number in drawn.items():
        resources["XObject"][name] = document.objects[number]


def _resources(page: Page) -> Dictionary | None:
    """Return the page's resources, its own or inherited from the page tree."""
    node: Any = page.obj
    for _ in range(64):
        if not isinstance(node, Dictionary):
            return None
        resources = node.get("Resources")
        if isinstance(resources, Dictionary):
            return resources
        node = node.get("Parent")
    return None


def _sub(resources: Dictionary | None, key: str) -> Dictionary | None:
    value = resources.get(key) if resources is not None else None
    return value if isinstance(value, Dictionary) else None


def _key(dictionary: Dictionary) -> object:
    """Return what identifies a font dictionary: its object number, or the handle of a direct one."""
    return dictionary.xref if dictionary.is_indirect else ("direct", id(dictionary))


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _shown(scope: _Scope, font_of: Any) -> Iterator[tuple[int, _Font | None]]:  # noqa: ANN401 - _Editor._font_of.
    """Yield the index and the font of each text-showing operation of ``scope``."""
    states: list[_Font | None] = []
    font: _Font | None = None
    for index, operation in enumerate(scope.operations):
        operator = operation.operator
        if operator == "q":
            states.append(font)
        elif operator == "Q" and states:
            font = states.pop()
        elif operator in ("Tf", "gs"):
            found = font_of(scope.resources, operation)
            if found is not None:
                font = found[0]
        elif operator in _SHOWS_TEXT:
            yield index, font


def _probe(page: Page, fonts: list[_Font]) -> None:
    """Show every code of ``fonts`` on ``page`` in a discarded transaction, and read the chars."""
    operations = [Operation("BT")]
    codes: dict[int, tuple[_Font, int]] = {}
    for number, font in enumerate(fonts):
        operations.append(Operation("Tf", (Name(f"P{number}"), _PROBE_SIZE)))
        for code in range(256):
            x = code % 16 * _PROBE_STEP
            y = (number * 16 + code // 16) * _PROBE_STEP
            operations.append(Operation("Tm", (1, 0, 0, 1, x, y)))
            codes[len(operations)] = (font, code)
            operations.append(Operation("Tj", (String(bytes([code])),)))
    operations.append(Operation("ET"))
    document = page.document
    chars = []
    with contextlib.suppress(_Rollback), document.transaction():
        page.obj["Resources"] = {"Font": {f"P{number}": font.dictionary for number, font in enumerate(fonts)}}
        for key in ("Rotate", "UserUnit"):
            if key in page.obj:
                del page.obj[key]
        page.obj["MediaBox"] = [0, 0, 16 * _PROBE_STEP, (len(fonts) * 16 + 1) * _PROBE_STEP]
        page.operations = operations
        chars = list(page.chars(clipped=True, unmapped="replacement", max_glyphs=None))
        raise _Rollback
    for char in chars:
        shown = codes.get(char.operation) if char.operation is not None else None
        if shown is None or char.is_synthetic:
            continue
        font, code = shown
        font.texts[code] = font.texts.get(code, "") + char.text
        font.advances[code] = char.bbox.x1 - char.bbox.x0


def _tokens(operation: Operation, font: _Font | None) -> list[_Glyph | _Kern] | None:
    """Return the glyphs and the kerning of a text-showing operation, or ``None`` for malformed operands."""
    assert font is not None  # noqa: S101 - _edit checks it.
    operands = operation.operands
    if operation.operator == "TJ":
        if len(operands) != 1 or not isinstance(operands[0], tuple):
            return None
        elements = operands[0]
    else:
        if not operands or not isinstance(operands[-1], bytes):
            return None
        elements = (operands[-1],)
    tokens: list[_Glyph | _Kern] = []
    for element in elements:
        if isinstance(element, bytes):
            tokens.extend(_Glyph(code, font.texts.get(code, "�")) for code in element)
        elif isinstance(element, (int, float)) and not isinstance(element, bool):
            tokens.append(_Kern(float(element)))
    return tokens


def _encode(font: _Font, character: str) -> int:
    code = font.code(character)
    if code is None:
        name = font.dictionary.get("BaseFont")
        msg = f"the font {name or 'of the text'} has no code for {character!r} (U+{ord(character):04X})"
        raise ValueError(msg)
    return code


def _advance(tokens: list[_Glyph | _Kern], font: _Font, state: _State) -> float:
    """Return the horizontal displacement of ``tokens`` in unscaled text space (ISO 32000-2 9.4.4)."""
    total = 0.0
    for token in tokens:
        if isinstance(token, _Kern):
            total -= token.amount / 1000 * state.size
        else:
            total += font.advances.get(token.code, 0.0) / 1000 * state.size + state.char_spacing
            if token.code == _SPACE:
                total += state.word_spacing
    return total


def _compensation(old: list[_Glyph | _Kern], new: list[_Glyph], font: _Font, state: _State) -> float:
    """Return the kerning after ``new`` that gives it the advance of ``old``."""
    if state.size == 0:
        return 0.0
    difference = _advance(list(new), font, state) - _advance(old, font, state)
    return round(difference * 1000 / state.size, 6)


def _group(tokens: list[_Glyph | _Kern]) -> Iterator[String | int | Real]:
    """Yield the elements of a ``TJ`` array: runs of codes as strings, and kerning as numbers."""
    codes = bytearray()
    for token in tokens:
        if isinstance(token, _Glyph):
            codes.append(token.code)
            continue
        if codes:
            yield String(bytes(codes))
            codes.clear()
        amount = float(token.amount)
        yield int(amount) if amount.is_integer() else Real(amount)
    if codes:
        yield String(bytes(codes))


__all__ = ["replace_text"]
