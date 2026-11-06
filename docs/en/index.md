---
title: pyropdf
description: PDFs, with Python ergonomics and a Rust core. Read, render, edit, and sign PDF from Python; every edit is a version that can be undone, and a failed one leaves the document as it was.
hide:
  - navigation
  - toc
---

<section class="pp-hero" aria-labelledby="pp-hero-title" markdown>
<div class="pp-hero__text">
  <h1 id="pp-hero-title">PDFs, with Python ergonomics and a Rust core.</h1>
  <p class="pp-lede">
    pyropdf reads, renders, edits, and signs PDF documents. Pages are a sequence, form fields a mapping, and edits a transaction.
    Underneath, one versioned object store in Rust keeps every version: any change can be undone, and an edit that fails leaves the document as it was.
  </p>
  <div class="pp-install" aria-label="Installation command">
    <code tabindex="0">pip install pyropdf</code>
    <button type="button" data-copy-command="pip install pyropdf" data-copied-label="Copied" aria-label="Copy the installation command"><span data-copy-label>Copy</span></button>
  </div>
  <p class="pp-actions">
    <a class="pp-button pp-button--primary" href="getting-started/">Get started</a>
    <a class="pp-button" href="recipes/">Recipes</a>
    <a class="pp-button" href="api/">API reference</a>
  </p>
  <p class="pp-facts">MIT licensed · No required dependencies · CPython 3.11–3.15, free-threaded too</p>
</div>

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    print(page.text())

    with doc.transaction("highlight totals"):
        hits = page.search("total")
        page.annotations.add(pyropdf.Highlight(hits))

    doc.save("reviewed.pdf", incremental=True)
```
</section>

## What it does

<dl class="pp-index">
  <div>
    <dt><a href="reading/">Read</a></dt>
    <dd>Text in reading order, words, characters, layout, search, tables, and Markdown.</dd>
    <dd><code>page.markdown()</code></dd>
  </div>
  <div>
    <dt><a href="reading/#offline-ocr">Recognize scans</a></dt>
    <dd>Offline OCR with the PP-OCR models of the <code>ocr</code> extra, written back as an invisible text layer.</dd>
    <dd><code>page.ocr(engine=OcrEngine())</code></dd>
  </div>
  <div>
    <dt><a href="rendering/">Render</a></dt>
    <dd>Pixmaps, PNG, JPEG, and SVG through hayro, and batches of pages in parallel.</dd>
    <dd><code>page.pixmap(dpi=144)</code></dd>
  </div>
  <div>
    <dt><a href="editing/#pages">Edit pages</a></dt>
    <dd>Insert, import, move, rotate, crop, and flatten pages; edit metadata, labels, outlines, and layers.</dd>
    <dd><code>doc.pages.insert(0, other.pages)</code></dd>
  </div>
  <div>
    <dt><a href="editing/#drawing">Draw and annotate</a></dt>
    <dd>Paths, images, pages, and text; highlights, notes, shapes, links, free text, and stamps.</dd>
    <dd><code>page.draw()</code></dd>
  </div>
  <div>
    <dt><a href="editing/#forms">Fill forms</a></dt>
    <dd>Read and set field values, add widgets, and keep their appearances current.</dd>
    <dd><code>doc.form["name"].value = "Ada"</code></dd>
  </div>
  <div>
    <dt><a href="versions/#saving">Save and sign</a></dt>
    <dd>Incremental saves that append only what changed, AES-256 encryption, and PAdES signatures in a new revision.</dd>
    <dd><code>doc.sign("approval", signer)</code></dd>
  </div>
  <div>
    <dt><a href="editing/#sanitizing">Clean and shrink</a></dt>
    <dd>Remove JavaScript, attachments, metadata, and hidden text before sharing, and subset fonts.</dd>
    <dd><code>doc.sanitize("shared.pdf")</code></dd>
  </div>
</dl>

## Built on versions

<div class="pp-split" markdown>
<div class="pp-split__text">
  <p>Every edit writes a new version of the document instead of changing a parsed copy in place. That one decision is where the guarantees come from.</p>
  <ul>
    <li><strong>An edit costs what it touches.</strong> Engines read the store, not a reserialized file: rotating a page writes one object.</li>
    <li><strong>Every change can be undone.</strong> A transaction is one named step of <code>doc.history</code>, and undo moves between versions without copying the document.</li>
    <li><strong>A failure leaves no trace.</strong> An edit that raises keeps the version it started from, and limits stop any operation's work with a typed <code>LimitError</code>.</li>
  </ul>
  <p><a href="versions/">Versions and saving</a> · <a href="concepts/">Concepts</a></p>
</div>

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("rotate"):
        page.rotation = 90
    print(doc.history.steps)   # ('rotate',)
    doc.history.undo()
    print(page.rotation)       # 0
```
</div>

## Before you adopt it

<div class="pp-notes">
  <p>The API may still change between minor versions, and rendering is slower than MuPDF's. No pyropdf release claims safety for untrusted input before hayro's bounded construction is released; <a href="status/">Status and safety</a> says what the limits do and do not promise.</p>
  <p><a href="choosing/">Choosing pyropdf</a> compares it with PyMuPDF, pypdf, and pikepdf, with timings.</p>
</div>
