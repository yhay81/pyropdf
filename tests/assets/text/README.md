# Text fixtures

| File | Content | Source and license |
| --- | --- | --- |
| `hebrew-niqqud.pdf` | One line, "שָׁלוֹם עולם", shaped in visual order with zero-advance marks placed over their base letters (#469) | Written by `evidence/embedded-text/probe` (`embedded-text-probe NotoSansHebrew-Regular.ttf out.pdf 1 "שָׁלוֹם עולם"`), then subset with `Document.subset_fonts`. The embedded subset of Noto Sans Hebrew Regular is under the SIL Open Font License 1.1: [LICENSES/NotoSansHebrew-OFL-1.1.txt](LICENSES/NotoSansHebrew-OFL-1.1.txt). |
| `nonembedded-cjk.pdf` | Seven pages, each one line in a CID font that the file does not embed (#600): 計画地 in MS-Gothic, as `CIDFontType0` and `CIDFontType2` with `UniJIS-UCS2-H` and as `CIDFontType2` with `90ms-RKSJ-H`, and in MS-Mincho; 简体中文 in SimHei (Adobe-GB1), 繁體中文 in MingLiU (Adobe-CNS1), and 한국어 in Batang (Adobe-Korea1) | Written by `nonembedded_cjk_pdf` in `tests/test_fallback_fonts.py`, which checks that the file holds its pages. It embeds no font program. |
