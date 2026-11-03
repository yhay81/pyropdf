# hayro-syntax's Pdf::new with page attributes read on first use, windows-x86_64

Written by `run.py` on an AMD Ryzen 5 3600XT, 2026-11-03, while other jobs
kept the machine busy; the versions run in turn, so they share that load.
Three more runs of the documents gave speedups of 2.26x to 2.54x. In them,
opening and then reading every page's attributes took 7 to 12% longer after
the change than before at 1,760 pages (19% in the run below), and between 8%
less and 4% more at 440 (13% more below).

## Documents

`uv run python evidence/hayro-page-attributes/run.py`

- before: https://github.com/LaurenzV/hayro/commit/4f7d16fab1a4b2c8de77d109674335e4536258b7
- geometry: https://github.com/yhay81/hayro/commit/f8cb1c43f752726ccceae7204212c522bd26cd4b
- after: https://github.com/yhay81/hayro/commit/7da104ef5b0cb6594a731a55745f6decd4c59294
- pyropdf 0.2.0, rustc 1.99.1 (58cf8f9c3 2026-10-13)
- Windows AMD64; median of 101 runs per document, version, and workload

| Document | Pages | Objects | `Pdf::new`, before | geometry | after | Speedup | Then every page's attributes, before | geometry | after |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| pages-110.pdf | 110 | 403 | 0.653 ms | 0.542 ms | 0.296 ms | 2.20x | 0.671 ms | 0.642 ms | 0.673 ms |
| pages-440.pdf | 440 | 1198 | 2.262 ms | 1.589 ms | 0.879 ms | 2.57x | 1.961 ms | 2.065 ms | 2.219 ms |
| pages-1760.pdf | 1760 | 4378 | 10.035 ms | 8.002 ms | 4.155 ms | 2.41x | 9.913 ms | 10.909 ms | 11.819 ms |

## Corpus

`uv run python evidence/hayro-page-attributes/run.py --corpus HAYRO/tests/downloads HAYRO/tests/pdfs`:
the 1,675 PDFs of a hayro checkout at the base revision after its test suite ran.

- before: https://github.com/LaurenzV/hayro/commit/4f7d16fab1a4b2c8de77d109674335e4536258b7
- geometry: https://github.com/yhay81/hayro/commit/f8cb1c43f752726ccceae7204212c522bd26cd4b
- after: https://github.com/yhay81/hayro/commit/7da104ef5b0cb6594a731a55745f6decd4c59294
- pyropdf 0.2.0, rustc 1.99.1 (58cf8f9c3 2026-10-13)
- Windows AMD64; median of 5 runs per document, version, and workload

| PDFs | Read the same | Pages | Unreadable by all | Different | `Pdf::new` total, before | geometry | after | Speedup | Then every page's attributes, before | geometry | after |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1675 | 1605 | 11328 | 70 | 0 | 639.9 ms | 609.5 ms | 475.9 ms | 1.34x | 612.8 ms | 622.7 ms | 570.3 ms |

| Slowest to open before | Pages | `Pdf::new`, before | geometry | after | Speedup |
| --- | ---: | ---: | ---: | ---: | ---: |
| 5952.pdf | 1 | 58.712 ms | 48.890 ms | 16.107 ms | 3.65x |
| 0092523.pdf | 93 | 46.031 ms | 52.700 ms | 44.157 ms | 1.04x |
| 0155999.pdf | 62 | 36.658 ms | 6.831 ms | 0.177 ms | 206.52x |
| 0134905.pdf | 14 | 35.736 ms | 37.787 ms | 35.913 ms | 1.00x |
| issue2098.pdf | 158 | 35.380 ms | 39.005 ms | 37.009 ms | 0.96x |
