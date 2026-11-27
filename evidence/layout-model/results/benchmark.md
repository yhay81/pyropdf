# PP-DocLayout on RTen 0.28.0: Size, Latency, and Peak RSS (`macos-arm64`, 11 labeled pages)

| Model (`rten 0.28.0`, `rten_format` only) | Classes | ONNX ops | Raw `.rten` | ZIP-9 wheel | PyPI <100 MiB? | Load (4t) | Mean infer (1t / 4t) | Peak RSS (1t / 4t) |
| --- | ---: | ---: | ---: | ---: | :---: | ---: | ---: | ---: |
| PP-DocLayout-S (FP32, 480x480) | 23 | 28 | 4.73 MiB | 4.26 MiB | Yes | 4.3 ms | 68 / 22 ms | 51.5 / 57.0 MiB |
| PP-DocLayout_plus-L (FP32, 800x800) | 20 | 39 | 123.93 MiB | 112.27 MiB | **No** | 53.5 ms | 2985 / 657 ms | 543.4 / 550.1 MiB |
| PP-DocLayout_plus-L (INT8 MatMul+Conv, 800x800) | 20 | 41 | 32.83 MiB | 23.10 MiB | Yes | 18.4 ms | 988 / 370 ms | 407.4 / 414.9 MiB |
| PP-DocLayoutV3 (FP32, 800x800) | 25 | 42 | 124.67 MiB | 112.46 MiB | **No** | 43.1 ms | 2380 / 857 ms | 918.1 / 980.4 MiB |
| PP-DocLayoutV3 (INT8 Conv-only, 800x800) | 25 | 43 | 54.18 MiB | 42.91 MiB | Yes | 23.6 ms | 1423 / 484 ms | 931.5 / 942.3 MiB |
| Oracle: `pymupdf 2.2 + pymupdf-layout 2.2 + pymupdf4llm 2.2` (`onnxruntime`) | 11 | ONNX | ~50 MiB (+ORT) | ~68 MiB | Yes | 186.6 ms | — / 358 ms | — / 1224.8 MiB |
