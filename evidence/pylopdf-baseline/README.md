# pylopdf baseline

The measurements behind section 2.1 of [DESIGN.md](../../DESIGN.md). Each
script runs against an installed pylopdf and takes the files it reads as
arguments.

| Script | Measures |
| --- | --- |
| `edit_read_loop.py BILL` | Reading every page, and rotating then reading every page, at 110, 440, and 1,760 pages |
| `form_fill_cost.py FORM BILL` | One text-field fill as pages without fields join the document |
| `field_read_cost.py` | Reading every field's value through handles, at 100, 400, and 1,600 fields |
| `font_subsets.py FONT` | Embedded font programs after stamping one face on 1, 10, and 100 pages, with and without `dedupe` |

`BILL` is pylopdf's `tests/assets/real_world/bill-hr815.pdf`, `FORM` its
`tests/assets/real_world/f1040.pdf`, and `FONT` the
`NotoSansHebrew-Variable.ttf` of `pylopdf-fonts-he`. From a pylopdf checkout:

```bash
uv run --no-sync python path/to/edit_read_loop.py tests/assets/real_world/bill-hr815.pdf
```

The results in DESIGN.md come from two runs on 2026-10-24, on macOS arm64
with CPython 3.14.8 and the pylopdf 0.15.0 extension of a development
environment; where the runs differ, DESIGN.md gives both.
