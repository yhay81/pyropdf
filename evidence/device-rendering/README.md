# Output-device controls

The fixtures create 32 × 16 point pages containing two adjacent patches. At 72 dpi
each patch covers 16 × 16 pixels. The tests compare interior samples or the
specified row of a thin stroke. They also reopen full and incremental saves in
pikepdf, checking the actual graphics-state, colour-space and halftone objects,
their threshold bytes and the unchanged content stream.

Run the comparisons from the repository root with Poppler and Ghostscript on
`PATH`. The resource and pikepdf checks need neither executable.

```sh
uv run pytest -q tests/test_device_rendering.py tests/test_output_device_samples.py
```

The following measurements used hayro `394e4a0`, Poppler 26.11.0, Ghostscript
10.08.0 and pikepdf 10.18.2. A process or spot plate uses white for no ink and
black for full ink. Composite CMYK tuples give the ink coverage directly.
Integer colour components in this table have the range 0–255.

| Control | Independent result | pyropdf result |
| --- | --- | --- |
| Half-strength DeviceRGB and the same numeric components in linear CalRGB | Poppler RGB 128 and 188. Ghostscript RGB 127 and (187, 188, 187) | RGB 128 and 188 |
| A named Varnish tint from zero to one, with magenta as its alternate | Poppler renders white and (236, 0, 140). Ghostscript creates a Varnish plate with samples 255 and 0. All four process plates stay white. | The RGB alternate matches Poppler. The pixmap API has no named-ink output. |
| Magenta over a solid cyan backdrop, nonstroking overprint enabled | Ghostscript records CMYK (0, 255, 0, 0) in mode 0 and (255, 255, 0, 0) in mode 1. | Both RGB patches are (236, 0, 140). |
| Absolute and relative rendering intents on the same calibrated colour | Ghostscript's CMYK output is (40, 156, 0, 0) for absolute and (54, 177, 0, 0) for relative. Its RGB device gives the same RGB for both. | RGB (255, 90, 255) for both. The pinned interpreter ignores the content operator `ri`. |
| Single and component transfer with `TR2` taking precedence over `TR` | Poppler maps RGB (64, 64, 64) to (191, 191, 191) for inversion, to (191, 64, 191) for selective inversion, and back to (64, 64, 64) for the default. | Exactly the same three results. A four-channel device must separately exercise the black transfer function. |
| 6 lpi horizontal line screen at half intensity | Ghostscript's bilevel device records alternating bands of solid rows. The default screen yields a different dot pattern. Poppler's continuous-tone sample is RGB 128 throughout. | Continuous-tone RGB 128 throughout. |
| Thin vertical line with stroke adjustment off/on | Ghostscript's 4-bit antialiasing changes the midline at x=3 from two coverage samples of 187 to one sample of 187. Both lines remain visible. | Both lines are visible. Their relative coverage is unchanged. The pinned interpreter ignores flatness and its ExtGState does not implement automatic stroke adjustment. |
| Custom black generation and undercolor removal, neutral RGB 0.5 | Ghostscript's classic PostScript conversion maps constant-zero functions to (0.5, 0.5, 0.5, 0) and identity functions to (0, 0, 0, 0.5). Its PDF TIFF device applies its defaults to both pairs, yielding (127, 127, 127, 0) with fast colour and (130, 111, 112, 20) with ICC. | Both custom functions survive a save. RGB preview stays 128. Page recolouring uses its existing full-black conversion for both. |

The RGB transfer, alternate, calibrated and embedded ICC controls use Poppler
directly. The print controls require distinct output devices: `tiffsep`,
`tiff32nc`, `pbmraw` and `pgmraw` with four bits of graphics antialiasing.
These outputs can differ under device profiles, screening and antialiasing
policies. Assertions use exact values for digital transfer and plate
isolation, modest channel tolerances for ICC conversion, or a controlled
change of coverage or dot pattern for device-dependent output.
[Ghostscript device documentation](https://ghostscript.readthedocs.io/en/gs10.08.0/Devices.html)
and [colour-management options](https://ghostscript.readthedocs.io/en/gs10.08.0/Use.html).

## Remaining work

- The plate and print-device proposal is [#1001](https://github.com/yhay81/pyropdf/issues/1001).
  It links the existing upstream proposals for native CMYK output and
  forwarding colourants and overprint state. Agree on and verify actual plate
  compositing, ICC intent, black transfer, screened output and scan controls
  on their target devices.
- Active black generation and undercolor removal in the content/image rewriting
  operations are tracked by [#1002](https://github.com/yhay81/pyropdf/issues/1002).
  It covers graphics state at painting time, bounded evaluation and inherited
  state in forms and images. The existing stored resources and their edits
  are exercised in both save modes.

The ISO 32000-2 corrections distinguish the default screen from component-specific
halftones and qualify the transfer-function and flatness requirements.
[Clause 10 rendering corrections](https://pdf-issues.pdfa.org/32000-2-2020/clause10.html)
and [clause 8 graphics corrections](https://pdf-issues.pdfa.org/32000-2-2020/clause08.html).
