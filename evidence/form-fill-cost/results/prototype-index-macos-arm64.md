# Prototype form-index update by field count

Implementation and measurement: `8cbd16c932556bff5f21f658e5a379058c3a9a32`. The synthetic form has the indicated number of indirect text widgets, all sharing an existing nonempty normal appearance. The cold measurement constructs the index once. Each warm measurement derives a new index from a version in which one field value changes; the old index and both versions remain available. Reproduce with `python3 evidence/form-fill-cost/measure_index.py`.

This measures the internal prototype only, not an end-to-end field fill. The result does not yet establish that #193 meets its acceptance criterion: the index still needs transaction and fill integration, public regressions and a complete fill measurement. Initial index construction walks the form, and real changes to shared dependencies can affect more than one field or widget.

- Python 3.15.2 on Darwin arm64
- Rust test profile (debug); median of seven batches of 100 updates

| Fields | Initial index | Warm update |
| ---: | ---: | ---: | 
| 1 | 0.180 ms | 0.0276 ms | 
| 10 | 0.424 ms | 0.0276 ms | 
| 100 | 3.420 ms | 0.0282 ms | 
| 1,000 | 40.331 ms | 0.0346 ms | 
| 4,000 | 171.453 ms | 0.0384 ms | 
