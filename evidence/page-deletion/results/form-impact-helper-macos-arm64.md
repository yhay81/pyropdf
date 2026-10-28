# Form impact discovery with unrelated fields

Implementation and benchmark: `b65c0884e031de58a29524d69fbda123a609de05`. The document has one removed page, one affected widget and field, and the indicated number of unrelated fields. Each measurement builds the reverse-reference index once and then runs the helper on that same version. The index build scans the document; the helper returns a conservative superset and does not read unrelated field objects, as the read-set regression verifies.

- Python 3.15.0 on Darwin arm64
- Rust test profile (debug); median of seven batches of 200 helper calls

| Unrelated fields | Initial index | Helper call |
| ---: | ---: | ---: |
| 0 | 0.087 ms | 0.0088 ms |
| 10 | 0.113 ms | 0.0094 ms |
| 100 | 0.646 ms | 0.0100 ms |
| 1,000 | 5.550 ms | 0.0110 ms |
| 4,000 | 22.078 ms | 0.0108 ms |

These are helper timings in the Rust debug test profile, not timings for complete page deletion. Index construction grows with the document. Issue #198 must demonstrate end-to-end deletion after adopting this helper.
