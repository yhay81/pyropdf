# Render cache

The profile behind issue #75: rendering every page of an unedited document
at 18 dpi was slower on pyropdf than on pylopdf (4.8 s against 3.2 s at
1,760 pages in evidence/edit-render), through the same hayro 0.8.

## What it measures

`render-cache-bench` renders every page of evidence/edit-render's 110-, 440-,
and 1,760-page documents through the bridge in three ways, per page:

- hayro with a fresh `RenderCache` for each page, as `render::raster` did:
  every page parses its fonts and images again;
- hayro with one `RenderCache` for every page of the version, as pylopdf
  keeps one per snapshot;
- `render::raster` now, which keeps one cache per thread for the bridge it
  rendered last.

```sh
cargo run --release --manifest-path evidence/render-cache/Cargo.toml -- pages-110.pdf pages-440.pdf pages-1760.pdf
```

The documents are those that evidence/edit-render/side.py builds.

## Results

`results/macos-arm64.md`: a fresh cache per page costs about 0.23 ms more
per page than a shared one, 1.96 ms against 1.73 ms, at every size; the
cached `render::raster` takes 1.77 ms. Through Python, `page.pixmap(dpi=18)`
took 2.2, 2.5, and 2.7 ms per page before and takes 1.8 ms at every size
after, so rendering all 1,760 pages takes 3.19 s against pylopdf's 3.17 s
(evidence/edit-render/results/macos-arm64.md).

hayro's cache holds `Rc`s, so it is not `Send`: each thread keeps its own,
together with the bridge it borrows from, until it renders another version.
