"""The documentation site matches the package: its examples run, its links resolve, and its reference is current."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest
from test_text import build_text_pdf

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
LANGUAGES = ("en", "ja")
PAGES = {language: sorted((DOCS / language).glob("*.md")) for language in LANGUAGES}
API_PAGES = sorted((DOCS / "en" / "api").glob("*.md"))
# A fence may carry options after the language, such as title="edit.py".
EXAMPLE = re.compile(r"^```python(?: [^\n]*)?\n(.*?)^```", re.MULTILINE | re.DOTALL)
# Generated signature/declaration blocks immediately follow `## ` or `### ` headings.
API_SIGNATURE = re.compile(r"^#{2,3} `[^`\n]+`\n\n```python\n(.*?)^```\n", re.MULTILINE | re.DOTALL)
FENCE = re.compile(r"^(?:```|~~~).*?^(?:```|~~~)[ \t]*$", re.MULTILINE | re.DOTALL)
SNIPPET = re.compile(r'^[ \t]*--8<--\s*"([^"]+)"[ \t]*$', re.MULTILINE)
ATTR_ID = re.compile(r"\s*\{#([A-Za-z0-9_:-]+)\}\s*$")
ENUM_REPR = re.compile(r"<[A-Za-z_][\w.]*:\s*[^>]+>")
MAX_SNIPPET_DEPTH = 8


def _examples() -> list[tuple[str, str]]:
    guide_examples = [
        (f"{page.relative_to(ROOT)}#{index}", code)
        for pages in PAGES.values()
        for page in pages
        for index, code in enumerate(EXAMPLE.findall(page.read_text(encoding="utf-8")))
    ]
    api_examples = [
        (f"{page.relative_to(ROOT)}#{index}", code)
        for page in API_PAGES
        for index, code in enumerate(EXAMPLE.findall(API_SIGNATURE.sub("", page.read_text(encoding="utf-8"))))
    ]
    return [*guide_examples, *api_examples]


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


def test_api_reference_examples_are_collected() -> None:
    api_ids = [where for where, _ in _examples() if where.startswith("docs/en/api/")]
    assert api_ids != []


def test_every_page_has_a_translation() -> None:
    names = {language: [page.name for page in pages] for language, pages in PAGES.items()}
    assert names["ja"] == names["en"]


@pytest.mark.skipif(sys.platform != "linux", reason="the worker example uses GNU timeout and util-linux prlimit")
def test_worker_example_reads_the_first_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "input.pdf").write_bytes(build_text_pdf("BT /F1 12 Tf 72 700 Td (two words) Tj ET"))
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent), prepend=os.pathsep)
    result = subprocess.run(  # noqa: S603 - run the same script that both guides include.
        ["/bin/sh", str(DOCS / "includes" / "read-in-worker.sh")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert result.stdout.strip() == "2"


def _expand_snippets(text: str, depth: int = 0) -> str:
    if depth >= MAX_SNIPPET_DEPTH:
        return text

    def replace(match: re.Match[str]) -> str:
        parts = match.group(1).split(":")
        target = (DOCS / parts[0]).resolve()
        assert target.is_relative_to(DOCS.resolve()), parts[0]
        assert target.is_file(), f"missing snippet: {parts[0]}"
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(parts) >= 2 and parts[1]:
            start = int(parts[1]) - 1
            end = int(parts[2]) if len(parts) >= 3 and parts[2] else None
            lines = lines[start:end]
        return _expand_snippets("".join(lines), depth + 1)

    return SNIPPET.sub(replace, text)


def _strip_inline_markdown(heading: str) -> str:
    heading = re.sub(r"`([^`]*)`", r"\1", heading)
    return re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", heading)


def _anchors_for(page: Path, language: str, cache: dict[tuple[Path, str], set[str]]) -> set[str]:
    key = (page.resolve(), language)
    if key in cache:
        return cache[key]
    no_code = FENCE.sub("", _expand_snippets(page.read_text(encoding="utf-8")))
    seen: set[str] = set(re.findall(r'\bid="([^"]+)"', no_code))
    for match in re.finditer(r"^#{1,6}\s+(.+?)\s*$", no_code, re.MULTILINE):
        heading = match.group(1)
        explicit = ATTR_ID.search(heading)
        if explicit is not None:
            seen.add(explicit.group(1))
            continue
        clean = _strip_inline_markdown(heading)
        if language == "en":
            normalized = unicodedata.normalize("NFKD", clean).encode("ascii", "ignore").decode("ascii")
        else:
            normalized = unicodedata.normalize("NFC", clean)
        slug = re.sub(r"[-\s]+", "-", re.sub(r"[^\w\s-]", "", normalized).strip().lower())
        unique, number = slug, 0
        while unique in seen:
            number += 1
            unique = f"{slug}_{number}"
        if unique:
            seen.add(unique)
    cache[key] = seen
    return seen


def _resolve_doc_target(page: Path, language: str, path_part: str) -> Path | None:
    if not path_part:
        return page
    if path_part.startswith("/ja/"):
        base_target = DOCS / "ja" / path_part.removeprefix("/ja/").strip("/")
    elif path_part.startswith("/"):
        # Files such as the hero's renders and `/api/` live in the English site, at the root.
        base_target = DOCS / "en" / path_part.lstrip("/").rstrip("/")
    else:
        base_target = page.parent / path_part.rstrip("/")
    resolved = base_target.resolve()
    if not resolved.is_relative_to(DOCS.resolve()):
        return None
    candidates = [
        resolved,
        resolved.with_suffix(".md") if not resolved.suffix else resolved,
        resolved / "index.md",
    ]
    if language == "ja" and not path_part.startswith("/") and resolved.suffix not in {"", ".md"}:
        shared = (DOCS / "en" / page.relative_to(DOCS / "ja").parent / path_part).resolve()
        if shared.is_relative_to((DOCS / "en").resolve()):
            candidates.append(shared)
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def test_internal_links_resolve() -> None:
    anchor_cache: dict[tuple[Path, str], set[str]] = {}
    broken: list[str] = []
    for language in LANGUAGES:
        for page in sorted((DOCS / language).rglob("*.md")):
            expanded = _expand_snippets(page.read_text(encoding="utf-8"))
            prose = re.sub(r"`[^`\n]+`", "", FENCE.sub("", expanded))
            links = [
                *re.findall(r'!?\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)', prose),
                *re.findall(r'(?:href|src)="([^"]+)"', prose),
            ]
            for link in links:
                if link.startswith(("http://", "https://", "mailto:", "data:")):
                    continue
                path_part, _, fragment = link.partition("#")
                target = _resolve_doc_target(page, language, path_part)
                if target is None:
                    broken.append(f"{page.relative_to(ROOT)} -> missing target {link!r}")
                    continue
                if fragment and target.suffix == ".md":
                    target_lang = "ja" if target.is_relative_to((DOCS / "ja").resolve()) else "en"
                    if fragment not in _anchors_for(target, target_lang, anchor_cache):
                        broken.append(
                            f"{page.relative_to(ROOT)} -> missing anchor {link!r} in {target.relative_to(ROOT)}"
                        )
    assert broken == []


def test_api_reference_formatting_invariants() -> None:
    or_set_entries: list[str] = []
    enum_reprs: list[str] = []
    for page in sorted((DOCS / "en" / "api").glob("*.md")):
        text = page.read_text(encoding="utf-8")
        rel = page.relative_to(ROOT)
        or_set_entries.extend(
            f"{rel}:{lineno}: {line}"
            for lineno, line in enumerate(text.splitlines(), start=1)
            if line.startswith("Or set ")
        )
        enum_reprs.extend(
            f"{rel}: {match.group(1).strip()}"
            for match in API_SIGNATURE.finditer(text)
            if ENUM_REPR.search(match.group(1))
        )
    assert or_set_entries == []
    assert enum_reprs == []


def test_the_api_reference_is_current() -> None:
    result = subprocess.run(  # noqa: S603 - runs the repository's own generator.
        [sys.executable, str(ROOT / "tools" / "api_reference.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
