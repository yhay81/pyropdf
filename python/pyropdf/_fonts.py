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
import re
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pyropdf import _core
from pyropdf._errors import LimitError
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
    for _ in range(_MAX_TREE_DEPTH):
        if not isinstance(node, Dictionary):
            return None
        if "Resources" in node:
            return node["Resources"]
        node = node.get("Parent")
    return None


#: The fonts that a page walk found, by identity: the dictionary, its resource names, and its forms.
_Found = dict[object, tuple[Dictionary, list[str], list[int]]]


def _collect(found: _Found, fonts: object, form: int) -> None:
    """Record the fonts of the ``/Font`` resources ``fonts`` of ``form``, 0 for the page."""
    if not isinstance(fonts, Dictionary):
        return
    for key in fonts:
        font = fonts[key]
        if not isinstance(font, Dictionary):
            continue
        # A direct font dictionary is its own entry.
        identity = font.xref if font.xref is not None else ("direct", form, key)
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
    pending: deque[tuple[object, int]] = deque([(_page_resources(page), 0)])
    seen: set[int] = set()
    while pending:
        resources, form = pending.popleft()
        if not isinstance(resources, Dictionary):
            continue
        _collect(found, resources.get("Font"), form)
        xobjects = resources.get("XObject")
        forms = [xobjects[key] for key in xobjects] if isinstance(xobjects, Dictionary) else []
        for xobject in forms:
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


# The package re-exports these names; reprs, pickles, and documentation name them.
for _public in (FontInfo, FontProgram, SubsetFont):
    _public.__module__ = "pyropdf"
