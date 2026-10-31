"""Page fonts, embedded font programs, and font subsetting.

``page.fonts()`` lists the fonts that a page's resources and the resources
of its Form XObjects name, and ``doc.font_program()`` returns a font's
embedded program (ISO 32000-2 sections 9.6 to 9.9).


``doc.subset_fonts()`` replaces each embedded TrueType, OpenType, and CFF
program with a subset of the glyphs that its pages draw, as hayro resolves
them for rendering, together with the glyphs that those draw as
components or accents. A subset keeps every glyph ID and empties the
outlines of the others, so the content strings, encodings, ``cmap``,
CIDToGIDMap, widths, and ToUnicode stay valid unchanged; subsetting
another time keeps only the glyphs then drawn (ISO 32000-2 section 9.9).

Optional content counts as drawn whatever its state. A program that an
annotation appearance or the interactive form's default resources reach
stays whole, since another appearance state, a hidden annotation, or a
field value filled later may draw any of its glyphs; so does a program that
a direct font dictionary or a Type 1 or Type 3 font uses.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import re
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast, get_args

from pyropdf import _core
from pyropdf._errors import LimitError, PdfError
from pyropdf._geometry import Rect
from pyropdf._limits import _validate_optional_positive_int
from pyropdf._objects import Array, Dictionary, Name, Stream

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pyropdf._document import Document
    from pyropdf._page import Page

#: The font program keys of a font descriptor (ISO 32000-2 table 124).
_PROGRAMS = ("FontFile", "FontFile2", "FontFile3")
#: The ``FontFile3`` subtypes that are bare CFF programs (ISO 32000-2 table 125).
_CFF = frozenset({"Type1C", "CIDFontType0C"})
#: The subtypes of font dictionaries (ISO 32000-2 table 109).
_FONT_SUBTYPES = frozenset({"Type0", "Type1", "MMType1", "Type3", "TrueType"})
#: A subset tag: six uppercase letters and a plus sign (ISO 32000-2 9.9.2).
_TAG = re.compile(r"[A-Z]{6}\+")
#: Form XObjects of appearance resources that a walk visits.
_MAX_APPEARANCE_FORMS = 4096
#: Nesting of direct values that the walk for direct font dictionaries follows.
_MAX_DEPTH = 32
#: Form XObjects that ``page.fonts()`` visits.
_MAX_PAGE_FORMS = 4096
#: ``/Font`` and ``/XObject`` entries that ``page.fonts()`` visits, over all its resources.
_MAX_PAGE_ENTRIES = 65536
#: Parents that a page's inherited ``/Resources`` lookup follows.
_MAX_TREE_DEPTH = 64


@dataclass(frozen=True, slots=True)
class FontInfo:
    """One font dictionary that a page's resources name, from :meth:`Page.fonts`."""

    xref: int | None
    """The object number of the font dictionary, or ``None`` for a direct one."""
    names: tuple[str, ...]
    """The resource names of the font, in the page's and then its forms' resources."""
    base_font: str
    """The ``BaseFont``, or ``""`` when absent, as for most Type 3 fonts."""
    subtype: str
    """The font ``Subtype``: ``Type1``, ``MMType1``, ``TrueType``, ``Type3``, or ``Type0``."""
    encoding: str
    """The ``Encoding`` name, the ``BaseEncoding`` of an encoding dictionary, or ``""``."""
    program: str | None
    """The embedded program's type: ``ttf``, ``otf``, ``cff``, or ``pfa``; ``None`` when not embedded."""
    subset: bool
    """Whether ``base_font`` carries a subset tag (ISO 32000-2 9.9.2)."""
    forms: tuple[int, ...]
    """The object numbers of the Form XObjects whose resources name the font; empty when only the page's do."""
    _font: Dictionary = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class FontProgram:
    """An embedded font program, from :meth:`Document.font_program`."""

    type: str
    """``ttf``, ``otf``, ``cff``, ``pfa``, or ``pfb``, from the descriptor key and the program's header."""
    data: bytes
    """The decoded program."""


@dataclass(frozen=True, slots=True)
class SubsetFont:
    """One font program that :meth:`Document.subset_fonts` subset."""

    xref: int
    """The object number of the program's stream."""
    name: str
    """The ``FontName`` of the font descriptor, with its new subset tag."""
    glyphs: int
    """The glyphs of the program."""
    kept: int
    """The glyphs whose outlines the subset keeps, with ``.notdef``."""
    size_before: int
    """The decoded length of the program before subsetting."""
    size_after: int
    """The decoded length of the subset program."""


class _Rollback(Exception):  # noqa: N818 - control flow, never raised to a caller.
    """Ends the transaction that turns every optional content group on."""


@dataclass(slots=True)
class _Program:
    """An embedded font program and the font dictionaries that use it."""

    stream: Stream
    format: str
    descriptors: list[Dictionary]
    fonts: list[Dictionary]


def _name(value: object) -> str:
    return str(value) if isinstance(value, str) else ""


def _encoding(font: Dictionary) -> str:
    encoding = font.get("Encoding")
    if isinstance(encoding, Dictionary) and not isinstance(encoding, Stream):
        encoding = encoding.get("BaseEncoding")
    return _name(encoding)


def _embedded(font: Dictionary) -> tuple[Stream, str] | None:
    """Return the embedded program of ``font`` and its type, as the descriptor key and subtype name it."""
    for descriptor in _descriptors(font):
        stream = descriptor.get("FontFile2")
        if isinstance(stream, Stream):
            return stream, "ttf"
        stream = descriptor.get("FontFile3")
        if isinstance(stream, Stream):
            subtype = stream.get("Subtype")
            if subtype == "OpenType":
                return stream, "otf"
            return (stream, "cff") if subtype in _CFF else None
        stream = descriptor.get("FontFile")
        if isinstance(stream, Stream):
            return stream, "pfa"
    return None


def _page_resources(page: Page) -> object:
    node: object = page.obj
    # The page itself, then up to _MAX_TREE_DEPTH parents.
    for _ in range(_MAX_TREE_DEPTH + 1):
        if not isinstance(node, Dictionary):
            return None
        if "Resources" in node:
            return node["Resources"]
        node = node.get("Parent")
    if not isinstance(node, Dictionary):
        return None
    msg = f"the page inherits its resources through more than {_MAX_TREE_DEPTH} parents"
    code = "page_fonts_tree"
    raise LimitError(code, msg)


#: The fonts that a page walk found, by identity: the dictionary, its resource names, and its forms.
_Found = dict[object, tuple[Dictionary, list[str], list[int]]]


class _Budget:
    """The ``/Font`` and ``/XObject`` entries that one ``page.fonts()`` walk may visit."""

    __slots__ = ("left",)

    def __init__(self) -> None:
        self.left = _MAX_PAGE_ENTRIES

    def spend(self, entries: int) -> None:
        self.left -= entries
        if self.left < 0:
            msg = f"the page's resources and forms hold more than {_MAX_PAGE_ENTRIES} font and XObject entries"
            code = "page_fonts_entries"
            raise LimitError(code, msg)


def _collect(found: _Found, budget: _Budget, resources: Dictionary, form: int) -> None:
    """Record the fonts of the ``/Font`` entry of ``resources``, the resources of ``form`` or 0 for the page."""
    fonts = resources.get("Font")
    if not isinstance(fonts, Dictionary):
        return
    budget.spend(len(fonts))
    # A direct font dictionary is one entry of the indirect dictionary that holds it.
    owner: object = fonts.xref if fonts.xref is not None else resources.xref
    if owner is None:
        owner = ("form", form)
    for key in fonts:
        font = fonts[key]
        if not isinstance(font, Dictionary):
            continue
        identity = font.xref if font.xref is not None else ("direct", owner, key)
        _, names, forms = found.setdefault(identity, (font, [], []))
        names.append(str(key))
        if form and form not in forms:
            forms.append(form)


def _info(font: Dictionary, names: list[str], forms: list[int]) -> FontInfo:
    base = _name(font.get("BaseFont"))
    embedded = _embedded(font)
    return FontInfo(
        xref=font.xref,
        names=tuple(names),
        base_font=base,
        subtype=_name(font.get("Subtype")),
        encoding=_encoding(font),
        program=embedded[1] if embedded is not None else None,
        subset=_TAG.match(base) is not None,
        forms=tuple(forms),
        _font=font,
    )


def page_fonts(page: Page) -> list[FontInfo]:
    """Return the fonts that the resources of ``page`` and of its Form XObjects name."""
    found: _Found = {}
    budget = _Budget()
    pending: deque[tuple[object, int]] = deque([(_page_resources(page), 0)])
    seen: set[int] = set()
    while pending:
        resources, form = pending.popleft()
        if not isinstance(resources, Dictionary):
            continue
        _collect(found, budget, resources, form)
        xobjects = resources.get("XObject")
        if not isinstance(xobjects, Dictionary):
            continue
        budget.spend(len(xobjects))
        for key in xobjects:
            xobject = xobjects[key]
            if not isinstance(xobject, Stream) or xobject.get("Subtype") != "Form" or xobject.xref is None:
                continue
            if xobject.xref in seen:
                continue
            seen.add(xobject.xref)
            if len(seen) > _MAX_PAGE_FORMS:
                msg = f"the page reaches more than {_MAX_PAGE_FORMS} form XObjects"
                code = "page_fonts_forms"
                raise LimitError(code, msg)
            pending.append((xobject.get("Resources"), xobject.xref))
    return [_info(*entry) for entry in found.values()]


def font_program(document: Document, font: int | FontInfo) -> FontProgram | None:
    """Return the decoded embedded program of ``font``, or ``None`` when it embeds none."""
    if isinstance(font, FontInfo):
        dictionary: object = font._font  # noqa: SLF001 - the module's own record.
        if font._font._document is not document:  # noqa: SLF001 - the package's own handle.
            msg = "the FontInfo comes from another document"
            raise ValueError(msg)
    else:
        dictionary = document.objects.get(font)
    if not isinstance(dictionary, Dictionary) or dictionary.get("Subtype") not in _FONT_SUBTYPES:
        msg = f"object {font!r} is not a font dictionary"
        raise ValueError(msg)
    embedded = _embedded(dictionary)
    if embedded is None:
        return None
    stream, kind = embedded
    data = stream.read()
    if kind == "pfa" and data[:2] == b"\x80\x01":
        kind = "pfb"
    return FontProgram(kind, data)


def _xref(value: object) -> int | None:
    return value.xref if isinstance(value, (Dictionary, Array)) else None


def _program(descriptor: object) -> tuple[Stream, str] | None:
    """Return the program of ``descriptor`` that the subsetter rewrites, and its format."""
    if not isinstance(descriptor, Dictionary):
        return None
    stream = descriptor.get("FontFile2")
    if isinstance(stream, Stream):
        return stream, "sfnt"
    stream = descriptor.get("FontFile3")
    if isinstance(stream, Stream):
        subtype = stream.get("Subtype")
        if subtype == "OpenType":
            return stream, "sfnt"
        if subtype in _CFF:
            return stream, "cff"
    return None


def _descriptors(font: Dictionary) -> Iterator[Dictionary]:
    """Yield the font descriptor of ``font``, or of its descendant font."""
    if font.get("Subtype") == "Type0":
        descendants = font.get("DescendantFonts")
        if isinstance(descendants, Array):
            font = next((item for item in descendants if isinstance(item, Dictionary)), font)
    descriptor = font.get("FontDescriptor")
    if isinstance(descriptor, Dictionary):
        yield descriptor


def _direct_fonts(document: Document) -> Iterator[Dictionary]:
    """Yield the font dictionaries that are direct values of indirect objects."""
    objects = document.objects
    for number in objects:
        pending: list[tuple[object, int]] = [(objects.get(number), 0)]
        while pending:
            value, depth = pending.pop()
            if depth >= _MAX_DEPTH:
                continue
            if isinstance(value, Dictionary):
                if depth and value.xref is not None:
                    continue
                if depth and value.get("Type") == "Font":
                    yield value
                pending.extend((value[key], depth + 1) for key in value)
            elif isinstance(value, Array):
                if depth and value.xref is not None:
                    continue
                pending.extend((item, depth + 1) for item in value)


def _resource_fonts(roots: list[object]) -> Iterator[Dictionary]:
    """Yield the fonts that the resources of streams ``roots`` reach, through their form XObjects."""
    pending = list(roots)
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        if isinstance(value, Dictionary) and not isinstance(value, Stream):
            # An appearance dictionary, or one of its state dictionaries.
            pending.extend(value[key] for key in value)
            continue
        if not isinstance(value, Stream) or value.xref is None or value.xref in seen:
            continue
        seen.add(value.xref)
        if len(seen) > _MAX_APPEARANCE_FORMS:
            msg = f"the appearances reach more than {_MAX_APPEARANCE_FORMS} form XObjects"
            code = "font_subset_appearances"
            raise LimitError(code, msg)
        resources = value.get("Resources")
        if not isinstance(resources, Dictionary):
            continue
        fonts = resources.get("Font")
        if isinstance(fonts, Dictionary):
            yield from (font for key in fonts if isinstance(font := fonts[key], Dictionary))
        forms = resources.get("XObject")
        if isinstance(forms, Dictionary):
            pending.extend(forms[key] for key in forms)


def _kept_whole(document: Document) -> set[int]:
    """Return the object numbers of the font descriptors whose programs stay whole."""
    fonts = list(_direct_fonts(document))
    appearances: list[object] = []
    for page in document.pages:
        annotations = page.obj.get("Annots")
        if isinstance(annotations, Array):
            appearances.extend(item["AP"] for item in annotations if isinstance(item, Dictionary) and "AP" in item)
    fonts.extend(_resource_fonts(appearances))
    form = document.catalog.get("AcroForm")
    if isinstance(form, Dictionary):
        resources = form.get("DR")
        default = resources.get("Font") if isinstance(resources, Dictionary) else None
        if isinstance(default, Dictionary):
            fonts.extend(font for key in default if isinstance(font := default[key], Dictionary))
    return {xref for font in fonts for descriptor in _descriptors(font) if (xref := descriptor.xref) is not None}


def _programs(document: Document) -> dict[int, _Program]:
    """Return the programs to subset by stream object number, with the indirect fonts that use them."""
    whole = _kept_whole(document)
    programs: dict[int, _Program] = {}
    blocked: set[int] = set()
    objects = document.objects
    for number in objects:
        font = objects.get(number)
        if not isinstance(font, Dictionary) or font.get("Type") != "Font" or font.get("Subtype") == "Type3":
            continue
        for descriptor in _descriptors(font):
            found = _program(descriptor)
            if found is None:
                continue
            stream, kind = found
            if stream.xref is None:
                continue
            if descriptor.xref is None or descriptor.xref in whole:
                blocked.add(stream.xref)
                continue
            program = programs.setdefault(stream.xref, _Program(stream, kind, [], []))
            if all(item.xref != descriptor.xref for item in program.descriptors):
                program.descriptors.append(descriptor)
            program.fonts.append(font)
    return {xref: program for xref, program in programs.items() if xref not in blocked}


def _glyphs(document: Document, fonts: list[Dictionary]) -> list[list[int]]:
    """Return the glyphs that the pages draw with each of ``fonts``, with every optional content group on."""
    keys = [(font.xref or 0, font.generation or 0) for font in fonts]
    properties = document.catalog.get("OCProperties")
    config = properties.get("D") if isinstance(properties, Dictionary) else None
    if not isinstance(config, Dictionary):
        return _core.used_glyphs(document._core(), keys)  # noqa: SLF001 - the package's own core.
    used: list[list[int]] = []
    with contextlib.suppress(_Rollback), document.transaction("subset fonts: optional content"):
        config["BaseState"] = Name("ON")
        if "OFF" in config:
            del config["OFF"]
        used = _core.used_glyphs(document._core(), keys)  # noqa: SLF001 - the package's own core.
        raise _Rollback
    return used


def _tag(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "".join(chr(ord("A") + byte % 26) for byte in digest[:6]) + "+"


def _rename(dictionary: Dictionary, key: str, tag: str) -> None:
    name = dictionary.get(key)
    if isinstance(name, str) and not _TAG.match(name):
        dictionary[key] = Name(tag + name)


def subset_fonts(document: Document) -> tuple[SubsetFont, ...]:
    """Subset each embedded program to the glyphs drawn, in one transaction, and report what changed."""
    programs = _programs(document)
    fonts = [font for program in programs.values() for font in program.fonts]
    used = iter(_glyphs(document, fonts))
    keep = {xref: sorted({glyph for _ in program.fonts for glyph in next(used)}) for xref, program in programs.items()}
    report: list[SubsetFont] = []
    with document.transaction("subset fonts"):
        for xref, program in programs.items():
            data = program.stream.read()
            subset = _core.subset_font_program(data, program.format, keep[xref])
            if subset is None or len(subset[0]) >= len(data):
                continue
            new, glyphs, kept = subset
            program.stream.write(new)
            if "Length1" in program.stream:
                program.stream["Length1"] = len(new)
            tag = _tag(new)
            for descriptor in program.descriptors:
                _rename(descriptor, "FontName", tag)
            for font in program.fonts:
                _rename(font, "BaseFont", tag)
                if font.get("Subtype") == "Type0":
                    descendants = font.get("DescendantFonts")
                    for descendant in descendants if isinstance(descendants, Array) else ():
                        if isinstance(descendant, Dictionary):
                            _rename(descendant, "BaseFont", tag)
            name = program.descriptors[0].get("FontName")
            report.append(SubsetFont(xref, str(name or ""), glyphs, kept, len(data), len(new)))
    return tuple(report)


#: The PostScript names of the Standard 14 fonts (ISO 32000-2 section 9.6.2.2).
StandardFontName = Literal[
    "Courier",
    "Courier-Oblique",
    "Courier-Bold",
    "Courier-BoldOblique",
    "Helvetica",
    "Helvetica-Oblique",
    "Helvetica-Bold",
    "Helvetica-BoldOblique",
    "Times-Roman",
    "Times-Italic",
    "Times-Bold",
    "Times-BoldItalic",
    "Symbol",
    "ZapfDingbats",
]
StandardFontName.__doc__ = "The PostScript names of the Standard 14 fonts, which :meth:`Font.standard` takes."
_STANDARD_FONT_NAMES: tuple[str, ...] = get_args(StandardFontName)
#: The UTF-8 bytes of text that a measurement or a drawing takes by default.
_DEFAULT_MAX_GENERATED_TEXT_SIZE = 1024 * 1024
#: The lines that one text box or text drawing lays out by default.
_MAX_GENERATED_TEXT_LINES = 4096
_CONSTRUCTOR_MESSAGE = (
    'Font has no public constructor: Font.standard(name) takes a Standard 14 PostScript name such as "Helvetica-Bold"'
)


def _measure_size(value: object, name: str) -> float:
    """Return a positive finite measure, such as a font size or a box width."""
    if isinstance(value, (bool, str, bytes)) or not hasattr(value, "__float__"):
        msg = f"{name} must be a positive number: {value!r}"
        raise TypeError(msg)
    number = float(cast("float", value))
    if not (math.isfinite(number) and number > 0):
        msg = f"{name} must be a positive number: {value!r}"
        raise ValueError(msg)
    return number


def _text_input(text: object, max_text_size: int | None) -> str:
    """Return ``text`` when it is a string within ``max_text_size`` bytes of UTF-8."""
    if not isinstance(text, str):
        msg = f"text must be a string: {text!r}"
        raise TypeError(msg)
    _validate_optional_positive_int("max_text_size", max_text_size)
    # Each character takes one to four bytes, so only a long text is encoded.
    if max_text_size is not None and len(text) * 4 > max_text_size:
        size = len(text.encode("utf-8", "surrogatepass"))
        if size > max_text_size:
            code = "text_input_size"
            msg = f"the text is {size} bytes of UTF-8, over the {max_text_size}-byte limit"
            raise LimitError(code, msg)
    return text


def _max_lines(max_text_size: int | None) -> int | None:
    """Return the lines that a layout may take: none past the default unless the text is trusted."""
    return None if max_text_size is None else _MAX_GENERATED_TEXT_LINES


def _textbox_text(text: object, max_text_size: int | None) -> str:
    """Return text as a textbox lays it out, with every line break a newline and every tab one space."""
    checked = _text_input(text, max_text_size)
    return checked.replace("\r\n", "\n").replace("\r", "\n").expandtabs(1)


def _one_char(char: object) -> str:
    """Return ``char`` when it is a one-character string."""
    if not isinstance(char, str):
        msg = f"char must be a one-character string: {char!r}"
        raise TypeError(msg)
    if len(char) != 1:
        msg = f"char must be a one-character string: {char!r}"
        raise ValueError(msg)
    return char


class Font:
    """A font face that draws and measures text.

    Get one from :meth:`standard`; pyropdf has no default font and keeps no
    font registry, so output depends only on the fonts passed in. Pass it as
    ``font`` to :meth:`Canvas.text` and :meth:`Canvas.textbox` on any
    document, and share it between threads. :meth:`text_width` measures one
    line and :meth:`text_height` wrapped text exactly as the canvas draws
    them; :meth:`char_widths` and :meth:`char_bbox` give the metrics of
    single characters drawn alone.

    A Standard 14 font is drawn without embedding, so it prints WinAnsi
    (roughly Latin-1) text only, and measures with Adobe's AFM widths.
    """

    __slots__ = ("_font",)

    _font: _core._Font

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        """Refuse direct construction; the class methods create fonts."""
        raise TypeError(_CONSTRUCTOR_MESSAGE)

    @classmethod
    def _wrap(cls, face: _core._Font) -> Font:
        font = cls.__new__(cls)
        font._font = face  # noqa: SLF001 - the class sets its own slot.
        return font

    @classmethod
    def standard(cls, name: StandardFontName) -> Font:
        """Return a Standard 14 font by its PostScript name, such as ``"Helvetica-Bold"``.

        The 14 names are those of :data:`StandardFontName`. A Standard 14 font
        is drawn without embedding, so it prints WinAnsi (roughly Latin-1)
        text only.
        """
        if not isinstance(name, str):
            msg = f"name must be a string: {name!r}"
            raise TypeError(msg)
        if name not in _STANDARD_FONT_NAMES:
            names = ", ".join(_STANDARD_FONT_NAMES)
            msg = f"name must be the PostScript name of a Standard 14 font ({names}): {name!r}"
            raise ValueError(msg)
        return cls._wrap(_core._Font.standard(name))  # noqa: SLF001 - the core's face.

    @property
    def name(self) -> str | None:
        """The PostScript name of a Standard 14 font."""
        return self._font.name

    @property
    def family(self) -> str | None:
        """The family name, such as ``"Times"`` for ``"Times-Bold"``."""
        return self._font.family

    @property
    def weight(self) -> int:
        """The weight: 400 for regular and 700 for bold faces."""
        return self._font.weight

    @property
    def is_bold(self) -> bool:
        """Whether the face is bold."""
        return self._font.bold

    @property
    def is_italic(self) -> bool:
        """Whether the face is italic or oblique."""
        return self._font.italic

    @property
    def is_serif(self) -> bool:
        """Whether the face has serifs."""
        return self._font.serif

    @property
    def units_per_em(self) -> int:
        """The design units per em of the face; 1000 for Standard 14 fonts."""
        return self._font.units_per_em

    @property
    def ascender(self) -> float:
        """The ascent above the baseline as a fraction of the font size.

        A Standard 14 font reports the bounding box of the URW font that
        renderers commonly substitute for it, such as 1.075 for Helvetica.
        The canvas places lines of text with :attr:`ascender` and
        :attr:`descender`.
        """
        return self._font.ascender

    @property
    def descender(self) -> float:
        """The descent below the baseline as a negative fraction of the font size."""
        return self._font.descender

    @property
    def bbox(self) -> Rect:
        """The font bounding box in fractions of the em, y upward from the baseline.

        A Standard 14 font reports the box of the URW font that renderers
        commonly substitute for it, whose top and bottom are its
        :attr:`ascender` and :attr:`descender`, such as
        ``Rect(-0.21, -0.299, 1.032, 1.075)`` for Helvetica.
        """
        return Rect(*self._font.bbox)

    @property
    def glyph_count(self) -> int:
        """The number of glyphs: those of a Standard 14 font's AFM metrics."""
        return self._font.glyph_count

    @property
    def is_monospaced(self) -> bool:
        """Whether every glyph has the same advance, as Adobe's AFM metrics declare for the Courier fonts."""
        return self._font.monospaced

    def chars(self) -> str:
        """Return every character that :meth:`has_glyph` accepts, in code point order.

        A Standard 14 text font has the WinAnsi characters (roughly
        Latin-1), and Symbol and ZapfDingbats the characters whose WinAnsi
        byte their built-in encoding maps to a glyph.
        """
        return self._font.chars()

    def char_widths(
        self, text: str, size: float, *, max_text_size: int | None = _DEFAULT_MAX_GENERATED_TEXT_SIZE
    ) -> tuple[float, ...]:
        """Return the advance of each character of ``text`` drawn alone at ``size`` points.

        A character the font has no glyph for raises :class:`PdfError`. Text
        is limited to 1 MiB of UTF-8 by default (``max_text_size``, ``None``
        for trusted input), refused with :class:`LimitError` code
        ``text_input_size``.
        """
        checked = _text_input(text, max_text_size)
        return tuple(self._font.char_widths(checked, _measure_size(size, "size")))

    def char_bbox(self, char: str) -> Rect:
        """Return the ink box of the glyph for the one-character string ``char``.

        The box is in fractions of the em, y upward from the glyph's origin
        on the baseline: Adobe's AFM box for a Standard 14 font. A glyph
        without ink, such as a space, has an empty box at its origin. A
        character the font has no glyph for raises :class:`PdfError`.
        """
        return Rect(*self._font.char_bbox(ord(_one_char(char))))

    def program(self) -> bytes | None:
        """Return the bytes that the face was loaded from, or ``None`` for a Standard 14 font."""
        return None

    def text_width(
        self, text: str, size: float, *, max_text_size: int | None = _DEFAULT_MAX_GENERATED_TEXT_SIZE
    ) -> float:
        """Return the width of one line of ``text`` at ``size`` points.

        Standard 14 text sums the AFM widths of its WinAnsi characters.
        Measure each line separately: a line break raises :class:`PdfError`,
        as does a character the font has no glyph for. Text is limited to
        1 MiB of UTF-8 by default (``max_text_size``, ``None`` for trusted
        input), refused with :class:`LimitError` code ``text_input_size``.
        """
        checked = _text_input(text, max_text_size)
        if "\n" in checked or "\r" in checked:
            msg = "text_width measures one line; split text at line breaks first"
            raise PdfError(msg)
        return self._font.text_width(checked, _measure_size(size, "size"))

    def text_height(
        self,
        text: str,
        size: float,
        *,
        width: float,
        line_height: float | None = None,
        max_text_size: int | None = _DEFAULT_MAX_GENERATED_TEXT_SIZE,
    ) -> float:
        """Return the height that :meth:`Canvas.textbox` needs for ``text`` wrapped ``width`` points wide.

        The text wraps and its lines are placed exactly as the textbox
        places them at ``size`` and ``line_height``: the first baseline one
        :attr:`ascender` below the top, later ones ``line_height`` times
        ``size`` apart (by default the font's line height), and the height
        ends at the :attr:`descender` of the last line. Empty text needs no
        height. A word that cannot fit ``width`` even one grapheme at a time
        raises :class:`PdfError`. Text is limited to 1 MiB of UTF-8 and 4,096
        lines by default (``max_text_size``, ``None`` for trusted input),
        refused with :class:`LimitError` code ``text_input_size`` or
        ``text_line_count``.
        """
        normalized = _textbox_text(text, max_text_size)
        font_size = _measure_size(size, "size")
        box_width = _measure_size(width, "width")
        spacing = None if line_height is None else _measure_size(line_height, "line_height")
        if not normalized:
            return 0.0
        _lines, height, _spare, _ascent, _leading = self._font.layout(
            normalized,
            box_width,
            0.0,
            font_size,
            spacing,
            False,  # noqa: FBT003 - the core's flag
            _max_lines(max_text_size),
        )
        return height

    def has_glyph(self, char: str) -> bool:
        """Return whether the face has a glyph for the one-character string ``char``.

        A Standard 14 font has one when its encoding, WinAnsi for the text
        fonts, maps the character to a glyph of the font.
        """
        return self._font.has_glyph(ord(_one_char(char)))

    def __repr__(self) -> str:
        """Return the font's name."""
        return f"Font({self.name!r})"


# The package re-exports these names; reprs, pickles, and documentation name them.
for _public in (FontInfo, FontProgram, SubsetFont, Font):
    _public.__module__ = "pyropdf"
