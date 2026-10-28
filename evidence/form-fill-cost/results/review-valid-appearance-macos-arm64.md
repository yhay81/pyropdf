# Repeated fills with existing normal appearances

Implementation: `31955be9ac5af39b7da3dfbde7ba24eef0825c8b` (the locally prepared PR #204; run the script on a version that implements field filling). Each synthetic field has a shared normal appearance stream with the nonempty, valid content `q Q`. The first field is filled once before the seven timed fills. The normal streams were checked with pikepdf before the measurement. Reproduce with `uv run python evidence/form-fill-cost/field_scale.py` on that implementation.

The earlier review measurement used a stream whose declared length was zero; its claim that all widgets initially had normal appearances was incorrect. This measurement corrects that fixture. It still shows growth with unrelated fields. Timings include appearance settling and the `/NeedAppearances` update, and are not isolated timings for the proposed index.

- Python 3.15.0 on Darwin arm64

| Fields | Median fill |
| ---: | ---: |
| 1 | 0.036 ms |
| 10 | 0.040 ms |
| 100 | 0.149 ms |
| 1,000 | 1.391 ms |
