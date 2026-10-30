# ruff: noqa: INP001, T201 - a script, not a package; it prints its results.
"""Time the operations that PyMuPDF and pyropdf share, for one engine.

    python bench.py ENGINE OUT FILE...

ENGINE is ``pymupdf`` or ``pyropdf``, and the Python that runs the script must
have that engine installed. Each operation runs on a document opened again for
that run, because pyropdf memoizes derived results per version and a second
read of the same document would measure the memo. Each row is the best of
``RUNS`` runs, in milliseconds, over every page of the document.
"""

from __future__ import annotations

import json
import platform
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

RUNS = 5
#: The word searched on every page; present in most of the corpus.
NEEDLE = "the"
OPERATIONS = ("open", "text", "words", "search", "render_72", "render_144", "save")


def _pymupdf() -> tuple[str, Callable[[Path], Any], dict[str, Callable[[Any], object]]]:
    import pymupdf  # noqa: PLC0415 - only in the PyMuPDF environment

    def pages(doc: Any) -> list[Any]:  # noqa: ANN401 - either engine's document
        return list(doc)

    return (
        f"PyMuPDF {pymupdf.__version__} (MuPDF {pymupdf.mupdf_version})",
        pymupdf.open,
        {
            "open": lambda doc: doc.page_count,
            "text": lambda doc: [page.get_text() for page in pages(doc)],
            "words": lambda doc: [page.get_text("words") for page in pages(doc)],
            "search": lambda doc: [page.search_for(NEEDLE) for page in pages(doc)],
            "render_72": lambda doc: [page.get_pixmap(dpi=72).samples for page in pages(doc)],
            "render_144": lambda doc: [page.get_pixmap(dpi=144).samples for page in pages(doc)],
            "save": lambda doc: doc.tobytes(),
        },
    )


def _pyropdf() -> tuple[str, Callable[[Path], Any], dict[str, Callable[[Any], object]]]:
    import pyropdf  # noqa: PLC0415 - only in the pyropdf environment

    return (
        f"pyropdf {pyropdf.__version__}",
        pyropdf.open,
        {
            "open": lambda doc: len(doc.pages),
            "text": lambda doc: [page.text() for page in doc.pages],
            "words": lambda doc: [page.words() for page in doc.pages],
            "search": lambda doc: [page.search(NEEDLE) for page in doc.pages],
            "render_72": lambda doc: [bytes(page.pixmap(dpi=72)) for page in doc.pages],
            "render_144": lambda doc: [bytes(page.pixmap(dpi=144)) for page in doc.pages],
            "save": lambda doc: doc.to_bytes(),
        },
    )


def _time(opener: Callable[[Path], Any], operation: Callable[[Any], object] | None, path: Path) -> float:
    """Return the best time of RUNS runs, in milliseconds, each on a fresh document."""
    best = float("inf")
    for _ in range(RUNS):
        start = time.perf_counter()
        doc = opener(path)
        if operation is not None:
            start = time.perf_counter()
            operation(doc)
        best = min(best, time.perf_counter() - start)
        doc.close()
    return best * 1000


def main() -> None:
    """Time every operation on every file and write the results as JSON."""
    engine, out, *files = sys.argv[1:]
    version, opener, operations = {"pymupdf": _pymupdf, "pyropdf": _pyropdf}[engine]()
    results: dict[str, dict[str, float]] = {}
    for name in files:
        path = Path(name)
        results[path.stem] = {
            key: _time(opener, None if key == "open" else operations[key], path) for key in OPERATIONS
        }
        print(engine, path.stem, " ".join(f"{key}={value:.1f}" for key, value in results[path.stem].items()))
    Path(out).write_text(
        json.dumps(
            {"engine": version, "python": platform.python_version(), "runs": RUNS, "results": results},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
