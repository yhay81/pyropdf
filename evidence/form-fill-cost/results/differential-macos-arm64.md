# Differential check of indexed field filling

Compared the reviewed pre-index implementation `31955be9ac5af39b7da3dfbde7ba24eef0825c8b` with the indexed implementation `8f151f9cb7dd976f75295e0891bcde16c7003ddb` on Python 3.15.0, macOS arm64 and pikepdf 10.17.0. The two JSON outputs were identical: 144 generated documents, seven observations per document, and no differences among 1,008 observations.

The fixtures vary an input `/NeedAppearances` flag, indirect shared, missing and empty normal appearances, and field values including null, arrays and non-WinAnsi text. Each sequence fills a field, edits another field, fills again, undoes, redoes, rolls back an explicit transaction, and fills once more. Each observation records field names, types and values, the resulting `/NeedAppearances`, the SHA-256 of decoded normal appearance streams, and any exception class and limit code. This comparison covers the generated text-field fixtures; it does not prove equivalence for every PDF or cover button filling.

To reproduce, copy `differential.py` to a temporary location, run it from the root of a checkout of each implementation with `uv run python /path/to/differential.py > output.json`, and compare the two JSON files. The script loads `tests/test_acroform.py` from each checkout for its raw-PDF fixture.
