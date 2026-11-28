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
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyropdf import _core
from pyropdf._errors import LimitError, PdfError
from pyropdf._fonts import (
    _MAX_MAPPED_UNITS,
    _NO_TEXT,
    Font,
    _cmap,
    _w2_array,
    _w_array,
    add_glyphs,
    embedded_font,
)
from pyropdf._limits import _validate_optional_positive_int
from pyropdf._objects import Array, Dictionary, Name, Real, Stream, String, _encode, _encode_text_string
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
#: Maximum nodes visited in a single ``/ParentTree`` number-tree lookup (F6).
_MAX_PARENT_TREE_NODES = 4096
#: Maximum nesting depth when converting named ``/Properties`` into inline operands (F6).
_MAX_OPERAND_DEPTH = 64


class _Rollback(Exception):  # noqa: N818 - control flow, never raised to a caller.
    """Ends the transaction that shows every code of the simple fonts."""


@dataclass(slots=True)
class _Font:
    """A font used by a text-showing operation, with its decoded metrics and mappings."""

    dictionary: Dictionary
    kind: str | bool
    """One of ``"simple"``, ``"type3"``, ``"type0"``, or ``"other"``."""
    vertical: bool = False
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
    existing_tounicode: dict[int, str] | None = None
    """Existing ``/ToUnicode`` mappings parsed by Rust when extending an editable Type 0 font."""

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
    taken_properties: set[str]
    drawn: dict[str, int]
    added_fonts: dict[str, object]
    added_properties: dict[str, dict[str, object]]
    budget: _core._OperationBudget


@dataclass(slots=True)
class _Copy:
    """A drawing of a Form XObject that changed, which becomes a new Form."""

    scope: _Scope
    operations: list[Operation | InlineImage]
    drawn: dict[str, int]
    added_fonts: dict[str, object]
    added_properties: dict[str, dict[str, object]]


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
        self.root_added_properties: dict[str, dict[str, object]] = {}
        self.copies: list[_Copy] = []
        self._fallback_glyphs: dict[bool, list[tuple[int, str]]] = {False: [], True: []}
        self._type0_plans: dict[
            tuple[object, str],
            tuple[
                bool,
                list[tuple[bool, bytes, str, float, float]],
                list[tuple[int, float, tuple[float, float, float] | None, str]],
                list[tuple[int, str]],
            ],
        ] = {}
        self._tagged_shared_elsewhere: set[int] = set()

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
            self.operations, self.drawn, self.root_added_fonts, self.root_added_properties = edited
        if self.copies:
            tagged_xrefs = {
                copy.scope.stream.xref
                for copy in self.copies
                if copy.scope.stream is not None
                and copy.scope.stream.xref is not None
                and _is_int(copy.scope.stream.get("StructParents"))
            }
            if tagged_xrefs:
                self._tagged_shared_elsewhere = _core.read_operation(
                    core,
                    lambda budget: _other_pages_use_xobjects(self.page.document, self.page, tagged_xrefs, budget),
                )

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
                    font.vertical, _, font.base_font = info
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
    ) -> tuple[list[Operation | InlineImage], dict[str, int], dict[str, object], dict[str, dict[str, object]]] | None:
        """Return edited operations, copied Form XObjects, added Fonts, and added Properties, or ``None``."""
        ctx = _EditContext(
            scope=scope,
            state=state,
            states=[],
            taken_xobjects=_taken_names(scope, "XObject", "Do") if scope.forms else set(),
            taken_fonts=_taken_names(scope, "Font", "Tf"),
            taken_properties=_taken_property_names(scope),
            drawn={},
            added_fonts={},
            added_properties={},
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
        return (out, ctx.drawn, ctx.added_fonts, ctx.added_properties) if changed else None

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
        inline_ok, inline_props = _to_inline_operand(updated_props)
        if inline_ok:
            new_bdc = Operation("BDC", (bdc.operands[0], inline_props))
        else:
            base_name = str(bdc.operands[1]) if isinstance(bdc.operands[1], Name) else "P_at"
            prop_name = _fresh(base_name, ctx.taken_properties)
            ctx.taken_properties.add(prop_name)
            ctx.added_properties[prop_name] = updated_props
            new_bdc = Operation("BDC", (bdc.operands[0], Name(prop_name)))
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
            if len(operands) >= 3:  # noqa: PLR2004 - aw, ac, and the string.
                return [Operation("Tw", (operands[0],)), Operation("Tc", (operands[1],)), Operation("T*"), *shown_ops]
            return [Operation("T*"), *shown_ops]
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
                    fb_tokens = self._encode_fallback(missing, state, ctx, vertical=False)
                    missing = ""
                    if fb_tokens:
                        out.extend(fb_tokens)
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
            fb_tokens = self._encode_fallback(missing, state, ctx, vertical=False)
            if fb_tokens:
                out.extend(fb_tokens)
                in_fallback = True
        if in_fallback:
            out.append(_Switch(self._restore_font_name(state, ctx)))
        return out

    def _type0_plan(
        self,
        font: _Font,
        text: str,
        budget: _core._OperationBudget,
    ) -> tuple[
        bool,
        list[tuple[bool, bytes, str, float, float]],
        list[tuple[int, float, tuple[float, float, float] | None, str]],
        list[tuple[int, str]],
    ]:
        cache_key = (_key(font.dictionary), text)
        plan = self._type0_plans.get(cache_key)
        if plan is not None:
            return plan
        core = self.page.document._core()  # noqa: SLF001
        plan = _core.type0_encode(
            core,
            font.dictionary._root,  # noqa: SLF001
            list(font.dictionary._path),  # noqa: SLF001
            text,
            operation=budget,
        )
        if plan is None:
            msg = f"the composite font {font.base_font or 'of the text'} cannot be encoded"
            raise ValueError(msg)
        self._type0_plans[cache_key] = plan
        _, _, extensions, existing_tounicode = plan
        if extensions:
            if font.existing_tounicode is None:
                font.existing_tounicode = dict(existing_tounicode)
            for gid, width, v_metrics, g_text in extensions:
                existing = font.extensions.get(gid)
                if existing is None:
                    font.extensions[gid] = (width, v_metrics, g_text)
                elif not existing[2] and g_text:
                    font.extensions[gid] = (existing[0], existing[1], g_text)
        return plan

    def _encode_type0(self, font: _Font, text: str, state: _State, ctx: _EditContext) -> list[_Token]:
        vertical, segments, _, _ = self._type0_plan(font, text, ctx.budget)
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
                fb_tokens = self._encode_fallback(seg_text, state, ctx, vertical=vertical)
                if fb_tokens:
                    out.extend(fb_tokens)
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
        if fb.program() is None:
            return self._encode_standard_fallback(fb, text, ctx, vertical=vertical)
        return self._encode_embedded_fallback(fb, text, ctx, vertical=vertical)

    def _encode_standard_fallback(self, fb: Font, text: str, ctx: _EditContext, *, vertical: bool) -> list[_Token]:
        if vertical:
            msg = "cannot use a Standard 14 fallback font for vertical text"
            raise ValueError(msg)
        try:
            encoded = fb._font.encode(text, operation=ctx.budget)  # noqa: SLF001
            widths = fb.char_widths(text, 1000.0)
        except PdfError as err:
            msg = f"fallback font {fb.name!r} cannot encode {text!r}: {err}"
            raise ValueError(msg) from err
        if not encoded:
            return []
        fb_name = self._fallback_font_name(ctx, vertical=vertical)
        tokens: list[_Token] = [_Switch(fb_name)]
        for byte, ch, width in zip(encoded, text, widths, strict=True):
            tokens.append(_Glyph(bytes([byte]), ch, width))
        return tokens

    def _encode_embedded_fallback(self, fb: Font, text: str, ctx: _EditContext, *, vertical: bool) -> list[_Token]:
        if not fb._font.outlines:  # noqa: SLF001
            msg = f"fallback font {fb.name!r} has colour bitmaps and no vector outlines"
            raise ValueError(msg)
        try:
            shaped = _core.shape_text_fallback([fb._font], text, vertical=vertical, operation=ctx.budget)  # noqa: SLF001
        except (PdfError, ValueError) as err:
            msg = f"fallback font {fb.name!r} cannot encode {text!r}: {err}"
            raise ValueError(msg) from err
        if any(row[1] == 0 or row[1] > 0xFFFF for row in shaped):  # noqa: PLR2004
            msg = f"fallback font {fb.name!r} cannot encode {text!r}: missing glyph"
            raise ValueError(msg)
        gids = sorted({row[1] for row in shaped})
        if not gids:
            return []
        fb_name = self._fallback_font_name(ctx, vertical=vertical)
        tokens: list[_Token] = [_Switch(fb_name)]
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
        assert state.font is not None  # noqa: S101
        if state.font_name is not None:
            fonts = _sub(ctx.scope.resources, "Font")
            local = fonts.get(state.font_name) if fonts is not None else None
            if isinstance(local, Dictionary) and _key(local) == _key(state.font.dictionary):
                return state.font_name
        target_key = _key(state.font.dictionary)
        for existing_name, existing_marker in ctx.added_fonts.items():
            if (
                isinstance(existing_marker, tuple)
                and len(existing_marker) == 2  # noqa: PLR2004
                and existing_marker[0] == "orig"
                and isinstance(existing_marker[1], Dictionary)
                and _key(existing_marker[1]) == target_key
            ):
                return Name(existing_name)
        name = _fresh("F_orig", ctx.taken_fonts)
        ctx.taken_fonts.add(name)
        ctx.added_fonts[name] = ("orig", state.font.dictionary)
        return Name(name)

    def write(self) -> None:
        """Write font extensions, fallback fonts, copied Forms, and page content in one transaction."""
        document = self.page.document
        total_form_counts = _count_form_drawings(self.root) if self.copies else {}
        with document.transaction():
            for font in self.fonts.values():
                if font.extensions:
                    _apply_type0_extensions(font)
            fb_objects = self._materialize_fallbacks(document)
            numbers = self._write_copies(document, fb_objects, total_form_counts)
            if self.operations is not None:
                self.page.operations = Operations(self.operations, tail=self.root.operations.tail)
            root_fonts = _resolve_added_fonts(self.root_added_fonts, fb_objects)
            root_drawn = {name: numbers[place] for name, place in self.drawn.items()}
            if root_drawn or root_fonts or self.root_added_properties:
                _repoint_page(self.page, root_drawn, root_fonts, self.root_added_properties)
            if self.copies:
                _repoint_structure_streams(
                    self.page,
                    self.copies,
                    numbers,
                    total_form_counts,
                    self._tagged_shared_elsewhere,
                )

    def _materialize_fallbacks(self, document: Any) -> dict[bool, object]:  # noqa: ANN401
        fb_objects: dict[bool, object] = {}
        if self.fallback is None:
            return fb_objects
        fb_cache: dict[bytes, int] = {}
        for vertical in (False, True):
            if self._needs_fallback(vertical=vertical):
                fb_objects[vertical] = self._materialize_fallback(document, fb_cache, vertical=vertical)
        return fb_objects

    def _write_copies(
        self,
        document: Any,  # noqa: ANN401
        fb_objects: dict[bool, object],
        total_form_counts: dict[int, int],
    ) -> list[int]:
        edited_counts: dict[int, int] = {}
        for copy in self.copies:
            stream = copy.scope.stream
            if stream is not None and stream.xref is not None:
                edited_counts[stream.xref] = edited_counts.get(stream.xref, 0) + 1
        reused_sp: set[int] = set()
        numbers: list[int] = []
        for copy in self.copies:
            stream = copy.scope.stream
            assert stream is not None  # noqa: S101
            dictionary = {key: value for key, value in stream.items() if key not in _STREAM_KEYS}
            if stream.xref is not None and _is_int(stream.get("StructParents")):
                still_on_page = edited_counts.get(stream.xref, 0) < total_form_counts.get(stream.xref, 0)
                shared_elsewhere = stream.xref in self._tagged_shared_elsewhere
                if not still_on_page and not shared_elsewhere and stream.xref not in reused_sp:
                    reused_sp.add(stream.xref)
                else:
                    # Non-unique copy gets its own fresh /StructParents in _repoint_structure_streams.
                    dictionary.pop("StructParents", None)
            drawn = {name: numbers[place] for name, place in copy.drawn.items()}
            fonts_to_add = _resolve_added_fonts(copy.added_fonts, fb_objects)
            if drawn or fonts_to_add or copy.added_properties:
                dictionary["Resources"] = self._own_resources(copy.scope, drawn, fonts_to_add, copy.added_properties)
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

    def _own_resources(
        self,
        scope: _Scope,
        drawn: dict[str, int],
        added_fonts: dict[str, object],
        added_properties: dict[str, dict[str, object]],
    ) -> dict[str, Any]:
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
        if added_properties:
            props = own.get("Properties")
            own["Properties"] = dict(props.items()) if isinstance(props, Dictionary) else {}
            own["Properties"].update(added_properties)
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
        if (shown_indices and op.operator in _TEXT_POSITION_OPS) or op.operator in ("'", '"'):
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
    _extend_tounicode(to_unicode, font.existing_tounicode or {}, font.extensions)


def _extend_w(
    descendant: Dictionary, extensions: dict[int, tuple[float, tuple[float, float, float] | None, str]]
) -> None:
    kept, ranges = _read_w_ranges(descendant, vertical=False)
    new_widths = {
        gid: width for gid, (width, _, _) in extensions.items() if not any(lo <= gid <= hi for lo, hi in ranges)
    }
    if new_widths:
        descendant["W"] = [*kept, *_w_array(new_widths)]


def _extend_w2(
    descendant: Dictionary, extensions: dict[int, tuple[float, tuple[float, float, float] | None, str]]
) -> None:
    kept, ranges = _read_w_ranges(descendant, vertical=True)
    new_metrics = {
        gid: v_metrics
        for gid, (_, v_metrics, _) in extensions.items()
        if v_metrics is not None and not any(lo <= gid <= hi for lo, hi in ranges)
    }
    if new_metrics:
        if "DW2" not in descendant:
            descendant["DW2"] = [880, -1000]
        descendant["W2"] = [*kept, *_w2_array(new_metrics)]


def _extend_tounicode(
    to_unicode: Stream,
    existing_tounicode: dict[int, str],
    extensions: dict[int, tuple[float, tuple[float, float, float] | None, str]],
) -> None:
    mapped = dict(existing_tounicode)
    changed = False
    for gid, (_, _, text) in extensions.items():
        if mapped.get(gid, _NO_TEXT) != _NO_TEXT or not text or mapped.get(gid) == text:
            continue
        if len(text.encode("utf-16-be", "surrogatepass")) > 2 * _MAX_MAPPED_UNITS:
            continue
        mapped[gid] = text
        changed = True
    if changed:
        to_unicode.write(_cmap(mapped))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _read_w_ranges(descendant: Dictionary, *, vertical: bool) -> tuple[list[object], list[tuple[int, int]]]:
    """Read ``/W`` or ``/W2`` into preserved entries and compact ``(start, end)`` CID ranges."""
    key = "W2" if vertical else "W"
    entries = descendant.get(key)
    if not isinstance(entries, Array):
        return [], []
    kept: list[object] = []
    ranges: list[tuple[int, int]] = []
    items = list(entries)
    i = 0
    while i + 1 < len(items) and _is_int(items[i]):
        first = int(items[i])
        nxt = items[i + 1]
        if isinstance(nxt, Array):
            arr_vals = list(nxt)
            kept.extend((first, arr_vals))
            if vertical:
                _record_w2_array_ranges(ranges, first, arr_vals)
            else:
                _record_w_array_ranges(ranges, first, arr_vals)
            i += 2
        elif not vertical and _is_int(nxt) and i + 2 < len(items) and _is_num(items[i + 2]):
            last = int(nxt)
            if first <= last:
                ranges.append((first, last))
            kept.extend((first, last, items[i + 2]))
            i += 3
        elif vertical and _is_int(nxt) and i + 4 < len(items) and all(_is_num(v) for v in items[i + 2 : i + 5]):
            last = int(nxt)
            if first <= last:
                ranges.append((first, last))
            kept.extend((first, last, items[i + 2], items[i + 3], items[i + 4]))
            i += 5
        else:
            break
    return kept, ranges


def _record_w_array_ranges(ranges: list[tuple[int, int]], first: int, vals: list[Any]) -> None:
    run_start: int | None = None
    for offset, w in enumerate(vals):
        if _is_num(w):
            if run_start is None:
                run_start = first + offset
        elif run_start is not None:
            ranges.append((run_start, first + offset - 1))
            run_start = None
    if run_start is not None:
        ranges.append((run_start, first + len(vals) - 1))


def _record_w2_array_ranges(ranges: list[tuple[int, int]], first: int, vals: list[Any]) -> None:
    run_start: int | None = None
    count = len(vals) // 3
    for offset in range(count):
        if all(_is_num(v) for v in vals[3 * offset : 3 * offset + 3]):
            if run_start is None:
                run_start = first + offset
        elif run_start is not None:
            ranges.append((run_start, first + offset - 1))
            run_start = None
    if run_start is not None:
        ranges.append((run_start, first + count - 1))


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


def _to_inline_operand(value: object, depth: int = 0) -> tuple[bool, Any]:
    """Convert object-model handles into plain values accepted by :class:`Operation`, or return ``(False, None)``."""
    if depth >= _MAX_OPERAND_DEPTH or isinstance(value, Stream):
        return False, None
    if isinstance(value, (Dictionary, Mapping)):
        return _to_inline_mapping(value, depth + 1)
    if isinstance(value, (Array, list, tuple)):
        return _to_inline_sequence(value, depth + 1)
    if value is None or isinstance(value, (bool, int, float, Real, Name, String, str, bytes, bytearray, memoryview)):
        return True, value
    return False, None


def _to_inline_mapping(mapping: Mapping[str, Any] | Dictionary, depth: int) -> tuple[bool, Any]:
    out: dict[str, Any] = {}
    for key, item in mapping.items():
        if not isinstance(key, str) or not key:
            return False, None
        ok, converted = _to_inline_operand(item, depth)
        if not ok:
            return False, None
        out[key] = converted
    return True, out


def _to_inline_sequence(sequence: Sequence[Any] | Array, depth: int) -> tuple[bool, Any]:
    items: list[Any] = []
    for item in sequence:
        ok, converted = _to_inline_operand(item, depth)
        if not ok:
            return False, None
        items.append(converted)
    return True, tuple(items)


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


def _taken_property_names(scope: _Scope) -> set[str]:
    """Return ``/Properties`` resource names defined or referenced by ``BDC`` in ``scope``."""
    sub = _sub(scope.resources, "Properties")
    names = set(sub) if sub is not None else set()
    pending = [scope]
    while pending:
        current = pending.pop()
        names.update(
            str(operation.operands[1])
            for operation in current.operations
            if isinstance(operation, Operation)
            and operation.operator == "BDC"
            and len(operation.operands) >= 2  # noqa: PLR2004
            and isinstance(operation.operands[1], Name)
        )
        pending.extend(form for form in current.forms.values() if form.resources is scope.resources)
    return names


def _count_form_drawings(root: _Scope) -> dict[int, int]:
    """Count total ``Do`` invocations per indirect Form XObject xref on the page."""
    counts: dict[int, int] = {}
    pending = [root]
    while pending:
        current = pending.pop()
        for child in current.forms.values():
            if child.stream is not None and child.stream.xref is not None:
                counts[child.stream.xref] = counts.get(child.stream.xref, 0) + 1
            pending.append(child)
    return counts


def _other_pages_use_xobjects(
    document: Any,  # noqa: ANN401
    current_page: Page,
    target_xrefs: set[int],
    budget: _core._OperationBudget,
) -> set[int]:
    """Return the subset of ``target_xrefs`` referenced by any page other than ``current_page``."""
    found: set[int] = set()
    seen_forms: set[int] = set()
    for other in document.pages:
        budget.poll()
        if other.obj.xref == current_page.obj.xref:
            continue
        _scan_resources_for_xobjects(_resources(other), target_xrefs, seen_forms, found, budget)
        if len(found) == len(target_xrefs) or len(seen_forms) >= _MAX_FORMS:
            break
    return found


def _scan_resources_for_xobjects(
    resources: Dictionary | None,
    target_xrefs: set[int],
    seen_forms: set[int],
    found: set[int],
    budget: _core._OperationBudget,
) -> None:
    stack: list[Dictionary] = [resources] if resources is not None else []
    while stack and len(found) < len(target_xrefs) and len(seen_forms) < _MAX_FORMS:
        xobjects = _sub(stack.pop(), "XObject")
        if xobjects is None:
            continue
        for name in xobjects:
            budget.poll()
            form = xobjects.get(name)
            if not isinstance(form, Stream) or form.get("Subtype") != "Form" or form.xref is None:
                continue
            if form.xref in target_xrefs:
                found.add(form.xref)
            if form.xref in seen_forms:
                continue
            seen_forms.add(form.xref)
            sub_res = form.get("Resources")
            if isinstance(sub_res, Dictionary):
                stack.append(sub_res)
            if len(seen_forms) >= _MAX_FORMS:
                break


def _fresh(name: str, taken: set[str]) -> str:
    number = 1
    while f"{name}_{number}" in taken:
        number += 1
    return f"{name}_{number}"


def _repoint_structure_streams(
    page: Page,
    copies: list[_Copy],
    numbers: list[int],
    total_form_counts: dict[int, int],
    shared_elsewhere: set[int],
) -> None:
    """Update ``/StructParents``, ``/ParentTree``, and ``/MCR /Stm`` when tagged Forms are copied."""
    document = page.document
    struct_root = document.catalog.get("StructTreeRoot")
    parent_tree = struct_root.get("ParentTree") if isinstance(struct_root, Dictionary) else None
    if not isinstance(parent_tree, Dictionary):
        return
    edited_counts: dict[int, int] = {}
    for copy in copies:
        stream = copy.scope.stream
        if stream is not None and stream.xref is not None and _is_int(stream.get("StructParents")):
            edited_counts[stream.xref] = edited_counts.get(stream.xref, 0) + 1

    seen_old: set[int] = set()
    for copy, new_xref in zip(copies, numbers, strict=True):
        stream = copy.scope.stream
        if stream is None or stream.xref is None or not _is_int(stream.get("StructParents")):
            continue
        old_xref = stream.xref
        is_first = old_xref not in seen_old
        seen_old.add(old_xref)
        still_on_page = edited_counts.get(old_xref, 0) < total_form_counts.get(old_xref, 0)
        reused = is_first and not still_on_page and old_xref not in shared_elsewhere
        _repoint_one_tagged_copy(
            page,
            parent_tree,
            old_xref,
            int(stream["StructParents"]),
            new_xref,
            reused=reused,
            repoint_mcr=is_first and not still_on_page,
        )


def _repoint_one_tagged_copy(  # noqa: PLR0913 - structure-repointing parameters.
    page: Page,
    parent_tree: Dictionary,
    old_xref: int,
    sp_key: int,
    new_xref: int,
    *,
    reused: bool,
    repoint_mcr: bool,
) -> None:
    document = page.document
    entry = _core.read_operation(
        document._core(),  # noqa: SLF001
        lambda budget: _number_tree_get(parent_tree, sp_key, budget),
    )
    if entry is None:
        return
    new_stream = document.objects[new_xref]
    if not reused:
        owners: object | None = (
            list(entry) if isinstance(entry, Array) else (entry if isinstance(entry, Dictionary) else None)
        )
        if owners is None:
            return
        new_stream["StructParents"] = _core.parent_tree_append(
            document._core(),  # noqa: SLF001
            _encode(document, owners, 1),
        )
    _update_structure_mcrs(entry, page.obj, old_xref, new_stream, repoint=repoint_mcr)


def _update_structure_mcrs(
    entry: object,
    page_obj: Dictionary,
    old_xref: int,
    new_stream: Stream,
    *,
    repoint: bool,
) -> None:
    """Repoint or append ``/MCR /Stm`` entries on structure elements listed in ``entry``."""
    elem_mcids: dict[int, list[int]] = {}
    if isinstance(entry, Array):
        elems = list(entry)
        for mcid, elem in enumerate(elems):
            if isinstance(elem, Dictionary) and elem.xref is not None:
                elem_mcids.setdefault(elem.xref, []).append(mcid)
    elif isinstance(entry, Dictionary):
        elems = [entry]
    else:
        return
    seen: set[int] = set()
    for elem in elems:
        if not isinstance(elem, Dictionary) or elem.xref is None or elem.xref in seen:
            continue
        seen.add(elem.xref)
        if repoint:
            _repoint_elem_mcr_stm(elem, page_obj, old_xref, new_stream)
        else:
            _append_elem_mcr_stm(elem, page_obj, old_xref, new_stream, elem_mcids.get(elem.xref))


def _number_tree_get(
    root: Dictionary,
    target: int,
    budget: _core._OperationBudget | None = None,
) -> object:
    """Look up integer key ``target`` in a PDF number tree (ISO 32000-2 7.9.7), bounded against cycles and DAGs."""
    stack: list[tuple[Dictionary, int]] = [(root, 0)]
    seen: set[int] = set()
    visited = 0
    while stack:
        node, depth = stack.pop()
        if depth >= 32 or visited >= _MAX_PARENT_TREE_NODES:  # noqa: PLR2004 - bounded tree depth and visits (F6).
            continue
        if node.xref is not None:
            if node.xref in seen:
                continue
            seen.add(node.xref)
        visited += 1
        if budget is not None:
            budget.poll()
        found = _number_tree_nums_get(node.get("Nums"), target, budget)
        if found is not None:
            return found
        _push_number_tree_kids(stack, node.get("Kids"), target, depth + 1, budget)
    return None


def _push_number_tree_kids(
    stack: list[tuple[Dictionary, int]],
    kids: object,
    target: int,
    next_depth: int,
    budget: _core._OperationBudget | None,
) -> None:
    if not isinstance(kids, Array):
        return
    for idx in range(len(kids) - 1, -1, -1):
        if budget is not None:
            _poll(budget, idx)
        kid = kids[idx]
        if isinstance(kid, Dictionary) and _number_tree_in_limits(kid.get("Limits"), target):
            stack.append((kid, next_depth))


def _number_tree_nums_get(
    nums: object,
    target: int,
    budget: _core._OperationBudget | None = None,
) -> object:
    if not isinstance(nums, Array):
        return None
    length = len(nums)
    for i in range(0, length - 1, 2):
        if budget is not None:
            _poll(budget, i)
        key = nums[i]
        if isinstance(key, int) and not isinstance(key, bool) and key == target:
            return nums[i + 1]
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


def _append_elem_mcr_stm(
    elem: Dictionary,
    page_obj: Dictionary,
    old_xref: int,
    new_stream: Stream,
    mcids: list[int] | None,
) -> None:
    """Append ``/MCR`` children pointing to ``new_stream`` for an additional tagged Form copy."""
    kids = elem.get("K")
    existing = list(kids) if isinstance(kids, Array) else ([kids] if kids is not None else [])
    if mcids is None:
        elem_pg = elem.get("Pg")
        mcids = []
        for item in existing:
            if not isinstance(item, Dictionary) or item.get("Type") != Name("MCR"):
                continue
            stm = item.get("Stm")
            pg = item.get("Pg", elem_pg)
            mcid = item.get("MCID")
            if (
                isinstance(stm, Stream)
                and stm.xref == old_xref
                and isinstance(pg, Dictionary)
                and pg.xref == page_obj.xref
                and _is_int(mcid)
            ):
                mcids.append(int(mcid))
    if not mcids:
        return
    for mcid in mcids:
        existing.append({"Type": Name("MCR"), "Pg": page_obj, "Stm": new_stream, "MCID": mcid})
    elem["K"] = existing


def _ensure_owned_subdict(resources: Dictionary, key: str) -> Dictionary:
    sub = resources.get(key)
    if sub is None or sub.is_indirect:
        resources[key] = {} if sub is None else dict(sub.items())
    return resources[key]


def _repoint_page(
    page: Page,
    drawn: dict[str, int],
    added_fonts: dict[str, object],
    added_properties: dict[str, dict[str, object]] | None = None,
) -> None:
    """Attach copied Forms, added fallback Fonts, and added Properties to the page's own Resources."""
    own = page.obj.get("Resources")
    if own is None or own.is_indirect:
        inherited = own if own is not None else _resources(page)
        page.obj["Resources"] = {} if inherited is None else dict(inherited.items())
    resources = page.obj["Resources"]
    document = page.document
    if drawn:
        xobjects = _ensure_owned_subdict(resources, "XObject")
        for name, number in drawn.items():
            xobjects[name] = document.objects[number]
    if added_fonts:
        fonts = _ensure_owned_subdict(resources, "Font")
        for name, font_obj in added_fonts.items():
            fonts[name] = font_obj
    if added_properties:
        props = _ensure_owned_subdict(resources, "Properties")
        for name, prop_dict in added_properties.items():
            props[name] = prop_dict


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
