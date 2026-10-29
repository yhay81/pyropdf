# Real-document corpus

These PDFs are pylopdf's
[tests/assets/real_world](https://github.com/yhay81/pylopdf/tree/884af0dbf21973cfd434a9eca4e769de62fcc25c/tests/assets/real_world)
at commit `884af0dbf21973cfd434a9eca4e769de62fcc25c`, byte for byte:
documents produced by real toolchains, plus minimal fixtures from independent
PDF implementations. The table below keeps that commit's record of each
file's source and license, which
[DESIGN.md section 12](../../../DESIGN.md#12-verification) brings over with
the corpus. Every license allows redistribution, and
[test_corpus_records.py](../../test_corpus_records.py) fails when a file has
no record. pylopdf's README also describes the lopdf limitations that these
files exposed, and pylopdf's other fixtures; those notes stay there.

[corpus.py](../../corpus.py) derives damaged inputs from these files with the
damage generators of pylopdf's `tests/test_real_world.py` at the same commit:
15 files under 9 kinds of damage. [damage.sha256](damage.sha256) records the
SHA-256 of each input, as `shasum -a 256 <kind>/<file>` prints it, and the
records test checks that the helper reproduces every one. pylopdf's generators
wrote the inputs of the first 14 files; the helper wrote those of
`pdfium-ocg-invisible.pdf`, which pyropdf added.

## Files

| File | Source | License | Coverage |
|---|---|---|---|
| `f1040.pdf` | [irs.gov](https://www.irs.gov/pub/irs-pdf/f1040.pdf) | US government work, public domain | PDF 1.7, AcroForm, tagged PDF, object streams, Adobe Designer output |
| `pdf20-simple.pdf` | [pdf-association/pdf20examples](https://github.com/pdf-association/pdf20examples), “Simple PDF 2.0 file.pdf” | CC BY 4.0 | PDF 2.0 header, minimal uncompressed structure, Type 1 font without `/Encoding` |
| `usrguide.pdf` | [latex-project.org](https://www.latex-project.org/help/documentation/usrguide.pdf) | LPPL, freely redistributable | PDF 1.5, pdfTeX output, subset Type 1 fonts, formulas and ligatures |
| `bill-hr815.pdf` | [govinfo.gov](https://www.govinfo.gov/content/pkg/BILLS-118hr815enr/pdf/BILLS-118hr815enr.pdf), H.R. 815, 118th Congress | US government work, public domain | PDF 1.5, GPO typesetting, medium-size 110-page document |
| `mhlw-doc.pdf` | [mhlw.go.jp](https://www.mhlw.go.jp/content/11201250/001526113.pdf), Study Group on “Workers” under the Labor Standards Act, material 2-1 | [Government Standard Terms of Use 2.0](https://www.digital.go.jp/resources/open_data/), CC BY 4.0 compatible | PDF 1.7, embedded CJK CID fonts, mixed vertical/horizontal layout |
| `bunka-kokugo-series-019-p4.pdf` | [Agency for Cultural Affairs](https://www.bunka.go.jp/kokugo_nihongo/sisaku/joho/joho/series/19/19.html), *Kokugo Series No. 19: Questions and Answers on Japanese Language, Vol. 2*, source PDF page 4 | [Government Standard Terms of Use 2.0](https://www.digital.go.jp/resources/open_data/), CC BY 4.0 compatible | Pixel-identical, single-page derivative of a 2007 image-only scan; 3311×4681 Japanese body text, historical type, dust, punctuation, and ruby |
| `patent-us223898.pdf` | [Google Patents](https://patents.google.com/patent/US223898A), Edison's 1880 light-bulb patent | Public-domain US patent | PDF 1.3, scanned CCITTFaxDecode image, OCR text layer; retrieved 2026-07-22 |
| `wdl6812-manuscript.pdf` | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Illuminated_Panel_and_Qur%27anic_Chapter_WDL6812.pdf), illuminated World Digital Library manuscript | Public domain | PDF 1.4, color scan using DCTDecode and JBIG2Decode, no text layer; retrieved 2026-07-22 |
| `nics-background-checks-2015-11.pdf` | FBI NICS monthly report, [pdfplumber test-corpus mirror](https://github.com/jsvine/pdfplumber/blob/stable/tests/pdfs/nics-background-checks-2015-11.pdf) | US government work, public domain; mirror repository is MIT licensed | PDF 1.3, Quartz output, dense 25-column records with internal row rules omitted; retrieved 2026-07-25 |
| `senate-expenditures.pdf` | US Senate expenditure-report excerpt, [pdfplumber test-corpus mirror](https://github.com/jsvine/pdfplumber/blob/stable/tests/pdfs/senate-expenditures.pdf) | US government work, public domain; mirror repository is MIT licensed | PDF 1.3, iText/pdftk output, 90-degree page rotation, merged bordered header, and borderless data rows; retrieved 2026-07-25 |
| `pdfium-type3.pdf` | [PDFium `type3.in`](https://github.com/chromium/pdfium/blob/a84323421e94f484faca52dd9d027934eba42ab8/testing/resources/pixel/type3.in) | PDFium BSD 3-Clause; see [LICENSES/PDFium-BSD-3-Clause.txt](LICENSES/PDFium-BSD-3-Clause.txt) | Type 3 stencil glyph program under three text transforms |
| `pdfium-smask-blend.pdf` | [PDFium `smask_blend.in`](https://github.com/chromium/pdfium/blob/a84323421e94f484faca52dd9d027934eba42ab8/testing/resources/pixel/smask_blend.in) | PDFium BSD 3-Clause; see [LICENSES/PDFium-BSD-3-Clause.txt](LICENSES/PDFium-BSD-3-Clause.txt) | Form XObjects, an alpha soft mask, partial opacity, and Screen blending; the forms spell `/Type XObject` without the name's slash |
| `pdfium-jpx-lzw.pdf` | [PDFium `jpx_lzw.pdf`](https://github.com/chromium/pdfium/blob/a84323421e94f484faca52dd9d027934eba42ab8/testing/resources/jpx_lzw.pdf) | PDFium BSD 3-Clause; see [LICENSES/PDFium-BSD-3-Clause.txt](LICENSES/PDFium-BSD-3-Clause.txt) | Full-page red image with an `ASCIIHexDecode` → `LZWDecode` → `JPXDecode` filter chain |
| `pdfium-links-highlights-annots.pdf` | [PDFium `links_highlights_annots.pdf`](https://github.com/chromium/pdfium/blob/a84323421e94f484faca52dd9d027934eba42ab8/testing/resources/links_highlights_annots.pdf) | PDFium BSD 3-Clause; see [LICENSES/PDFium-BSD-3-Clause.txt](LICENSES/PDFium-BSD-3-Clause.txt) | Existing Widget, URI Link, and Highlight annotations from an independent producer |
| `pdfium-ocg-invisible.pdf` | [PDFium `bug_491_invisible.in`](https://github.com/chromium/pdfium/blob/a84323421e94f484faca52dd9d027934eba42ab8/testing/resources/pixel/bug_491_invisible.in) | PDFium BSD 3-Clause; see [LICENSES/PDFium-BSD-3-Clause.txt](LICENSES/PDFium-BSD-3-Clause.txt) | Optional content: one group, `/BaseState /OFF`, and marked content under a membership dictionary; retrieved 2026-10-29 |

## Provenance

The PDFium fixtures are pinned to revision
`a84323421e94f484faca52dd9d027934eba42ab8`; the first four were retrieved on
2026-07-26 and `pdfium-ocg-invisible.pdf` on 2026-10-29. The three `.in`
templates were converted with PDFium's
[`fixup_pdf_template.py`](https://github.com/chromium/pdfium/blob/a84323421e94f484faca52dd9d027934eba42ab8/testing/tools/fixup_pdf_template.py).
The resulting fixture SHA-256 values are:

- `pdfium-type3.pdf`:
  `dc213b3d2952f06517ac487d9892b11628f4b5bedc2ad8278a4c05c1b40be8f6`
- `pdfium-smask-blend.pdf`:
  `9947d9c15d853823cedc1e87378dea89a665570e4f233b7a0ba215e5859134ab`
- `pdfium-jpx-lzw.pdf`:
  `ba14897a4139944e56674fefe176a56f29326e69ff2baf3e07ba096b520def78`
- `pdfium-links-highlights-annots.pdf`:
  `d6b813376c52b326a0eac798d215eb135054ba77fdaa2c059f99e57d6726ac9f`
- `pdfium-ocg-invisible.pdf`:
  `cd8a567bc9d7c595b6dd9545ab2885c0246744ce638179ed5c938d91352589f9`

`bunka-kokugo-series-019-p4.pdf` was prepared on 2026-07-25 from
`kokugo_series_019_02.pdf` (source SHA-256
`b5eb4fe45cb7db1dfb6ad25255ffc685a1e4409e6a0487983eed440db0f20e43`).
The first page's decoded image was re-embedded losslessly in a one-page PDF to
avoid retaining the source document's shared 22-page image resources. Its
scale-1 RGBA rendering is byte-identical to the source page (SHA-256
`3f1ca966b541d3c7963dee0de82af8d55734f2edad997eb2e746980731cddf25`);
the fixture SHA-256 is
`f8d9d89a8b738ac2fc3108d01d1974ca413d3fa8dd32d4f812f075e3337ab9a3`.

## Adding a file

Choose additions using three criteria: a redistributable license, a size below
1 MB, and coverage not already represented in the corpus. A new file needs a
record in the table above, and the SHA-256 of each of its damaged inputs in
`damage.sha256`.
