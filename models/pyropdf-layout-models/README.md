# pyropdf-layout-models

A data-only package containing the Apache-2.0 `PP-DocLayout_plus-L` (dynamic INT8) document
layout detection model converted to `.rten` format for
[pyropdf](https://github.com/yhay81/pyropdf)'s optional offline layout engine (`pyropdf[layout]`).

The model runs locally through pyropdf's existing pure-Rust `rten` backend — requiring no
ONNX Runtime, no PyTorch, no system binaries, and no network access at runtime.

Install via the optional extra:

```bash
pip install "pyropdf[layout]"
```

## Bundled artifacts

| File | Purpose | SHA-256 |
| --- | --- | --- |
| `PP-DocLayout_plus-L_int8.rten` | 20-class 800×800 document layout detector (INT8 `MatMul+Conv`) | `9f41f26cfae974913846ebe251a597da5a55f75cf426fe4f160320fed355a078` |
| `model.json` | Input size, RGB `0..1` normalization, 20-class label table, provenance | `512dddf12af658a66835475a4b59d24ece0c7d19c3423bafcf54feaa7c37fa6b` |
| `SHA256SUMS` | Artifact integrity digests | — |

## Reproducible conversion

```bash
uv run --with onnx==1.23.2 --with onnxruntime==1.32.0 --with rten-convert==0.22.0 \
  python models/pyropdf-layout-models/scripts/convert_model.py \
  --work-dir /tmp/layout-convert \
  --out-dir models/pyropdf-layout-models/src/pyropdf_layout_models \
  --verify
uv build models/pyropdf-layout-models --out-dir dist
```

## Source & License

- Source: [`PaddlePaddle/PP-DocLayout_plus-L_onnx@feb74619326f`](https://huggingface.co/PaddlePaddle/PP-DocLayout_plus-L_onnx/tree/feb74619326f634e0e883218598096a3733ad9f7)
- License: Apache-2.0 (see `LICENSE` and `NOTICE`)
