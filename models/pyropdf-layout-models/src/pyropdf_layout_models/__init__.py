"""PP-DocLayout_plus-L INT8 model paths for pyropdf's optional offline layout engine."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

__version__ = "0.1.0"
__all__ = ["LayoutModelPaths", "model_paths"]

_BASE = Path(__file__).parent


class LayoutModelPaths(NamedTuple):
    """Paths to one compatible PP-DocLayout RTen model and its metadata."""

    model: Path
    metadata: Path
    version: str


def model_paths() -> LayoutModelPaths:
    """Return paths to the bundled PP-DocLayout_plus-L INT8 RTen model."""
    return LayoutModelPaths(
        model=_BASE / "PP-DocLayout_plus-L_int8.rten",
        metadata=_BASE / "model.json",
        version=__version__,
    )
