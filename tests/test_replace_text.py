"""Replacing text that simple fonts draw with ``page.replace_text``.

The ``replace_text`` cases of pylopdf's tests/test_draw.py at ebb01d3c,
ported. Intended differences:

- the arguments are ``old``, ``new``, and ``count``, as the issue names
  them, instead of ``search``, ``replacement``, and ``default_char``;
- a character that the font has no code for raises ``ValueError`` and
  leaves the document unchanged, instead of becoming ``default_char``;
- the following text keeps its position: a kerning number after the
  replacement makes up the difference in advance, where lopdf's model left
  the widths alone;
- a text-showing operation without a usable font is left alone and counts
  no replacement, where lopdf's model refused the page;
- the input and output guards of pylopdf (``max_size``, the 4,096-byte
  input, and the shape of ``/Contents``) are not ported (DESIGN.md section
  14): the limits of ``page.operations`` and of the document bound the work.
"""

from __future__ import annotations

import pytest

import pyropdf

HELVETICA = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


def build_raw_pdf(objects: dict[int, str]) -> bytes:
    """Return a PDF of ``objects`` by number, with a correct cross-reference table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode("latin-1")
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 00000 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def stream(content: str, entries: str = "") -> str:
    return f"<< /Length {len(content.encode('latin-1'))} {entries}>>\nstream\n{content}\nendstream"


def content_pdf(*contents: str, font: str = HELVETICA, resources: str = "") -> bytes:
    """Return one page per content stream, with ``/F1`` as ``font`` on the page tree."""
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(contents)))
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {len(contents)} /MediaBox [0 0 612 792] "
        f"/Resources << /Font << /F1 3 0 R >> {resources}>> >>",
        3: font,
    }
    for i, content in enumerate(contents):
        objects[4 + 2 * i] = f"<< /Type /Page /Parent 2 0 R /Contents {5 + 2 * i} 0 R >>"
        objects[5 + 2 * i] = stream(content)
    return build_raw_pdf(objects)


def build_pdf(texts: list[str]) -> bytes:
    return content_pdf(*(f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET" for text in texts))


def boxes(page: pyropdf.Page) -> list[tuple[str, pyropdf.Rect]]:
    return [(word.text, word.bbox) for word in page.words()]


def assert_same_box(before: pyropdf.Rect, after: pyropdf.Rect) -> None:
    for a, b in zip(before, after, strict=True):
        assert abs(a - b) <= 0.01


# Ported from pylopdf.


def test_replace_text_replaces_and_counts() -> None:
    doc = pyropdf.open(build_pdf(["Hello PDF"]))
    page = doc.pages[0]
    assert page.replace_text("PDF", "Cat") == 1
    text = page.text()
    assert "Hello Cat" in text
    assert "PDF" not in text


def test_replace_text_returns_zero_when_absent() -> None:
    doc = pyropdf.open(build_pdf(["Hello PDF"]))
    page = doc.pages[0]
    assert "Hello PDF" in page.text()
    before = doc.to_bytes()

    assert page.replace_text("XYZ", "abc") == 0
    assert doc.to_bytes() == before
    assert "Hello PDF" in page.text()


def test_replace_text_requires_needle() -> None:
    doc = pyropdf.open(build_pdf(["Hello PDF"]))
    with pytest.raises(ValueError, match="old"):
        doc.pages[0].replace_text("", "abc")


def test_replace_text_detaches_shared_page_contents() -> None:
    doc = pyropdf.open(build_pdf(["Hello PDF"]))
    doc.pages.extend(doc.pages[0])

    assert doc.pages[0].replace_text("PDF", "Cat") == 1
    assert "Hello Cat" in doc.pages[0].text()
    assert "Hello PDF" in doc.pages[1].text()


@pytest.mark.parametrize(("argument", "value"), [("old", 1), ("new", 1), ("count", "1")])
def test_replace_text_rejects_non_string_inputs(argument: str, value: object) -> None:
    doc = pyropdf.open(build_pdf(["Hello PDF"]))
    kwargs: dict[str, object] = {"old": "PDF", "new": "Cat", argument: value}
    with pytest.raises(TypeError, match=argument):
        doc.pages[0].replace_text(**kwargs)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_replace_text_validates_count(count: object) -> None:
    doc = pyropdf.open(build_pdf(["Hello PDF"]))
    with pytest.raises((TypeError, ValueError), match="count"):
        doc.pages[0].replace_text("PDF", "Cat", count=count)  # ty: ignore[invalid-argument-type]


def test_replace_text_refuses_a_character_without_a_code() -> None:
    doc = pyropdf.open(build_pdf(["Hello PDF"]))
    before = doc.to_bytes()
    steps = doc.history.steps

    # Helvetica's standard encoding has no code for a precomposed é or a crab.
    for new in ("Café", "🦀"):
        with pytest.raises(ValueError, match="no code for"):
            doc.pages[0].replace_text("PDF", new)
    assert doc.to_bytes() == before
    assert doc.history.steps == steps
    assert "Hello PDF" in doc.pages[0].text()


def test_replace_text_skips_an_operation_without_a_font() -> None:
    content = "BT (Hello) Tj ET"
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>",
                3: "<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>",
                4: stream(content),
            }
        )
    )
    before = doc.to_bytes()

    assert doc.pages[0].replace_text("Hello", "Goodbye") == 0
    assert doc.to_bytes() == before


# Positions.


@pytest.mark.parametrize(("old", "new"), [("PDF", "Cat"), ("PDF", "PDF"), ("PDF", "WWWWW"), ("Hello", "")])
def test_replace_text_keeps_the_following_words_in_place(old: str, new: str) -> None:
    doc = pyropdf.open(build_pdf(["Hello PDF world again"]))
    page = doc.pages[0]
    before = dict(boxes(page))

    assert page.replace_text(old, new) == 1
    after = dict(boxes(page))
    for word in ("world", "again"):
        assert_same_box(before[word], after[word])
    assert page.text().split() == " ".join(["Hello PDF world again".replace(old, new)]).split()


def test_replace_text_with_equal_widths_keeps_every_box() -> None:
    # In Helvetica, a, b, d, e, and o all advance 556 units.
    doc = pyropdf.open(build_pdf(["bad def"]))
    page = doc.pages[0]
    before = boxes(page)

    assert page.replace_text("bad", "ode") == 1
    after = boxes(page)
    assert [text for text, _ in after] == ["ode", "def"]
    for (_, a), (_, b) in zip(before, after, strict=True):
        assert_same_box(a, b)


def test_replace_text_keeps_positions_with_spacing_and_kerning() -> None:
    content = "BT /F1 18 Tf 2 Tc 5 Tw 72 720 Td [(Hel) -120 (lo P) 30 (DF) -250 (next)] TJ ET"
    doc = pyropdf.open(content_pdf(content))
    page = doc.pages[0]
    before = dict(boxes(page))

    assert page.replace_text("lo PD", "p me") == 1
    text = page.text()
    assert "p me" in text
    assert "PD" not in text
    assert_same_box(before["next"], dict(boxes(page))["next"])


def test_replace_text_keeps_positions_of_quote_operators() -> None:
    content = "BT /F1 12 Tf 14 TL 72 720 Td (first) Tj (Hello PDF tail) ' 3 1 (more PDF end) \" ET"
    doc = pyropdf.open(content_pdf(content))
    page = doc.pages[0]
    before = boxes(page)

    assert page.replace_text("PDF", "X") == 2
    after = boxes(page)
    assert [text for text, _ in after] == ["first", "Hello", "X", "tail", "more", "X", "end"]
    for index in (0, 1, 3, 4, 6):
        assert_same_box(before[index][1], after[index][1])


# Encodings, counts, and Forms.


def test_replace_text_encodes_through_the_differences() -> None:
    font = (
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        "/Encoding << /BaseEncoding /WinAnsiEncoding /Differences [1 /eacute /Euro] >> >>"
    )
    doc = pyropdf.open(content_pdf("BT /F1 24 Tf 72 720 Td (Cafe 5) Tj ET", font=font))
    page = doc.pages[0]

    assert page.replace_text("Cafe 5", "Café 5€") == 1
    assert page.text().strip() == "Café 5€"
    # The lowest code that shows each character: 1 for é, 2 for the euro.
    shown = next(operation for operation in page.operations if operation.operator == "TJ")
    assert bytes(shown.operands[0][0]) == b"Caf\x01 5\x02"


def test_replace_text_bounds_with_count() -> None:
    doc = pyropdf.open(content_pdf("BT /F1 12 Tf 72 720 Td (a PDF b PDF) Tj 0 -20 Td (PDF c) Tj ET"))
    page = doc.pages[0]

    assert page.replace_text("PDF", "X", count=2) == 2
    assert page.text().split() == ["a", "X", "b", "X", "PDF", "c"]
    assert page.replace_text("PDF", "X") == 1


def test_replace_text_is_one_undo_step() -> None:
    doc = pyropdf.open(build_pdf(["Hello PDF"]))
    before = doc.to_bytes()
    steps = len(doc.history.steps)

    assert doc.pages[0].replace_text("PDF", "Cat") == 1
    assert len(doc.history.steps) == steps + 1
    doc.history.undo()
    assert doc.to_bytes() == before


def test_replace_text_copies_a_shared_form() -> None:
    form = "BT /F1 20 Tf 0 0 Td (Form PDF here) Tj ET"
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [4 0 R 6 0 R] /Count 2 /MediaBox [0 0 612 792] "
                "/Resources << /Font << /F1 3 0 R >> /XObject << /X1 8 0 R >> >> >>",
                3: HELVETICA,
                4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
                5: stream("q 1 0 0 1 72 700 cm /X1 Do Q"),
                6: "<< /Type /Page /Parent 2 0 R /Contents 7 0 R >>",
                7: stream("q 1 0 0 1 72 700 cm /X1 Do Q"),
                8: stream(
                    form, "/Type /XObject /Subtype /Form /BBox [0 -10 400 30] /Resources << /Font << /F1 3 0 R >> >> "
                ),
            }
        )
    )
    before = dict(boxes(doc.pages[0]))

    assert doc.pages[0].replace_text("PDF", "Cat") == 1
    assert doc.pages[0].text().strip() == "Form Cat here"
    assert doc.pages[1].text().strip() == "Form PDF here"
    assert_same_box(before["here"], dict(boxes(doc.pages[0]))["here"])
    assert "PDF" in doc.objects[8].read().decode()
    reloaded = pyropdf.open(doc.to_bytes())
    assert reloaded.pages[0].text().strip() == "Form Cat here"
    assert reloaded.pages[1].text().strip() == "Form PDF here"


def test_replace_text_copies_nested_forms() -> None:
    inner = "BT /F1 20 Tf 0 0 Td (Inner PDF) Tj ET"
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 612 792] "
                "/Resources << /Font << /F1 3 0 R >> /XObject << /Outer 6 0 R >> >> >>",
                3: HELVETICA,
                4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
                5: stream("q 1 0 0 1 72 700 cm /Outer Do Q"),
                6: stream(
                    "/Inner Do",
                    "/Type /XObject /Subtype /Form /BBox [0 -10 400 30] /Resources << /XObject << /Inner 7 0 R >> >> ",
                ),
                7: stream(
                    inner, "/Type /XObject /Subtype /Form /BBox [0 -10 400 30] /Resources << /Font << /F1 3 0 R >> >> "
                ),
            }
        )
    )

    assert doc.pages[0].replace_text("PDF", "Cat") == 1
    assert doc.pages[0].text().strip() == "Inner Cat"
    assert "PDF" in doc.objects[7].read().decode()
    assert "/Inner Do" in doc.objects[6].read().decode()


def test_replace_text_refuses_a_composite_font() -> None:
    text = "".join(f"\\{byte:03o}" for byte in "日本語".encode("cp932"))
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 612 792] "
                "/Resources << /Font << /F1 3 0 R /F2 8 0 R >> >> >>",
                3: "<< /Type /Font /Subtype /Type0 /BaseFont /MS-Mincho /Encoding /90ms-RKSJ-H "
                "/DescendantFonts [6 0 R] >>",
                4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
                5: stream(f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET BT /F2 24 Tf 72 680 Td (Hello) Tj ET"),
                6: "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /MS-Mincho"
                " /CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 6 >>"
                " /FontDescriptor 7 0 R /DW 1000 >>",
                7: "<< /Type /FontDescriptor /FontName /MS-Mincho /Flags 6 /FontBBox [0 -137 1000 859]"
                " /ItalicAngle 0 /Ascent 859 /Descent -140 /CapHeight 769 /StemV 78 >>",
                8: HELVETICA,
            }
        )
    )
    page = doc.pages[0]
    assert "日本語" in page.text()
    before = doc.to_bytes()

    with pytest.raises(ValueError, match="composite font"):
        page.replace_text("本", "x")
    assert doc.to_bytes() == before
    # Text that only simple fonts draw is still replaced.
    assert page.replace_text("Hello", "Howdy") == 1
    assert "日本語" in page.text()
    assert "Howdy" in page.text()
