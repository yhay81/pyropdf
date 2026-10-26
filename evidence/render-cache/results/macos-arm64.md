# Render cache, macos-arm64

Written by `render-cache-bench` on an Apple M2 Pro, 2026-10-26, over evidence/edit-render's documents; the second of three runs, which agreed within 0.03 ms.

| Pages | Fresh cache per page | One cache per version | `render::raster` |
| ---: | ---: | ---: | ---: |
| 110 | 1.975 ms | 1.730 ms | 1.774 ms |
| 440 | 1.943 ms | 1.741 ms | 1.763 ms |
| 1760 | 1.958 ms | 1.738 ms | 1.782 ms |
