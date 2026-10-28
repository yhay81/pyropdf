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

`results/macos-arm64.md`: one text fill takes 0.04 ms and one check box
fill 0.02 ms with 2,663 objects as with 6,903, where pylopdf took 2.17 ms
for a text fill with 2,626 objects and 6.4 ms with 8,646. The first fill
takes about 3.5 ms: it builds the form index and draws the 73 button
states that f1040.pdf lacks. The table is the last of three runs, which
agreed within 0.01 ms for one fill.

## Form-size scaling

`field_scale.py` varies the number of fields while keeping one page and a nonempty normal appearance for every widget. It reports the median of seven warm fills with ordinary history, no history, and an open transaction. It also measures an external field edit and the next fill separately. `results/review-valid-appearance-macos-arm64.md` records the corrected pre-index baseline; `results/indexed-fill-macos-arm64.md` records the indexed result. The earlier `review-field-scale.md` used an empty stream and is retained only as historical context. The initial index still walks the form; a structural change may require it to be built again.

`results/button-scale-macos-arm64.md` repeats the text measurement and adds check box fills, as the review of #203 asked: both stay flat from 1 to 4,000 fields, because a fill looks only at the widgets that the index records as lacking an appearance.

`results/differential-macos-arm64.md` compares 144 generated forms and 1,008 observations against the reviewed pre-index fill implementation, including external edits, undo, redo and transaction rollback.
