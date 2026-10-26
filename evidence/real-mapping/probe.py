# ruff: noqa: INP001 - a script, not a package.
"""Probe how three Python shapes for a PDF real meet ordinary Python code (#11).

Each option is a prototype, not the object model of #32. Run from the
repository root: ``uv run --with numpy python evidence/real-mapping/probe.py``.
"""

from __future__ import annotations

import json
import pickle
import sys
from decimal import Decimal
from typing import TYPE_CHECKING, Self

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Callable

SOURCE = b"35.68123456789012345"
PARSED = float(SOURCE)


class RealFloat(float):
    """Option A: a ``float`` subclass that carries the lexeme it was read from."""

    __slots__ = ("lexeme",)
    lexeme: bytes

    def __new__(cls, lexeme: bytes) -> Self:
        """Return the real that ``lexeme`` writes."""
        self = super().__new__(cls, float(lexeme))
        self.lexeme = lexeme
        return self

    def __reduce__(self) -> tuple[type[RealFloat], tuple[bytes]]:
        """Pickle the lexeme, not the ``f64``."""
        return (RealFloat, (self.lexeme,))

    def to_decimal(self) -> Decimal:
        """Return the exact decimal that the lexeme writes."""
        return Decimal(self.lexeme.decode())


class RealValue:
    """Option C: a value type with ``float()`` and ``Decimal`` views, not a ``float``."""

    __slots__ = ("lexeme",)

    def __init__(self, lexeme: bytes) -> None:
        """Keep ``lexeme``."""
        self.lexeme = lexeme

    def __float__(self) -> float:
        """Return the ``f64`` nearest the lexeme."""
        return float(self.lexeme)

    def __eq__(self, other: object) -> bool:
        """Compare ``f64`` values, as the store does."""
        if isinstance(other, RealValue | int | float):
            return float(self) == float(other)
        return NotImplemented

    def __hash__(self) -> int:
        """Hash as the ``f64`` does, so that equal values hash equally."""
        return hash(float(self))

    def to_decimal(self) -> Decimal:
        """Return the exact decimal that the lexeme writes."""
        return Decimal(self.lexeme.decode())


def write(value: object, kept: bytes | None) -> bytes:
    """Return what assigning ``value`` writes, where the slot kept ``kept`` (option B)."""
    if isinstance(value, RealFloat | RealValue):
        return value.lexeme
    if isinstance(value, Decimal):
        return format(value, "f").encode()
    if isinstance(value, float):
        if kept is not None and float(kept) == value:
            return kept
        return repr(value).encode()
    raise TypeError(type(value).__name__)


def attempt(thunk: Callable[[], object]) -> object:
    """Return what ``thunk`` returns, or the error it raises."""
    try:
        return thunk()
    except TypeError as error:
        return f"raises TypeError: {error}"


def decimal_view(real: object) -> Decimal:
    """Return the option's ``Decimal`` view of ``real``."""
    if isinstance(real, RealFloat | RealValue):
        return real.to_decimal()
    assert isinstance(real, float)  # noqa: S101 - option B is a plain float.
    return Decimal(real)


def probe(real: object) -> list[tuple[str, object]]:
    """Return each question and the option's answer for ``real``."""
    questions: list[tuple[str, Callable[[], object]]] = [
        ("isinstance(x, float)", lambda: isinstance(real, float)),
        ("x == float(source)", lambda: real == PARSED),
        ("hash(x) == hash(float(source))", lambda: hash(real) == hash(PARSED)),
        ("x + 1", lambda: real + 1),  # ty: ignore[unsupported-operator]
        ("json.dumps(x)", lambda: json.dumps(real)),
        (
            "pickle round trip keeps the lexeme",
            lambda: getattr(pickle.loads(pickle.dumps(real)), "lexeme", None) == SOURCE,  # noqa: S301 - our own bytes.
        ),
        ("np.array([x]).dtype", lambda: np.array([real]).dtype),
        ("Decimal view", lambda: decimal_view(real)),
        ("assigned back to the same slot", lambda: write(real, SOURCE)),
        ("copied to another slot", lambda: write(real, None)),
    ]
    return [(question, attempt(thunk)) for question, thunk in questions]


def main() -> None:
    """Print one line per question and option."""
    lines: list[str] = []
    options = {"A float subclass": RealFloat(SOURCE), "B plain float": PARSED, "C value type": RealValue(SOURCE)}
    for name, real in options.items():
        lines.append(f"== {name}")
        lines.extend(f"{question:<38} {answer}" for question, answer in probe(real))
    lines.append("== assigning other values")
    values = (0.1, Decimal(SOURCE.decode()), Decimal("1E-7"))
    lines.extend(f"{value!r:<38} {write(value, None)}" for value in values)
    sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
