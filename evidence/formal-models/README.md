# Formal State-Machine Models (TLA+ / TLC)

Exhaustive state-space models of `pyropdf-store`'s core protocols
([DESIGN.md](../../DESIGN.md) sections 4.1, 5, and 7; #1255), paired with
model-driven trace validation tests in
`crates/pyropdf-store/tests/model_traces.rs`.

## Running

```bash
python3 evidence/formal-models/run.py
```

`tools/fetch_tlc.py` downloads the pinned `tla2tools.jar` (`v1.7.4`, verified
against its SHA-256 digest) into `target/tlc/tla2tools.jar` and runs TLC on all
three models.

## Models

| Model | Code under test | Verified invariants |
| --- | --- | --- |
| `HistoryAndSaveNumbers.tla` | `crates/pyropdf-store/src/version/{document,transaction}.rs` | Monotonic lifetime allocation high-water mark (`allocate()` never repeats across commits, drops, rollbacks, undo/redo, saves, or restores); exact `History` `size` and `redo_size` accounting and `fit()` eviction order; exact label alignment across `restore_history`; deterministic clean-save numbering (`SaveNumbers`). |
| `MemoInvalidation.tla` | `crates/pyropdf-store/src/memo/mod.rs`, `version/stamps.rs` | F4 read-set memoization soundness across edits, undo/redo, `Weak<Version>` expiry, and concurrent lock-free `Memo::reuse` pointer advancement; `ObjectStamps` soundness (including same-byte rewrites). |
| `SettlingProtocol.tla` | `crates/pyropdf-store/src/settle.rs`, `version/transaction.rs` | `Scope::One` / `Commit` / `Save` filtering (`FontSubset` never cleared before `Save`); zero unsettled covered structures on `Ok`; atomic savepoint rollback on error or cycle refusal at `MAX_ROUNDS`; monotonic high-water mark across rollback. |
