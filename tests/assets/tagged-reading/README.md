# Logical reading order

These two unmodified files come from the PDF Association's [Techniques for Accessible PDF](https://github.com/pdf-association/techniques-for-accessible-pdf/tree/3e31095d8aeea941eccbbb766a23369374c2e9a6/fundamentals/4-logical-content-order), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

| Technique | SHA-256 | What the structure specifies |
| --- | --- | --- |
| G4_02 | `490a3d74cbf5c6315a06890e5d39c3261481b215bc73598b35aa7891754cd87e` | Five lines in order in each of two paragraphs. |
| G4_F02 | `e79543cc5765b7111920293f73601addd6803b58440cb2c4d7141f7314d7e754` | A failure label followed by five lines in reverse order within each paragraph. |

`pdfinfo -struct-text FILE.pdf` prints each referenced text fragment in the order given by the PDF. The public tests retain the intentionally incorrect order in G4_F02.

## Reused Form XObjects

`shared-forms.pdf` is an original two-page fixture under the repository license.
Both pages paint the same Form (11), and the first also paints Form 10 inside
Form 12. Each source uses MCID 0. MCID 1 on the first page surrounds the
shared Form invocation. The shared Form defines its expansion in its own
named property list and contains a separately marked artifact. The last
structure element has an explicit Japanese replacement.

With Java 17 or later and the [Apache PDFBox 3.0.5 application JAR](https://archive.apache.org/dist/pdfbox/3.0.5/),
run the independent text and structure reader from this directory.

```sh
java -cp /path/to/pdfbox-app-3.0.5.jar FormText.java shared-forms.pdf
```

Each row identifies the one-based page, form object (zero for page content),
MCID, decoded text and expanded text. PDFBox follows the graphics interpreter
into each Form, uses its resources to decode glyphs, excludes the artifact,
then visits the logical children in their declared order.

| Page | Form | MCID | Text | Expanded |
| --- | --- | --- | --- | --- |
| 2 | 11 | 0 | Shared | Shared expanded |
| 1 | 0 | 0 | Page one | Page one |
| 1 | 10 | 0 | Nested | Nested |
| 2 | 0 | 0 | Page two | Page two |
| 1 | 0 | 1 | Shared | Shared |
| 1 | 11 | 0 | 日本 | 日本 |

The public tests check the identified Form substitution, explicit element
replacement and page-local text. The remaining glyph rows need the interpreter's
stream provenance in [#104](https://github.com/yhay81/pyropdf/issues/104).
