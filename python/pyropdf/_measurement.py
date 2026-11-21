"""Viewports, rectilinear and geospatial measurements, and document requirements (ISO 32000-2 §12.9, §12.10)."""

from __future__ import annotations

import enum
import itertools
import math
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Context, Decimal, InvalidOperation, localcontext
from typing import TYPE_CHECKING, Literal, TypeAlias, cast

from pyropdf import _core
from pyropdf._actions import _MAX_ENCODED_SCRIPT_SIZE, _MAX_SCRIPT_SIZE, _validate_script
from pyropdf._errors import LimitError, PdfError
from pyropdf._geometry import Rect
from pyropdf._media import _base_or_extension
from pyropdf._metadata import _require_version
from pyropdf._objects import Array, Dictionary, Name, Real, Stream, String, _catalog, _Handle, _unlocked
from pyropdf._presentations import _FLOAT32_MAX, _to_display
from pyropdf._threads import _display_rect, _display_to_pdf_rect

if TYPE_CHECKING:
    from pyropdf._document import Document
    from pyropdf._limit_codes import LimitCode
    from pyropdf._page import Page

_PointName: TypeAlias = Literal["LAT", "LON", "ALT"]

_INT64_MAX = (1 << 63) - 1
_RECT_COORDS = 4
_ORIGIN_LEN = 2
_PDU_LEN = 3
_PAIR: Literal[2] = 2
_TRIPLE: Literal[3] = 3
_PCSM_LEN = 12
_MAX_PENALTY = 100

#: Maximum UTF-8 byte length of text fields written in one measurement call.
_MAX_MEASUREMENT_TEXT_BYTES = 1024 * 1024
#: Maximum positional ASCII length of an exact decimal real written to PDF.
_MAX_DECIMAL_LEXEME_BYTES = 512
#: Maximum number of viewports on one page.
_MAX_VIEWPORTS = 4096
#: Maximum number of ``NumberFormat`` entries in one rectilinear unit chain.
_MAX_NUMBER_FORMATS = 64
#: Maximum number of numeric coordinates in ``/GPTS``, ``/LPTS``, or ``/Bounds``.
_MAX_GEO_NUMBERS = 8192
#: Maximum number of ``/Names`` columns in ``PtData``.
_MAX_POINT_NAMES = 64
#: Maximum number of point rows in ``PtData`` ``/XPTS``.
_MAX_POINT_DATA_ROWS = 4096
#: Maximum number of requirements in ``Catalog['Requirements']``.
_MAX_REQUIREMENTS = 4096
#: Maximum number of handlers in one requirement's ``/RH`` array.
_MAX_HANDLERS = 256
#: Maximum container nodes visited when collecting retired indirect objects.
_MAX_CANDIDATE_NODES = 16384

_PDU_NAMES = frozenset(
    {
        "M",
        "KM",
        "FT",
        "USFT",
        "MI",
        "NM",
        "SQM",
        "HA",
        "SQKM",
        "SQFT",
        "A",
        "SQMI",
        "DEG",
        "GRD",
    }
)
_POINT_NAMES: frozenset[str] = frozenset({"LAT", "LON", "ALT"})


def _is_non_bool_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_bounded_int(value: object, low: int, high: int) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
        return int(value)
    return None


def _is_dict(value: object) -> bool:
    return isinstance(value, Dictionary) and not isinstance(value, Stream)


def _check_text_limit(utf8_bytes: int, label: str) -> None:
    if utf8_bytes > _MAX_MEASUREMENT_TEXT_BYTES:
        code: LimitCode = "text_input_size"
        msg = f"{label} exceeds the {_MAX_MEASUREMENT_TEXT_BYTES}-byte UTF-8 limit"
        raise LimitError(code, msg)


def _validate_str(value: object, label: str, *, allow_empty: bool = False, budget: list[int] | None = None) -> str:
    if not isinstance(value, str):
        msg = f"{label} must be a str: {value!r}"
        raise TypeError(msg)
    plain = str.__str__(value)
    if not allow_empty and not plain:
        msg = f"{label} must not be empty"
        raise ValueError(msg)
    current = 0 if budget is None else budget[0]
    if len(plain) > _MAX_MEASUREMENT_TEXT_BYTES - current:
        _check_text_limit(_MAX_MEASUREMENT_TEXT_BYTES + 1, label)
    utf8_len = len(plain.encode("utf-8", "surrogatepass"))
    _check_text_limit(current + utf8_len, label)
    if budget is not None:
        budget[0] = current + utf8_len
    return plain


def _validate_ascii_str(value: object, label: str, *, budget: list[int] | None = None) -> str:
    plain = _validate_str(value, label, allow_empty=False, budget=budget)
    if not (plain.isascii() and plain.isprintable()):
        msg = f"{label} must be a printable ASCII string: {value!r}"
        raise ValueError(msg)
    return plain


def _validate_pdf_name(value: object, label: str) -> str:
    plain = _validate_str(value, label, allow_empty=False)
    if plain.startswith("/") or "\x00" in plain:
        msg = f"{label} must be a PDF name without a leading slash or NUL byte: {value!r}"
        raise ValueError(msg)
    return plain


def _positional_decimal_len(value: Decimal) -> int:
    sign, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)  # noqa: S101 - caller verified value.is_finite().
    num_digits = len(digits)
    if exponent >= 0:
        body = num_digits + exponent + 2
    elif -exponent < num_digits:
        body = num_digits + 1
    else:
        body = -exponent + 2
    return body + (1 if sign else 0)


def _parse_decimal_str(value: str, label: str) -> Decimal:
    stripped = str.__str__(value).strip()
    if not stripped or len(stripped) > _MAX_DECIMAL_LEXEME_BYTES:
        msg = f"{label} is not a valid bounded decimal: {value!r}"
        raise ValueError(msg)
    try:
        return Decimal(stripped)
    except InvalidOperation as error:
        msg = f"{label} is not a valid decimal number: {value!r}"
        raise ValueError(msg) from error


def _to_decimal(value: object, label: str) -> Decimal:
    """Validate and normalize ``value`` to a finite :class:`Decimal` writable as a PDF real."""
    if isinstance(value, bool):
        msg = f"{label} must be a Decimal, Real, int, float, or numeric str, not bool"
        raise TypeError(msg)
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, Real):
        dec = value.to_decimal()
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            msg = f"{label} must be finite: {value!r}"
            raise ValueError(msg)
        dec = Decimal(str(value))
    elif isinstance(value, str):
        dec = _parse_decimal_str(value, label)
    else:
        msg = f"{label} must be a Decimal, Real, int, float, or numeric str: {type(value).__name__}"
        raise TypeError(msg)

    if not dec.is_finite():
        msg = f"{label} must be finite: {value!r}"
        raise ValueError(msg)
    if _positional_decimal_len(dec) > _MAX_DECIMAL_LEXEME_BYTES or not math.isfinite(float(dec)):
        msg = f"{label} exceeds the PDF real precision or range limit: {value!r}"
        raise ValueError(msg)
    return dec


def _read_pdf_decimal(value: object) -> Decimal | None:
    """Convert a numeric PDF scalar (``Real``, ``int``, or ``float``) to an exact :class:`Decimal`."""
    if isinstance(value, bool):
        return None
    if isinstance(value, Real):
        dec = value.to_decimal()
        return dec if dec.is_finite() and _positional_decimal_len(dec) <= _MAX_DECIMAL_LEXEME_BYTES else None
    if isinstance(value, int):
        dec = Decimal(value)
        return dec if _positional_decimal_len(dec) <= _MAX_DECIMAL_LEXEME_BYTES and math.isfinite(float(dec)) else None
    if isinstance(value, float):
        return Decimal(str(value)) if math.isfinite(value) else None
    return None


def _require_measurement_version(document: Document, catalog: Dictionary, minimum: tuple[int, int]) -> None:
    """Promote ``catalog['Version']`` to ``minimum`` and ensure PDF 2.0 trailer ``/ID`` when needed."""
    from pyropdf._annotations import _version  # noqa: PLC0415

    before = document.check() if _version(document) < (2, 0) <= minimum else None
    _require_version(document, catalog, minimum)
    if minimum >= (2, 0) and "ID" not in document.trailer:
        file_id = secrets.token_bytes(16)
        document.trailer["ID"] = [file_id, file_id]
    if before is not None:
        after = document.check()
        if before.truncated or after.truncated:
            msg = "cannot verify PDF 2.0 promotion with a truncated structural report"
            raise PdfError(msg)
        existing = set(before.defects)
        for defect in after.defects:
            if defect not in existing:
                msg = f"PDF 2.0 promotion would introduce a structural defect: {defect.clause}"
                raise PdfError(msg)


def _container_children(container: Array | Dictionary) -> list[Array | Dictionary]:
    children: list[Array | Dictionary] = []
    if isinstance(container, Array):
        for idx in range(min(len(container), _MAX_CANDIDATE_NODES)):
            child = container[idx]
            if isinstance(child, Array) or _is_dict(child):
                children.append(cast("Array | Dictionary", child))
    elif _is_dict(container):
        for key in container:
            child = container.get(key)
            if isinstance(child, Array) or _is_dict(child):
                children.append(cast("Array | Dictionary", child))
    return children


def _collect_indirect_candidates(root: object) -> list[int]:
    """Collect indirect object numbers reachable inside ``root`` iteratively with cycle and node bounds."""
    if not isinstance(root, (Array, Dictionary)) or isinstance(root, Stream):
        return []
    stack: list[Array | Dictionary] = [root]
    seen_xrefs: set[int] = set()
    candidates: list[int] = []
    visited = 0
    while stack and visited < _MAX_CANDIDATE_NODES:
        current = stack.pop()
        visited += 1
        if isinstance(current, _Handle) and current.is_indirect and current.xref is not None:
            if current.xref in seen_xrefs:
                continue
            seen_xrefs.add(current.xref)
            candidates.append(current.xref)
        room = _MAX_CANDIDATE_NODES - len(stack)
        if room > 0:
            stack.extend(_container_children(current)[:room])
    return candidates


class NumberFormatFraction(enum.StrEnum):
    """Fractional display mode of the final :class:`NumberFormat` in a rectilinear unit chain (``/F``, Table 268)."""

    DECIMAL = "D"
    """Decimal format using :attr:`NumberFormat.decimal_separator` and :attr:`NumberFormat.thousands_separator`."""
    FRACTION = "F"
    """Vulgar fraction denominated by :attr:`NumberFormat.denominator`."""
    ROUND = "R"
    """Round to the nearest whole unit."""
    TRUNCATE = "T"
    """Truncate fractional parts to whole units."""


@dataclass(frozen=True, slots=True, init=False)
class NumberFormat:
    """One unit and conversion factor in a rectilinear measurement chain (``NumberFormat``, ISO 32000-2 Table 268).

    ``factor`` (``/C``) is stored as an exact :class:`~decimal.Decimal` and
    written in positional decimal form without binary floating-point rounding.
    ``fraction`` (``/F``), ``denominator`` (``/D``), and ``force_denominator``
    (``/FD``) are valid only on the final :class:`NumberFormat` of a unit chain.
    """

    unit: str
    factor: Decimal
    fraction: NumberFormatFraction | None
    denominator: int | None
    force_denominator: bool | None
    thousands_separator: str | None
    decimal_separator: str | None
    prefix_spacing: str | None
    suffix_spacing: str | None
    label_position: Literal["S", "P"] | None

    def __init__(  # noqa: PLR0913 - ISO 32000-2 Table 268 NumberFormat fields.
        self,
        unit: str,
        factor: Decimal | Real | float | str,
        *,
        fraction: NumberFormatFraction | str | None = None,
        denominator: int | None = None,
        force_denominator: bool | None = None,
        thousands_separator: str | None = None,
        decimal_separator: str | None = None,
        prefix_spacing: str | None = None,
        suffix_spacing: str | None = None,
        label_position: Literal["S", "P"] | None = None,
    ) -> None:
        """Validate a rectilinear measurement number format."""
        budget = [0]
        checked_unit = _validate_str(unit, "unit", allow_empty=False, budget=budget)
        checked_factor = _to_decimal(factor, "factor")

        checked_fraction: NumberFormatFraction | None = None
        if fraction is not None:
            if not isinstance(fraction, str):
                msg = f"fraction must be a NumberFormatFraction, str, or None: {fraction!r}"
                raise TypeError(msg)
            checked_fraction = NumberFormatFraction(fraction)

        checked_denominator: int | None = None
        if denominator is not None:
            if not _is_non_bool_int(denominator):
                msg = f"denominator must be an int or None: {denominator!r}"
                raise TypeError(msg)
            if not 0 < denominator <= _INT64_MAX:
                msg = f"denominator must be positive: {denominator}"
                raise ValueError(msg)
            checked_denominator = int(denominator)

        if force_denominator is not None and not isinstance(force_denominator, bool):
            msg = f"force_denominator must be a bool or None: {force_denominator!r}"
            raise TypeError(msg)

        checked_rt = (
            None
            if thousands_separator is None
            else _validate_str(thousands_separator, "thousands_separator", allow_empty=True, budget=budget)
        )
        checked_rd = (
            None
            if decimal_separator is None
            else _validate_str(decimal_separator, "decimal_separator", allow_empty=True, budget=budget)
        )
        checked_ps = (
            None
            if prefix_spacing is None
            else _validate_str(prefix_spacing, "prefix_spacing", allow_empty=True, budget=budget)
        )
        checked_ss = (
            None
            if suffix_spacing is None
            else _validate_str(suffix_spacing, "suffix_spacing", allow_empty=True, budget=budget)
        )

        checked_pos: Literal["S", "P"] | None = None
        if label_position is not None:
            if not isinstance(label_position, str):
                msg = f"label_position must be 'S', 'P', or None: {label_position!r}"
                raise TypeError(msg)
            plain_pos = str.__str__(label_position)
            if plain_pos not in {"S", "P"}:
                msg = f"label_position must be 'S' or 'P': {label_position!r}"
                raise ValueError(msg)
            checked_pos = plain_pos

        object.__setattr__(self, "unit", checked_unit)
        object.__setattr__(self, "factor", checked_factor)
        object.__setattr__(self, "fraction", checked_fraction)
        object.__setattr__(self, "denominator", checked_denominator)
        object.__setattr__(self, "force_denominator", force_denominator)
        object.__setattr__(self, "thousands_separator", checked_rt)
        object.__setattr__(self, "decimal_separator", checked_rd)
        object.__setattr__(self, "prefix_spacing", checked_ps)
        object.__setattr__(self, "suffix_spacing", checked_ss)
        object.__setattr__(self, "label_position", checked_pos)


def _validate_number_format_chain(items: object, label: str, budget: list[int]) -> tuple[NumberFormat, ...]:
    if isinstance(items, (str, bytes, bytearray, memoryview)) or not isinstance(items, Iterable):
        msg = f"{label} must be an iterable of NumberFormat values, not {type(items).__name__}"
        raise TypeError(msg)
    taken = list(itertools.islice(items, _MAX_NUMBER_FORMATS + 1))
    if len(taken) > _MAX_NUMBER_FORMATS:
        msg = f"{label} cannot contain more than {_MAX_NUMBER_FORMATS} NumberFormat entries"
        raise ValueError(msg)
    if not taken:
        msg = f"{label} must contain at least one NumberFormat"
        raise ValueError(msg)
    for index, nf in enumerate(taken):
        if not isinstance(nf, NumberFormat):
            msg = f"{label}[{index}] must be a NumberFormat: {type(nf).__name__}"
            raise TypeError(msg)
        for text in (nf.unit, nf.thousands_separator, nf.decimal_separator, nf.prefix_spacing, nf.suffix_spacing):
            if text is not None:
                _validate_str(text, label, allow_empty=True, budget=budget)
    for nf in taken[:-1]:
        if nf.fraction is not None or nf.denominator is not None or nf.force_denominator is not None:
            msg = f"{label} allows fraction, denominator, and force_denominator only on its final NumberFormat"
            raise ValueError(msg)
    return tuple(taken)


@dataclass(frozen=True, slots=True, init=False)
class RectilinearMeasure:
    """A rectilinear coordinate system and unit conversion dictionary (``/Subtype /RL``, ISO 32000-2 Table 267).

    ``scale_ratio`` describes the drawing scale (``/R``, such as ``"1 in = 10 ft"``).
    ``x`` (``/X``), ``distance`` (``/D``), and ``area`` (``/A``) are non-empty
    unit chains from largest to smallest unit; ``cyx`` (``/CYX``) may be set
    only when a distinct ``y`` (``/Y``) chain is provided.
    """

    scale_ratio: str
    x: tuple[NumberFormat, ...]
    distance: tuple[NumberFormat, ...]
    area: tuple[NumberFormat, ...]
    y: tuple[NumberFormat, ...] | None
    angles: tuple[NumberFormat, ...] | None
    slope: tuple[NumberFormat, ...] | None
    origin: tuple[Decimal, Decimal] | None
    cyx: Decimal | None

    def __init__(  # noqa: PLR0913 - ISO 32000-2 Table 267 MeasureRL fields.
        self,
        scale_ratio: str,
        x: Iterable[NumberFormat],
        distance: Iterable[NumberFormat],
        area: Iterable[NumberFormat],
        *,
        y: Iterable[NumberFormat] | None = None,
        angles: Iterable[NumberFormat] | None = None,
        slope: Iterable[NumberFormat] | None = None,
        origin: Sequence[Decimal | Real | float | str] | None = None,
        cyx: Decimal | Real | float | str | None = None,
    ) -> None:
        """Validate rectilinear unit chains, origin, and ``cyx`` invariants."""
        budget = [0]
        checked_ratio = _validate_str(scale_ratio, "scale_ratio", allow_empty=False, budget=budget)
        checked_x = _validate_number_format_chain(x, "x", budget)
        checked_d = _validate_number_format_chain(distance, "distance", budget)
        checked_a = _validate_number_format_chain(area, "area", budget)
        checked_y = None if y is None else _validate_number_format_chain(y, "y", budget)
        checked_t = None if angles is None else _validate_number_format_chain(angles, "angles", budget)
        checked_s = None if slope is None else _validate_number_format_chain(slope, "slope", budget)

        checked_origin: tuple[Decimal, Decimal] | None = None
        if origin is not None:
            if isinstance(origin, (str, bytes, bytearray, memoryview)) or not isinstance(origin, Sequence):
                msg = f"origin must be a 2-element sequence of numbers: {origin!r}"
                raise TypeError(msg)
            if len(origin) != _ORIGIN_LEN:
                msg = f"origin must have 2 coordinates (x, y): {origin!r}"
                raise ValueError(msg)
            checked_origin = (_to_decimal(origin[0], "origin[0]"), _to_decimal(origin[1], "origin[1]"))

        checked_cyx = None if cyx is None else _to_decimal(cyx, "cyx")
        if checked_cyx is not None and checked_y is None:
            msg = "cyx (/CYX) can be set only when y (/Y) is provided"
            raise ValueError(msg)

        object.__setattr__(self, "scale_ratio", checked_ratio)
        object.__setattr__(self, "x", checked_x)
        object.__setattr__(self, "distance", checked_d)
        object.__setattr__(self, "area", checked_a)
        object.__setattr__(self, "y", checked_y)
        object.__setattr__(self, "angles", checked_t)
        object.__setattr__(self, "slope", checked_s)
        object.__setattr__(self, "origin", checked_origin)
        object.__setattr__(self, "cyx", checked_cyx)


@dataclass(frozen=True, slots=True, init=False)
class CoordinateSystem:
    """A geographic (``/GEOGCS``) or projected (``/PROJCS``) coordinate reference system (Tables 270–271).

    ``epsg`` (``/EPSG``) and ``wkt`` (``/WKT``, printable ASCII Well-Known Text)
    are mutually exclusive; a ``"projected"`` coordinate system must supply one
    of them.
    """

    kind: Literal["geographic", "projected"]
    epsg: int | None
    wkt: str | None

    def __init__(
        self,
        kind: Literal["geographic", "projected"] = "geographic",
        *,
        epsg: int | None = None,
        wkt: str | None = None,
    ) -> None:
        """Validate ``kind`` and mutual exclusion of ``epsg`` and ``wkt``."""
        if not isinstance(kind, str):
            msg = f"kind must be 'geographic' or 'projected': {kind!r}"
            raise TypeError(msg)
        plain_kind = str.__str__(kind)
        if plain_kind not in {"geographic", "projected"}:
            msg = f"kind must be 'geographic' or 'projected': {kind!r}"
            raise ValueError(msg)

        checked_epsg: int | None = None
        if epsg is not None:
            if not _is_non_bool_int(epsg):
                msg = f"epsg must be an int or None: {epsg!r}"
                raise TypeError(msg)
            if not 0 < epsg <= _INT64_MAX:
                msg = f"epsg must be a positive integer: {epsg}"
                raise ValueError(msg)
            checked_epsg = int(epsg)

        checked_wkt = None if wkt is None else _validate_ascii_str(wkt, "wkt")
        if checked_epsg is not None and checked_wkt is not None:
            msg = "CoordinateSystem cannot set both epsg (/EPSG) and wkt (/WKT)"
            raise ValueError(msg)
        if plain_kind == "projected" and checked_epsg is None and checked_wkt is None:
            msg = "projected CoordinateSystem requires either epsg (/EPSG) or wkt (/WKT)"
            raise ValueError(msg)

        object.__setattr__(self, "kind", plain_kind)
        object.__setattr__(self, "epsg", checked_epsg)
        object.__setattr__(self, "wkt", checked_wkt)


def _validate_decimal_tuple(
    items: object,
    label: str,
    *,
    max_len: int,
    even: bool = False,
    exact_len: int | None = None,
) -> tuple[Decimal, ...]:
    if isinstance(items, (str, bytes, bytearray, memoryview)) or not isinstance(items, Iterable):
        msg = f"{label} must be an iterable of numbers, not {type(items).__name__}"
        raise TypeError(msg)
    limit = exact_len if exact_len is not None else max_len
    taken = list(itertools.islice(items, limit + 1))
    if exact_len is not None:
        if len(taken) != exact_len:
            msg = f"{label} must contain {exact_len} numbers, got {len(taken)}"
            raise ValueError(msg)
    else:
        if len(taken) > max_len:
            msg = f"{label} cannot contain more than {max_len} numbers"
            raise ValueError(msg)
        if not taken:
            msg = f"{label} must not be empty"
            raise ValueError(msg)
        if even and len(taken) % 2 != 0:
            msg = f"{label} must contain an even number of coordinates, got {len(taken)}"
            raise ValueError(msg)
    return tuple(_to_decimal(item, f"{label}[{index}]") for index, item in enumerate(taken))


def _validate_pdu(pdu: Sequence[str] | None) -> tuple[str, str, str] | None:
    if pdu is None:
        return None
    if isinstance(pdu, (str, bytes, bytearray, memoryview)) or not isinstance(pdu, Sequence):
        msg = f"pdu must be a 3-element sequence of unit names: {pdu!r}"
        raise TypeError(msg)
    if len(pdu) != _PDU_LEN:
        msg = f"pdu must contain 3 unit names (linear, area, angular): {pdu!r}"
        raise ValueError(msg)
    units: list[str] = []
    for index, unit in enumerate(pdu):
        if not isinstance(unit, str):
            msg = f"pdu[{index}] must be a str: {unit!r}"
            raise TypeError(msg)
        plain_unit = str.__str__(unit)
        if plain_unit not in _PDU_NAMES:
            msg = f"pdu[{index}] must be one of {sorted(_PDU_NAMES)}: {unit!r}"
            raise ValueError(msg)
        units.append(plain_unit)
    return (units[0], units[1], units[2])


@dataclass(frozen=True, slots=True, init=False)
class GeospatialMeasure:
    """A geospatial measure dictionary with full-precision decimal coordinates (``/Subtype /GEO``, Table 269, PDF 2.0).

    ``gpts`` (``/GPTS``), ``lpts`` (``/LPTS``), ``bounds`` (``/Bounds``), and
    ``pcsm`` (``/PCSM``) hold :class:`~decimal.Decimal` values that write their
    exact positional digits into the PDF object store without ``float`` rounding.
    Each local point must have a matching ground point. By default, local pairs
    lie in the unit square. Set ``dimensions=3`` for a declared Geospatial3D
    annotation's world-coordinate triples, with ground elevation last.
    """

    gcs: CoordinateSystem
    gpts: tuple[Decimal, ...]
    dcs: CoordinateSystem | None
    lpts: tuple[Decimal, ...]
    dimensions: Literal[2, 3]
    bounds: tuple[Decimal, ...] | None
    pdu: tuple[str, str, str] | None
    pcsm: tuple[Decimal, ...] | None

    def __init__(  # noqa: PLR0913 - ISO 32000-2 Table 269 MeasureGEO fields.
        self,
        gcs: CoordinateSystem,
        gpts: Iterable[Decimal | Real | float | str],
        *,
        dcs: CoordinateSystem | None = None,
        lpts: Iterable[Decimal | Real | float | str],
        dimensions: Literal[2, 3] = 2,
        bounds: Iterable[Decimal | Real | float | str] | None = None,
        pdu: Sequence[str] | None = None,
        pcsm: Iterable[Decimal | Real | float | str] | None = None,
    ) -> None:
        """Validate coordinate systems, coordinate arrays, ``pdu``, and ``pcsm``."""
        if not isinstance(gcs, CoordinateSystem):
            msg = f"gcs must be a CoordinateSystem: {type(gcs).__name__}"
            raise TypeError(msg)
        if dcs is not None and not isinstance(dcs, CoordinateSystem):
            msg = f"dcs must be a CoordinateSystem or None: {type(dcs).__name__}"
            raise TypeError(msg)

        if type(dimensions) is not int or dimensions not in (2, 3):
            msg = "dimensions must be 2 or 3"
            raise ValueError(msg)
        checked_gpts = _validate_decimal_tuple(gpts, "gpts", max_len=_MAX_GEO_NUMBERS, even=dimensions == _PAIR)
        checked_lpts = _validate_decimal_tuple(lpts, "lpts", max_len=_MAX_GEO_NUMBERS)
        if len(checked_gpts) % dimensions:
            msg = "3D gpts must contain complete triples"
            raise ValueError(msg)
        if len(checked_lpts) != len(checked_gpts):
            msg = f"lpts length ({len(checked_lpts)}) must match gpts length ({len(checked_gpts)})"
            raise ValueError(msg)
        if dimensions == _PAIR and any(not 0 <= number <= 1 for number in checked_lpts):
            msg = "2D lpts coordinates must be in the unit square"
            raise ValueError(msg)

        checked_bounds = (
            None if bounds is None else _validate_decimal_tuple(bounds, "bounds", max_len=_MAX_GEO_NUMBERS, even=True)
        )
        checked_pdu = _validate_pdu(pdu)
        checked_pcsm = (
            None if pcsm is None else _validate_decimal_tuple(pcsm, "pcsm", max_len=_PCSM_LEN, exact_len=_PCSM_LEN)
        )

        object.__setattr__(self, "gcs", gcs)
        object.__setattr__(self, "gpts", checked_gpts)
        object.__setattr__(self, "dimensions", dimensions)
        object.__setattr__(self, "dcs", dcs)
        object.__setattr__(self, "lpts", checked_lpts)
        object.__setattr__(self, "bounds", checked_bounds)
        object.__setattr__(self, "pdu", checked_pdu)
        object.__setattr__(self, "pcsm", checked_pcsm)


def _validate_point_names(names: object) -> tuple[_PointName, ...]:
    if isinstance(names, (str, bytes, bytearray, memoryview)) or not isinstance(names, Iterable):
        msg = f"names must be an iterable of 'LAT', 'LON', or 'ALT' strings, not {type(names).__name__}"
        raise TypeError(msg)
    raw_names = list(itertools.islice(names, _MAX_POINT_NAMES + 1))
    if len(raw_names) > _MAX_POINT_NAMES:
        msg = f"names cannot contain more than {_MAX_POINT_NAMES} entries"
        raise ValueError(msg)
    if not raw_names:
        msg = "names must contain at least one attribute name"
        raise ValueError(msg)
    checked_names: list[_PointName] = []
    for index, item in enumerate(raw_names):
        if not isinstance(item, str):
            msg = f"names[{index}] must be a str: {item!r}"
            raise TypeError(msg)
        plain = str.__str__(item)
        if plain not in _POINT_NAMES:
            msg = f"names[{index}] must be 'LAT', 'LON', or 'ALT': {item!r}"
            raise ValueError(msg)
        checked_names.append(cast("_PointName", plain))
    return tuple(checked_names)


def _validate_point_rows(points: object, width: int) -> tuple[tuple[Decimal | None, ...], ...]:
    if isinstance(points, (str, bytes, bytearray, memoryview)) or not isinstance(points, Iterable):
        msg = f"points must be an iterable of point rows, not {type(points).__name__}"
        raise TypeError(msg)
    raw_rows = list(itertools.islice(points, _MAX_POINT_DATA_ROWS + 1))
    if len(raw_rows) > _MAX_POINT_DATA_ROWS:
        msg = f"points cannot contain more than {_MAX_POINT_DATA_ROWS} rows"
        raise ValueError(msg)
    checked_rows: list[tuple[Decimal | None, ...]] = []
    for row_idx, row in enumerate(raw_rows):
        if isinstance(row, (str, bytes, bytearray, memoryview)) or not isinstance(row, Sequence):
            msg = f"points[{row_idx}] must be a sequence of {width} values: {row!r}"
            raise TypeError(msg)
        if len(row) != width:
            msg = f"points[{row_idx}] length ({len(row)}) must match names length ({width})"
            raise ValueError(msg)
        checked_rows.append(
            tuple(
                None if cell is None else _to_decimal(cell, f"points[{row_idx}][{col_idx}]")
                for col_idx, cell in enumerate(row)
            )
        )
    return tuple(checked_rows)


@dataclass(frozen=True, slots=True, init=False)
class PointData:
    """Extended geospatial point cloud attributes attached to a viewport (``/Type /PtData``, Table 272, PDF 2.0).

    ``names`` lists one or more standard attribute identifiers from ``"LAT"``,
    ``"LON"``, and ``"ALT"`` (``/Names``), and each row of ``points`` (``/XPTS``)
    supplies one exact :class:`~decimal.Decimal` (or ``None`` for ``null``) per
    entry in ``names``.
    """

    names: tuple[_PointName, ...]
    points: tuple[tuple[Decimal | None, ...], ...]

    def __init__(
        self,
        names: Iterable[Literal["LAT", "LON", "ALT"]],
        points: Iterable[Sequence[Decimal | Real | float | str | None]],
    ) -> None:
        """Validate ``names`` and ``points`` rows."""
        checked_names = _validate_point_names(names)
        checked_rows = _validate_point_rows(points, len(checked_names))
        object.__setattr__(self, "names", checked_names)
        object.__setattr__(self, "points", checked_rows)


@dataclass(frozen=True, slots=True, init=False)
class Viewport:
    """A rectangular measurement region on a page (``/Type /Viewport``, ISO 32000-2 Table 265).

    ``bbox`` is a non-empty :class:`Rect` in the page's display space (points
    from the top-left of the visible page, y downward, honoring ``/CropBox``,
    ``/Rotate``, and ``/UserUnit``). ``measure`` holds a
    :class:`RectilinearMeasure` or :class:`GeospatialMeasure`, and ``point_data``
    holds optional :class:`PointData`.
    """

    bbox: Rect
    name: str | None
    measure: RectilinearMeasure | GeospatialMeasure | None
    point_data: PointData | None

    def __init__(
        self,
        bbox: Rect | Sequence[float],
        *,
        name: str | None = None,
        measure: RectilinearMeasure | GeospatialMeasure | None = None,
        point_data: PointData | None = None,
    ) -> None:
        """Validate ``bbox``, ``name``, ``measure``, and ``point_data``."""
        checked_bbox = _display_rect(bbox)
        checked_name = None if name is None else _validate_str(name, "name", allow_empty=False)
        if measure is not None and not isinstance(measure, (RectilinearMeasure, GeospatialMeasure)):
            msg = f"measure must be a RectilinearMeasure, GeospatialMeasure, or None: {type(measure).__name__}"
            raise TypeError(msg)
        if isinstance(measure, GeospatialMeasure) and measure.dimensions != _PAIR:
            msg = "a viewport requires a 2D geospatial registration"
            raise ValueError(msg)
        if point_data is not None and not isinstance(point_data, PointData):
            msg = f"point_data must be a PointData or None: {type(point_data).__name__}"
            raise TypeError(msg)

        object.__setattr__(self, "bbox", checked_bbox)
        object.__setattr__(self, "name", checked_name)
        object.__setattr__(self, "measure", measure)
        object.__setattr__(self, "point_data", point_data)


class RequirementType(enum.StrEnum):
    """Standard viewer capability requirement names for :attr:`Document.requirements` (ISO 32000-2 Table 275)."""

    ENABLE_JAVASCRIPTS = "EnableJavaScripts"
    """ECMAScript execution support (PDF 1.7)."""
    OC_INTERACT = "OCInteract"
    """Interactive optional-content group switching (PDF 2.0)."""
    OC_AUTO_STATES = "OCAutoStates"
    """Event-driven optional-content usage application dictionaries (PDF 2.0)."""
    ACROFORM_INTERACT = "AcroFormInteract"
    """Interactive AcroForm field filling (PDF 2.0)."""
    NAVIGATION = "Navigation"
    """Sub-page navigation node support (PDF 2.0)."""
    MARKUP = "Markup"
    """Markup annotation interaction (PDF 2.0)."""
    MARKUP_3D = "3DMarkup"
    """3D markup views and comments (PDF 2.0)."""
    MULTIMEDIA = "Multimedia"
    """Multimedia rendition playback (PDF 2.0)."""
    U3D = "U3D"
    """Universal 3D artwork rendering (PDF 2.0)."""
    PRC = "PRC"
    """Product Representation Compact 3D artwork rendering (PDF 2.0)."""
    ACTION = "Action"
    """Action dictionary execution (PDF 2.0)."""
    ATTACHMENT = "Attachment"
    """Embedded file attachment viewing and extraction (PDF 2.0)."""
    ATTACHMENT_EDITING = "AttachmentEditing"
    """Adding, modifying, or removing embedded file attachments (PDF 2.0)."""
    COLLECTION = "Collection"
    """Portable collection presentation (PDF 2.0)."""
    COLLECTION_EDITING = "CollectionEditing"
    """Modifying portable collection items and schema (PDF 2.0)."""
    DIGSIG_VALIDATION = "DigSigValidation"
    """Digital signature verification (PDF 2.0)."""
    DIGSIG = "DigSig"
    """Applying digital signatures (PDF 2.0)."""
    DIGSIG_MDP = "DigSigMDP"
    """DocMDP and FieldMDP modification detection enforcement (PDF 2.0)."""
    RICH_MEDIA = "RichMedia"
    """RichMedia annotation playback (PDF 2.0)."""
    GEOSPATIAL_2D = "Geospatial2D"
    """2D geospatial measurement and coordinate transformation (PDF 2.0)."""
    GEOSPATIAL_3D = "Geospatial3D"
    """3D geospatial measurement and coordinate transformation (PDF 2.0)."""
    DPART_INTERACT = "DPartInteract"
    """Document parts (DPart) navigation and metadata interaction (PDF 2.0)."""
    SEPARATION_SIMULATION = "SeparationSimulation"
    """Spot color and overprint separation preview (PDF 2.0)."""
    TRANSITIONS = "Transitions"
    """Slide transition presentation effects (PDF 2.0)."""
    ENCRYPTION = "Encryption"
    """Cryptographic security handler support (PDF 2.0)."""
    GLTF = "glTF"
    """glTF 2.0 3D asset support per ISO/TS 32007 (PDF 2.0)."""


@dataclass(frozen=True, slots=True, init=False)
class RequirementHandler:
    """Fallback handler invoked when a viewer cannot satisfy a :class:`DocumentRequirement` (Table 276).

    ``kind`` is ``"NoOp"`` (``/S /NoOp``) or ``"JS"`` (``/S /JS``); ``script``
    supplies an optional ECMAScript string (``/Script``) when ``kind`` is ``"JS"``.
    """

    kind: Literal["JS", "NoOp"]
    script: str | None

    def __init__(
        self,
        kind: Literal["JS", "NoOp"] = "NoOp",
        *,
        script: str | None = None,
    ) -> None:
        """Validate ``kind`` and ``script``."""
        if not isinstance(kind, str):
            msg = f"kind must be 'JS' or 'NoOp': {kind!r}"
            raise TypeError(msg)
        plain_kind = str.__str__(kind)
        if plain_kind not in {"JS", "NoOp"}:
            msg = f"kind must be 'JS' or 'NoOp': {kind!r}"
            raise ValueError(msg)
        checked_script: str | None = None
        if script is not None:
            if plain_kind == "NoOp":
                msg = "script (/Script) can be set only when kind is 'JS'"
                raise ValueError(msg)
            checked_script = _validate_script(script, "script")

        object.__setattr__(self, "kind", plain_kind)
        object.__setattr__(self, "script", checked_script)


def _validate_handlers(handlers: object) -> tuple[RequirementHandler, ...]:
    if isinstance(handlers, (str, bytes, bytearray, memoryview)) or not isinstance(handlers, Iterable):
        msg = f"handlers must be an iterable of RequirementHandler values, not {builtins_type_name(handlers)}"
        raise TypeError(msg)
    raw_handlers = list(itertools.islice(handlers, _MAX_HANDLERS + 1))
    if len(raw_handlers) > _MAX_HANDLERS:
        msg = f"handlers cannot contain more than {_MAX_HANDLERS} entries"
        raise ValueError(msg)
    script_bytes = 0
    for index, handler in enumerate(raw_handlers):
        if not isinstance(handler, RequirementHandler):
            msg = f"handlers[{index}] must be a RequirementHandler: {builtins_type_name(handler)}"
            raise TypeError(msg)
        if handler.script is not None:
            script_bytes += len(handler.script.encode("utf-8", "surrogatepass"))
            if script_bytes > _MAX_SCRIPT_SIZE:
                code: LimitCode = "script_size"
                msg = f"requirement handler scripts exceed the {_MAX_SCRIPT_SIZE}-byte safety limit"
                raise LimitError(code, msg)
    return tuple(raw_handlers)


@dataclass(frozen=True, slots=True, init=False)
class DocumentRequirement:
    """One viewer capability requirement in ``Catalog['Requirements']`` (ISO 32000-2 Table 273).

    ``type`` names the required feature (``/S``), ``handlers`` lists fallback
    :class:`RequirementHandler` entries (``/RH``), ``penalty`` is an integer
    severity from ``0`` to ``100`` (``/Penalty``, PDF 2.0), and ``version`` is
    an optional specification version PDF name (``/V``, PDF 2.0; defaults to
    ``"2.0"`` for :attr:`RequirementType.GLTF`).
    """

    type: RequirementType
    handlers: tuple[RequirementHandler, ...]
    penalty: int | None
    version: str | None

    def __init__(
        self,
        type: RequirementType | str,  # noqa: A002 - matches ISO 32000-2 requirement terminology and other pyropdf records.
        *,
        handlers: Iterable[RequirementHandler] = (),
        penalty: int | None = None,
        version: str | None = None,
    ) -> None:
        """Validate requirement type, fallback handlers, penalty, and version."""
        if not isinstance(type, str):
            msg = f"type must be a RequirementType or str: {type!r}"
            raise TypeError(msg)
        checked_type = RequirementType(type)
        checked_handlers = _validate_handlers(handlers)

        checked_penalty: int | None = None
        if penalty is not None:
            if not _is_non_bool_int(penalty):
                msg = f"penalty must be an int or None: {penalty!r}"
                raise TypeError(msg)
            if not 0 <= penalty <= _MAX_PENALTY:
                msg = f"penalty must be between 0 and 100: {penalty}"
                raise ValueError(msg)
            checked_penalty = int(penalty)

        checked_version = None if version is None else _validate_pdf_name(version, "version")
        if checked_type is RequirementType.GLTF:
            if checked_version is None:
                checked_version = "2.0"
            elif checked_version != "2.0":
                msg = f"glTF requirement version (/V) must be '2.0': {version!r}"
                raise ValueError(msg)

        object.__setattr__(self, "type", checked_type)
        object.__setattr__(self, "handlers", checked_handlers)
        object.__setattr__(self, "penalty", checked_penalty)
        object.__setattr__(self, "version", checked_version)


def builtins_type_name(value: object) -> str:
    return value.__class__.__name__


def _read_opt_text(raw: object) -> str | None:
    return raw.text if isinstance(raw, String) else None


def _read_number_format(raw: object, *, is_last: bool) -> NumberFormat | None:
    if not _is_dict(raw):
        return None
    d = cast("Dictionary", raw)
    unit = _read_opt_text(d.get("U"))
    factor = _read_pdf_decimal(d.get("C"))
    if not unit or factor is None:
        return None

    fraction: NumberFormatFraction | None = None
    denominator: int | None = None
    force_denominator: bool | None = None
    if is_last:
        raw_f = d.get("F")
        if isinstance(raw_f, Name):
            try:
                fraction = NumberFormatFraction(str(raw_f))
            except ValueError:
                fraction = None
        denominator = _read_bounded_int(d.get("D"), 1, _INT64_MAX)
        raw_fd = d.get("FD")
        if isinstance(raw_fd, bool):
            force_denominator = raw_fd

    raw_o = d.get("O")
    label_position: Literal["S", "P"] | None = None
    if isinstance(raw_o, Name) and str(raw_o) in {"S", "P"}:
        label_position = cast("Literal['S', 'P']", str(raw_o))

    try:
        return NumberFormat(
            unit=unit,
            factor=factor,
            fraction=fraction,
            denominator=denominator,
            force_denominator=force_denominator,
            thousands_separator=_read_opt_text(d.get("RT")),
            decimal_separator=_read_opt_text(d.get("RD")),
            prefix_spacing=_read_opt_text(d.get("PS")),
            suffix_spacing=_read_opt_text(d.get("SS")),
            label_position=label_position,
        )
    except (ValueError, LimitError):
        return None


def _read_number_format_chain(raw: object) -> tuple[NumberFormat, ...] | None:
    if not isinstance(raw, Array) or len(raw) == 0:
        return None
    if len(raw) > _MAX_NUMBER_FORMATS:
        code: LimitCode = "page_entry_count"
        msg = f"number format array exceeds the {_MAX_NUMBER_FORMATS}-entry safety limit"
        raise LimitError(code, msg)
    raw_items = list(raw)
    parsed: list[NumberFormat] = []
    # Parse initially allowing last-element fields so we can ensure only the final surviving entry keeps them.
    for item in raw_items:
        nf = _read_number_format(item, is_last=True)
        if nf is not None:
            parsed.append(nf)
    if not parsed:
        return None
    normalized: list[NumberFormat] = []
    for nf in parsed[:-1]:
        if nf.fraction is not None or nf.denominator is not None or nf.force_denominator is not None:
            normalized.append(
                NumberFormat(
                    unit=nf.unit,
                    factor=nf.factor,
                    thousands_separator=nf.thousands_separator,
                    decimal_separator=nf.decimal_separator,
                    prefix_spacing=nf.prefix_spacing,
                    suffix_spacing=nf.suffix_spacing,
                    label_position=nf.label_position,
                )
            )
        else:
            normalized.append(nf)
    normalized.append(parsed[-1])
    return tuple(normalized)


def _read_rectilinear_measure(d: Dictionary) -> RectilinearMeasure | None:
    ratio = _read_opt_text(d.get("R"))
    x = _read_number_format_chain(d.get("X"))
    distance = _read_number_format_chain(d.get("D"))
    area = _read_number_format_chain(d.get("A"))
    if not ratio or x is None or distance is None or area is None:
        return None
    y = _read_number_format_chain(d.get("Y"))
    angles = _read_number_format_chain(d.get("T"))
    slope = _read_number_format_chain(d.get("S"))

    origin: tuple[Decimal, Decimal] | None = None
    raw_o = d.get("O")
    if isinstance(raw_o, Array) and len(raw_o) == _ORIGIN_LEN:
        ox = _read_pdf_decimal(raw_o[0])
        oy = _read_pdf_decimal(raw_o[1])
        if ox is not None and oy is not None:
            origin = (ox, oy)

    cyx = _read_pdf_decimal(d.get("CYX")) if y is not None else None
    try:
        return RectilinearMeasure(
            scale_ratio=ratio,
            x=x,
            distance=distance,
            area=area,
            y=y,
            angles=angles,
            slope=slope,
            origin=origin,
            cyx=cyx,
        )
    except (ValueError, LimitError):
        return None


def _read_coordinate_system(raw: object) -> CoordinateSystem | None:
    if not _is_dict(raw):
        return None
    d = cast("Dictionary", raw)
    raw_type = d.get("Type")
    if not isinstance(raw_type, Name):
        return None
    type_str = str(raw_type)
    if type_str == "GEOGCS":
        kind: Literal["geographic", "projected"] = "geographic"
    elif type_str == "PROJCS":
        kind = "projected"
    else:
        return None

    epsg = _read_bounded_int(d.get("EPSG"), 1, _INT64_MAX)

    wkt: str | None = None
    if epsg is None:
        raw_wkt = _read_opt_text(d.get("WKT"))
        if raw_wkt and raw_wkt.isascii() and raw_wkt.isprintable():
            wkt = raw_wkt

    if kind == "projected" and epsg is None and wkt is None:
        return None
    try:
        return CoordinateSystem(kind=kind, epsg=epsg, wkt=wkt)
    except (ValueError, LimitError):
        return None


def _read_decimal_array(
    raw: object, *, max_len: int, even: bool = False, exact_len: int | None = None
) -> tuple[Decimal, ...] | None:
    if not isinstance(raw, Array):
        return None
    if exact_len is not None:
        if len(raw) != exact_len:
            return None
    else:
        if len(raw) > max_len:
            code: LimitCode = "page_entry_count"
            msg = f"geospatial coordinate array exceeds the {max_len}-number safety limit"
            raise LimitError(code, msg)
        if len(raw) == 0 or (even and len(raw) % 2 != 0):
            return None
    out: list[Decimal] = []
    for item in raw:
        dec = _read_pdf_decimal(item)
        if dec is None:
            return None
        out.append(dec)
    return tuple(out)


def _read_geospatial_measure(d: Dictionary, dimensions: Literal[2, 3] = 2) -> GeospatialMeasure | None:
    gcs = _read_coordinate_system(d.get("GCS"))
    gpts = _read_decimal_array(d.get("GPTS"), max_len=_MAX_GEO_NUMBERS)
    if gcs is None or gpts is None:
        return None
    dcs = _read_coordinate_system(d.get("DCS"))
    lpts = _read_decimal_array(d.get("LPTS"), max_len=_MAX_GEO_NUMBERS)
    if lpts is None or len(lpts) != len(gpts):
        return None
    bounds = _read_decimal_array(d.get("Bounds"), max_len=_MAX_GEO_NUMBERS, even=True)

    pdu: tuple[str, str, str] | None = None
    raw_pdu = d.get("PDU")
    if isinstance(raw_pdu, Array) and len(raw_pdu) == _PDU_LEN:
        pdu_names = [str(u) for u in raw_pdu if isinstance(u, Name) and str(u) in _PDU_NAMES]
        if len(pdu_names) == _PDU_LEN:
            pdu = (pdu_names[0], pdu_names[1], pdu_names[2])

    raw_pcsm = d.get("PCSM")
    pcsm = _read_decimal_array(raw_pcsm, max_len=_PCSM_LEN, exact_len=_PCSM_LEN)
    if raw_pcsm is not None and pcsm is None and gcs.kind == "projected":
        return None
    try:
        return GeospatialMeasure(
            gcs=gcs,
            gpts=gpts,
            dcs=dcs,
            lpts=lpts,
            dimensions=dimensions,
            bounds=bounds,
            pdu=pdu,
            pcsm=pcsm,
        )
    except ValueError:
        return None


def _read_measure(raw: object) -> RectilinearMeasure | GeospatialMeasure | None:
    if not _is_dict(raw):
        return None
    d = cast("Dictionary", raw)
    subtype = d.get("Subtype")
    if isinstance(subtype, Name):
        sub_str = str(subtype)
        if sub_str == "GEO":
            return _read_geospatial_measure(d)
        if sub_str == "RL":
            return _read_rectilinear_measure(d)
        return None
    if subtype is None:
        # ISO 32000-2 Table 266 defaults omitted /Subtype to /RL.
        return _read_rectilinear_measure(d)
    return None


def _parse_point_row(raw_row: object, width: int) -> tuple[Decimal | None, ...] | None:
    if not isinstance(raw_row, Array) or len(raw_row) != width:
        return None
    cells: list[Decimal | None] = []
    for cell in raw_row:
        if cell is None:
            cells.append(None)
        else:
            dec = _read_pdf_decimal(cell)
            if dec is None:
                return None
            cells.append(dec)
    return tuple(cells)


def _read_point_data(raw: object) -> PointData | None:
    if not _is_dict(raw):
        return None
    d = cast("Dictionary", raw)
    raw_names = d.get("Names")
    raw_xpts = d.get("XPTS")
    if not isinstance(raw_names, Array) or not isinstance(raw_xpts, Array) or len(raw_names) == 0:
        return None
    if len(raw_names) > _MAX_POINT_NAMES:
        code: LimitCode = "page_entry_count"
        msg = f"point data /Names exceeds the {_MAX_POINT_NAMES}-entry safety limit"
        raise LimitError(code, msg)
    if len(raw_xpts) > _MAX_POINT_DATA_ROWS:
        code = "page_entry_count"
        msg = f"point data /XPTS exceeds the {_MAX_POINT_DATA_ROWS}-row safety limit"
        raise LimitError(code, msg)

    names: list[_PointName] = []
    for item in raw_names:
        if not isinstance(item, Name) or str(item) not in _POINT_NAMES:
            return None
        names.append(cast("_PointName", str(item)))

    width = len(names)
    rows = [parsed for raw_row in raw_xpts if (parsed := _parse_point_row(raw_row, width)) is not None]
    try:
        return PointData(names=names, points=rows)
    except ValueError:
        return None


def _read_viewport_bbox(
    crop: tuple[float, float, float, float],
    rotation: int,
    unit: float,
    raw_bbox: object,
) -> Rect | None:
    if not isinstance(raw_bbox, Array) or len(raw_bbox) != _RECT_COORDS:
        return None
    nums: list[float] = []
    for item in raw_bbox:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            return None
        nums.append(float(item))
    dx0, dy0 = _to_display(crop, rotation, unit, nums[0], nums[1])
    dx1, dy1 = _to_display(crop, rotation, unit, nums[2], nums[3])
    x0, x1 = (dx0, dx1) if dx0 <= dx1 else (dx1, dx0)
    y0, y1 = (dy0, dy1) if dy0 <= dy1 else (dy1, dy0)
    if not (x0 < x1 and y0 < y1) or not all(math.isfinite(c) and abs(c) <= _FLOAT32_MAX for c in (x0, y0, x1, y1)):
        return None
    return Rect(x0, y0, x1, y1)


def _serialize_number_format(nf: NumberFormat) -> dict[str, object]:
    entry: dict[str, object] = {
        "Type": Name("NumberFormat"),
        "U": nf.unit,
        "C": nf.factor,
    }
    if nf.fraction is not None:
        entry["F"] = Name(nf.fraction.value)
    if nf.denominator is not None:
        entry["D"] = nf.denominator
    if nf.force_denominator is not None:
        entry["FD"] = nf.force_denominator
    if nf.thousands_separator is not None:
        entry["RT"] = nf.thousands_separator
    if nf.decimal_separator is not None:
        entry["RD"] = nf.decimal_separator
    if nf.prefix_spacing is not None:
        entry["PS"] = nf.prefix_spacing
    if nf.suffix_spacing is not None:
        entry["SS"] = nf.suffix_spacing
    if nf.label_position is not None:
        entry["O"] = Name(nf.label_position)
    return entry


def _serialize_crs(crs: CoordinateSystem) -> dict[str, object]:
    entry: dict[str, object] = {
        "Type": Name("GEOGCS" if crs.kind == "geographic" else "PROJCS"),
    }
    if crs.epsg is not None:
        entry["EPSG"] = crs.epsg
    if crs.wkt is not None:
        entry["WKT"] = crs.wkt
    return entry


def _serialize_rectilinear(measure: RectilinearMeasure) -> dict[str, object]:
    rl: dict[str, object] = {
        "Type": Name("Measure"),
        "Subtype": Name("RL"),
        "R": measure.scale_ratio,
        "X": [_serialize_number_format(nf) for nf in measure.x],
        "D": [_serialize_number_format(nf) for nf in measure.distance],
        "A": [_serialize_number_format(nf) for nf in measure.area],
    }
    if measure.y is not None:
        rl["Y"] = [_serialize_number_format(nf) for nf in measure.y]
    if measure.angles is not None:
        rl["T"] = [_serialize_number_format(nf) for nf in measure.angles]
    if measure.slope is not None:
        rl["S"] = [_serialize_number_format(nf) for nf in measure.slope]
    if measure.origin is not None:
        rl["O"] = [measure.origin[0], measure.origin[1]]
    if measure.cyx is not None:
        rl["CYX"] = measure.cyx
    return rl


def _serialize_measure(measure: RectilinearMeasure | GeospatialMeasure) -> dict[str, object]:
    if isinstance(measure, RectilinearMeasure):
        return _serialize_rectilinear(measure)

    geo: dict[str, object] = {
        "Type": Name("Measure"),
        "Subtype": Name("GEO"),
        "GCS": _serialize_crs(measure.gcs),
        "GPTS": list(measure.gpts),
    }
    if measure.dcs is not None:
        geo["DCS"] = _serialize_crs(measure.dcs)
    geo["LPTS"] = list(measure.lpts)
    if measure.bounds is not None:
        geo["Bounds"] = list(measure.bounds)
    if measure.pdu is not None:
        geo["PDU"] = [Name(unit) for unit in measure.pdu]
    if measure.pcsm is not None:
        geo["PCSM"] = list(measure.pcsm)
    return geo


def _is_untyped_viewport(owner: Dictionary) -> bool:
    # /Type is optional. Only its position in a page's /VP array
    # distinguishes an untyped viewport from another dictionary with /BBox.
    remaining = _MAX_CANDIDATE_NODES
    code: LimitCode = "page_entry_count"
    for page in owner._document.pages:  # noqa: SLF001
        viewports = page.obj.get("VP")
        if isinstance(viewports, Array):
            for candidate in viewports:
                if remaining == 0:
                    msg = "too many entries while locating an untyped viewport"
                    raise LimitError(code, msg)
                remaining -= 1
                if candidate == owner:
                    return True
    return False


def _registration_owner(owner: Dictionary) -> tuple[str, Literal[2, 3]] | None:
    kind, subtype = owner.get("Type"), owner.get("Subtype")
    if isinstance(owner, Stream):
        if kind in (None, Name("XObject")) and subtype in (Name("Form"), Name("Image")):
            return "Measure", 2
    elif kind == Name("Viewport") or (
        kind is None and subtype is None and "BBox" in owner and _is_untyped_viewport(owner)
    ):
        return "Measure", 2
    elif (
        (kind == Name("Annot") or (kind is None and "Rect" in owner))
        and subtype in (Name("3D"), Name("RichMedia"))
        and any(req.type == RequirementType.GEOSPATIAL_3D for req in owner._document.requirements)  # noqa: SLF001
    ):
        return "GEO", 3
    return None


def _owner_measure(owner: Dictionary) -> GeospatialMeasure | None:
    slot = _registration_owner(owner)
    if slot is None:
        return None
    key, dimensions = slot
    value = owner.get(key)
    if isinstance(value, Dictionary) and not isinstance(value, Stream) and value.get("Subtype") == Name("GEO"):
        return _read_geospatial_measure(value, dimensions)
    return None


def _registration_version(doc: Document, value: GeospatialMeasure) -> tuple[int, int]:
    # Adobe's PDF 1.7 extension level 3 defines the latitude/longitude
    # mapping. Projected ground points, PCSM and 3D triples use PDF 2.0.
    if value.dimensions == _PAIR and value.gcs.kind == "geographic" and value.pcsm is None:
        return _base_or_extension(doc, 3)
    return (2, 0)


def _set_owner_measure(owner: Dictionary, value: GeospatialMeasure) -> None:
    if not isinstance(value, GeospatialMeasure):
        msg = "geospatial_measure must be a GeospatialMeasure"
        raise TypeError(msg)
    doc = owner._document  # noqa: SLF001
    with doc.transaction("geospatial measure"):
        slot = _registration_owner(owner)
        if slot is None:
            msg = "expected a viewport, image, form, or annotation with a declared Geospatial3D requirement"
            raise ValueError(msg)
        key, dimensions = slot
        if value.dimensions != dimensions:
            msg = f"this owner requires a {dimensions}D registration"
            raise ValueError(msg)
        _require_measurement_version(doc, doc.catalog, _registration_version(doc, value))
        previous = owner.get(key)
        if isinstance(previous, Dictionary) and not isinstance(previous, Stream) and previous.get("Subtype") == "GEO":
            _replace_registration(doc, owner, key, previous, value)
        else:
            candidates = _collect_indirect_candidates(previous)
            owner[key] = _serialize_measure(value)
            if candidates:
                _core.object_free_unreferenced(doc._core(), candidates)  # noqa: SLF001


def _replace_crs(doc: Document, target: Dictionary, name: str, fields: dict[str, object]) -> None:
    current = target.get(name)
    if not isinstance(current, Dictionary) or isinstance(current, Stream):
        target[name] = fields
        return
    if current.is_indirect:
        target[name] = doc.objects[doc.objects.add(current)]
    entry = target[name]
    for key in ("EPSG", "WKT"):
        if key not in fields and key in entry:
            del entry[key]
    for key, field in fields.items():
        entry[key] = field


def _replace_registration(
    doc: Document, owner: Dictionary, key: str, previous: Dictionary, value: GeospatialMeasure
) -> None:
    # Copy a shared indirect measurement before editing it. Direct values
    # already belong to this owner. Unchanged references and private entries
    # in the measurement survive replacement of the controls.
    original = _read_geospatial_measure(previous, value.dimensions)
    candidates = _collect_indirect_candidates(previous)
    if previous.is_indirect:
        owner[key] = doc.objects[doc.objects.add(previous)]
    target = owner[key]
    fields = _serialize_measure(value)
    for name in ("DCS", "Bounds", "PDU", "PCSM"):
        if name not in fields and name in target:
            del target[name]
    for name, field in fields.items():
        if original is not None and name not in ("Type", "Subtype"):
            attribute = name.lower()
            if getattr(original, attribute) == getattr(value, attribute):
                continue
        if name in ("GCS", "DCS") and isinstance(field, dict):
            _replace_crs(doc, target, name, field)
        else:
            target[name] = field
    if candidates:
        _core.object_free_unreferenced(doc._core(), candidates)  # noqa: SLF001


def _owner_point(owner: Dictionary, index: int) -> tuple[tuple[Decimal, ...], tuple[Decimal, ...]]:
    registration = _owner_measure(owner)
    if registration is None:
        msg = "owner has no valid geospatial registration"
        raise ValueError(msg)
    if not isinstance(index, int) or isinstance(index, bool):
        msg = "registration point index must be an integer"
        raise TypeError(msg)
    dimensions = registration.dimensions
    count = len(registration.lpts) // dimensions
    if not -count <= index < count:
        msg = f"registration point index {index} out of range"
        raise IndexError(msg)
    start = (index % count) * dimensions
    local = registration.lpts[start : start + dimensions]
    ground = registration.gpts[start : start + dimensions]
    # A bounds interpolation followed by a matrix product may multiply three
    # PDF decimals. Keep all positional digits, independent of the caller's
    # Decimal context, including the carries from the final additions.
    with localcontext(Context(prec=6 * _MAX_DECIMAL_LEXEME_BYTES + 8)):
        if dimensions == _PAIR and not (isinstance(owner, Stream) and owner.get("Subtype") == "Image"):
            box = _read_decimal_array(owner.get("BBox"), max_len=_RECT_COORDS, exact_len=_RECT_COORDS)
            if box is None:
                msg = "viewport or form must have a numeric BBox"
                raise ValueError(msg)
            x0, y0, x1, y1 = box
            local = (min(x0, x1) + abs(x1 - x0) * local[0], min(y0, y1) + abs(y1 - y0) * local[1])
        matrix = registration.pcsm
        if matrix is not None and registration.gcs.kind == "projected":
            x, y = local[:2]
            z = local[2] if dimensions == _TRIPLE else Decimal(0)
            ground = tuple(
                x * matrix[i] + y * matrix[3 + i] + z * matrix[6 + i] + matrix[9 + i] for i in range(dimensions)
            )
    return local, ground


def _serialize_point_data(pt: PointData) -> dict[str, object]:
    return {
        "Type": Name("PtData"),
        "Subtype": Name("Cloud"),
        "Names": [Name(name) for name in pt.names],
        "XPTS": [list(row) for row in pt.points],
    }


def _read_page_viewports(page: Page) -> tuple[Viewport, ...]:
    document = page.document
    space = _core.page_space(document._core(), page.xref, page._generation)  # noqa: SLF001
    if space is None:
        raise page._stale()  # noqa: SLF001
    rotation, crop, unit = space
    raw_vp = page.obj.get("VP")
    if not isinstance(raw_vp, Array):
        return ()
    if len(raw_vp) > _MAX_VIEWPORTS:
        code: LimitCode = "page_entry_count"
        msg = f"page /VP exceeds the {_MAX_VIEWPORTS}-viewport safety limit"
        raise LimitError(code, msg)

    viewports: list[Viewport] = []
    for item in raw_vp:
        if not _is_dict(item):
            continue
        d = cast("Dictionary", item)
        bbox = _read_viewport_bbox(crop, rotation, unit, d.get("BBox"))
        if bbox is None:
            continue
        raw_name = _read_opt_text(d.get("Name"))
        name = raw_name or None
        measure = _read_measure(d.get("Measure"))
        point_data = _read_point_data(d.get("PtData"))
        try:
            viewports.append(Viewport(bbox=bbox, name=name, measure=measure, point_data=point_data))
        except (ValueError, LimitError):
            continue
    return tuple(viewports)


def _validate_viewports_iterable(viewports: Iterable[Viewport]) -> list[Viewport]:
    if isinstance(viewports, (str, bytes, bytearray, memoryview)) or not isinstance(viewports, Iterable):
        msg = f"viewports must be an iterable of Viewport records, not {type(viewports).__name__}"
        raise TypeError(msg)
    items = list(itertools.islice(viewports, _MAX_VIEWPORTS + 1))
    if len(items) > _MAX_VIEWPORTS:
        msg = f"viewports cannot contain more than {_MAX_VIEWPORTS} entries"
        raise ValueError(msg)
    budget = [0]
    for index, vp in enumerate(items):
        if not isinstance(vp, Viewport):
            msg = f"viewports[{index}] must be a Viewport: {type(vp).__name__}"
            raise TypeError(msg)
        if vp.name is not None:
            _validate_str(vp.name, "viewport name", allow_empty=False, budget=budget)
        if isinstance(vp.measure, RectilinearMeasure):
            _validate_str(vp.measure.scale_ratio, "scale_ratio", allow_empty=False, budget=budget)
        elif isinstance(vp.measure, GeospatialMeasure):
            for crs in (vp.measure.gcs, vp.measure.dcs):
                if crs is not None and crs.wkt is not None:
                    _validate_str(crs.wkt, "wkt", allow_empty=False, budget=budget)
    return items


def _write_page_viewports(page: Page, viewports: Iterable[Viewport]) -> None:
    items = _validate_viewports_iterable(viewports)
    _ = page.index
    if not items and "VP" not in page.obj:
        return

    document = page.document
    with document.transaction("page viewports"):
        _ = page.index
        serialized: list[dict[str, object]] = []
        minimum = (1, 6)
        for vp in items:
            vp_dict: dict[str, object] = {
                "Type": Name("Viewport"),
                "BBox": _display_to_pdf_rect(document, page, vp.bbox),
            }
            if vp.name is not None:
                vp_dict["Name"] = vp.name
            if isinstance(vp.measure, GeospatialMeasure):
                minimum = max(minimum, _registration_version(document, vp.measure))
            if vp.measure is not None:
                vp_dict["Measure"] = _serialize_measure(vp.measure)
            if vp.point_data is not None:
                minimum = (2, 0)
                vp_dict["PtData"] = _serialize_point_data(vp.point_data)
            serialized.append(vp_dict)

        candidates = _collect_indirect_candidates(page.obj.get("VP"))
        if not serialized:
            if "VP" in page.obj:
                del page.obj["VP"]
        else:
            _require_measurement_version(document, _catalog(document), minimum)
            page.obj["VP"] = serialized
        if candidates:
            _core.object_free_unreferenced(document._core(), candidates)  # noqa: SLF001


def _delete_page_viewports(page: Page) -> None:
    _ = page.index
    if "VP" not in page.obj:
        return
    document = page.document
    with document.transaction("page viewports"):
        _ = page.index
        candidates = _collect_indirect_candidates(page.obj.get("VP"))
        if "VP" in page.obj:
            del page.obj["VP"]
        if candidates:
            _core.object_free_unreferenced(document._core(), candidates)  # noqa: SLF001


def _read_handler_script(raw: object) -> str | None:
    if not isinstance(raw, String):
        return None
    if len(raw) > _MAX_ENCODED_SCRIPT_SIZE:
        code: LimitCode = "script_size"
        msg = f"requirement handler script exceeds the {_MAX_SCRIPT_SIZE}-byte safety limit"
        raise LimitError(code, msg)
    return _validate_script(raw.text, "script")


def _read_requirement_handler(raw: object) -> RequirementHandler | None:
    if not _is_dict(raw):
        return None
    d = cast("Dictionary", raw)
    s_val = d.get("S")
    if not isinstance(s_val, Name) or str(s_val) not in {"JS", "NoOp"}:
        return None
    kind = cast("Literal['JS', 'NoOp']", str(s_val))
    script = _read_handler_script(d.get("Script")) if kind == "JS" else None
    return RequirementHandler(kind=kind, script=script)


def _read_requirement_handlers(raw: object) -> tuple[RequirementHandler, ...]:
    if _is_dict(raw):
        single = _read_requirement_handler(raw)
        return (single,) if single is not None else ()
    if not isinstance(raw, Array):
        return ()
    if len(raw) > _MAX_HANDLERS:
        code: LimitCode = "page_entry_count"
        msg = f"requirement /RH exceeds the {_MAX_HANDLERS}-handler safety limit"
        raise LimitError(code, msg)
    handlers: list[RequirementHandler] = []
    script_bytes = 0
    for item in raw:
        parsed = _read_requirement_handler(item)
        if parsed is not None:
            if parsed.script is not None:
                script_bytes += len(parsed.script.encode("utf-8", "surrogatepass"))
                if script_bytes > _MAX_SCRIPT_SIZE:
                    code = "script_size"
                    msg = f"requirement handler scripts exceed the {_MAX_SCRIPT_SIZE}-byte safety limit"
                    raise LimitError(code, msg)
            handlers.append(parsed)
    return tuple(handlers)


def _parse_document_requirement(item: object, script_budget: list[int]) -> DocumentRequirement | None:
    if not _is_dict(item):
        return None
    d = cast("Dictionary", item)
    s_val = d.get("S")
    if not isinstance(s_val, Name):
        return None
    try:
        req_type = RequirementType(str(s_val))
    except ValueError:
        return None
    handlers = _read_requirement_handlers(d.get("RH"))
    for h in handlers:
        if h.script is not None:
            script_budget[0] += len(h.script.encode("utf-8", "surrogatepass"))
            if script_budget[0] > _MAX_SCRIPT_SIZE:
                code: LimitCode = "script_size"
                msg = f"document requirement scripts exceed the {_MAX_SCRIPT_SIZE}-byte safety limit"
                raise LimitError(code, msg)

    penalty = _read_bounded_int(d.get("Penalty"), 0, _MAX_PENALTY)

    raw_v = d.get("V")
    version = str(raw_v) if isinstance(raw_v, Name) and str(raw_v) and "\x00" not in str(raw_v) else None
    if req_type is RequirementType.GLTF and version not in {None, "2.0"}:
        return None

    try:
        return DocumentRequirement(type=req_type, handlers=handlers, penalty=penalty, version=version)
    except ValueError:
        return None


def _read_document_requirements(document: Document) -> tuple[DocumentRequirement, ...]:
    catalog = _catalog(document)
    raw = catalog.get("Requirements")
    if not isinstance(raw, Array):
        return ()
    if len(raw) > _MAX_REQUIREMENTS:
        code: LimitCode = "page_entry_count"
        msg = f"Catalog['Requirements'] exceeds the {_MAX_REQUIREMENTS}-entry safety limit"
        raise LimitError(code, msg)

    script_budget = [0]
    return tuple(req for item in raw if (req := _parse_document_requirement(item, script_budget)) is not None)


def _serialize_handler(handler: RequirementHandler) -> dict[str, object]:
    entry: dict[str, object] = {
        "Type": Name("ReqHandler"),
        "S": Name(handler.kind),
    }
    if handler.script is not None:
        entry["Script"] = handler.script
    return entry


def _validate_requirements_iterable(requirements: Iterable[DocumentRequirement]) -> list[DocumentRequirement]:
    if isinstance(requirements, (str, bytes, bytearray, memoryview)) or not isinstance(requirements, Iterable):
        msg = f"requirements must be an iterable of DocumentRequirement records, not {type(requirements).__name__}"
        raise TypeError(msg)
    items = list(itertools.islice(requirements, _MAX_REQUIREMENTS + 1))
    if len(items) > _MAX_REQUIREMENTS:
        msg = f"requirements cannot contain more than {_MAX_REQUIREMENTS} entries"
        raise ValueError(msg)
    total_script_bytes = 0
    for index, req in enumerate(items):
        if not isinstance(req, DocumentRequirement):
            msg = f"requirements[{index}] must be a DocumentRequirement: {type(req).__name__}"
            raise TypeError(msg)
        for h in req.handlers:
            if h.script is not None:
                total_script_bytes += len(h.script.encode("utf-8", "surrogatepass"))
                if total_script_bytes > _MAX_SCRIPT_SIZE:
                    code: LimitCode = "script_size"
                    msg = f"document requirement scripts exceed the {_MAX_SCRIPT_SIZE}-byte safety limit"
                    raise LimitError(code, msg)
    return items


def _write_document_requirements(document: Document, requirements: Iterable[DocumentRequirement]) -> None:
    _unlocked(document)
    items = _validate_requirements_iterable(requirements)
    catalog = _catalog(document)
    if not items and "Requirements" not in catalog:
        return

    needs_20 = any(
        req.type is not RequirementType.ENABLE_JAVASCRIPTS or req.penalty is not None or req.version is not None
        for req in items
    )
    minimum = (2, 0) if needs_20 else (1, 7)

    serialized: list[dict[str, object]] = []
    for req in items:
        entry: dict[str, object] = {
            "Type": Name("Requirement"),
            "S": Name(req.type.value),
        }
        if req.version is not None:
            entry["V"] = Name(req.version)
        if req.handlers:
            entry["RH"] = [_serialize_handler(h) for h in req.handlers]
        if req.penalty is not None:
            entry["Penalty"] = req.penalty
        serialized.append(entry)

    with document.transaction("document requirements"):
        cat = _catalog(document)
        candidates = _collect_indirect_candidates(cat.get("Requirements"))
        if not serialized:
            if "Requirements" in cat:
                del cat["Requirements"]
        else:
            _require_measurement_version(document, cat, minimum)
            cat["Requirements"] = serialized
        if candidates:
            _core.object_free_unreferenced(document._core(), candidates)  # noqa: SLF001


def _delete_document_requirements(document: Document) -> None:
    catalog = _catalog(document)
    if "Requirements" not in catalog:
        return
    with document.transaction("document requirements"):
        cat = _catalog(document)
        candidates = _collect_indirect_candidates(cat.get("Requirements"))
        if "Requirements" in cat:
            del cat["Requirements"]
        if candidates:
            _core.object_free_unreferenced(document._core(), candidates)  # noqa: SLF001


for _public in (
    CoordinateSystem,
    DocumentRequirement,
    GeospatialMeasure,
    NumberFormat,
    NumberFormatFraction,
    PointData,
    RectilinearMeasure,
    RequirementHandler,
    RequirementType,
    Viewport,
):
    _public.__module__ = "pyropdf"

__all__ = [
    "CoordinateSystem",
    "DocumentRequirement",
    "GeospatialMeasure",
    "NumberFormat",
    "NumberFormatFraction",
    "PointData",
    "RectilinearMeasure",
    "RequirementHandler",
    "RequirementType",
    "Viewport",
]
