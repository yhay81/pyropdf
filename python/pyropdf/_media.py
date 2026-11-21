"""Multimedia annotation specifications and their PDF object relationships."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

from pyropdf import _core, typed
from pyropdf._errors import LimitError, PdfError
from pyropdf._objects import Array, Dictionary, Name

if TYPE_CHECKING:
    from pyropdf._check import StructuralDefect
    from pyropdf._document import Document
    from pyropdf._geometry import Rect
    from pyropdf._page import Page

_MAX_ITEMS = 4096
_MAX_TEXT = 1 << 20
_MAX_ASSET = 64 << 20


def _text(value: str) -> None:
    if not isinstance(value, str) or not value:
        msg = "media names must be non-empty strings"
        raise ValueError(msg)
    if len(value) > _MAX_TEXT or len(value.encode("utf-8")) > _MAX_TEXT:
        msg = "media name exceeds the annotation text limit"
        code = "annotation_input_size"
        raise LimitError(code, msg)


def _data(value: bytes) -> None:
    if not isinstance(value, bytes):
        msg = "media data must be bytes"
        raise TypeError(msg)
    if len(value) > _MAX_ASSET:
        msg = "embedded media exceeds 64 MiB"
        code = "embedded_file_size"
        raise LimitError(code, msg)


def _bounded(items: Array | tuple[object, ...]) -> None:
    if len(items) > _MAX_ITEMS:
        msg = "media relationships exceed 4096 entries"
        code = "annotation_count"
        raise LimitError(code, msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class MediaAsset:
    """An embedded media file shared by its file specification and media instances."""

    filename: str
    content_type: str
    data: bytes

    def __post_init__(self) -> None:
        """Check the filename, ASCII media type and payload size."""
        _text(self.filename)
        _text(self.content_type)
        if (
            not self.content_type.isascii()
            or "/" not in self.content_type
            or any(c.isspace() for c in self.content_type)
        ):
            msg = "content_type must be an ASCII media type"
            raise ValueError(msg)
        _data(self.data)


class MediaKind(enum.StrEnum):
    """Base-standard RichMedia configuration and instance subtypes."""

    THREE_D = "3D"
    SOUND = "Sound"
    VIDEO = "Video"


class ModelFormat(enum.StrEnum):
    """Embedded base-standard 3D stream formats."""

    U3D = "U3D"
    PRC = "PRC"


Measure: TypeAlias = (
    typed.ThreeDMeasure3DC
    | typed.ThreeDMeasureAD3
    | typed.ThreeDMeasureLD3
    | typed.ThreeDMeasurePD3
    | typed.ThreeDMeasureRD3
)
_MEASURES: dict[type[typed.DictionaryView], str] = {
    typed.ThreeDMeasure3DC: "3DC",
    typed.ThreeDMeasureAD3: "AD3",
    typed.ThreeDMeasureLD3: "LD3",
    typed.ThreeDMeasurePD3: "PD3",
    typed.ThreeDMeasureRD3: "RD3",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelView:
    """A named 3D view with optional links to existing 3D measurements."""

    name: str
    measurements: tuple[Measure, ...] = ()

    def __post_init__(self) -> None:
        """Validate the name and the measurement dictionary identities."""
        _text(self.name)
        if not isinstance(self.measurements, tuple):
            msg = "measurements must be a tuple"
            raise TypeError(msg)
        _bounded(self.measurements)
        for measure in self.measurements:
            _measurement(measure)


def _measurement(measure: Measure) -> Dictionary:
    if type(measure) not in _MEASURES or not measure.obj.is_indirect or measure.Subtype != _MEASURES[type(measure)]:
        msg = "expected an indirect 3D measurement dictionary of the declared subtype"
        raise ValueError(msg)
    for key, _, required, _ in _core.schema_object(f"3DMeasure{measure.Subtype}") or ():
        if required == "TRUE":
            measure[key]
    return measure.obj


@dataclass(frozen=True, slots=True, kw_only=True)
class Screen:
    """A Screen annotation with an optional media clip and its rendition action."""

    rect: Rect
    asset: MediaAsset | None = None

    def __post_init__(self) -> None:
        """Validate the display rectangle and optional asset."""
        _rect(self)
        if self.asset is not None and not isinstance(self.asset, MediaAsset):
            msg = "asset must be a MediaAsset"
            raise TypeError(msg)


def _rect(spec: Screen | ThreeD | RichMedia | ProjectionAnnotation) -> None:
    from pyropdf._threads import _display_rect  # noqa: PLC0415

    object.__setattr__(spec, "rect", _display_rect(spec.rect))


def _views(views: tuple[ModelView, ...]) -> None:
    if not isinstance(views, tuple):
        msg = "views must be a tuple"
        raise TypeError(msg)
    _bounded(views)
    names: set[str] = set()
    size = count = 0
    for view in views:
        if not isinstance(view, ModelView) or view.name in names:
            msg = "views must be ModelView values with distinct names"
            raise ValueError(msg)
        names.add(view.name)
        size += len(view.name.encode("utf-8"))
        count += len(view.measurements) + 1
        if size > _MAX_TEXT or count > _MAX_ITEMS:
            msg = "view names or measurement links exceed the annotation input limit"
            code = "annotation_input_size"
            raise LimitError(code, msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class ThreeD:
    """A 3D annotation whose stream holds an opaque U3D or PRC asset.

    Each named view is stored once. Supply existing measurement views to
    share the same geometry with a projection annotation. Viewers handle
    3D rendering. pyropdf preserves the model bytes across edits.
    """

    rect: Rect
    format: ModelFormat
    data: bytes
    views: tuple[ModelView, ...] = ()

    def __post_init__(self) -> None:
        """Validate the rectangle, format, data and views."""
        _rect(self)
        _data(self.data)
        object.__setattr__(self, "format", ModelFormat(self.format))
        _views(self.views)


@dataclass(frozen=True, slots=True, kw_only=True)
class RichMedia:
    """A RichMedia annotation with a named asset, configuration, instance and optional 3D views.

    The asset name tree and instance reference the same file specification.
    Use the annotation's live Arlington view to inspect its configurations,
    rename one, and read or replace its embedded stream. A viewer can use
    the stored data for playback.
    """

    rect: Rect
    kind: MediaKind
    asset: MediaAsset
    views: tuple[ModelView, ...] = ()

    def __post_init__(self) -> None:
        """Validate the rectangle, instance kind, asset and views."""
        _rect(self)
        object.__setattr__(self, "kind", MediaKind(self.kind))
        if not isinstance(self.asset, MediaAsset):
            msg = "asset must be a MediaAsset"
            raise TypeError(msg)
        _views(self.views)
        if self.views and self.kind != MediaKind.THREE_D:
            msg = "named 3D views require a 3D media instance"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectionAnnotation:
    """A projection annotation that links back to a 3D measurement."""

    rect: Rect
    measurement: Measure

    def __post_init__(self) -> None:
        """Validate the rectangle and indirect measurement dictionary."""
        _rect(self)
        _measurement(self.measurement)


MediaSpecification: TypeAlias = Screen | ThreeD | RichMedia | ProjectionAnnotation
MediaAnnotation: TypeAlias = (
    typed.AnnotScreen
    | typed.Annot3D
    | typed.AnnotRichMedia
    | typed.AnnotProjection
    | typed.AnnotMovie
    | typed.AnnotSound
)

_MEDIA_TYPES = {
    "Screen": typed.AnnotScreen,
    "3D": typed.Annot3D,
    "RichMedia": typed.AnnotRichMedia,
    "Projection": typed.AnnotProjection,
    "Movie": typed.AnnotMovie,
    "Sound": typed.AnnotSound,
}


def media_annotation(obj: Dictionary) -> MediaAnnotation | None:
    """Return the Arlington view matching this media annotation's subtype."""
    kind = _MEDIA_TYPES.get(obj.get("Subtype"))
    return None if kind is None else kind(obj)


def _indirect(doc: Document, entries: dict[str, object]) -> Dictionary:
    return doc.objects[doc.objects.add(entries)]


def _filespec(doc: Document, asset: MediaAsset) -> Dictionary:
    stream = doc.objects.add_stream(
        asset.data, dictionary={"Type": Name("EmbeddedFile"), "Subtype": Name(asset.content_type)}
    )
    return _indirect(doc, {"Type": Name("Filespec"), "F": asset.filename, "EF": {"F": doc.objects[stream]}})


def _write_views(doc: Document, views: tuple[ModelView, ...]) -> list[Dictionary]:
    result = []
    for view in views:
        entries: dict[str, object] = {"Type": Name("3DView"), "XN": view.name, "IN": view.name}
        if view.measurements:
            entries["MA"] = [_measurement(m) for m in view.measurements]
        result.append(_indirect(doc, entries))
    return result


def _write_rich_content(doc: Document, spec: RichMedia) -> dict[str, object]:
    asset = _filespec(doc, spec.asset)
    instance = _indirect(doc, {"Type": Name("RichMediaInstance"), "Subtype": Name(spec.kind), "Asset": asset})
    config = _indirect(
        doc, {"Type": Name("RichMediaConfiguration"), "Subtype": Name(spec.kind), "Instances": [instance]}
    )
    content: dict[str, object] = {"Assets": {"Names": [spec.asset.filename, asset]}, "Configurations": [config]}
    if spec.views:
        content["Views"] = _write_views(doc, spec.views)
    return content


def _write_media(doc: Document, annot: Dictionary, spec: MediaSpecification) -> None:
    if isinstance(spec, Screen) and spec.asset is not None:
        clip = {
            "Type": Name("MediaClip"),
            "S": Name("MCD"),
            "CT": spec.asset.content_type,
            "D": _filespec(doc, spec.asset),
        }
        rendition = _indirect(doc, {"Type": Name("Rendition"), "S": Name("MR"), "C": clip})
        annot["A"] = {"S": Name("Rendition"), "OP": 0, "AN": annot, "R": rendition}
    elif isinstance(spec, ThreeD):
        stream: dict[str, object] = {"Type": Name("3D"), "Subtype": Name(spec.format)}
        if spec.views:
            stream["VA"] = _write_views(doc, spec.views)
            stream["DV"] = Name("F")
        annot["3DD"] = doc.objects[doc.objects.add_stream(spec.data, dictionary=stream)]
    elif isinstance(spec, RichMedia):
        annot["RichMediaContent"] = _write_rich_content(doc, spec)
    elif isinstance(spec, ProjectionAnnotation):
        measure = _measurement(spec.measurement)
        if measure.get("S") is not None:
            msg = "the measurement already has a projection annotation"
            raise ValueError(msg)
        annot["ExData"] = {"Type": Name("ExData"), "Subtype": Name("3DM"), "M3DREF": measure}
        measure["S"] = annot


def _base_or_extension(doc: Document, level: int) -> tuple[int, int]:
    """Use an existing Adobe PDF 1.7 extension declaration or the base PDF 2.0 feature."""
    extensions = doc.catalog.get("Extensions")
    adobe = extensions.get("ADBE") if isinstance(extensions, Dictionary) else None
    if isinstance(adobe, Dictionary) and adobe.get("BaseVersion") == "1.7":
        declared = adobe.get("ExtensionLevel")
        if type(declared) is int and declared >= level:
            return (1, 7)
    return (2, 0)


def _require_media_version(doc: Document, minimum: tuple[int, int]) -> None:
    from pyropdf._measurement import _require_measurement_version  # noqa: PLC0415

    _require_measurement_version(doc, doc.catalog, minimum)


def finish_media_import(doc: Document, major: int, minor: int, first: int, *, contains_prc: bool) -> None:
    """Check the receiver's edition and the imported media graph before committing its new pages."""
    from pyropdf._annotations import _version  # noqa: PLC0415

    def imported_defect() -> StructuralDefect | None:
        report = doc.check()
        if report.truncated:
            msg = "cannot verify imported media with a truncated structural report"
            raise PdfError(msg)
        return next(
            (d for d in report.defects if d.xref is not None and d.xref >= first and d.clause != "FileTrailer/Size"),
            None,
        )

    required = (major, minor)
    if contains_prc:
        # The pinned Arlington descriptor lists PRC together with U3D.
        # ISO 32000-1 required Adobe extension level 1 for this model format.
        _require_media_version(doc, _base_or_extension(doc, 1))
    defect = imported_defect()
    if required > _version(doc) and defect is not None:
        _require_media_version(doc, required)
        defect = imported_defect()
    if defect is not None:
        msg = f"importing the multimedia annotation would introduce a structural defect: {defect.clause}"
        raise PdfError(msg)


def add_media(page: Page, spec: MediaSpecification) -> tuple[int, int]:
    """Create the complete relationship graph and normal appearance in one transaction."""
    from pyropdf._threads import _display_to_pdf_rect  # noqa: PLC0415

    doc = page.document
    annotation_types: dict[type[MediaSpecification], tuple[str, tuple[int, int]]] = {
        Screen: ("Screen", (1, 5)),
        ThreeD: ("3D", (1, 6)),
        RichMedia: ("RichMedia", (2, 0)),
        ProjectionAnnotation: ("Projection", (2, 0)),
    }
    subtype, minimum = annotation_types[type(spec)]
    with doc.transaction("add multimedia annotation"):
        if isinstance(spec, (RichMedia, ProjectionAnnotation)):
            minimum = _base_or_extension(doc, 3)
        elif isinstance(spec, ThreeD):
            if any(view.measurements for view in spec.views):
                minimum = (2, 0)
            elif spec.format is ModelFormat.PRC:
                minimum = _base_or_extension(doc, 1)
        _require_media_version(doc, minimum)
        rect = _display_to_pdf_rect(doc, page, spec.rect)
        appearance = doc.objects.add_stream(
            b"",
            dictionary={
                "Type": Name("XObject"),
                "Subtype": Name("Form"),
                "BBox": [0, 0, rect[2] - rect[0], rect[3] - rect[1]],
                "Resources": {},
            },
        )
        annot = _indirect(
            doc,
            {
                "Type": Name("Annot"),
                "Subtype": Name(subtype),
                "P": page.obj,
                "Rect": rect,
                "AP": {"N": doc.objects[appearance]},
            },
        )
        _write_media(doc, annot, spec)
        annots = page.obj.get("Annots")
        if annots is None:
            page.obj["Annots"] = [annot]
        elif isinstance(annots, Array):
            annots.append(annot)
        else:
            msg = "page /Annots must be an array"
            raise ValueError(msg)
        # Use the ordinary listing's limits before publishing the annotation.
        _ = len(page.annotations)
        assert annot.xref is not None  # noqa: S101 - allocated above.
        assert annot.generation is not None  # noqa: S101 - allocated above.
        return annot.xref, annot.generation
