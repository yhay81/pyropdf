- Apple Silicon Mac, release build; NotoSans-Regular.ttf; each stamp draws `Page {i}` on its own 200×200 page.
- "Documents written" times every document's `finish_into` or `finish_with_resources`; storing and committing are not timed.

| Stamps | Mode | Font programs | Objects | Bytes | Documents written (ms) | finalize_into (ms) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | one document per stamp (W1) | 1 | 10 | 2773 | 0.163 | – |
| 1 | shared resources (W2) | 1 | 10 | 2773 | 0.038 | 0.110 |
| 10 | one document per stamp (W1) | 10 | 100 | 28019 | 1.198 | – |
| 10 | shared resources (W2) | 1 | 46 | 7014 | 0.216 | 0.132 |
| 100 | one document per stamp (W1) | 100 | 1000 | 289552 | 10.994 | – |
| 100 | shared resources (W2) | 1 | 406 | 42379 | 2.069 | 0.167 |
| 1000 | one document per stamp (W1) | 1000 | 10000 | 2988232 | 120.517 | – |
| 1000 | shared resources (W2) | 1 | 4006 | 405080 | 20.252 | 0.167 |
