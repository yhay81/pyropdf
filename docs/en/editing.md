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

`doc.pages.new_from_image(source)` adds a page per image frame, sized by
the image's resolution. JPEG and JPEG 2000 are embedded without
recompression, and PNG is read directly, keeping its alpha as a soft mask;
TIFF, GIF, and other formats go through Pillow when it is installed:

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pixmap = doc.pages[0].pixmap(dpi=72)
    scan = pyropdf.Document()
    scan.pages.new_from_image(pixmap)       # or a path, or JPEG/PNG bytes
    scan.save("scan.pdf")
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

## Drawing

`page.draw()` returns a `Canvas` that draws paths, images, other pages, and
text in display space, over the page's content or, with `underlay=True`, below
it. The `with` block is one transaction and one undo step; an exception
inside it discards the drawing.

```python
import pyropdf
from pyropdf import Color, PathBuilder, Stroke

with pyropdf.open("input.pdf") as doc, pyropdf.open("input.pdf") as other:
    page = doc.pages[0]
    with page.draw() as canvas:
        canvas.rect((36, 36, 236, 136), fill=Color(1, 1, 0, 0.4))
        canvas.circle((300, 100), 40, stroke=Stroke(paint=Color(0, 0, 1), width=2))
        with canvas.clip(PathBuilder().rect((36, 200, 236, 300), radius=12)), canvas.opacity(0.5):
            canvas.page((36, 200, 236, 300), other.pages[0])
        canvas.image((300, 200, 400, 300), page.pixmap(dpi=36))
```

`canvas.image` takes JPEG, PNG, or JPEG 2000 data, a path, a `Pixmap`, or
an image of the document; `canvas.page` places a page of any open document,
optionally clipped and turned. An image or a page drawn several times is
stored once. `clip`, `transform`, `opacity`, `blend`, `layer`, and `tag` are
scopes for everything drawn inside them.

### Gradients

`LinearGradient` and `RadialGradient` paint wherever a `Color` does on a path,
a shape, or text, as the `fill` or as the `paint` of a `Stroke`. A gradient's
coordinates are in the space of what it paints, so it moves with
`canvas.transform`.

```python
import pyropdf
from pyropdf import Color, ColorStop, GradientExtend, LinearGradient, Point, RadialGradient, Stroke

sky = LinearGradient(
    start=Point(0, 0),
    end=Point(0, 200),
    stops=(ColorStop(0, Color(0.2, 0.4, 0.9)), ColorStop(1, Color(1, 1, 1))),
)
sun = RadialGradient(
    start=Point(100, 100),
    start_radius=0,
    end=Point(100, 100),
    end_radius=40,
    # A stop's alpha fades the color out, through a soft mask.
    stops=(ColorStop(0, Color(1, 0.8, 0)), ColorStop(1, Color(1, 0.8, 0, 0))),
)
stripes = LinearGradient(
    start=Point(0, 0),
    end=Point(20, 0),
    stops=(ColorStop(0, Color(0, 0, 0)), ColorStop(1, Color(1, 1, 1))),
    extend=GradientExtend.REFLECT,
)

doc = pyropdf.Document()
page = doc.pages.new((200, 200))
with page.draw() as canvas:
    canvas.rect((0, 0, 200, 200), fill=sky)
    canvas.circle((100, 100), 40, fill=sun)
    canvas.line((0, 180), (200, 180), stroke=Stroke(paint=stripes, width=6))
```

`extend` says what a gradient paints beyond its line: the end colors continue
(`PAD`, the default), only the start or end color continues (`PAD_START`,
`PAD_END`), nothing (`NONE`), or the line runs again (`REPEAT`) or back and
forth (`REFLECT`). Equal functions and shadings are written once in a canvas.
`page.content()` and `page.paths()` read axial and radial shadings back as a
`ShadingPattern` with their color function, and `canvas.draw` repaints their
fills and strokes as a bounded sampled approximation (other shading types and
`TilingPattern` are not repainted). When a soft mask requires separate text
fill and stroke objects, the second has empty `ActualText` so that text
extraction reads the text once.

### Text

`canvas.text` draws lines from a baseline point, and `canvas.textbox`
wraps text inside a rectangle. Both draw in Helvetica at 11 points unless
`font` and `size` say otherwise. `Font.standard` returns one of the 14
Standard 14 fonts by name, such as `"Times-Roman"` or `"Courier-Bold"`;
these are not embedded, so the file stays small. The 12 text fonts accept
WinAnsi (roughly Latin-1) characters and refuse other characters with
`PdfError`. Symbol and ZapfDingbats use their built-in encodings: a
WinAnsi byte selects a glyph, which may differ from the input character.
For other scripts, load an OpenType face with
`Font.from_file`, which the document embeds once. For a variable TrueType
face, `weight=700` selects that instance on the face's weight axis. A glyph
that COLR paints, or that a PNG strike of `sbix` or CBDT holds, draws in its
own colours, whatever the fill, and reads back as text. Any other colour glyph draws its
monochrome fallback outline, and drawing one with no outline raises
`PdfError`, rather than draw nothing under text that reads back.

```python
import pyropdf
from pyropdf import Color, Font

doc = pyropdf.Document()
for _ in range(2):
    doc.pages.new((300, 200))
note = "Totals include tax. Prices are in euros and valid until the end of the month."
helvetica = Font.standard("Helvetica")
for number, page in enumerate(doc.pages, start=1):
    with page.draw() as canvas:
        canvas.text("Invoice", (20, 40), font=Font.standard("Helvetica-Bold"), size=18)
        box = (20, 60, 160, 80)
        if not canvas.textbox(box, note, size=10, align="justify").fits:
            height = helvetica.text_height(note, 10, width=140)
            canvas.textbox((20, 60, 160, 60 + height), note, size=10, align="justify")
        canvas.text(f"Page {number} of {len(doc.pages)}", (240, 190), size=8, fill=Color(0.4, 0.4, 0.4))
```

A textbox either fits or draws nothing: the returned `TextFit` says
whether it fit, and its `spare_height` is the room left below the last
line, or the negative shortfall. `Font.text_height` and
`Font.text_width` measure text exactly as the canvas lays it out, so a
caller can size a box before drawing. Lines break where Unicode allows,
and a word wider than the box breaks between grapheme clusters. If even
one cluster is too wide, `PdfError` is raised. `align` is
`"left"`, `"center"`, `"right"`, or `"justify"`, and `line_height`
spaces lines in multiples of `size`. Text is filled black by default;
pass `stroke` to outline it and `fill=None` to leave it hollow. `rotate`
turns text by a right angle, and `canvas.transform` by any other.

Text is limited by default to 1 MiB of UTF-8 and 4,096 lines per call, refused with
`LimitError` before anything is drawn; pass `max_text_size=None` for
trusted input. See [Limits and errors](limits.md).

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

Text markup (highlight, underline, squiggly, strike-out), notes, free text
(`FreeText`), stamps (`Stamp`), carets (`Caret`), lines, squares, circles,
polygons, polylines, ink, sounds (`Sound`), and file attachments are
available; the [API reference](api/annotations.md) lists their options.

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

## Article threads

`doc.threads` exposes the document's article threads (`/Threads`) as a
sequence of `Thread` handles. Each thread holds an ordered `Beads` ring of
`Bead` handles whose `rect` is in its page's display coordinates, plus `/I`
metadata properties (`title`, `author`, `subject`, `keywords`, `created`,
`modified`). A `ThreadAction` navigates to a thread or a specific bead from a
link, an outline item, or a presentation navigation step.

```python
import pyropdf
from pyropdf import BeadSpecification, OutlineEntry, ThreadAction

with pyropdf.open("input.pdf") as doc:
    article = doc.threads.add(
        [
            BeadSpecification(0, (36, 72, 260, 400)),
            BeadSpecification(1, (36, 72, 260, 400)),
        ],
        title="Cover Story",
        author="Editorial",
    )
    article.beads.add(1, (280, 72, 500, 400))
    doc.pages[0].links.add((36, 40, 180, 60), ThreadAction(0, 1))
    doc.outline = [OutlineEntry(1, "Read Cover Story", ThreadAction("Cover Story"))]
    print(article.title, len(article.beads), doc.pages[0].links[-1].target)
```

## Structure and accessibility

`doc.structure` exposes the logical structure tree as its top-level
`StructureElement` handles, `walk()` visits every element in pre-order, and
`doc.structure.role_map` reads and edits `/RoleMap`. Each element's `role`,
`title`, `language` (a BCP 47 tag), `alt`, `actual_text`, `expanded` (`/E`
abbreviation expansion), and `page` are assignable and deletable, and
`children` and `move()` reorder or reparent elements while keeping `/P`, `/K`,
`/ParentTree`, and `/IDTree` consistent.

```python
import pyropdf
from pyropdf import TagRole

with pyropdf.open("input.pdf") as doc:
    doc.structure.role_map["Callout"] = TagRole.NOTE
    root = doc.structure.add("Document")
    heading = root.children.add(TagRole.HEADING_1, page=0, title="Summary")
    abbr = heading.children.add(TagRole.SPAN, expanded="portable document format")
    note = root.children.add("Callout", page=0, language="en-US", alt="Payment terms")
    print([elem.standard_role for elem in doc.structure.walk()])
    print(abbr.expanded, note.language, note.alt)
```

## Attachments and portable collections

`doc.attachments` manages embedded files (`/Names /EmbeddedFiles`), each
attachment's `collection_item` reads and writes its `/CI` `CollectionItem`
values (including `CollectionSubitem` display prefixes), and `doc.collection`
configures a portable collection's schema, sort order, colors, split pane,
custom `Navigator`, and `CollectionFolder` hierarchy.

```python
import pyropdf
from pyropdf import (
    Collection,
    CollectionField,
    CollectionFieldKind,
    CollectionFolder,
    CollectionSortKey,
    CollectionSubitem,
    CollectionView,
    Navigator,
)

with pyropdf.open("input.pdf") as doc:
    att = doc.attachments.add("data.csv", b"a,b\n1,2\n", description="Source data")
    att.collection_item = {
        "Department": "Finance",
        "Total": CollectionSubitem(42, "$"),
    }
    doc.collection = Collection(
        view=CollectionView.CUSTOM,
        initial_document="data.csv",
        schema={
            "Department": CollectionField(CollectionFieldKind.STRING, "Department", order=1),
            "Total": CollectionField(CollectionFieldKind.NUMBER, "Total", order=2),
        },
        sort=(CollectionSortKey("Department"),),
        navigator=Navigator("FilmStrip"),
        root_folder=CollectionFolder(0, "Portfolio", children=(CollectionFolder(1, "Q1"),)),
    )
    print(doc.attachments["data.csv"].read(), doc.collection.navigator.layout)
```

## Page labels and named pages

`doc.page_labels` lists the `PageLabelRange` records of the document, and
`page.label` gives one page's label. Assigning a list replaces them; the first
range starts at page 0. Inserting, moving, and deleting pages keeps each
page's label. `doc.named_pages` maps string keys in `/Names /Pages` to live
`Page` handles, pruning entries automatically when pages are removed.

```python
import pyropdf
from pyropdf import PageLabelRange, PageLabelStyle

with pyropdf.open("input.pdf") as doc:
    doc.page_labels = [
        PageLabelRange(0, PageLabelStyle.LOWER_ROMAN),   # i, ii, iii, ...
        PageLabelRange(1, prefix="A-"),                  # A-1, A-2, ...
    ]
    doc.named_pages["summary"] = 0
    doc.named_pages["appendix"] = doc.pages[1]
    print(doc.pages[1].label, doc.pages.labeled("A-1"), doc.named_pages["appendix"].index)
```

## Presentations and thumbnails

`page.duration` sets how many seconds a viewer displays a page during a
presentation before advancing, `page.transition` sets its `Transition` effect,
and `page.navigation_steps` lists the sub-page `NavigationStep` sequence
(`/PresSteps`). A `TransitionAction` triggers a transition from a navigation
step, a link, or an outline item. `page.thumbnail` decodes or replaces the
page's embedded `/Thumb` preview as a `Pixmap`; `del` removes any of them.

```python
import pyropdf
from pyropdf import NavigationStep, Transition, TransitionAction, TransitionStyle

with pyropdf.open("input.pdf") as doc:
    slide = doc.pages[0]
    slide.duration = 5.0
    slide.transition = Transition(style=TransitionStyle.WIPE, duration=0.8, direction=0)
    slide.navigation_steps = [
        NavigationStep(
            next_action=TransitionAction(Transition(style=TransitionStyle.FADE)),
            duration=2.0,
        ),
    ]
    slide.thumbnail = slide.pixmap(fit=96)
    assert slide.thumbnail is not None
    print(slide.transition.style, len(slide.navigation_steps), slide.thumbnail.width)
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

The catalog's viewer and accessibility properties live on `doc` as assignable
and deletable attributes: `doc.page_mode`, `doc.page_layout`, `doc.language`
(a BCP 47 tag), `doc.mark_info`, and `doc.viewer_preferences`.

```python
import pyropdf
from pyropdf import Duplex, MarkInfo, PageLayout, PageMode, ReadingDirection, ViewerPreferences

with pyropdf.open("input.pdf") as doc:
    doc.page_mode = PageMode.USE_OUTLINES
    doc.page_layout = PageLayout.TWO_PAGE_LEFT
    doc.language = "en-US"
    doc.mark_info = MarkInfo(marked=True)
    doc.viewer_preferences = ViewerPreferences(
        display_title=True,
        direction=ReadingDirection.L2R,
        duplex=Duplex.FLIP_LONG_EDGE,
        print_page_ranges=((0, 1),),
    )
    print(doc.page_mode, doc.language, doc.viewer_preferences.display_title)
    del doc.viewer_preferences
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
