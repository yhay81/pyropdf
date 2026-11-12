"""Document-level drawn-resource caching and page content/resource assembly."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pyropdf import _core
from pyropdf._canvas._text import _font_value
from pyropdf._objects import Name, Stream

if TYPE_CHECKING:
    from pyropdf._document import Document
    from pyropdf._fonts import Font
    from pyropdf._objects import Dictionary, Objects
    from pyropdf._page import Page

#: Operators that change the graphics state that later content inherits.
_STATE_OPERATORS = frozenset(
    {"cm", "gs", "W", "W*", "w", "J", "j", "M", "d", "ri", "i", "Tc", "Tw", "Tz", "TL", "Tf", "Tr", "Ts"}
)


class _Images:
    """The images and pages that a document's canvases wrote, keyed by their content."""

    __slots__ = ("colour", "faces", "fonts", "forms", "images", "shadings")

    def __init__(self) -> None:
        self.fonts: dict[bytes, int] = {}
        # The Type 3 fonts of the faces that text drew in colour, by program digest.
        self.colour: dict[bytes, list[int]] = {}
        # The shading objects that those fonts' COLR glyphs share.
        self.shadings = _core._Shadings()  # noqa: SLF001
        # The faces of the Type0 fonts that free text was drawn in, by font object.
        self.faces: dict[int, Font] = {}
        self.images: dict[bytes, int] = {}
        # The Forms of placed pages, with the source objects each depends on.
        self.forms = _core._PageForms()  # noqa: SLF001


def _gstate_dict(key: tuple[float | None, float | None, str | None]) -> dict[str, object]:
    fill_alpha, stroke_alpha, blend = key
    entries: dict[str, object] = {"Type": Name("ExtGState")}
    if fill_alpha is not None:
        entries["ca"] = fill_alpha
    if stroke_alpha is not None:
        entries["CA"] = stroke_alpha
    if blend is not None:
        entries["BM"] = Name(blend)
    return entries


def _resources_dict(  # noqa: PLR0913, PLR0917 - the store's objects, and each kind of resource.
    objects: Objects,
    xobjects: dict[str, int],
    gstates: dict[tuple[float | None, float | None, str | None], str],
    fonts: dict[str, str | int],
    properties: dict[str, int],
    patterns: dict[str, int] | None = None,
    masks: dict[int, str] | None = None,
) -> dict[str, dict[str, object]]:
    """Build the resource subdictionaries for a canvas's Form or page merge."""
    resources: dict[str, dict[str, object]] = {}
    if xobjects:
        resources["XObject"] = {name: objects[xref] for name, xref in xobjects.items()}
    if gstates or masks:
        resources["ExtGState"] = {
            **{name: _gstate_dict(key) for key, name in gstates.items()},
            **{name: objects[xref] for xref, name in (masks or {}).items()},
        }
    if patterns:
        resources["Pattern"] = {name: objects[xref] for name, xref in patterns.items()}
    if fonts:
        resources["Font"] = {name: _font_value(objects, font) for name, font in fonts.items()}
    if properties:
        resources["Properties"] = {name: objects[xref] for name, xref in properties.items()}
    return resources


def _page_space(page: Page) -> tuple[int, tuple[float, float, float, float], float]:
    space = _core.page_space(page.document._core(), page.xref, page._generation)  # noqa: SLF001
    if space is None:
        raise page._stale()  # noqa: SLF001
    return space


def _cache(document: Document) -> _Images:
    cache = document._drawn  # noqa: SLF001
    if cache is None:
        cache = document._drawn = _Images()  # noqa: SLF001
    return cache


def _still_image(document: Document, xref: int, size: int) -> bool:
    """Return whether ``xref`` is still the image object that a canvas stored from ``size`` bytes."""
    stream = document.objects.get(xref) if xref in document.objects else None
    if not isinstance(stream, Stream) or stream.get("Subtype") != "Image":
        return False
    if stream.get("Filter") in ("DCTDecode", "JPXDecode"):
        return len(stream.read(decode=False)) == size
    return True


def _contents(page: Page) -> list[Stream]:
    """Return the page's content streams, in order."""
    contents = page.obj.get("Contents")
    if contents is None:
        return []
    if isinstance(contents, Stream):
        return [contents]
    return [item for item in contents if isinstance(item, Stream)]


def _isolated(page: Page) -> bool:
    """Return whether the page's content leaves the graphics state as it found it."""
    try:
        operations = page.operations
    except Exception:  # noqa: BLE001 - content that cannot be read is wrapped.
        return False
    depth = 0
    for operation in operations:
        operator = getattr(operation, "operator", None)
        if operator == "q":
            depth += 1
        elif operator == "Q":
            depth -= 1
            if depth < 0:
                return False
        elif depth == 0 and operator in _STATE_OPERATORS:
            return False
    return depth == 0


def _merge_resources(  # noqa: PLR0913, PLR0917 - the page, and each kind of resource.
    page: Page,
    xobjects: dict[str, int],
    gstates: dict[tuple[float | None, float | None, str | None], str],
    fonts: dict[str, str | int],
    properties: dict[str, int],
    patterns: dict[str, int] | None = None,
    masks: dict[int, str] | None = None,
) -> dict[str, str]:
    """Add the canvas's resources to the page's own, renaming any that collide.

    The page gets its own direct ``/Resources`` when it inherits or shares
    them, so no other page changes. Returns the renamed names.
    """
    added = _resources_dict(page.document.objects, xobjects, gstates, fonts, properties, patterns, masks)
    if not added:
        return {}
    own = page.obj.get("Resources")
    if own is None or own.is_indirect:
        inherited = own if own is not None else _inherited_resources(page)
        page.obj["Resources"] = {} if inherited is None else dict(cast("Dictionary", inherited).items())
    resources = page.obj["Resources"]
    renamed: dict[str, str] = {}
    for key, entries in added.items():
        sub = resources.get(key)
        if sub is None:
            resources[key] = {}
        elif sub.is_indirect:
            resources[key] = dict(sub.items())
        sub = resources[key]
        for name, value in entries.items():
            unique = name
            serial = 0
            while unique in sub:
                serial += 1
                unique = f"{name}_{serial}"
            if unique != name:
                renamed[name] = unique
            sub[unique] = value
    return renamed


def _inherited_resources(page: Page) -> object | None:
    """Return the ``/Resources`` that the page inherits from its page tree."""
    node = page.obj.get("Parent")
    for _ in range(64):
        if node is None:
            return None
        if "Resources" in node:
            return node["Resources"]
        node = node.get("Parent")
    return None
