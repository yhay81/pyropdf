# Collection at live roots

Issue #324 keeps objects whose liveness is already proven outside the
candidate closure. A trailer root, and an object directly referenced by it,
cannot become unreachable unless it is an explicitly forced removal; trailer
roots themselves always stay. Both candidate discovery and subsequent live
propagation therefore avoid their unrelated retained subtrees.

The graph oracle checks exact freed-object sets, including forced removals and
cycles. The budget regression has 10,000 retained objects behind a root child
and permits only 64 KiB for collection. It refused before this change and now
returns exactly the removed page and its private object without traversing
that retained subtree. Cancellation is polled while checking incoming edges.

## Measurement

Run `uv run python evidence/reference-collection/measure.py` from the repository
root. The script repeats the flat/nested workload used for review of #186;
only its module documentation/entrypoint differs from that original script.
Both result files name the implementation and script blob. The before run
used the prebuilt reviewed #185 extension and the original copy of this script
before rebuilding the next implementation. These timings include all page-edit
work and do not isolate garbage collection.

At 64,000 pages, medians of seven repeated deletions after undo are:

| Workload | Before | After |
| --- | ---: | ---: |
| Untagged | 58.14 ms | 50.10 ms |
| Flat tagged | 171.87 ms | 154.05 ms |
| Nested tagged | 134.44 ms | 125.29 ms |

This change does not include #186's targeted structure traversal. Its whole
structure walk and the page list still grow with document size. #186 combines
this collector change with targeted planning and records that comparison
separately under `evidence/page-deletion/`.
