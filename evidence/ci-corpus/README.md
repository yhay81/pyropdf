# Developer and CI corpus selection

The exhaustive suite runs once per revision on Python 3.11. The pre-push check,
Python 3.15 and Python 3.15t use `--quick-corpus`. For the four preservation
and consistency grids, the quick selection keeps both save modes and the following cases.

- Every edit on `pdf20-simple.pdf` (a minimal PDF 2.0 file) and
  `pdfium-links-highlights-annots.pdf` (existing widgets, links and highlights).
- Rotation on each remaining source, including tagged forms, scanned pages
  and embedded fonts.

Every case outside these four grids is selected in both runs. The floor
interpreter tests every source/edit/mode combination, so a corpus-specific
failure still gates the PR. On the other interpreters, a failure specific
to an omitted combination can go undetected.

## Local comparison

Apple M2 Pro, macOS 27.2, CPython 3.15.2, pytest 9.2.0 and qpdf 12.4.2.
The two processes ran sequentially in the same locked environment. Time
includes pytest collection, execution and writing the JUnit report.

| Measurement | Full | Quick |
| --- | ---: | ---: |
| Wall-clock seconds | 348.839 | 235.719 |
| Reported test cases | 20,929 | 12,193 |
| Cases in the four grids | 10,200 | 1,464 |
| Other cases | 10,729 | 10,729 |

The comparison checks each JUnit ID and outcome, independently matching the
source, edit and mode in all four grids. Each run had 9 optional or
platform-specific skips.

```sh
uv sync --locked --python 3.15
uv run --no-sync python evidence/ci-corpus/measure.py full target/ci-corpus/full.xml
uv run --no-sync python evidence/ci-corpus/measure.py quick target/ci-corpus/quick.xml
uv run --no-sync python evidence/ci-corpus/measure.py compare target/ci-corpus/full.xml target/ci-corpus/quick.xml
```

## CI timing

[PR #1116](https://github.com/yhay81/pyropdf/actions/runs/44993795634) took
17m16s from workflow creation to completion. The following [main run](https://github.com/yhay81/pyropdf/actions/runs/44995162312) took 20m33s. In the PR run, the Python 3.11 job ran pytest
for 7m36s, then veraPDF/Arlington for 3m16s and pdf.js for 2m45s. With the
new assignment, the regular 3.15 job runs veraPDF/Arlington and the 3.15t
job runs pdf.js. The release evidence job checks the two reader revisions
and uses the full 3.11 JUnit from the same workflow run.

The job and step timestamps are available for each run.

```sh
gh run view RUN_ID --json createdAt,updatedAt,jobs
```
