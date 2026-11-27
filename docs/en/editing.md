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
the image's resolution (or fitted centered into a uniform `size=(width, height)`
page), honoring Exif orientation (1–8) without recompressing JPEG or JPEG 2000
streams. PNG is read directly, keeping its alpha as a soft mask; TIFF, GIF,
and other formats go through Pillow when it is installed. To convert a sequence
of images or a CBZ (`.cbz` / ZIP) archive in one undoable transaction—sorting
archive entries naturally, ignoring `__MACOSX` and metadata files, and enforcing
`DocumentLimits` on every chunk—use `doc.pages.new_from_images(sources)` or
`doc.pages.new_from_cbz(source)`:

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pixmaps = [page.pixmap(dpi=72) for page in doc.pages]
    scan = pyropdf.Document()
    scan.pages.new_from_image(pixmaps[0])            # one image (path, bytes, or Pixmap)
    scan.pages.new_from_images(pixmaps, size=(595, 842))  # a batch fitted to A4
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
stored once. `clip`, `clip_text`, `transform`, `opacity`, `blend`, `layer`,
and `tag` are scopes for everything drawn inside them.

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
one cluster is too wide, `PdfError` is raised. `align` is `"left"`, `"center"`, `"right"`, or `"justify"`, and `line_height`
spaces lines in multiples of `size`. Text is filled black by default;
pass `stroke` to outline it, `fill=None` to leave it hollow, or
`render_mode=TextRenderMode.INVISIBLE` (`3 Tr`) to emit searchable invisible
text without drawing ink. To clip subsequent paths or images to glyph outlines
(`4 Tr`–`7 Tr`), enter a `with canvas.clip_text(...):` scope (clip-only by
default, or pass `fill` and/or `stroke` to paint and clip together). `rotate`
turns text by a right angle, and `canvas.transform` by any other.

Pass `direction="vertical"` with embedded OpenType faces to shape vertical text
with `harfrust` and embed `Identity-V` with `/DW2`/`/W2` vertical metrics;
columns run top-to-bottom and stack right-to-left. For mixed-script strings,
pass an explicit `fallback_fonts` sequence (or `doc.fallback_fonts`) to resolve
missing grapheme clusters per face without any global font registry; each used
face is embedded once and subsetted at save.

```python
import pyropdf
from pyropdf import Color, Font, TextRenderMode

doc = pyropdf.Document()
page = doc.pages.new((300, 200))
latin = Font.standard("Helvetica")
with page.draw() as canvas:
    canvas.text("Hidden OCR layer", (20, 30), render_mode=TextRenderMode.INVISIBLE)
    with canvas.clip_text("CLIP", (20, 120), font=Font.standard("Helvetica-Bold"), size=48):
        canvas.rect((0, 70, 220, 140), fill=Color(0.2, 0.5, 0.9))
    if "ja" in doc.fallback_fonts and (jp := doc.fallback_fonts["ja"].sans) is not None:
        canvas.text("Hello 日本語", (20, 160), font=latin, fallback_fonts=(jp,), size=14)
        canvas.text("縦書き\n二行目", (260, 30), font=jp, size=16, direction="vertical")
```

Text is limited by default to 1 MiB of UTF-8 and 4,096 lines per call, refused with
`LimitError` before anything is drawn; pass `max_text_size=None` for
trusted input. See [Limits and errors](limits.md).

To copy text already painted in a PDF, pass the extracted runs to a canvas.
The source document must be open during the calls to `draw`.

```python
import pyropdf

with pyropdf.open("input.pdf") as source, pyropdf.Document() as target:
    page = source.pages[0]
    copy = target.pages.new((page.rect.width, page.rect.height))
    with copy.draw() as canvas:
        for run in page.content(paths=False, images=False).text_runs:
            canvas.draw(run)
    target.save("copied-text.pdf")
```

Each glyph keeps its original PDF character code, text matrix, and font
dictionary. The canvas copies the selected fonts from the version that
produced the runs, so later changes to the source encoding or program do
not change a drawing. Fonts from the same content result share their
copied resources. Embedded simple fonts, Standard 14 fonts, embedded
Identity-H and Identity-V fonts, and Type 3 fonts with their own resources
can be copied. A run with an unavailable face, a direct font dictionary,
a composite encoding of unknown code width, a singular glyph transform,
enclosing replacement text, a pattern paint, or a soft mask raises
`ValueError`. A closed source raises `DocumentClosedError`. A canvas that exceeds its document or source limits
rolls back the drawing.

The 15-document round-trip corpus redraws 17,246 text runs. Eight runs in
`mhlw-doc.pdf` use an unembedded, nonstandard font. The corpus test checks
the remaining page pixels within 16 levels per RGB channel at 72 dpi, and
checks that the number and reason for these exclusions stay the same.

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

`doc.form.to_fdf()` and `doc.form.to_xfdf()` export field values in FDF
(ISO 32000-2 §12.7.8) and XFDF (ISO 19444-1), `parse_fdf()` and
`parse_xfdf()` inspect them as `FdfData`, and `doc.form.import_fdf()` and
`doc.form.import_xfdf()` apply matching fields in one undoable transaction:

```python
import pyropdf

with pyropdf.Document() as source, pyropdf.Document() as target:
    for doc in (source, target):
        page = doc.pages.new((400, 200))
        page.widgets.add(pyropdf.TextField("name", (40, 40, 240, 64)))
        page.widgets.add(pyropdf.CheckBox("agree", (40, 80, 58, 98)))
    source.form["name"].value = "Ada Lovelace"
    source.form["agree"].value = True

    fdf = source.form.to_fdf(file="form.pdf")
    assert pyropdf.parse_fdf(fdf).fields["name"] == "Ada Lovelace"
    assert target.form.import_fdf(fdf) == ["agree", "name"]

    xfdf = target.form.to_xfdf(include_empty=False)
    assert pyropdf.parse_xfdf(xfdf).fields == {"agree": "Yes", "name": "Ada Lovelace"}
```

## Actions, scripts, and triggers {#actions-and-triggers}

Links, outline items, presentation steps, `doc.open_action`, `page.triggers`,
`widget.triggers`, and `Screen` annotations (`annotation.action` and
`annotation.triggers`) share the `Target` action union, including
`EmbeddedDestination`, `HideAction`, `SetLayerStateAction`, `JavaScriptAction`,
`SubmitFormAction`, `ResetFormAction`, and `ImportDataAction`. Document-level
scripts live in `doc.scripts` (`/Names /JavaScript`) and catalog lifecycle
scripts in `doc.triggers` (`Catalog["AA"]`); pyropdf never runs scripts.

```python
import pyropdf
from pyropdf import (
    AnnotationTrigger,
    Destination,
    DocumentTrigger,
    EmbeddedDestination,
    EmbeddedTarget,
    HideAction,
    NamedAction,
    PageTrigger,
    PushButton,
    ResetFormAction,
    SubmitFormAction,
    SubmitFormFlags,
    TextField,
)

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.widgets.add(TextField("email", (72, 300, 260, 324)))
    send = page.widgets.add(PushButton("send", (270, 300, 340, 324), caption="Send"))
    doc.open_action = Destination(0)
    doc.scripts["init"] = "var ready = true;"
    doc.triggers[DocumentTrigger.WILL_SAVE] = "console.println('saving');"
    page.triggers[PageTrigger.OPEN] = NamedAction("FirstPage")
    send.triggers[AnnotationTrigger.ACTIVATE] = SubmitFormAction(
        "https://example.com/submit",
        fields=("email",),
        flags=SubmitFormFlags.EXPORT_FORMAT,
    )
    send.triggers[AnnotationTrigger.DOWN] = HideAction("email", hide=False)
    page.links.add(
        (72, 340, 220, 358),
        EmbeddedDestination(page=0, target=EmbeddedTarget(embedded_file="attachment.pdf")),
    )
    page.links.add((230, 340, 340, 358), ResetFormAction(("email",)))
    assert not pyropdf.open(doc.to_bytes()).check()
    print(doc.open_action, list(doc.scripts), send.triggers[AnnotationTrigger.ACTIVATE].url)
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

Each embedded file can also identify related files. Assign a mapping to `att.related_files` using the filename keys present under its `/EF` dictionary (`F` or `UF`). Each sequence retains its order. Multiple names can share one stream. The same property is available on `annotation.file`.

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
    legend = doc.objects.add_stream(b"units: metres", {"Type": pyropdf.Name("EmbeddedFile")})
    att.related_files = {"F": [pyropdf.RelatedFile(filename="legend.txt", stream=doc.objects[legend])]}
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

## Web Capture

`doc.web_capture.by_url(url)` and `by_id(source_digest)` return the page and image
sets recorded for a source. Each is a writable Arlington view. A page set's
`O` entries identify pages, an image set's `O` entries identify image streams,
and `SI` contains source information, including URL aliases and stored commands.
Reading these records works offline.

To change source locations, call `doc.web_capture.set_sources(content, source)`
with a `typed.SourceInformation` view or a tuple of views. This updates the URL
index and preserves other sets sharing the old or new URL. `remove(content)`
unlinks the set from its indexes and clears its members' parent identifiers.
Remove the relevant set before deleting or copying one of its pages, or a page
that uses one of its images. Reordering pages preserves set membership.

To register an indirectly stored `typed.WebCapturePageSet` or
`typed.WebCaptureImageSet` in PDF 1.3–1.7, call `doc.web_capture.add(content)`.
Supply the 16-byte source digest, member references, source dictionaries and,
for images, their page reference counts. PDF 2.0 deprecates new Web Capture
records. Existing records may still be read, edited and removed.

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

## Signature structures and encrypted wrappers {#signature-structures}

Beyond [signing](versions.md#signing) itself, `doc.sign` and
`doc.prepare_signature` configure DocMDP certification permissions
(`DocMdpPermission`, read back via `doc.certification` and `sig.doc_mdp`) and
FieldMDP field locks (`FieldLock` and `FieldMdpAction`, read back via
`field.lock` and `sig.field_locks`). `doc.security_store` reads and writes the
Document Security Store (`/DSS` and `/VRI` keyed by `vri_key`) as a
`SecurityStore` of deduplicated indirect streams without invalidating earlier
signatures on incremental saves, `doc.timestamp(timestamper)` appends an RFC
3161 `/DocTimeStamp` (`sig.is_timestamp`), and `doc.legal_attestation` edits
`Catalog["Legal"]` (`LegalAttestation`). For proprietary cryptographic
handlers, `wrap_encrypted_payload` builds a PDF 2.0 unencrypted wrapper
document (ISO 32000-2 §7.6.7) that `doc.encrypted_payload` reads back as an
`EncryptedPayload`. In a wrapper created by this function, the file
specification holds the display filename and `doc.collection.initial_document`
holds the opaque key for `doc.attachments`. Use that key to edit the selected
payload when the wrapper contains other files.

```python
import pyropdf
from pyropdf import (
    DocMdpPermission,
    EncryptedPayload,
    Encryption,
    FieldLock,
    FieldMdpAction,
    LegalAttestation,
    TextField,
    wrap_encrypted_payload,
)

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].widgets.add(TextField("amount", (72, 300, 220, 324)))
    doc.prepare_signature(
        "author_sig",
        doc_mdp=DocMdpPermission.FORM_FILLING_AND_SIGNATURES,
        field_lock=FieldLock(FieldMdpAction.INCLUDE, ("amount",)),
        reason="Certified invoice",
    )
    doc.legal_attestation = LegalAttestation(annotations=0, optional_content=False)
    assert doc.form["author_sig"].lock == FieldLock(FieldMdpAction.INCLUDE, ("amount",))
    locked_bytes = doc.to_bytes(
        encryption=Encryption(user_password="reader", owner_password="owner"),
    )

with wrap_encrypted_payload(EncryptedPayload(locked_bytes, filename="invoice.pdf")) as wrapper:
    assert wrapper.encrypted_payload is not None
    print(wrapper.encrypted_payload.filename, wrapper.encrypted_payload.subtype)
```

A DSS requires PDF 2.0. When writing it to an older document would introduce a structural defect, the setter raises `PdfError` and preserves the original revision. This example uses sample signature contents on a fresh document. For an existing signature, pass its CMS contents to `vri_key` and save incrementally to preserve the signed revision.

```python
import pyropdf
from pyropdf import SecurityStore, ValidationInfo, vri_key

with pyropdf.Document() as doc:
    doc.pages.new((612, 792))
    key = vri_key(b"cms-contents")
    info = ValidationInfo(created="D:20260101000000Z")
    doc.security_store = SecurityStore(vri={key: info})
    assert doc.security_store.vri[key] == info
```

## Viewports, geospatial measurements, and document requirements {#viewports-and-requirements}

`page.viewports` reads and sets a page's measurement viewports (`/VP`) in
display coordinates across `/CropBox`, `/Rotate`, and `/UserUnit`, with
`RectilinearMeasure` (`NumberFormat`, `NumberFormatFraction`) for CAD scale
chains and `GeospatialMeasure` (`CoordinateSystem`, `PointData`) for EPSG or
WKT coordinate systems. Numeric coordinates and conversion factors use
`decimal.Decimal` so high-precision latitude/longitude values write their exact
digits into the PDF object store without `float` rounding. `doc.requirements`
manages `Catalog["Requirements"]` (`DocumentRequirement`, `RequirementType`,
`RequirementHandler`), and `del page.viewports` or `del doc.requirements`
removes the entry and frees unreferenced objects in one transaction.

Read or assign `owner.geospatial_measure` on a viewport dictionary, image or
form XObject stream, or a 3D or RichMedia annotation's `obj`. A 2D registration
pairs each normalized local point with one ground point. For a 3D annotation,
declare `RequirementType.GEOSPATIAL_3D` and use `dimensions=3` for world
coordinates and elevation. `owner.geospatial_point(index)` returns the local
point in the owner's PDF coordinate space and its ground coordinates. Geographic
coordinates are latitude then longitude. Projected coordinates are easting then
northing, with the optional projected matrix taking precedence over the ground
array. A PDF 1.7 document that declares Adobe extension level 3 or later can
store 2D geographic controls without changing editions. If PDF 2.0 is required,
an upgrade that would introduce structural defects is refused in one transaction.
Malformed registrations remain inspectable through the raw PDF dictionary.
Decimal inputs retain their exact digits on save. For numeric tokens read from a
PDF, precision currently follows the underlying reader (see
[parser tracking issue #16](https://github.com/yhay81/pyropdf/issues/16)).

```python
from decimal import Decimal
import pyropdf
from pyropdf import (
    CoordinateSystem,
    DocumentRequirement,
    GeospatialMeasure,
    NumberFormat,
    NumberFormatFraction,
    PointData,
    RectilinearMeasure,
    RequirementHandler,
    RequirementType,
    Viewport,
)

lat = Decimal("35.6812361234567890123456789")
lon = Decimal("139.7671249876543210987654321")

with pyropdf.Document() as doc:
    page = doc.pages.new()
    page.viewports = (
        Viewport(
            (36, 36, 260, 220),
            name="Floor plan",
            measure=RectilinearMeasure(
                "1 in = 10 ft",
                x=(
                    NumberFormat(
                        "ft",
                        Decimal("0.1388888888888889"),
                        fraction=NumberFormatFraction.FRACTION,
                        denominator=16,
                    ),
                ),
                distance=(NumberFormat("ft", Decimal("0.1388888888888889")),),
                area=(NumberFormat("sq ft", Decimal("0.0192901234567901")),),
            ),
        ),
        Viewport(
            (36, 240, 260, 460),
            name="Site map",
            measure=GeospatialMeasure(
                CoordinateSystem(epsg=4326),
                (lat, lon, lat, lon),
                lpts=(0, 0, 1, 1),
                pdu=("M", "SQM", "DEG"),
            ),
            point_data=PointData(("LAT", "LON", "ALT"), ((lat, lon, Decimal("42.5")),)),
        ),
    )
    doc.requirements = (
        DocumentRequirement(
            RequirementType.GEOSPATIAL_2D,
            handlers=(RequirementHandler("JS", script="app.alert('Geospatial viewer required');"),),
            penalty=50,
        ),
    )
    saved = doc.to_bytes()
    assert b"35.6812361234567890123456789" in saved

with pyropdf.open(saved) as reopened:
    assert not reopened.check()
    assert len(reopened.pages[0].viewports) == 2
    assert reopened.requirements[0].type is RequirementType.GEOSPATIAL_2D
```

## Interchange and structure namespaces

`doc.extensions` manages direct developer and ISO extensions (`DeveloperExtension`,
`ISO_EXTENSIONS`), `associated_files` attaches files with an
`AssociatedFileRelationship` to the document, pages, structure elements,
annotations, and document parts, `piece_info` stores `/PieceInfo` application
dictionaries on the catalog and pages, `doc.parts` builds `/DPartRoot` page-range
trees that `DocumentPartAction` targets, and `doc.structure.namespaces` mixes
PDF 1.7 (`PDF_1_7_NAMESPACE`), PDF 2.0 (`PDF_2_0_NAMESPACE`), and custom
namespaces in one structure tree.

```python
import pyropdf
from pyropdf import (
    PDF_1_7_NAMESPACE,
    PDF_2_0_NAMESPACE,
    AssociatedFileRelationship,
    DeveloperExtension,
    DocumentPartAction,
    OutlineEntry,
)

with pyropdf.Document() as doc:
    first = doc.pages.new()
    second = doc.pages.new()
    doc.extensions.add(DeveloperExtension("ISO_", "2.0", 32001))
    doc.associated_files.add(
        "source.xml",
        b"<invoice><total>42</total></invoice>",
        media_type="application/xml",
        relationship=AssociatedFileRelationship.SOURCE,
    )
    first.piece_info.set("ACME", {"stage": "approved"})
    part = doc.parts.add_part(first, second, dpm={"Record": "INV-1"})
    doc.outline = [OutlineEntry(1, "Invoice part", DocumentPartAction(part))]

    ns20 = doc.structure.namespaces.add(PDF_2_0_NAMESPACE)
    ns17 = doc.structure.namespaces.add(PDF_1_7_NAMESPACE)
    custom = doc.structure.namespaces.add("https://example.com/ns", role_map={"Callout": ("Aside", ns20)})
    root_elem = doc.structure.add("Document", namespace=ns20)
    root_elem.children.add("TOC", namespace=ns17)
    note = root_elem.children.add("Callout", namespace=custom)
    assert note.resolved_role == ("Aside", PDF_2_0_NAMESPACE)
    saved = doc.to_bytes()

with pyropdf.open(saved) as reopened:
    assert not reopened.check()
    assert reopened.associated_files[0].filename == "source.xml"
    assert isinstance(reopened.outline[0].destination, DocumentPartAction)
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

## Print production data

`page.box_styles` describes stored guides for the crop, bleed, trim and art
boxes. An omitted guide uses the reader's settings. The page box itself
controls whether the guide applies. `page.separations` lists the actual
page handles for a pre-separated sheet, in plate order.

```python
import pyropdf

with pyropdf.Document() as doc:
    cyan, black = doc.pages.new(), doc.pages.new()
    cyan.separations = {
        cyan: pyropdf.SeparationPlate(colorant=pyropdf.Name("Cyan")),
        black: pyropdf.SeparationPlate(colorant=pyropdf.Name("Black")),
    }
    assert list(black.separations) == [cyan, black]
```

Assign the group again to change its members or order. Deleting
`cyan.separations` clears the group on both pages. Moving a plate keeps its
identity. Removing or importing part of a group and duplicating an individual
plate require detaching it first.

`PrinterMark` and `Watermark` take an existing Form XObject from the same
document. The annotation's `print_appearance` resolves the selected normal
appearance and its resources. On a watermark, `fixed_print` stores the matrix
and translation fractions relative to the print medium. On legacy trap
networks, the selected Form exposes the process colour model, separation
names, and trap regions. The annotation dictionary retains the original
trapping snapshot. An image or Form's `opi` exposes the existing proxy
file specification and version-specific parameters. Use these views to edit
existing legacy data.

The stored parameters describe the intended output. Device separation,
trap generation, OPI substitution and physical press placement belong to
the rendering engine. [Rendering work](https://github.com/yhay81/pyropdf/issues/248)
and [engine tracker](https://github.com/yhay81/pyropdf/issues/7).
