---
title: pyropdf
description: Complete control of PDF from Python, on one versioned object store in Rust. Every edit is a version, every change can be undone, and a failed transaction leaves the document as it was.
hide:
  - navigation
  - toc
---

<section class="pp-hero" aria-labelledby="pp-hero-title">
  <div>
    <p class="pp-eyebrow">0.1 alpha · the successor of pylopdf</p>
    <h1 id="pp-hero-title">Every PDF edit is a <em>version</em>.</h1>
    <p class="pp-lede">
      pyropdf reads, renders, and edits PDF from Python on one versioned object store in Rust.
      An edit costs what it touches, every change can be undone, and a failed transaction leaves the document as it was.
    </p>
    <div class="pp-actions">
      <a class="pp-button pp-button--primary" href="getting-started/">Get started →</a>
      <a class="pp-button" href="api/">API reference</a>
    </div>
    <div class="pp-install" aria-label="Installation command">
      <span class="pp-install__prompt" aria-hidden="true">$</span>
      <code tabindex="0">pip install pyropdf</code>
      <button type="button" data-copy-command="pip install pyropdf" data-copied-label="Copied" aria-label="Copy the installation command"><span data-copy-label>Copy</span></button>
    </div>
    <ul class="pp-facts" aria-label="Project facts">
      <li>MIT licensed</li>
      <li>No required dependencies</li>
      <li>Python 3.11–3.15, free-threaded too</li>
    </ul>
  </div>
  <div class="pp-stack" role="img" aria-label="Three saved versions of a document stacked on each other, each with the edit that made it, and an undo for the newest">
    <div class="pp-version" aria-hidden="true">
      <div class="pp-version__head"><span><b>v1</b> · opened</span><span>12 pages</span></div>
      <span class="pp-version__line">trailer /Root 1 0 R</span>
      <span class="pp-version__line">xref · 214 objects</span>
    </div>
    <div class="pp-version" aria-hidden="true">
      <div class="pp-version__head"><span><b>v2</b> · "rotate"</span><span>1 object</span></div>
      <span class="pp-version__line pp-version__line--change">7 0 obj /Rotate 90</span>
      <span class="pp-version__line">pages[0].rotation = 90</span>
    </div>
    <div class="pp-version" aria-hidden="true">
      <div class="pp-version__head"><span><b>v3</b> · "annotate"</span><span>2 objects</span></div>
      <span class="pp-version__line pp-version__line--add">215 0 obj /Subtype /Highlight</span>
      <span class="pp-version__line pp-version__line--change">7 0 obj /Annots [215 0 R]</span>
      <span class="pp-version__line">page.annotations.add(Highlight(hits))</span>
      <div class="pp-version__foot">
        <span class="pp-chip pp-chip--ember">history.undo()</span>
        <span class="pp-chip pp-chip--moss">save(incremental=True)</span>
        <span class="pp-chip">+2 objects</span>
      </div>
    </div>
  </div>
</section>

<section class="pp-principles" aria-label="What the object store guarantees">
  <article class="pp-principle">
    <span class="pp-principle__number">01</span>
    <h2>An edit costs what it touches</h2>
    <p>Engines read the store, not a reserialized file. Rotating a page writes one object, and the next page read serializes only what the edit wrote.</p>
  </article>
  <article class="pp-principle">
    <span class="pp-principle__number">02</span>
    <h2>Every change can be undone</h2>
    <p>Each transaction becomes a named step in <code>doc.history</code>. Undo and redo move between versions without copying the document.</p>
  </article>
  <article class="pp-principle">
    <span class="pp-principle__number">03</span>
    <h2>Failures leave no trace</h2>
    <p>An edit that raises leaves the current version as it was. Limits bound every operation's work and fail with a typed <code>LimitError</code>.</p>
  </article>
</section>

<div class="pp-section-title">
<h2>What it does today</h2>
<p>The 0.1 releases cover reading, rendering, and editing. Drawing with <code>page.draw</code> comes later.</p>
</div>

<div class="pp-cards">
  <a class="pp-card" href="reading/">
    <h3>Read text and structure</h3>
    <p>Text in reading order, words, characters, layout, search, tables, and Markdown.</p>
    <code>page.search("total")</code>
  </a>
  <a class="pp-card" href="reading/#offline-ocr">
    <h3>Offline OCR</h3>
    <p>Recognize scanned pages with the PP-OCR models of the <code>ocr</code> extra, and add an invisible text layer.</p>
    <code>page.ocr(engine=OcrEngine())</code>
  </a>
  <a class="pp-card" href="rendering/">
    <h3>Render</h3>
    <p>Pixmaps, PNG, JPEG, and SVG through hayro, with parallel batch rendering.</p>
    <code>page.pixmap(dpi=144)</code>
  </a>
  <a class="pp-card" href="editing/">
    <h3>Edit pages and annotations</h3>
    <p>Insert, import, move, rotate, and flatten pages; add highlights, notes, shapes, and links.</p>
    <code>doc.pages.insert(0, other.pages)</code>
  </a>
  <a class="pp-card" href="editing/#forms">
    <h3>Fill and build forms</h3>
    <p>Read and set field values, add widgets, and keep appearances settled.</p>
    <code>doc.form["name"].value = "Ada"</code>
  </a>
  <a class="pp-card" href="versions/">
    <h3>Save precisely</h3>
    <p>Incremental saves that append only the changed objects, full rewrites, and AES-256 encryption.</p>
    <code>doc.save(path, incremental=True)</code>
  </a>
</div>

<div class="pp-split" markdown>
<div class="pp-section-title">
<h2>Transactions, not snapshots</h2>
<p>Group edits into a named step. If the block raises, nothing of it stays; if it completes, one undo takes it back.</p>
<p><a href="versions/">Versions and saving →</a></p>
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

<div class="pp-status">
  <p><strong>Alpha.</strong> The API may change between minor versions. No pyropdf release claims safety for untrusted input before hayro's bounded construction is released; <a href="status/">status and safety</a> explains what the limits do and do not promise.</p>
</div>
