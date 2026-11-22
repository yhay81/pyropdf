# Independent profile edit controls

Issue [#932](https://github.com/yhay81/pyropdf/issues/932) extends the shared
mutation matrix with alternate PDF encodings. `profile_edits.py` prepares
each input from the checked-in `pdfa/pdfa-*.pdf` or `pdfua/pdfua-*.pdf`
source. The dual fixture `pdfua/pdfua-2-pdfa-4.pdf` runs under **both**
forced profiles on exactly the same bytes. The individual PDF/A fixtures
cover 1a, 1b, 2a, 2b, 2u, 3a, 3b, 3u, 4, 4e and 4f.

The 188 prepared inputs and their rotation-only controls pass veraPDF 1.28.2.
The matrix produces 1,301 explicit profile verdicts, with 805 passes and 496
intentional rejections.
The table records the complete failed-rule sets for the edit. Both full and
incremental saves give the listed result. For each rejection, opening the
rejected full save and rotating the page retains exactly the same failed
rules in both modes. The existing CMYK failure is also checked after removing the RGB intent. That last edit has an empty pre-save claim report.

| Representation and edit | PDF/A-1 | PDF/A-2 and -3 | PDF/A-4, -4e and -4f | PDF/UA-1 | PDF/UA-2 |
| --- | --- | --- | --- | --- | --- |
| Invoked inner Form using DeviceRGB, remove catalog intent | 6.2.3.3-1 | 6.2.4.3-2 | 6.2.4.3-2 | — | Pass¹ |
| Paint the inner Form in implicit DeviceGray, remove the intent | 6.2.3.3-3 | 6.2.4.3-4 | 6.2.4.3-4 | — | Pass¹ |
| Switch the inner Form to DeviceCMYK with its RGB intent still present | 6.2.3.3-2 | 6.2.4.3-3 | 6.2.4.3-3 | — | Pass¹ |
| Same Form with ICCBased DefaultRGB, remove catalog intent | Pass | Pass | Pass | — | Pass¹ |
| Page inherits ICCBased DefaultRGB and has no intent, remove DefaultRGB on Pages | 6.2.3.3-1 | 6.2.4.3-2 | 6.2.4.3-2 | — | Pass¹ |
| Remove the sole page output intent | — | — | 6.2.4.3-2 | — | Pass¹ |
| Remove the page intent with the catalog intent still present | — | — | Pass | — | Pass¹ |
| Nested simple TrueType font, remove its program after a shown space or an empty text-show | 6.3.4-1 | 6.2.11.4.1-1 | 6.2.10.4.1-1 | 7.21.4.1-1 | 8.4.5.5.1-1 |
| Remove that program when no text operator selects the font | Pass | Pass | Pass | Pass | Pass |
| Nested Type 0/CIDFontType0 font, remove descendant descriptor's program | 6.3.4-1, 6.3.5-3 | 6.2.11.4.1-1 | 6.2.10.4.1-1 | 7.21.4.1-1 | 8.4.5.5.1-1 |
| Nested Type 3 font, remove the procedure for the encoded, shown A | 6.3.5-1, 6.3.6-1 | 6.2.11.4.1-2, 6.2.11.5-1 | 6.2.10.4.1-2, 6.2.10.5-1 | 7.21.4.1-2, 7.21.5-1 | 8.4.5.5.1-2, 8.4.5.6-1 |
| Remove the encoded Type 3 procedure for B while only A is shown | Pass | Pass | Pass | Pass | Pass |
| Remove the MCID from the property dictionary named by BDC | Pass² | Pass² | Pass¹ | 7.1-3 | 8.2.2-1 |
| Replace the used parent-tree slot with null, retaining its Kids, Nums and indirect value array | Pass² | Pass² | Pass¹ | 7.1-3 | 8.2.2-1 |
| Point that slot at another existing structure element | Pass² | Pass² | Pass¹ | Pass | Pass |
| Remove the Figure's sole ActualText, or remove its Alt when its parent has Alt | Pass² | Pass² | Pass¹ | 7.3-1 | 8.2.5.28.2-1 |
| Remove the Figure's ActualText when its own Alt remains | Pass² | Pass² | Pass¹ | Pass | Pass |

¹ Tested through the dual claim. ² Tagged level A fixture. A dash denotes a
representation outside the selected fixture's scope.

The nested font sources are the repository's OFL Noto Sans Hebrew simple
TrueType and Noto Sans JP composite CFF fixtures. The Type 3 glyph, number
tree, named property, nested Forms, and their controls are generated locally.
The validator accepts an alternate existing parent-tree target, even though
a reader must still decide whether it describes the intended content. The
tests evaluate machine rules. The separate authoring work in
[#846](https://github.com/yhay81/pyropdf/issues/846) and the broader source
corpus in [#911](https://github.com/yhay81/pyropdf/issues/911) retain their
own acceptance criteria.

Reproduce with `uv run python tools/verapdf.py --jar /path/to/greenfield-apps-1.28.2.jar`.
