---
title: pyropdf
description: PDFs, with Python ergonomics and a Rust core. Read, render, edit, and sign PDF from Python; every edit is a version that can be undone, and a failed one leaves the document as it was.
home: true
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
    <span class="pp-install__prompt" aria-hidden="true">$</span>
    <code tabindex="0">pip install pyropdf</code>
    <button type="button" data-copy-command="pip install pyropdf" data-copied-label="Copied" aria-label="Copy the installation command"><span data-copy-label>Copy</span></button>
  </div>
  <p class="pp-actions">
    <a class="pp-button pp-button--primary" href="getting-started/">Get started</a>
    <a class="pp-button" href="recipes/">Recipes</a>
    <a class="pp-button" href="api/">API reference</a>
  </p>
  <ul class="pp-facts" aria-label="Project facts">
    <li>MIT licensed</li>
    <li>No required dependencies</li>
    <li>CPython 3.11–3.15, free-threaded too</li>
  </ul>
</div>
<div class="pp-hero__code" markdown>

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

</div>
</section>

<section class="pp-section" aria-labelledby="pp-features">
  <header class="pp-section__head">
    <h2 id="pp-features">One package for the whole document</h2>
    <p>Reading, rendering, and editing share one store, so what you read is always the version you just edited.</p>
  </header>
  <div class="pp-cards">
    <a class="pp-card" href="reading/">
      <h3>Read</h3>
      <p>Text in reading order, words, characters, layout, search, tables, and Markdown.</p>
      <code>page.markdown()</code>
    </a>
    <a class="pp-card" href="reading/#offline-ocr">
      <h3>Recognize scans</h3>
      <p>Offline OCR with the PP-OCR models of the <code>ocr</code> extra, written back as an invisible text layer.</p>
      <code>page.ocr(engine=OcrEngine())</code>
    </a>
    <a class="pp-card" href="rendering/">
      <h3>Render</h3>
      <p>Pixmaps, PNG, JPEG, and SVG through hayro, and batches of pages in parallel.</p>
      <code>page.pixmap(dpi=144)</code>
    </a>
    <a class="pp-card" href="editing/#pages">
      <h3>Edit pages</h3>
      <p>Insert, import, move, rotate, crop, and flatten pages; edit metadata, labels, outlines, and layers.</p>
      <code>page.rotation = 90</code>
    </a>
    <a class="pp-card" href="editing/#drawing">
      <h3>Draw and annotate</h3>
      <p>Paths, images, pages, and text; highlights, notes, shapes, links, free text, and stamps.</p>
      <code>page.draw()</code>
    </a>
    <a class="pp-card" href="editing/#forms">
      <h3>Fill forms</h3>
      <p>Read and set field values, add widgets, and keep their appearances current.</p>
      <code>doc.form["name"].value = "Ada"</code>
    </a>
    <a class="pp-card" href="versions/#saving">
      <h3>Save and sign</h3>
      <p>Incremental saves that append only what changed, AES-256 encryption, and PAdES signatures in a new revision.</p>
      <code>doc.sign("approval", signer)</code>
    </a>
    <a class="pp-card" href="editing/#sanitizing">
      <h3>Clean and shrink</h3>
      <p>Remove JavaScript, attachments, metadata, and hidden text before sharing, and subset fonts.</p>
      <code>doc.sanitize("shared.pdf")</code>
    </a>
  </div>
</section>

<section class="pp-section pp-split" aria-labelledby="pp-versions" markdown>
<div class="pp-split__text">
  <header class="pp-section__head">
    <h2 id="pp-versions">Built on versions</h2>
    <p>Every edit writes a new version of the document instead of changing a parsed copy in place. The guarantees follow from that.</p>
  </header>
  <ul class="pp-points">
    <li><strong>An edit costs what it touches.</strong> Engines read the store, not a reserialized file: rotating a page writes one object.</li>
    <li><strong>Every change can be undone.</strong> A transaction is one named step of <code>doc.history</code>, and undo moves between versions without copying the document.</li>
    <li><strong>A failure leaves no trace.</strong> An edit that raises keeps the version it started from, and limits stop any operation's work with a typed <code>LimitError</code>.</li>
  </ul>
  <p class="pp-more"><a href="versions/">Versions and saving</a><a href="concepts/">Concepts</a></p>
</div>
<div class="pp-split__code" markdown>

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
</section>

<section class="pp-section pp-closing" aria-labelledby="pp-adopt">
  <h2 id="pp-adopt">Before you adopt it</h2>
  <p>The API may still change between minor versions, and rendering is slower than MuPDF's. No pyropdf release claims safety for untrusted input before hayro's bounded construction is released; <a href="status/">Status and safety</a> says what the limits do and do not promise. <a href="choosing/">Choosing pyropdf</a> compares it with PyMuPDF, pypdf, and pikepdf, with timings.</p>
</section>
