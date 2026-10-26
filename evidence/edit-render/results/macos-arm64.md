# Rotate and render, macos-arm64

Written by `measure.py` on an Apple M2 Pro, 2026-10-25, pylopdf at its checkout's ef5bd814.

- pyropdf 0.0.0 3.15.0, pylopdf 0.15.0 3.14.8
- Darwin arm64; pages rendered at 18 dpi

## Rotate and render every page

| Pages | pyropdf: render all | pyropdf: rotate and render all | Ratio | pylopdf: render all | pylopdf: rotate and render all | Ratio |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 110 | 0.24 s | 0.27 s | 1.1x | 0.29 s | 0.29 s | 1.0x |
| 440 | 1.08 s | 1.89 s | 1.8x | 0.84 s | 2.06 s | 2.4x |
| 1,760 | 4.81 s | 11.08 s | 2.3x | 3.22 s | 22.78 s | 7.1x |

## Where one rotate-and-render goes in pyropdf

The loop of the table above, through the store and the engine without Python; each column is the mean per page.

| Pages | Transaction | Page index | Bridge: store | Bridge: hayro re-read | Render | Total |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 110 | 0.004 ms | 0.046 ms | 0.027 ms | 0.191 ms | 2.166 ms | 2.434 ms |
| 440 | 0.005 ms | 0.134 ms | 0.122 ms | 0.641 ms | 1.995 ms | 2.897 ms |
| 1760 | 0.007 ms | 0.563 ms | 0.411 ms | 2.689 ms | 2.093 ms | 5.763 ms |
