"""Multimedia annotation specifications and their PDF object relationships."""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any, TypeAlias

from pyropdf import _core, typed
from pyropdf._errors import LimitError, PdfError
from pyropdf._objects import Array, Dictionary, Name, Stream, String

if TYPE_CHECKING:
    from pyropdf._check import StructuralDefect, StructuralReport
    from pyropdf._document import Document
    from pyropdf._geometry import Rect
    from pyropdf._page import Page

_MAX_ITEMS = 4096
_MAX_TEXT = 1 << 20
_MAX_ASSET = 64 << 20
_AXES = 3


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


class ModelMeasurementType(enum.StrEnum):
    """Kinds of stored 3D measurements and markups (ISO 32000-2, Tables 326–331)."""

    POINT_COMMENT = "3DC"
    ANGLE = "AD3"
    DISTANCE = "LD3"
    PERPENDICULAR_DISTANCE = "PD3"
    RADIUS = "RD3"


_GEOMETRY_FIELDS = {
    "A1": "anchor",
    "TP": "text_position",
    "AP": "annotation_plane",
    "A2": "second_anchor",
    "D1": "first_direction",
    "D2": "second_direction",
    "TX": "text_x",
    "TY": "text_y",
    "A3": "third_anchor",
    "A4": "fourth_anchor",
    "V": "value",
    "U": "unit",
}


@cache
def _geometry_keys(subtype: str) -> dict[str, bool]:
    """Map geometry keys of one Arlington measurement descriptor to requiredness."""
    return {
        key: required == "TRUE"
        for key, _, required, _ in _core.schema_object(f"3DMeasure{subtype}") or ()
        if key in _GEOMETRY_FIELDS
    }


def _finite(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return -(1 << 63) <= value < 1 << 63
    return isinstance(value, float) and math.isfinite(value)


def _vector(value: object, size: int, key: str) -> None:
    if not isinstance(value, (tuple, Array)) or len(value) != size or not all(_finite(n) for n in value):
        msg = f"/{key} requires {size} finite numbers"
        raise ValueError(msg)
    if key in {"AP", "D1", "D2", "TX", "TY"} and not any(value):
        msg = f"/{key} requires a nonzero direction"
        raise ValueError(msg)


def _tuple_vector(value: object, size: int, key: str) -> None:
    if not isinstance(value, tuple):
        msg = f"/{key} must be a coordinate tuple"
        raise TypeError(msg)
    _vector(value, size, key)


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelMeasurement:
    """Geometry of one 3D measurement in model coordinates.

    Every kind has an anchor and text position. Dimensional kinds also
    take the annotation plane, second anchor, text Y direction and value.
    Distance, perpendicular distance and radius take a unit. Perpendicular
    distance adds the first direction. Angle adds both directions and text
    X. Radius takes text X and optionally the third and fourth anchors.

    Pass the specification in `ModelView.measurements` to allocate it in
    the model's document. Use `apply_to` on a live measurement to change
    its geometry while keeping its projection and other references.
    """

    kind: ModelMeasurementType
    anchor: tuple[float, float, float]
    text_position: tuple[float, float, float]
    annotation_plane: tuple[float, float, float] | None = None
    second_anchor: tuple[float, float, float] | None = None
    first_direction: tuple[float, float, float] | None = None
    second_direction: tuple[float, float, float] | None = None
    text_x: tuple[float, float, float] | None = None
    text_y: tuple[float, float, float] | None = None
    third_anchor: tuple[float, float, float] | None = None
    fourth_anchor: tuple[float, float, float] | None = None
    value: float | None = None
    unit: str | None = None

    def __post_init__(self) -> None:
        """Check required and permitted coordinates against this subtype's descriptor."""
        object.__setattr__(self, "kind", ModelMeasurementType(self.kind))
        keys = _geometry_keys(self.kind)
        for key, field in _GEOMETRY_FIELDS.items():
            value = getattr(self, field)
            if value is None:
                if keys.get(key):
                    msg = f"{self.kind} requires {field}"
                    raise ValueError(msg)
            elif key not in keys:
                msg = f"{field} is not a geometry field of {self.kind}"
                raise ValueError(msg)
            elif key == "U":
                if not isinstance(value, str) or isinstance(value, Name):
                    msg = "unit must be a PDF text string"
                    raise TypeError(msg)
                if value:
                    _text(value)
            elif key == "V":
                if not _finite(value):
                    msg = "measurement value must be finite"
                    raise ValueError(msg)
            else:
                _tuple_vector(value, 3, key)

    def _entries(self) -> dict[str, object]:
        return {key: value for key, field in _GEOMETRY_FIELDS.items() if (value := getattr(self, field)) is not None}

    @classmethod
    def from_view(cls, measurement: Measure) -> ModelMeasurement:
        """Read a stored 3D measurement as immutable geometry in model coordinates."""
        if type(measurement) not in _MEASURES:
            msg = "expected a typed 3D measurement"
            raise ValueError(msg)
        # Keep the geometry and its projection on the same version throughout this read.
        with measurement.obj._document.transaction():  # noqa: SLF001
            obj = _measurement(measurement)
            fields: dict[str, Any] = {}
            for key in _geometry_keys(measurement.Subtype):
                if key in obj:
                    value = measurement[key]
                    fields[_GEOMETRY_FIELDS[key]] = tuple(value.obj) if isinstance(value, typed.ArrayView) else value
            return cls(kind=ModelMeasurementType(measurement.Subtype), **fields)

    def apply_to(self, measurement: Measure) -> None:
        """Replace geometry of the same subtype in one undo step, keeping its object identity."""
        if type(measurement) not in _MEASURES or _MEASURES[type(measurement)] != self.kind:
            msg = "replacement geometry must match the measurement subtype"
            raise ValueError(msg)
        obj = measurement.obj
        with obj._document.transaction("edit 3D measurement geometry"):  # noqa: SLF001
            _measurement(measurement)
            entries = self._entries()
            for key in _geometry_keys(self.kind):
                if key in entries:
                    current = measurement[key] if key in obj else None
                    if isinstance(current, typed.ArrayView):
                        current = tuple(current.obj)
                    if current != entries[key]:
                        obj[key] = entries[key]
                elif key in obj:
                    del obj[key]


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelView:
    """A named 3D view, its camera-to-world matrix, scene settings and measurements.

    Lighting and render mode use the PDF subtype names. Background is a
    DeviceRGB triple in [0, 1]. Supply indirect Arlington node and cross
    section views to share their settings. Measurements may be new geometry
    specifications or existing indirect measurement views. For example,
    `camera_to_world=(1, 0, 0, 0, 1, 0, 0, 0, 1, 4, 5, 6)` sets an
    identity orientation with camera position (4, 5, 6). After creation,
    `annotation.media["3DD"].VA[0].LS.Subtype = "Day"` updates lighting.
    """

    name: str
    measurements: tuple[ModelMeasurement | Measure, ...] = ()
    camera_to_world: tuple[float, ...] | None = None
    lighting: str | None = None
    background: tuple[float, float, float] | None = None
    render_mode: str | None = None
    nodes: tuple[typed.ThreeDNode, ...] = ()
    cross_sections: tuple[typed.ThreeDCrossSection, ...] = ()

    def __post_init__(self) -> None:
        """Validate the name and the measurement dictionary identities."""
        _text(self.name)
        if isinstance(self.name, Name):
            msg = "a model view name must be a PDF text string"
            raise TypeError(msg)
        if self.camera_to_world is not None:
            _tuple_vector(self.camera_to_world, 12, "C2W")
        if self.background is not None:
            _tuple_vector(self.background, 3, "C")
            if not all(0 <= n <= 1 for n in self.background):
                msg = "3D background components must be in [0, 1]"
                raise ValueError(msg)
        for kind, value in (("3DLightingScheme", self.lighting), ("3DRenderMode", self.render_mode)):
            if value is not None and value not in _subtypes(kind):
                msg = f"invalid {kind} subtype {value!r}"
                raise ValueError(msg)
        self._check_relationships()

    def _check_relationships(self) -> None:
        for items in (self.measurements, self.nodes, self.cross_sections):
            if not isinstance(items, tuple):
                msg = "3D view relationships must be tuples"
                raise TypeError(msg)
            _bounded(items)
        for measure in self.measurements:
            if not isinstance(measure, ModelMeasurement):
                _measurement(measure)
        for node in self.nodes:
            _node(node)
        for section in self.cross_sections:
            _cross_section(section)


@cache
def _subtypes(kind: str) -> frozenset[str]:
    row = next(types for key, _, _, types in _core.schema_object(kind) or () if key == "Subtype")
    return frozenset(row[0][3])


def _resource(value: typed.DictionaryView, cls: type[typed.DictionaryView]) -> Dictionary:
    if not isinstance(value, cls) or not value.obj.is_indirect or isinstance(value.obj, Stream):
        msg = f"expected an indirect {cls.__name__} dictionary"
        raise ValueError(msg)
    return value.obj


def _node(node: typed.ThreeDNode) -> Dictionary:
    obj = _resource(node, typed.ThreeDNode)
    if "M" in obj:
        _vector(obj["M"], 12, "M")
    return obj


def _cross_section(section: typed.ThreeDCrossSection) -> Dictionary:
    obj = _resource(section, typed.ThreeDCrossSection)
    orientation = obj.get("O")
    if (
        not isinstance(orientation, Array)
        or len(orientation) != _AXES
        or orientation.count(None) != 1
        or not all(axis is None or _finite(axis) for axis in orientation)
    ):
        msg = "cross section /O requires one null axis and two rotation angles"
        raise ValueError(msg)
    if "C" in obj:
        _vector(obj["C"], 3, "C")
    return obj


def _measurement(measure: Measure) -> Dictionary:
    if type(measure) not in _MEASURES:
        msg = "expected a typed 3D measurement"
        raise ValueError(msg)
    obj = _resource(measure, type(measure))
    if measure.Subtype != _MEASURES[type(measure)] or obj.get("Type", Name("3DMeasure")) != Name("3DMeasure"):
        msg = "expected a 3D measurement dictionary of the declared subtype"
        raise ValueError(msg)
    for key, required in _geometry_keys(measure.Subtype).items():
        if required or key in obj:
            value = measure[key]
            if key not in {"V", "U"}:
                _vector(value.obj, 3, key)
            elif key == "V" and not _finite(value):
                msg = "measurement value must be finite"
                raise ValueError(msg)
    projection = obj.get("S")
    if projection is not None:
        extra = projection.get("ExData") if isinstance(projection, Dictionary) else None
        if not (
            isinstance(projection, Dictionary)
            and not isinstance(projection, Stream)
            and projection.is_indirect
            and projection.get("Subtype") == Name("Projection")
            and isinstance(extra, Dictionary)
            and not isinstance(extra, Stream)
            and extra.get("Type") == Name("ExData")
            and extra.get("Subtype") == Name("3DM")
            and extra.get("M3DREF") == obj
        ):
            msg = "measurement /S must reference its reciprocal projection annotation"
            raise ValueError(msg)
    return obj


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
        count += len(view.measurements) + len(view.nodes) + len(view.cross_sections) + 1
        for measurement in view.measurements:
            if isinstance(measurement, ModelMeasurement) and measurement.unit:
                size += len(measurement.unit.encode("utf-8"))
                if size > _MAX_TEXT:
                    break
        if size > _MAX_TEXT or count > _MAX_ITEMS:
            msg = "view names or measurement links exceed the annotation input limit"
            code = "annotation_input_size"
            raise LimitError(code, msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class ThreeD:
    """A 3D annotation whose stream holds an opaque U3D or PRC asset.

    Each named view is stored once. Supply an indirect Arlington activation
    dictionary to set the annotation's activation conditions. Attach a
    projection to a stored measurement from `annotation.media["3DD"].VA[0].MA`
    to share its geometry. Viewers handle 3D rendering.
    """

    rect: Rect
    format: ModelFormat
    data: bytes
    views: tuple[ModelView, ...] = ()
    activation: typed.ThreeDActivation | None = None

    def __post_init__(self) -> None:
        """Validate the rectangle, format, data and views."""
        _rect(self)
        _data(self.data)
        object.__setattr__(self, "format", ModelFormat(self.format))
        _views(self.views)
        if self.activation is not None:
            _resource(self.activation, typed.ThreeDActivation)


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
        if view.camera_to_world is not None:
            entries.update(MS=Name("M"), C2W=view.camera_to_world)
        if view.lighting is not None:
            entries["LS"] = {"Subtype": Name(view.lighting)}
        if view.render_mode is not None:
            entries["RM"] = {"Subtype": Name(view.render_mode)}
        if view.background is not None:
            entries["BG"] = {"C": view.background}
        if view.nodes:
            entries["NA"] = [_node(node) for node in view.nodes]
        if view.cross_sections:
            entries["SA"] = [_cross_section(section) for section in view.cross_sections]
        if view.measurements:
            entries["MA"] = [
                _indirect(doc, {"Type": Name("3DMeasure"), "Subtype": Name(m.kind), **m._entries()})  # noqa: SLF001
                if isinstance(m, ModelMeasurement)
                else _measurement(m)
                for m in view.measurements
            ]
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
        if spec.activation is not None:
            annot["3DA"] = spec.activation.obj
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


def _check_edit(doc: Document, before: StructuralReport, operation: str) -> None:
    after = doc.check()
    if before.truncated or after.truncated:
        msg = f"cannot verify {operation} with a truncated structural report"
        raise PdfError(msg)
    existing = set(before.defects)
    for defect in after.defects:
        # The writer settles the trailer's size at save time.
        if defect not in existing and (operation != "3D scene" or defect.clause != "FileTrailer/Size"):
            msg = f"{operation} would introduce a structural defect: {defect.clause}"
            raise PdfError(msg)


def _scene_version(doc: Document, spec: ThreeD) -> tuple[int, int]:
    minimum = _base_or_extension(doc, 1) if spec.format is ModelFormat.PRC else (1, 6)
    if spec.activation is not None:
        activation = spec.activation.obj
        if any(key in activation for key in ("Style", "Window", "Transparent")):
            minimum = max(minimum, _base_or_extension(doc, 3))
        if "TB" in activation or "NP" in activation:
            minimum = max(minimum, (1, 7))
    for view in spec.views:
        if (
            view.measurements
            or any(key in node.obj for node in view.nodes for key in ("Instance", "Data", "RM"))
            or any(key in section.obj for section in view.cross_sections for key in ("ST", "SC"))
        ):
            return (2, 0)
        if view.lighting or view.render_mode or view.nodes or view.cross_sections:
            minimum = max(minimum, (1, 7))
    return minimum


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
        has_resources = isinstance(spec, (ThreeD, RichMedia)) and any(v.nodes or v.cross_sections for v in spec.views)
        before = doc.check() if has_resources or (isinstance(spec, ThreeD) and spec.activation is not None) else None
        if isinstance(spec, RichMedia) and spec.views:
            # RichMedia's owned 3DViewAddEntries keys start at 2.0 in the pinned model.
            minimum = (2, 0)
        elif isinstance(spec, (RichMedia, ProjectionAnnotation)):
            minimum = _base_or_extension(doc, 3)
        elif isinstance(spec, ThreeD):
            minimum = _scene_version(doc, spec)
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
        if before is not None:
            _check_edit(doc, before, "3D scene")
        assert annot.xref is not None  # noqa: S101 - allocated above.
        assert annot.generation is not None  # noqa: S101 - allocated above.
        return annot.xref, annot.generation


@dataclass(slots=True)
class _PresentationTree:
    nodes: list[tuple[Dictionary, int | None]]
    entries: dict[bytes, Dictionary]


def _validate_resource(key: bytes, value: Dictionary) -> None:
    try:
        key.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = "slideshow resource names must be UTF-8"
        raise PdfError(msg) from exc
    if isinstance(value, Stream):
        valid = value.get("Subtype") == Name("Image")
    else:
        valid = value.get("Type") in (None, Name("Filespec")) and any(
            isinstance(value.get(field), String) for field in ("F", "DOS", "Mac", "Unix")
        )
    if not valid:
        msg = "a slideshow resource must be an image XObject or a file specification"
        raise PdfError(msg)


def _resource_entries(
    names: Array, entries: dict[bytes, Dictionary], budget: _core._OperationBudget, *, resources: bool
) -> None:
    if len(entries) + len(names) // 2 > _MAX_ITEMS:
        msg = "too many slideshow name-tree entries"
        code = "object_count"
        raise LimitError(code, msg)
    for offset in range(0, len(names), 2):
        budget.poll()
        key, value = names[offset], names[offset + 1]
        if not isinstance(key, String) or not isinstance(value, Dictionary):
            msg = "invalid slideshow name-tree entry"
            raise PdfError(msg)
        if len(key) > _MAX_TEXT:
            msg = "slideshow resource name exceeds 1 MiB"
            code = "text_input_size"
            raise LimitError(code, msg)
        if entries and key <= next(reversed(entries)):
            msg = "slideshow name-tree keys must be unique and ordered"
            raise PdfError(msg)
        budget.charge("memory_size", 256 + 5 * len(key))
        if resources:
            _validate_resource(key, value)
        entries[bytes(key)] = value


def _presentation_tree(root: object, *, resources: bool = False) -> _PresentationTree:
    if not isinstance(root, Dictionary) or isinstance(root, Stream):
        msg = "expected a slideshow name-tree dictionary"
        raise PdfError(msg)
    return _core.read_operation(root._core(), lambda budget: _read_tree(root, budget, resources=resources))  # noqa: SLF001


def _read_tree(root: Dictionary, budget: _core._OperationBudget, *, resources: bool) -> _PresentationTree:
    nodes: list[tuple[Dictionary, int | None]] = []
    entries: dict[bytes, Dictionary] = {}
    seen: set[int] = set()
    stack = [(root, None, 1)]
    while stack:
        node, parent, depth = stack.pop()
        budget.poll()
        if depth > 32 or len(nodes) >= _MAX_ITEMS:  # noqa: PLR2004 - name-tree depth.
            msg = "slideshow name tree exceeds its node or depth limit"
            code = "object_count"
            raise LimitError(code, msg)
        if node.xref in seen:
            msg = "repeated node in slideshow name tree"
            raise PdfError(msg)
        if node.xref is not None:
            seen.add(node.xref)
        budget.charge("memory_size", 160)
        index = len(nodes)
        nodes.append((node, parent))
        names, kids = node.get("Names"), node.get("Kids")
        if isinstance(kids, Array) and names is None and len(kids):
            if len(kids) + len(stack) + len(nodes) > _MAX_ITEMS:
                msg = "too many slideshow name-tree nodes"
                code = "object_count"
                raise LimitError(code, msg)
            for child in reversed(kids):
                if not isinstance(child, Dictionary) or isinstance(child, Stream) or not child.is_indirect:
                    msg = "slideshow name-tree children must be indirect dictionaries"
                    raise PdfError(msg)
                stack.append((child, index, depth + 1))
        elif isinstance(names, Array) and kids is None and len(names) % 2 == 0:
            _resource_entries(names, entries, budget, resources=resources)
        else:
            msg = "slideshow name-tree node requires one Names or Kids array"
            raise PdfError(msg)
    return _PresentationTree(nodes, entries)


def _resource_key(name: str) -> bytes:
    if name != "":
        _text(name)
    return name.encode("utf-8")


class SlideShow:
    """An existing embedded slideshow and the UTF-8 resource names in its virtual file system."""

    __slots__ = ("_show",)

    def __init__(self, show: typed.SlideShow) -> None:
        """Wrap an existing SlideShow dictionary."""
        if (
            not isinstance(show, typed.SlideShow)
            or show.obj.get("Type") != Name("SlideShow")
            or show.obj.get("Subtype") != Name("Embedded")
        ):
            msg = "expected an existing embedded SlideShow dictionary"
            raise TypeError(msg)
        self._show = show

    def _resource_tree(self) -> _PresentationTree:
        return _presentation_tree(self._show.obj.get("Resources"), resources=True)

    @property
    def resources(self) -> dict[str, Dictionary]:
        """Map UTF-8 resource names to their live image streams or file specifications."""
        entries = self._resource_tree().entries
        return {key.decode("utf-8"): obj for key, obj in entries.items()}

    @property
    def start_resource(self) -> str:
        """The name selected by StartResource. Assign the name of an existing resource to select it."""
        name = self._show.obj.get("StartResource")
        if not isinstance(name, String) or name not in self._resource_tree().entries:
            msg = "the slideshow start resource is missing from its name tree"
            raise PdfError(msg)
        return name.decode("utf-8")

    @start_resource.setter
    def start_resource(self, name: str) -> None:
        key = _resource_key(name)
        with self._show.obj._document.transaction("retarget slideshow"):  # noqa: SLF001
            if key not in self._resource_tree().entries:
                raise KeyError(name)
            self._show.obj["StartResource"] = key

    def rename_resource(self, old: str, new: str, *, keep_alias: bool = False) -> None:
        """Move a resource to a new UTF-8 name and update StartResource in one transaction.

        Shared resource trees keep their other slideshows' original names.
        Set keep_alias=True to retain the old name for references inside opaque media files.
        The media bytes are retained in both cases.
        """
        if not isinstance(keep_alias, bool):
            msg = "keep_alias must be a bool"
            raise TypeError(msg)
        original, replacement = _resource_key(old), _resource_key(new)
        doc = self._show.obj._document  # noqa: SLF001
        with doc.transaction("rename slideshow resource"):
            tree = self._resource_tree()
            if original not in tree.entries:
                raise KeyError(old)
            start = self._show.obj.get("StartResource")
            if not isinstance(start, String) or start not in tree.entries:
                msg = "the slideshow start resource is missing from its name tree"
                raise PdfError(msg)
            if original == replacement:
                return
            if replacement in tree.entries:
                msg = "the destination slideshow resource name already exists"
                raise ValueError(msg)
            if keep_alias and len(tree.entries) >= _MAX_ITEMS:
                msg = "too many slideshow name-tree entries"
                code = "object_count"
                raise LimitError(code, msg)
            tree.entries[replacement] = tree.entries[original] if keep_alias else tree.entries.pop(original)
            self._show.obj["Resources"] = _renamed_tree(doc, tree)
            if start == original:
                self._show.obj["StartResource"] = replacement
            retired = [node.xref for node, _ in tree.nodes if node.xref is not None]
            _core.object_free_unreferenced(doc._core(), retired)  # noqa: SLF001


def _renamed_tree(doc: Document, tree: _PresentationTree) -> Dictionary:
    # Reuse the tree shape and private dictionary keys. Stage replacements
    # before touching the show so even a shared leaf stays valid elsewhere.
    pairs = iter(sorted(tree.entries.items()))
    children: dict[int, list[int]] = {}
    leaf_values: dict[int, list[tuple[bytes, Dictionary]]] = {}
    for index, (node, parent) in enumerate(tree.nodes):
        if parent is not None:
            children.setdefault(parent, []).append(index)
        names = node.get("Names")
        if isinstance(names, Array):
            leaf_values[index] = [next(pairs) for _ in range(len(names) // 2)]

    leaf_values[next(reversed(leaf_values))].extend(pairs)
    built: dict[int, Dictionary] = {}
    bounds: dict[int, tuple[bytes, bytes]] = {}
    for index in reversed(range(len(tree.nodes))):
        node, parent = tree.nodes[index]
        copy = doc.objects[doc.objects.add(node)]
        if index in leaf_values:
            values = leaf_values[index]
            if not values:
                msg = "empty slideshow name-tree leaf"
                raise PdfError(msg)
            copy["Names"] = [item for pair in values for item in pair]
            bounds[index] = values[0][0], values[-1][0]
        else:
            kids = children[index]
            copy["Kids"] = [built[kid] for kid in kids]
            bounds[index] = bounds[kids[0]][0], bounds[kids[-1]][1]
        if parent is not None:
            copy["Limits"] = bounds[index]
        built[index] = copy
    return built[0]


def alternate_presentations(doc: Document) -> dict[bytes, SlideShow]:
    names = doc.catalog.get("Names")
    root = names.get("AlternatePresentations") if isinstance(names, Dictionary) else None
    if root is None:
        return {}
    return {key: SlideShow(typed.SlideShow(value)) for key, value in _presentation_tree(root).entries.items()}
