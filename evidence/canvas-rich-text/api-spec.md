# Draft Python API Specification: Styled Rich Text Runs & BiDi Layout on `Canvas` (#1198)

Status: **Draft for owner review** (follows [DESIGN.md §10](../../DESIGN.md#10-python-api)).

## 1. Design alignment with DESIGN.md §10

- **One way**: No new `Canvas.rich_text()` or `Canvas.rich_textbox()` aliases. Existing
  `Canvas.text()` and `Canvas.textbox()` accept `text: str | Sequence[TextSpan]`.
  Single-string calls keep their exact current behavior and defaults.
- **Specification type**: `TextSpan` is a frozen dataclass (`frozen=True, slots=True, kw_only=True`)
  validated on construction (finite positive `size`, valid paint, valid link target type).
- **No synthetic bold/italic**: Consistent with `Font.from_file(..., weight=...)` (which refuses
  weights outside the face's variable axis and never imitates bold or oblique), `TextSpan` takes
  an explicit `font: Font | None` (where `None` inherits the call's `font`, defaulting to
  Helvetica). Callers select bold/italic faces explicitly with `Font.standard("Helvetica-Bold")`,
  `Font.from_file(..., weight=700)`, or an italic face.
- **Reuses existing types**: Paints use `Color | Gradient | PatternPaint | None` and
  `Stroke | Color | Gradient | PatternPaint | None`; hyperlink targets reuse `Target` from
  `pyropdf._targets` (the exact type accepted by `page.links.add(rect, target)`: URI string,
  `Destination`, `RemoteDestination`, `Launch`, `NamedAction`, etc.).
- **Display coordinates & atomic transaction**: All coordinates, decoration strokes, and link
  annotation rectangles live in the page's display space (top-left origin, y downward, honoring
  `rotate`). Any `/Link` annotations produced by `TextSpan.link` are added to the page's
  `/Annots` array inside the **same `Canvas` transaction and undo step** on clean `__exit__`,
  and discarded if the box does not fit (`TextFit.fits is False`) or an exception aborts the
  canvas.
- **No global font registry (§10.1 Concurrency)**: Output depends solely on the document, the
  call arguments, and the `Font` instances passed in. No OS font database is queried.
- **Python 3.11+**: `TypeAlias` assignments (no PEP 695 `type` statements), `slots=True`,
  `kw_only=True`.

## 2. Public types

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

TextAlign: TypeAlias = Literal["left", "center", "right", "justify", "start", "end"]
TextDirection: TypeAlias = Literal["auto", "ltr", "rtl"]


@dataclass(frozen=True, slots=True, kw_only=True)
class TextSpan:
    """One inline styled run inside :meth:`Canvas.text` or :meth:`Canvas.textbox`.

    Fields set to ``None`` inherit the enclosing call's defaults (``font``,
    ``size``, ``fill``, ``stroke``). Explicit ``fill=None`` is distinguished
    from inheriting the call's fill only when ``stroke`` is set; to stroke a
    span without filling it, pass ``fill=Color.TRANSPARENT`` or set
    ``fill=None, stroke=...`` on the enclosing call.
    """

    text: str
    font: Font | None = None
    size: float | None = None
    fill: Color | Gradient | PatternPaint | None = None
    stroke: Stroke | Color | Gradient | PatternPaint | None = None
    underline: bool = False
    strikethrough: bool = False
    link: Target | None = None
```

### Field notes & validation

1. **`text: str`** — Validated as `str`; all spans' UTF-8 bytes sum against `max_text_size`
   (default 1 MiB, `LimitError` code `"text_input_size"`). The span count is bounded by
   `max_spans` or `max_text_size` (at most 65,536 spans by default, `LimitError` code
   `"content_element_count"`).
2. **`font: Font | None`** — Inherits the call's `font` (or `Font.standard("Helvetica")` if both
   are `None`). Standard 14 and embedded OpenType `Font` spans may be mixed freely in one
   paragraph or line. A Standard 14 span still refuses non-WinAnsi characters with `ValueError`.
3. **`size: float | None`** — Must be finite and `> 0` when set (`ValueError` otherwise).
   Mixed font sizes on one line share a single baseline; line ascent is `max(face.ascender * size)`
   and line descent is `max(-face.descender * size)` across spans on that line.
4. **`underline: bool` / `strikethrough: bool`** — Drawn as vector paths in the span's paint
   (fill color if set, else stroke paint) positioned from the OpenType `post` table
   (`underlinePosition`, `underlineThickness`) and `OS/2` table (`yStrikeoutPosition`,
   `yStrikeoutSize`), with standard fallbacks (`-0.1 em` / `0.05 em` and `0.3 em` / `0.05 em`)
   for Standard 14 fonts. Drawn so `Page.layout(styles=True)` detects `Span.is_underlined` and
   `Span.is_struck` on round-trip.
5. **`link: Target | None`** — When non-`None`, each visual run fragment produced by this span
   (including fragments split across a line wrap or BiDi reordering) emits a `/Link` annotation
   covering its display-space bounding box (`[x0, baseline - ascent, x1, baseline + descent]`,
   transformed by `rotate` and any active `canvas.transform(...)`), using the same validation
   and target encoding as `page.links.add(rect, target)`.
6. **Sentinel vs explicit `fill=None` (open ergonomic detail for owner confirmation)**:
   If the enclosing `canvas.textbox(..., fill=Color.BLACK)` has a default fill and one span
   wants stroke-only hollow text, either (a) use an explicit `_UNSET` sentinel default on
   `TextSpan.fill` so `TextSpan(text="X", fill=None, stroke=Color.RED)` explicitly clears fill,
   or (b) keep `fill: Color | Gradient | PatternPaint | None = None` as "inherit" and use
   `Color(0, 0, 0, alpha=0)` / call-level `fill=None`. Recommendation: keep the dataclass
   signature simple (`None` = inherit) unless stroke-only inline spans inside filled paragraphs
   are a priority, in which case use a `Literal["inherit"]` default (no opaque private sentinel
   in typing/repr).

## 3. Changes to `Canvas.text()` and `Canvas.textbox()`

```python
class Canvas:
    def text(
        self,
        text: str | Sequence[TextSpan],
        at: Sequence[float],
        *,
        font: Font | None = None,
        size: float = 11,
        fill: Color | Gradient | PatternPaint | None = _BLACK,
        stroke: Stroke | Color | Gradient | PatternPaint | None = None,
        line_height: float | None = None,
        direction: TextDirection = "auto",
        rotate: int = 0,
        max_text_size: int | None = _DEFAULT_MAX_GENERATED_TEXT_SIZE,
    ) -> None: ...

    def textbox(
        self,
        rect: Sequence[float],
        text: str | Sequence[TextSpan],
        *,
        font: Font | None = None,
        size: float = 11,
        align: TextAlign = "left",
        direction: TextDirection = "auto",
        line_height: float | None = None,
        fill: Color | Gradient | PatternPaint | None = _BLACK,
        stroke: Stroke | Color | Gradient | PatternPaint | None = None,
        rotate: int = 0,
        max_text_size: int | None = _DEFAULT_MAX_GENERATED_TEXT_SIZE,
    ) -> TextFit: ...
```

### Alignment & BiDi semantics

- **`direction="auto"` (default)**: Each `\n`-delimited paragraph resolves its base direction
  via UAX #9 Rules P2/P3 (first strong directional character; defaults to LTR if none).
  - Existing LTR `str` calls behave identically to today.
  - Hebrew/Arabic paragraphs automatically resolve to RTL base direction.
- **`direction="ltr"` / `"rtl"`**: Forces the paragraph embedding level (0 or 1) for every
  paragraph in the call (e.g. an RTL Arabic invoice line starting with an invoice number
  `"2026-001 فاتورة"` stays RTL).
- **`align="start"` / `"end"`**: Direction-aware alignment — `"start"` aligns left for LTR
  paragraphs and right for RTL paragraphs; `"end"` is the opposite. `"left"` and `"right"`
  remain physical left/right (consistent with existing `textbox` behavior).
- **`align="justify"`**: Stretches inter-word spaces on soft-wrapped lines to fill the box
  width. The last line of each paragraph (and hard-broken lines) aligns to the paragraph's
  `"start"` edge (left for LTR, right for RTL).
- **Line-breaking vs BiDi order (UAX #9 L1/L2)**: Line breaking (UAX #14, with UAX #29
  emergency grapheme breaks) runs in **logical order** first; UAX #9 visual reordering
  (`L1` trailing whitespace reset + `L2` level reversal) runs **per wrapped line** afterward.
- **Measurement helper**: Extend `Font.text_height(...)`? No — mixed-font spans do not belong
  to a single `Font`. Instead, callers who need pre-flight height measurement can inspect
  `TextFit.spare_height` from `canvas.textbox(...)` (which draws nothing when `not fit.fits`),
  or a follow-up `Canvas.measure_text(...)` / `measure_rich_text(...)` can be added if needed.
  Following DESIGN.md §10 ("One way"), do not add speculative measurement helpers before a
  concrete use case requires one.

## 4. Usage example

```python
import pyropdf
from pyropdf import Color, Font, TextSpan

doc = pyropdf.Document()
page = doc.pages.add()
regular = Font.from_file("NotoSans-Regular.ttf")
bold = Font.from_file("NotoSans-Bold.ttf")
arabic = Font.from_file("NotoSansArabic-Regular.ttf")

with page.draw() as canvas:
    fit = canvas.textbox(
        (50, 50, 545, 220),
        [
            TextSpan(text="Invoice #2026-042 — ", font=bold, size=14),
            TextSpan(text="فاتورة ضريبية ", font=arabic, size=14),
            TextSpan(
                text="View online",
                fill=Color(0.05, 0.32, 0.78),
                underline=True,
                link="https://example.com/invoices/2026-042",
            ),
        ],
        font=regular,
        size=11,
        align="start",
        direction="auto",
    )
    assert fit.fits
```
