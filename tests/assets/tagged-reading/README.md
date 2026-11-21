# Logical reading order

These two unmodified files come from the PDF Association's [Techniques for Accessible PDF](https://github.com/pdf-association/techniques-for-accessible-pdf/tree/3e31095d8aeea941eccbbb766a23369374c2e9a6/fundamentals/4-logical-content-order), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

| Technique | SHA-256 | What the structure specifies |
| --- | --- | --- |
| G4_02 | `490a3d74cbf5c6315a06890e5d39c3261481b215bc73598b35aa7891754cd87e` | Five lines in order in each of two paragraphs. |
| G4_F02 | `e79543cc5765b7111920293f73601addd6803b58440cb2c4d7141f7314d7e754` | A failure label followed by five lines in reverse order within each paragraph. |

`pdfinfo -struct-text FILE.pdf` prints each referenced text fragment in the order given by the PDF. The public tests retain the intentionally incorrect order in G4_F02.
