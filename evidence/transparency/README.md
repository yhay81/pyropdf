# Group compositing and detecting page transparency

Run the public controls and the independent PDF/A-4 page predicate from the repository root.

```sh
uv run pytest tests/test_page_transparency.py tests/test_transparency_compositing.py
uv run python evidence/transparency/verify_annex_q.py --jar /path/to/vera.jar
```

## Group controls

Samples below are 8-bit RGB at 72 dpi, away from antialiased edges. The first rectangle paints Multiply red over the page's blue left half and white right half. The second paints half-opaque green over part of the first. The inner group has no additional opacity. Ghostscript 10.08.0 used its default colour management and the png16m device. Poppler 26.11.0 used pdftoppm with PNG output.

| Group isolation | Knockout | Ghostscript over blue (20, 30) | Ghostscript overlap over white (47, 30) | Poppler at the same overlap |
| --- | --- | --- | --- | --- |
| false | false | 0, 0, 0 | 127, 128, 0 | 127, 128, 0 |
| false | true | 0, 0, 0 | 127, 255, 127 | 127, 128, 127 |
| true | false | 255, 0, 0 | 127, 128, 0 | 127, 128, 0 |
| true | true | 255, 0, 0 | 127, 255, 127 | 127, 128, 0 |

The two readers agree on isolation over blue. The Ghostscript overlap matches the knockout calculation over the initial white backdrop. Pinned hayro 394e4a0 returned (255, 0, 0) and (127, 128, 0) at these two samples in all four cases. On separate isolated, solid red groups, Ghostscript produced (255, 0, 0) in DeviceRGB, (130, 130, 130) in DeviceGray, and (237, 28, 36) in DeviceCMYK. Both pinned hayro and Poppler returned (255, 0, 0) in all three. Colour-managed RGB conversions can vary with the output profile, so the public test checks neutrality for DeviceGray and the red channel relationship for DeviceCMYK.

Independent controls also cover nested isolated 50% opacity, editing Multiply to Screen, and alpha and luminosity group masks. For these cases native and Poppler rendering agreed within three levels per channel. pikepdf separately reads the written state, groups and unmodified second page after full and incremental saves.

The remaining native group behavior is tracked in [#1024](https://github.com/yhay81/pyropdf/issues/1024). Existing upstream work [forwards the flags](https://github.com/LaurenzV/hayro/pull/1807) and [proposes knockout path and glyph compositing](https://github.com/LaurenzV/hayro/pull/1395).

## Annex Q controls

The predicate reads the current graphics state at paint time, follows used resource streams, and checks annotation blend modes and appearance streams. Annotation appearances and Type 3 procedures follow the published [ISO 32000-2 Annex Q corrections](https://pdf-issues.pdfa.org/32000-2-2020/clauseAnnexQ.html).

veraPDF 1.28.2 (CLI jar SHA-256 `687d4d8bcfec48c9f7e931cf78ed061f1eeccfba55d4cdc000ba9b4d8aa46ce6`) evaluated nineteen fixtures as originals, full rewrites and incremental saves. Each file contains a single page, has no output intent and declares no page-group colour space. These controls isolate `containsTransparency` in the PDF/A-4 rule ISO 19005-4:2020 §6.2.9 test 2. The small input files intentionally trigger unrelated archival-profile rules.

The pinned validator and public predicate agreed on 54 of 57 files. The three differences are saves of one inline image with nonstroking opacity 0.5. On its white backdrop, opaque gray 120 would produce (120, 120, 120). Half-opacity gray produces approximately (188, 188, 188), as measured with both Ghostscript and Poppler. The public predicate reports transparency in all three versions. The evidence driver checks the exact difference set so changes in independent verdicts are visible.
