---
title: Migrating from PyMuPDF and pylopdf
description: Side-by-side migration guide from PyMuPDF (fitz/pymupdf) and archived pylopdf 0.15/0.16 to pyropdf, plus the automated codemod CLI.
---

# Migrating from PyMuPDF and pylopdf

pyropdf covers everyday PDF workflows with an MIT-licensed Rust core and one
versioned object store, through a Pythonic API of its own. Per
[DESIGN.md §10.1](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md#101-rules)
(*One way*), the `pyropdf` package contains **no compatibility aliases or `fitz`
shim**. Instead, this guide maps each common PyMuPDF and `pylopdf` 0.15 pattern
to its canonical pyropdf equivalent, and `tools/codemod.py` automates mechanical
renames at migration time.

## Automated codemod {#codemod}

Run the zero-dependency AST codemod from the repository to preview or apply
mechanical renames:

```bash
# Preview unified diffs without writing files
python3 tools/codemod.py --dry-run --diff path/to/your_app/

# Apply rewrites in place for PyMuPDF (fitz / pymupdf) callers
python3 tools/codemod.py --from pymupdf path/to/your_app/

# Apply rewrites in place for pylopdf 0.15 callers
python3 tools/codemod.py --from pylopdf path/to/your_app/

# Fail in CI if any unmigrated calls remain
python3 tools/codemod.py --check path/to/your_app/
```

The codemod preserves comments and formatting, rewrites mechanical imports,
`doc.pages` indexing/iteration, save keyword options, metadata/attachment/text
calls, and enum constants, and prints actionable `file:line: [code] message`
warnings for behavioral differences and non-mechanical APIs (`apply_redactions`,
`Story`, `get_text("dict")`, `csCMYK`, `insert_text`, `get_toc`, immutable
geometry mutations).

## Key behavioral differences {#differences}

Before porting code line by line, check four deliberate design differences:

1. **Display-space coordinates everywhere.** Every coordinate read from or
   written to a page (`page.rect`, `word.bbox`, `canvas.text`, annotations,
   links, page boxes) uses display space: points from the **top-left** of the
   visible page, `y` downward, after CropBox and `/Rotate`. You do not need to
   undo `/Rotate` or flip `y` manually.
2. **Zero-based page indexes everywhere.** `doc.pages`, `doc.outline`, and
   `doc.page_labels` all use zero-based page indexes (PyMuPDF's `get_toc()` and
   `set_toc()` are one-based).
3. **Atomic transactions and immutable values.** Grouping edits in
   `with doc.transaction():` makes them one undoable version (`doc.history`)
   and rolls everything back if an exception occurs. Geometry (`Point`, `Rect`,
   `Matrix`) and `Pixmap` are immutable values: use `r = r | p`, `r = r & o`,
   and `m = ~m` rather than mutating in place.
4. **PDF-only scope and explicit setters.** `pyropdf.open()` reads PDF files
   only (not XPS, EPUB, or HTML `Story`; convert images via
   `doc.pages.new_from_image()`). Handle setters (`page.rotation = 90`,
   `doc.form["name"].value = "Ada"`) settle dependent structures immediately;
   there is no `widget.update()` or `annot.update()`.

## Opening, probing, and saving {#open-save}

| PyMuPDF / pylopdf 0.15 | pyropdf | Notes |
| --- | --- | --- |
| `import fitz` / `import pymupdf` / `import pylopdf` | `import pyropdf` | No runtime alias module |
| `fitz.open()` | `pyropdf.Document()` | Creates an empty PDF |
| `fitz.open(stream=data, filetype="pdf")` | `pyropdf.open(data)` | Accepts path, `bytes`, `bytearray`, or `memoryview` |
| `pylopdf.peek_metadata(path)` | `pyropdf.probe(path)` | Reads header/metadata without full open |
| `doc.save(path, deflate=True, linear=True, use_objstms=True)` | `doc.save(path, compress=True, linearize=True, object_streams=True)` | Replace `garbage=1..4` with `prune=True`, `renumber=True`, `dedupe=True` |
| `doc.saveIncr()` | `doc.save(path, incremental=True)` | Keeps original bytes as an exact prefix |
| `doc.tobytes()` | `doc.to_bytes()` | Returns PDF bytes in memory |

```py title="Before (PyMuPDF)"
import fitz

with fitz.open("input.pdf") as doc:
    data = doc.tobytes(deflate=True, linear=True)
    doc.save("out.pdf", deflate=True, garbage=4)
```

```python title="After (pyropdf)"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    data = doc.to_bytes(compress=True, linearize=True)
    doc.save("out.pdf", compress=True, prune=True, renumber=True, dedupe=True)
```

## Pages, boxes, and geometry {#pages-geometry}

| PyMuPDF | pyropdf | Notes |
| --- | --- | --- |
| `len(doc)`, `doc.page_count` | `len(doc.pages)` | Live sequence view |
| `doc[i]`, `doc.load_page(i)` | `doc.pages[i]` | `Page` handles follow their page across moves/inserts |
| `page.number`, `page.parent` | `page.index`, `page.document` | Constant-time `page.index` |
| `doc.delete_page(i)`, `doc.move_page(a, b)` | `del doc.pages[i]`, `doc.pages.move(a, b)` | Referential rules keep links/outlines valid |
| `doc.insert_pdf(other)` | `doc.pages.extend(other.pages)` | Or `doc.pages.insert(index, page)` |
| `page.set_rotation(90)`, `page.remove_rotation()` | `page.rotation = 90`, `page.remove_rotation()` | Display coordinates stay consistent |
| `r.include_point(p)`, `r.intersect(o)`, `m.invert()` | `r = r | p`, `r = r & o`, `m = ~m` | Geometry values are immutable |

```py title="Before (PyMuPDF)"
import fitz

with fitz.open("input.pdf") as doc:
    page = doc.load_page(0)
    page.set_rotation(90)
    r = fitz.Rect(10, 20, 100, 80)
    r.include_point((140, 120))
    doc.move_page(0, 1)
```

```python title="After (pyropdf)"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.rotation = 90
    r = pyropdf.Rect(10, 20, 100, 80) | (140, 120)
    doc.pages.move(0, 1)
    assert page.index == 1 and r.x1 == 140
```

## Text, words, search, tables, and Markdown {#reading}

| PyMuPDF / pymupdf4llm | pyropdf | Notes |
| --- | --- | --- |
| `page.get_text()` | `page.text()` | Display reading order by default; pass `actual_text=False` to opt out of `/ActualText` |
| `page.get_text("words")` | `page.words()` | Returns frozen `Word` records (`.bbox`, `.text`, `.block`, `.line`, `.word`) |
| `page.get_text("dict")` / `"html"` / `"xml"` | `page.layout(chars=True).to_dict()` / `.to_html()` / `.to_xml()` | Also columnar `page.chars()` |
| `page.search_for(q)` | `page.search(q)` | Pass `quads=True` for rotated quads |
| `page.find_tables()` | `page.tables()` | Returns `Table` records with `.rows` and `.cells` |
| `pymupdf4llm.to_markdown(doc)` / `to_chunks` | `doc.markdown()`, `doc.to_chunks()`, `doc.to_json()` | Built in; uses PDF structure tree automatically (`use_structure="auto"`) |

```py title="Before (PyMuPDF)"
import fitz

with fitz.open("input.pdf") as doc:
    page = doc[0]
    text = page.get_text(sort=True)
    words = [(w[4], w[:4]) for w in page.get_text("words")]
    hits = page.search_for("Invoice")
```

```python title="After (pyropdf)"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    text = page.text()
    words = [(word.text, word.bbox) for word in page.words()]
    hits = page.search("Invoice")
    md = doc.markdown()
    chunks = doc.to_chunks(max_chars=1000)
```

## Rendering and Pixmaps {#rendering}

| PyMuPDF | pyropdf | Notes |
| --- | --- | --- |
| `page.get_pixmap(dpi=150, annots=False)` | `page.pixmap(dpi=150, annotations=False)` | Supports `antialias=`, `hinting=`, `glyph_cache=`, `colorspace="rgb" \| "gray"` |
| `page.get_svg_image()` | `page.svg()` | Emits text as paths (`text_as_path=False` awaits `#435`) |
| `pix.save("page.png")`, `pix.tobytes("jpeg")` | `pix.save("page.png")`, `pix.to_bytes("jpeg")` | Exposes PEP 3118 buffer via `bytes(pix)` / `memoryview(pix)` |
| `pix.invert_irect()`, `pix.tint_with(...)` | `pix.invert()`, `pix.tint(...)`, `pix.rotate(...)`, `pix.crop(...)` | Immutable: each operation returns a new `Pixmap` |

```py title="Before (PyMuPDF)"
import fitz

with fitz.open("input.pdf") as doc:
    pix = doc[0].get_pixmap(dpi=144, alpha=False, annots=True)
    pix.save("page.png")
```

```python title="After (pyropdf)"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pix = doc.pages[0].pixmap(dpi=144, alpha=False, annotations=True, antialias=True)
    pix.save("page.png")
    gray = pix.to_gray()
```

## Drawing with Canvas {#drawing}

| PyMuPDF / pylopdf 0.15 | pyropdf | Notes |
| --- | --- | --- |
| `page.new_shape()` / `draw_rect` / `draw_line` | `with page.draw() as canvas:` | Commits atomically on clean exit; rolls back on exception |
| `page.insert_text(pt, text, fontsize=14)` | `canvas.text(text, pt, font=..., size=14)` | Display coordinates (top-left origin) |
| `page.insert_textbox(rect, text)` | `canvas.textbox(rect, text, font=..., size=12)` | Returns a `TextFit` record |
| `Font(fontname="Helvetica")` / `Font(fontfile=p)` | `Font.standard("Helvetica")` / `Font.from_file(p)` | Embedded fonts are shaped with `harfrust` and subset at save |
| `page.insert_image(rect, filename=p)` | `canvas.image(rect, p)` | Accepts path, bytes, `Pixmap`, or `PageImage` |
| `page.show_pdf_page(rect, src_doc, pno)` | `canvas.page(rect, src_doc.pages[pno])` | Supports `clip=`, `rotate=`, `keep_aspect=` |

```py title="Before (PyMuPDF)"
import fitz

doc = fitz.open()
page = doc.new_page(width=400, height=200)
page.draw_rect(fitz.Rect(20, 20, 380, 180), color=(0.1, 0.3, 0.7), width=2)
page.insert_text((36, 72), "Hello from PDF", fontsize=18)
doc.save("drawn.pdf")
```

```python title="After (pyropdf)"
import pyropdf

with pyropdf.Document() as doc:
    page = doc.pages.new((400, 200))
    with page.draw() as canvas:
        canvas.rect(
            (20, 20, 380, 180),
            stroke=pyropdf.Stroke(paint=pyropdf.Color(0.1, 0.3, 0.7), width=2),
        )
        canvas.text(
            "Hello from PDF",
            (36, 72),
            font=pyropdf.Font.standard("Helvetica"),
            size=18,
        )
    doc.save("drawn.pdf")
```

## Annotations, links, and forms {#annotations-forms}

| PyMuPDF / pylopdf 0.15 | pyropdf | Notes |
| --- | --- | --- |
| `page.annots()`, `page.add_highlight_annot(quads)` | `page.annotations`, `page.annotations.add(Highlight(quads))` | Specifications build settled appearances immediately |
| `page.get_links()`, `page.insert_link(d)` | `page.links`, `page.links.add(rect, target)` | Targets are URI strings or typed destination/action records |
| `doc.set_form_field(name, value)` / `widget.update()` | `doc.form[name].value = value` | Regenerates appearances immediately; no `update()` method |
| `page.add_widget(w)` | `page.widgets.add(TextField(name, rect, ...))` | Validated frozen specifications |
| `doc.bake(annots=True, widgets=True)` | `doc.flatten(annotations=True, widgets=True)` | Flattens into page content |

```py title="Before (PyMuPDF)"
import fitz

with fitz.open("input.pdf") as doc:
    page = doc[0]
    page.add_highlight_annot(page.search_for("Invoice"))
    page.add_text_annot((36, 36), "Reviewed")
    doc.save("annotated.pdf")
```

```python title="After (pyropdf)"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.annotations.add(pyropdf.Highlight(page.search("Invoice")))
    page.annotations.add(pyropdf.Note((36, 36), contents="Reviewed"))
    page.widgets.add(pyropdf.TextField("reviewer", (36, 120, 220, 144), value="Ada"))
    doc.form["reviewer"].value = "Ada Lovelace"
    doc.save("annotated.pdf")
```

## Encryption, signatures, transactions, and undo {#security-transactions}

| PyMuPDF | pyropdf | Notes |
| --- | --- | --- |
| `doc.needs_pass`, `doc.authenticate(pw)` | `doc.needs_password`, `doc.unlock(pw)` | Raises `PasswordError` on wrong password instead of returning `0` |
| `doc.save(p, encryption=..., user_pw=...)` | `doc.save(p, encryption=Encryption(user_password=...))` | Standard AES, public-key recipients, and AES-GCM supported |
| `doc.journal_undo()`, `doc.journal_redo()` | `with doc.transaction("name"):`, `doc.history.undo()`, `doc.history.redo()` | Persist with `doc.history.save()` / `doc.history.restore(data)` |
| — | `doc.changes(incremental=True)` | Pre-save report of invalidated signatures and PDF/A or PDF/UA claims |
| — | `doc.sign(...)`, `doc.signatures` | Built-in PAdES B-B / B-T signing and signature verification |

```py title="Before (PyMuPDF)"
import fitz

with fitz.open("input.pdf") as doc:
    doc.journal_enable()
    doc.journal_start_op("rotate")
    doc[0].set_rotation(90)
    doc.journal_stop_op()
    doc.journal_undo()
    doc.save("encrypted.pdf", encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="open")
```

```python title="After (pyropdf)"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    with doc.transaction("rotate"):
        doc.pages[0].rotation = 90
    doc.history.undo()
    doc.history.redo()
    report = doc.changes(incremental=True)
    assert report.signatures == ()
    doc.save(
        "encrypted.pdf",
        encryption=pyropdf.Encryption(user_password="open", owner_password="admin"),
    )
```

## Known gaps and exclusions {#gaps}

Check these items before migrating a workflow that depends on them (full matrix
in [evidence/pymupdf-comparison/features.md](https://github.com/yhay81/pyropdf/blob/main/evidence/pymupdf-comparison/features.md)):

| Area | Current status | Tracking |
| --- | --- | --- |
| Permanent redaction (`apply_redactions`) | `RedactionMark` annotations and `doc.sanitize()` exist; permanent removal of underlying text/images/vectors awaits nested provenance | [#362](https://github.com/yhay81/pyropdf/issues/362), [#104](https://github.com/yhay81/pyropdf/issues/104) |
| Permanent clipping (`clip_to_rect`) | `page.cropbox` hides outside content without removing it | [#420](https://github.com/yhay81/pyropdf/issues/420) |
| CMYK raster pixmaps (`csCMYK`) | `page.pixmap()` outputs RGB and gray; CMYK rasterization awaits upstream engine support | [#425](https://github.com/yhay81/pyropdf/issues/425), [#433](https://github.com/yhay81/pyropdf/issues/433) |
| Selectable text in SVG (`text_as_path=False`) | `page.svg()` emits glyphs as vector paths | [#435](https://github.com/yhay81/pyropdf/issues/435) |
| Non-PDF inputs and HTML layout (`Story`, `insert_htmlbox`) | Intentionally out of scope (`pyropdf` handles PDF and image-to-PDF only) | [#355](https://github.com/yhay81/pyropdf/issues/355) |
| Input safety | See [Status and safety](status.md#safety) and [API stability](stability.md) | [#15](https://github.com/yhay81/pyropdf/issues/15) |
