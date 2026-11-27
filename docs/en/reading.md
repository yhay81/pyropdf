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

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for table in doc.pages[0].tables():
        print(table.markdown())
    print(doc.markdown())
```

## JSON and retrieval chunks

`doc.to_json()` exports the same layout elements—headings, paragraphs, list
items, tables, and images—as JSON, each with its page index and bounding box in
display space. `doc.to_chunks()` groups consecutive elements into Markdown
retrieval chunks within a character budget (`max_chars`, 2000 by default),
starting a new chunk at each heading so every `Chunk` keeps its enclosing
`section`, `pages`, and `elements` with provenance. Tables, images, and
headings are never split (`chunk.oversized` marks one that exceeds the budget),
and both methods accept `pages=` and `table_strategy=`.

```python
import json
import pyropdf

with pyropdf.open("input.pdf") as doc:
    data = json.loads(doc.to_json())
    print(len(data["pages"]), [elem["kind"] for elem in data["pages"][0]["elements"]])
    for chunk in doc.to_chunks(max_chars=500):
        print(chunk.index, chunk.pages, chunk.section, chunk.text)
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
and clips, with the marked content that encloses them. `page.operations`
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
