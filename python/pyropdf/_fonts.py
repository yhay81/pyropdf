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
import os
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
    from collections.abc import Iterator, Sequence

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
_UNKNOWN_GLYPHS = 0xFFFFFFFF
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


def _kept_glyphs(programs: dict[int, _Program], used: Iterator[list[int]]) -> dict[int, list[int]]:
    """Keep shared programs whole if any font's glyph use is unknown."""
    keep: dict[int, list[int]] = {}
    for xref, program in programs.items():
        glyphs = [next(used) for _ in program.fonts]
        # The core reports an undecodable content stream with a value outside
        # the font's 16-bit glyph ID range. Keep every shared program whole.
        if any(_UNKNOWN_GLYPHS in items for items in glyphs):
            continue
        keep[xref] = sorted({glyph for items in glyphs for glyph in items})
    return keep


def subset_fonts(document: Document) -> tuple[SubsetFont, ...]:
    """Subset each embedded program to the glyphs drawn, in one transaction, and report what changed."""
    report: list[SubsetFont] = []
    with document.transaction("subset fonts"):
        programs = _programs(document)
        fonts = [font for program in programs.values() for font in program.fonts]
        keep = _kept_glyphs(programs, iter(_glyphs(document, fonts)))
        for xref, program in programs.items():
            if xref not in keep:
                continue
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
    'Font has no public constructor: Font.standard(name) takes a Standard 14 PostScript name such as "Helvetica-Bold", '
    "and Font.from_file(path) or Font.from_bytes(data) loads an OpenType face"
)
_DEFAULT_MAX_FONT_INPUT_SIZE = 64 * 1024 * 1024
_MAX_FONT_WEIGHT = 1000
_BOLD_FONT_WEIGHT = 700
_UINT32_MAX = 2**32 - 1
#: The ToUnicode destinations that one ``beginbfchar`` block holds (Adobe TN 5411).
_BFCHAR_BLOCK = 100
#: UTF-16 code units of the longest text that a ToUnicode entry maps a glyph to.
_MAX_MAPPED_UNITS = 256
_BFCHAR = re.compile(rb"<([0-9A-Fa-f]{4})>\s*<([0-9A-Fa-f]*)>")
_BFCHAR_BLOCKS = re.compile(rb"beginbfchar(.*?)endbfchar", re.DOTALL)
_CMAP_HEAD = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
"""
_CMAP_TAIL = b"""endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""


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
    # Count without encoding an input-sized copy, and stop at the limit.
    if max_text_size is not None and str.__len__(text) * 4 > max_text_size:
        size = 0
        for char in str.__iter__(text):
            size += len(char.encode("utf-8", "surrogatepass"))
            if size > max_text_size:
                code = "text_input_size"
                msg = f"the text exceeds the {max_text_size}-byte UTF-8 limit"
                raise LimitError(code, msg)
    return text


def _max_lines(max_text_size: int | None) -> int | None:
    """Return the lines that a layout may take: none past the default unless the text is trusted."""
    return None if max_text_size is None else _MAX_GENERATED_TEXT_LINES


def _textbox_text(text: object, max_text_size: int | None) -> str:
    """Return text as a textbox lays it out, with every line break a newline and every tab one space."""
    checked = _text_input(text, max_text_size)
    return checked.replace("\r\n", "\n").replace("\r", "\n").expandtabs(1)


def _font_weight(weight: object) -> int | None:
    """Return an optional OS/2 weight class."""
    if weight is None:
        return None
    msg = f"weight must be an OS/2 weight class from 1 through {_MAX_FONT_WEIGHT} or None: {weight!r}"
    if isinstance(weight, bool) or not isinstance(weight, int):
        raise TypeError(msg)
    if not 1 <= weight <= _MAX_FONT_WEIGHT:
        raise ValueError(msg)
    return weight


def _font_index(index: object) -> int:
    """Return the index of a face in a font collection."""
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= _UINT32_MAX:
        msg = f"index must be an integer from 0 through {_UINT32_MAX}: {index!r}"
        raise ValueError(msg)
    return index


def _font_input_limit(size: int, max_size: int) -> LimitError:
    msg = f"the font data is {size} bytes, over the {max_size}-byte limit"
    return LimitError("font_input_size", msg)


def _describe_weight(weight: int) -> str:
    return {400: "regular", _BOLD_FONT_WEIGHT: "bold"}.get(weight, f"weight-{weight}")


def _at_weight(face: _core._Font, weight: int | None) -> _core._Font:
    """Return ``face`` when it is drawn at ``weight``.

    pyropdf draws a face's default instance only, and never imitates a
    weight that the font does not define.
    """
    if weight is None:
        return face
    axis = face.weight_axis
    current = face.weight if axis is None else round(axis[1])
    if weight == current:
        return face
    requested = _describe_weight(weight)
    if axis is not None:
        msg = (
            f"the font's default instance has weight {current}, not {weight}; "
            "pyropdf draws a variable font at its default instance only"
        )
        raise PdfError(msg)
    msg = (
        f"the font has no weight axis, and its only face is {_describe_weight(current)}, not {requested}; "
        f"load the {requested} face's file instead"
    )
    raise PdfError(msg)


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

    Get one from :meth:`standard`, :meth:`from_file`, or :meth:`from_bytes`;
    pyropdf has no default font and keeps no
    font registry, so output depends only on the fonts passed in. Pass it as
    ``font`` to :meth:`Canvas.text` and :meth:`Canvas.textbox` on any
    document, and share it between threads. :meth:`text_width` measures one
    line and :meth:`text_height` wrapped text exactly as the canvas draws
    them; :meth:`char_widths` and :meth:`char_bbox` give the metrics of
    single characters drawn alone.

    A Standard 14 font is drawn without embedding, so it prints WinAnsi
    (roughly Latin-1) text only, and measures with Adobe's AFM widths. An
    OpenType face is shaped with harfrust, in bidirectional and script runs,
    and a document embeds it once as a Type0 font whose program holds every
    glyph until a save.
    """

    __slots__ = ("_digest", "_font")

    _font: _core._Font
    _digest: bytes | None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        """Refuse direct construction; the class methods create fonts."""
        raise TypeError(_CONSTRUCTOR_MESSAGE)

    @classmethod
    def _wrap(cls, face: _core._Font) -> Font:
        font = cls.__new__(cls)
        font._font = face  # noqa: SLF001 - the class sets its own slot.
        font._digest = None  # noqa: SLF001 - the class sets its own slot.
        return font

    @classmethod
    def from_file(
        cls,
        path: str | os.PathLike[str],
        /,
        *,
        index: int = 0,
        weight: int | None = None,
        max_size: int | None = _DEFAULT_MAX_FONT_INPUT_SIZE,
    ) -> Font:
        """Load an OpenType face (TrueType or CFF outlines) from a font file.

        ``index`` selects a face in a collection such as a ``.ttc`` file. A
        variable face is drawn at its default instance. ``weight``, a weight
        class from 1 through 1000, must be the face's own weight: pyropdf
        never imitates a weight that the font does not define, and refuses
        other instances of a variable face. Invalid or unsupported font data
        raises :class:`PdfError`; a missing file raises
        :class:`FileNotFoundError`. The file is limited to 64 MiB by default
        (``max_size``, ``None`` for trusted input), refused with
        :class:`LimitError` code ``font_input_size``.
        """
        _validate_optional_positive_int("max_size", max_size)
        resolved = _font_weight(weight)
        face_index = _font_index(index)
        if not isinstance(path, (str, os.PathLike)):
            msg = f"path must be a str or os.PathLike, not {type(path).__name__}"
            raise TypeError(msg)
        with open(path, "rb") as file:  # noqa: PTH123 - a str or PathLike path.
            size = os.fstat(file.fileno()).st_size
            if max_size is not None and size > max_size:
                raise _font_input_limit(size, max_size)
            data = file.read() if max_size is None else file.read(max_size + 1)
        if max_size is not None and len(data) > max_size:
            raise _font_input_limit(len(data), max_size)
        return cls._wrap(_at_weight(_core._Font.from_bytes(data, face_index), resolved))  # noqa: SLF001

    @classmethod
    def from_bytes(
        cls,
        data: bytes | bytearray | memoryview,
        /,
        *,
        index: int = 0,
        weight: int | None = None,
        max_size: int | None = _DEFAULT_MAX_FONT_INPUT_SIZE,
    ) -> Font:
        """Load an OpenType face from the bytes of a font file.

        ``index``, ``weight``, and ``max_size`` work as for :meth:`from_file`.
        """
        _validate_optional_positive_int("max_size", max_size)
        resolved = _font_weight(weight)
        face_index = _font_index(index)
        if not isinstance(data, (bytes, bytearray, memoryview)):
            msg = f"data must be bytes, bytearray, or memoryview, not {type(data).__name__}"
            raise TypeError(msg)
        size = memoryview(data).nbytes
        if max_size is not None and size > max_size:
            raise _font_input_limit(size, max_size)
        return cls._wrap(_at_weight(_core._Font.from_bytes(bytes(data), face_index), resolved))  # noqa: SLF001

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
        """The PostScript name of a Standard 14 font, or the full name of an embedded face."""
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
        return self._font.char_widths(checked, _measure_size(size, "size"), max_text_size)

    def char_bbox(self, char: str) -> Rect:
        """Return the ink box of the glyph for the one-character string ``char``.

        The box is in fractions of the em, y upward from the glyph's origin
        on the baseline: Adobe's AFM box for a Standard 14 font. A glyph
        without ink, such as a space, has an empty box at its origin. A
        character the font has no glyph for raises :class:`PdfError`.
        """
        return Rect(*self._font.char_bbox(ord(_one_char(char))))

    def program(self) -> bytes | None:
        """Return the program that a document embeds, or ``None`` for a Standard 14 font.

        The program is the face alone with all its glyphs, as an OpenType
        file, even when it was loaded from a collection; a save subsets it
        to the glyphs that the document draws.
        """
        return self._font.program()

    def _key(self) -> bytes:
        """Return the digest that identifies the embedded program in a document."""
        if self._digest is None:
            self._digest = hashlib.sha256(cast("bytes", self._font.program())).digest()
        return self._digest

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
        return self._font.text_width(checked, _measure_size(size, "size"), max_text_size)

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
        checked = _text_input(text, max_text_size)
        font_size = _measure_size(size, "size")
        box_width = _measure_size(width, "width")
        spacing = None if line_height is None else _measure_size(line_height, "line_height")
        if not checked:
            return 0.0
        _lines, height, _spare, _ascent, _leading = self._font.layout(
            checked,
            box_width,
            0.0,
            font_size,
            spacing,
            False,  # noqa: FBT003 - the core's flag
            _max_lines(max_text_size),
            max_text_size,
            True,  # noqa: FBT003 - normalize the text within the core budget
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


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-be", "surrogatepass").hex().upper().encode()


def _flags(font: _core._Font) -> int:
    """Return the font descriptor flags of an embedded face (ISO 32000-2 table 121)."""
    flags = 4  # Symbolic: the glyphs are addressed by CID, not by a standard encoding.
    if font.monospaced:
        flags |= 1
    if font.serif:
        flags |= 2
    if font.italic:
        flags |= 64
    return flags


def _type0(document: Document, face: Font) -> int:
    """Write the Type0 font of an embedded face, its program provisional with all its glyphs."""
    font = face._font  # noqa: SLF001 - the package's own face.
    objects = document.objects
    program = cast("bytes", font.program())
    cff = font.cff
    file_key = "FontFile3" if cff else "FontFile2"
    file_entries: dict[str, object] = {"Subtype": Name("OpenType")} if cff else {}
    program_xref = objects.add_stream(program, file_entries)
    x0, y0, x1, y1 = font.bbox
    cap_height, italic_angle = font.descriptor_metrics
    name = Name(font.postscript_name)
    descriptor = objects.add(
        {
            "Type": Name("FontDescriptor"),
            "FontName": name,
            "Flags": _flags(font),
            "FontBBox": [round(x0 * 1000), round(y0 * 1000), round(x1 * 1000), round(y1 * 1000)],
            "ItalicAngle": italic_angle,
            "Ascent": round(font.ascender * 1000),
            "Descent": round(font.descender * 1000),
            "CapHeight": round(cap_height),
            "StemV": 80,
            file_key: objects[program_xref],
        }
    )
    cid_font: dict[str, object] = {
        "Type": Name("Font"),
        "Subtype": Name("CIDFontType0" if cff else "CIDFontType2"),
        "BaseFont": name,
        "CIDSystemInfo": {"Registry": "Adobe", "Ordering": "Identity", "Supplement": 0},
        "FontDescriptor": objects[descriptor],
        "W": [],
    }
    if not cff:
        cid_font["CIDToGIDMap"] = Name("Identity")
    descendant = objects.add(cid_font)
    to_unicode = objects.add_stream(_CMAP_HEAD + _CMAP_TAIL)
    return objects.add(
        {
            "Type": Name("Font"),
            "Subtype": Name("Type0"),
            "BaseFont": name,
            "Encoding": Name("Identity-H"),
            "DescendantFonts": [objects[descendant]],
            "ToUnicode": objects[to_unicode],
        }
    )


def _type0_parts(document: Document, xref: int) -> tuple[Dictionary, Stream] | None:
    """Return the CIDFont and ToUnicode stream of the Type0 font ``xref``, if it still is one."""
    objects = document.objects
    font = objects.get(xref) if xref in objects else None
    if not isinstance(font, Dictionary) or font.get("Subtype") != "Type0" or font.get("Encoding") != "Identity-H":
        return None
    descendants = font.get("DescendantFonts")
    descendant = descendants[0] if isinstance(descendants, Array) and len(descendants) == 1 else None
    to_unicode = font.get("ToUnicode")
    if not isinstance(descendant, Dictionary) or not isinstance(to_unicode, Stream):
        return None
    return descendant, to_unicode


def embedded_font(document: Document, face: Font, cache: dict[bytes, int]) -> int:
    """Return the Type0 font of ``face`` in ``document``, written on first use.

    ``cache`` maps the digests of programs to the fonts written for them; an
    entry that an undo removed or another write replaced is written again.
    """
    key = face._key()  # noqa: SLF001 - the package's own face.
    xref = cache.get(key)
    if xref is not None and _type0_parts(document, xref) is not None:
        base = document.objects[xref].get("BaseFont")
        name = face._font.postscript_name  # noqa: SLF001
        text = str(base) if isinstance(base, Name) else ""
        # A save renames the font with a subset tag.
        if text == name or (_TAG.match(text) is not None and text[7:] == name):
            return xref
    xref = _type0(document, face)
    cache[key] = xref
    return xref


def _widths(descendant: Dictionary, glyph_count: int) -> dict[int, float]:
    """Return the widths that a CIDFont's ``W`` array records, in its ``c [w1 w2 ...]`` form."""
    widths: dict[int, float] = {}
    entries = descendant.get("W")
    if not isinstance(entries, Array):
        return widths
    items = list(entries)
    for first, group in zip(items[::2], items[1::2], strict=False):
        if not isinstance(first, int) or not isinstance(group, Array):
            continue
        for offset, width in enumerate(group):
            glyph = first + offset
            if 0 <= glyph < glyph_count and isinstance(width, (int, float)):
                widths[glyph] = float(width)
    return widths


def _mapped(stream: Stream, glyph_count: int) -> dict[int, str]:
    """Return the texts that a ToUnicode stream's ``bfchar`` entries map glyphs to."""
    mapped: dict[int, str] = {}
    data = stream.read()
    for block in _BFCHAR_BLOCKS.findall(data):
        for code, text in _BFCHAR.findall(block):
            glyph = int(code, 16)
            if glyph < glyph_count and len(text) % 4 == 0:
                mapped[glyph] = bytes.fromhex(text.decode()).decode("utf-16-be", "surrogatepass")
    return mapped


def _w_array(widths: dict[int, float]) -> list[object]:
    """Return ``W`` entries that give each run of consecutive glyphs one array."""
    entries: list[object] = []
    run: list[float] = []
    start = previous = -2
    for glyph in sorted(widths):
        if glyph != previous + 1:
            if run:
                entries += [start, run]
            start, run = glyph, []
        width = widths[glyph]
        run.append(int(width) if width == int(width) else round(width, 3))
        previous = glyph
    if run:
        entries += [start, run]
    return entries


def _cmap(mapped: dict[int, str]) -> bytes:
    lines = [_CMAP_HEAD]
    codes = sorted(mapped)
    for at in range(0, len(codes), _BFCHAR_BLOCK):
        block = codes[at : at + _BFCHAR_BLOCK]
        lines.append(b"%d beginbfchar\n" % len(block))
        lines.extend(b"<%04X> <%s>\n" % (code, _utf16(mapped[code])) for code in block)
        lines.append(b"endbfchar\n")
    lines.append(_CMAP_TAIL)
    return b"".join(lines)


def add_glyphs(document: Document, xref: int, face: Font, glyphs: Sequence[tuple[int, str]]) -> dict[int, str]:
    """Record the widths and texts of ``glyphs`` in the Type0 font ``xref`` and return the texts that the CMap maps.

    Each glyph maps to its text in the ToUnicode CMap unless the CMap
    already maps it to other text, or its text is empty or too long for a
    CMap entry. The result holds the CMap's text of each glyph that it
    maps; the content draws an occurrence with other text inside
    ``/ActualText``. Writes only what changed, in the caller's transaction.
    """
    parts = _type0_parts(document, xref)
    if parts is None:
        msg = f"object {xref} is no longer the font that pyropdf wrote"
        raise PdfError(msg)
    descendant, stream = parts
    font = face._font  # noqa: SLF001 - the package's own face.
    count = font.glyph_count
    widths = _widths(descendant, count)
    mapped = _mapped(stream, count)
    new = sorted({glyph for glyph, _ in glyphs if glyph not in widths})
    widths.update(zip(new, font.glyph_widths(new), strict=True))
    added = False
    for glyph, text in glyphs:
        if glyph in mapped or not text or len(text.encode("utf-16-be", "surrogatepass")) > 2 * _MAX_MAPPED_UNITS:
            continue
        mapped[glyph] = text
        added = True
    if new:
        descendant["W"] = _w_array(widths)
        # A save subsets the program to the glyphs of ``W``.
        _core.mark_font_subset(document._core(), xref)  # noqa: SLF001 - the package's own core.
    if added:
        stream.write(_cmap(mapped))
    return {glyph: mapped[glyph] for glyph, _ in glyphs if glyph in mapped}


# The package re-exports these names; reprs, pickles, and documentation name them.
for _public in (FontInfo, FontProgram, SubsetFont, Font):
    _public.__module__ = "pyropdf"
