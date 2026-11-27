# Reproducible crash, exponential-parse, and truncation corpus under `DocumentLimits.web()`

- Platform: `Darwin arm64`
- Python: `3.15.2`
- pyropdf: `0.4.1`
- Per-case timeout: `15.0s`

| Case | Issue | PDF bytes | Method | Outcome | Signal / Exit | Time (ms) | Detail |
| --- | --- | ---: | --- | --- | --- | ---: | --- |
| `type4-braces-20000` | #15 | 40,654 | `page.text()` | **crash** | SIGSEGV (exit 139) | 290.2 | `exit -11` |
| `type4-braces-20000` | #15 | 40,654 | `page.pixmap()` | **crash** | SIGSEGV (exit 139) | 248.5 | `exit -11` |
| `type4-braces-20000` | #15 | 40,654 | `page.content()` | **crash** | SIGSEGV (exit 139) | 227.8 | `exit -11` |
| `type4-braces-50000` | #15 | 100,656 | `page.text()` | **crash** | SIGSEGV (exit 139) | 268.8 | `exit -11` |
| `type4-braces-50000` | #15 | 100,656 | `page.pixmap()` | **crash** | SIGSEGV (exit 139) | 235.4 | `exit -11` |
| `type4-braces-50000` | #15 | 100,656 | `page.content()` | **crash** | SIGSEGV (exit 139) | 228.9 | `exit -11` |
| `type4-braces-50000` | #15 | 100,656 | `page.svg()` | **crash** | SIGSEGV (exit 139) | 231.3 | `exit -11` |
| `type4-braces-50000-separation` | #15 | 100,647 | `page.text()` | **crash** | SIGSEGV (exit 139) | 330.9 | `exit -11` |
| `type4-braces-50000-separation` | #15 | 100,647 | `page.pixmap()` | **crash** | SIGSEGV (exit 139) | 267.1 | `exit -11` |
| `type4-braces-50000-separation` | #15 | 100,647 | `page.content()` | **crash** | SIGSEGV (exit 139) | 256.5 | `exit -11` |
| `type4-braces-50000-extgstate-tr` | #15 | 100,629 | `page.text()` | **crash** | SIGSEGV (exit 139) | 317.4 | `exit -11` |
| `type4-braces-50000-extgstate-tr` | #15 | 100,629 | `page.pixmap()` | **crash** | SIGSEGV (exit 139) | 1156.3 | `exit -11` |
| `type4-braces-50000-extgstate-tr` | #15 | 100,629 | `page.content()` | **crash** | SIGSEGV (exit 139) | 431.4 | `exit -11` |
| `type3-function-cycle` | #15 | 649 | `page.text()` | **returned** | 0 | 181.4 | `len=0 text=''` |
| `type3-function-cycle` | #15 | 649 | `page.pixmap()` | **returned** | 0 | 274.9 | `200x200` |
| `type3-function-chain-20000` | #15 | 2,238,475 | `page.text()` | **crash** | SIGSEGV (exit 139) | 719.4 | `exit -11` |
| `type3-function-chain-20000` | #15 | 2,238,475 | `page.pixmap()` | **crash** | SIGSEGV (exit 139) | 584.0 | `exit -11` |
| `type3-function-chain-20000` | #15 | 2,238,475 | `page.content()` | **crash** | SIGSEGV (exit 139) | 473.2 | `exit -11` |
| `type3-function-chain-20000` | #15 | 2,238,475 | `page.svg()` | **crash** | SIGSEGV (exit 139) | 416.0 | `exit -11` |
| `cs-indexed-cycle` | #15 | 519 | `page.text()` | **returned** | 0 | 178.3 | `len=0 text=''` |
| `cs-indexed-cycle` | #15 | 519 | `page.pixmap()` | **returned** | 0 | 166.2 | `200x200` |
| `cs-indexed-chain-20000` | #15 | 1,318,346 | `page.text()` | **crash** | SIGSEGV (exit 139) | 343.2 | `exit -11` |
| `cs-indexed-chain-20000` | #15 | 1,318,346 | `page.pixmap()` | **crash** | SIGSEGV (exit 139) | 336.8 | `exit -11` |
| `cs-indexed-chain-20000` | #15 | 1,318,346 | `page.content()` | **crash** | SIGSEGV (exit 139) | 348.2 | `exit -11` |
| `cs-indexed-chain-20000` | #15 | 1,318,346 | `page.svg()` | **crash** | SIGSEGV (exit 139) | 343.5 | `exit -11` |
| `cs-separation-chain-20000` | #15 | 1,587,326 | `page.text()` | **crash** | SIGSEGV (exit 139) | 346.3 | `exit -11` |
| `cs-separation-chain-20000` | #15 | 1,587,326 | `page.pixmap()` | **crash** | SIGSEGV (exit 139) | 442.2 | `exit -11` |
| `cs-separation-chain-20000` | #15 | 1,587,326 | `page.content()` | **crash** | SIGSEGV (exit 139) | 478.8 | `exit -11` |
| `cs-icc-alternate-chain-10000` | #15 | 1,568,350 | `page.text()` | **crash** | SIGSEGV (exit 139) | 784.7 | `exit -11` |
| `cs-icc-alternate-chain-10000` | #15 | 1,568,350 | `page.pixmap()` | **crash** | SIGSEGV (exit 139) | 729.1 | `exit -11` |
| `cs-icc-alternate-chain-10000` | #15 | 1,568,350 | `page.content()` | **crash** | SIGSEGV (exit 139) | 652.4 | `exit -11` |
| `inline-image-nested-22` | #740 | 459 | `page.text()` | **returned** | 0 | 1038.9 | `len=0 text=''` |
| `inline-image-nested-22` | #740 | 459 | `page.pixmap()` | **returned** | 0 | 1047.4 | `200x200` |
| `inline-image-nested-22` | #740 | 459 | `page.content()` | **returned** | 0 | 1021.5 | `items=22` |
| `inline-image-nested-22` | #740 | 459 | `page.operations` | **returned** | 0 | 1071.5 | `ops=24` |
| `inline-image-nested-25` | #740 | 461 | `page.text()` | **returned** | 0 | 6987.7 | `len=0 text=''` |
| `inline-image-nested-25` | #740 | 461 | `page.pixmap()` | **returned** | 0 | 12337.0 | `200x200` |
| `inline-image-nested-25` | #740 | 461 | `page.operations` | **returned** | 0 | 7805.5 | `ops=30` |
| `inline-image-nested-200000` | #740 | 12,120 | `page.text()` | **crash** | SIGSEGV (exit 139) | 303.4 | `exit -11` |
| `inline-image-nested-200000` | #740 | 12,120 | `page.pixmap()` | **crash** | SIGSEGV (exit 139) | 278.4 | `exit -11` |
| `inline-image-nested-200000` | #740 | 12,120 | `page.content()` | **crash** | SIGSEGV (exit 139) | 326.4 | `exit -11` |
| `inline-image-nested-200000` | #740 | 12,120 | `page.operations` | **crash** | SIGSEGV (exit 139) | 290.3 | `exit -11` |
| `tiling-pattern-chain-2000` | new-audit | 466,029 | `page.text()` | **returned** | 0 | 202.3 | `len=0 text=''` |
| `tiling-pattern-chain-2000` | new-audit | 466,029 | `page.pixmap()` | **crash** | SIGSEGV (exit 139) | 384.7 | `exit -11` |
| `tiling-pattern-chain-2000` | new-audit | 466,029 | `page.content()` | **returned** | 0 | 200.7 | `items=1` |
| `tiling-pattern-chain-2000` | new-audit | 466,029 | `page.svg()` | **crash** | SIGSEGV (exit 139) | 297.4 | `exit -11` |
| `type3-glyph-chain-2000` | new-audit | 736,020 | `page.text()` | **returned** | 0 | 202.0 | `len=2 text='a\n'` |
| `type3-glyph-chain-2000` | new-audit | 736,020 | `page.pixmap()` | **crash** | SIGSEGV (exit 139) | 319.3 | `exit -11` |
| `type3-glyph-chain-2000` | new-audit | 736,020 | `page.content()` | **crash** | SIGSEGV (exit 139) | 318.9 | `exit -11` |
| `type3-glyph-chain-2000` | new-audit | 736,020 | `page.svg()` | **crash** | SIGSEGV (exit 139) | 304.4 | `exit -11` |
| `form-xobject-cycle` | hayro-bounded | 588 | `page.text()` | **returned** | 0 | 209.2 | `len=0 text=''` |
| `form-xobject-cycle` | hayro-bounded | 588 | `page.pixmap()` | **returned** | 0 | 180.2 | `200x200` |
| `form-xobject-cycle` | hayro-bounded | 588 | `page.content()` | **returned** | 0 | 174.5 | `items=0` |
| `soft-mask-cycle` | hayro-bounded | 754 | `page.text()` | **returned** | 0 | 175.3 | `len=0 text=''` |
| `soft-mask-cycle` | hayro-bounded | 754 | `page.pixmap()` | **returned** | 0 | 169.8 | `200x200` |
| `soft-mask-cycle` | hayro-bounded | 754 | `page.content()` | **returned** | 0 | 157.9 | `items=1` |
| `object-depth-65` | store-bounded | 588 | `page.text()` | **limit_error** | 0 | 163.7 | `object_depth: object 5 0: an object nests arrays and dictionaries too deeply: 65 levels, over the limit of 64` |
| `content-operand-depth-600` | #15/#1666 | 1,700 | `page.text()` | **returned** | 0 | 177.5 | `len=0 text=''` |
| `content-operand-depth-600` | #15/#1666 | 1,700 | `page.pixmap()` | **returned** | 0 | 168.6 | `200x200` |
| `content-operand-depth-600` | #15/#1666 | 1,700 | `page.content()` | **returned** | 0 | 193.0 | `items=0` |
| `content-operand-depth-600` | #15/#1666 | 1,700 | `page.operations` | **returned** | 0 | 162.4 | `ops=0` |
