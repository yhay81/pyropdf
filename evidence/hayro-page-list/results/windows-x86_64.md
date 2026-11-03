# hayro's Pdf::new split, windows-x86_64

Written by `run.py` on an AMD Ryzen 5 3600XT, 2026-11-03. A second run gave
the same shares, with times up to 17% higher.

- hayro-syntax 0.8.0, pyropdf 0.2.0, rustc 1.99.1 (58cf8f9c3 2026-10-13)
- Windows AMD64; median of 50 opens per document

| Document | Pages | Objects | Cross-reference | Page list | Page list share |
| --- | ---: | ---: | ---: | ---: | ---: |
| pages-110.pdf | 110 | 403 | 0.029 ms | 0.479 ms | 94% |
| pages-440.pdf | 440 | 1198 | 0.094 ms | 1.906 ms | 95% |
| pages-1760.pdf | 1760 | 4378 | 0.351 ms | 6.977 ms | 95% |
