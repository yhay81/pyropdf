# Independent readers over every save

`tools/oracles.py` reads each document of the real-world corpus and of the
generated PDF/A and PDF/UA corpora (20 documents), and each save of each
`EDITS` entry of `tests/test_preservation.py` applied to it, in full and
incrementally (280 saves), with pymupdf 1.28.2 and pypdfium2 5.14.0. On
every page that the edit did not touch, each reader must read the same size
and extracted text from the save as from the original, and the page count
must match, plus one after `new_page`.

[output.txt](output.txt) is the run of `uv run --group oracles python
tools/oracles.py` at the commit that adds it: no difference with either
reader. The readers are development-only oracles: the `oracles` dependency
group is not installed by `uv sync` or by CI, and pymupdf's AGPL stays out of
the package's dependencies. pdf.js, which needs Node, is #285.
