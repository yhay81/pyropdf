"""Select representative corpus cases for the short developer and CI checks."""

from __future__ import annotations

from pathlib import Path

import pytest

# A minimal PDF 2.0 file and a document with existing widgets, links and highlights.
QUICK_SOURCES = {"pdf20-simple.pdf", "pdfium-links-highlights-annots.pdf"}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--quick-corpus",
        action="store_true",
        help="Run every edit on two representative PDFs and the rotate edit on every corpus PDF.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "corpus_matrix: checks each source, edit and save mode")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--quick-corpus"):
        return
    selected = []
    deselected = []
    for item in items:
        if item.get_closest_marker("corpus_matrix") is not None:
            assert isinstance(item, pytest.Function)
            params = item.callspec.params
            source = params["source"]
            assert isinstance(source, Path)
            if source.name not in QUICK_SOURCES and params["edit"] != "rotate":
                deselected.append(item)
                continue
        selected.append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)
