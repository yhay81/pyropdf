"""Automated AST codemod for migrating PyMuPDF (fitz/pymupdf) and pylopdf 0.15 code to pyropdf.

Uses only the Python 3.11+ standard library (``ast`` and span-based edits) so
original formatting, comments, and untouched statements are preserved. Lives
exclusively under ``tools/``; ``pyropdf`` itself carries zero compatibility
aliases (DESIGN.md section 10.1 "One way").

Examples:
    python3 tools/codemod.py --dry-run --diff path/to/app.py
    python3 tools/codemod.py --from pymupdf src/
    python3 tools/codemod.py --from pylopdf --check src/

"""

from __future__ import annotations

import argparse
import ast
import difflib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SourceKind = Literal["auto", "pymupdf", "pylopdf"]

_PAIR_ARITY = 2
_PYMUPDF_MODULES = frozenset({"fitz", "pymupdf"})
_PYLOPDF_MODULES = frozenset({"pylopdf"})
_NON_PDF_FILETYPES = frozenset({"xps", "epub", "mobi", "fb2", "cbz", "svg", "txt", "html", "xhtml"})
_IMAGE_FILETYPES = frozenset({"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "jp2", "jpx", "pnm", "pam"})

# Mechanical module-level symbol renames when imported from fitz/pymupdf/pylopdf.
_MODULE_SYMBOL_RENAMES: dict[str, str] = {
    "PylopdfWarning": "PyropdfWarning",
    "SigFlag_SignaturesExist": "SignatureFlags.SIGNATURES_EXIST",
    "SigFlag_AppendOnly": "SignatureFlags.APPEND_ONLY",
    "PDF_ANNOT_LE_NONE": "LineEnding.NONE",
    "PDF_ANNOT_LE_SQUARE": "LineEnding.SQUARE",
    "PDF_ANNOT_LE_CIRCLE": "LineEnding.CIRCLE",
    "PDF_ANNOT_LE_DIAMOND": "LineEnding.DIAMOND",
    "PDF_ANNOT_LE_OPEN_ARROW": "LineEnding.OPEN_ARROW",
    "PDF_ANNOT_LE_CLOSED_ARROW": "LineEnding.CLOSED_ARROW",
    "PDF_ANNOT_LE_BUTT": "LineEnding.BUTT",
    "PDF_ANNOT_LE_R_OPEN_ARROW": "LineEnding.R_OPEN_ARROW",
    "PDF_ANNOT_LE_R_CLOSED_ARROW": "LineEnding.R_CLOSED_ARROW",
    "PDF_ANNOT_LE_SLASH": "LineEnding.SLASH",
    "PDF_BM_Normal": "BlendMode.NORMAL",
    "PDF_BM_Multiply": "BlendMode.MULTIPLY",
    "PDF_BM_Screen": "BlendMode.SCREEN",
    "PDF_BM_Overlay": "BlendMode.OVERLAY",
    "PDF_BM_Darken": "BlendMode.DARKEN",
    "PDF_BM_Lighten": "BlendMode.LIGHTEN",
    "PDF_BM_ColorDodge": "BlendMode.COLOR_DODGE",
    "PDF_BM_ColorBurn": "BlendMode.COLOR_BURN",
    "PDF_BM_HardLight": "BlendMode.HARD_LIGHT",
    "PDF_BM_SoftLight": "BlendMode.SOFT_LIGHT",
    "PDF_BM_Difference": "BlendMode.DIFFERENCE",
    "PDF_BM_Exclusion": "BlendMode.EXCLUSION",
    "PDF_BM_Hue": "BlendMode.HUE",
    "PDF_BM_Saturation": "BlendMode.SATURATION",
    "PDF_BM_Color": "BlendMode.COLOR",
    "PDF_BM_Luminosity": "BlendMode.LUMINOSITY",
    "PDF_BORDER_STYLE_SOLID": "BorderStyle.SOLID",
    "PDF_BORDER_STYLE_DASHED": "BorderStyle.DASHED",
    "PDF_BORDER_STYLE_BEVELED": "BorderStyle.BEVELED",
    "PDF_BORDER_STYLE_INSET": "BorderStyle.INSET",
    "PDF_BORDER_STYLE_UNDERLINE": "BorderStyle.UNDERLINE",
    "csRGB": "Colorspace.RGB",
    "csGRAY": "Colorspace.GRAY",
}

# Module-level attributes that cannot be mechanically translated.
_UNSUPPORTED_MODULE_SYMBOLS: dict[str, tuple[str, str]] = {
    "Story": (
        "unsupported-story",
        "PyMuPDF Story/HTML layout is out of scope (#355); render HTML externally or draw with page.draw().",
    ),
    "csCMYK": (
        "unsupported-cmyk-pixmap",
        "CMYK raster pixmaps are not yet supported (#425); use Colorspace.RGB or Colorspace.GRAY.",
    ),
    "TEXT_COLLECT_STRUCTURE": (
        "manual-text-flag",
        "Use doc.markdown(use_structure='auto') or doc.structure instead of PyMuPDF TEXT_* bitflags.",
    ),
    "TEXT_IGNORE_ACTUALTEXT": (
        "manual-text-flag",
        "Pass actual_text=False to page.text()/words()/chars()/layout()/search() instead of TEXT_IGNORE_ACTUALTEXT.",
    ),
}

# Simple zero-argument method -> property renames.
_ZERO_ARG_METHOD_TO_PROPERTY: dict[str, str] = {
    "get_xml_metadata": "xmp",
    "get_page_labels": "page_labels",
    "get_label": "label",
    "get_links": "links",
    "links": "links",
    "annots": "annotations",
    "widgets": "widgets",
    "get_sigflags": "form.signature_flags",
}

# Direct method -> method renames that preserve all arguments verbatim.
_DIRECT_METHOD_RENAMES: dict[str, str] = {
    "scrub": "sanitize",
    "search_for": "search",
    "get_text_selection": "selection",
    "find_tables": "tables",
}

# Simple one-argument setter method -> property assignment renames (statement context only).
_ONE_ARG_SETTER_TO_ATTR: dict[str, tuple[str, str | None]] = {
    "set_xml_metadata": ("xmp", None),
    "set_language": ("language", None),
    "set_rotation": ("rotation", None),
    "set_mediabox": ("mediabox", None),
    "set_cropbox": ("cropbox", None),
    "set_bleedbox": ("bleedbox", None),
    "set_trimbox": ("trimbox", None),
    "set_artbox": ("artbox", None),
    "set_metadata": (
        "metadata",
        "Check metadata keys: pyropdf uses 'created' and 'modified' instead of 'creationDate' and 'modDate'.",
    ),
}

# Simple one-argument markup annotation creators.
_MARKUP_ANNOT_METHODS: dict[str, str] = {
    "add_highlight_annot": "Highlight",
    "add_underline_annot": "Underline",
    "add_strikeout_annot": "StrikeOut",
    "add_squiggly_annot": "Squiggly",
}

# In-place geometry methods that require explicit reassignment because pyropdf geometry is immutable.
_IMMUTABLE_GEOMETRY_METHODS: dict[str, tuple[str, str]] = {
    "include_point": ("immutable-geometry", "Rect is immutable in pyropdf; replace r.include_point(p) with r = r | p."),
    "include_rect": ("immutable-geometry", "Rect is immutable in pyropdf; replace r.include_rect(o) with r = r | o."),
    "intersect": ("immutable-geometry", "Rect is immutable in pyropdf; replace r.intersect(o) with r = r & o."),
    "normalize": (
        "immutable-geometry",
        "Rect is immutable in pyropdf; replace r.normalize() with r = r.normalized().",
    ),
    "invert": ("immutable-geometry", "Matrix is immutable in pyropdf; replace m.invert() with m = ~m."),
    "prerotate": (
        "immutable-geometry",
        "Matrix is immutable in pyropdf; replace m.prerotate(deg) with m = Matrix(deg) * m.",
    ),
    "prescale": (
        "immutable-geometry",
        "Matrix is immutable in pyropdf; replace m.prescale(sx, sy) with m = Matrix(sx, sy) * m.",
    ),
    "pretranslate": (
        "immutable-geometry",
        "Matrix is immutable in pyropdf; replace m.pretranslate(tx, ty) with m = Matrix(1, 0, 0, 1, tx, ty) * m.",
    ),
}

# Method calls that require manual migration with targeted advice.
_MANUAL_METHODS: dict[str, tuple[str, str]] = {
    "add_redact_annot": (
        "manual-redaction",
        "Use page.annotations.add(pyropdf.RedactionMark(rect)); permanent content removal awaits #362.",
    ),
    "apply_redactions": (
        "manual-redaction",
        "Permanent content removal under redaction marks is tracked in #362; use doc.sanitize() for hidden data.",
    ),
    "insert_htmlbox": (
        "unsupported-htmlbox",
        "HTML layout (insert_htmlbox) is out of scope for pyropdf (#355); draw styled content via page.draw().",
    ),
    "clip_to_rect": (
        "manual-clip-to-rect",
        "Permanent content removal outside a rectangle awaits #420; setting page.cropbox only hides outside content.",
    ),
    "insert_text": (
        "manual-canvas",
        "Use 'with page.draw() as canvas: canvas.text(text, at, ...)' in display coordinates (top-left origin).",
    ),
    "insert_textbox": (
        "manual-canvas",
        "Use 'with page.draw() as canvas: canvas.textbox(rect, text, ...)' in display coordinates.",
    ),
    "insert_image": (
        "manual-canvas",
        "Use 'with page.draw() as canvas: canvas.image(rect, source)' in display coordinates.",
    ),
    "show_pdf_page": (
        "manual-canvas",
        "Use 'with page.draw() as canvas: canvas.page(rect, source_page)' in display coordinates.",
    ),
    "new_shape": (
        "manual-canvas",
        "Use 'with page.draw() as canvas:' context manager instead of Shape/new_shape() (commits atomically on exit).",
    ),
    "draw_rect": (
        "manual-canvas",
        "Use 'with page.draw() as canvas: canvas.rect(rect, fill=..., stroke=...)' in display coordinates.",
    ),
    "draw_line": (
        "manual-canvas",
        "Use 'with page.draw() as canvas: canvas.line(start, end, stroke=...)' in display coordinates.",
    ),
    "draw_circle": (
        "manual-canvas",
        "Use 'with page.draw() as canvas: canvas.circle(center, radius, ...)' in display coordinates.",
    ),
    "insert_pdf": (
        "manual-page-ops",
        "Use doc.pages.extend(other.pages[start:stop]) or doc.pages.insert(index, page) instead of insert_pdf().",
    ),
    "select": (
        "manual-page-ops",
        "Reorder or filter pages via doc.pages (0-based sequence supporting del, move, insert, and extend).",
    ),
    "get_toc": (
        "manual-outline",
        "Use doc.outline; pyropdf page indexes are 0-based (PyMuPDF get_toc() uses 1-based page numbers).",
    ),
    "set_toc": (
        "manual-outline",
        "Assign doc.outline with OutlineEntry records using 0-based page indexes (PyMuPDF set_toc() is 1-based).",
    ),
    "set_page_labels": (
        "manual-page-labels",
        "Assign doc.page_labels = [pyropdf.PageLabelRange(start=..., ...)] using 0-based start indexes.",
    ),
    "get_layers": (
        "manual-layers",
        "Use doc.layers to inspect and configure optional-content layers (OCMD /VE rendering awaits #373).",
    ),
    "set_layer": (
        "manual-layers",
        "Configure layer visibility via doc.layers (OCMD /VE rendering awaits #373).",
    ),
    "insert_link": (
        "manual-links",
        "Use page.links.add(rect, target) with URI string, Destination, RemoteDestination, Launch, or NamedAction.",
    ),
    "update_link": (
        "manual-links",
        "Mutate link.rect and link.target directly on a Link handle from page.links (no update() call needed).",
    ),
    "add_widget": (
        "manual-forms",
        "Use page.widgets.add(TextField(...)) / CheckBox(...) / ComboBox(...) specifications instead of add_widget().",
    ),
    "journal_save": (
        "manual-history",
        "Use doc.history.save() which returns bytes (write the bytes to a file yourself; format is pyropdf's own).",
    ),
    "journal_load": (
        "manual-history",
        "Use doc.history.restore(data) with bytes produced by doc.history.save().",
    ),
    "get_form_fields": (
        "manual-forms",
        "Use doc.form (a live mapping of Field handles; read field.value or assign doc.form[name].value = ...).",
    ),
    "convert_to_pdf": (
        "manual-image-to-pdf",
        "Create a pyropdf.Document() and call doc.pages.new_from_image(source) for images; non-PDF is unsupported.",
    ),
}

_SAVE_KWARG_RENAMES: dict[str, str] = {
    "deflate": "compress",
    "linear": "linearize",
    "use_objstms": "object_streams",
}

_SAVE_MANUAL_KWARGS: dict[str, tuple[str, str]] = {
    "garbage": (
        "manual-save-option",
        "Replace garbage=1..4 with explicit keyword options: prune=True, renumber=True, dedupe=True.",
    ),
    "clean": (
        "manual-save-option",
        "Call page.clean_contents() before saving instead of save(clean=True) (pre-save flag tracked in #1191).",
    ),
    "encryption": (
        "manual-encryption",
        "Pass encryption=pyropdf.Encryption(user_password=..., owner_password=..., permissions=...) to save().",
    ),
    "owner_pw": (
        "manual-encryption",
        "Pass encryption=pyropdf.Encryption(owner_password=...) instead of owner_pw=.",
    ),
    "user_pw": (
        "manual-encryption",
        "Pass encryption=pyropdf.Encryption(user_password=...) instead of user_pw=.",
    ),
    "pretty": (
        "manual-save-option",
        "ASCII/pretty save serialization is tracked in #1191.",
    ),
    "ascii": (
        "manual-save-option",
        "ASCII/pretty save serialization is tracked in #1191.",
    ),
}

_GET_TEXT_MODES: dict[str, tuple[str, tuple[str, str] | None]] = {
    "text": ("{recv}.text({kw})", None),
    "words": (
        "{recv}.words({kw})",
        (
            "behavior-words-record",
            "page.words() returns Word records (.bbox, .text, .block, .line, .word) instead of 8-tuples.",
        ),
    ),
    "blocks": (
        "{recv}.layout({kw}).blocks",
        ("behavior-blocks-record", "page.layout().blocks returns Block records rather than PyMuPDF tuples."),
    ),
    "html": ("{recv}.layout({kw}).to_html()", None),
    "xml": ("{recv}.layout({kw}).to_xml()", None),
    "xhtml": ("{recv}.layout({kw}).to_xml()", None),
    "dict": (
        "{recv}.layout({kw}).to_dict()",
        (
            "manual-get-text-dict",
            "get_text('dict') schema differs from PyMuPDF; inspect page.layout(chars=True).to_dict() or page.chars().",
        ),
    ),
    "rawdict": (
        "{recv}.layout({kw}).to_dict()",
        (
            "manual-get-text-dict",
            "get_text('rawdict') schema differs from PyMuPDF; inspect page.layout(chars=True).to_dict() or chars().",
        ),
    ),
    "json": (
        "{recv}.layout({kw}).to_dict()",
        (
            "manual-get-text-dict",
            "get_text('json') schema differs from PyMuPDF; serialize page.layout().to_dict() or use doc.to_json().",
        ),
    ),
    "rawjson": (
        "{recv}.layout({kw}).to_dict()",
        (
            "manual-get-text-dict",
            "get_text('rawjson') schema differs from PyMuPDF; serialize page.layout(chars=True).to_dict().",
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A manual-review warning emitted for a construct that cannot be rewritten purely mechanically."""

    line: int
    col: int
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class TransformResult:
    """Result of transforming one Python source string."""

    source: str
    changed: bool
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True, slots=True)
class _Edit:
    start: int
    end: int
    replacement: str


class _SpanIndex:
    """Maps AST ``(lineno, col_offset)`` UTF-8 byte coordinates to Python ``str`` character offsets."""

    def __init__(self, source: str) -> None:
        self._source = source
        self._lines = source.splitlines(keepends=True)
        self._line_starts: list[int] = [0]
        total = 0
        for line in self._lines:
            total += len(line)
            self._line_starts.append(total)

    def offset(self, lineno: int, col_bytes: int) -> int:
        """Convert 1-based line number and UTF-8 byte column offset to a character index."""
        line_idx = lineno - 1
        if line_idx >= len(self._lines):
            return len(self._source)
        prefix = self._lines[line_idx].encode("utf-8")[:col_bytes].decode("utf-8")
        return self._line_starts[line_idx] + len(prefix)

    def span(self, node: ast.AST) -> tuple[int, int]:
        """Return ``(start, end)`` character offsets for an AST node with location metadata."""
        lineno = getattr(node, "lineno", None)
        col = getattr(node, "col_offset", None)
        end_lineno = getattr(node, "end_lineno", None)
        end_col = getattr(node, "end_col_offset", None)
        if lineno is None or col is None or end_lineno is None or end_col is None:
            msg = f"AST node {type(node).__name__} lacks location metadata"
            raise ValueError(msg)
        return self.offset(lineno, col), self.offset(end_lineno, end_col)


def _apply_edits(text: str, edits: list[_Edit], base_offset: int = 0) -> str:
    """Apply non-overlapping edits whose offsets are relative to ``text`` starting at ``base_offset``."""
    out = text
    for edit in sorted(edits, key=lambda item: (item.start, item.end), reverse=True):
        rel_start = edit.start - base_offset
        rel_end = edit.end - base_offset
        out = out[:rel_start] + edit.replacement + out[rel_end:]
    return out


def _target_modules(mode: SourceKind) -> frozenset[str]:
    if mode == "pymupdf":
        return _PYMUPDF_MODULES
    if mode == "pylopdf":
        return _PYLOPDF_MODULES
    return _PYMUPDF_MODULES | _PYLOPDF_MODULES


def _is_doc_open_expr(node: ast.AST, mod_aliases: set[str], direct_open_names: set[str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in mod_aliases:
        return func.attr in {"open", "Document"}
    return isinstance(func, ast.Name) and func.id in direct_open_names


def _collect_import_bindings(tree: ast.Module, target_mods: frozenset[str]) -> tuple[dict[str, str], set[str]]:
    alias_map: dict[str, str] = {}
    direct_open: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in target_mods:
                    local = alias.asname or alias.name
                    new_local = "pyropdf" if alias.asname in {None, "fitz", "pymupdf", "pylopdf"} else alias.asname
                    alias_map[local] = new_local
        elif isinstance(node, ast.ImportFrom) and node.module in target_mods:
            for alias in node.names:
                if alias.name in {"open", "Document"}:
                    direct_open.add(alias.asname or alias.name)
    return alias_map, direct_open


def _collect_doc_vars(tree: ast.Module, mod_aliases: set[str], direct_open: set[str]) -> set[str]:
    doc_vars: set[str] = set()
    non_doc_vars: set[str] = set()

    def record(target: ast.AST | None, expr: ast.AST | None) -> None:
        if not isinstance(target, ast.Name) or expr is None:
            return
        if _is_doc_open_expr(expr, mod_aliases, direct_open):
            doc_vars.add(target.id)
        else:
            non_doc_vars.add(target.id)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                record(target, node.value)
        elif isinstance(node, ast.AnnAssign):
            record(node.target, node.value)
        elif isinstance(node, ast.With):
            for item in node.items:
                record(item.optional_vars, item.context_expr)
    return doc_vars - non_doc_vars


class _Rewriter:
    """Span-preserving AST rewriter that composes nested edits cleanly."""

    def __init__(self, source: str, mode: SourceKind) -> None:
        self._source = source
        self._index = _SpanIndex(source)
        self._target_mods = _target_modules(mode)
        self._tree = ast.parse(source)
        self._alias_map, self._direct_open = _collect_import_bindings(self._tree, self._target_mods)
        self._mod_aliases = set(self._alias_map)
        self._doc_vars = _collect_doc_vars(self._tree, self._mod_aliases, self._direct_open)
        self._diagnostics: list[Diagnostic] = []
        self._pyropdf_ref = next(iter(self._alias_map.values()), "pyropdf")

    def _warn(self, node: ast.AST, code: str, message: str) -> None:
        lineno = getattr(node, "lineno", 1)
        col = getattr(node, "col_offset", 0)
        self._diagnostics.append(Diagnostic(line=lineno, col=col, code=code, message=message))

    def _raw(self, node: ast.AST) -> str:
        start, end = self._index.span(node)
        return self._source[start:end]

    def _render(self, node: ast.AST) -> str:
        start, end = self._index.span(node)
        edits = self._collect_edits(node)
        return _apply_edits(self._source[start:end], edits, base_offset=start)

    def _replace(self, node: ast.AST, replacement: str) -> list[_Edit]:
        start, end = self._index.span(node)
        return [_Edit(start=start, end=end, replacement=replacement)]

    def run(self) -> TransformResult:
        """Transform the parsed module and return updated source plus sorted diagnostics."""
        if not self._has_relevant_imports():
            return TransformResult(source=self._source, changed=False, diagnostics=())
        edits = self._collect_edits(self._tree)
        new_source = _apply_edits(self._source, edits)
        deduped = sorted(set(self._diagnostics), key=lambda diag: (diag.line, diag.col, diag.code, diag.message))
        return TransformResult(
            source=new_source,
            changed=new_source != self._source,
            diagnostics=tuple(deduped),
        )

    def _has_relevant_imports(self) -> bool:
        if self._mod_aliases or self._direct_open:
            return True
        for node in ast.walk(self._tree):
            if isinstance(node, ast.Import) and any(alias.name == "pymupdf4llm" for alias in node.names):
                return True
            if isinstance(node, ast.ImportFrom) and node.module in self._target_mods | {"pymupdf4llm"}:
                return True
        return False

    def _collect_edits(self, node: ast.AST) -> list[_Edit]:
        match node:
            case ast.Import():
                edits = self._visit_import(node)
            case ast.ImportFrom():
                edits = self._visit_import_from(node)
            case ast.Expr():
                edits = self._visit_expr(node)
            case ast.For():
                edits = self._visit_for(node)
            case ast.Subscript():
                edits = self._visit_subscript(node)
            case ast.Attribute():
                edits = self._visit_attribute(node)
            case ast.Call():
                edits = self._visit_call(node)
            case _:
                edits = self._visit_children(node)
        return edits

    def _visit_children(self, node: ast.AST) -> list[_Edit]:
        edits: list[_Edit] = []
        for child in ast.iter_child_nodes(node):
            edits.extend(self._collect_edits(child))
        return edits

    def _visit_import(self, node: ast.Import) -> list[_Edit]:
        changed = False
        rendered_names: list[str] = []
        for alias in node.names:
            if alias.name == "pymupdf4llm":
                self._warn(
                    node,
                    "manual-pymupdf4llm",
                    "pyropdf provides doc.markdown(), doc.to_chunks(), and doc.to_json() natively; remove pymupdf4llm.",
                )
                rendered_names.append(f"{alias.name} as {alias.asname}" if alias.asname else alias.name)
            elif alias.name in self._target_mods:
                changed = True
                if alias.asname in {None, "fitz", "pymupdf", "pylopdf"}:
                    rendered_names.append("pyropdf")
                else:
                    rendered_names.append(f"pyropdf as {alias.asname}")
            else:
                rendered_names.append(f"{alias.name} as {alias.asname}" if alias.asname else alias.name)
        return self._replace(node, f"import {', '.join(rendered_names)}") if changed else []

    def _visit_import_from(self, node: ast.ImportFrom) -> list[_Edit]:
        if node.module == "pymupdf4llm":
            self._warn(
                node,
                "manual-pymupdf4llm",
                "pyropdf provides doc.markdown(), doc.to_chunks(), and doc.to_json() natively; remove pymupdf4llm.",
            )
            return []
        if node.module not in self._target_mods:
            return []
        parts = [self._map_imported_alias(node, alias) for alias in node.names]
        deduped = list(dict.fromkeys(parts))
        return self._replace(node, f"from pyropdf import {', '.join(deduped)}")

    def _map_imported_alias(self, node: ast.ImportFrom, alias: ast.alias) -> str:
        if alias.name in _UNSUPPORTED_MODULE_SYMBOLS:
            code, message = _UNSUPPORTED_MODULE_SYMBOLS[alias.name]
            self._warn(node, code, message)
            return f"{alias.name} as {alias.asname}" if alias.asname else alias.name
        if alias.name in _MODULE_SYMBOL_RENAMES:
            target = _MODULE_SYMBOL_RENAMES[alias.name]
            top = target.split(".")[0]
            if "." in target:
                self._warn(
                    node,
                    "manual-enum-import",
                    f"Imported constant {alias.name} maps to {target}; update references to use {top}.",
                )
                return top
            return f"{target} as {alias.asname}" if alias.asname else target
        if alias.name == "peek_metadata":
            return f"probe as {alias.asname}" if alias.asname else "probe"
        return f"{alias.name} as {alias.asname}" if alias.asname else alias.name

    def _visit_for(self, node: ast.For) -> list[_Edit]:
        edits: list[_Edit] = []
        if isinstance(node.iter, ast.Name) and node.iter.id in self._doc_vars:
            edits.extend(self._replace(node.iter, f"{node.iter.id}.pages"))
        else:
            edits.extend(self._collect_edits(node.iter))
        edits.extend(self._collect_edits(node.target))
        for stmt in (*node.body, *node.orelse):
            edits.extend(self._collect_edits(stmt))
        return edits

    def _visit_subscript(self, node: ast.Subscript) -> list[_Edit]:
        if isinstance(node.value, ast.Name) and node.value.id in self._doc_vars:
            return self._replace(node, f"{node.value.id}.pages[{self._render(node.slice)}]")
        return self._visit_children(node)

    def _visit_attribute(self, node: ast.Attribute) -> list[_Edit]:
        if isinstance(node.value, ast.Name) and node.value.id in self._mod_aliases:
            return self._rewrite_module_attr(node, self._alias_map[node.value.id])

        simple_attr = {
            "page_count": lambda recv: f"len({recv}.pages)",
            "needs_pass": lambda recv: f"{recv}.needs_password",
            "is_fast_webaccess": lambda recv: f"{recv}.is_linearized",
            "number": lambda recv: f"{recv}.index",
        }.get(node.attr)
        if simple_attr is not None:
            return self._replace(node, simple_attr(self._render(node.value)))
        if node.attr == "name" and isinstance(node.value, ast.Name) and node.value.id in self._doc_vars:
            return self._replace(node, f"{node.value.id}.path")
        return self._visit_children(node)

    def _rewrite_module_attr(self, node: ast.Attribute, mod_name: str) -> list[_Edit]:
        if node.attr in _UNSUPPORTED_MODULE_SYMBOLS:
            code, message = _UNSUPPORTED_MODULE_SYMBOLS[node.attr]
            self._warn(node, code, message)
            return self._replace(node, f"{mod_name}.{node.attr}")
        target = _MODULE_SYMBOL_RENAMES.get(node.attr, node.attr)
        return self._replace(node, f"{mod_name}.{target}")

    def _visit_expr(self, stmt: ast.Expr) -> list[_Edit]:
        if isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Attribute):
            rewritten = self._rewrite_stmt_call(stmt, stmt.value, stmt.value.func)
            if rewritten is not None:
                return rewritten
        return self._visit_children(stmt)

    def _rewrite_stmt_call(self, stmt: ast.Expr, call: ast.Call, func: ast.Attribute) -> list[_Edit] | None:
        attr = func.attr
        recv_node = func.value
        if attr in _ONE_ARG_SETTER_TO_ATTR and len(call.args) == 1 and not call.keywords:
            prop, note = _ONE_ARG_SETTER_TO_ATTR[attr]
            if note:
                self._warn(call, "manual-metadata-keys", note)
            return self._replace(stmt, f"{self._render(recv_node)}.{prop} = {self._render(call.args[0])}")
        if attr == "del_xml_metadata" and not call.args and not call.keywords:
            return self._replace(stmt, f"del {self._render(recv_node)}.xmp")
        if attr == "delete_page" and len(call.args) == 1 and not call.keywords:
            return self._replace(stmt, f"del {self._render(recv_node)}.pages[{self._render(call.args[0])}]")
        if attr == "embfile_del" and len(call.args) == 1 and not call.keywords:
            return self._replace(stmt, f"del {self._render(recv_node)}.attachments[{self._render(call.args[0])}]")
        if attr == "set_form_field" and len(call.args) == _PAIR_ARITY:
            if call.keywords:
                self._warn(
                    call,
                    "manual-form-font",
                    "Assign doc.form[name].value = value and configure doc.form.font / widget.font separately.",
                )
            recv = self._render(recv_node)
            return self._replace(
                stmt, f"{recv}.form[{self._render(call.args[0])}].value = {self._render(call.args[1])}"
            )
        return None

    def _visit_call(self, node: ast.Call) -> list[_Edit]:
        if isinstance(node.func, ast.Name):
            if node.func.id == "len" and len(node.args) == 1 and not node.keywords:
                arg = node.args[0]
                if isinstance(arg, ast.Name) and arg.id in self._doc_vars:
                    return self._replace(node, f"len({arg.id}.pages)")
            if node.func.id in self._direct_open:
                return self._rewrite_open_call(node, prefix="")
        elif isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id in self._mod_aliases:
                return self._rewrite_module_call(node, self._alias_map[node.func.value.id], node.func.attr)
            return self._rewrite_method_call(node, node.func)
        return self._visit_children(node)

    def _rewrite_module_call(self, node: ast.Call, mod_name: str, attr: str) -> list[_Edit]:
        if attr in {"open", "Document"}:
            return self._rewrite_open_call(node, prefix=f"{mod_name}.")
        if attr == "peek_metadata":
            return self._replace(node, f"{mod_name}.probe({self._format_call_args(node)})")
        if attr == "Font":
            return self._rewrite_font_call(node, mod_name)
        if attr in _UNSUPPORTED_MODULE_SYMBOLS:
            code, message = _UNSUPPORTED_MODULE_SYMBOLS[attr]
            self._warn(node, code, message)
            return self._replace(node, f"{mod_name}.{attr}({self._format_call_args(node)})")
        return self._visit_children(node)

    def _rewrite_font_call(self, node: ast.Call, mod_name: str) -> list[_Edit]:
        kw = {k.arg: k.value for k in node.keywords if k.arg is not None}
        if not node.args and set(kw) == {"fontname"}:
            return self._replace(node, f"{mod_name}.Font.standard({self._render(kw['fontname'])})")
        if len(node.args) == 1 and not node.keywords:
            return self._replace(node, f"{mod_name}.Font.standard({self._render(node.args[0])})")
        for key, factory in (("fontfile", "from_file"), ("fontbuffer", "from_bytes")):
            if not node.args and key in kw:
                extra = [f"{k}={self._render(v)}" for k, v in kw.items() if k != key]
                rest = f", {', '.join(extra)}" if extra else ""
                return self._replace(node, f"{mod_name}.Font.{factory}({self._render(kw[key])}{rest})")
        self._warn(
            node,
            "manual-font-factory",
            "Use Font.standard(name), Font.from_file(path), or Font.from_bytes(data) in pyropdf.",
        )
        return self._visit_children(node)

    def _rewrite_open_call(self, node: ast.Call, prefix: str) -> list[_Edit]:
        if not node.args and not node.keywords:
            return self._replace(node, f"{prefix}Document()")

        kw = {k.arg: k.value for k in node.keywords if k.arg is not None}
        filetype_node = kw.get("filetype")
        if filetype_node is None and len(node.args) == _PAIR_ARITY and isinstance(node.args[0], ast.Constant):
            filetype_node = node.args[0]
        self._check_open_filetype(node, filetype_node)

        if "stream" in kw and not node.args:
            other_kw = [f"{k}={self._render(v)}" for k, v in kw.items() if k not in {"stream", "filetype"}]
            rest = f", {', '.join(other_kw)}" if other_kw else ""
            return self._replace(node, f"{prefix}open({self._render(kw['stream'])}{rest})")

        if (
            len(node.args) == _PAIR_ARITY
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.lower().lstrip(".") == "pdf"
        ):
            other_kw = [f"{k}={self._render(v)}" for k, v in kw.items() if k != "filetype"]
            rest = f", {', '.join(other_kw)}" if other_kw else ""
            return self._replace(node, f"{prefix}open({self._render(node.args[1])}{rest})")

        other_args = [self._render(arg) for arg in node.args]
        other_kw = [f"{k}={self._render(v)}" for k, v in kw.items() if k != "filetype"]
        return self._replace(node, f"{prefix}open({', '.join([*other_args, *other_kw])})")

    def _check_open_filetype(self, node: ast.Call, filetype_node: ast.AST | None) -> None:
        if not (isinstance(filetype_node, ast.Constant) and isinstance(filetype_node.value, str)):
            return
        ft = filetype_node.value.lower().lstrip(".")
        if ft in _NON_PDF_FILETYPES:
            self._warn(
                node,
                "unsupported-filetype",
                f"pyropdf opens PDF files only; non-PDF format {filetype_node.value!r} is out of scope (#355).",
            )
        elif ft in _IMAGE_FILETYPES:
            self._warn(
                node,
                "manual-image-to-pdf",
                "To build a PDF from an image, use doc = pyropdf.Document(); doc.pages.new_from_image(source).",
            )

    def _rewrite_method_call(self, node: ast.Call, func: ast.Attribute) -> list[_Edit]:
        attr = func.attr
        recv = self._render(func.value)
        if attr in _MANUAL_METHODS:
            code, message = _MANUAL_METHODS[attr]
            self._warn(node, code, message)
            return self._visit_children(node)
        if attr in _IMMUTABLE_GEOMETRY_METHODS:
            code, message = _IMMUTABLE_GEOMETRY_METHODS[attr]
            self._warn(node, code, message)
            return self._visit_children(node)
        if attr in _ZERO_ARG_METHOD_TO_PROPERTY and not node.args and not node.keywords:
            return self._replace(node, f"{recv}.{_ZERO_ARG_METHOD_TO_PROPERTY[attr]}")
        if attr in _DIRECT_METHOD_RENAMES:
            return self._replace(node, f"{recv}.{_DIRECT_METHOD_RENAMES[attr]}({self._format_call_args(node)})")
        special = {
            "save": lambda: self._rewrite_save_call(node, recv, attr),
            "tobytes": lambda: self._rewrite_save_call(node, recv, attr),
            "to_bytes": lambda: self._rewrite_save_call(node, recv, attr),
            "get_text": lambda: self._rewrite_get_text(node, recv),
            "get_pixmap": lambda: self._rewrite_get_pixmap(node, recv),
            "get_svg_image": lambda: self._rewrite_get_svg(node, recv),
            "bake": lambda: self._rewrite_bake(node, recv),
            "new_page": lambda: self._rewrite_new_page(node, recv),
        }.get(attr)
        if special is not None:
            return special()
        return self._rewrite_page_or_storage_method(node, recv, attr)

    def _rewrite_page_or_storage_method(self, node: ast.Call, recv: str, attr: str) -> list[_Edit]:
        rewritten = self._rewrite_page_helper(node, recv, attr)
        if rewritten is not None:
            return rewritten
        rewritten = self._rewrite_storage_helper(node, recv, attr)
        if rewritten is not None:
            return rewritten
        return self._visit_children(node)

    def _rewrite_page_helper(self, node: ast.Call, recv: str, attr: str) -> list[_Edit] | None:
        if attr == "load_page" and len(node.args) == 1 and not node.keywords:
            return self._replace(node, f"{recv}.pages[{self._render(node.args[0])}]")
        if attr == "authenticate":
            self._warn(
                node,
                "behavior-unlock",
                "doc.unlock(password) returns Access and raises PasswordError on failure (authenticate() returned 0).",
            )
            return self._replace(node, f"{recv}.unlock({self._format_call_args(node)})")
        if attr == "saveIncr" and not node.args and not node.keywords:
            return self._replace(node, f"{recv}.save(incremental=True)")
        if attr == "get_fonts":
            return self._replace(node, f"{recv}.fonts()")
        if attr in {"get_images", "get_image_info"}:
            self._warn(
                node,
                "behavior-images",
                "page.images() returns PageImage records in display space rather than PyMuPDF tuples/dicts.",
            )
            return self._replace(node, f"{recv}.images()")
        return self._rewrite_annot_and_move_helper(node, recv, attr)

    def _rewrite_annot_and_move_helper(self, node: ast.Call, recv: str, attr: str) -> list[_Edit] | None:
        if attr == "get_drawings":
            self._warn(
                node,
                "behavior-drawings",
                "page.content().paths returns immutable Path values in display coordinates rather than PyMuPDF dicts.",
            )
            return self._replace(node, f"{recv}.content().paths")
        if attr == "move_page" and len(node.args) == _PAIR_ARITY and not node.keywords:
            first, second = self._render(node.args[0]), self._render(node.args[1])
            return self._replace(node, f"{recv}.pages.move({first}, {second})")
        if attr in _MARKUP_ANNOT_METHODS and len(node.args) == 1 and not node.keywords:
            spec = _MARKUP_ANNOT_METHODS[attr]
            return self._replace(
                node, f"{recv}.annotations.add({self._pyropdf_ref}.{spec}({self._render(node.args[0])}))"
            )
        if attr == "add_text_annot" and len(node.args) == _PAIR_ARITY and not node.keywords:
            point, text = self._render(node.args[0]), self._render(node.args[1])
            return self._replace(node, f"{recv}.annotations.add({self._pyropdf_ref}.Note({point}, contents={text}))")
        return None

    def _rewrite_storage_helper(self, node: ast.Call, recv: str, attr: str) -> list[_Edit] | None:
        zero_arg = {
            "embfile_names": f"list({recv}.attachments)",
            "embfile_count": f"len({recv}.attachments)",
            "journal_undo": f"{recv}.history.undo()",
            "journal_redo": f"{recv}.history.redo()",
        }.get(attr)
        if zero_arg is not None and not node.args and not node.keywords:
            return self._replace(node, zero_arg)
        if attr == "embfile_get" and len(node.args) == 1 and not node.keywords:
            return self._replace(node, f"{recv}.attachments[{self._render(node.args[0])}].data")
        if attr == "embfile_add" and len(node.args) >= _PAIR_ARITY:
            if len(node.args) > _PAIR_ARITY or node.keywords:
                self._warn(
                    node,
                    "manual-attachment-options",
                    "Review extra embfile_add() arguments; pyropdf uses attachments.add(name, data, *, description=).",
                )
            return self._replace(
                node, f"{recv}.attachments.add({self._render(node.args[0])}, {self._render(node.args[1])})"
            )
        if attr == "xref_stream" and len(node.args) == 1 and not node.keywords:
            return self._replace(node, f"{recv}.objects[{self._render(node.args[0])}].read()")
        if attr == "xref_stream_raw" and len(node.args) == 1 and not node.keywords:
            return self._replace(node, f"{recv}.objects[{self._render(node.args[0])}].read(decode=False)")
        return None

    def _rewrite_new_page(self, node: ast.Call, recv: str) -> list[_Edit]:
        kw = {k.arg: k.value for k in node.keywords if k.arg is not None}
        if not node.args and not kw:
            return self._replace(node, f"{recv}.pages.new()")
        if "pno" in kw or node.args:
            self._warn(
                node,
                "manual-new-page",
                "doc.pages.new((width, height)) appends a page; use doc.pages.move() if you need a specific index.",
            )
            return self._visit_children(node)
        width = kw.get("width")
        height = kw.get("height")
        if width is not None and height is not None and len(kw) == _PAIR_ARITY:
            return self._replace(node, f"{recv}.pages.new(({self._render(width)}, {self._render(height)}))")
        return self._visit_children(node)

    def _rewrite_save_call(self, node: ast.Call, recv: str, attr: str) -> list[_Edit]:
        target_name = "to_bytes" if attr in {"tobytes", "to_bytes"} else "save"
        changed = target_name != attr
        pos_args = [self._render(arg) for arg in node.args]
        kw_parts: list[str] = []
        for keyword in node.keywords:
            if keyword.arg is None:
                kw_parts.append(f"**{self._render(keyword.value)}")
                continue
            val = self._render(keyword.value)
            if keyword.arg in _SAVE_KWARG_RENAMES:
                changed = True
                kw_parts.append(f"{_SAVE_KWARG_RENAMES[keyword.arg]}={val}")
            else:
                if keyword.arg in _SAVE_MANUAL_KWARGS:
                    code, message = _SAVE_MANUAL_KWARGS[keyword.arg]
                    self._warn(keyword, code, message)
                kw_parts.append(f"{keyword.arg}={val}")
        if not changed:
            return self._visit_children(node)
        return self._replace(node, f"{recv}.{target_name}({', '.join([*pos_args, *kw_parts])})")

    def _rewrite_get_text(self, node: ast.Call, recv: str) -> list[_Edit]:
        if not node.args:
            mode_arg = "text"
        elif len(node.args) == 1 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            mode_arg = node.args[0].value.lower()
        else:
            self._warn(
                node,
                "manual-get-text",
                "Dynamic get_text() option; map to page.text(), page.words(), page.chars(), or page.layout().",
            )
            return self._visit_children(node)

        kw_str = self._collect_get_text_kwargs(node.keywords)
        entry = _GET_TEXT_MODES.get(mode_arg)
        if entry is None:
            self._warn(
                node,
                "manual-get-text",
                f"Unsupported get_text({mode_arg!r}) mode; choose text(), words(), chars(), or layout().",
            )
            return self._visit_children(node)
        template, warning = entry
        if warning is not None:
            self._warn(node, warning[0], warning[1])
        return self._replace(node, template.format(recv=recv, kw=kw_str))

    def _collect_get_text_kwargs(self, keywords: list[ast.keyword]) -> str:
        extra_kw: list[str] = []
        for keyword in keywords:
            if keyword.arg is None:
                extra_kw.append(f"**{self._render(keyword.value)}")
            elif keyword.arg == "sort":
                self._warn(
                    keyword,
                    "behavior-reading-order",
                    "pyropdf extracts text in display reading order by default; remove sort=True.",
                )
            elif keyword.arg == "flags":
                self._warn(
                    keyword,
                    "manual-text-flags",
                    "Replace flags= bitmasks with keyword options (ligatures=, whitespace=, actual_text=).",
                )
            else:
                extra_kw.append(f"{keyword.arg}={self._render(keyword.value)}")
        return ", ".join(extra_kw)

    def _rewrite_get_pixmap(self, node: ast.Call, recv: str) -> list[_Edit]:
        if node.args:
            self._warn(
                node,
                "manual-pixmap-args",
                "page.pixmap() uses keyword-only arguments (dpi=, matrix=, clip=, alpha=, colorspace=, antialias=).",
            )
        pos_args = [self._render(arg) for arg in node.args]
        kw_parts: list[str] = []
        for keyword in node.keywords:
            if keyword.arg is None:
                kw_parts.append(f"**{self._render(keyword.value)}")
                continue
            val = self._render(keyword.value)
            if keyword.arg == "annots":
                kw_parts.append(f"annotations={val}")
            else:
                if keyword.arg == "colorspace" and "csCMYK" in self._raw(keyword.value):
                    self._warn(
                        keyword,
                        "unsupported-cmyk-pixmap",
                        "CMYK pixmaps are not yet supported (#425); use colorspace='rgb' or 'gray'.",
                    )
                kw_parts.append(f"{keyword.arg}={val}")
        return self._replace(node, f"{recv}.pixmap({', '.join([*pos_args, *kw_parts])})")

    def _rewrite_get_svg(self, node: ast.Call, recv: str) -> list[_Edit]:
        kw_parts: list[str] = []
        for keyword in node.keywords:
            if keyword.arg == "text_as_path":
                if isinstance(keyword.value, ast.Constant) and keyword.value.value is False:
                    self._warn(
                        keyword,
                        "unsupported-svg-text",
                        "Selectable SVG text (text_as_path=False) is not yet supported (#435); svg() emits paths.",
                    )
                continue
            if keyword.arg == "annots":
                kw_parts.append(f"annotations={self._render(keyword.value)}")
            elif keyword.arg is None:
                kw_parts.append(f"**{self._render(keyword.value)}")
            else:
                kw_parts.append(f"{keyword.arg}={self._render(keyword.value)}")
        return self._replace(node, f"{recv}.svg({', '.join(kw_parts)})")

    def _rewrite_bake(self, node: ast.Call, recv: str) -> list[_Edit]:
        kw_parts: list[str] = []
        for keyword in node.keywords:
            if keyword.arg == "annots":
                kw_parts.append(f"annotations={self._render(keyword.value)}")
            elif keyword.arg is None:
                kw_parts.append(f"**{self._render(keyword.value)}")
            else:
                kw_parts.append(f"{keyword.arg}={self._render(keyword.value)}")
        return self._replace(node, f"{recv}.flatten({', '.join(kw_parts)})")

    def _format_call_args(self, node: ast.Call) -> str:
        parts = [self._render(arg) for arg in node.args]
        for keyword in node.keywords:
            if keyword.arg is None:
                parts.append(f"**{self._render(keyword.value)}")
            else:
                parts.append(f"{keyword.arg}={self._render(keyword.value)}")
        return ", ".join(parts)


def transform_source(source: str, *, mode: SourceKind = "auto") -> TransformResult:
    """Rewrite a Python source string from PyMuPDF or pylopdf 0.15 to pyropdf."""
    return _Rewriter(source, mode=mode).run()


def _iter_python_files(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(sorted(p for p in path.rglob("*.py") if p.is_file()))
        else:
            found.append(path)
    return found


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for migrating files or directories."""
    parser = argparse.ArgumentParser(
        description="Rewrite PyMuPDF (fitz/pymupdf) and pylopdf 0.15 Python code for pyropdf.",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Python files or directories to inspect/rewrite.",
    )
    parser.add_argument(
        "--from",
        dest="source_kind",
        choices=("auto", "pymupdf", "pylopdf"),
        default="auto",
        help="Source API family to migrate from (default: auto).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report changes and diagnostics without writing files.",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Print unified diffs for modified files (implies --dry-run).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit with status 1 if any file would change (implies --dry-run).",
    )
    args = parser.parse_args(argv)

    dry_run = bool(args.dry_run or args.diff or args.check)
    changed_count = 0
    diagnostic_count = 0

    for file_path in _iter_python_files(args.paths):
        original = file_path.read_text(encoding="utf-8")
        try:
            result = transform_source(original, mode=args.source_kind)
        except SyntaxError as error:
            sys.stderr.write(f"{file_path}:{error.lineno or 1}: syntax error: {error.msg}\n")
            return 2

        if result.changed:
            changed_count += 1
            if args.diff:
                diff = difflib.unified_diff(
                    original.splitlines(keepends=True),
                    result.source.splitlines(keepends=True),
                    fromfile=f"a/{file_path}",
                    tofile=f"b/{file_path}",
                )
                sys.stdout.writelines(diff)
            elif dry_run:
                sys.stdout.write(f"would rewrite {file_path}\n")
            else:
                file_path.write_text(result.source, encoding="utf-8")
                sys.stdout.write(f"rewrote {file_path}\n")

        for diag in result.diagnostics:
            diagnostic_count += 1
            sys.stderr.write(f"{file_path}:{diag.line}:{diag.col}: [{diag.code}] {diag.message}\n")

    if dry_run and not args.diff:
        sys.stdout.write(f"{changed_count} file(s) would change, {diagnostic_count} warning(s)\n")
    if args.check and changed_count > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
