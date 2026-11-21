# Independent PDF/A and PDF/UA sources

The 26 PDF files exercise 13 flavours with a conforming source and a targeted negative. The original files are by the veraPDF Consortium under CC BY 4.0. The accompanying manifest identifies the original path, exact repository revision, SHA-256 before and after any change, and the change for every file. [Source corpus and attribution](https://github.com/veraPDF/veraPDF-corpus/tree/bb75f4f0073d9350dfd058c0162a367e6fadf25e) · [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/) · [Source manifest](sources.json)

The corpus has a dedicated PDF/A-3b directory. The A-3a and A-3u pairs reuse the corresponding A-2a and A-2u PDF content, changing only the one-byte XMP part digit from 2 to 3. Both pairs pass or fail their indicated rule under the forced part-3 validator. The A-4e negative omits its E conformance value. Its public claim consequently reads PDF/A-4 while the forced A-4e validation rejects it.

## Reproduce the checks

```sh
uv run pytest tests/test_profile_sources.py
uv run python tools/verapdf.py --jar /path/to/greenfield-apps-1.28.2.jar --profile-sources-only
```

The normal CI validator also runs this matrix. The pinned veraPDF 1.28.2 jar has SHA-256 `687d4d8bcfec48c9f7e931cf78ed061f1eeccfba55d4cdc000ba9b4d8aa46ce6`.

| Flavour | Source negative rule |
| --- | --- |
| PDF/A-1a | 6.8.2.2-1 |
| PDF/A-1b | 6.2.3.3-2 |
| PDF/A-2a and 3a | 6.7.3.4-1 |
| PDF/A-2b | 6.1.13-11 |
| PDF/A-2u and 3u | 6.2.11.7.2-1 |
| PDF/A-3b | 6.8-4 |
| PDF/A-4 | 6.2.4.3-2 |
| PDF/A-4e | 6.7.3-3 |
| PDF/A-4f | 6.9-5 |
| PDF/UA-1 | 7.1-3 |
| PDF/UA-2 | 8.4.5.7-2 |

All 111 expected outcomes matched. Each of the original sources, a full save after rotation, and an incremental save after the same edit retained its expected result. That produced 39 independently accepted documents and 39 negatives with exactly their one expected rule. Eleven new profile documents and their two saves added 33 independent passes. The authoring fixture starts from an empty Document, embeds shaped text, creates Document/P/Figure structure, describes a coloured square, writes the identification metadata, and supplies the appropriate archive output intent and PDF/A-4f attachment. The dual PDF/A-4 plus PDF/UA-2 public authoring workflow is tracked separately in [#846](https://github.com/yhay81/pyropdf/issues/846).

Two additional pristine third-party files supplied independent A-3a and A-3u confirmations. For each file, open it, read its matching profile, rotate the first page by 90 degrees and save both ways. Force the corresponding 3a or 3u flavour on the source and both saves. The six outcomes passed with the same pinned validator. These optional files can be fetched directly from their original projects.

- Apache PDFBox's Word-produced A-3a sample has SHA-256 `f0d3b0e32007d4b385b8d31bd759ce0b3ec8a465d48dc7471b99faf5d8bd3b57`. [PDFBox source](https://github.com/apache/pdfbox/blob/b81a7c5648ed0e47f3976fe53adc311176dec5bb/pdfbox/src/test/resources/input/merge/PDFA3A.pdf)
- iText's A-3u check-field appearance sample has SHA-256 `ba18bf3e4c6f8082abb9da97481a972dae3e4d13ed1ccf67b3e7b724a9fe7d09`. [iText source](https://github.com/itext/itext-java/blob/0105269a16a94f4bfe910db09428528f5ff52d8a/pdfa/src/test/resources/com/itextpdf/pdfa/cmp/PdfACheckfieldTest/cmp_pdfA3u_checkFieldOnAppearance.pdf)
