# Full-save redaction controls

This fixture exercises the three observable requirements in #362 on one PDF
page. It has a visible `SECRET` inside a `TJ` array, invisible OCR text
`HUSH`, and an 8 × 4 RGB image whose 32 source pixels have distinct colours.
The test removes both target words and clears a 4 × 2 region of the image.
`LEFT` and `RIGHT` give the text readers two positions to compare across the edit.

PyMuPDF produces the independently redacted full save. pyropdf, PyMuPDF and
Poppler each read the original and the result. The checker compares all four
bounding-box coordinates for both retained words within each reader, with a
0.001-point tolerance. It compares the rendered text area with the reference,
reads the 96 decoded RGB bytes directly with pikepdf and samples
the pixel centres of an independent 72 dpi Poppler render.

| File | Extracted words in all three readers | Largest outside-word shift | Correct stored pixels, covered / outside | Correct painted samples |
| --- | --- | ---: | ---: | ---: |
| Reference and linearized full saves | LEFT, RIGHT | 0.000007 pt | 8 / 24 | 32 / 32 |
| White rectangles over source | LEFT, SECRET, RIGHT, HUSH | 0 pt | 0 / 24 | 32 / 32 |
| Suffix shifted right | LEFT, RIGHT | 3 pt | 8 / 24 | 32 / 32 |
| Only the image covered by a rectangle | LEFT, RIGHT | 0.000007 pt | 0 / 24 | 32 / 32 |
| Entire edited image covered | LEFT, RIGHT | 0.000007 pt | 8 / 24 | 8 / 32 |
| Kept word covered | LEFT, RIGHT | 0.000007 pt | 8 / 24 | 32 / 32 |
| Incremental metadata edit of the reference | LEFT, RIGHT | 0.000007 pt | 8 / 24 | 32 / 32 |

The reference and the image-only overlay produced identical complete Poppler
rasters. Inspecting the saved image distinguishes them. Covering an outside
word changes the painted text even though all three readers still extract it.
The metadata-only incremental control retains two revisions and fails the
full-save check. The linearized full save has one revision and passes.

The checker expects
this fixture's single 8 × 4 image in DeviceRGB or three-channel ICCBased RGB,
with its default sample decoding and no mask. It reports a different layout for
inspection. The production redaction API and other fonts, paths, forms and
annotations remain tracked in #362.

## Reproduce

Install Poppler's `pdftotext` and `pdftoppm`. The following command writes the
PDFs and per-reader measurements to a fresh subdirectory of the scratch path.
The measurements above used pyropdf 0.3.1, PyMuPDF 1.28.2, pikepdf 10.18.2
and Poppler 26.11.0.

```sh
uv run --locked --group oracles python evidence/redaction/verify.py /tmp/redaction-check
```

To check another implementation, use the generated `original.pdf`, apply the
same three rectangles, use white for the eight covered image samples and
write a full save. The rectangles in `REGIONS` use a top-left origin measured
in points. The checker writes `results.json` and returns a nonzero status
when the candidate fails a check.

```sh
uv run --locked --group oracles python evidence/redaction/verify.py /tmp/redaction-check --candidate /path/to/full-save.pdf
```
