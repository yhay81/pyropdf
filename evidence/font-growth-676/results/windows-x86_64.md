# Font subsetting and compaction, before and after #676

Written by `run.py`; the README describes the workload.

- CPU: AMD Ryzen 5 3600XT (AMD64 Family 23 Model 113)
- System: Windows 11 (10.0.26300)
- Rust: rustc 1.99.1
- Date: 2026-11-05
- Base 0bae7fd, head 33551e2, five rounds

| Face | Operation | Glyphs | Kept | Face bytes | Result bytes | Base µs | Head µs | Change |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| NotoSansJP-subset.otf | subset | 99 | 99 | 15,240 | 15,344 | 70 | 72 | +2.9% |
| NotoSansJP-subset.otf | compact | 99 | 99 | 15,240 | 12,450 | 229 | 231 | +0.9% |
| NotoSansHebrew-subset.ttf | subset | 13 | 13 | 6,408 | 6,448 | 12 | 12 | +0.0% |
| NotoSansHebrew-subset.ttf | compact | 13 | 13 | 6,408 | 5,176 | 10 | 10 | +0.0% |
| arial.ttf | subset | 4,652 | 531 | 1,045,956 | 492,544 | 1,118 | 1,091 | -2.4% |
| arial.ttf | compact | 4,652 | 531 | 1,045,956 | 150,748 | 281 | 273 | -2.8% |
| segoeui.ttf | subset | 5,395 | 578 | 960,076 | 433,484 | 879 | 888 | +1.0% |
| segoeui.ttf | compact | 5,395 | 578 | 960,076 | 102,220 | 201 | 196 | -2.5% |
| ARIALUNI.ttf | subset | 50,386 | 3,390 | 23,449,860 | 2,471,392 | 7,334 | 7,343 | +0.1% |
| ARIALUNI.ttf | compact | 50,386 | 3,390 | 23,449,860 | 1,656,464 | 4,021 | 3,965 | -1.4% |
| Xolonium-Regular.otf | subset | 847 | 293 | 230,760 | 205,372 | 436 | 455 | +4.4% |
| Xolonium-Regular.otf | compact | 847 | 293 | 230,760 | 28,660 | 412 | 422 | +2.4% |
| NotoSansJP-Regular.otf | subset | 17,936 | 1,361 | 4,533,028 | 948,080 | 2,308 | 2,234 | -3.2% |
| NotoSansJP-Regular.otf | compact | 17,936 | 1,361 | 4,533,028 | 393,221 | 5,265 | 5,296 | +0.6% |
