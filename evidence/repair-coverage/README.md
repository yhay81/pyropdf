# Damaged-PDF repair coverage

Reproducible comparison of deterministic damaged-PDF recovery across `pyropdf`,
PyMuPDF, `qpdf`, and PDFium (`pypdfium2`), built for issue #1190 (DESIGN.md
sections 2.2, 4.3, and 12).

## License isolation

PyMuPDF is AGPL-licensed and is **never** added to `pyproject.toml`, `uv.lock`,
or the project's `.venv`. `run.py` installs PyMuPDF and `pypdfium2` into
disposable, isolated virtual environments under `target/repair-coverage/` and
evaluates each engine in its own subprocess via `worker.py` over the exact same
deterministic byte streams from `cases.py`. If a native C library aborts
(`SIGABRT` / `SIGSEGV`) on a damaged PDF, `run.py` attributes the crash to that
exact case and resumes at the next case.

## Reproducing

```bash
uv sync --locked
python3 evidence/repair-coverage/run.py
```

Outputs are written to `results/<platform>.md` and `results/<platform>.json`.
Pass `--reuse-oracles` to reuse existing `target/repair-coverage/*-env` venvs.

## Matrix (316 damaged PDFs)

- **Corpus-derived (285 cases)**: all 15 real-world PDFs in
  `tests/assets/real_world/*.pdf` under 19 deterministic damage categories:
  the 9 historical categories from `tests/corpus.py` (`truncate-95`,
  `truncate-60`, `junk-prefix-512`, `startxref-keyword`,
  `xref-section-removed`, `length-off-37`, `bit-flips-12`, `endobj-removed-2`,
  `bytes-inserted-late`) plus 10 additional structural damage categories
  (`truncated-eof-trailer`, `truncated-mid-trailer`, `corrupt-xref-payload`,
  `shifted-offsets-start`, `leading-garbage-2048`, `trailing-garbage-4096`,
  `trailing-stray-object`, `wrong-startxref-offset`, `corrupt-root-ref`,
  `broken-prev-chain`).
- **Targeted malformed fixtures (31 cases)**: self-contained synthetic PDFs
  isolating missing `%%EOF`, truncated trailers, corrupt classic tables and
  FlateDecode xref streams, shifted offsets, 2048-byte leading garbage,
  4096-byte trailing garbage, trailing unindexed updates, missing `startxref`,
  dangling/cyclic/corrupt `/Prev` chains, missing `/Root` with Catalog
  uncompressed or in `ObjStm`, Catalog without `/Type /Catalog`, unreferenced
  `/Type /Pages` and orphan `/Type /Page` objects with broken `/Root`,
  duplicate overlapping generations, broken later duplicates, mismatched and
  overstated-`/N` `ObjStm`s, missing `/Size`, missing `%PDF-` header, bare CR
  line endings, off-by-one xref subsections, wrong stream `/Length`, live
  object 0, compact objects without `endobj`, and empty-password encrypted PDF
  with broken xref and `/Root`.

## Hardening summary (#1190)

Measured on `darwin-arm64` ([full report](results/darwin-arm64.md)):

| Metric | pyropdf (before) | pyropdf (after) | PyMuPDF 1.28.2 | qpdf 12.4.2 | pypdfium2 5.15.2 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Open | 316/316 (100.0%) | 314/316 (99.4%) | 309/316 (97.8%) | 260/316 (82.3%) | 214/316 (67.7%) |
| Pages >= 1 | 297/316 (94.0%) | **309/316 (97.8%)** | 276/316 (87.3%) | 260/316 (82.3%) | 214/316 (67.7%) |
| Exact pages | — | **305/316 (96.5%)** | 276/316 (87.3%) | 260/316 (82.3%) | 214/316 (67.7%) |
| Text p0 | 278/316 (88.0%) | **291/316 (92.1%)** | 257/316 (81.3%) | n/a | 199/316 (63.0%) |
| Render p0 | 295/316 (93.4%) | **308/316 (97.5%)** | 276/316 (87.3%) | n/a | 212/316 (67.1%) |
| Full save | 275/316 (87.0%) | **300/316 (94.9%)** | 267/316 (84.5%) | 256/316 (81.0%) | 206/316 (65.2%) |
| Native crashes | 0 | **0** | 1 (`SIGABRT`) | 0 | 0 |

Incremental save refusal on repaired `pyropdf` documents: **256/256 (100%)**.

### What was hardened

1. **Stream validation & omission of unparseable stubs (`scan.rs`)**: stream
   objects without a matching direct `/Length` are validated with
   `hayro-syntax` within the bounded slice before the next object header;
   truncated trailing streams and unparseable bit-flipped objects are omitted
   from the rebuilt entries (while their header positions still bound the
   preceding object), eliminating 15+ full-save failures on truncated files and
   recovering all 109 intact pages of `bit-flips-12/bill-hr815.pdf`. Files with
   zero parseable objects (such as truncated `bunka-kokugo-series-019-p4.pdf`)
   now refuse cleanly at open.
2. **Trailer metadata preservation & bridge repair routing (`scan.rs`,
   `bridge/mod.rs`)**: when replacing a missing or broken `/Root`, the scan
   preserves `/Encrypt`, `/ID`, and `/Info` from trailer candidates, and the
   hayro bridge no longer bypasses the store's repaired index for unedited
   encrypted bases.
3. **Typeless catalogs & unreferenced/orphan page recovery (`scan.rs`)**:
   recovers untyped catalog dictionaries whose `/Pages` names a live
   `/Type /Pages` dictionary, and — when no intact catalog reaches a live page
   tree — synthesizes a bounded `/Type /Catalog` (and `/Type /Pages` root when
   needed) for intact unreferenced `/Type /Pages` roots or orphan `/Type /Page`
   objects (both uncompressed and inside decoded `ObjStm`s), strictly in linear
   time and charged to `Budget` (F6).
4. **Lenient `ObjStm` table recovery on repair (`objstm.rs`, `scan.rs`)**:
   during repair scans only, stops cleanly at `/First` when `/N` overstates
   the member table, recovering valid preceding pairs while keeping strict
   errors for intact cross-reference streams.
5. **Header origin past 1024-byte leading garbage (`index/mod.rs`)**: repaired
   indexes locate `%PDF-` past the 1024-byte window so `probe()` and full saves
   preserve the original PDF version (e.g. `%PDF-2.0`).

### Documented non-gaps (intentional invariants)

All 8 remaining differences against individual oracles reflect deliberate
DESIGN.md invariants rather than repair bugs:

- **`truncate-95/senate-expenditures.pdf` & `truncate-60/senate-expenditures.pdf`**:
  object 16 (the only `/Type /Page`) lies past the truncation byte offset; zero
  page objects exist in the file. `pyropdf`, `qpdf`, and `pdfium` honestly
  report 0 pages (PyMuPDF synthesizes an empty page from `/Count 1`).
- **`truncate-95/patent-us223898.pdf` & `truncate-60/pdf20-simple.pdf`**:
  `pyropdf` full-saves and reopens both cleanly (`is_repaired=False`), and
  `qpdf --show-npages` reads their pages; `qpdf --check` exits 2 only because
  an untouched page's `/Contents` array still references a truncated stream now
  represented as free/null (F3 prohibits silently rewriting untouched objects).
- **`bit-flips-12/f1040.pdf` & `bit-flips-12/pdf20-simple.pdf`**: bit flips hit
  inside object bodies while leaving the cross-reference table and object
  headers intact (`is_repaired=False`). Opening builds only the index
  (DESIGN.md §4.1), and full save refuses to emit corrupted object bytes.
  Notably, PyMuPDF aborts with `SIGABRT` (double-free/heap error) on
  `bit-flips-12/f1040.pdf`.
- **`targeted/objstm-mismatched-index`**: under an intact cross-reference
  stream (`is_repaired=False`), DESIGN.md §4.3 explicitly rejects index/number
  mismatches and reports them via `ObjectStream::mismatched()` so object
  resolution never depends on read order.
- **`targeted/object-zero-in-body`**: `pyropdf` opens, extracts text, and
  renders page 0 cleanly; full save refuses live object 0 (ISO 32000-2 §7.5.4
  free-list head) until explicitly freed, avoiding silent object deletion.
