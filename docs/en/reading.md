---
title: Reading content
description: Text, words, characters, layout, search, tables, Markdown, and offline OCR.
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
is the page's top-left corner.

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
