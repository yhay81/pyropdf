# Full and incremental save validation

Issue: [#27](https://github.com/yhay81/pyropdf/issues/27).
Recorded on 2026-10-25, macOS arm64, Rust 1.99.1, qpdf 12.4.2.
This is correctness evidence, not a performance measurement.

## Reproduce

From the repository root:

```sh
cargo test -p pyropdf-store save::
cargo test -p pyropdf-store qpdf_accepts -- --ignored --nocapture
```

The first command needs no external PDF tool and runs in ordinary CI. The
second requires the development-only `qpdf` executable on `PATH`; it writes
outputs under `target/save-validation/` and runs `qpdf --check` on each.
Neither command adds qpdf as a runtime dependency.

The implementation and tests are in
`crates/pyropdf-store/src/save/{mod,base,raw,tests}.rs`.
The corpus provenance is in `tests/assets/real_world/README.md`, pinned to
pylopdf `884af0dbf21973cfd434a9eca4e769de62fcc25c`. The existing bounded-save
behavior in pylopdf's `rust/src/document/saving.rs` was inspected at
`ef5bd814ca8b513cd79d9ca85ba0fcda8904ed29`; the new store writer does not port
its lopdf implementation or whole-document comparison.

## Results

- All 14 original PDFs have byte-identical no-edit incremental saves.
- All 14 full saves preserve every indexed object's value and exact value
  bytes. Object-stream members retain their decoded member bytes. Stream
  payloads, number lexemes, string representations, and dictionary order
  are not rewritten for untouched objects.
- All 14 files also round-trip after an added object and a trailer edit.
  Incremental output retains the entire original file as its prefix.
- 96 generated transaction sequences cover writes, frees, dropped
  transactions, undo, and redo. Both save modes reproduce the final state
  and retain untouched object bytes.
- Separate regressions cover a trailer-only update, multiple revisions,
  junk before the PDF header, repaired-base refusal, encryption refusal,
  free generations, sparse object numbers through `u32::MAX`, indirect
  stream lengths with embedded `endstream`/`endobj` text, cancellation,
  exact output limits, metadata-memory refusal, and a partially failing
  writer. Failed saves leave the immutable version unchanged.
- qpdf checks 56 corpus outputs (14 files, both modes, with and without
  edits) and 8 outputs from four delete/reuse revisions. There are no
  errors and no new warnings compared with the source documents. Corpus
  outputs use a file sink directly, without an intermediate output buffer.

## Existing corpus warnings

qpdf reports exit status 0 for no warnings, 3 for warnings without errors,
and 2 for errors, as documented in its
[CLI reference](https://qpdf.readthedocs.io/en/stable/cli.html#exit-status).
Three original corpus files already produce warnings. Silently modifying
their untouched objects would violate F3, and the no-edit incremental
output must be identical to the original file.

The acceptance test checks exit status and a multiset of warning messages,
excluding file paths and byte offsets. It rejects any new warning message
or increased count. No warning-suppression option is used.

| File | Source warnings | Full, no edit | Incremental, no edit | Full, edited | Incremental, edited |
| --- | ---: | ---: | ---: | ---: | ---: |
| `mhlw-doc.pdf` | 1 | 0 | 1 | 0 | 0 |
| `pdfium-links-highlights-annots.pdf` | 4 | 4 | 4 | 4 | 4 |
| `pdfium-smask-blend.pdf` | 3 | 3 | 3 | 3 | 3 |
| Other 11 corpus files | 0 | 0 | 0 | 0 | 0 |

The warnings concern the source xref stream's missing self-entry, invalid
AcroForm parent entries, and bare `XObject` tokens in the PDFium soft-mask
fixture. Full save emits a fresh xref table and retains the `/Size` bound
with a sparse free entry when needed. Existing object values remain intact.

## Scope

The writer emits classic xref tables and refuses offsets exceeding their
ten-digit representation. It does not create new compressed object streams,
renumber, deduplicate, recompress, or implement encryption. Encryption is
#28, and Python APIs and atomic file replacement are #31. The underlying
hayro-syntax limitations tracked by #15, #16, #18, #45, and #48 remain; this
change makes no new untrusted-input or performance claim.
