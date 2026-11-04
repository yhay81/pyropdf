"""Rendering the CJK fonts that a document does not embed with fallback faces (#600).

Ported from pylopdf 4ff4a266 ``tests/test_locale_fonts.py``, its rendering
cases, with the faces of the installed font extras, which the dev group
installs, and the subset of Noto Sans JP in the engine's test data, which
draws 日本語. Intended differences:

- ``FallbackFont`` takes its faces by keyword, as pyropdf's specifications
  do, and a language is exactly ``"ja"``, ``"ko"``, ``"zh-CN"``, or
  ``"zh-TW"``, without pylopdf's aliases;
- rendering reads an installed face the first time that a page needs it,
  so the cases of pylopdf's package-font cache, which loads every installed
  face up front and shares it with text generation, do not apply: pyropdf
  draws generated text only in the fonts passed in;
- text extraction reads the character collection's Unicode and does not use
  the fallback faces, except for the exact boxes of text (#624), so other
  text keeps its memo key.
"""

from __future__ import annotations

import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_content import build_raw_pdf

import pyropdf
from pyropdf import FallbackFont, Font, _core

ROOT = Path(__file__).resolve().parents[1]
#: The subset of Noto Sans JP in the engine's test data, which draws 日本語.
SUBSET = ROOT / "crates" / "pyropdf-engine" / "src" / "fonts" / "testdata" / "NotoSansJP-subset.otf"
#: The pages of PAGES in one file, which the differential tests against
#: frozen pylopdf read too (evidence/phase1-parity).
FIXTURE = ROOT / "tests" / "assets" / "text" / "nonembedded-cjk.pdf"

#: Pages that show a text at 40 points in a CID font that the file does not
#: embed: its BaseFont, descendant subtype, CMap, Adobe character
#: collection, and text. The first three are #600's MS-Gothic page in both
#: descendant types and both CMaps.
PAGES = (
    ("MS-Gothic", "CIDFontType0", "UniJIS-UCS2-H", "Japan1", "計画地"),
    ("MS-Gothic", "CIDFontType2", "UniJIS-UCS2-H", "Japan1", "計画地"),
    ("MS-Gothic", "CIDFontType2", "90ms-RKSJ-H", "Japan1", "計画地"),
    ("MS-Mincho", "CIDFontType2", "UniJIS-UCS2-H", "Japan1", "計画地"),
    ("SimHei", "CIDFontType2", "UniGB-UCS2-H", "GB1", "简体中文"),
    ("MingLiU", "CIDFontType2", "UniCNS-UCS2-H", "CNS1", "繁體中文"),
    ("Batang", "CIDFontType2", "UniKS-UCS2-H", "Korea1", "한국어"),
)
#: A page of 日本語, which the subset face draws.
JAPANESE = ("MS-Gothic", "CIDFontType2", "UniJIS-UCS2-H", "Japan1", "日本語")
#: The font package of each language.
PACKAGES = {
    "ja": "pylopdf_fonts_jp",
    "ko": "pylopdf_fonts_ko",
    "zh-CN": "pylopdf_fonts_zh_cn",
    "zh-TW": "pylopdf_fonts_zh_tw",
}


def _font_objects(  # noqa: PLR0913 - a page of PAGES, and the entries that some tests add.
    first: int, name: str, subtype: str, encoding: str, ordering: str, *, descendant: str = ""
) -> dict[int, str]:
    """Return a Type0 font that the file does not embed, numbered from ``first``, with its descendant and descriptor."""
    return {
        first: (
            f"<< /Type /Font /Subtype /Type0 /BaseFont /{name} /Encoding /{encoding} "
            f"/DescendantFonts [{first + 1} 0 R] >>"
        ),
        first + 1: (
            f"<< /Type /Font /Subtype /{subtype} /BaseFont /{name} "
            f"/CIDSystemInfo << /Registry (Adobe) /Ordering ({ordering}) /Supplement 2 >> "
            f"/FontDescriptor {first + 2} 0 R /DW 1000{descendant} >>"
        ),
        first + 2: (
            f"<< /Type /FontDescriptor /FontName /{name} /Flags 4 /FontBBox [0 -141 1000 859] "
            "/ItalicAngle 0 /Ascent 859 /Descent -141 /CapHeight 709 /StemV 80 >>"
        ),
    }


def _shown(encoding: str, text: str) -> str:
    """Return the content that shows ``text`` at 40 points in ``/F1``, in the codes of ``encoding``."""
    codes = text.encode("cp932" if encoding == "90ms-RKSJ-H" else "utf-16-be").hex().upper()
    return f"BT /F1 40 Tf 20 30 Td <{codes}> Tj ET"


def nonembedded_cjk_pdf(*pages: tuple[str, str, str, str, str], descendant: str = "") -> bytes:
    """Build one 200 by 100 point page for each of ``pages``, as PAGES describes them.

    Each descendant font also holds the entries ``descendant``.
    """
    objects = {1: "<< /Type /Catalog /Pages 2 0 R >>"}
    kids = []
    for at, (name, subtype, encoding, ordering, text) in enumerate(pages):
        page = 3 + 5 * at
        stream = _shown(encoding, text)
        objects[page] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] "
            f"/Resources << /Font << /F1 {page + 2} 0 R >> >> /Contents {page + 1} 0 R >>"
        )
        objects[page + 1] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
        objects |= _font_objects(page + 2, name, subtype, encoding, ordering, descendant=descendant)
        kids.append(f"{page} 0 R")
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    return build_raw_pdf(objects)


def _annotated_pdf() -> bytes:
    """Build a page that shows 日本語 in its content and in an annotation's appearance."""
    content = _shown("UniJIS-UCS2-H", "日本語")
    appearance = "BT /F1 20 Tf 4 6 Td <65E5672C8A9E> Tj ET"
    return build_raw_pdf({
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 5 0 R >> >> "
            "/Contents 4 0 R /Annots [8 0 R] >>"
        ),
        4: f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        **_font_objects(5, *JAPANESE[:4]),
        8: "<< /Type /Annot /Subtype /Square /Rect [20 120 100 150] /AP << /N 9 0 R >> >>",
        9: (
            "<< /Type /XObject /Subtype /Form /BBox [0 0 80 30] /Resources << /Font << /F1 5 0 R >> >> "
            f"/Length {len(appearance)} >>\nstream\n{appearance}\nendstream"
        ),
    })  # fmt: skip


def _ink(pixmap: pyropdf.Pixmap) -> int:
    """Return the pixels that have a channel darker than mid-gray, as #600 counts them."""
    data, channels, stride = bytes(pixmap), pixmap.channels, pixmap.stride
    return sum(
        min(data[y * stride + x * channels : y * stride + x * channels + 3]) < 128
        for y in range(pixmap.height)
        for x in range(pixmap.width)
    )


def _empty_glyphs(page: pyropdf.Page) -> int:
    """Return the glyphs that ``page.svg()`` defines without an outline."""
    return page.svg().count(' d=""')


def _render(data: bytes, fonts: dict[str, FallbackFont] | None = None) -> bytes:
    """Return the samples of the first page at 72 dpi, with ``fonts`` or the installed extras."""
    doc = pyropdf.open(data)
    if fonts is not None:
        doc.fallback_fonts = fonts
    return bytes(doc.pages[0].pixmap())


def _installed(language: str) -> FallbackFont:
    """Return the faces of the installed font package of ``language``, loaded from its files."""
    package = pytest.importorskip(PACKAGES[language])
    return FallbackFont(sans=Font.from_file(package.sans_path()), serif=Font.from_file(package.serif_path()))


def test_the_fixture_holds_the_pages() -> None:
    assert FIXTURE.read_bytes() == nonembedded_cjk_pdf(*PAGES)
    with pyropdf.open(FIXTURE) as doc:
        assert [page.text().strip() for page in doc.pages] == [text for *_, text in PAGES]


@pytest.mark.parametrize("spec", PAGES[:3], ids=lambda spec: f"{spec[2]}-{spec[1]}")
def test_the_installed_japanese_faces_draw_the_page(spec: tuple[str, str, str, str, str]) -> None:
    pytest.importorskip("pylopdf_fonts_jp")
    data = nonembedded_cjk_pdf(spec)
    with pyropdf.open(data) as doc:
        page = doc.pages[0]
        assert "ja" in doc.fallback_fonts
        assert _ink(page.pixmap(dpi=72)) > 1000
        assert _empty_glyphs(page) == 0
        assert page.text().strip() == "計画地"

        doc.fallback_fonts = {}
        assert _ink(page.pixmap(dpi=72)) == 0
        assert _empty_glyphs(page) == 3
        assert page.text().strip() == "計画地"


def test_a_mincho_font_takes_the_serif_face() -> None:
    japanese = _installed("ja")
    mincho, gothic = nonembedded_cjk_pdf(PAGES[3]), nonembedded_cjk_pdf(PAGES[1])
    serif_only = {"ja": FallbackFont(serif=japanese.serif)}
    sans_only = {"ja": FallbackFont(sans=japanese.sans)}

    # The installed files draw as the loaded faces of the same files do.
    assert _render(mincho) == _render(mincho, serif_only)
    assert _render(mincho) != _render(mincho, sans_only)
    assert _render(gothic) == _render(gothic, sans_only)
    assert _render(gothic) != _render(gothic, serif_only)


@pytest.mark.parametrize(
    ("spec", "language"),
    [(PAGES[4], "zh-CN"), (PAGES[5], "zh-TW"), (PAGES[6], "ko")],
    ids=["GB1", "CNS1", "Korea1"],
)
def test_each_collection_takes_the_faces_of_its_language(spec: tuple[str, str, str, str, str], language: str) -> None:
    faces = _installed(language)
    others = {other: _installed(other) for other in PACKAGES if other != language}
    data = nonembedded_cjk_pdf(spec)
    blank = _render(data, {})

    drawn = _render(data)
    assert drawn == _render(data, {language: faces})
    assert drawn != blank
    # Another language's faces never draw this collection's font.
    assert _render(data, others) == blank
    doc = pyropdf.open(data)
    del doc.fallback_fonts[language]
    assert bytes(doc.pages[0].pixmap()) == blank
    assert doc.pages[0].text().strip() == spec[4]


def test_without_installed_faces_the_text_renders_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyropdf._fonts._font_packages", tuple)
    with pyropdf.open(nonembedded_cjk_pdf(PAGES[0])) as doc:
        assert list(doc.fallback_fonts) == []
        assert repr(doc.fallback_fonts) == "FallbackFonts([])"
        page = doc.pages[0]
        assert _ink(page.pixmap()) == 0
        assert _empty_glyphs(page) == 3
        assert page.text().strip() == "計画地"


def test_discovery_imports_the_four_cjk_font_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    def absent(name: str) -> None:
        requested.append(name)

    discover = pyropdf._fonts._font_packages  # noqa: SLF001 - the discovery under test.
    discover.cache_clear()
    try:
        with monkeypatch.context() as patch:
            patch.setattr("pyropdf._fonts._import_font_package", absent)
            assert discover() == ()
    finally:
        discover.cache_clear()
    assert requested == list(PACKAGES.values())


def test_a_broken_font_package_is_not_taken_for_an_absent_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "pyropdf_test_broken_fonts").mkdir()
    (tmp_path / "pyropdf_test_broken_fonts" / "__init__.py").write_text("import pyropdf_test_absent_dependency\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    load = pyropdf._fonts._import_font_package  # noqa: SLF001 - the import under test.

    assert load("pyropdf_test_absent_fonts") is None
    with pytest.raises(ModuleNotFoundError, match="pyropdf_test_absent_dependency"):
        load("pyropdf_test_broken_fonts")


def test_each_font_package_has_its_extra() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = project["project"]["optional-dependencies"]
    development = project["dependency-groups"]["dev"]
    for language, extra, module in pyropdf._fonts._FONT_PACKAGES:  # noqa: SLF001 - the packages that discovery finds.
        assert PACKAGES[language] == module
        requirement = f"{module.replace('_', '-')}>=0.1.1,<0.2"
        assert extras[extra] == [requirement]
        assert requirement in development


def test_fallback_fonts_list_the_installed_extras_until_edited() -> None:
    serif = _installed("ja").serif
    data = nonembedded_cjk_pdf(PAGES[3])
    doc = pyropdf.open(data)
    installed = list(doc.fallback_fonts)
    assert "ja" in installed
    assert installed == [language for language in PACKAGES if language in installed]
    assert repr(doc.fallback_fonts) == f"FallbackFonts({installed!r})"
    japanese = doc.fallback_fonts["ja"]
    assert japanese == doc.fallback_fonts["ja"]
    assert japanese.sans is not None
    assert japanese.serif is not None
    assert japanese.serif.is_serif
    assert not japanese.sans.is_serif
    installed_render = bytes(doc.pages[0].pixmap())

    # Editing one language keeps the installed extras of the others.
    doc.fallback_fonts["ja"] = FallbackFont(serif=serif)
    assert list(doc.fallback_fonts) == installed
    assert doc.fallback_fonts["ja"] == FallbackFont(serif=serif)
    assert bytes(doc.pages[0].pixmap()) == installed_render

    del doc.fallback_fonts["ja"]
    assert "ja" not in doc.fallback_fonts
    assert bytes(doc.pages[0].pixmap()) == _render(data, {})
    with pytest.raises(KeyError):
        del doc.fallback_fonts["ja"]
    with pytest.raises(KeyError):
        doc.fallback_fonts["ja"]

    del doc.fallback_fonts  # Back to the installed extras.
    assert list(doc.fallback_fonts) == installed
    assert bytes(doc.pages[0].pixmap()) == installed_render

    doc.fallback_fonts = {}
    assert len(doc.fallback_fonts) == 0
    assert bytes(doc.pages[0].pixmap()) == _render(data, {})


def test_a_fallback_font_checks_its_faces() -> None:
    font = Font.from_file(SUBSET)
    assert FallbackFont(sans=font) == FallbackFont(sans=font, serif=None)
    assert hash(FallbackFont(serif=font)) == hash(FallbackFont(serif=font))
    with pytest.raises(ValueError, match="a sans face, a serif face, or both"):
        FallbackFont()
    with pytest.raises(ValueError, match="Standard 14"):
        FallbackFont(sans=Font.standard("Helvetica"))
    with pytest.raises(TypeError, match="Font or None"):
        FallbackFont(serif=SUBSET)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        FallbackFont(font)  # ty: ignore[too-many-positional-arguments]


def test_fallback_fonts_refuse_before_any_change() -> None:
    subset = FallbackFont(sans=Font.from_file(SUBSET))
    doc = pyropdf.open(nonembedded_cjk_pdf(JAPANESE))
    doc.fallback_fonts = {"ja": subset}
    drawn = bytes(doc.pages[0].pixmap())

    with pytest.raises(ValueError, match="'ja', 'ko', 'zh-CN', or 'zh-TW'"):
        doc.fallback_fonts = {"ko": subset, "th": subset}
    with pytest.raises(ValueError, match="'ja', 'ko', 'zh-CN', or 'zh-TW'"):
        doc.fallback_fonts["zh-cn"] = subset
    with pytest.raises(TypeError, match="FallbackFont"):
        doc.fallback_fonts["ko"] = Font.from_file(SUBSET)  # ty: ignore[invalid-assignment]
    with pytest.raises(TypeError, match="FallbackFont"):
        doc.fallback_fonts = {"ko": None}  # ty: ignore[invalid-assignment]
    with pytest.raises(TypeError, match="mapping"):
        doc.fallback_fonts = [("ko", subset)]  # ty: ignore[invalid-assignment]
    with pytest.raises(TypeError, match="a str"):
        doc.fallback_fonts[1]  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="'ja', 'ko', 'zh-CN', or 'zh-TW'"):
        doc.fallback_fonts["th"]
    assert "th" not in doc.fallback_fonts
    assert 1 not in doc.fallback_fonts
    with pytest.raises(KeyError):
        del doc.fallback_fonts["ko"]

    assert dict(doc.fallback_fonts) == {"ja": subset}
    assert bytes(doc.pages[0].pixmap()) == drawn
    doc.close()
    with pytest.raises(pyropdf.DocumentClosedError):
        _ = doc.fallback_fonts


def test_the_next_rendering_follows_a_change_of_the_fallback_fonts() -> None:
    doc = pyropdf.open(nonembedded_cjk_pdf(JAPANESE))
    page = doc.pages[0]
    doc.fallback_fonts = {}
    blank = page.pixmap()
    assert _ink(blank) == 0

    # One version of the document, rendered through each thread's cache.
    doc.fallback_fonts["ja"] = FallbackFont(sans=Font.from_file(SUBSET))
    drawn = page.pixmap()
    assert _ink(drawn) > 500
    with ThreadPoolExecutor(4) as pool:
        assert set(pool.map(lambda _: bytes(page.pixmap()), range(8))) == {bytes(drawn)}
    del doc.fallback_fonts["ja"]
    assert bytes(page.pixmap()) == bytes(blank)
    with ThreadPoolExecutor(4) as pool:
        assert set(pool.map(lambda _: bytes(page.pixmap()), range(8))) == {bytes(blank)}


def test_every_rendering_draws_with_the_fallback_fonts() -> None:
    doc = pyropdf.open(_annotated_pdf())
    page = doc.pages[0]
    (annotation,) = page.annotations
    doc.fallback_fonts = {"ja": FallbackFont(sans=Font.from_file(SUBSET))}

    assert _ink(page.pixmap(annotations=False)) > 500
    assert _ink(annotation.pixmap()) > 100
    assert doc.render(workers=2) == [page.pixmap().to_bytes()]
    assert _empty_glyphs(page) == 0

    doc.fallback_fonts = {}
    assert _ink(page.pixmap()) == 0
    assert _ink(annotation.pixmap()) == 0
    assert doc.render() == [page.pixmap().to_bytes()]
    assert _empty_glyphs(page) > 0


def test_other_fonts_render_as_without_fallback_fonts() -> None:
    content = "BT /F1 24 Tf 10 60 Td (Helvetica) Tj ET BT /F2 24 Tf 10 20 Td (Arial) Tj ET"
    data = build_raw_pdf({
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] "
            "/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>"
        ),
        4: f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        6: (
            "<< /Type /Font /Subtype /TrueType /BaseFont /ArialMT /FirstChar 32 /LastChar 126 "
            "/Encoding /WinAnsiEncoding /FontDescriptor 7 0 R >>"
        ),
        7: (
            "<< /Type /FontDescriptor /FontName /ArialMT /Flags 32 /FontBBox [-665 -325 2000 1006] "
            "/ItalicAngle 0 /Ascent 905 /Descent -212 /CapHeight 716 /StemV 80 >>"
        ),
    })  # fmt: skip
    every = {language: FallbackFont(sans=Font.from_file(SUBSET)) for language in PACKAGES}
    plain = _render(data, {})
    assert _ink(pyropdf.open(data).pages[0].pixmap()) > 0
    assert _render(data) == plain
    assert _render(data, every) == plain


def test_text_without_exact_boxes_does_not_use_the_fallback_fonts() -> None:
    data = nonembedded_cjk_pdf(JAPANESE)

    def read(fonts: dict[str, FallbackFont]) -> tuple[str, object, object]:
        doc = pyropdf.open(data)
        doc.fallback_fonts = fonts
        page = doc.pages[0]
        words = [(word.text, tuple(word.bbox)) for word in page.words()]
        return page.text(), words, page.layout(chars=True, styles=True).to_dict()

    without = read({})
    assert without[0].strip() == "日本語"
    assert read({"ja": FallbackFont(sans=Font.from_file(SUBSET))}) == without


def _exact_words(page: pyropdf.Page) -> list[tuple[str, tuple[float, ...]]]:
    """Return the text and the exact box of each word of ``page``."""
    return [(word.text, tuple(word.bbox)) for word in page.words(exact_bboxes=True)]


def _counts(document: pyropdf.Document) -> tuple[int, int]:
    """Return the hits and the computations of the document's page results."""
    return _core.page_result_counts(document._core())  # noqa: SLF001


def _ink_box(pixmap: pyropdf.Pixmap) -> tuple[int, int, int, int]:
    """Return the box of the pixels that have a channel darker than mid-gray."""
    data, channels, stride = bytes(pixmap), pixmap.channels, pixmap.stride
    inked = [
        (x, y)
        for y in range(pixmap.height)
        for x in range(pixmap.width)
        if min(data[y * stride + x * channels : y * stride + x * channels + 3]) < 128
    ]
    xs, ys = zip(*inked, strict=True)
    return min(xs), min(ys), max(xs) + 1, max(ys) + 1


def test_exact_boxes_measure_the_face_that_draws_the_font() -> None:
    doc = pyropdf.open(nonembedded_cjk_pdf(JAPANESE))
    page = doc.pages[0]
    doc.fallback_fonts = {}
    # Without a face the glyphs have no outline, so they keep the font's box.
    without = _exact_words(page)
    assert without == [(word.text, tuple(word.bbox)) for word in page.words()]
    plain_span = page.layout(exact_bboxes=True).blocks[0].lines[0].spans[0]

    doc.fallback_fonts = {"ja": FallbackFont(sans=Font.from_file(SUBSET))}
    ((text, box),) = _exact_words(page)
    assert text == without[0][0]
    assert box != without[0][1]
    # The box is the one around what a rendering draws, within a pixel.
    assert box == pytest.approx(_ink_box(page.pixmap(dpi=72)), abs=1)

    # The other exact boxes of text measure the same outlines.
    layout = page.layout(chars=True, exact_bboxes=True)
    span = layout.blocks[0].lines[0].spans[0]
    assert tuple(span.bbox) == box
    assert tuple(span.chars[0].bbox | span.chars[1].bbox | span.chars[2].bbox) == box
    assert [tuple(rect) for rect in page.search(text, exact_bboxes=True)] == [box]
    # A face describes no font of the PDF, so the font keeps its metrics and name.
    assert (span.ascender, span.descender, span.font) == (plain_span.ascender, plain_span.descender, plain_span.font)
    # Exact boxes decide what a clip keeps, so the clip reads the face too.
    x0, y0, x1, y1 = box
    assert [word.text for word in page.words(clip=(x0 - 1, y0 - 1, x1 + 1, y1 + 1), exact_bboxes=True)] == [text]


def test_a_stretched_face_keeps_the_words_of_the_page() -> None:
    # /W holds half the widths of the face's glyphs, which draw at half their width.
    doc = pyropdf.open(nonembedded_cjk_pdf(JAPANESE, descendant=" /W [0 65535 500]"))
    page = doc.pages[0]
    doc.fallback_fonts = {"ja": FallbackFont(sans=Font.from_file(SUBSET))}
    plain = [word.text for word in page.words()]
    assert [word.text for word in page.words(exact_bboxes=True)] == plain == ["日本語"]


def test_editing_the_fallback_fonts_changes_the_exact_boxes_of_the_next_call() -> None:
    subset = FallbackFont(sans=Font.from_file(SUBSET))
    installed = pyropdf.open(FIXTURE).fallback_fonts["ja"]
    assert installed.serif is not None
    serif = FallbackFont(sans=installed.serif)
    doc = pyropdf.open(nonembedded_cjk_pdf(JAPANESE))
    page = doc.pages[0]

    doc.fallback_fonts = {"ja": subset}
    first = _exact_words(page)
    hits, computations = _counts(doc)
    # The same faces reuse the result, however they are assigned.
    assert _exact_words(page) == first
    doc.fallback_fonts = {"ja": subset}
    assert _exact_words(page) == first
    doc.fallback_fonts["ja"] = subset
    assert _exact_words(page) == first
    assert _counts(doc) == (hits + 3, computations)

    # Other faces compute it again, and going back finds the first result.
    doc.fallback_fonts["ja"] = serif
    second = _exact_words(page)
    assert second != first
    assert _counts(doc) == (hits + 3, computations + 1)
    doc.fallback_fonts = {}
    assert _exact_words(page) not in (first, second)
    assert _counts(doc) == (hits + 3, computations + 2)
    doc.fallback_fonts = {"ja": subset}
    assert _exact_words(page) == first
    assert _counts(doc) == (hits + 4, computations + 2)

    # The memo of the layout with chars keys the faces too.
    exact = page.layout(chars=True, exact_bboxes=True).to_dict()
    doc.fallback_fonts["ja"] = serif
    assert page.layout(chars=True, exact_bboxes=True).to_dict() != exact


def test_the_memo_of_other_text_keeps_its_key_when_the_fallback_fonts_change() -> None:
    subset = FallbackFont(sans=Font.from_file(SUBSET))
    doc = pyropdf.open(nonembedded_cjk_pdf(JAPANESE))
    page = doc.pages[0]
    before = (page.text(), page.words(), page.layout(chars=True).to_dict())
    hits, computations = _counts(doc)

    doc.fallback_fonts = {"ja": subset}
    assert (page.text(), page.words(), page.layout(chars=True).to_dict()) == before
    assert _counts(doc) == (hits + 3, computations)


def test_fallback_fonts_survive_unlock() -> None:
    encryption = pyropdf.Encryption(user_password="user", owner_password="owner")  # noqa: S106 - public test fixture.
    data = pyropdf.open(nonembedded_cjk_pdf(JAPANESE)).to_bytes(encryption=encryption)
    subset = FallbackFont(sans=Font.from_file(SUBSET))
    doc = pyropdf.open(data, password="user")  # noqa: S106 - public test fixture.
    doc.fallback_fonts = {"ja": subset}
    before = bytes(doc.pages[0].pixmap())
    assert before == _render(nonembedded_cjk_pdf(JAPANESE), {"ja": subset})

    assert doc.unlock("owner") == pyropdf.Access.OWNER

    assert bytes(doc.pages[0].pixmap()) == before
    assert dict(doc.fallback_fonts) == {"ja": subset}


def test_one_face_draws_every_document_that_shares_it() -> None:
    subset = FallbackFont(sans=Font.from_file(SUBSET))
    data = nonembedded_cjk_pdf(JAPANESE)
    documents = [pyropdf.open(data) for _ in range(3)]
    for doc in documents:
        doc.fallback_fonts = {"ja": subset}

    renders = {bytes(doc.pages[0].pixmap()) for doc in documents}
    assert len(renders) == 1
    assert renders != {_render(data, {})}
