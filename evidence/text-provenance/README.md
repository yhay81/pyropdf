# Sources of layout chars

The measurement behind issue #98: each char of `Page.layout(chars=True)`
that a page's own content streams show names the content stream and the
index in `Page.operations` of the operation that showed it.

## Approach

hayro-interpret 0.8 shows one glyph run per text-showing operation, or two
for text that is filled and stroked, but does not say which operation
(I4, #104). After the layout's interpretation, a second one reads the
page's content as `Page.operations` joins it, with each text-showing
operation wrapped in marked content whose tag names its index, through the
same interpreter cache. Marked content changes nothing that hayro paints,
so the runs of the second interpretation are those of the page's content
in the first, in order, and its device reads the operation, if any, whose
marked content encloses each run. A run that a Form XObject shows has none,
and the annotations, which hayro shows last, have no counterpart. Every
run is checked against its counterpart by its glyph count and the bits of
its first glyph's transform; a page whose runs disagree, or whose own
marked content uses the tags, has no sources.

## Workload

- `coverage`: every page of every corpus document
  (`tests/assets/real_world`): the chars that are not synthetic spaces,
  those with a source, and the pages that have text-showing operations and
  chars but no source.
- `timing`: the median of 11 rounds of `layout(chars=True)` on the first
  five pages of five documents, each round on a newly opened document so
  that nothing is memoized. The same script ran on the base of the branch,
  387a567, which has no sources.

## Running

```sh
uv run python evidence/text-provenance/measure.py
```

## Results

`results/macos-arm64.md`:

- All 375,337 chars of the 155 pages have a source, and no page with its
  own text lost its sources to a disagreement.
- `layout(chars=True)` takes 13 to 49 percent longer, 2.3 to 7.5 ms on
  five pages. On `f1040.pdf` and `nics-background-checks-2015-11.pdf`, the
  two with the most text per page, the parts are about equal: the larger
  records and their Python conversion, reading the content as operations,
  and the second interpretation. `text`, `words`, and a layout without
  chars do not read the sources. A memoized layout (#132) pays for them
  once per version that changes what the page reads, and I4 would remove
  the second interpretation.
