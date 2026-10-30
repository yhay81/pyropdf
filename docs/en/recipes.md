---
title: Recipes
description: Short, complete programs for common PDF tasks — merge, split, render, extract, highlight, fill forms, shrink, clean, and protect.
---

# Recipes

Each recipe is a complete program. Replace `input.pdf` with your file.

## Merge documents

```python
import pyropdf

with pyropdf.open("input.pdf") as doc, pyropdf.open("input.pdf") as other:
    doc.pages.insert(len(doc.pages), other.pages)
    doc.save("merged.pdf")
```

Imported pages bring their resources, annotations, form fields, and
structure with them.

## Split into one file per page

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for number, page in enumerate(doc.pages, start=1):
        with pyropdf.Document() as part:
            part.pages.insert(0, page)
            part.save(f"page-{number}.pdf")
```

## Keep, reorder, or delete pages

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages.select([1, 0])   # keep these pages, in this order
    del doc.pages[-1]          # delete the last one
    doc.save("reordered.pdf")
```

## Render every page to PNG

```python
from pathlib import Path

import pyropdf

with pyropdf.open("input.pdf") as doc:
    for number, data in enumerate(doc.render(dpi=150, format="png"), start=1):
        Path(f"page-{number}.png").write_bytes(data)
```

`doc.render` renders in parallel. A thumbnail is the same call at a low
`dpi`, such as 36.

## Extract text or Markdown

```python
from pathlib import Path

import pyropdf

with pyropdf.open("input.pdf") as doc:
    Path("input.txt").write_text(doc.text(), encoding="utf-8")
    Path("input.md").write_text(doc.markdown(), encoding="utf-8")
```

Markdown keeps headings and tables, which suits search indexes and
language models.

## Highlight every match of a word

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    with doc.transaction("highlight"):
        for page in doc.pages:
            if hits := page.search("total"):
                page.annotations.add(pyropdf.Highlight(hits))
    doc.save("highlighted.pdf", incremental=True)
```

## Fill a form from a dictionary

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:  # make a form to fill
    doc.pages[0].widgets.add(pyropdf.TextField("name", (72, 300, 300, 324)))
    doc.save("form.pdf")

values = {"name": "Ada Lovelace"}
with pyropdf.open("form.pdf") as doc:
    with doc.transaction("fill"):
        for name, value in values.items():
            doc.form[name].value = value
    doc.flatten()  # optional: draw the values into the pages for good
    doc.save("filled.pdf")
```

## Remove pages without text

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    blank = [index for index, page in enumerate(doc.pages) if not page.text().strip()]
    with doc.transaction("remove blank pages"):
        for index in reversed(blank):
            del doc.pages[index]
    doc.save("without-blanks.pdf")
```

## Add a bookmark for each page

```python
import pyropdf
from pyropdf import Destination, OutlineEntry

with pyropdf.open("input.pdf") as doc:
    doc.outline = [
        OutlineEntry(1, page.text().strip().splitlines()[0] or f"Page {index + 1}", Destination(index))
        for index, page in enumerate(doc.pages)
        if page.text().strip()
    ]
    doc.save("bookmarked.pdf")
```

## Make a file smaller

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.subset_fonts()
    doc.save("smaller.pdf", compress=True, object_streams=True, prune=True, dedupe=True)
```

`subset_fonts` keeps only the glyphs that the pages draw, `prune` drops
unreachable objects, and `dedupe` writes identical objects once.

## Clean a document before sharing it

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    report = doc.sanitize("shared.pdf")
    print(dict(report.removed))
```

`sanitize` removes JavaScript, actions, attachments, metadata, comments,
hidden text, and unused objects; each has its own switch.

## Protect with a password

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.save("locked.pdf", encryption=pyropdf.Encryption(user_password="reader", owner_password="owner"))
```

## Open a document with bounded work

```python
import pyropdf

def page_count(data: bytes) -> int | None:
    try:
        with pyropdf.open(data, limits=pyropdf.DocumentLimits.web()) as doc:
            return len(doc.pages)
    except pyropdf.LimitError as error:
        print("refused:", error.code)
    except pyropdf.PdfError as error:
        print("unreadable:", error)
    return None

with open("input.pdf", "rb") as file:
    print(page_count(file.read()))
```

Limits bound the work of each operation; [Status and safety](status.md)
says what they do not promise.
