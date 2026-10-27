"""Point, Matrix, Rect, IRect, and Quad: immutable affine geometry in display coordinates.

pylopdf's tests/test_geometry.py and tests/test_rect_quad.py at 036be111,
ported. Their expected values were observed with pymupdf 1.28.2. MuPDF
transforms in single precision, so the examples use exactly representable
results. Intended differences from pylopdf:

- An unknown attribute raises a plain ``AttributeError``: the migration
  hints, which name a pymupdf name's replacement, come with the migration
  tooling.
- The round trips through drawing, text extraction, and search wait for
  those features. In their place, ``Page.rect`` is a ``Rect`` and
  ``page.pixmap`` takes a ``Matrix``.
"""

from __future__ import annotations

import copy
import math
import operator
import pickle
from typing import TYPE_CHECKING, get_type_hints

import pytest
from test_pixmap import boxed

import pyropdf
from pyropdf import IRect, Matrix, Point, Quad, Rect

if TYPE_CHECKING:
    from collections.abc import Callable

LIMIT_LOW = -2147483648.0
LIMIT_HIGH = 2147483520.0
UNIT_QUAD = Quad(Point(1, 2), Point(3, 2), Point(1, 4), Point(3, 4))


def _flat(quad: Quad) -> list[float]:
    return [coordinate for corner in quad for coordinate in corner]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((), (0, 0)),
        ((3, 4), (3, 4)),
        (((3, 4),), (3, 4)),
        (([3, 4],), (3, 4)),
        ((Point(3, 4),), (3, 4)),
    ],
)
def test_point_constructors(args: tuple, expected: tuple[float, float]) -> None:
    point = Point(*args)
    assert point == expected
    assert all(isinstance(value, float) for value in point)
    assert Point(x=3, y=4) == Point(3, y=4) == Point(3, 4)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((), (0, 0, 0, 0, 0, 0)),
        ((0,), (1, 0, 0, 1, 0, 0)),
        ((90,), (0, 1, -1, 0, 0, 0)),
        ((-90,), (0, -1, 1, 0, 0, 0)),
        ((450,), (0, 1, -1, 0, 0, 0)),
        ((180,), (-1, 0, 0, -1, 0, 0)),
        ((2, 3), (2, 0, 0, 3, 0, 0)),
        ((2, 3, 0), (2, 0, 0, 3, 0, 0)),
        ((2, 3, 1), (1, 3, 2, 1, 0, 0)),
        ((1, 2, 3, 4, 5, 6), (1, 2, 3, 4, 5, 6)),
        (((1, 2, 3, 4, 5, 6),), (1, 2, 3, 4, 5, 6)),
        (([1, 2, 3, 4, 5, 6],), (1, 2, 3, 4, 5, 6)),
        ((Matrix(2, 3),), (2, 0, 0, 3, 0, 0)),
    ],
)
def test_matrix_constructors(args: tuple, expected: tuple[float, ...]) -> None:
    matrix = Matrix(*args)
    assert matrix == expected
    assert all(isinstance(value, float) for value in matrix)
    assert Matrix(a=1, b=2, c=3, d=4, e=5, f=6) == Matrix(1, 2, c=3, d=4, e=5, f=6)


@pytest.mark.parametrize("angle", [30, 45, 123.5, -400.5, 1e300])
def test_rotation_uses_degrees_and_bounded_angles(angle: float) -> None:
    radians = math.radians(angle % 360)
    assert Matrix(angle) == pytest.approx(
        (math.cos(radians), math.sin(radians), -math.sin(radians), math.cos(radians), 0, 0)
    )
    assert Point(1, 0) * Matrix(90) == (0, 1)


@pytest.mark.parametrize("value", [Point(3, 4), Matrix(1, 2, 3, 4, 5, 6)])
def test_points_and_matrices_keep_named_tuple_contracts(value: Point | Matrix) -> None:
    assert isinstance(value, tuple)
    assert len(value) == len(value._fields)
    assert value[0] == next(iter(value))
    assert value[:] == tuple(value)
    assert hash(value) == hash(tuple(value))
    assert {value: "value"}[type(value)(value)] == "value"
    assert type(value).__module__ == "pyropdf"
    assert repr(value).startswith(type(value).__name__ + "(")
    for restored in (copy.copy(value), copy.deepcopy(value), pickle.loads(pickle.dumps(value))):  # noqa: S301 - locally serialized values.
        assert restored == value
        assert type(restored) is type(value)
    assert value._asdict() == dict(zip(value._fields, value, strict=True))
    assert get_type_hints(type(value)) == dict.fromkeys(value._fields, float)
    with pytest.raises(AttributeError):
        setattr(value, value._fields[0], 99)
    with pytest.raises(TypeError):
        operator.setitem(value, 0, 99)  # ty: ignore[no-matching-overload]


@pytest.mark.parametrize("point", [Point(3, -4), Point(0, 0)])
def test_point_arithmetic(point: Point) -> None:
    x, y = point
    assert point + (2, 3) == Point(x + 2, y + 3)  # noqa: RUF005 - geometric addition, not concatenation.
    assert point + 2 == Point(x + 2, y + 2)
    assert point - [2, 3] == Point(x - 2, y - 3)
    assert point - 2 == Point(x - 2, y - 2)
    assert point * 2 == Point(x * 2, y * 2)
    assert point / 2 == Point(x / 2, y / 2)
    assert -point == Point(-x, -y)
    assert +point == point
    assert bool(point) == (x != 0 or y != 0)
    assert point.norm() == abs(point) == math.hypot(x, y)


def test_point_directions() -> None:
    assert Point().unit == Point().abs_unit == (0, 0)
    assert Point(-3, 4).unit == pytest.approx((-0.6, 0.8))
    assert Point(-3, -4).abs_unit == pytest.approx((0.6, 0.8))
    assert abs(Point(1.7e308, 1.7e308).unit) == pytest.approx(1)
    assert Point(1e-300, 0).unit == (1, 0)


def test_matrix_arithmetic() -> None:
    matrix = Matrix(1, 2, 3, 4, 5, 6)
    assert matrix + 2 == (3, 4, 5, 6, 7, 8)
    assert matrix - 2 == (-1, 0, 1, 2, 3, 4)
    assert matrix + (6, 5, 4, 3, 2, 1) == (7,) * 6  # noqa: RUF005 - geometric addition, not concatenation.
    assert matrix - [1, 1, 2, 2, 3, 3] == (0, 1, 1, 2, 2, 3)
    assert matrix * 2 == (2, 4, 6, 8, 10, 12)
    assert matrix / 2 == (0.5, 1, 1.5, 2, 2.5, 3)
    assert -matrix == (-1, -2, -3, -4, -5, -6)
    assert +matrix == matrix
    assert matrix.norm() == abs(matrix) == math.sqrt(91)
    assert not Matrix()
    assert Matrix(0, 0, 0, 0, 0, 1)


def test_composition_applies_the_left_matrix_first() -> None:
    scale = Matrix(2, 3)
    translate = Matrix(1, 0, 0, 1, 10, -20)
    point = Point(4, 5)
    assert point * scale * translate == (18, -5)
    assert point * (scale * translate) == (18, -5)
    assert point * (translate * scale) == (28, -45)
    assert point * tuple(translate) == (14, -15)
    assert scale * tuple(translate) == scale * translate
    assert point * Matrix(2, 3, 1) == (14, 17)


@pytest.mark.parametrize(
    "matrix",
    [Matrix(2, 3), Matrix(90), Matrix(37), Matrix(1, 2, 3, 4, 5, 6), Matrix(1, 0, 0, 1, -300, 200)],
)
def test_inverse_and_division_round_trips(matrix: Matrix) -> None:
    identity = Matrix(1, 1)
    assert matrix * ~matrix == pytest.approx(identity, abs=1e-10)
    assert ~matrix * matrix == pytest.approx(identity, abs=1e-10)
    assert matrix / matrix == pytest.approx(identity, abs=1e-10)
    assert identity / tuple(matrix) == pytest.approx(~matrix)
    for point in [Point(), Point(23, -17), Point(-1000, 5000)]:
        assert (point * matrix) / matrix == pytest.approx(point, abs=1e-10)
        assert point / tuple(matrix) == pytest.approx(point * ~matrix)


@pytest.mark.parametrize("scale", [1e-200, 2e-162, 1e-160, 1e-155, 1e200])
def test_inverse_does_not_overflow_or_underflow_the_determinant(scale: float) -> None:
    matrix = Matrix(scale, scale)
    assert ~matrix == pytest.approx(Matrix(1 / scale, 1 / scale))
    assert matrix * ~matrix == pytest.approx(Matrix(1, 1))
    assert Point(1, 2) * matrix / matrix == pytest.approx(Point(1, 2))


def test_inverse_preserves_axes_with_very_different_scales() -> None:
    matrix = Matrix(1e200, 1e-200)
    assert ~matrix == Matrix(1e-200, 1e200)
    assert matrix * ~matrix == Matrix(1, 1)


@pytest.mark.parametrize(
    "matrix",
    [
        Matrix(),
        Matrix(1, 2, 2, 4, 5, 6),
        Matrix(0, 0, 0, 0, 10, 20),
        Matrix(1, 5, 5, 25, 0, 0),
        Matrix(3, 4, 15, 20, 0, 0),
        Matrix(1e200, 1e200, 1e200, 1e200, 0, 0),
        Matrix(1e-200, 1e-200, 1e-200, 1e-200, 0, 0),
    ],
)
def test_singular_inverse_is_zero_and_division_raises(matrix: Matrix) -> None:
    assert ~matrix == Matrix()
    for value in (Point(1, 2), Matrix(1, 1)):
        with pytest.raises(ZeroDivisionError, match="not invertible"):
            operator.truediv(value, matrix)


@pytest.mark.parametrize("value", [Point(1, 2), Matrix(1, 1)])
def test_zero_division_and_reverse_repetition_are_rejected(value: Point | Matrix) -> None:
    with pytest.raises(ZeroDivisionError):
        operator.truediv(value, 0)
    with pytest.raises(TypeError):
        operator.mul(2, value)
    with pytest.raises(TypeError):
        operator.add(tuple(value), value)


@pytest.mark.parametrize(
    ("matrix", "expected"),
    [
        (Matrix(), True),
        (Matrix(2, -3), True),
        (Matrix(90), True),
        (Matrix(180), True),
        (Matrix(270), True),
        (Matrix(45), False),
        (Matrix(1, 2, 1), False),
        (Matrix(1, 1e-6, 0, 1, 40, 20), True),
        (Matrix(1, 1e-4, 0, 1, 40, 20), False),
    ],
)
def test_rectilinearity(matrix: Matrix, *, expected: bool) -> None:
    assert matrix.is_rectilinear is expected


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ((6, 8), 5),
        ((0, 0, 10, 10), 0),
        ((10, 10, 0, 0), 0),
        ((6, 0, 10, 10), 3),
        ((6, 8, 10, 10), 5),
        ((0, 0, 0, 0), 5),
        (pyropdf.Rect(3, 4, 3, 4), 0),
    ],
)
def test_distance_to_point_or_rectangle(target: tuple[float, ...], expected: float) -> None:
    assert Point(3, 4).distance_to(target) == expected


@pytest.mark.parametrize(("unit", "expected"), [("px", 72), ("in", 1), ("cm", 2.54), ("mm", 25.4)])
def test_distance_units(unit: str, expected: float) -> None:
    assert Point().distance_to((72, 0), unit) == pytest.approx(expected)
    with pytest.raises(pyropdf.PdfError, match="unit"):
        Point().distance_to((72, 0), "invalid")


@pytest.mark.parametrize("coordinate", [math.nan, math.inf, -math.inf, 10**400])
def test_nonfinite_coordinates_and_operands_are_refused(coordinate: float) -> None:
    for construct in (
        lambda: Point(coordinate, 1),
        lambda: Matrix(coordinate),
        lambda: Matrix(coordinate, 1),
        lambda: Matrix(1, 0, 0, 1, coordinate, 0),
        lambda: Point(1, 2) * coordinate,
        lambda: Matrix(1, 1) + coordinate,
        lambda: Point(1, 2).distance_to((coordinate, 0)),
    ):
        with pytest.raises(pyropdf.PdfError, match="finite"):
            construct()


@pytest.mark.parametrize(
    "construct",
    [
        lambda: Point("12"),  # ty: ignore[invalid-argument-type]
        lambda: Point([1]),
        lambda: Point([1, 2, 3]),
        lambda: Point(iter([1, 2])),  # ty: ignore[invalid-argument-type]
        lambda: Point(1, x=2, y=3),  # ty: ignore[no-matching-overload]
        lambda: Point(x=1),  # ty: ignore[no-matching-overload]
        lambda: Matrix("123456"),  # ty: ignore[invalid-argument-type]
        lambda: Matrix([1] * 7),
        lambda: Matrix(iter(range(6))),  # ty: ignore[invalid-argument-type]
        lambda: Matrix(1, 2, 2),
        lambda: Matrix(a=1),  # ty: ignore[no-matching-overload]
        lambda: Point(1, 2) * (3, 4),
        lambda: Point(1, 2) / (3, 4),
        lambda: operator.add(Matrix(1, 1), (1, 2)),
        lambda: Matrix(1, 1) * (1, 2),
        lambda: Matrix(1, 1) / (1, 2),
    ],
)
def test_malformed_points_and_matrices_fail_without_partial_values(construct: Callable[[], object]) -> None:
    with pytest.raises((TypeError, pyropdf.PdfError)):
        construct()


def test_finite_input_that_overflows_an_operation_is_refused() -> None:
    with pytest.raises(pyropdf.PdfError, match="finite"):
        _ = Point(1e308, 1) * 10
    with pytest.raises(pyropdf.PdfError, match="finite"):
        _ = Matrix(1e308, 1) * 10
    with pytest.raises(pyropdf.PdfError, match="finite"):
        _ = ~Matrix(5e-324, 5e-324)


@pytest.mark.parametrize("name", ["transform"])
def test_point_has_no_mutating_alias(name: str) -> None:
    assert not hasattr(Point(), name)


@pytest.mark.parametrize("name", ["concat", "invert", "prerotate", "prescale", "preshear", "pretranslate"])
def test_matrix_has_no_mutating_alias(name: str) -> None:
    assert not hasattr(Matrix(), name)


@pytest.mark.parametrize(
    "args",
    [
        (1, 2, 3, 4),
        ((1, 2, 3, 4),),
        ([1, 2, 3, 4],),
        (Point(1, 2), Point(3, 4)),
        ((1, 2), (3, 4)),
        (Point(1, 2), 3, 4),
        (1, 2, Point(3, 4)),
        (Rect(1, 2, 3, 4),),
        (IRect(1, 2, 3, 4),),
    ],
)
def test_rect_constructors(args: tuple) -> None:
    rect = Rect(*args)
    assert rect == (1, 2, 3, 4)
    assert all(isinstance(value, float) for value in rect)
    assert Rect() == (0, 0, 0, 0)
    assert Rect(x0=1, y0=2, x1=3, y1=4) == Rect(1, 2, x1=3, y1=4) == rect


def test_irect_rounds_fractional_edges_outward() -> None:
    assert IRect(1.7, 2.2, 3.5, 4.9) == (1, 2, 4, 5)
    assert IRect(-1.7, -2.2, 3.5, 4.9) == (-2, -3, 4, 5)
    assert IRect(1.0005, 2, 3.0005, 3.9995) == (1, 2, 4, 4)
    assert IRect(3, 4, 1, 2) == (3, 4, 1, 2)
    assert all(type(value) is int for value in IRect(1.5, 2, 3, 4))
    assert IRect(Point(1.5, 2.5), Point(3.5, 4.5)) == (1, 2, 4, 5)


@pytest.mark.parametrize("value", [Rect(1, 2, 3, 4), IRect(1, 2, 3, 4), UNIT_QUAD])
def test_rectangles_and_quads_keep_named_tuple_contracts(value: Rect | IRect | Quad) -> None:
    assert isinstance(value, tuple)
    assert len(value) == 4
    assert value[:] == tuple(value)
    assert hash(value) == hash(tuple(value))
    assert type(value).__module__ == "pyropdf"
    for restored in (copy.copy(value), copy.deepcopy(value), pickle.loads(pickle.dumps(value))):  # noqa: S301 - locally serialized values.
        assert restored == value
        assert type(restored) is type(value)
    with pytest.raises(AttributeError):
        setattr(value, value._fields[0], 99)
    with pytest.raises(TypeError):
        operator.setitem(value, 0, 99)  # ty: ignore[no-matching-overload]


def test_field_types() -> None:
    assert get_type_hints(Rect) == dict.fromkeys(("x0", "y0", "x1", "y1"), float)
    assert get_type_hints(IRect) == dict.fromkeys(("x0", "y0", "x1", "y1"), int)
    assert get_type_hints(Quad) == dict.fromkeys(("ul", "ur", "ll", "lr"), Point)
    assert Rect(1, 2, 3, 4) == IRect(1, 2, 3, 4)
    assert not isinstance(IRect(), Rect)


def test_size_and_validity() -> None:
    assert Rect(1, 2, 4, 6).width == 3
    assert Rect(1, 2, 4, 6).height == 4
    assert Rect(3, 4, 1, 2).width == Rect(3, 4, 1, 2).height == 0
    assert type(IRect(1, 2, 4, 6).width) is int
    assert IRect(3, 4, 1, 2).width == 0
    assert Rect(3, 4, 1, 2).is_empty
    assert not Rect(3, 4, 1, 2).is_valid
    assert Rect(1, 2, 1, 4).is_empty
    assert Rect(1, 2, 1, 4).is_valid
    assert not Rect(1, 2, 3, 4).is_empty


def test_constants_are_class_attributes() -> None:
    for name in (
        "EMPTY_RECT",
        "EMPTY_IRECT",
        "EMPTY_QUAD",
        "INFINITE_RECT",
        "INFINITE_IRECT",
        "INFINITE_QUAD",
        "FZ_MIN_INF_RECT",
        "FZ_MAX_INF_RECT",
        "Identity",
        "IdentityMatrix",
        "planish_line",
    ):
        assert not hasattr(pyropdf, name)
        assert name not in pyropdf.__all__
    assert (Rect.INFINITE.x0, Rect.INFINITE.x1) == (LIMIT_LOW, LIMIT_HIGH)
    assert type(Rect.EMPTY) is Rect
    assert type(Quad.EMPTY) is Quad


def test_constants() -> None:
    assert Rect.INFINITE == (LIMIT_LOW, LIMIT_LOW, LIMIT_HIGH, LIMIT_HIGH)
    assert Rect.EMPTY == (LIMIT_HIGH, LIMIT_HIGH, LIMIT_LOW, LIMIT_LOW)
    assert IRect.INFINITE == Rect.INFINITE
    assert IRect.EMPTY == Rect.EMPTY
    assert type(IRect.INFINITE) is IRect
    assert Rect.INFINITE.quad == Quad.INFINITE
    assert Rect.EMPTY.quad == Quad.EMPTY
    assert Rect.INFINITE.is_infinite
    assert IRect.INFINITE.is_infinite
    assert Quad.INFINITE.is_infinite
    assert not Rect.INFINITE.is_empty
    assert Rect.INFINITE.is_valid
    assert Rect.EMPTY.is_empty
    assert not Rect.EMPTY.is_valid
    assert not Rect(LIMIT_LOW, LIMIT_LOW, LIMIT_HIGH, LIMIT_HIGH + 1).is_infinite
    # pymupdf measures a quad by its edges, so the empty quad is not empty.
    assert not Quad.EMPTY.is_empty


def test_rect_corners_keep_the_stored_order() -> None:
    rect = Rect(1, 2, 3, 4)
    assert (rect.tl, rect.tr, rect.bl, rect.br) == ((1, 2), (3, 2), (1, 4), (3, 4))
    assert (rect.top_left, rect.top_right, rect.bottom_left, rect.bottom_right) == (rect.tl, rect.tr, rect.bl, rect.br)
    assert isinstance(rect.tl, Point)
    assert Rect(3, 4, 1, 2).top_left == (3, 4)
    assert rect.quad == UNIT_QUAD
    assert Rect(3, 4, 1, 2).quad == ((3, 4), (1, 4), (3, 2), (1, 2))
    assert IRect(1, 2, 3, 4).quad == UNIT_QUAD
    assert IRect(1, 2, 3, 4).rect == rect
    assert type(IRect(1, 2, 3, 4).rect) is Rect
    assert Rect(3, 4, 1, 2).normalized() == rect
    assert IRect(3, 4, 1, 2).normalized() == (1, 2, 3, 4)


def test_rect_arithmetic() -> None:
    rect = Rect(1, 2, 3, 4)
    assert rect + 1 == (2, 3, 4, 5)
    assert rect + (1, 2, 3, 4) == (2, 4, 6, 8)  # noqa: RUF005 - geometric addition, not concatenation.
    assert rect + IRect(1, 1, 1, 1) == (2, 3, 4, 5)
    assert rect - 1 == (0, 1, 2, 3)
    assert rect - rect == (0, 0, 0, 0)
    assert rect * 2 == (2, 4, 6, 8)
    assert Rect(3, 4, 1, 2) * 2 == (6, 8, 2, 4)
    assert rect / 2 == (0.5, 1, 1.5, 2)
    assert -rect == (-1, -2, -3, -4)
    assert +rect is rect
    assert type(rect + 1) is Rect


def test_irect_arithmetic_rounds_the_result_outward() -> None:
    irect = IRect(1, 2, 3, 4)
    assert irect + 1 == (2, 3, 4, 5)
    assert irect + 0.5 == (1, 2, 4, 5)
    assert irect - 1 == (0, 1, 2, 3)
    assert irect * 1.5 == (1, 3, 5, 6)
    assert irect / 2 == (0, 1, 2, 2)
    assert irect * Matrix(90) == (-4, 1, -2, 3)
    assert irect * (1, 0, 0, 1, 0.5, 0.5) == (1, 2, 4, 5)
    assert -irect == (-1, -2, -3, -4)
    assert all(type(value) is IRect for value in (irect + 0.5, irect * 1.5, irect / 2, -irect))


@pytest.mark.parametrize(
    ("rect", "matrix", "expected"),
    [
        (Rect(1, 2, 3, 4), Matrix(90), (-4, 1, -2, 3)),
        (Rect(1, 2, 3, 4), (1, 0, 0, 1, 5, 5), (6, 7, 8, 9)),
        (Rect(1, 2, 3, 4), Matrix(-1, 1), (-3, 2, -1, 4)),
        (Rect(1, 2, 3, 4), Matrix(1, -1), (1, -4, 3, -2)),
        (Rect(1, 2, 3, 4), Matrix(0, -1, 1, 0, 0, 0), (2, -3, 4, -1)),
        (Rect(1, 2, 3, 4), Matrix(1, 0, 0.5, 1, 0, 0), (2, 2, 5, 4)),
        (Rect(1, 2, 3, 4), Matrix(), (0, 0, 0, 0)),
        (Rect(1, 2, 3, 4), Matrix(0, 1, 0, 0, 0, 0), (0, 1, 0, 3)),
        # An invalid rectangle keeps its exchanged edges along the same axes.
        (Rect(3, 2, 1, 4), Matrix(1, 1), (3, 2, 1, 4)),
        (Rect(3, 2, 1, 4), Matrix(90), (-4, 3, -2, 1)),
        (Rect(1, 4, 3, 2), Matrix(90), (-2, 1, -4, 3)),
        (Rect(1, 1, 1, 5), Matrix(90), (-5, 1, -1, 1)),
        # Other matrices exchange every corner of an invalid result.
        (Rect(3, 2, 1, 4), Matrix(1, 0, 0.5, 1, 0, 0), (5, 4, 2, 2)),
        (Rect(3, 4, 1, 2), Matrix(1, 0, 0.5, 1, 0, 0), (5, 4, 2, 2)),
    ],
)
def test_rect_transformation(rect: Rect, matrix: Matrix, expected: tuple[float, ...]) -> None:
    assert rect * matrix == expected


def test_general_rotation_returns_the_bounding_box() -> None:
    half = math.sqrt(0.5)
    expected = (-3 * half, 3 * half, half, 7 * half)
    assert Rect(1, 2, 3, 4) * Matrix(45) == pytest.approx(expected, abs=1e-7)
    assert Rect(3, 4, 1, 2) * Matrix(45) == pytest.approx((half, 7 * half, -3 * half, 3 * half), abs=1e-7)
    assert (Rect(1, 2, 3, 4) * Matrix(45)).quad != (Rect(1, 2, 3, 4).quad * Matrix(45))


def test_infinite_rect_stays_infinite_under_any_matrix() -> None:
    for matrix in (Matrix(2, 2), Matrix(90), Matrix(30), Matrix(1, 0, 0, 1, 5, 5)):
        assert (Rect.INFINITE * matrix).is_infinite
    assert not (Rect.INFINITE * 2).is_infinite
    assert not (Rect.INFINITE + 1).is_infinite
    assert Rect.EMPTY * Matrix(2, 2) == (2 * LIMIT_HIGH, 2 * LIMIT_HIGH, 2 * LIMIT_LOW, 2 * LIMIT_LOW)


def test_rect_division_by_matrix() -> None:
    assert Rect(1, 2, 3, 4) / Matrix(2, 2) == (0.5, 1, 1.5, 2)
    assert Rect(1, 2, 3, 4) * Matrix(90) / Matrix(90) == (1, 2, 3, 4)
    # A bounding box only grows under a general rotation and its inverse.
    assert Rect(1, 2, 3, 4) in Rect(1, 2, 3, 4) * Matrix(30) / Matrix(30)
    with pytest.raises(ZeroDivisionError, match="not invertible"):
        _ = Rect(1, 2, 3, 4) / Matrix()
    with pytest.raises(ZeroDivisionError):
        _ = Rect(1, 2, 3, 4) / 0


def test_union() -> None:
    rect = Rect(1, 2, 3, 4)
    assert rect | Point(10, 10) == (1, 2, 10, 10)
    assert rect | (10, 10) == (1, 2, 10, 10)
    assert rect | Rect(0, 0, 2, 2) == (0, 0, 3, 4)
    assert rect | (0, 0, 2, 2) == (0, 0, 3, 4)
    # A point extends every rectangle, even an empty or invalid one.
    assert Rect() | Point(5, 5) == (0, 0, 5, 5)
    assert Rect.EMPTY | Point(1, 2) == (1, 2, 1, 2)
    assert Rect(3, 4, 1, 2) | Point(5, 5) == (3, 4, 5, 5)
    # An empty rectangle contributes nothing to a union of rectangles.
    assert Rect() | Rect(1, 1, 2, 2) == (1, 1, 2, 2)
    assert Rect(0, 0, 0, 5) | Rect(1, 1, 2, 2) == (1, 1, 2, 2)
    assert rect | Rect(3, 4, 1, 2) == rect
    assert rect | Rect() == rect
    assert Rect.EMPTY | rect == rect
    assert (Rect.INFINITE | rect).is_infinite
    assert (rect | Rect.INFINITE).is_infinite
    assert (Rect.INFINITE | Point(1e12, 1e12)).is_infinite
    assert (Rect.INFINITE | Rect.EMPTY).is_infinite
    assert IRect(1, 2, 3, 4) | Point(10.5, 10.5) == (1, 2, 11, 11)
    assert IRect(1, 2, 3, 4) | Rect(0.5, 0.5, 1, 1) == (0, 0, 3, 4)


def test_intersection() -> None:
    rect = Rect(1, 2, 3, 4)
    assert rect & Rect(2, 3, 5, 5) == (2, 3, 3, 4)
    assert rect & (2, 3, 5, 5) == (2, 3, 3, 4)
    # Disjoint rectangles give an invalid, empty result.
    assert rect & Rect(5, 5, 6, 6) == (5, 5, 3, 4)
    assert (rect & Rect(5, 5, 6, 6)).is_empty
    # An empty operand is the result; the infinite one yields the other.
    assert Rect() & rect == (0, 0, 0, 0)
    assert Rect(3, 4, 1, 2) & rect == (3, 4, 1, 2)
    assert rect & Rect(3, 4, 1, 2) == (3, 4, 1, 2)
    assert Rect(1, 1, 1, 5) & Rect(0, 0, 3, 3) == (1, 1, 1, 5)
    assert Rect.INFINITE & rect == rect
    assert Rect.INFINITE & Rect.EMPTY == Rect.EMPTY
    # With both operands empty, the right one is the result.
    assert Rect(14, 4, 17, -20) & Rect(4, 5, 0, 20) == (4, 5, 0, 20)
    assert Rect.EMPTY & Rect() == Rect()
    assert IRect(1, 2, 3, 4) & Rect(1.5, 2.5, 2.5, 3.5) == (1, 2, 3, 4)


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (Point(2, 3), True),
        (Point(1, 2), True),
        # Points use half-open intervals, so the far edges are outside.
        (Point(3, 4), False),
        (Point(3, 3), False),
        ((2, 3), True),
        # Rectangles include their edges, and an empty one inside counts.
        (Rect(1, 2, 3, 4), True),
        (Rect(1, 2, 2, 3), True),
        (Rect(2, 3, 2, 3), True),
        (IRect(1, 2, 2, 3), True),
        ((1, 2, 3, 4), True),
        (Rect(), False),
        (Rect(5, 5, 5, 5), False),
        (Rect(2.5, 3.5, 2, 3), False),
        (UNIT_QUAD, True),
        ((UNIT_QUAD * Matrix(1, 0, 0, 1, 1, 0)), False),
        # A number tests the coordinates, as for a tuple.
        (2, True),
        (5, False),
        ("x", False),
        ((1, 2, 3), False),
    ],
)
def test_rect_containment(item: object, *, expected: bool) -> None:
    assert (item in Rect(1, 2, 3, 4)) is expected
    assert Rect(1, 2, 3, 4).contains(item) is expected
    assert (item in IRect(1, 2, 3, 4)) is expected


def test_containment_in_degenerate_and_infinite_rects() -> None:
    assert Rect(1, 1, 1, 1) in Rect(1, 1, 1, 1)
    assert Point(1, 1) not in Rect(1, 1, 1, 1)
    assert Point(2, 3) not in Rect(3, 4, 1, 2)
    assert Rect(1, 2, 3, 4) not in Rect(3, 4, 1, 2)
    assert Rect(1, 2, 3, 4) in Rect.INFINITE
    assert Rect.INFINITE in Rect.INFINITE
    assert Point(1e12, 0) not in Rect.INFINITE


def test_intersects_requires_two_finite_areas() -> None:
    rect = Rect(1, 2, 3, 4)
    assert rect.intersects(Rect(2, 3, 5, 6))
    assert rect.intersects((2, 3, 5, 6))
    assert IRect(1, 2, 3, 4).intersects(IRect(2, 3, 5, 6))
    assert not rect.intersects(Rect(3, 4, 5, 6))
    assert not rect.intersects(Rect(3, 2, 5, 4))
    assert not rect.intersects(Rect())
    assert not rect.intersects(Rect(3, 4, 1, 2))
    assert not Rect(3, 4, 1, 2).intersects(rect)
    assert not rect.intersects(Rect.INFINITE)
    with pytest.raises(pyropdf.PdfError):
        rect.intersects(Point(2, 3))


def test_area_norm_and_truth() -> None:
    assert abs(Rect(1, 2, 3, 4)) == 4
    assert abs(Rect(3, 4, 1, 2)) == 0
    assert abs(Rect.INFINITE) == 0
    assert Rect(1, 2, 3, 4).get_area() == 4
    assert IRect(1, 2, 3, 4).get_area() == 4
    assert type(IRect(1, 2, 3, 4).get_area()) is float
    assert Rect(1, 2, 1, 4).get_area() == 0
    assert Rect.INFINITE.get_area() == (LIMIT_HIGH - LIMIT_LOW) ** 2
    square_inch = Rect(0, 0, 72, 72)
    assert square_inch.get_area("px") == 5184
    assert square_inch.get_area("in") == pytest.approx(1)
    assert square_inch.get_area("cm") == pytest.approx(2.54**2)
    assert square_inch.get_area("mm") == pytest.approx(25.4**2)
    with pytest.raises(pyropdf.PdfError, match="unit"):
        square_inch.get_area("IN")
    assert Rect(1, 2, 3, 4).norm() == Rect(-1, -2, -3, -4).norm() == math.sqrt(30)
    assert not Rect()
    assert Rect(0, 0, 0, 1)
    assert Rect(1, 1, 1, 1)
    # pymupdf's IRect has neither abs() nor geometric truth testing.
    assert bool(IRect()) is True
    with pytest.raises(TypeError):
        abs(IRect(1, 2, 3, 4))  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("rect", "expected"),
    [
        (Rect(0.5, 1.5, 2.5, 3.2), (0, 1, 3, 4)),
        (Rect(-0.5, -1.5, 2.0001, 3.9999), (-1, -2, 2, 4)),
        (Rect(1.0005, 2.0009, 3.0005, 3.9995), (1, 2, 3, 4)),
        (Rect(1.002, 2, 3.002, 4), (1, 2, 4, 4)),
        (Rect(3.5, 4.5, 1.2, 2.2), (3, 4, 2, 3)),
        (Rect.INFINITE, (-16777216, -16777216, 16777216, 16777216)),
    ],
)
def test_round_tolerates_float_noise_and_clamps(rect: Rect, expected: tuple[int, ...]) -> None:
    assert rect.round() == expected
    assert rect.irect == expected
    assert type(rect.round()) is IRect


def test_torect_maps_one_rectangle_onto_another() -> None:
    matrix = Rect(1, 2, 3, 4).torect(Rect(0, 0, 10, 10))
    assert matrix == (5, 0, 0, 5, -5, -10)
    assert Rect(1, 2, 3, 4) * matrix == (0, 0, 10, 10)
    assert IRect(1, 2, 3, 4).torect(IRect(0, 0, 4, 4)) == (2, 0, 0, 2, -2, -4)
    assert Rect(1, 2, 3, 4).torect((0, 0, 4, 4)) == (2, 0, 0, 2, -2, -4)
    for source, target in [
        (Rect(1, 1, 1, 1), Rect(0, 0, 10, 10)),
        (Rect(1, 2, 3, 4), Rect(3, 4, 1, 2)),
        (Rect.INFINITE, Rect(0, 0, 1, 1)),
    ]:
        with pytest.raises(pyropdf.PdfError, match="finite and not empty"):
            source.torect(target)


def test_morph_turns_corners_around_a_fixed_point() -> None:
    assert Rect(1, 2, 3, 4).morph(Point(2, 3), Matrix(90)) == ((3, 2), (3, 4), (1, 2), (1, 4))
    assert Rect(1, 2, 3, 4).morph((2, 3), Matrix(2, 2)) == ((0, 1), (4, 1), (0, 5), (4, 5))
    assert Rect(3, 4, 1, 2).morph(Point(2, 3), Matrix(2, 2)) == ((4, 5), (0, 5), (4, 1), (0, 1))
    assert Rect(1, 2, 3, 4).morph(Point(2, 3), Matrix()) == (Point(2, 3),) * 4
    assert IRect(1, 2, 3, 4).morph(Point(2, 3), Matrix(90)) == ((3, 2), (3, 4), (1, 2), (1, 4))
    assert UNIT_QUAD.morph(Point(2, 3), (0, 1, -1, 0, 0, 0)) == ((3, 2), (3, 4), (1, 2), (1, 4))
    assert Rect.INFINITE.morph(Point(), Matrix(90)) == Quad.INFINITE
    assert Quad.INFINITE.morph(Point(1, 1), Matrix(2, 2)) == Quad.INFINITE


@pytest.mark.parametrize(
    "args",
    [
        (Point(1, 2), Point(3, 2), Point(1, 4), Point(3, 4)),
        ((1, 2), (3, 2), (1, 4), (3, 4)),
        ([(1, 2), (3, 2), (1, 4), (3, 4)],),
        (UNIT_QUAD,),
    ],
)
def test_quad_constructors(args: tuple) -> None:
    quad = Quad(*args)
    assert quad == UNIT_QUAD
    assert all(isinstance(corner, Point) for corner in quad)
    assert (quad.ul, quad.ur, quad.ll, quad.lr) == ((1, 2), (3, 2), (1, 4), (3, 4))
    assert Quad(ul=(1, 2), ur=(3, 2), ll=(1, 4), lr=(3, 4)) == quad
    assert Quad() == (Point(),) * 4


def test_quad_measurements() -> None:
    assert UNIT_QUAD.rect == (1, 2, 3, 4)
    assert type(UNIT_QUAD.rect) is Rect
    assert UNIT_QUAD.width == UNIT_QUAD.height == 2
    sheared = UNIT_QUAD * Matrix(1, 0, 0.5, 1, 0, 0)
    assert sheared.width == 2
    assert sheared.height == math.sqrt(5)
    assert abs(sheared) == 2 * math.sqrt(5)
    # Width and height take the longer of each pair of opposite edges,
    # while abs() multiplies the two edges that meet at ul.
    trapezoid = Quad(Point(0, 0), Point(3, 0), Point(1, 2), Point(2, 2))
    assert trapezoid.width == 3
    assert trapezoid.height == math.sqrt(5)
    kite = Quad(Point(0, 0), Point(2, 0), Point(1, 1), Point(1, 3))
    assert kite.height == math.sqrt(10)
    assert abs(kite) == 2 * math.sqrt(2)
    assert abs(UNIT_QUAD) == 4
    assert abs(Quad()) == 0
    assert abs(Quad.INFINITE) == (LIMIT_HIGH - LIMIT_LOW) ** 2


@pytest.mark.parametrize(
    ("quad", "rectangular", "convex", "empty"),
    [
        (UNIT_QUAD, True, True, False),
        (UNIT_QUAD * Matrix(30), True, True, False),
        (UNIT_QUAD * Matrix(1, 0, 0.5, 1, 0, 0), False, True, False),
        (Quad(), False, True, True),
        # The mirror image of a rectangle's quad turns the other way.
        (Quad(Point(0, 0), Point(2, 0), Point(0, -1), Point(2, -1)), False, True, False),
        (Quad(Point(2, 0), Point(0, 0), Point(2, 1), Point(0, 1)), False, True, False),
        # Corners in outline order make a bow tie.
        (Quad(Point(0, 0), Point(2, 0), Point(2, 2), Point(0, 2)), False, False, False),
        (Quad(Point(0, 0), Point(2, 0), Point(1, 1), Point(1, 3)), False, False, False),
        (Quad(Point(0, 0), Point(1, 1), Point(2, 2), Point(3, 3)), False, True, False),
        (Quad(Point(0, 0), Point(2, 0), Point(0, 0), Point(2, 0)), False, True, True),
        (Quad(Point(0, 0), Point(2, 0), Point(0, 1e-6), Point(2, 1e-6)), True, True, True),
        (Quad(Point(0, 0), Point(2, 0), Point(0, 1e-5), Point(2, 1e-5)), True, True, False),
        # Each corner's sine may differ from 1 by 1e-5.
        (Quad(Point(0, 0), Point(2, 0), Point(0.0044, 1), Point(2.0044, 1)), True, True, False),
        (Quad(Point(0, 0), Point(2, 0), Point(0.0046, 1), Point(2.0046, 1)), False, True, False),
        (Quad(Point(0, 0), Point(2, 0), Point(0, 1), Point(2.004, 1)), True, True, False),
        (Quad(Point(0, 0), Point(2, 0), Point(0, 1), Point(2.01, 1)), False, True, False),
        (Quad.INFINITE, True, True, False),
    ],
)
def test_quad_shape(quad: Quad, *, rectangular: bool, convex: bool, empty: bool) -> None:
    assert quad.is_rectangular is rectangular
    assert quad.is_convex is convex
    assert quad.is_empty is empty
    assert bool(quad) is not empty


def test_quad_arithmetic() -> None:
    assert UNIT_QUAD + 1 == ((2, 3), (4, 3), (2, 5), (4, 5))
    assert UNIT_QUAD + UNIT_QUAD == UNIT_QUAD * 2
    assert UNIT_QUAD + ((1, 1),) * 4 == UNIT_QUAD + 1
    assert UNIT_QUAD - 1 == ((0, 1), (2, 1), (0, 3), (2, 3))
    assert Quad() == UNIT_QUAD - UNIT_QUAD
    assert UNIT_QUAD * 2 == ((2, 4), (6, 4), (2, 8), (6, 8))
    assert UNIT_QUAD / 2 == ((0.5, 1), (1.5, 1), (0.5, 2), (1.5, 2))
    assert UNIT_QUAD * (1, 0, 0, 1, 1, 1) == UNIT_QUAD + 1
    assert UNIT_QUAD / Matrix(2, 2) == UNIT_QUAD / 2
    assert _flat(UNIT_QUAD * Matrix(30) / Matrix(30)) == pytest.approx(_flat(UNIT_QUAD))
    assert -UNIT_QUAD == ((-1, -2), (-3, -2), (-1, -4), (-3, -4))
    assert +UNIT_QUAD is UNIT_QUAD
    with pytest.raises(ZeroDivisionError, match="not invertible"):
        _ = UNIT_QUAD / Matrix()
    # Transforming the quad keeps the corners; transforming the rect boxes them.
    rotated = UNIT_QUAD * Matrix(45)
    assert rotated.is_rectangular
    assert rotated.rect == pytest.approx(Rect(1, 2, 3, 4) * Matrix(45))
    assert not (Quad.INFINITE * Matrix(2, 2)).is_infinite


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (Point(2, 3), True),
        # Quads include their edges and corners.
        (Point(1, 2), True),
        (Point(3, 4), True),
        (Point(3, 3), True),
        ((2, 3), True),
        (Point(1, 1), False),
        (Rect(1, 2, 2, 3), True),
        (Rect(1, 2, 3, 4), True),
        (Rect(2, 3, 3, 4), True),
        ((1, 2, 3, 4), True),
        (Rect(0, 2, 3, 4), False),
        (UNIT_QUAD, True),
        (Rect(1, 2, 2, 3).quad, True),
        (UNIT_QUAD + 0.5, False),
        (2, False),
        ("x", False),
    ],
)
def test_quad_containment(item: object, *, expected: bool) -> None:
    assert (item in UNIT_QUAD) is expected


def test_quad_containment_follows_rotation_and_shape() -> None:
    rotated = UNIT_QUAD * Matrix(45)
    assert Point(0, 3.5) in rotated
    assert Point(2, 3) not in rotated
    assert Point(1.5, 1) in Quad(Point(0, 0), Point(3, 0), Point(1, 2), Point(2, 2))
    assert Point(0.2, 1.9) not in Quad(Point(0, 0), Point(3, 0), Point(1, 2), Point(2, 2))
    # A degenerate quad contains nothing.
    assert Point(1, 1) not in Quad(Point(0, 0), Point(1, 1), Point(2, 2), Point(3, 3))
    assert Point(1, 0) not in Quad(Point(0, 0), Point(2, 0), Point(0, 0), Point(2, 0))
    assert Point(0, 0) not in UNIT_QUAD * Matrix()
    # Every corner of a rectangle must be inside, not only two of them.
    kite = Quad(Point(0, 0), Point(2, 0), Point(1, 1), Point(1, 3))
    assert Rect(0.5, 0.2, 1.5, 0.4) in kite
    assert Rect(0, 0, 1, 1) not in kite


@pytest.mark.parametrize(
    "construct",
    [
        lambda: Rect(1, 2, 3),  # ty: ignore[no-matching-overload]
        lambda: Rect(1, 2, 3, 4, 5),  # ty: ignore[no-matching-overload]
        lambda: Rect("abcd"),  # ty: ignore[invalid-argument-type]
        lambda: Rect(iter([1, 2, 3, 4])),  # ty: ignore[invalid-argument-type]
        lambda: Rect(math.nan, 0, 1, 1),
        lambda: Rect(math.inf, 0, 1, 1),
        lambda: Rect(x0=1),  # ty: ignore[no-matching-overload]
        lambda: IRect(math.nan, 0, 1, 1),
        lambda: IRect("1234"),  # ty: ignore[invalid-argument-type]
        lambda: Rect(1, 2, 3, 4) + (1, 2),  # noqa: RUF005 - geometric addition, not concatenation.
        lambda: Rect(1, 2, 3, 4) + Point(1, 2),
        lambda: Rect(1, 2, 3, 4) * Rect(1, 2, 3, 4),
        lambda: Rect(1, 2, 3, 4) | (1, 2, 3),
        lambda: Rect(1, 2, 3, 4) & Point(1, 1),
        lambda: Rect(1, 2, 3, 4) & UNIT_QUAD,
        lambda: Rect(1, 2, 3, 4).torect(Point(1, 1)),
        lambda: Quad(Rect(1, 2, 3, 4)),  # ty: ignore[invalid-argument-type]
        lambda: Quad(1, 2, 3, 4, 5, 6, 7, 8),  # ty: ignore[no-matching-overload]
        lambda: Quad(Point(), Point(), Point()),  # ty: ignore[no-matching-overload]
        lambda: Quad(ul=Point()),  # ty: ignore[no-matching-overload]
        lambda: Quad((math.inf, 0), Point(), Point(), Point()),
        lambda: UNIT_QUAD + (1, 2),  # noqa: RUF005 - geometric addition, not concatenation.
        lambda: UNIT_QUAD * Rect(0, 0, 1, 1),
        lambda: operator.mul(2, Rect(1, 2, 3, 4)),
        lambda: operator.add((1, 2, 3, 4), Rect(1, 2, 3, 4)),
        lambda: operator.mul(2, UNIT_QUAD),
        lambda: operator.add(tuple(UNIT_QUAD), UNIT_QUAD),
    ],
)
def test_malformed_rectangles_and_quads_fail_without_partial_values(construct: Callable[[], object]) -> None:
    with pytest.raises((TypeError, pyropdf.PdfError)):
        construct()


@pytest.mark.parametrize(
    ("value", "names"),
    [
        (Rect(), ("include_point", "include_rect", "intersect", "normalize", "transform")),
        (IRect(), ("include_point", "include_rect", "intersect", "normalize", "transform")),
        (Quad(), ("transform",)),
    ],
)
def test_no_mutating_aliases(value: object, names: tuple[str, ...]) -> None:
    for name in names:
        assert not hasattr(value, name)


def test_identity_and_infinite_bounds() -> None:
    assert Rect.INFINITE == (LIMIT_LOW,) * 2 + (LIMIT_HIGH,) * 2
    identity = Matrix.IDENTITY
    assert identity == Matrix(1, 1)
    assert repr(identity).startswith("Matrix(")
    assert type(identity * Matrix(2, 2)) is Matrix
    assert pickle.loads(pickle.dumps(identity)) == identity  # noqa: S301 - locally serialized value.
    assert type(copy.deepcopy(identity)) is Matrix


@pytest.mark.parametrize(
    ("start", "end"),
    [((1, 1), (4, 5)), (Point(0, 0), Point(-3, 0)), ((0, 0), (0, 3)), ((-2, 7), (5, -1))],
)
def test_planish_moves_a_line_onto_the_x_axis(start: tuple[float, float], end: tuple[float, float]) -> None:
    matrix = Matrix.planish(start, end)
    assert Point(start) * matrix == pytest.approx((0, 0), abs=1e-12)
    assert Point(end) * matrix == pytest.approx((abs(Point(end) - start), 0), abs=1e-12)
    assert matrix.is_rectilinear or abs(matrix.a * matrix.d - matrix.b * matrix.c) == pytest.approx(1)


def test_planish_of_one_point_is_zero() -> None:
    assert Matrix.planish((1, 1), Point(1, 1)) == Matrix()
    assert Matrix.planish((1, 1), (4, 5)) == pytest.approx((0.6, -0.8, 0.8, 0.6, -1.4, 0.2))


def test_page_rect_is_a_rect_that_follows_the_rotation() -> None:
    doc = boxed()
    page = doc.pages[0]
    assert type(page.rect) is Rect
    assert page.rect == Rect(0, 0, 200, 100)
    assert all(type(value) is float for value in page.rect)
    page.rotation = 90
    assert page.rect == (0, 0, 100, 200)
    box = page.rect * Matrix(0.5, 0.5) + (20, 20, 20, 20)
    assert box in page.rect
    assert (box | page.rect) == page.rect
    assert (box & page.rect) == box
    assert page.rect.irect == IRect(0, 0, 100, 200)


def test_pixmap_takes_a_matrix() -> None:
    page = boxed().pages[0]
    scaled = page.pixmap(matrix=Matrix(0.5, 2) * Matrix(1, 0, 0, 1, 100, 100))
    assert (scaled.width, scaled.height, scaled.resolution) == (100, 200, (36, 144))
    turned = page.pixmap(matrix=Matrix(90))
    assert (turned.width, turned.height) == (100, 200)
    assert bytes(memoryview(page.pixmap(matrix=Matrix.IDENTITY))) == bytes(memoryview(page.pixmap()))
    with pytest.raises(ValueError, match="invertible"):
        page.pixmap(matrix=Matrix())


def test_the_package_exports_the_geometry_types() -> None:
    for value in (Point, Matrix, Rect, IRect, Quad):
        assert getattr(pyropdf, value.__name__) is value
        assert value.__name__ in pyropdf.__all__
