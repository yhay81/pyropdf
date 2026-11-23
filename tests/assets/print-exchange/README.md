# Single-file print exchange

The generated two-page job carries an embedded CMYK output-device profile, trim boxes, and synchronized XMP and Info identification. The PDF/VT-1 variant adds a job with two single-page recipients, CIP4 recipient identifiers and a backlink from each page to its leaf. The tests edit the title, print condition description and second recipient, then reopen full and incremental saves. They compare the first recipient, the embedded ICC data and both painted pages.

The file template uses a PDF 1.6 header. Both standards identify the flavour in the XMP metadata. ISO 15930-7:2010 6.4.2.1 specifies one PDF/X output intent with an embedded output-device profile for PDF/X-4. ISO 16612-2:2010 6.2.1 and 6.5 specify PDF/X-4 as the basis of PDF/VT-1 and define the document part hierarchy and each page's leaf reference. [ISO 15930-7](https://www.iso.org/standard/55843.html) · [ISO 16612-2](https://www.iso.org/standard/46428.html)

## Reproduce the independent checks

Use [Kura 1.4.1](https://github.com/alam00000/bentopdf-kura/releases/tag/v1.4.1) and verify its archive against the release's SHA256SUMS. Install the dependencies of [phpdftk at fa78d621](https://github.com/phpdftk/phpdftk/tree/fa78d621cae9663fbab1b89376e2ffdb75343944) with Composer in that checkout. The verification used PHP 8.5.8 and Poppler 26.11.0.

```sh
uv run pytest tests/test_print_exchange.py
uv run python tests/assets/print-exchange/validate.py \
  --kura /path/to/kura --php /path/to/php \
  --autoload /path/to/phpdftk/vendor/autoload.php
```

Both validators accepted the two fresh files and all four edited files. Poppler produced byte-identical RGB page rasters before and after every edit. The 22 malformed controls and eight checks of the underlying X-4 flavour had the following results. The PHP clause identifiers below are that validator's own result codes.

| Change | Kura X-4 or VT-1 | phpdftk |
| --- | --- | --- |
| Remove the entire intent or its embedded ICC stream | `OUTPUT_INTENT_ADDED` | `6.2.2` |
| Duplicate the PDF/X output intent | `OUTPUT_INTENTS_PRUNED` | Accepts |
| Remove the VT root | `VT_SINGLE_PART` for VT-1, accepts X-4 | `6.1` for VT-1, accepts X-4 |
| Use a flat child array, remove a page backlink, or point it at the other recipient | `VT_DPART_REBUILT`, `VT_SINGLE_PART` for VT-1, accepts X-4 | Accepts |
| Declare PDF/X-4p on a PDF/VT-1 file | `XMP_NONCONFORMING` | Accepts |

The structural report also identifies a non-text output condition identifier and a forbidden external profile reference alongside the embedded profile. #1050 tracks ICC device-class verification and arbitrary-depth document-part membership. This matrix covers the single-file PDF/X-4 and PDF/VT-1 flavours.

## Independent producer inputs

The source PDFs are available from their publishers. Place the three original PDFs in one directory and add `--sources /path/to/samples` to the command above to verify their hashes, read them, edit their actual job records or output intent, and check all six reopened saves. Each original has an indirect output-intent array. The public profile reader resolves it. phpdftk flags its own clause `6.2.2` on the originals and accepts both PDFlib samples when pikepdf stores that same array directly. Kura reports additional findings in the originals, including font and date handling. Their identification and part structure serve as independent reading controls.

| Source | SHA-256 | Observed evidence |
| --- | --- | --- |
| [PDFlib PDF/X-4 starter](https://www.pdflib.com/fileadmin/cookbooks/PDFlib/results/starter_pdfx4.pdf) | `61e2622f5a5b81bd9e0f592951db0b2a7a8d362eca0ba6c8845ace3ac9b4874b` | One PDF/X-4 page with an embedded output intent |
| [PDFlib PDF/VT-1 starter](https://www.pdflib.com/fileadmin/cookbooks/PDFlib/results/starter_pdfvt1.pdf) | `8d2acc725edd75ed4cf818c357d286a6c70545a553ed0de16c9eb63ca3d65efc` | 100 pages, 100 leaf recipients, IDs `ID_0` through `ID_99`, record level 1 |
| [GWG Output Suite 5.0 combined X-4 pages](https://gwg.org/wp-content/uploads/2026/05/Ghent_PDF_Output_Suite_V50_Testpages.zip) | `6d9686303ea9049350bc1b3253c22a3cc8578bd7dfcc6afeaef89cbbe37d4cbe` | Six PDF/X-4 pages in `Ghent_PDF-Output-Test-V50_ALL_X4.pdf` |

## ICC asset

`FOGRA39L-Coated.icc` has SHA-256 `19d6cd1de88bf48816a7d2bfea5b71e3fff65d09ba0bb4b26f58f76cb4840929`. It contains a four-channel CMYK output-device profile from colord under CC0, distributed in the linked Postext revision. [colord source and rights](https://github.com/hughsie/colord/blob/78a33c9fb83ce4cca42864ec360ce6937d2ac1f4/data/profiles/FOGRA39L_coated.iccprofile.xml) · [Distribution and provenance](https://github.com/drnachio/postext/blob/366cea7f64f0f102b20a5e17c483ccd1f5b205c6/packages/postext/icc/README.md)
