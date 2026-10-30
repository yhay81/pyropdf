# Text and behavior comparison against main

Compared 2026-10-30: PyMuPDF 2.0 (MuPDF 1.28.5, Python 3.13.16) and
pyropdf main `d06091e27d931fb8f766d2548675a8fdfbcb2677` (package version
0.2.0, Python 3.15.0), in separate environments, on macOS arm64. The main
commit includes #418's page-font API. The [machine-readable results](text-main.json)
include the SHA-256 of each input. Run the commands in `compare_text.py` from
the repository root with each engine's Python. No corpus text is stored in
the results; temporary worker files contain only hashes and counts.

Every one of the 15 documents opened in both engines; both enumerated 156
pages and no text/word extraction call raised. Extraction warnings were
suppressed, so this does not mean that no glyphs were skipped.

| Per-page comparison | Pages equal | Pages different |
| --- | ---: | ---: |
| Default text, exact bytes | 7 | 149 |
| Default text, Unicode NFKC and whitespace normalized | 128 | 28 |
| Normalized word texts, compared as a multiset | 135 | 21 |

Six pages yield empty text in both engines and count as equal in every row.
These are default outputs, not a common formal extraction contract: pyropdf
uses its own reading-order and word-segmentation heuristics, while the
PyMuPDF calls are `get_text()` and `get_text("words")` without `sort`.
Differences can reflect line breaks, layout, segmentation, mapping or actual
extraction errors. Neither matching outputs nor disagreement establishes
correctness; boxes, ordering against ground truth, tables, OCR accuracy and
rendering fidelity were not measured. #430 tracks a labeled comparison.

Examples:

- On the first page of `mhlw-doc.pdf`, word boundaries around Japanese text
  and numbers differ between engines; this is not by itself a correctness
  verdict.
- On the first page of `usrguide.pdf`, PyMuPDF returns `LATEX` as one word;
  pyropdf returns `LAT`, `E`, and `X` as separate words.
- `pdfium-type3.pdf` has a Type 3 font with no `/ToUnicode`, with character
  code 65 mapped to the custom name `/C1`. PyMuPDF returns three `A` words;
  pyropdf skips the three unmapped glyphs with a warning by default and
  returns replacements with `unmapped="glyph_id"`. The original code does
  not prove the intended Unicode text, so PyMuPDF's fallback is not treated
  as ground truth. #437 tracks access to the code or an explicit fallback.

## A demonstrated API semantic difference

`gray_probe.py` creates a PDF with a red vector rectangle and no images,
then calls `rewrite_images(set_to_gray=True)` in each engine and samples an
interior pixel at 72 dpi. The starting pixel is RGB `(255, 0, 0)` in both.
PyMuPDF returns `(129, 129, 129)` after the call; pyropdf returns
`(255, 0, 0)` and reports zero rewritten images. This follows the
[PyMuPDF 2.0 documentation](https://pymupdf.readthedocs.io/en/latest/document.html#Document.rewrite_images):
PyMuPDF recolors page text and vectors as well as images, whereas pyropdf's
same-named argument affects eligible images only. #400 and #432 track the
gap. This single synthetic case does not establish behavior on other inputs.

A separate wording correction: PyMuPDF's
[`clip_to_rect`](https://pymupdf.readthedocs.io/en/latest/page.html#Page.clip_to_rect)
keeps paths and images that intersect the rectangle *in their entirety*,
even where they extend outside it. #420 seeks stronger removal and must not
be read as a description of PyMuPDF's guarantee.
