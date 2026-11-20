# Corpus and accessibility review

The two generator scripts in `tests/assets/pdfua/` and `tests/assets/pdfa/`
use the Python standard library. All drawings, structure descriptions and
ICC profile bytes are generated from this repository's MIT-licensed source.
Running the scripts rewrites every fixture deterministically.

## Machine checks

CI runs `tools/verapdf.py` with the SHA-256-pinned veraPDF 1.28.2 jars
in `.github/workflows/ci.yml`. It forces the claimed flavour for each
original and every full and incremental save in the preservation harness.
The dual-claim fixture is checked under both PDF/UA-2 and PDF/A-4.
Arlington independently checks the object model.

Each positive has a negative control retaining its XMP claim. For ordinary
PDF/A the control removes the output intent needed by the DeviceRGB drawing.
For PDF/A-4f it removes the required EmbeddedFiles name tree. For PDF/UA it
removes the Figure's alternate description. The driver requires the precise
failed rule. Untagged annotations, attachment edits and page deletion have
separate expectations for the scopes in which they break conformance.
In UA-2 an attachment without a description fails 8.14.1-1. Deleting the
sole tagged page also prunes the root Document child and fails 8.2.5.2-1.
Issue [#848](https://github.com/yhay81/pyropdf/issues/848) tracks the
pre-save reporting of these effects.

## Human-verifiable checks

Render each original at normal viewing size and read its structure in order
using an accessibility inspector or a screen reader. For a reproducible
visual and structure reference using Poppler, run the following from the
repository root and repeat for the other fixture filenames.

```sh
pdftoppm -png -singlefile -scale-to 600 tests/assets/pdfua/pdfua-2.pdf /tmp/water-level
pdfinfo -struct-text tests/assets/pdfua/pdfua-2.pdf
pdfinfo -meta tests/assets/pdfua/pdfua-2.pdf
```

| Fixture | Visible meaning to compare with alternate text | Order, language and title to inspect |
| --- | --- | --- |
| pdfua-1.pdf | A blue rectangle crossed by a red diagonal line. The alternate description identifies both elements. | One Figure in the Document, en-US, title “pyropdf PDF/UA fixture”. |
| pdfua-2.pdf and pdfua-2-pdfa-4.pdf | Three blue bars rise from left to right, with heights in the ratio 1, 2, 3. The description explains the rise in water level and its scale. Height conveys the change independently of colour. | One Figure in the Document in the PDF 2.0 namespace, en-US, title “Water level across three readings”. |

Reviewers should record the reader and assistive technology versions, the
title announced, the order and content heard, and whether the alternate
description communicates the visible figure's purpose. Repeat with the
alternate description removed to check that its loss is perceptible. These
observations and any reader-specific issues are recorded separately from
the machine verdict. The automated corpus supplies no human sign-off.
