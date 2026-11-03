# Hinted renders of subset font programs, windows-x86_64

Written by `run.py` on 2026-11-03 with pyropdf at the head of
`claude/18-hayro-0.9-u32` before it turned hinting off, so that pyropdf's
own renders, of the canvas case, were hinted as hayro renders by default.

`uv run python evidence/hinted-subsets/run.py`

- hayro: https://github.com/yhay81/hayro/commit/394e4a0d37baf8af513f6ebda15f4bada85d6f23
- rustc 1.99.1 (58cf8f9c3 2026-10-13), Windows AMD64

mhlw-doc.pdf: programs subset: 1; the file goes from 332,904 to 126,024 bytes

| pair | page | hinting | differing pixels | pixels | largest difference |
| --- | --- | --- | --- | --- | --- |
| mhlw-doc.pdf | 0 | true | 5907 | 500395 | 239 |
| mhlw-doc.pdf | 1 | true | 1349 | 500395 | 225 |
| mhlw-doc.pdf | 0 | false | 0 | 500395 | 0 |
| mhlw-doc.pdf | 1 | false | 0 | 500395 | 0 |

Canvas text in a CID-keyed CFF face, rendered by pyropdf before and after the save: 873 of 36000 pixels differ.
