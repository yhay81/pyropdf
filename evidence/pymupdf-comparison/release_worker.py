# ruff: noqa: INP001, T201 - development-only subprocess for a single installed wheel.
"""Emit one JSON observation per line so a later worker failure preserves earlier results."""

from __future__ import annotations

import inspect
import json
import sys
import time
from importlib.metadata import distributions
from pathlib import Path
from typing import TYPE_CHECKING, Any

import bench

if TYPE_CHECKING:
    from collections.abc import Callable

# PDF-facing classes and the public functions attached to them (including aliases).
CLASSES = (
    "Annot Colorspace DisplayList Document DocumentWriter Font Link Outline Page Pixmap "
    "Shape TextPage TextWriter Widget"
)


def surface() -> dict[str, str]:
    """Inventory public calls, constructor signatures and properties in the audited PDF classes."""
    import pymupdf  # noqa: PLC0415

    entries = {}
    for cls_name in CLASSES.split():
        cls = getattr(pymupdf, cls_name, None)
        if cls is None:
            continue
        for name in [cls_name, *(n for n in dir(cls) if not n.startswith("_"))]:
            key = cls_name if name == cls_name else f"{cls_name}.{name}"
            value = cls if name == cls_name else inspect.getattr_static(cls, name)
            if isinstance(value, (staticmethod, classmethod)):
                value = value.__func__
            if callable(value):
                try:
                    entries[key] = str(inspect.signature(value))
                except (ValueError, TypeError):
                    entries[key] = "callable"
            else:
                entries[key] = type(value).__name__
    return entries


def measure(opener: Callable, operation: Callable | None, path: Path, refusal: tuple[type[Exception], ...]) -> dict:
    """Time fresh document operations, keeping every completed sample even on refusal."""
    samples = []
    try:
        for _ in range(bench.RUNS):
            start = time.perf_counter_ns()
            doc = opener(path)
            try:
                if operation is not None:
                    start = time.perf_counter_ns()
                    operation(doc)
                samples.append((time.perf_counter_ns() - start) / 1e6)
            finally:
                doc.close()
    except Exception as exc:  # noqa: BLE001 - failures are evidence
        return {
            "status": "refused" if isinstance(exc, refusal) else "failed",
            "reason": f"{type(exc).__name__}: {exc}",
            "samples_ms": samples,
        }
    return {"status": "ok", "samples_ms": samples}


def main() -> None:
    """Run outside the project environment, with exactly one engine imported."""
    mode, engine, *args = sys.argv[1:]
    version, opener, operations = {"pymupdf": bench._pymupdf, "pyropdf": bench._pyropdf}[engine]()  # noqa: SLF001
    if mode == "info":
        data: dict[str, Any] = {
            "engine": version,
            "installed": {dist.metadata["Name"]: dist.version for dist in distributions()},
        }
        if engine == "pymupdf":
            data["surface"] = surface()
        print(json.dumps(data, sort_keys=True))
        return
    refusal: tuple[type[Exception], ...] = ()
    if engine == "pyropdf":
        import pyropdf  # noqa: PLC0415

        refusal = (pyropdf.LimitError, pyropdf.PasswordError, pyropdf.EncryptedDocumentError)
    for name in bench.OPERATIONS:
        result = measure(opener, None if name == "open" else operations[name], Path(args[0]), refusal)
        print(json.dumps([name, result]), flush=True)


if __name__ == "__main__":
    main()
