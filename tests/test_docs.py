"""The documentation site matches the package: its examples run, its reference is current."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from test_text import build_text_pdf

ROOT = Path(__file__).resolve().parent.parent
LANGUAGES = ("en", "ja")
PAGES = {language: sorted((ROOT / "docs" / language).glob("*.md")) for language in LANGUAGES}
EXAMPLE = re.compile(r"^```python\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _examples() -> list[tuple[str, str]]:
    return [
        (f"{page.relative_to(ROOT)}#{index}", code)
        for pages in PAGES.values()
        for page in pages
        if page.name != "api.md"  # signatures, not examples
        for index, code in enumerate(EXAMPLE.findall(page.read_text(encoding="utf-8")))
    ]


@pytest.mark.parametrize(("where", "code"), _examples(), ids=[where for where, _ in _examples()])
def test_the_example_runs(where: str, code: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "input.pdf").write_bytes(
        build_text_pdf(
            "BT /F1 24 Tf 72 700 Td (Invoice) Tj ET BT /F1 12 Tf 72 650 Td (total 42) Tj ET",
            "BT /F1 12 Tf 72 700 Td (Second page) Tj ET",
        )
    )
    monkeypatch.chdir(tmp_path)

    exec(compile(code, where, "exec"), {"__name__": "__main__"})  # noqa: S102 - the site's own examples.


def test_every_page_has_a_translation() -> None:
    names = {language: [page.name for page in pages] for language, pages in PAGES.items()}
    english = [name for name in names["en"] if name != "api.md"]
    assert names["ja"] == english


def test_the_home_pages_link_to_pages_that_exist() -> None:
    for language in LANGUAGES:
        home = (ROOT / "docs" / language / "index.md").read_text(encoding="utf-8")
        targets = {link.split("#")[0].strip("/") for link in re.findall(r'href="([^":]+)"', home)}
        missing = [target for target in targets if target and not (ROOT / "docs" / language / f"{target}.md").exists()]
        assert missing == [], language


def test_the_api_reference_is_current() -> None:
    result = subprocess.run(  # noqa: S603 - runs the repository's own generator.
        [sys.executable, str(ROOT / "tools" / "api_reference.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
