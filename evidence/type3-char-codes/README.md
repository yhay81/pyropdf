# What hayro gives for Type 3 glyphs (issue #437)

The original investigation compared what hayro's glyph
callbacks reported for a Type 3 glyph in the release pyropdf then pinned
(hayro 0.8.0, hayro-interpret 0.8.0) and on `main`
(LaurenzV/hayro@04a801de, 2026-11-01), and whether pyropdf needs an
upstream change.

## Adoption in pyropdf

pyropdf uses the hayro 0.9-based revision `394e4a0d`, which includes
LaurenzV/hayro#1512. The public `unmapped="type3_char_code"` option uses
the code point with the same numeric value as the original Type 3 character
code when hayro has exhausted the font's Unicode and glyph-name mappings.
Custom encodings make this interpretation heuristic. On other fonts it
uses U+FFFD for missing mappings. The default skips unmapped glyphs and
warns. Text, words and characters use the same mapping and budget charges.

For `tests/assets/real_world/pdfium-type3.pdf`, PyMuPDF 2.0
(`doc[0].get_text()`) gives `'A\nA\nA\n'`. PDFium 156.0.8076.0 via
pypdfium2 5.14.0 (`doc[0].get_textpage().get_text_bounded()`) gives
`'A\r\nAA'`. Both emit the same three letters with different line breaks.
The probe below independently records the three source codes as 65. The
file's custom name `/C1` supplies no evidence of the intended character.

## Original upstream investigation

No new upstream change is needed. The change that #437 needs is already
merged on hayro `main` and waits on a release:

- LaurenzV/hayro#1512 (merged 2026-10-17, fixes LaurenzV/hayro#1331,
  supersedes the closed LaurenzV/hayro#1332) adds `Glyph::char_code()`,
  `OutlineGlyph::char_code()` and `Type3Glyph::char_code()`, and makes a
  Type 3 glyph's `as_unicode()` fall back to the glyph name that the font's
  `/Encoding` selects, as simple outline fonts do.
- LaurenzV/hayro#1678 (merged 2026-10-29) adds `Type3Glyph::advance_width`,
  which can replace pyropdf's half-font-size advance estimate for Type 3.
- LaurenzV/hayro#1696 and #1699 (2026-11-01) moved the font layer into a new
  `hayro-font` crate; `Glyph` stays reachable through
  `hayro_interpret::font`, but adoption should expect path changes.

None of these is in hayro-interpret 0.8.0 (tag `hayro-interpret-v0.8.0`,
2026-10-04). The issue's upstream state is therefore `upstream:merged`.

## Observed output

`probe.rs` implements a `Device` that prints, for each glyph,
`as_unicode()` and, on `main`, `char_code()`. `make_cases.py` writes
`type3-cases.pdf`, one Type 3 font per case showing codes 65 and 66.
`probe-output.txt` is the full output; in summary:

| Case | `/Differences` | `/ToUnicode` | 0.8.0 `as_unicode` | `main` `as_unicode` | `main` `char_code` |
| --- | --- | --- | --- | --- | --- |
| `pdfium-type3.pdf` corpus fixture | `65 /C1` | none | `None` ×3 | `None` ×3 | 65 ×3 |
| Custom names | `65 /C1 /C2` | none | `None`, `None` | `None`, `None` | 65, 66 |
| Standard names | `65 /A /B` | none | `None`, `None` | `A`, `B` | 65, 66 |
| `uniXXXX` and AGL non-Latin names | `65 /uni3042 /afii10017` | none | `None`, `None` | `あ`, `А` (U+0410) | 65, 66 |
| Valid `/ToUnicode` over custom names | `65 /C1 /C2` | 65→`B`, 66→`あ` | `B`, `あ` | `B`, `あ` | 65, 66 |
| `/ToUnicode` to U+0000 over standard names | `65 /A /B` | 65, 66→U+0000 | `\0`, `\0` | `A`, `B` | 65, 66 |
| Suffixed and malformed names | `65 /A.swash /uni004` | none | `None`, `None` | `A`, `None` | 65, 66 |

In 0.8.0 the fields of `Type3Glyph` (`char_code`, `glyph_id`, the font) are
`pub(crate)`, and `as_unicode()` reads only `/ToUnicode`. A Type 3 glyph
without one therefore gives pyropdf nothing to recover: not the code, not
the glyph name, and not even a standard name such as `/A`. A `/ToUnicode`
entry to U+0000 also comes through as a NUL character.

On `main`, a valid `/ToUnicode` keeps priority, a U+0000 entry falls back to
the glyph name, and standard, `uniXXXX`, and AGL names map. A custom name
such as `/C1` still gives `None`: hayro asserts no Unicode for it, and the
original code 65 is available through `char_code()`. That is enough for the
fixture's acceptance item, as an explicit, opt-in fallback that pyropdf
labels as heuristic.

## What stays with pyropdf

- hayro `main` does not expose the encoding-selected glyph name of a code
  (`Type3Font::code_to_ps_name` is private). pyropdf does not need it for
  #437: the code is the recoverable fact, and a caller that wants the name
  can read the font's `/Encoding` from the store. If a later issue wants the
  name as provenance, that is a small new upstream accessor, not part of
  this issue.
- Interpreting a code as Unicode (for example code 65 as `A`) is a pyropdf
  policy, not something hayro asserts; it must stay opt-in and labelled.

## Reproduce

From the repository root, with hayro `main` extracted by
`git archive origin/main` into `<hayro-main>`:

```bash
python3 evidence/type3-char-codes/make_cases.py evidence/type3-char-codes/type3-cases.pdf
```

Build `probe.rs` as `src/main.rs` of a binary crate that depends on
`kurbo = "0.13.0"` and either `hayro-interpret = "=0.8.0"` or
`hayro-interpret = { path = "<hayro-main>/hayro-interpret" }` with a
`char_code` feature enabled, and run it with
`tests/assets/real_world/pdfium-type3.pdf evidence/type3-char-codes/type3-cases.pdf`.
