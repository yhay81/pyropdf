# ruff: noqa: INP001, T201, S603, S607 - a script that builds the harness and prints its table.
"""Time the two steps of hayro-syntax's ``Pdf::new`` at 110, 440, and 1,760 pages (issue #585).

Usage, from the repository root:

    uv run python evidence/hayro-page-list/run.py

``Pdf::new`` reads the cross-reference data (``root_xref``) and then builds
the page list (``CachedPages::new``). Both are crate-private, so this copies
the hayro-syntax that the workspace locks from the Cargo registry into
``target/evidence/hayro-page-list/``, wraps both calls in timers, and builds
the harness against that copy. pyropdf joins copies of bill-hr815.pdf with
``pages.extend``, as pylopdf does in evidence/edit-render. It prints Markdown.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pyropdf

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / "target" / "evidence" / "hayro-page-list"
BILL = ROOT / "tests" / "assets" / "real_world" / "bill-hr815.pdf"
COPIES = (1, 4, 16)

#: The two calls in ``Pdf::new_with_password`` that the timers wrap.
XREF_CALL = "        let xref = match root_xref(data.clone(), password) {\n"
PAGES_CALL = "        let pages = CachedPages::new(xref.clone()).ok_or(LoadPdfError::Invalid)?;\n"

#: Each edit of ``src/pdf.rs``: the text it replaces, and the replacement.
PATCH = (
    (XREF_CALL, "        let timer = std::time::Instant::now();\n" + XREF_CALL),
    (
        PAGES_CALL,
        (
            "        let xref_time = timer.elapsed();\n"
            "        let timer = std::time::Instant::now();\n"
            + PAGES_CALL
            + "        load_split::record(xref_time, timer.elapsed());\n"
        ),
    ),
)

#: Appended to ``src/pdf.rs``; ``lib.rs`` re-exports it as ``hayro_syntax::load_split``.
TIMERS = """
/// The two steps of the last `Pdf::new` on this thread (evidence/hayro-page-list).
pub mod load_split {
    use std::cell::Cell;
    use std::time::Duration;

    std::thread_local! {
        static LAST: Cell<(Duration, Duration)> = const { Cell::new((Duration::ZERO, Duration::ZERO)) };
    }

    pub(crate) fn record(xref: Duration, pages: Duration) {
        LAST.with(|last| last.set((xref, pages)));
    }

    /// The time of reading the cross-reference data, and of building the page list.
    pub fn last() -> (Duration, Duration) {
        LAST.with(Cell::get)
    }
}
"""


def build_documents() -> list[Path]:
    """Write the documents of 110, 440, and 1,760 pages and return their paths."""
    paths = []
    for copies in COPIES:
        doc = pyropdf.open(BILL)
        for _ in range(copies - 1):
            doc.pages.extend(pyropdf.open(BILL).pages)
        path = OUT / f"pages-{len(doc.pages)}.pdf"
        path.write_bytes(doc.to_bytes())
        paths.append(path)
    return paths


def patched_crate() -> str:
    """Copy the locked hayro-syntax into ``OUT``, add the timers, and return its version."""
    metadata = json.loads(
        subprocess.run(
            ["cargo", "metadata", "--format-version", "1", "--locked", "--manifest-path", str(ROOT / "Cargo.toml")],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    (package,) = (p for p in metadata["packages"] if p["name"] == "hayro-syntax")
    copy = OUT / "hayro-syntax"
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(Path(package["manifest_path"]).parent, copy)
    source = copy / "src" / "pdf.rs"
    text = source.read_text(encoding="utf-8")
    for old, new in PATCH:
        if text.count(old) != 1:
            sys.exit(f"hayro-syntax {package['version']}: the patch no longer applies to src/pdf.rs")
        text = text.replace(old, new)
    source.write_text(text + TIMERS, encoding="utf-8")
    return package["version"]


def main() -> None:
    """Build the documents and the patched crate, run the harness, and print the table."""
    OUT.mkdir(parents=True, exist_ok=True)
    documents = build_documents()
    version = patched_crate()
    table = subprocess.run(
        ["cargo", "run", "--release", "--quiet", "--locked", "--manifest-path", str(HERE / "Cargo.toml"), "--"]
        + [str(path) for path in documents],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rustc = subprocess.run(["rustc", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    print(f"- hayro-syntax {version}, pyropdf {pyropdf.__version__}, {rustc}")
    print(f"- {platform.system()} {platform.machine()}; median of 50 opens per document")
    print()
    print(table.strip())


if __name__ == "__main__":
    main()
