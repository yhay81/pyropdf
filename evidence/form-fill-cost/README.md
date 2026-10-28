# Form fill cost

The measurement behind issue #193: setting a text field's value writes the
field and its widgets as one transaction of the store, and the appearance
settler draws only the widgets that the fill marks (DESIGN.md sections 5
and 7), so a fill costs the same as pages without fields join the document.
pylopdf cloned the whole document for each fill so that it could roll back
(DESIGN.md section 2.1).

## Workload

pylopdf's `evidence/pylopdf-baseline/form_fill_cost.py`: the first text
field of `f1040.pdf`, after the pages of `bill-hr815.pdf` join it 0, 4, and
16 times and the document is saved and opened again. Each row reports:

- First fill: the first fill, which also draws the other text and choice
  widgets that have no appearance yet.
- One fill: the best of seven later fills, as the pylopdf script times them.

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

`results/macos-arm64.md`: one fill takes 0.21 to 0.22 ms with 2,589 and
with 6,829 objects, where pylopdf took 2.17 ms with 2,626 objects and
6.4 ms with 8,646. The first fill takes about 1 ms. The first run of a
process measured 3.6 ms for its first fill and 0.68 ms for one fill of the
smallest document, the cost of a cold process; the table is the second of
two later runs, which agreed within 0.1 ms.

## Form-size scaling

`field_scale.py` varies the number of fields while keeping one page and a nonempty normal appearance for every widget. It reports the median of seven warm fills with ordinary history, no history, and an open transaction. It also measures an external field edit and the next fill separately. `results/review-valid-appearance-macos-arm64.md` records the corrected pre-index baseline; `results/indexed-fill-macos-arm64.md` records the indexed result. The earlier `review-field-scale.md` used an empty stream and is retained only as historical context. The initial index still walks the form; a structural change may require it to be built again.
