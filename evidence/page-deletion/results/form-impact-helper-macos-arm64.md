# Form impact discovery with unrelated fields

Implementation and benchmark: `8db96034bf3cc7f7ad470dad25fc3958e4971517`. The document has one removed page, one affected widget and field, and the indicated number of unrelated fields. Each measurement builds the reverse-reference index once and then runs the helper on that same version. The index build scans the document; the helper returns a conservative superset and does not read unrelated field objects, as the read-set regression verifies.

- Python 3.15.0 on Darwin arm64
- Rust test profile (debug); median of seven batches of 200 helper calls

| Unrelated fields | Initial index | Helper call |
| ---: | ---: | ---: |
| 0 | 0.741 ms | 0.0169 ms |
| 10 | 0.221 ms | 0.0128 ms |
| 100 | 0.869 ms | 0.0117 ms |
| 1,000 | 6.057 ms | 0.0109 ms |
| 4,000 | 24.647 ms | 0.0101 ms |

These are helper timings in the Rust debug test profile, not timings for complete page deletion. Index construction grows with the document; the first rows also include ordinary process and allocator warm-up variability. Issue #198 must demonstrate end-to-end deletion after adopting this helper.
