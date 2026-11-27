# Rich-text + BiDi Layout Prototype Benchmark (`macos-arm64`, 50 runs, best)

| Case | Option A `parley` (ms) | A lines / glyphs | Option B `harfrust+bidi` (ms) | B lines / glyphs | B peak F6 Budget (KiB) | B rules / links |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `styled-latin-justified` | 0.021 | 2 / 36 | 0.049 | 2 / 35 | 6.1 | 3 / 1 |
| `bidi-ltr-with-hebrew-and-digits` | 0.018 | 1 / 25 | 0.057 | 1 / 25 | 3.8 | 1 / 1 |
| `bidi-rtl-arabic-wrapped` | 0.020 | 2 / 35 | 0.060 | 2 / 34 | 4.4 | 1 / 1 |
| `multilingual-cjk-hebrew-arabic` | 0.021 | 3 / 35 | 0.072 | 3 / 33 | 6.5 | 2 / 1 |
| `long-bidi-500-tokens` | 0.361 | 33 / 1760 | 0.866 | 33 / 1727 | 210.7 | 0 / 0 |

F6 budget refusal verification (Option B): `ShapedGlyphCount`=true, `TextLineCount`=true, `MemorySize`=true.
Mixed Standard-14 + OpenType verification (Option B): 1 line, 2 runs (WinAnsi Helvetica + RTL Hebrew), 1 link hit-box.
