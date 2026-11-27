---
title: Reading content
description: Text, words, characters, layout, search, tables, Markdown, JSON, retrieval chunks, tagged reading order, and offline OCR.
---

# Reading content

Every reader works on the current version of the document, so it sees
edits made a moment before without a save.

## Text and words

`page.text()` returns the text in reading order, with spaces and line ends
where the layout puts them. `page.words()` returns `Word` items with their
rectangles, and `page.chars()` the characters with their glyph boxes.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    print(page.text())
    print([word.text for word in page.words(clip=(0, 0, 300, 200))])
    print(doc.text())  # every page, joined in order
```

`clip` limits a reader to a rectangle in display coordinates, whose origin
is the page's top-left corner. By default, marked-content `/ActualText`
replaces the text of its enclosed glyphs; pass `actual_text=False` to
`page.text()`, `page.words()`, `page.layout()`, `page.chars()`,
`page.search()`, or `annotation.text()` to inspect the underlying glyph
Unicode values directly.

## Layout

`page.layout()` groups the text into blocks, lines, and spans, with fonts,
sizes, and colours.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    layout = doc.pages[0].layout()
    for block in layout.blocks:
        for line in block.lines:
            print([span.text for span in line.spans])
```

## Search

`page.search` returns the rectangles of each hit, or quads with
`quads=True`, ready for a highlight:

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    hits = page.search("total")
    if hits:
        page.annotations.add(pyropdf.Highlight(hits))
```

## Tables and Markdown

`page.tables()` finds tables from their ruling lines, or from text
alignment with `strategy="text"`. `page.markdown()` and `doc.markdown()`
turn pages into Markdown, tables included.

When a document is tagged and passes trustworthiness checks
(`/MarkInfo << /Marked true >>`, no `/Suspects`, valid tree, and sufficient
coverage of non-artifact page text), `markdown()` uses the PDF structure
tree by default (`use_structure="auto"`): headings (`H1`–`H6`, `H`, `Title`),
lists (`L`/`LI`/`Lbl`/`LBody`), tables (`Table`/`THead`/`TBody`/`TFoot`/`TR`/`TH`/`TD`),
block quotes, code blocks, figures with `/Alt` descriptions, `/ActualText`
replacements, and logical reading order come from the tree, while `Artifact`
running headers and footers are skipped. Custom roles resolve through
`/RoleMap` and PDF 2.0 namespaces. Pass `expand_abbreviations=True` to use
`/E` expansions, `use_structure="always"` to read the structure tree without
trustworthiness checks, or `use_structure="never"` to force geometric layout.
Untagged pages and pages with unresolved Form XObject marked content fall back
to geometric extraction automatically in `"auto"` mode.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for table in doc.pages[0].tables():
        print(table.markdown())
    print(doc.markdown())
    print(doc.markdown(use_structure="never"))  # force geometric layout
```

## JSON and retrieval chunks

`doc.to_json()` exports the same page elements—headings, paragraphs, list
items, tables, images, block quotes, and code blocks—as JSON, each with its
page index and bounding box in display space, plus optional `alt_text`,
inherited BCP 47 `language` when extracted from a structure tree, and `image`
when image export is enabled. `doc.to_chunks()` groups consecutive elements
into Markdown retrieval chunks within a character budget (`max_chars`, 2000 by
default), starting a new chunk at each heading so every `Chunk` keeps its
enclosing `section`, `pages`, dominant `language`, and `elements` with
provenance. Tables, images, headings, and code blocks are never split
(`chunk.oversized` marks one that exceeds the budget).

All three document exports—`doc.markdown()`, `doc.to_json()`, and
`doc.to_chunks()`—share composable retrieval controls (and `page.markdown()`
accepts the single-page controls `margins`, `images`, and `max_graphics`):

- **`margins`**: excludes headers, footers, sidebars, tables, and images
  outside the display-space body region on both upright and rotated pages. Pass
  a single number for all four sides, `(horizontal, vertical)`, or
  `(left, top, right, bottom)`.
- **`ignore_headers_footers=True`**: deterministically suppresses repeated
  running headers and footers in the top and bottom bands across multi-page
  selections—normalizing sequential Arabic and Roman page numbers such as
  `Page 1 of 10`, `1 / 10`, `- 1 -`, and `Page iv of xii`—while preserving
  unique first-page titles and body lines.
- **`images`**: defaults to `None` (pure in-memory, zero filesystem writes, no
  image decoding). Pass a directory path to stage and write bounded PNG or JPEG
  files (`page-{page}-image-{index}.{ext}`) atomically after the export
  succeeds and emit Markdown image links, or pass a callback receiving each
  `ExportedImage` (`page`, `index`, `bbox`, `filename`, `media_type`, `data`,
  `width`, `height`, `alt_text`) and returning a link destination (or `None` to
  omit the link) without writing to disk.
- **`max_graphics`**: bounds graphic elements (paths, segments, clips, and
  images) inspected per page (65,536 by default, raising `LimitError` with code
  `content_element_count` on vector-heavy CAD or map pages without stalling;
  pass `None` for trusted input).

```python
import json
from pathlib import Path
import pyropdf

with pyropdf.open("input.pdf") as doc:
    md = doc.markdown(
        margins=(36, 54, 36, 54),
        ignore_headers_footers=True,
        images=Path("exported_images"),
        max_graphics=10_000,
    )
    data = json.loads(
        doc.to_json(
            ignore_headers_footers=True,
            images=lambda img: f"https://cdn.example/{img.filename}",
        )
    )
    print(len(data["pages"]), [elem["kind"] for elem in data["pages"][0]["elements"]])
    for chunk in doc.to_chunks(
        max_chars=500,
        margins=(36, 54),
        ignore_headers_footers=True,
        expand_abbreviations=True,
    ):
        print(chunk.index, chunk.pages, chunk.section, chunk.language, chunk.text)
```

## Tagged reading order

For tagged PDFs, `doc.structure.reading_order()` extracts logical text in the
document's structure-tree order across pages, reading page marked-content
sequences, omitting `Artifact` elements, and returning `()` for untagged
documents. Each `StructureText` keeps its `element`, content reference `item`,
and `text` (`None` when text cannot be resolved, such as a Form XObject without
replacement text). Element `actual_text` and `alt` (on `Figure`, `Formula`, and
`Form`) replace painted glyphs, and `expand_abbreviations=True` uses `/E`
expansions. Because `page.draw()` draws into a Form XObject, pass
`actual_text`, `alt`, or `expanded` to `canvas.tag()` when tagging drawn
content for logical extraction. See
[Structure and accessibility](editing.md#structure-and-accessibility) for
authoring structure trees.

```python
import pyropdf
from pyropdf import TagRole

with pyropdf.Document() as doc:
    page = doc.pages.new((360, 200))
    with page.draw() as canvas:
        with canvas.tag(TagRole.HEADING_1, actual_text="Summary"):
            canvas.text("Summary", (36, 40), size=18)
        with canvas.tag(TagRole.SPAN, actual_text="PDF", expanded="Portable Document Format"):
            canvas.text("PDF", (36, 70), size=12)
        with canvas.tag(TagRole.FIGURE, alt="Revenue chart"):
            canvas.rect((36, 90, 140, 140), fill=pyropdf.Color(0.2, 0.4, 0.8))
        with canvas.tag(TagRole.ARTIFACT):
            canvas.text("Page 1", (36, 180), size=9)
    for item in doc.structure.reading_order(expand_abbreviations=True):
        print(item.element.standard_role, item.text)
```

## Content items

`page.content()` returns what the page paints, glyph runs, paths, images,
and clips, with the marked content that encloses them. Each extracted `Color`
carries its stored PDF `colorspace` (such as `"DeviceGray"`, `"DeviceRGB"`,
`"DeviceCMYK"`, `"ICCBased"`, `"CalGray"`, `"CalRGB"`, `"Lab"`, `"Indexed"`,
`"Separation"`, or `"DeviceN"`) and original `components` alongside its sRGB
`red`, `green`, `blue`, and `alpha` values (equality and hashing compare only
sRGB and alpha). Each `TextRun` exposes its `writing_mode` (`WritingMode.HORIZONTAL`
or `WritingMode.VERTICAL`), and its `FontRef` reports PDF object identity
(`xref`, `subtype`, `name`, and `is_embedded`). `page.operations`
reads and writes the content stream as operators.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    print(len(page.content()), "items")
    print(page.operations[:3])
```

`page.paths()` returns the same paths as `Paths` columns, one row per path,
with the segments of every path in columns of their own, and builds no
Python object per path or segment, so that a drawing of a few hundred
thousand lines reads in about the time that rendering the page takes.
`column()` returns a column as a read-only `memoryview` that NumPy reads
without a copy, and pyarrow, polars, and DuckDB import the whole table
through the Arrow PyCapsule interface. Rows are `Path` records on demand.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    paths = doc.pages[0].paths()
    print(len(paths), "paths of", len(paths.column("verb")), "segments")
    # numpy.asarray(paths.column("stroke_red")) reads the same buffer.
    red = paths.column("stroke_red")
    print([path.bbox for path, value in zip(paths, red) if value == 1])
```

`page.chars()` (and `page.layout(chars=True).chars`) exposes characters in the
same columnar `Chars` form without building a Python object per glyph.
`chars.names` lists the columns: glyph boxes (`x0`, `y0`, `x1`, `y1`), baseline
origins (`origin_x`, `origin_y`), UTF-8 text buffers (`text_offsets`,
`text_data`), layout indices (`block`, `line`, `span`), `is_synthetic`, and
content provenance (`stream`, `operation`, `0` when absent).
`chars.column(name)` returns a read-only `memoryview` that NumPy reads without
a copy, `chars[start:stop]` slices without copying, pyarrow, polars, and DuckDB
import the table as an Arrow struct array through the Arrow PyCapsule interface
(`__arrow_c_schema__` and `__arrow_c_array__`, with one `text` UTF-8 field and
nullable `stream` and `operation`), and indexing or iterating yields `Char`
records on demand.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    chars = doc.pages[0].chars()
    x0 = chars.column("x0")
    offsets = chars.column("text_offsets")
    data = bytes(chars.column("text_data"))
    # numpy.asarray(x0) reads the same buffer; pyarrow.array(chars) uses Arrow PyCapsule.
    print(len(chars), chars.names)
    print([(data[offsets[i] : offsets[i + 1]].decode(), x0[i]) for i in range(min(3, len(chars)))])
```

## Images

`page.images()` lists the images that the page paints, in the order of
`page.content()`. `pixmap()` decodes an image as the renderer does, its soft
mask as alpha; `stored()` returns JPEG, JPEG 2000, JBIG2, or CCITT data as
the document stores it. `doc.replace_image(xref, data)` swaps in JPEG,
JPEG 2000, or PNG data, and `doc.remove_image(xref)` blanks the image, on
every page that paints it.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for image in doc.pages[0].images():
        stored = image.stored()
        if stored is not None:
            print(image.item.xref, stored.extension, len(stored.data))
        else:
            image.pixmap().save(f"image-{image.index}.png")
```

## Offline OCR

With the `ocr` extra installed, `OcrEngine()` loads the PP-OCR models, and
`page.ocr` returns the words it recognizes. `page.apply_ocr` adds them as an
invisible text layer, so `text()` and `search()` find them.

```python
import pyropdf

engine = pyropdf.OcrEngine()
with pyropdf.open("input.pdf") as doc:
    words = doc.pages[0].ocr(engine=engine, dpi=150)
    print([word.text for word in words])
```

Create an engine once and reuse it: loading the models is the slow part.
