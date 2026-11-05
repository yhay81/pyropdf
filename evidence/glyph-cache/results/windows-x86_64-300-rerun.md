## Time

Every page of each document in order, best of 9 runs, in milliseconds.

### 300 dpi

| Document | Pages | Off | On | Off / on | Off, median | On, median | `render::raster` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 | 110 | 7468.5 | 5521.5 | 1.35 | 9614.2 | 8479.5 | 6033.1 |
| bunka-kokugo-series-019-p4 | 1 | 126.0 | 108.4 | 1.16 | 147.4 | 181.5 | 109.7 |
| f1040 | 2 | 95.6 | 92.5 | 1.03 | 138.3 | 188.7 | 91.8 |
| mhlw-doc | 2 | 60.1 | 59.5 | 1.01 | 90.0 | 107.8 | 55.0 |
| nics-background-checks-2015-11 | 1 | 51.0 | 52.6 | 0.97 | 78.8 | 67.5 | 57.6 |
| patent-us223898 | 4 | 263.1 | 242.5 | 1.08 | 347.2 | 311.6 | 253.9 |
| pdf20-simple | 1 | 8.5 | 8.2 | 1.04 | 11.9 | 12.5 | 8.3 |
| pdfium-jpx-lzw | 1 | 38.4 | 37.0 | 1.04 | 57.2 | 50.7 | 38.8 |
| pdfium-links-highlights-annots | 1 | 17.0 | 15.8 | 1.08 | 23.1 | 23.0 | 16.4 |
| pdfium-ocg-invisible | 1 | 1.0 | 1.1 | 0.90 | 1.3 | 1.4 | 1.0 |
| pdfium-smask-blend | 1 | 4.0 | 4.1 | 0.98 | 4.7 | 4.7 | 4.0 |
| pdfium-type3 | 1 | 0.9 | 0.9 | 0.96 | 1.2 | 1.2 | 0.9 |
| senate-expenditures | 1 | 41.6 | 37.3 | 1.12 | 57.2 | 54.5 | 42.5 |
| usrguide | 27 | 1107.0 | 938.7 | 1.18 | 1622.4 | 1482.6 | 886.9 |
| wdl6812-manuscript | 2 | 92.4 | 104.7 | 0.88 | 154.0 | 185.2 | 94.2 |
| **Corpus** | 156 | **9375.2** | **7224.8** | **1.30** | 12348.9 | 11152.3 | 7694.2 |

## Memory

What every page of a document allocates at its peak, and what one `RenderCache` still holds after its last page, in MiB.

### 300 dpi

| Document | Peak, off | Peak, on | Held, off | Held, on |
| --- | ---: | ---: | ---: | ---: |
| bill-hr815 | 56.2 | 60.6 | 1.4 | 4.8 |
| bunka-kokugo-series-019-p4 | 67.1 | 67.1 | 0.0 | 0.0 |
| f1040 | 39.1 | 41.5 | 0.6 | 2.6 |
| mhlw-doc | 37.2 | 39.4 | 2.4 | 4.9 |
| nics-background-checks-2015-11 | 46.5 | 50.2 | 0.7 | 1.1 |
| patent-us223898 | 61.0 | 61.0 | 0.2 | 0.2 |
| pdf20-simple | 16.3 | 16.4 | 0.0 | 0.1 |
| pdfium-jpx-lzw | 34.5 | 34.5 | 0.0 | 0.0 |
| pdfium-links-highlights-annots | 32.5 | 32.7 | 0.0 | 0.2 |
| pdfium-ocg-invisible | 2.7 | 2.7 | 0.0 | 0.0 |
| pdfium-smask-blend | 3.5 | 3.5 | 0.0 | 0.0 |
| pdfium-type3 | 2.1 | 2.1 | 0.0 | 0.0 |
| senate-expenditures | 35.4 | 37.5 | 0.5 | 0.7 |
| usrguide | 38.4 | 43.5 | 3.1 | 6.7 |
| wdl6812-manuscript | 22.1 | 22.1 | 0.0 | 0.0 |
| **Corpus, held** | | | **8.9** | **21.4** |

## Pixels

Each page drawn both ways, and drawn without the cache at 4 times the resolution and averaged over each 4 by 4 block as the reference. A pixel is far when a channel differs by more than 16. The errors are mean absolute channel errors against the reference, over all pixels and over the pixels that the cache changes.

## Distinct sizes

One page of 1000 glyphs, `W` of Helvetica each at a size of its own between 20 and 120 points, at 72 dpi.

| | Off | On |
| --- | ---: | ---: |
| Time, best of 9 (ms) | 15.7 | 42.8 |
| Peak (MiB) | 6.3 | 19.3 |
| Held after the page (MiB) | 0.0 | 14.9 |

## Positions

One page of 2,000 glyphs `e` of Helvetica at 10 points, at 72 dpi, where a point is a pixel. Each column steps a further quarter of a pixel. Aligned: every origin on a whole pixel vertically and a quarter of a pixel horizontally, where the cache places glyphs. Offset: the same, an eighth of a pixel right and half a pixel up.

| Page | Changed pixels | Far, on against off | Error, off | Error, on | Changed error, off | Changed error, on |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| aligned | 11.450% | 3.198% | 1.576 | 0.235 | 13.69 | 1.98 |
| offset | 14.029% | 11.141% | 1.688 | 9.101 | 11.84 | 64.69 |
