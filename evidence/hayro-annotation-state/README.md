# Annotation appearances after a stray `Q` (issue #448)

hayro draws an annotation's appearance stream in the graphics state that the
page content leaves behind when that content has a `Q` with no matching `q`.
Ghostscript draws it from the initial state, as it does when the `Q` is absent.

The first report of this issue named an unclosed `q` as the trigger. The
reduction below shows that an unclosed `q` alone does not change hayro's
output; the stray `Q` does.

## Files

- `make_pdf.py` writes `stray-q.pdf` and `balanced.pdf`. The page content is
  `Q 1 0 0 rg` and `1 0 0 rg`. One square annotation's appearance fills its box
  green inside `q ... Q` and then draws "Note" with no color of its own.
- `src/main.rs` renders page 1 with hayro 0.8.0 alone (no pyropdf) and counts
  the red, green, blue, and black pixels of the annotation's box.
- `compare.py` runs it and Ghostscript over both files.

```bash
python3 evidence/hayro-annotation-state/make_pdf.py
python3 evidence/hayro-annotation-state/compare.py
```

## Result

Pixels of the annotation's box at 72 dpi (hayro 0.8.0, Ghostscript 10 on
macOS). Antialiasing makes the totals differ between the renderers; the colors
do not.

| File | hayro | Ghostscript |
| --- | --- | --- |
| `stray-q.pdf` | 67 red, 660 green, 0 black | 706 green, 94 black |
| `balanced.pdf` | 660 green, 69 black | 706 green, 94 black |

hayro paints the text red, the page's leftover fill, only when the stray `Q`
is present. A page that ends with an open `q` and sets its colors inside it, or
that sets a color at depth 0 and then opens a `q`, did not change the output.

## Reduction

The first reproduction was the fixture of
`tests/test_clean_content.py::test_appearances_after_an_open_q_draw_the_same_in_ghostscript`.
Removing lines of its page content until the output stopped differing left two:
`Q` and `/CS1 cs 1 sc 70 10 20 20 re f`. The files here use a plain `rg` instead
of the Indexed color space, which gives the same result.
