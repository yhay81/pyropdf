"""The options that the text views share, and the warning of unmapped glyphs."""

from __future__ import annotations

import warnings
from typing import Literal, TypeAlias

from pyropdf._errors import PyropdfWarning
from pyropdf._limits import _validate_optional_positive_int

Unmapped: TypeAlias = Literal["skip", "replacement", "glyph_id"]
Unmapped.__doc__ = """What text extraction makes of a glyph without Unicode: ``"skip"`` leaves it
out with a :class:`PyropdfWarning`, ``"replacement"`` gives U+FFFD, and
``"glyph_id"`` gives the character whose code point is the glyph ID."""

_UNMAPPED_CODES = {"skip": 0, "replacement": 1, "glyph_id": 2}

#: Glyphs that one page's text collects by default (``text_glyph_count``).
_DEFAULT_MAX_TEXT_GLYPHS = 262_144
#: UTF-8 bytes of collected and returned text by default (``text_size``).
_DEFAULT_MAX_TEXT_SIZE = 64 * 1024 * 1024

# The core's text options: (clip, keep clipped glyphs, unmapped code, expand
# ligatures, normalize whitespace, dehyphenate).
_TextOptions: TypeAlias = tuple[tuple[float, float, float, float] | None, bool, int, bool, bool, bool]


def text_options(  # noqa: PLR0913 - the shared options of the text views.
    *,
    clip: tuple[float, float, float, float] | None,
    clipped: bool,
    ligatures: bool,
    whitespace: bool,
    dehyphenate: bool,
    unmapped: Unmapped,
    max_glyphs: int | None,
    max_size: int | None,
) -> _TextOptions:
    """Validate the options of the text views, with ``clip`` already validated."""
    for name, value in (
        ("clipped", clipped),
        ("ligatures", ligatures),
        ("whitespace", whitespace),
        ("dehyphenate", dehyphenate),
    ):
        if not isinstance(value, bool):
            msg = f"{name} must be True or False: {value!r}"
            raise TypeError(msg)
    code = _UNMAPPED_CODES.get(unmapped) if isinstance(unmapped, str) else None
    if code is None:
        msg = f"unmapped must be 'skip', 'replacement', or 'glyph_id': {unmapped!r}"
        raise ValueError(msg)
    _validate_optional_positive_int("max_glyphs", max_glyphs)
    _validate_optional_positive_int("max_size", max_size)
    return (clip, clipped, code, not ligatures, not whitespace, dehyphenate)


def _warn_skipped(skipped: int) -> None:
    """Warn once per call that glyphs without a Unicode mapping were left out."""
    if skipped:
        msg = (
            f"text extraction skipped {skipped} glyphs with no Unicode mapping "
            "(for example a Type 3 font without a ToUnicode CMap); "
            "pass unmapped='replacement' or 'glyph_id' to keep them"
        )
        warnings.warn(msg, PyropdfWarning, stacklevel=3)


__all__ = ["Unmapped"]
