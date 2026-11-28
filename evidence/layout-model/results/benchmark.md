# PP-DocLayout on RTen 0.28.0: Size, Latency, Peak RSS, and ORT Fidelity (`macos-arm64`, 12 labeled pages)

| Model (`rten 0.28.0`, `rten_format` only) | Classes | ONNX ops | Raw `.rten` | ZIP-9 wheel | PyPI <100 MiB? | Load (4t) | Mean infer (1t / 4t) | Peak RSS (1t / 4t) |
| --- | ---: | ---: | ---: | ---: | :---: | ---: | ---: | ---: |
| PP-DocLayout-S (FP32, 480x480) | 23 | 28 | 4.73 MiB | 4.26 MiB | Yes | 4.5 ms | 74 / 26 ms | 54.8 / 57.5 MiB |
| PP-DocLayout_plus-L (FP32, 800x800) | 20 | 39 | 123.93 MiB | 112.27 MiB | **No** | 43.4 ms | 2208 / 700 ms | 542.7 / 550.5 MiB |
| PP-DocLayout_plus-L (INT8 MatMul+Conv, 800x800) | 20 | 41 | 32.83 MiB | 23.10 MiB | Yes | 21.5 ms | 1031 / 372 ms | 410.8 / 412.2 MiB |
| PP-DocLayoutV3 (FP32, 800x800) | 25 | 42 | 124.67 MiB | 112.46 MiB | **No** | 48.5 ms | 2536 / 916 ms | 971.8 / 977.3 MiB |
| PP-DocLayoutV3 (INT8 Conv-only, 800x800) | 25 | 43 | 54.18 MiB | 42.91 MiB | Yes | 25.4 ms | 1578 / 718 ms | 935.7 / 940.6 MiB |
| Oracle: `pymupdf 2.2 + pymupdf-layout 2.2 + pymupdf4llm 2.2` (`onnxruntime`) | 11 | ONNX | ~50 MiB (+ORT) | ~68 MiB | Yes | 186.6 ms | — / 345 ms | — / 1224.8 MiB |

## Identical-Tensor Numerical Fidelity vs `onnxruntime` (`mhlw-doc-p1`, score >= 0.5)

| Model | ORT boxes | RTen boxes | Matched | Max score diff | Max box diff (px) |
| --- | ---: | ---: | ---: | ---: | ---: |
| PP-DocLayout-S (FP32, 480x480) | 17 | 17 | 17 | 0.00005 | 0.005 |
| PP-DocLayout_plus-L (FP32, 800x800) | 20 | 20 | 20 | 0.00005 | 0.005 |
| PP-DocLayout_plus-L (INT8 MatMul+Conv, 800x800) | 21 | 21 | 21 | 0.01489 | 0.726 |
| PP-DocLayoutV3 (FP32, 800x800) | 20 | 20 | 20 | 0.00007 | 0.005 |
| PP-DocLayoutV3 (INT8 Conv-only, 800x800) | 18 | 19 | 17 | 0.08053 | 5.263 |
