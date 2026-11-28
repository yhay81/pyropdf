"""The pure-Rust RTen document layout engine and its public region types."""

from __future__ import annotations

import functools
import json
import math
import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, TypeAlias

from pyropdf._core import LayoutError, Pixmap, _LayoutEngine
from pyropdf._geometry import Rect
from pyropdf._limits import _validate_optional_positive_int

if TYPE_CHECKING:
    from pyropdf._page import Page

LayoutKind: TypeAlias = Literal[
    "title",
    "heading",
    "text",
    "table",
    "figure",
    "caption",
    "header",
    "footer",
    "footnote",
    "formula",
    "code",
    "toc",
    "reference",
    "page_number",
    "abstract",
    "aside",
]
LayoutKind.__doc__ = """Normalized semantic role of one detected document layout region."""

_DEFAULT_MAX_LAYOUT_MODEL_SIZE = 64 * 1024 * 1024
_LAYOUT_MAX_THREADS = 16
_LAYOUT_MAX_CONCURRENT = 16
_DEFAULT_INPUT_SIZE = 800
_MAX_INPUT_SIZE = 2048
_MAX_METADATA_SIZE = 1024 * 1024
_PDF_POINTS_PER_INCH = 72.0

#: Default 20-class PP-DocLayout_plus-L label order.
_PP_DOCLAYOUT_PLUS_L_LABELS: Final[tuple[str, ...]] = (
    "paragraph_title",
    "image",
    "text",
    "number",
    "abstract",
    "content",
    "figure_title",
    "formula",
    "table",
    "reference",
    "doc_title",
    "footnote",
    "header",
    "algorithm",
    "footer",
    "seal",
    "chart",
    "formula_number",
    "aside_text",
    "reference_content",
)

#: Maps PP-DocLayout (20/23/25-class) and normalized semantic labels to ``LayoutKind``.
_LABEL_TO_KIND: Final[Mapping[str, LayoutKind]] = MappingProxyType(
    {
        "doc_title": "title",
        "title": "title",
        "paragraph_title": "heading",
        "heading": "heading",
        "text": "text",
        "vertical_text": "text",
        "table": "table",
        "image": "figure",
        "chart": "figure",
        "seal": "figure",
        "header_image": "figure",
        "footer_image": "figure",
        "figure": "figure",
        "figure_title": "caption",
        "table_title": "caption",
        "chart_title": "caption",
        "caption": "caption",
        "header": "header",
        "footer": "footer",
        "footnote": "footnote",
        "vision_footnote": "footnote",
        "formula": "formula",
        "display_formula": "formula",
        "inline_formula": "formula",
        "formula_number": "formula",
        "algorithm": "code",
        "code": "code",
        "content": "toc",
        "toc": "toc",
        "reference": "reference",
        "reference_content": "reference",
        "number": "page_number",
        "page_number": "page_number",
        "abstract": "abstract",
        "aside_text": "aside",
        "aside": "aside",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class LayoutRegion:
    """One detected semantic layout region on a page or pixmap.

    :meth:`Page.regions` returns ``bbox`` in the page's display space (points,
    top-left origin, matching :meth:`Page.layout` and :meth:`Page.ocr`).
    :meth:`LayoutEngine.detect` returns ``bbox`` in pixmap pixels (top-left origin).
    """

    kind: LayoutKind
    label: str
    bbox: Rect
    confidence: float
    reading_order: int | None = None


def _label_to_kind(label: str) -> LayoutKind:
    """Map one raw model label to a normalized :data:`LayoutKind`, falling back to ``"text"``."""
    return _LABEL_TO_KIND.get(label, "text")


def _validate_layout_dpi(dpi: float) -> float:
    """Resolve one positive finite layout-detection resolution."""
    if isinstance(dpi, bool):
        msg = f"dpi must be a positive finite number: {dpi!r}"
        raise TypeError(msg)
    try:
        resolved_dpi = float(dpi)
    except (TypeError, ValueError) as exc:
        msg = f"dpi must be a positive finite number: {dpi!r}"
        raise LayoutError(msg) from exc
    if not (math.isfinite(resolved_dpi) and resolved_dpi > 0):
        msg = f"dpi must be a positive finite number: {dpi!r}"
        raise LayoutError(msg)
    return resolved_dpi


def _validate_layout_confidence(min_confidence: float) -> float:
    """Resolve one finite confidence threshold from 0 through 1."""
    if isinstance(min_confidence, bool):
        msg = f"min_confidence must be a finite number from 0 through 1: {min_confidence!r}"
        raise TypeError(msg)
    try:
        resolved_confidence = float(min_confidence)
    except (TypeError, ValueError) as exc:
        msg = f"min_confidence must be a finite number from 0 through 1: {min_confidence!r}"
        raise LayoutError(msg) from exc
    if not (math.isfinite(resolved_confidence) and 0 <= resolved_confidence <= 1):
        msg = f"min_confidence must be a finite number from 0 through 1: {min_confidence!r}"
        raise LayoutError(msg)
    return resolved_confidence


def _validate_layout_threads(threads: int | None) -> int:
    """Resolve one thread count from 1 through ``_LAYOUT_MAX_THREADS``."""
    if threads is None:
        return min(4, os.cpu_count() or 1)
    if isinstance(threads, bool) or not isinstance(threads, int):
        msg = f"threads must be an integer from 1 through {_LAYOUT_MAX_THREADS} or None: {threads!r}"
        raise TypeError(msg)
    if not 1 <= threads <= _LAYOUT_MAX_THREADS:
        msg = f"threads must be from 1 through {_LAYOUT_MAX_THREADS}: {threads}"
        raise LayoutError(msg)
    return threads


def _validate_layout_concurrency(max_concurrent: int) -> int:
    """Validate the number of concurrent layout detection calls admitted per engine."""
    if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int):
        msg = f"max_concurrent must be an integer from 1 through {_LAYOUT_MAX_CONCURRENT}: {max_concurrent!r}"
        raise TypeError(msg)
    if not 1 <= max_concurrent <= _LAYOUT_MAX_CONCURRENT:
        msg = f"max_concurrent must be from 1 through {_LAYOUT_MAX_CONCURRENT}: {max_concurrent}"
        raise LayoutError(msg)
    return max_concurrent


def _validate_labels(labels: Sequence[str]) -> tuple[str, ...]:
    """Validate and copy a caller- or metadata-supplied class label sequence."""
    if isinstance(labels, (str, bytes)) or not isinstance(labels, Sequence):
        msg = f"labels must be a sequence of strings or None: {labels!r}"
        raise TypeError(msg)
    resolved: list[str] = []
    for item in labels:
        if not isinstance(item, str):
            msg = f"labels entries must be strings: {item!r}"
            raise TypeError(msg)
        if not item:
            msg = "labels entries must not be empty"
            raise LayoutError(msg)
        resolved.append(item)
    if not resolved:
        msg = "labels must not be empty"
        raise LayoutError(msg)
    return tuple(resolved)


def _read_model_metadata(metadata_path: str | os.PathLike[str]) -> tuple[tuple[str, ...] | None, int]:
    """Read optional ``model.json`` metadata from a discovered model package."""
    path = Path(os.fspath(metadata_path))
    try:
        size = path.stat().st_size
    except OSError as exc:
        msg = f"failed to read layout model metadata {path}: {exc}"
        raise LayoutError(msg) from exc
    if size > _MAX_METADATA_SIZE:
        msg = f"layout model metadata {path} exceeds {_MAX_METADATA_SIZE} bytes"
        raise LayoutError(msg)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"failed to parse layout model metadata {path}: {exc}"
        raise LayoutError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"layout model metadata {path} must be a JSON object"
        raise LayoutError(msg)
    raw_labels = payload.get("labels")
    parsed_labels = None if raw_labels is None else _validate_labels(raw_labels)
    raw_input_size = payload.get("input_size", _DEFAULT_INPUT_SIZE)
    if (
        isinstance(raw_input_size, bool)
        or not isinstance(raw_input_size, int)
        or not 1 <= raw_input_size <= _MAX_INPUT_SIZE
    ):
        msg = (
            f"layout model metadata input_size must be an integer from 1 through {_MAX_INPUT_SIZE}: {raw_input_size!r}"
        )
        raise LayoutError(msg)
    return parsed_labels, raw_input_size


def _discover_default_model(
    labels: Sequence[str] | None,
) -> tuple[str | os.PathLike[str], tuple[str, ...], int]:
    """Locate the default ``.rten`` model and metadata from ``pyropdf-layout-models``."""
    try:
        import pyropdf_layout_models  # ty: ignore[import-not-found]  # noqa: PLC0415 - lazy optional dependency.
    except ImportError as exc:
        msg = "Layout models are not installed; install pyropdf[layout] or pass an explicit model_path"
        raise LayoutError(msg) from exc
    try:
        discovered = pyropdf_layout_models.model_paths()
        discovered_model = discovered.model
        metadata_path = getattr(discovered, "metadata", None)
    except (AttributeError, TypeError) as exc:
        msg = "pyropdf-layout-models does not expose a compatible model_paths() result"
        raise LayoutError(msg) from exc
    if discovered_model is None:
        msg = "layout model discovery returned an incomplete model path"
        raise LayoutError(msg)
    meta_labels: tuple[str, ...] | None = None
    input_size = _DEFAULT_INPUT_SIZE
    if metadata_path is not None:
        meta_labels, input_size = _read_model_metadata(metadata_path)
    resolved_labels = _validate_labels(labels) if labels is not None else (meta_labels or _PP_DOCLAYOUT_PLUS_L_LABELS)
    return discovered_model, resolved_labels, input_size


def _build_regions(
    raw_detections: Sequence[tuple[int, float, float, float, float, float, int | None]],
    labels: tuple[str, ...],
    *,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    scale: float = 1.0,
) -> list[LayoutRegion]:
    """Convert raw engine tuples into :class:`LayoutRegion` records."""
    regions: list[LayoutRegion] = []
    for class_id, confidence, x0, y0, x1, y1, reading_order in raw_detections:
        label = labels[class_id] if 0 <= class_id < len(labels) else f"class_{class_id}"
        regions.append(
            LayoutRegion(
                kind=_label_to_kind(label),
                label=label,
                bbox=Rect(
                    offset_x + x0 * scale,
                    offset_y + y0 * scale,
                    offset_x + x1 * scale,
                    offset_y + y1 * scale,
                ),
                confidence=confidence,
                reading_order=reading_order,
            )
        )
    return regions


class LayoutEngine:
    """Pure-Rust RTen document layout detector.

    Loads a pre-converted ``.rten`` model (by default discovered from
    ``pyropdf-layout-models`` via ``pyropdf[layout]``). Thread-safe and
    reusable across documents and threads.
    """

    def __init__(
        self,
        model_path: str | os.PathLike[str] | None = None,
        *,
        labels: Sequence[str] | None = None,
        threads: int | None = None,
        max_concurrent: int = 1,
        max_model_size: int | None = _DEFAULT_MAX_LAYOUT_MODEL_SIZE,
    ) -> None:
        """Load a layout detection model once for reuse across pages and documents.

        ``threads=None`` uses up to four logical CPUs. ``max_concurrent`` bounds
        concurrent render-and-detect calls sharing this engine; its memory-safe
        default is one. ``max_model_size`` bounds the ``.rten`` file size before
        reading; pass ``None`` only for trusted custom models.
        """
        _validate_optional_positive_int("max_model_size", max_model_size)
        resolved_threads = _validate_layout_threads(threads)
        resolved_max_concurrent = _validate_layout_concurrency(max_concurrent)
        if model_path is None:
            resolved_model_path, resolved_labels, input_size = _discover_default_model(labels)
        else:
            resolved_model_path = model_path
            resolved_labels = _validate_labels(labels) if labels is not None else _PP_DOCLAYOUT_PLUS_L_LABELS
            input_size = _DEFAULT_INPUT_SIZE
        self._engine = _LayoutEngine(
            os.fspath(resolved_model_path),
            resolved_threads,
            max_model_size,
            input_size,
        )
        self._labels = resolved_labels
        self._threads = resolved_threads
        self._max_concurrent = resolved_max_concurrent
        self._max_model_size = max_model_size
        self._detection_slots = threading.BoundedSemaphore(resolved_max_concurrent)

    @property
    def threads(self) -> int:
        """Number of RTen inference threads configured on this engine."""
        return self._threads

    @property
    def max_concurrent(self) -> int:
        """Maximum concurrent render-and-detect calls admitted by this engine."""
        return self._max_concurrent

    @property
    def max_model_size(self) -> int | None:
        """Maximum ``.rten`` model file size in bytes, or ``None`` when unbounded."""
        return self._max_model_size

    def detect(
        self,
        pixmap: Pixmap,
        *,
        min_confidence: float = 0.5,
    ) -> list[LayoutRegion]:
        """Detect semantic layout regions in any :class:`Pixmap`.

        Boxes are returned in the pixmap's pixel coordinates with top-left
        origin. Transparent pixels are composited over white. Refuses rasters
        larger than 512 MiB in RGBA or more than 1,024 retained regions with
        :class:`LimitError`. To detect regions on a page in display space, use
        :meth:`Page.regions`.
        """
        if not isinstance(pixmap, Pixmap):
            msg = f"pixmap must be a pyropdf.Pixmap: {pixmap!r}"
            raise TypeError(msg)
        resolved_confidence = _validate_layout_confidence(min_confidence)
        with self._detection_slots:
            raster_results = self._engine.detect_pixmap(
                pixmap,
                min_confidence=resolved_confidence,
            )
        return _build_regions(raster_results, self._labels)

    def _detect_page(
        self,
        page: Page,
        *,
        dpi: float,
        min_confidence: float,
        clip: tuple[float, float, float, float] | None,
    ) -> list[LayoutRegion]:
        """Render a page on white at ``dpi`` and detect layout regions in display space."""
        resolved_dpi = _validate_layout_dpi(dpi)
        resolved_confidence = _validate_layout_confidence(min_confidence)
        with self._detection_slots:
            pixmap = page.pixmap(dpi=resolved_dpi, clip=clip, alpha=True, background=(255, 255, 255))
            raster_results = self._engine.detect_pixmap(
                pixmap,
                min_confidence=resolved_confidence,
                document=page.document._core(),  # noqa: SLF001 - share document operation limits.
            )
        pixel_to_page = _PDF_POINTS_PER_INCH / resolved_dpi
        if clip is None:
            offset_x = offset_y = 0.0
        else:
            page_rect = page.rect
            offset_x = math.floor(max(clip[0], page_rect.x0) / pixel_to_page) * pixel_to_page
            offset_y = math.floor(max(clip[1], page_rect.y0) / pixel_to_page) * pixel_to_page
        return _build_regions(
            raster_results,
            self._labels,
            offset_x=offset_x,
            offset_y=offset_y,
            scale=pixel_to_page,
        )


@functools.cache
def _default_layout_engine() -> LayoutEngine:
    """Load and cache the optional default layout model."""
    return LayoutEngine()


# The package re-exports these classes; reprs, pickles, and documentation name them.
LayoutEngine.__module__ = "pyropdf"
LayoutRegion.__module__ = "pyropdf"
