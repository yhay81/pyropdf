# Original triangle fixtures

These files were written specifically for pyropdf and may be redistributed under the repository's MIT license. No third-party model, mesh or document was used. Each model has a single triangle with vertices (0, 0, 0), (3, 0, 0) and (0, 4, 0).

* `triangle.step` is a hand-written Part 21 exchange structure. The 44 entities define one STEP AP242 edition 2 product and one planar face in millimetres, with three straight boundary edges. The edition 2 schema identifier is `{ 1 0 10303 442 3 1 4 }`. [MBx-IF EXPRESS schema registry](https://www.mbx-if.org/home/mbx/resources/express-schemas/)
* `triangle.gltf` declares one glTF 2.0 scene, node, mesh, and non-indexed triangle. `triangle.bin` contains the three vertex positions as nine little-endian IEEE 754 binary32 values in metres. `triangle-inline.gltf` embeds the same 36 bytes as a data URI. [Khronos glTF specification](https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html)

The original buffer is reproducible with Python's `struct.pack("<9f", 0, 0, 0, 3, 0, 0, 0, 4, 0)`. Neither file format requires a renderer or 3D PDF viewer for these fixture checks.

## Independent verification

From this directory, install isolated verification tools and run the script.

```sh
npm install --prefix /tmp/pyropdf-model-tools --ignore-scripts --no-audit --no-fund occt-import-js@0.0.23 gltf-validator@2.0.0-dev.3.10
NODE_PATH=/tmp/pyropdf-model-tools/node_modules node verify.cjs .
```

[OpenCascade's importer binding](https://github.com/kovacsv/occt-import-js) reads one B-rep face, one triangulated mesh, and three vertices with the exact coordinates above. [Khronos's validator](https://github.com/KhronosGroup/glTF-Validator) finds three vertices, one triangle and one draw call, with zero errors, warnings, informational reports, or hints. Only the external glTF requests `triangle.bin`.

The independent PDF producer and pyropdf's writer are both exercised by
`tests/test_rich_models.py`. To check the two original PDFs, the independently
authored outputs and their full and incremental saves against Arlington 1.30.4,
run this command from the repository root.

```sh
uv run python tests/fixtures/rich-models/check_pdf.py /path/to/arlington-pdf-model-checker
```

Both the ISO/TS 24064 and the ISO/TS 32007 extension profiles give four passes
each. The older 1.28.2 Arlington CLI throws a JavaScript evaluation error on
these same controls. The base test suite uses pikepdf to check the declarations,
file identities, MIME names, view and unchanged payload bytes independently.
