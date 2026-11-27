"""Text editing: replace text in simple, composite (Type 0), Type 3, and vertical fonts.

``page.replace_text(old, new, *, count=None, fallback=None)`` finds ``old`` in
the text-showing operations (``Tj``, ``TJ``, ``'``, and ``"``) and
``/ActualText`` marked-content spans of the page and of the Form XObjects that
it draws. It decodes simple and Type 3 fonts through hayro and Type 0
(composite / CID) fonts through :mod:`pyropdf._core`'s CMap codec, encodes
``new`` through the same font (extending an un-subsetted embedded face when
possible, or switching to ``fallback`` when provided), and emits compensating
``TJ`` adjustments along the active writing axis (horizontal for ``WMode = 0``,
vertical for ``WMode = 1``) so following text keeps its display position
(ISO 32000-2 9.4.4, 9.6.5, 9.7, 14.9.4).
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyropdf import _core
from pyropdf._errors import LimitError, PdfError
from pyropdf._fonts import Font, _cmap, _w2_array, _w_array, add_glyphs, embedded_font
from pyropdf._limits import _validate_optional_positive_int
from pyropdf._objects import Array, Dictionary, Name, Real, Stream, String, _encode_text_string
from pyropdf._operations import InlineImage, Operation, Operations
from pyropdf._xobjects import _parse_stream_operations

if TYPE_CHECKING:
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
#: The space between two probed codes, beyond any glyph's advance.
_PROBE_STEP = 2000
#: The font subtypes whose codes are one byte each (ISO 32000-2 9.6).
_SIMPLE = frozenset({"Type1", "MMType1", "TrueType", "Type3"})
#: The operators that show text.
_SHOWS_TEXT = frozenset({"Tj", "TJ", "'", '"'})
#: Operators that move the text matrix inside a text object.
_TEXT_POSITION_OPS = frozenset({"Td", "TD", "Tm", "T*", "'", '"', "BT", "ET"})
#: Standard 14 fonts that use a built-in symbolic encoding.
_SYMBOLIC_STANDARD = frozenset({"Symbol", "ZapfDingbats"})
#: The single-byte space code that word spacing applies to (ISO 32000-2 9.3.3).
_SPACE_BYTE = b" "
#: The stream entries that a copy of a Form sets from its new data.
_STREAM_KEYS = frozenset({"Length", "Filter", "DecodeParms", "DL"})
#: Maximum ligature length considered during greedy simple/Type 3 reverse lookup.
_MAX_LIGATURE = 8
#: Maximum CID range expanded while reading ``/W``, ``/W2``, or ``bfrange``.
_MAX_RANGE_EXPAND = 65_536
#: Patterns for reading existing ToUnicode entries when extending an embedded face.
_BFCHAR_BLOCKS = re.compile(rb"beginbfchar\s*(.*?)\s*endbfchar", re.DOTALL)
_BFCHAR_ROW = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")
_BFRANGE_BLOCKS = re.compile(rb"beginbfrange\s*(.*?)\s*endbfrange", re.DOTALL)
_BFRANGE_ROW = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<([0-9A-Fa-f]+)>|\[([^\]]*)\])")
_HEX_STR = re.compile(rb"<([0-9A-Fa-f]+)>")


class _Rollback(Exception):  # noqa: N818 - control flow, never raised to a caller.
    """Ends the transaction that shows every code of the simple fonts."""


@dataclass(slots=True)
class _Font:
    """A font used by a text-showing operation, with its decoded metrics and mappings."""

    dictionary: Dictionary
    kind: str | bool
    """One of ``"simple"``, ``"type3"``, ``"type0"``, or ``"other"``."""
    vertical: bool = False
    can_extend: bool = False
    base_font: str | None = None
    texts: dict[int, str] = field(default_factory=dict)
    """Unicode text of each one-byte code (simple / Type 3)."""
    advances: dict[int, float] = field(default_factory=dict)
    """Advance of each one-byte code in thousandths of text space."""
    drawable: list[bool] | None = None
    """Per-byte drawability from ``/CharProcs`` for Type 3 fonts."""
    decoded: dict[bytes, list[_Glyph]] = field(default_factory=dict)
    """Cached decoded glyph sequences for raw strings of a Type 0 font."""
    extensions: dict[int, tuple[float, tuple[float, float, float] | None, str]] = field(default_factory=dict)
    """Planned ``(width, v_metrics, text)`` extensions by GID for an editable Type 0 font."""

    def __post_init__(self) -> None:
        if isinstance(self.kind, bool):
            self.kind = "simple" if self.kind else "other"

    @property
    def usable(self) -> bool:
        return self.kind in ("simple", "type3", "type0")

    def simple_reverse_map(self) -> dict[str, bytes]:
        """Build Unicode -> single-byte code map for simple and Type 3 fonts."""
        mapping: dict[str, bytes] = {}
        for code in sorted(self.texts):
            if self.drawable is not None and not self.drawable[code]:
                continue
            text = self.texts[code]
            if not text:
                continue
            if text == " " and code == 32:  # noqa: PLR2004 - ASCII space code.
                mapping[text] = _SPACE_BYTE
            elif text not in mapping:
                mapping[text] = bytes([code])
        return mapping


@dataclass(slots=True)
class _State:
    """The text state that a replacement tracks (ISO 32000-2 9.3)."""

    font: _Font | None = None
    font_name: Name | None = None
    size: float = 0.0
    char_spacing: float = 0.0
    word_spacing: float = 0.0

    def copy(self) -> _State:
        return _State(self.font, self.font_name, self.size, self.char_spacing, self.word_spacing)

    def apply(self, operation: Operation, resources: Dictionary | None, font_of: Any) -> None:  # noqa: ANN401
        """Apply a text state operator or graphics state font entry."""
        operator, operands = operation.operator, operation.operands
        found = font_of(resources, operation)
        if found is not None:
            self.font, self.font_name, self.size = found
        elif operator == "Tc" and operands:
            self.char_spacing = _number(operands[0])
        elif operator == "Tw" and operands:
            self.word_spacing = _number(operands[0])
        elif operator == '"' and len(operands) == 3:  # noqa: PLR2004 - aw, ac, and the string.
            self.word_spacing = _number(operands[0])
            self.char_spacing = _number(operands[1])


@dataclass(slots=True)
class _Scope:
    """A content stream as one drawing reads it: the page's or a Form XObject's."""

    operations: Operations
    resources: Dictionary | None
    stream: Stream | None
    depth: int
    forms: dict[int, _Scope] = field(default_factory=dict)


@dataclass(slots=True)
class _EditContext:
    """Per-scope state carried while rewriting one content stream."""

    scope: _Scope
    state: _State
    states: list[_State]
    taken_xobjects: set[str]
    taken_fonts: set[str]
    drawn: dict[str, int]
    added_fonts: dict[str, object]
    budget: _core._OperationBudget


@dataclass(slots=True)
class _Copy:
    """A drawing of a Form XObject that changed, which becomes a new Form."""

    scope: _Scope
    operations: list[Operation | InlineImage]
    drawn: dict[str, int]
    added_fonts: dict[str, object]


@dataclass(slots=True)
class _Glyph:
    code: bytes
    text: str
    advance: float


@dataclass(slots=True)
class _Kern:
    amount: float


@dataclass(slots=True)
class _Switch:
    """Switch active font within an emitted replacement run."""

    font_name: Name


_Token = _Glyph | _Kern | _Switch


def replace_text(
    page: Page,
    old: str,
    new: str,
    *,
    count: int | None,
    fallback: Font | None = None,
) -> int:
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
    if fallback is not None and not isinstance(fallback, Font):
        msg = f"fallback must be a Font or None: {type(fallback).__name__}"
        raise TypeError(msg)
    editor = _Editor(page, old, new, count, fallback)
    editor.read()
    if editor.replaced:
        editor.write()
    return editor.replaced


def _poll(budget: _core._OperationBudget, index: int = 0) -> None:
    if index % 256 == 0:
        budget.poll()


class _Editor:
    """One replacement: reads drawings and fonts, plans edits atomically, then writes once."""

    def __init__(self, page: Page, old: str, new: str, count: int | None, fallback: Font | None) -> None:
        self.page = page
        self.old = old
        self.new = new
        self.count = count
        self.fallback = fallback
        self.replaced = 0
        self.fonts: dict[object, _Font] = {}
        self.forms = 0
        self.parsed: dict[object, Operations] = {}
        self.root = _Scope(Operations(), None, None, 0)
        self.operations: list[Operation | InlineImage] | None = None
        self.drawn: dict[str, int] = {}
        self.root_added_fonts: dict[str, object] = {}
        self.copies: list[_Copy] = []
        self._fallback_glyphs: dict[bool, list[tuple[int, str]]] = {False: [], True: []}

    def read(self) -> None:
        """Collect drawings and fonts, inspect/decode fonts, and plan edits in drawing order."""
        core = self.page.document._core()  # noqa: SLF001

        def collect(budget: _core._OperationBudget) -> _Scope:
            root = _Scope(self.page.operations, _resources(self.page), None, 0)
            self._collect(root, budget)
            return root

        self.root = _core.read_operation(core, collect)
        simple_fonts = [font for font in self.fonts.values() if font.kind in ("simple", "type3")]
        if simple_fonts:
            self._probe(simple_fonts)
        _core.read_operation(core, self._inspect_non_simple)
        edited = _core.read_operation(core, lambda budget: self._edit(self.root, _State(), budget))
        if edited is not None:
            self.operations, self.drawn, self.root_added_fonts = edited

    def _collect(self, scope: _Scope, budget: _core._OperationBudget) -> None:
        """Walk Form XObjects drawn by ``scope`` and record every referenced font."""
        xobjects = _sub(scope.resources, "XObject")
        for index, operation in enumerate(scope.operations):
            _poll(budget, index)
            if not isinstance(operation, Operation):
                continue
            if operation.operator in ("Tf", "gs"):
                self._font_of(scope.resources, operation)
            elif operation.operator == "Do" and operation.operands and isinstance(operation.operands[0], Name):
                form = xobjects.get(operation.operands[0]) if xobjects is not None else None
                if not isinstance(form, Stream) or form.get("Subtype") != "Form":
                    continue
                if scope.depth + 1 > _MAX_FORM_DEPTH:
                    code = "text_edit_forms"
                    msg = f"replace_text reaches Form XObjects nested deeper than {_MAX_FORM_DEPTH}"
                    raise LimitError(code, msg)
                if self.forms >= _MAX_FORMS:
                    code = "text_edit_forms"
                    msg = f"replace_text reaches more than {_MAX_FORMS} drawings of Form XObjects"
                    raise LimitError(code, msg)
                self.forms += 1
                budget.poll()
                resources = form.get("Resources")
                if not isinstance(resources, Dictionary):
                    resources = scope.resources
                key = _key(form)
                if key not in self.parsed:
                    self.parsed[key] = _parse_stream_operations(form)
                child = _Scope(self.parsed[key], resources, form, scope.depth + 1)
                scope.forms[index] = child
                self._collect(child, budget)

    def _inspect_non_simple(self, budget: _core._OperationBudget) -> None:
        """Inspect Type 3 metrics/drawability and batch-decode Type 0 strings in Rust."""
        core = self.page.document._core()  # noqa: SLF001
        for font in self.fonts.values():
            budget.poll()
            if font.kind == "type3":
                self._inspect_type3(core, font, budget)
            elif font.kind == "type0":
                info = _core.type0_inspect(core, font.dictionary._root, list(font.dictionary._path), operation=budget)  # noqa: SLF001
                if info is None:
                    font.kind = "other"
                else:
                    font.vertical, font.can_extend, font.base_font = info
        type0_strings = self._collect_type0_strings(self.root, _State(), budget)
        for font_key, strings in type0_strings.items():
            font = self.fonts[font_key]
            if not strings:
                continue
            unique = list(dict.fromkeys(strings))
            result = _core.type0_decode(
                core,
                font.dictionary._root,  # noqa: SLF001
                list(font.dictionary._path),  # noqa: SLF001
                unique,
                operation=budget,
            )
            if result is None:
                font.kind = "other"
                continue
            font.vertical, batches = result
            for raw, items in zip(unique, batches, strict=True):
                font.decoded[raw] = [_Glyph(code, text, adv) for code, text, adv in items]

    def _inspect_type3(self, core: _core._Document, font: _Font, budget: _core._OperationBudget) -> None:
        info = _core.type3_inspect(core, font.dictionary._root, list(font.dictionary._path), operation=budget)  # noqa: SLF001
        if info is None:
            return
        advances, drawable, to_unicode = info
        font.drawable = drawable
        for code in range(256):
            if drawable[code] or advances[code] != 0.0:
                font.advances[code] = advances[code]
        for code, text in to_unicode:
            font.texts[code] = text

    def _collect_type0_strings(
        self, scope: _Scope, state: _State, budget: _core._OperationBudget
    ) -> dict[object, list[bytes]]:
        """Collect raw byte strings shown in Type 0 fonts across ``scope`` and its Forms."""
        found: dict[object, list[bytes]] = {}
        states: list[_State] = []
        for index, operation in enumerate(scope.operations):
            _poll(budget, index)
            if not isinstance(operation, Operation):
                continue
            _advance_state(operation, scope.resources, state, states, self._font_of)
            if operation.operator in _SHOWS_TEXT and state.font is not None and state.font.kind == "type0":
                key = _key(state.font.dictionary)
                found.setdefault(key, []).extend(_raw_strings(operation))
            form = scope.forms.get(index)
            if form is not None:
                sub = self._collect_type0_strings(form, state.copy(), budget)
                for key, strings in sub.items():
                    found.setdefault(key, []).extend(strings)
        return found

    def _font_of(
        self, resources: Dictionary | None, operation: Operation
    ) -> tuple[_Font | None, Name | None, float] | None:
        """Return ``(font, tf_name, size)`` set by ``Tf`` or ``gs``."""
        operator, operands = operation.operator, operation.operands
        if operator == "Tf" and len(operands) == 2 and isinstance(operands[0], Name):  # noqa: PLR2004
            fonts = _sub(resources, "Font")
            dictionary = fonts.get(operands[0]) if fonts is not None else None
            return self._font(dictionary), operands[0], _number(operands[1])
        if operator == "gs" and operands and isinstance(operands[0], Name):
            states = _sub(resources, "ExtGState")
            state = states.get(operands[0]) if states is not None else None
            entry = state.get("Font") if isinstance(state, Dictionary) else None
            if isinstance(entry, Array) and len(entry) == 2:  # noqa: PLR2004
                return self._font(entry[0]), None, _number(entry[1])
        return None

    def _font(self, dictionary: object) -> _Font | None:
        if not isinstance(dictionary, Dictionary):
            return None
        key = _key(dictionary)
        font = self.fonts.get(key)
        if font is None:
            subtype = str(dictionary.get("Subtype") or "")
            if subtype == "Type3":
                kind = "type3"
            elif subtype in _SIMPLE:
                kind = "simple"
            elif subtype == "Type0":
                kind = "type0"
            else:
                kind = "other"
            base = dictionary.get("BaseFont")
            font = _Font(dictionary, kind, base_font=str(base) if isinstance(base, Name) else None)
            self.fonts[key] = font
        return font

    def _probe(self, fonts: list[_Font]) -> None:
        """Read text and advances of every code of simple / Type 3 fonts via hayro."""
        for start in range(0, len(fonts), _PROBE_FONTS):
            _probe(self.page, fonts[start : start + _PROBE_FONTS])

    def _edit(
        self, scope: _Scope, state: _State, budget: _core._OperationBudget
    ) -> tuple[list[Operation | InlineImage], dict[str, int], dict[str, object]] | None:
        """Return edited operations, copied Form XObject map, and added Font resources, or ``None``."""
        ctx = _EditContext(
            scope=scope,
            state=state,
            states=[],
            taken_xobjects=_taken_names(scope, "XObject", "Do") if scope.forms else set(),
            taken_fonts=_taken_names(scope, "Font", "Tf"),
            drawn={},
            added_fonts={},
            budget=budget,
        )
        out: list[Operation | InlineImage] = []
        changed = False
        ops = scope.operations
        index = 0
        while index < len(ops):
            _poll(budget, index)
            if self.count is not None and self.replaced >= self.count:
                out.extend(ops[index:])
                break
            emitted, next_index, step_changed = self._edit_step(ctx, index)
            out.extend(emitted)
            changed = changed or step_changed
            index = next_index
        return (out, ctx.drawn, ctx.added_fonts) if changed else None

    def _edit_step(self, ctx: _EditContext, index: int) -> tuple[list[Operation | InlineImage], int, bool]:
        """Process one operation (or one ``/ActualText`` span) at ``index``."""
        operation = ctx.scope.operations[index]
        if not isinstance(operation, Operation):
            return [operation], index + 1, False
        if operation.operator == "BDC":
            span_result = self._handle_actual_text_span(ctx, index)
            if span_result is not None:
                return span_result
        _advance_state(operation, ctx.scope.resources, ctx.state, ctx.states, self._font_of)
        if operation.operator in _SHOWS_TEXT and ctx.state.font is not None and ctx.state.font.usable:
            replacement = self._replace(operation, ctx)
            if replacement is not None:
                return list(replacement), index + 1, True
        form = ctx.scope.forms.get(index)
        if form is not None:
            copy_op = self._copy(form, operation, ctx)
            if copy_op is not None:
                return [copy_op], index + 1, True
        return [operation], index + 1, False

    def _handle_actual_text_span(
        self, ctx: _EditContext, start: int
    ) -> tuple[list[Operation | InlineImage], int, bool] | None:
        """Handle a ``BDC ... EMC`` span carrying ``/ActualText`` (ISO 32000-2 14.9.4)."""
        bdc = ctx.scope.operations[start]
        if not isinstance(bdc, Operation) or len(bdc.operands) != 2 or not isinstance(bdc.operands[0], Name):  # noqa: PLR2004
            return None
        props = _resolve_bdc_properties(bdc.operands[1], ctx.scope.resources)
        if props is None or "ActualText" not in props:
            return None
        emc = _find_matching_emc(ctx.scope.operations, start)
        if emc is None:
            return None
        actual_text = _decode_pdf_text_string(props["ActualText"])
        inner = list(ctx.scope.operations[start + 1 : emc])
        remaining = None if self.count is None else self.count - self.replaced
        occurrences = actual_text.count(self.old)
        replace_n = occurrences if remaining is None else min(occurrences, remaining)
        if replace_n <= 0:
            _advance_span_state(inner, ctx, self._font_of)
            return list(ctx.scope.operations[start : emc + 1]), emc + 1, False

        updated_text = actual_text.replace(self.old, self.new, replace_n)
        edited_inner = self._edit_actual_text_inner(ctx, inner, updated_text, replace_n)
        updated_props = dict(props)
        updated_props["ActualText"] = String(_encode_text_string(updated_text))
        new_bdc = Operation("BDC", (bdc.operands[0], updated_props))
        self.replaced += replace_n
        return [new_bdc, *edited_inner, ctx.scope.operations[emc]], emc + 1, True

    def _edit_actual_text_inner(
        self,
        ctx: _EditContext,
        inner: list[Operation | InlineImage],
        updated_text: str,
        replace_n: int,
    ) -> list[Operation | InlineImage]:
        """Rewrite text-showing operators inside a matching ``/ActualText`` span."""
        shown_indices, shown_states, positioned = _scan_span_shown(inner, ctx, self._font_of)
        first_state = shown_states[0] if shown_states else None
        same_font = (
            first_state is not None
            and first_state.font is not None
            and first_state.font.usable
            and all(s.font is first_state.font and s.size == first_state.size for s in shown_states)
        )
        if not positioned and same_font and first_state is not None:
            return self._rewrite_single_line_span(ctx, inner, shown_indices, shown_states, updated_text)
        return self._rewrite_positioned_span(ctx, inner, replace_n)

    def _rewrite_single_line_span(
        self,
        ctx: _EditContext,
        inner: list[Operation | InlineImage],
        shown_indices: list[int],
        shown_states: list[_State],
        updated_text: str,
    ) -> list[Operation | InlineImage]:
        first_state = shown_states[0]
        total_old_adv = 0.0
        for idx, op_state in zip(shown_indices, shown_states, strict=True):
            op = inner[idx]
            assert isinstance(op, Operation)  # noqa: S101
            tokens = _tokens(op, op_state.font)
            if tokens is not None:
                total_old_adv += _advance(tokens, op_state)
        encoded_tokens = self._encode_replacement(updated_text, first_state, ctx)
        new_adv = _advance(encoded_tokens, first_state)
        if first_state.size != 0:
            kern = round((new_adv - total_old_adv) * 1000 / first_state.size, 6)
            if kern:
                encoded_tokens.append(_Kern(kern))
        emitted = _emit_runs(encoded_tokens, first_state)
        result: list[Operation | InlineImage] = []
        shown_set = set(shown_indices)
        first_shown = shown_indices[0]
        for idx, op in enumerate(inner):
            if isinstance(op, Operation):
                _advance_state(op, ctx.scope.resources, ctx.state, ctx.states, self._font_of)
            if idx == first_shown:
                result.extend(emitted)
            elif idx not in shown_set:
                result.append(op)
        return result

    def _rewrite_positioned_span(
        self,
        ctx: _EditContext,
        inner: list[Operation | InlineImage],
        replace_n: int,
    ) -> list[Operation | InlineImage]:
        saved_replaced = self.replaced
        self.replaced = 0
        saved_count = self.count
        self.count = replace_n
        try:
            result: list[Operation | InlineImage] = []
            for op in inner:
                if isinstance(op, Operation):
                    _advance_state(op, ctx.scope.resources, ctx.state, ctx.states, self._font_of)
                    if op.operator in _SHOWS_TEXT and ctx.state.font is not None and ctx.state.font.usable:
                        rep = self._replace(op, ctx)
                        if rep is not None:
                            result.extend(rep)
                            continue
                result.append(op)
            matched = self.replaced
        finally:
            self.replaced = saved_replaced
            self.count = saved_count
        if matched == replace_n:
            return result
        msg = f"cannot replace {self.old!r} inside positioned /ActualText span with incompatible glyphs"
        raise ValueError(msg)

    def _copy(self, form: _Scope, operation: Operation, ctx: _EditContext) -> Operation | None:
        """Return the ``Do`` that draws a copy of ``form`` with its replacements, or ``None``."""
        edited = self._edit(form, ctx.state.copy(), ctx.budget)
        if edited is None:
            return None
        name = _fresh(str(operation.operands[0]), ctx.taken_xobjects)
        ctx.taken_xobjects.add(name)
        ctx.drawn[name] = len(self.copies)
        self.copies.append(_Copy(form, *edited))
        return Operation("Do", (Name(name),))

    def _replace(self, operation: Operation, ctx: _EditContext) -> list[Operation] | None:
        """Return replacement operations for ``operation``, or ``None`` when ``old`` is absent."""
        tokens = _tokens(operation, ctx.state.font)
        if tokens is None:
            return None
        edited = self._replace_tokens(tokens, ctx)
        if edited is None:
            return None
        shown_ops = _emit_runs(edited, ctx.state)
        operator, operands = operation.operator, operation.operands
        if operator == "'":
            return [Operation("T*"), *shown_ops]
        if operator == '"':
            return [Operation("Tw", (operands[0],)), Operation("Tc", (operands[1],)), Operation("T*"), *shown_ops]
        return shown_ops

    def _replace_tokens(self, tokens: list[_Glyph | _Kern], ctx: _EditContext) -> list[_Token] | None:
        """Return ``tokens`` with occurrences of ``old`` replaced, or ``None`` when none match."""
        glyphs = [index for index, token in enumerate(tokens) if isinstance(token, _Glyph)]
        starts: dict[int, int] = {}
        ends: dict[int, int] = {}
        text = ""
        for place, index in enumerate(glyphs):
            starts[len(text)] = place
            glyph = tokens[index]
            assert isinstance(glyph, _Glyph)  # noqa: S101
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

        replacement_tokens = self._encode_replacement(self.new, ctx.state, ctx)
        out: list[_Token] = []
        last = 0
        for first, final in found:
            out.extend(tokens[last:first])
            out.extend(replacement_tokens)
            kern = _compensation(tokens[first : final + 1], replacement_tokens, ctx.state)
            if kern:
                out.append(_Kern(kern))
            last = final + 1
        out.extend(tokens[last:])
        self.replaced += len(found)
        return out

    def _encode_replacement(self, text: str, state: _State, ctx: _EditContext) -> list[_Token]:
        """Encode ``text`` in ``state.font``, using editable extension or ``fallback`` when needed."""
        font = state.font
        assert font is not None  # noqa: S101
        if not text:
            return []
        if font.kind == "type0":
            return self._encode_type0(font, text, state, ctx)
        return self._encode_simple(font, text, state, ctx)

    def _encode_simple(self, font: _Font, text: str, state: _State, ctx: _EditContext) -> list[_Token]:
        reverse = font.simple_reverse_map()
        out: list[_Token] = []
        cursor = 0
        missing = ""
        in_fallback = False
        while cursor < len(text):
            matched = _longest_simple_match(text, cursor, reverse)
            if matched is not None:
                step, raw = matched
                if missing:
                    out.extend(self._encode_fallback(missing, state, ctx, vertical=False))
                    missing = ""
                    in_fallback = True
                if in_fallback:
                    out.append(_Switch(self._restore_font_name(state, ctx)))
                    in_fallback = False
                out.append(_Glyph(raw, text[cursor : cursor + step], font.advances.get(raw[0], 0.0)))
                cursor += step
            else:
                missing += text[cursor]
                cursor += 1
        if missing:
            out.extend(self._encode_fallback(missing, state, ctx, vertical=False))
            in_fallback = True
        if in_fallback:
            out.append(_Switch(self._restore_font_name(state, ctx)))
        return out

    def _encode_type0(self, font: _Font, text: str, state: _State, ctx: _EditContext) -> list[_Token]:
        core = self.page.document._core()  # noqa: SLF001
        plan = _core.type0_encode(
            core,
            font.dictionary._root,  # noqa: SLF001
            list(font.dictionary._path),  # noqa: SLF001
            text,
            operation=ctx.budget,
        )
        if plan is None:
            msg = f"the composite font {font.base_font or 'of the text'} cannot be encoded"
            raise ValueError(msg)
        vertical, segments, extensions = plan
        for gid, width, v_metrics, g_text in extensions:
            font.extensions.setdefault(gid, (width, v_metrics, g_text))
        out: list[_Token] = []
        in_fallback = False
        for is_glyph, code_bytes, seg_text, advance, kern_after in segments:
            if is_glyph:
                if in_fallback:
                    out.append(_Switch(self._restore_font_name(state, ctx)))
                    in_fallback = False
                out.append(_Glyph(code_bytes, seg_text, advance))
                if kern_after:
                    out.append(_Kern(kern_after))
            else:
                out.extend(self._encode_fallback(seg_text, state, ctx, vertical=vertical))
                in_fallback = True
        if in_fallback:
            out.append(_Switch(self._restore_font_name(state, ctx)))
        return out

    def _encode_fallback(self, text: str, state: _State, ctx: _EditContext, *, vertical: bool) -> list[_Token]:
        """Encode missing text slice ``text`` using ``self.fallback`` or raise ``ValueError``."""
        if self.fallback is None:
            font = state.font
            name = (font.base_font if font is not None else None) or "of the text"
            first = text[0]
            msg = f"the font {name} has no code for {first!r} (U+{ord(first):04X})"
            raise ValueError(msg)
        fb = self.fallback
        fb_name = self._fallback_font_name(ctx, vertical=vertical)
        tokens: list[_Token] = [_Switch(fb_name)]
        if fb.program() is None:
            if vertical:
                msg = "cannot use a Standard 14 fallback font for vertical text"
                raise ValueError(msg)
            try:
                encoded = fb._font.encode(text, operation=ctx.budget)  # noqa: SLF001
                widths = fb.char_widths(text, 1000.0)
            except PdfError as err:
                msg = f"fallback font {fb.name!r} cannot encode {text!r}: {err}"
                raise ValueError(msg) from err
            for byte, ch, width in zip(encoded, text, widths, strict=True):
                tokens.append(_Glyph(bytes([byte]), ch, width))
            return tokens

        try:
            shaped = _core.shape_text_fallback([fb._font], text, vertical=vertical, operation=ctx.budget)  # noqa: SLF001
        except (PdfError, ValueError) as err:
            msg = f"fallback font {fb.name!r} cannot encode {text!r}: {err}"
            raise ValueError(msg) from err
        gids = sorted({row[1] for row in shaped})
        if not gids:
            return []
        em = fb._font.units_per_em  # noqa: SLF001
        scale = 1000.0 / em
        h_widths = dict(zip(gids, fb._font.glyph_widths(gids), strict=True))  # noqa: SLF001
        v_metrics = dict(zip(gids, fb._font.glyph_v_metrics(gids), strict=True)) if vertical else {}  # noqa: SLF001
        for _, gid, x_adv, y_adv, _, _, _, _, _, t_start, t_end in shaped:
            g_text = text[t_start:t_end]
            self._fallback_glyphs[vertical].append((gid, g_text))
            nominal = v_metrics[gid][0] if vertical else h_widths[gid]
            actual = (y_adv if vertical else x_adv) * scale
            tokens.append(_Glyph(gid.to_bytes(2, "big"), g_text, nominal))
            diff = nominal - actual
            if abs(diff) > 0.01:  # noqa: PLR2004
                tokens.append(_Kern(round(diff, 3)))
        return tokens

    def _fallback_font_name(self, ctx: _EditContext, *, vertical: bool) -> Name:
        marker = ("fallback", vertical)
        for existing_name, existing_marker in ctx.added_fonts.items():
            if existing_marker == marker:
                return Name(existing_name)
        name = _fresh("F_fb", ctx.taken_fonts)
        ctx.taken_fonts.add(name)
        ctx.added_fonts[name] = marker
        return Name(name)

    def _restore_font_name(self, state: _State, ctx: _EditContext) -> Name:
        if state.font_name is not None:
            return state.font_name
        assert state.font is not None  # noqa: S101
        marker = ("orig", state.font.dictionary)
        for existing_name, existing_marker in ctx.added_fonts.items():
            if existing_marker == marker:
                return Name(existing_name)
        name = _fresh("F_orig", ctx.taken_fonts)
        ctx.taken_fonts.add(name)
        ctx.added_fonts[name] = marker
        return Name(name)

    def write(self) -> None:
        """Write font extensions, fallback fonts, copied Forms, and page content in one transaction."""
        document = self.page.document
        with document.transaction():
            for font in self.fonts.values():
                if font.extensions:
                    _apply_type0_extensions(font)
            fb_objects = self._materialize_fallbacks(document)
            numbers = self._write_copies(document, fb_objects)
            if self.operations is not None:
                self.page.operations = Operations(self.operations, tail=self.root.operations.tail)
            root_fonts = _resolve_added_fonts(self.root_added_fonts, fb_objects)
            root_drawn = {name: numbers[place] for name, place in self.drawn.items()}
            if root_drawn or root_fonts:
                _repoint_page(self.page, root_drawn, root_fonts)
            if self.copies:
                _repoint_structure_streams(self.page, self.copies, numbers)

    def _materialize_fallbacks(self, document: Any) -> dict[bool, object]:  # noqa: ANN401
        fb_objects: dict[bool, object] = {}
        if self.fallback is None:
            return fb_objects
        fb_cache: dict[bytes, int] = {}
        for vertical in (False, True):
            if self._needs_fallback(vertical=vertical):
                fb_objects[vertical] = self._materialize_fallback(document, fb_cache, vertical=vertical)
        return fb_objects

    def _write_copies(self, document: Any, fb_objects: dict[bool, object]) -> list[int]:  # noqa: ANN401
        numbers: list[int] = []
        for copy in self.copies:
            stream = copy.scope.stream
            assert stream is not None  # noqa: S101
            dictionary = {key: value for key, value in stream.items() if key not in _STREAM_KEYS}
            drawn = {name: numbers[place] for name, place in copy.drawn.items()}
            fonts_to_add = _resolve_added_fonts(copy.added_fonts, fb_objects)
            if drawn or fonts_to_add:
                dictionary["Resources"] = self._own_resources(copy.scope, drawn, fonts_to_add)
            data = Operations(copy.operations, tail=copy.scope.operations.tail).to_bytes()
            numbers.append(document.objects.add_stream(data, dictionary))
        return numbers

    def _needs_fallback(self, *, vertical: bool) -> bool:
        marker = ("fallback", vertical)
        if marker in self.root_added_fonts.values():
            return True
        return any(marker in copy.added_fonts.values() for copy in self.copies)

    def _materialize_fallback(self, document: Any, cache: dict[bytes, int], *, vertical: bool) -> object:  # noqa: ANN401
        fb = self.fallback
        assert fb is not None  # noqa: S101
        if fb.program() is None:
            assert fb.name is not None  # noqa: S101 - every Standard 14 font has a name.
            return _standard_font_dict(fb.name)
        xref = embedded_font(document, fb, cache, vertical=vertical)
        glyphs = self._fallback_glyphs[vertical]
        if glyphs:
            add_glyphs(document, xref, fb, glyphs, vertical=vertical)
        return document.objects[xref]

    def _own_resources(self, scope: _Scope, drawn: dict[str, int], added_fonts: dict[str, object]) -> dict[str, Any]:
        assert scope.stream is not None  # noqa: S101
        resources = scope.stream.get("Resources")
        if not isinstance(resources, Dictionary):
            resources = scope.resources
        own = dict(resources.items()) if isinstance(resources, Dictionary) else {}
        if drawn:
            xobjects = own.get("XObject")
            own["XObject"] = dict(xobjects.items()) if isinstance(xobjects, Dictionary) else {}
            document = self.page.document
            for name, number in drawn.items():
                own["XObject"][name] = document.objects[number]
        if added_fonts:
            fonts = own.get("Font")
            own["Font"] = dict(fonts.items()) if isinstance(fonts, Dictionary) else {}
            own["Font"].update(added_fonts)
        return own


def _scan_span_shown(
    inner: list[Operation | InlineImage],
    ctx: _EditContext,
    font_of: Any,  # noqa: ANN401
) -> tuple[list[int], list[_State], bool]:
    """Scan an ``/ActualText`` span's inner operations to locate text-showing ops and positioning ops."""
    probe_state = ctx.state.copy()
    probe_stack = [s.copy() for s in ctx.states]
    shown_indices: list[int] = []
    shown_states: list[_State] = []
    positioned = False
    for idx, op in enumerate(inner):
        if not isinstance(op, Operation):
            continue
        if (shown_indices and op.operator in _TEXT_POSITION_OPS) or (op.operator in ("'", '"') and idx > 0):
            positioned = True
        _advance_state(op, ctx.scope.resources, probe_state, probe_stack, font_of)
        if op.operator in _SHOWS_TEXT:
            shown_indices.append(idx)
            shown_states.append(probe_state.copy())
    return shown_indices, shown_states, positioned


def _advance_span_state(
    inner: list[Operation | InlineImage],
    ctx: _EditContext,
    font_of: Any,  # noqa: ANN401
) -> None:
    for op in inner:
        if isinstance(op, Operation):
            _advance_state(op, ctx.scope.resources, ctx.state, ctx.states, font_of)


def _resolve_added_fonts(added: dict[str, object], fb_objects: dict[bool, object]) -> dict[str, object]:
    resolved: dict[str, object] = {}
    for name, marker in added.items():
        if isinstance(marker, tuple) and len(marker) == 2:  # noqa: PLR2004
            kind, value = marker
            if kind == "fallback" and isinstance(value, bool):
                resolved[name] = fb_objects[value]
            elif kind == "orig":
                resolved[name] = value
    return resolved


def _standard_font_dict(base: str) -> dict[str, object]:
    font: dict[str, object] = {"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name(base)}
    if base not in _SYMBOLIC_STANDARD:
        font["Encoding"] = Name("WinAnsiEncoding")
    return font


def _apply_type0_extensions(font: _Font) -> None:
    """Extend an un-subsetted embedded Type 0 font's ``/W``, ``/W2``, and ``/ToUnicode``."""
    descendants = font.dictionary.get("DescendantFonts")
    if not isinstance(descendants, Array) or not descendants:
        return
    descendant = descendants[0]
    to_unicode = font.dictionary.get("ToUnicode")
    if not isinstance(descendant, Dictionary) or not isinstance(to_unicode, Stream):
        return
    _extend_w(descendant, font.extensions)
    if font.vertical:
        _extend_w2(descendant, font.extensions)
    _extend_tounicode(to_unicode, font.extensions)


def _extend_w(
    descendant: Dictionary, extensions: dict[int, tuple[float, tuple[float, float, float] | None, str]]
) -> None:
    widths = _read_w_map(descendant)
    changed = False
    for gid, (width, _, _) in extensions.items():
        if gid not in widths:
            widths[gid] = width
            changed = True
    if changed:
        descendant["W"] = _w_array(widths)


def _extend_w2(
    descendant: Dictionary, extensions: dict[int, tuple[float, tuple[float, float, float] | None, str]]
) -> None:
    widths2 = _read_w2_map(descendant)
    changed = False
    for gid, (_, v_metrics, _) in extensions.items():
        if v_metrics is not None and gid not in widths2:
            widths2[gid] = v_metrics
            changed = True
    if changed:
        if "DW2" not in descendant:
            descendant["DW2"] = [880, -1000]
        descendant["W2"] = _w2_array(widths2)


def _extend_tounicode(
    to_unicode: Stream, extensions: dict[int, tuple[float, tuple[float, float, float] | None, str]]
) -> None:
    mapped = _read_tounicode_map(to_unicode)
    changed = False
    for gid, (_, _, text) in extensions.items():
        if text and mapped.get(gid) != text:
            mapped[gid] = text
            changed = True
    if changed:
        to_unicode.write(_cmap(mapped))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _read_w_map(descendant: Dictionary) -> dict[int, float]:
    """Read ``/W`` entries (both ``c [w ...]`` and ``c1 c2 w`` forms) into a glyph-to-width map."""
    widths: dict[int, float] = {}
    entries = descendant.get("W")
    if not isinstance(entries, Array):
        return widths
    items = list(entries)
    i = 0
    while i + 1 < len(items) and _is_int(items[i]):
        first = int(items[i])
        nxt = items[i + 1]
        if isinstance(nxt, Array):
            for offset, w in enumerate(nxt):
                if _is_num(w):
                    widths[first + offset] = float(w)
            i += 2
        elif _is_int(nxt) and i + 2 < len(items) and _is_num(items[i + 2]):
            last = int(nxt)
            w_val = float(items[i + 2])
            if 0 <= last - first <= _MAX_RANGE_EXPAND:
                widths.update(dict.fromkeys(range(first, last + 1), w_val))
            i += 3
        else:
            break
    return widths


def _read_w2_map(descendant: Dictionary) -> dict[int, tuple[float, float, float]]:
    """Read ``/W2`` entries (both array and range forms) into a glyph-to-``(w1y, vx, vy)`` map."""
    metrics: dict[int, tuple[float, float, float]] = {}
    entries = descendant.get("W2")
    if not isinstance(entries, Array):
        return metrics
    items = list(entries)
    i = 0
    while i + 1 < len(items) and _is_int(items[i]):
        first = int(items[i])
        nxt = items[i + 1]
        if isinstance(nxt, Array):
            _read_w2_array_run(metrics, first, list(nxt))
            i += 2
        elif _is_int(nxt) and i + 4 < len(items):
            last = int(nxt)
            triple = items[i + 2 : i + 5]
            if all(_is_num(v) for v in triple) and 0 <= last - first <= _MAX_RANGE_EXPAND:
                entry = (float(triple[0]), float(triple[1]), float(triple[2]))
                metrics.update(dict.fromkeys(range(first, last + 1), entry))
            i += 5
        else:
            break
    return metrics


def _read_w2_array_run(metrics: dict[int, tuple[float, float, float]], first: int, vals: list[Any]) -> None:
    for offset in range(len(vals) // 3):
        triple = vals[3 * offset : 3 * offset + 3]
        if all(_is_num(v) for v in triple):
            metrics[first + offset] = (float(triple[0]), float(triple[1]), float(triple[2]))


def _read_tounicode_map(stream: Stream) -> dict[int, str]:
    """Read all ``bfchar`` and ``bfrange`` mappings from a ``/ToUnicode`` stream."""
    mapped: dict[int, str] = {}
    data = stream.read()
    for block in _BFCHAR_BLOCKS.findall(data):
        for code_hex, text_hex in _BFCHAR_ROW.findall(block):
            if len(text_hex) % 4 == 0:
                mapped[int(code_hex, 16)] = bytes.fromhex(text_hex.decode()).decode("utf-16-be", "surrogatepass")
    for block in _BFRANGE_BLOCKS.findall(data):
        for row in _BFRANGE_ROW.findall(block):
            _parse_bfrange_row(mapped, row)
    return mapped


def _parse_bfrange_row(mapped: dict[int, str], row: tuple[bytes, bytes, bytes, bytes, bytes]) -> None:
    start_hex, end_hex, _, single_hex, arr_body = row
    start_code = int(start_hex, 16)
    end_code = int(end_hex, 16)
    if end_code < start_code or end_code - start_code > _MAX_RANGE_EXPAND:
        return
    if arr_body:
        for offset, item_hex in enumerate(_HEX_STR.findall(arr_body)):
            if start_code + offset > end_code:
                break
            if len(item_hex) % 4 == 0:
                mapped[start_code + offset] = bytes.fromhex(item_hex.decode()).decode("utf-16-be", "surrogatepass")
        return
    if single_hex and len(single_hex) % 4 == 0:
        raw = bytes.fromhex(single_hex.decode())
        base_units = [int.from_bytes(raw[k : k + 2], "big") for k in range(0, len(raw), 2)]
        for offset in range(end_code - start_code + 1):
            last_u = base_units[-1] + offset
            if last_u > 0xFFFF:  # noqa: PLR2004
                break
            units = [*base_units[:-1], last_u]
            raw_out = b"".join(u.to_bytes(2, "big") for u in units)
            mapped[start_code + offset] = raw_out.decode("utf-16-be", "surrogatepass")


def _longest_simple_match(text: str, cursor: int, reverse: dict[str, bytes]) -> tuple[int, bytes] | None:
    limit = min(_MAX_LIGATURE, len(text) - cursor)
    for step in range(limit, 0, -1):
        code = reverse.get(text[cursor : cursor + step])
        if code is not None:
            return step, code
    return None


def _resolve_bdc_properties(operand: object, resources: Dictionary | None) -> Mapping[str, Any] | None:
    if isinstance(operand, Mapping):
        return operand
    if isinstance(operand, Name):
        props_dict = _sub(resources, "Properties")
        entry = props_dict.get(operand) if props_dict is not None else None
        if isinstance(entry, Dictionary):
            return dict(entry.items())
    return None


def _find_matching_emc(ops: Sequence[Operation | InlineImage], start: int) -> int | None:
    depth = 1
    for idx in range(start + 1, len(ops)):
        op = ops[idx]
        if not isinstance(op, Operation):
            continue
        if op.operator in ("BMC", "BDC"):
            depth += 1
        elif op.operator == "EMC":
            depth -= 1
            if depth == 0:
                return idx
    return None


def _decode_pdf_text_string(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return _core.decode_text_string(bytes(value))
    return ""


def _advance_state(
    operation: Operation,
    resources: Dictionary | None,
    state: _State,
    states: list[_State],
    font_of: Any,  # noqa: ANN401
) -> None:
    if operation.operator == "q":
        states.append(state.copy())
    elif operation.operator == "Q":
        if states:
            saved = states.pop()
            state.font = saved.font
            state.font_name = saved.font_name
            state.size = saved.size
            state.char_spacing = saved.char_spacing
            state.word_spacing = saved.word_spacing
    else:
        state.apply(operation, resources, font_of)


def _taken_names(scope: _Scope, resource_key: str, operator: str) -> set[str]:
    """Return resource names already defined or referenced by ``operator`` in ``scope``."""
    sub = _sub(scope.resources, resource_key)
    names = set(sub) if sub is not None else set()
    pending = [scope]
    while pending:
        current = pending.pop()
        names.update(
            str(operation.operands[0])
            for operation in current.operations
            if isinstance(operation, Operation) and operation.operator == operator and operation.operands
        )
        pending.extend(form for form in current.forms.values() if form.resources is scope.resources)
    return names


def _fresh(name: str, taken: set[str]) -> str:
    number = 1
    while f"{name}_{number}" in taken:
        number += 1
    return f"{name}_{number}"


def _repoint_structure_streams(page: Page, copies: list[_Copy], numbers: list[int]) -> None:
    """Update ``/MCR /Stm`` pointers on ``page`` when a tagged Form XObject is copied."""
    document = page.document
    struct_root = document.catalog.get("StructTreeRoot")
    if not isinstance(struct_root, Dictionary):
        return
    parent_tree = struct_root.get("ParentTree")
    if not isinstance(parent_tree, Dictionary):
        return
    # Map old tagged Form xref -> new copy xref when replaced uniquely on this page.
    replaced: dict[int, tuple[int, int]] = {}
    counts: dict[int, int] = {}
    for copy, new_xref in zip(copies, numbers, strict=True):
        stream = copy.scope.stream
        if stream is None or stream.xref is None:
            continue
        sp_key = stream.get("StructParents")
        if isinstance(sp_key, int) and not isinstance(sp_key, bool):
            counts[stream.xref] = counts.get(stream.xref, 0) + 1
            replaced[stream.xref] = (sp_key, new_xref)
    for old_xref, (sp_key, new_xref) in replaced.items():
        if counts.get(old_xref) != 1:
            continue
        entry = _number_tree_get(parent_tree, sp_key)
        elems = list(entry) if isinstance(entry, Array) else ([entry] if isinstance(entry, Dictionary) else [])
        seen: set[int] = set()
        for elem in elems:
            if not isinstance(elem, Dictionary) or elem.xref is None or elem.xref in seen:
                continue
            seen.add(elem.xref)
            _repoint_elem_mcr_stm(elem, page.obj, old_xref, document.objects[new_xref])


def _number_tree_get(node: Dictionary, target: int, depth: int = 0) -> object:
    """Look up integer key ``target`` in a PDF number tree (ISO 32000-2 7.9.7)."""
    if depth >= 32:  # noqa: PLR2004 - bounded tree depth (F6).
        return None
    found = _number_tree_nums_get(node.get("Nums"), target)
    if found is not None:
        return found
    kids = node.get("Kids")
    if not isinstance(kids, Array):
        return None
    for kid in kids:
        if not isinstance(kid, Dictionary) or not _number_tree_in_limits(kid.get("Limits"), target):
            continue
        found = _number_tree_get(kid, target, depth + 1)
        if found is not None:
            return found
    return None


def _number_tree_nums_get(nums: object, target: int) -> object:
    if not isinstance(nums, Array):
        return None
    items = list(nums)
    for i in range(0, len(items) - 1, 2):
        key = items[i]
        if isinstance(key, int) and not isinstance(key, bool) and key == target:
            return items[i + 1]
    return None


def _number_tree_in_limits(limits: object, target: int) -> bool:
    if (
        isinstance(limits, Array)
        and len(limits) == 2  # noqa: PLR2004
        and isinstance(limits[0], int)
        and not isinstance(limits[0], bool)
        and isinstance(limits[1], int)
        and not isinstance(limits[1], bool)
    ):
        return limits[0] <= target <= limits[1]
    return True


def _repoint_elem_mcr_stm(elem: Dictionary, page_obj: Dictionary, old_xref: int, new_stream: Stream) -> None:
    """Repoint ``/Stm`` on ``/MCR`` children of ``elem`` belonging to ``page_obj``."""
    elem_pg = elem.get("Pg")
    kids = elem.get("K")
    items = list(kids) if isinstance(kids, Array) else ([kids] if isinstance(kids, Dictionary) else [])
    for item in items:
        if not isinstance(item, Dictionary) or item.get("Type") != Name("MCR"):
            continue
        stm = item.get("Stm")
        if not isinstance(stm, Stream) or stm.xref != old_xref:
            continue
        pg = item.get("Pg", elem_pg)
        if isinstance(pg, Dictionary) and pg.xref == page_obj.xref:
            item["Stm"] = new_stream


def _repoint_page(page: Page, drawn: dict[str, int], added_fonts: dict[str, object]) -> None:
    """Attach copied Forms and added fallback Fonts to the page's own Resources."""
    own = page.obj.get("Resources")
    if own is None or own.is_indirect:
        inherited = own if own is not None else _resources(page)
        page.obj["Resources"] = {} if inherited is None else dict(inherited.items())
    resources = page.obj["Resources"]
    document = page.document
    if drawn:
        xobjects = resources.get("XObject")
        if xobjects is None or xobjects.is_indirect:
            resources["XObject"] = {} if xobjects is None else dict(xobjects.items())
        for name, number in drawn.items():
            resources["XObject"][name] = document.objects[number]
    if added_fonts:
        fonts = resources.get("Font")
        if fonts is None or fonts.is_indirect:
            resources["Font"] = {} if fonts is None else dict(fonts.items())
        for name, font_obj in added_fonts.items():
            resources["Font"][name] = font_obj


def _resources(page: Page) -> Dictionary | None:
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
    return dictionary.xref if dictionary.is_indirect else dictionary


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _pdf_num(value: float) -> int | Real:
    num = float(value)
    return int(num) if num.is_integer() else Real(num)


def _raw_strings(operation: Operation) -> list[bytes]:
    operands = operation.operands
    if operation.operator == "TJ":
        if len(operands) != 1 or not isinstance(operands[0], tuple):
            return []
        return [bytes(el) for el in operands[0] if isinstance(el, bytes)]
    if not operands or not isinstance(operands[-1], bytes):
        return []
    return [bytes(operands[-1])]


def _probe(page: Page, fonts: list[_Font]) -> None:
    """Show every byte code of ``fonts`` on ``page`` in a discarded transaction, and read chars."""
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
    probe_box = [0, 0, 16 * _PROBE_STEP, (len(fonts) * 16 + 1) * _PROBE_STEP]
    with contextlib.suppress(_Rollback), document.transaction():
        page.obj["Resources"] = {"Font": {f"P{number}": font.dictionary for number, font in enumerate(fonts)}}
        if "UserUnit" in page.obj:
            del page.obj["UserUnit"]
        page.obj["Rotate"] = 0
        page.obj["MediaBox"] = probe_box
        page.obj["CropBox"] = probe_box
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
    """Return glyphs and kerning of a text-showing operation, or ``None`` if malformed."""
    assert font is not None  # noqa: S101
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
            raw = bytes(element)
            if font.kind == "type0":
                tokens.extend(font.decoded.get(raw, ()))
            else:
                tokens.extend(
                    _Glyph(bytes([code]), font.texts.get(code, "\ufffd"), font.advances.get(code, 0.0)) for code in raw
                )
        elif isinstance(element, (int, float)) and not isinstance(element, bool):
            tokens.append(_Kern(float(element)))
    return tokens


def _advance(tokens: Sequence[_Token], state: _State) -> float:
    """Return displacement of ``tokens`` along the active writing axis in unscaled text space."""
    total = 0.0
    for token in tokens:
        if isinstance(token, _Kern):
            total -= token.amount / 1000 * state.size
        elif isinstance(token, _Glyph):
            total += token.advance / 1000 * state.size + state.char_spacing
            # ISO 32000-2 9.3.3: Tw applies ONLY to the single-byte code 0x20.
            if token.code == _SPACE_BYTE:
                total += state.word_spacing
    return total


def _compensation(old: Sequence[_Token], new: Sequence[_Token], state: _State) -> float:
    """Return the trailing ``TJ`` kerning that gives ``new`` the exact displacement of ``old``."""
    if state.size == 0:
        return 0.0
    difference = _advance(new, state) - _advance(old, state)
    return round(difference * 1000 / state.size, 6)


def _emit_runs(tokens: Sequence[_Token], state: _State) -> list[Operation]:
    """Convert ``tokens`` (glyphs, kerning, and font switches) into PDF ``TJ`` / ``Tf`` operations."""
    ops: list[Operation] = []
    run: list[_Glyph | _Kern] = []
    size_operand = _pdf_num(state.size)
    current_font = state.font_name

    def flush() -> None:
        if not run:
            return
        array = tuple(_group(run))
        if array:
            ops.append(Operation("TJ", (array,)))
        run.clear()

    for token in tokens:
        if isinstance(token, _Switch):
            if token.font_name != current_font:
                flush()
                ops.append(Operation("Tf", (token.font_name, size_operand)))
                current_font = token.font_name
        else:
            run.append(token)
    flush()
    if not ops:
        ops.append(Operation("TJ", ((),)))
    return ops


def _group(tokens: Sequence[_Glyph | _Kern]) -> Iterator[String | int | Real]:
    """Yield elements of a ``TJ`` array: runs of code bytes as strings, and kerning as numbers."""
    codes = bytearray()
    for token in tokens:
        if isinstance(token, _Glyph):
            codes.extend(token.code)
            continue
        if codes:
            yield String(bytes(codes))
            codes.clear()
        yield _pdf_num(float(token.amount))
    if codes:
        yield String(bytes(codes))


__all__ = ["replace_text"]
