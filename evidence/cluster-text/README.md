# The extra glyphs of a multi-glyph cluster (issue #637)

What readers make of the ways to write a cluster that the shaper draws with
more glyphs than it has characters, such as the base and the dot of Arabic beh,
or the two parts of a Tamil vowel.

## Method

```bash
npm install --prefix PDFJS_DIR pdfjs-dist@4.10.38
uv run --with pypdfium2 --with pymupdf --with pdfminer.six --with pypdf python evidence/cluster-text/measure.py PDFJS_DIR evidence/cluster-text/results.json
```

`measure.py` draws ten samples at one size, in faces that Windows 11 installs
and in this repository's test faces. It writes each sample in each way, saves
it, and reads it back with seven readers. `results.json` holds every reading.
Measured on Windows 11 with Xpdf's `pdftotext` 4.06, PyMuPDF, pypdfium2, pdf.js
4.10.38, pypdf, and pdfminer.six. Poppler was not installed. It derives from
Xpdf, and its source reads `/ActualText` the same way, but this was not measured.

A reading is `exact` when it equals the sample after NFC and without direction
controls and line ends. The next grades differ from the sample by invisible
characters (`invisible`), then spaces (`space`), then the order of the same
characters (`order`). Anything else is `stray`.

## Results

Exact readings out of ten samples:

| Way | pyropdf | Xpdf | MuPDF | PDFium | pdf.js | pypdf | pdfminer |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Before this change | 6 | 4 | 1 | 0 | 1 | 1 | 0 |
| **This change** | **9** | **9** | **9** | **8** | **4** | **1** | **0** |
| This change, no CMap entry | 9 | 9 | 9 | 8 | 1 | 1 | 0 |
| This change, empty CMap entry `<gid> <>` | 2 | 9 | 9 | 8 | 1 | 6 | 3 |
| One span per cluster, in visual order | 9 | 8 | 8 | 8 | 4 | 1 | 0 |
| An empty span over each extra glyph | 6 | 0 | 6 | 0 | 4 | 1 | 0 |

Stray readings out of ten samples, which are what a person notices:

| Way | pyropdf | Xpdf | MuPDF | PDFium | pdf.js | pypdf | pdfminer |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Before this change | 0 | 0 | 9 | 9 | 9 | 9 | 9 |
| **This change** | **0** | **0** | **0** | **0** | **2** | **1** | **1** |
| This change, no CMap entry | 0 | 0 | 0 | 0 | 9 | 9 | 9 |
| This change, empty CMap entry `<gid> <>` | 6 | 0 | 0 | 0 | 9 | 1 | 1 |
| One span per cluster, in visual order | 0 | 0 | 1 | 0 | 2 | 1 | 1 |
| An empty span over each extra glyph | 1 | 1 | 1 | 1 | 2 | 1 | 1 |

`page.text()` warned of a skipped glyph on nine samples before this change, and
on none after it. The drawing does not change: `same_pixels` in `results.json`
is true for all ten samples, comparing hayro's render at 144 dpi with the writer
before this change.

## What decided each choice

- **One `/ActualText` over the whole cluster.** Xpdf, MuPDF, and PDFium then
  read the cluster exactly. An empty `/ActualText` over the extra glyph alone
  keeps the base's geometry, but PDFium ignores an empty one and reads the glyph.
- **A glyph with a width comes first.** In a right-to-left run the shaper puts a
  mark before its base. MuPDF takes the box of `/ActualText` from the first
  glyph, so a mark first gives a box without a width: it inserts a space, as in
  `مرح با`, and in Calibri reads a word twice. Moving the base first fixes both.
- **The last glyph ends at the cluster's end.** Xpdf takes the box from the
  first glyph's origin to the last one's end, so a mark last leaves a gap that
  reads as a word break. `Tc` carries the text position to the cluster's end. It
  applies only when the gap exceeds a twentieth of the em, because PDFium reads
  a space when an ordinary last glyph is split into its own `TJ`.
- **A glyph without text maps to U+2060.** Readers that honour `/ActualText` read
  the cluster without any entry. pdf.js, pypdf, and pdfminer ignore it and fall
  back to the CID as a character code, as the "no CMap entry" row shows. An empty
  entry `<gid> <>` reads cleanly in pypdf and pdfminer, but hayro then drops the
  whole CMap, so `page.text()` keeps only the text under `/ActualText`, and pdf.js
  still reads stray text in all nine samples with such a glyph. pdf.js skips U+2060
  as an invisible mark, and U+2060 does not break a word as U+200B does.
- **Real text replaces U+2060.** A base such as Bengali ক is an extra glyph of
  `কো` and a letter of its own. If U+2060 held, a reader that ignores
  `/ActualText` would lose the letter everywhere.
- **Marks in another canonical order need no `/ActualText`.** The shaper reorders
  Hebrew points, which read the same in NFD. A first run that compared without NFD
  drew spans over them, and MuPDF, pdf.js, and pypdf read the Hebrew sample worse;
  that run is not in `results.json`. With NFD it writes and reads as it did.

## What stays imperfect

- **pdf.js in a right-to-left run: `order`.** pdf.js ends its text chunk at every
  `BDC` and restores the direction per chunk, so a span inside a right-to-left
  word scrambles the word: `مرحبا عالم` reads `ا عالمبمرح`. Before this change it
  read `مرحب&ا عالم`, in order with a stray `&`. Left-to-right scripts read exactly.
- **pdf.js, pypdf, and pdfminer ignore `/ActualText`.** They read the CMap, which
  holds one text per glyph because CIDs are the face's glyph IDs (DESIGN.md
  section 7). So a glyph that one cluster maps to its whole text reads that text
  in another cluster: Bengali `বৌ` reads `কো`, the one `stray` left in pypdf and
  pdfminer.
- **Lao in pdf.js is scored `stray`,** but it only decomposes ຳ into its
  compatibility form ໍ + າ, which looks the same.
- **A ligature in a right-to-left run: `order`.** MuPDF and PDFium reverse several
  characters that one glyph stands for, with or without `/ActualText`. `في` in
  Calibri reads `يف`, before this change as well.
- **Myanmar in pyropdf: `order`.** Its text extraction places a zero-width mark
  after the next letter, before this change as well.

## Files

- `measure.py`: the writers and the measurement.
- `read_pdfjs.mjs`: reads a page with pdf.js.
- `results.json`: every reading and score.
