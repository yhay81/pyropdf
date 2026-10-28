# Text and check box fills by form size

Measured on the integration of #203 onto the indexed fill (#204, #331), with `uv run python evidence/form-fill-cost/field_scale.py`. The text table repeats `indexed-fill-macos-arm64.md`; the check box table fills the first of N check boxes whose `Yes` and `Off` states hold content, alternately checked and cleared. Each result is the median of seven warm fills. A fill looks only at the widgets that the index records as lacking an appearance, so neither table grows with the number of unrelated fields.

- Python 3.15.0 on Darwin arm64

Text fields:

| Fields | Ordinary | No history | Transaction | External edit | Fill after edit |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 0.039 ms | 0.029 ms | 0.028 ms | 0.006 ms | 0.029 ms |
| 10 | 0.029 ms | 0.029 ms | 0.028 ms | 0.006 ms | 0.031 ms |
| 100 | 0.030 ms | 0.029 ms | 0.029 ms | 0.007 ms | 0.036 ms |
| 1,000 | 0.035 ms | 0.033 ms | 0.039 ms | 0.007 ms | 0.039 ms |
| 4,000 | 0.038 ms | 0.034 ms | 0.037 ms | 0.007 ms | 0.038 ms |

Check box fields:

| Fields | Ordinary | No history | Transaction | External edit | Fill after edit |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 0.016 ms | 0.016 ms | 0.015 ms | 0.007 ms | 0.017 ms |
| 10 | 0.015 ms | 0.015 ms | 0.014 ms | 0.013 ms | 0.025 ms |
| 100 | 0.016 ms | 0.015 ms | 0.014 ms | 0.007 ms | 0.017 ms |
| 1,000 | 0.018 ms | 0.017 ms | 0.016 ms | 0.007 ms | 0.019 ms |
| 4,000 | 0.021 ms | 0.018 ms | 0.017 ms | 0.008 ms | 0.021 ms |
