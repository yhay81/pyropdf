Base: `89cbba51f8cd29295545a0bf37a087222efe4aaa`. Current function SHA-256: `da5cdf24a2872121a60f510dd4c50dd1fb08eaaef79e9bfb9b11ad184b113f87`.

Windows-11-10.0.26300-SP0, AMD64, AMD64 Family 23 Model 113 Stepping 0, AuthenticAMD.

rustc 1.99.1 (58cf8f9c3 2026-10-13); optimized, thin LTO, 5 alternating rounds.

| Input | Glyphs | Base us | Current us |
| --- | ---: | ---: | ---: |
| empty | 0 | 0.020 | 0.086 |
| one listed glyph | 1 | 0.093 | 0.126 |
| 16 sparse listed glyphs | 16 | 0.369 | 0.944 |
| 1 full ranges | 65536 | 4091.100 | 170.100 |
| 10 full ranges | 65536 | 24978.200 | 207.400 |
| 100 full ranges | 65536 | 235911.900 | 165.300 |
| 1000 full ranges | 65536 | 2313081.000 | 175.700 |
| 10000 full ranges | 65536 | not run | 202.500 |
| 100000 full ranges | 65536 | not run | 1220.100 |

Every measured implementation returns the complete expected sorted vector.
Input construction and equality checks are outside timing; result allocation and drop are inside.
The base is deliberately not run above 1,000 full ranges; those cells are not estimates.
