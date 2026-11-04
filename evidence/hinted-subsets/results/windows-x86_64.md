# Hinted renders of subset font programs, windows-x86_64

Written by `run.py` on 2026-11-04, with the script of #611 at both commits.

`uv run python evidence/hinted-subsets/run.py`

- hayro: https://github.com/yhay81/hayro/commit/394e4a0d37baf8af513f6ebda15f4bada85d6f23
- rustc 1.99.1 (58cf8f9c3 2026-10-13), Windows AMD64

## Before #611, with pyropdf at 614fb83

mhlw-doc.pdf: programs subset: 1; the file goes from 332,904 to 126,024 bytes
Canvas text: the program goes from 15,152 to 1,109 bytes

| pair | page | hinting | differing pixels | pixels | largest difference |
| --- | --- | --- | --- | --- | --- |
| mhlw-doc.pdf | 0 | true | 5907 | 500395 | 239 |
| mhlw-doc.pdf | 1 | true | 1349 | 500395 | 225 |
| mhlw-doc.pdf | 0 | false | 0 | 500395 | 0 |
| mhlw-doc.pdf | 1 | false | 0 | 500395 | 0 |
| canvas text | 0 | true | 873 | 36000 | 207 |
| canvas text | 0 | false | 0 | 36000 | 0 |

| stamps | saved bytes | program bytes |
| --- | --- | --- |
| latin, same | 19,854 | 733 |
| latin, varying | 21,797 | 796 |
| japanese, same | 20,449 | 1,320 |
| japanese, varying | 21,733 | 1,320 |

## With #611

mhlw-doc.pdf: programs subset: 1; the file goes from 332,904 to 126,024 bytes
Canvas text: the program goes from 15,152 to 1,432 bytes

| pair | page | hinting | differing pixels | pixels | largest difference |
| --- | --- | --- | --- | --- | --- |
| mhlw-doc.pdf | 0 | true | 5907 | 500395 | 239 |
| mhlw-doc.pdf | 1 | true | 1349 | 500395 | 225 |
| mhlw-doc.pdf | 0 | false | 0 | 500395 | 0 |
| mhlw-doc.pdf | 1 | false | 0 | 500395 | 0 |
| canvas text | 0 | true | 0 | 36000 | 0 |
| canvas text | 0 | false | 0 | 36000 | 0 |

| stamps | saved bytes | program bytes |
| --- | --- | --- |
| latin, same | 20,034 | 913 |
| latin, varying | 21,998 | 997 |
| japanese, same | 20,763 | 1,634 |
| japanese, varying | 22,047 | 1,634 |
