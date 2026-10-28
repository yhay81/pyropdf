# Form impact discovery with unrelated fields

Implementation and benchmark: `3a5e46b8f5c951500ef50fe1d923731a7af80855`. The document has one removed page, one affected widget and field, and the indicated number of unrelated indirect fields. Each measurement builds the reverse-reference index once and then runs the helper on that same version. The index build scans the document; the helper returns a conservative superset and does not read unrelated field objects, as the read-set regression verifies.

- Python 3.15.0 on Darwin arm64
- Rust test profile (debug); median of seven batches of 200 helper calls

| Unrelated fields | Initial index | Helper call |
| ---: | ---: | ---: |
| 0 | 0.091 ms | 0.0111 ms |
| 10 | 0.111 ms | 0.0116 ms |
| 100 | 0.609 ms | 0.0124 ms |
| 1,000 | 5.325 ms | 0.0151 ms |
| 4,000 | 21.888 ms | 0.0131 ms |

These are helper timings in the Rust debug test profile, not timings for complete page deletion. Index construction grows with the document. To identify a `/P` inside a referrer, the helper may inspect that entire object, including unrelated fields embedded directly in it. Issue #198 must demonstrate end-to-end deletion after adopting this helper.
