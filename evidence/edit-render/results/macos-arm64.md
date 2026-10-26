# Rotate and render, macos-arm64

Written by `measure.py` on an Apple M2 Pro, 2026-10-26, pylopdf at its checkout's ef5bd814, after the render cache of #75.

- pyropdf 0.0.0 3.15.0, pylopdf 0.15.0 3.14.8
- Darwin arm64; pages rendered at 18 dpi

## Rotate and render every page

| Pages | pyropdf: render all | pyropdf: rotate and render all | Ratio | pylopdf: render all | pylopdf: rotate and render all | Ratio |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 110 | 0.20 s | 0.25 s | 1.2x | 0.24 s | 0.29 s | 1.2x |
| 440 | 0.79 s | 1.32 s | 1.7x | 0.80 s | 2.02 s | 2.5x |
| 1,760 | 3.19 s | 11.50 s | 3.6x | 3.17 s | 22.73 s | 7.2x |

## Where one rotate-and-render goes in pyropdf

The loop of the table above, through the store and the engine without Python; each column is the mean per page.

| Pages | Transaction | Page index | Bridge: store | Bridge: hayro re-read | Render | Total |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 110 | 0.004 ms | 0.045 ms | 0.026 ms | 0.190 ms | 2.038 ms | 2.302 ms |
| 440 | 0.005 ms | 0.140 ms | 0.121 ms | 0.686 ms | 2.097 ms | 3.049 ms |
| 1760 | 0.007 ms | 0.585 ms | 0.404 ms | 2.887 ms | 2.487 ms | 6.369 ms |
