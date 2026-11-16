# Validation on 2026-11-16

## Released package

Copied the README's Python block unchanged to `quickstart.py` in a
temporary directory without an input PDF. Ran it outside the checkout:

```bash
uv run --isolated --no-project --with pyropdf==0.3.1 python quickstart.py
```

The isolated environment installed one package, pyropdf. Its reported
version was 0.3.1. Standard output was:

```text
Invoice total: $42
After undo: 0
```

`reviewed.png` was a 17,645-byte PNG, visually checked at 720 by 480 pixels:
the invoice text was readable and only `$42` was highlighted. Reopening
`reviewed.pdf` with the released package returned the same text and one
annotation of type `AnnotationType.HIGHLIGHT`.

`tests/test_release.py::test_the_readme_example_runs` now runs the actual
README block in an empty directory and checks those observable results,
including that the incremental output starts with the original PDF bytes.
It replaces the earlier test's supplied-input and old-output assumptions.

## Discovery surfaces

The repository description and all nine topics were read back through
GitHub's API; see [`metadata-after.json`](metadata-after.json). The existing
homepage was preserved. Before-state metadata and aggregate traffic are in
[`baseline-2026-11-16.json`](baseline-2026-11-16.json).

The docs homepage, recipes, API reference, choosing guide, Japanese docs,
reviewed demo PDF, and both README image URLs returned HTTP 200 when checked
with `curl --head --location --fail`. The documentation host returned 403
to Python's default urllib request, so that client was not used as evidence
of a broken link.

## Capture failure behavior

Ran the capture script with a simulated failed `gh` request: it raised an
error and created no snapshot. Ran it with an existing destination file:
it refused to overwrite it and retained the original bytes. This checks
that missing data does not silently become a zero count or replace prior
evidence. These checks use mocked subprocess responses, not new traffic.

Full repository checks and current-head CI are recorded on the linked PR.
No performance effect or increase in stars is claimed by this validation.
