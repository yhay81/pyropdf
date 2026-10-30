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

from pyropdf._limits import _validate_optional_positive_int
from pyropdf._objects import Dictionary, Name, Real, Stream, String
from pyropdf._operations import InlineImage, Operation, Operations

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pyropdf._page import Page

#: Nesting of Form XObjects that a replacement follows from the page.
_MAX_FORM_DEPTH = 16
#: Form XObjects that one replacement reads.
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

    def apply(self, operation: Operation, fonts: Dictionary | None, font_of: Any) -> None:  # noqa: ANN401 - _Editor._font.
        """Set what a text state operator sets."""
        operator, operands = operation.operator, operation.operands
        if operator == "Tf" and len(operands) == 2 and isinstance(operands[0], Name):  # noqa: PLR2004 - name and size.
            self.font = font_of(fonts, operands[0])
            self.size = _number(operands[1])
        elif operator == "Tc" and operands:
            self.char_spacing = _number(operands[0])
        elif operator == "Tw" and operands:
            self.word_spacing = _number(operands[0])
        elif operator == '"' and len(operands) == 3:  # noqa: PLR2004 - aw, ac, and the string.
            self.word_spacing = _number(operands[0])
            self.char_spacing = _number(operands[1])


@dataclass(slots=True)
class _Scope:
    """A content stream: the page's or a Form XObject's, with its resources."""

    operations: Operations
    resources: Dictionary | None
    stream: Stream | None
    """The Form XObject, or ``None`` for the page's content."""
    depth: int


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
    """One replacement: reads the page's scopes, then writes the ones that changed."""

    def __init__(self, page: Page, old: str, new: str, count: int | None) -> None:
        self.page = page
        self.old = old
        self.new = new
        self.count = count
        self.replaced = 0
        self.fonts: dict[object, _Font] = {}
        self.forms = 0
        #: The new operations of each scope that changed, by the scope's place.
        self.changed: dict[int, list[Operation | InlineImage]] = {}
        #: The scopes, in the order that the walk read them.
        self.scopes: list[_Scope] = []
        #: The parent of each Form scope and the name it draws it by.
        self.parents: dict[int, tuple[int, str]] = {}

    def read(self) -> None:
        """Find the scopes and fonts, read the fonts' codes, and plan the replacements."""
        page_resources = _resources(self.page)
        self._collect(_Scope(self.page.operations, page_resources, None, 0), None)
        self._probe([font for font in self.fonts.values() if font.simple])
        self._refuse_composite()
        for index, scope in enumerate(self.scopes):
            if self.count is not None and self.replaced >= self.count:
                break
            operations = self._edit(scope)
            if operations is not None:
                self.changed[index] = operations

    def _collect(self, scope: _Scope, parent: tuple[int, str] | None) -> None:
        """Add ``scope`` and the Form XObjects that it draws, and the fonts they use."""
        place = len(self.scopes)
        self.scopes.append(scope)
        if parent is not None:
            self.parents[place] = parent
        fonts = _sub(scope.resources, "Font")
        xobjects = _sub(scope.resources, "XObject")
        for operation in scope.operations:
            if not isinstance(operation, Operation):
                continue
            if operation.operator == "Tf" and operation.operands and isinstance(operation.operands[0], Name):
                self._font(fonts, operation.operands[0])
            elif operation.operator == "Do" and operation.operands and isinstance(operation.operands[0], Name):
                name = operation.operands[0]
                form = xobjects.get(name) if xobjects is not None else None
                if not isinstance(form, Stream) or form.get("Subtype") != "Form":
                    continue
                if scope.depth + 1 > _MAX_FORM_DEPTH or self.forms >= _MAX_FORMS:
                    continue
                self.forms += 1
                resources = form.get("Resources")
                if not isinstance(resources, Dictionary):
                    resources = scope.resources
                child = _Scope(Operations.parse(form.read()), resources, form, scope.depth + 1)
                self._collect(child, (place, str(name)))

    def _font(self, fonts: Dictionary | None, name: str) -> _Font | None:
        """Return the font of ``name`` in a scope's fonts, recording it on first use."""
        dictionary = fonts.get(name) if fonts is not None else None
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
        scope = self.scopes[0]
        composite = {index for index, font in _shown(scope, self._font) if font is not None and not font.simple}
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

    def _edit(self, scope: _Scope) -> list[Operation | InlineImage] | None:
        """Return the scope's operations with the replacements, or ``None`` when none applies."""
        fonts = _sub(scope.resources, "Font")
        states: list[_State] = []
        state = _State()
        out: list[Operation | InlineImage] = []
        changed = False
        for operation in scope.operations:
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
                state.apply(operation, fonts, self._font)
            if operator in _SHOWS_TEXT and state.font is not None and state.font.simple:
                replacement = self._replace(operation, state)
                if replacement is not None:
                    out.extend(replacement)
                    changed = True
                    continue
            out.append(operation)
        return out if changed else None

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
        """Write the scopes that changed, in one transaction."""
        document = self.page.document
        # A Form changes when it or a Form that it draws changes.
        for place in sorted(self.changed, reverse=True):
            parent = self.parents.get(place)
            while parent is not None:
                self.changed.setdefault(parent[0], list(self.scopes[parent[0]].operations))
                parent = self.parents.get(parent[0])
        copies: dict[int, int] = {}
        with document.transaction():
            for place in sorted(self.changed, reverse=True):
                scope = self.scopes[place]
                if scope.stream is None:
                    continue
                dictionary = {key: value for key, value in scope.stream.items() if key not in _STREAM_KEYS}
                resources = self._own_resources(place, scope.stream.get("Resources"), copies)
                if resources is not None:
                    dictionary["Resources"] = resources
                data = Operations(self.changed[place], tail=scope.operations.tail).to_bytes()
                copies[place] = document.objects.add_stream(data, dictionary)
            if 0 in self.changed:
                self.page.operations = Operations(self.changed[0], tail=self.scopes[0].operations.tail)
            drawn = self._drawn(0, copies)
            if drawn:
                _repoint_page(self.page, drawn)

    def _drawn(self, place: int, copies: dict[int, int]) -> dict[str, int]:
        """Return the copies that scope ``place`` draws directly, by the name it draws them by."""
        return {
            name: copies[child] for child, (parent, name) in self.parents.items() if parent == place and child in copies
        }

    def _own_resources(self, place: int, resources: object, copies: dict[int, int]) -> dict[str, Any] | None:
        """Return the resources of a Form's copy that draws the copies of its own Forms."""
        drawn = self._drawn(place, copies)
        if not drawn:
            return None
        if not isinstance(resources, Dictionary):
            # A Form without resources uses its parent's, which name its Forms.
            resources = self.scopes[place].resources
        own = dict(resources.items()) if isinstance(resources, Dictionary) else {}
        xobjects = own.get("XObject")
        own["XObject"] = dict(xobjects.items()) if isinstance(xobjects, Dictionary) else {}
        document = self.page.document
        for name, number in drawn.items():
            own["XObject"][name] = document.objects[number]
        return own


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


def _shown(scope: _Scope, font_of: Any) -> Iterator[tuple[int, _Font | None]]:  # noqa: ANN401 - _Editor._font.
    """Yield the index and the font of each text-showing operation of ``scope``."""
    fonts = _sub(scope.resources, "Font")
    states: list[_Font | None] = []
    font: _Font | None = None
    for index, operation in enumerate(scope.operations):
        operator = operation.operator
        if operator == "q":
            states.append(font)
        elif operator == "Q" and states:
            font = states.pop()
        elif operator == "Tf" and operation.operands and isinstance(operation.operands[0], Name):
            font = font_of(fonts, operation.operands[0])
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
