---
title: Editing
description: Edit pages, annotations, links, forms, outlines, and attachments, each change a new version.
---

# Editing

Every edit below writes a new version of the document. Wrap related edits in
`doc.transaction(name)` to make them one step of the history; see
[Versions and saving](versions.md).

## Pages

`doc.pages` is a sequence of `Page` handles. A handle keeps pointing at its
page while pages move, and raises `StalePageError` once its page is removed.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pages = doc.pages
    pages.new((595, 842))           # append an A4 page
    pages.insert(0, pages[-1])      # copy it to the front
    pages.move(0, len(pages) - 1)   # and move it to the end
    pages[0].rotation = 90
    del pages[-1]
```

Pages from another document import with their resources, annotations, form
fields, and structure:

```python
import pyropdf

with pyropdf.open("input.pdf") as doc, pyropdf.open("input.pdf") as other:
    doc.pages.insert(len(doc.pages), other.pages)
    doc.pages.select([0, 1])        # keep only these, in this order
```

`page.media_box`, `crop_box`, `bleed_box`, `trim_box`, and `art_box` read
the page boxes, inherited and clipped to the MediaBox, in the same display
space as text and rendering: `(0, 0)` is the top-left of the visible page.
Assigning a `Rect` sets that box on this page only; a box that leaves the
MediaBox raises `ValueError`. Rendering and text follow a new CropBox:

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.crop_box = (36, 36, page.rect.width - 36, page.rect.height - 36)
    page.pixmap(dpi=72).save("cropped.png")
```

`page.flatten()` draws annotation and widget appearances into the page
content and removes them from the page.

## Annotations

`page.annotations.add` takes a specification and returns an `Annotation`
handle. Each annotation is written with its appearance, so every viewer
draws it alike.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.annotations.add(pyropdf.Highlight(page.search("total")))
    page.annotations.add(pyropdf.Note((40, 40), contents="Check this"))
    page.annotations.add(pyropdf.Square((60, 300, 200, 360), color=pyropdf.Color(0.8, 0.2, 0.1)))
    for annotation in page.annotations:
        print(annotation.type, annotation.rect)
```

Text markup (highlight, underline, squiggly, strike-out), notes, lines,
squares, circles, polygons, polylines, ink, and file attachments are
available; the [API reference](api.md#annotations) lists their options.

## Links

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.links.add((72, 72, 200, 90), "https://pyropdf.haya.works/")
    page.links.add((72, 100, 200, 118), pyropdf.Destination(0))
```

## Forms

`doc.form` maps full field names to `Field` handles. Setting a value
updates the field and settles its widgets' appearances.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.widgets.add(pyropdf.TextField("name", (72, 300, 300, 324)))
    page.widgets.add(pyropdf.CheckBox("agree", (72, 340, 90, 358)))
    doc.form["name"].value = "Ada Lovelace"
    doc.form["agree"].value = True
    print({name: field.value for name, field in doc.form.items()})
```

## Outline

```python
import pyropdf
from pyropdf import Destination, OutlineEntry

with pyropdf.open("input.pdf") as doc:
    doc.outline = [
        OutlineEntry(1, "Introduction", Destination(0)),
        OutlineEntry(2, "Details", Destination(0)),
    ]
    print([item.title for item in doc.outline])
```

## Attachments

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.attachments.add("data.csv", b"a,b\n1,2\n", description="Source data")
    print(doc.attachments["data.csv"].read())
```

## Page labels

`doc.page_labels` lists the `PageLabelRange` records of the document, and
`page.label` gives one page's label. Assigning a list replaces them; the first
range starts at page 0. Inserting, moving, and deleting pages keeps each
page's label.

```python
import pyropdf
from pyropdf import PageLabelRange, PageLabelStyle

with pyropdf.open("input.pdf") as doc:
    doc.page_labels = [
        PageLabelRange(0, PageLabelStyle.LOWER_ROMAN),   # i, ii, iii, ...
        PageLabelRange(1, prefix="A-"),                  # A-1, A-2, ...
    ]
    print(doc.pages[1].label, doc.pages.labeled("A-1"))
```

## Layers

`doc.layers` lists the optional content groups as `Layer` records, with their
names and whether the default configuration shows them; `doc.layers.configs`
lists the configurations. `switch` shows or hides a group and `use` applies a
configuration, and rendering and text extraction follow both.
`visible` evaluates a group or membership dictionary, visibility expressions
included.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for layer in doc.layers:
        print(layer.name, layer.on)
        if layer.name == "Draft":
            doc.layers.switch(layer.xref, on=False)
    print(doc.pages[0].text())
```

## Metadata

`doc.metadata` is a live mapping of the Info dictionary. When the document
has an XMP packet, each edit writes the matching XMP properties in the same
transaction, so a PDF/A document keeps its two copies equivalent.

```python
from datetime import UTC, datetime

import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.metadata.update(title="Invoice 42", author="Accounts")
    doc.metadata["modified"] = datetime.now(UTC)
    print(doc.metadata["title"])                       # Invoice 42
    print(pyropdf.parse_pdf_date(doc.metadata["modified"]).year > 2000)  # True
    print(doc.xmp is None)                             # True: no XMP packet
```

## Sanitizing

`doc.sanitize(path)` removes JavaScript, actions, attachments, metadata,
comments, hidden text, and unused objects, each behind its own switch, and
saves the document in full, so the file has one revision. The returned
`SanitizeReport` counts what each kind removed and lists the signatures and
profile claims that the result breaks or no longer makes.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    report = doc.sanitize("clean.pdf", comments=False)
    print(dict(report.removed), report.changes.signatures, report.dropped_claims)
```

## Font subsetting

`doc.subset_fonts()` cuts each embedded TrueType, OpenType, and CFF program
down to the glyphs that the pages draw, with every optional content group
on, in one transaction. Glyph IDs stay as they were and the unused outlines
become empty, so text extraction and widths are unchanged. Programs that
annotation appearances or the form's default resources reach stay whole.
Each returned `SubsetFont` reports a program that shrank.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for font in doc.subset_fonts():
        print(font.name, font.kept, font.glyphs, font.size_before, font.size_after)
    doc.save("smaller.pdf")
```

## Objects

`doc.objects` maps object numbers to the indirect objects, and every value
is a typed Python object (`Dictionary`, `Array`, `Name`, `Stream`, ...) that
edits write back as a new version. `doc.check()` reports how the document
departs from the Arlington PDF model.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    catalog = doc.catalog
    print(catalog["Type"], doc.pages[0].obj["MediaBox"])
    print(doc.check().defects)
```
