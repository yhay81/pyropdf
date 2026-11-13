"""Fallback fonts: installed CJK faces and the document settings that select them."""

from __future__ import annotations

import functools
import importlib
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pyropdf import _core
from pyropdf._core import _FontFile
from pyropdf._fonts import _DEFAULT_MAX_FONT_INPUT_SIZE, Font

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

    from pyropdf._document import Document


@dataclass(frozen=True, slots=True, kw_only=True)
class FallbackFont:
    """The faces that render the CJK fonts of one language that a document does not embed.

    Rendering draws a font whose name marks it as serif, such as a Mincho,
    Song, Ming, or Batang face, in ``serif``, and any other in ``sans``;
    when one of them is ``None``, the other draws both. A variable face
    renders at its default instance, or the instance that ``weight`` selected
    when it loaded. :attr:`Document.fallback_fonts` maps each language to one.
    """

    sans: Font | None = None
    """The sans (gothic) face, an OpenType :class:`Font`, or ``None``."""
    serif: Font | None = None
    """The serif (Mincho) face, an OpenType :class:`Font`, or ``None``."""

    def __post_init__(self) -> None:
        """Refuse a face that is not an OpenType :class:`Font`, and a record without one."""
        for kind, face in (("sans", self.sans), ("serif", self.serif)):
            if face is None:
                continue
            if not isinstance(face, Font):
                msg = f"{kind} must be a Font or None: {face!r}"
                raise TypeError(msg)
            if face._font.standard_name is not None:  # noqa: SLF001 - the package's own face.
                msg = f"{kind} must be an OpenType face; a Standard 14 font has no CJK glyphs: {face.name!r}"
                raise ValueError(msg)
        if self.sans is None and self.serif is None:
            msg = "a FallbackFont needs a sans face, a serif face, or both"
            raise ValueError(msg)


#: The languages of the Adobe-Japan1, Korea1, GB1, and CNS1 character
#: collections, which name the fallback fonts.
_FALLBACK_LANGUAGES = ("ja", "ko", "zh-CN", "zh-TW")
#: pylopdf's font packages of each language's faces, and the extras that
#: install them (DESIGN.md section 14).
_FONT_PACKAGES = (
    ("ja", "jp", "pylopdf_fonts_jp"),
    ("ko", "ko", "pylopdf_fonts_ko"),
    ("zh-CN", "zh-cn", "pylopdf_fonts_zh_cn"),
    ("zh-TW", "zh-tw", "pylopdf_fonts_zh_tw"),
)


@dataclass(frozen=True, slots=True)
class _FontPackage:
    """An installed font package: the language of its faces, and their files."""

    language: str
    sans: Path
    serif: Path


def _import_font_package(name: str) -> object | None:
    """Import a font package, or return ``None`` when it is not installed."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name == name:
            return None
        raise


@functools.cache
def _font_packages() -> tuple[_FontPackage, ...]:
    """Return the installed font packages, found once per process."""
    found = []
    for language, _, name in _FONT_PACKAGES:
        module = cast("Any", _import_font_package(name))
        if module is not None:
            found.append(_FontPackage(language, Path(module.sans_path()), Path(module.serif_path())))
    return tuple(found)


@functools.cache
def _package_files(package: _FontPackage) -> tuple[_FontFile, _FontFile]:
    """Return the files of a package's sans and serif faces, which every document shares.

    Rendering reads a file the first time that a page needs its face, so a
    document without CJK fonts that it does not embed reads none of them.
    """
    return (
        _FontFile(str(package.sans), _DEFAULT_MAX_FONT_INPUT_SIZE),
        _FontFile(str(package.serif), _DEFAULT_MAX_FONT_INPUT_SIZE),
    )


@functools.cache
def _package_font(package: _FontPackage) -> FallbackFont:
    """Return a package's faces as fonts, loaded once per process."""
    return FallbackFont(sans=Font.from_file(package.sans), serif=Font.from_file(package.serif))


def _fallback_language(language: object) -> str:
    """Return ``language`` when it names a fallback font language, else refuse it."""
    if not isinstance(language, str):
        msg = f"a fallback font language is a str: {language!r}"
        raise TypeError(msg)
    if language not in _FALLBACK_LANGUAGES:
        msg = f"a fallback font language is 'ja', 'ko', 'zh-CN', or 'zh-TW': {language!r}"
        raise ValueError(msg)
    return language


def _installed_state() -> dict[str, FallbackFont | _FontPackage]:
    """Return the installed font packages by language, each standing for its faces."""
    return {package.language: package for package in _font_packages()}


def _fallback_state(document: Document) -> dict[str, FallbackFont | _FontPackage]:
    """Return the document's fallback fonts, an installed package standing for its faces."""
    state = document._fallback  # noqa: SLF001 - the package's own document.
    return _installed_state() if state is None else state


def _set_fallback(document: Document, state: dict[str, FallbackFont | _FontPackage] | None) -> None:
    """Render ``document`` with the fonts of ``state``, ``None`` for the installed font packages, and keep it."""
    faces: list[tuple[str, bool, _core._Font | _FontFile]] = []
    for language, value in (_installed_state() if state is None else state).items():
        pair = _package_files(value) if isinstance(value, _FontPackage) else (value.sans, value.serif)
        for serif, face in zip((False, True), pair, strict=True):
            if isinstance(face, Font):
                faces.append((language, serif, face._font))  # noqa: SLF001 - the package's own face.
            elif face is not None:
                faces.append((language, serif, face))
    _core.set_fallback_faces(document._core(), faces)  # noqa: SLF001 - the package's own document.
    document._fallback = state  # noqa: SLF001


def _replace_fallback_fonts(document: Document, fonts: Mapping[str, FallbackFont]) -> None:
    """Render ``document`` with ``fonts`` alone, checking all of them before any change."""
    if not isinstance(fonts, Mapping):
        msg = f"fallback fonts are a mapping from language to FallbackFont: {type(fonts).__name__}"
        raise TypeError(msg)
    state: dict[str, FallbackFont | _FontPackage] = {}
    for language, value in fonts.items():
        if not isinstance(value, FallbackFont):
            msg = f"a fallback font is a FallbackFont: {value!r}"
            raise TypeError(msg)
        state[_fallback_language(language)] = value
    _set_fallback(document, state)


class FallbackFonts(MutableMapping[str, FallbackFont]):
    """The fonts that render the CJK fonts that a document does not embed, from :attr:`Document.fallback_fonts`.

    The keys are ``"ja"``, ``"ko"``, ``"zh-CN"``, and ``"zh-TW"``, the
    languages of the Adobe-Japan1, Korea1, GB1, and CNS1 character
    collections, and each value is a :class:`FallbackFont`. Until it is
    edited, the mapping holds the faces of the installed font extras,
    ``pyropdf[jp]``, ``[ko]``, ``[zh-cn]``, and ``[zh-tw]``. Assigning or
    deleting a language changes only that language, and the next rendering
    uses the change.
    """

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Wrap a document; use :attr:`Document.fallback_fonts`."""
        self._document = document

    def _state(self) -> dict[str, FallbackFont | _FontPackage]:
        self._document._ensure_open()  # noqa: SLF001 - the package's own document.
        return _fallback_state(self._document)

    def __getitem__(self, language: str) -> FallbackFont:
        """Return the faces that render ``language``, loading an installed extra's once per process."""
        value = self._state()[_fallback_language(language)]
        return _package_font(value) if isinstance(value, _FontPackage) else value

    def __setitem__(self, language: str, value: FallbackFont) -> None:
        """Render ``language`` with ``value``'s faces."""
        key = _fallback_language(language)
        if not isinstance(value, FallbackFont):
            msg = f"a fallback font is a FallbackFont: {value!r}"
            raise TypeError(msg)
        state = dict(self._state())
        state[key] = value
        _set_fallback(self._document, state)

    def __delitem__(self, language: str) -> None:
        """Render ``language`` without a fallback font, as a Standard 14 font draws it: without CJK glyphs."""
        state = dict(self._state())
        del state[_fallback_language(language)]
        _set_fallback(self._document, state)

    def __contains__(self, language: object) -> bool:
        """Return whether ``language`` has a fallback font."""
        return isinstance(language, str) and language in self._state()

    def __iter__(self) -> Iterator[str]:
        """Iterate over the languages that have a fallback font."""
        return iter(list(self._state()))

    def __len__(self) -> int:
        """Return the number of languages that have a fallback font."""
        return len(self._state())

    def __repr__(self) -> str:
        """Return ``FallbackFonts([...])`` with the languages that have a fallback font."""
        return f"FallbackFonts({list(self._state())!r})"


for _public in (FallbackFont, FallbackFonts):
    _public.__module__ = "pyropdf"
