# Form fill cost

The measurement behind issues #193 and #203: setting a field's value writes
the field and its widgets as one transaction of the store, and the
appearance settler draws only the widgets that the fill marks (DESIGN.md
sections 5 and 7), so a fill costs the same as pages without fields join
the document. pylopdf cloned the whole document for each fill so that it
could roll back (DESIGN.md section 2.1).

## Workload

pylopdf's `evidence/pylopdf-baseline/form_fill_cost.py`: the first text
field of `f1040.pdf`, after the pages of `bill-hr815.pdf` join it 0, 4, and
16 times and the document is saved and opened again. Each row reports:

- First fill: the first fill, which also draws the other text and choice
  widgets that have no appearance yet.
- One fill: the best of seven later fills, as the pylopdf script times them.
- Check box: the best of seven fills of the first check box, alternately
  checked and cleared.

Pages import only the objects that they reach, so the object counts are
lower than pylopdf's for the same pages.

## Running

From this checkout, with pylopdf's checkout beside it:

```sh
uv run python evidence/form-fill-cost/measure.py \
  ../pylopdf/tests/assets/real_world/f1040.pdf \
  ../pylopdf/tests/assets/real_world/bill-hr815.pdf
```

## Results

`results/macos-arm64.md`: one text fill takes 0.35 to 0.38 ms and one check
box fill 0.34 to 0.35 ms, with 2,662 objects as with 6,902, where pylopdf
took 2.17 ms for a text fill with 2,626 objects and 6.4 ms with 8,646. A
fill reads the widgets of the form's other text, choice, and button fields
to find those that lack an appearance, which is most of its cost. The first
fill takes about 3 ms: it draws the 73 button states that f1040.pdf lacks.
The table is the second of three runs, which agreed within 0.1 ms for one
fill.
