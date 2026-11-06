---
title: pyropdf
description: PDFs, with Python ergonomics and a Rust core. Read, render, edit, and sign PDF from Python; every edit is a version that can be undone, and a failed one leaves the document as it was.
home: true
hide:
  - navigation
  - toc
---

<section class="pp-hero" aria-labelledby="pp-hero-title">
  <h1 id="pp-hero-title">PDFs, with <span class="pp-accent">Python ergonomics</span> and a <span class="pp-accent">Rust core</span>.</h1>
<div class="pp-hero__aside">
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
</section>

<section class="pp-demo" aria-label="pyropdf, step by step on one invoice" markdown>
<div class="pp-reel" markdown>
  <div class="pp-reel__steps" role="radiogroup" aria-label="Steps">
    <input type="radio" name="pp-step" id="pp-s1" value="1" checked>
    <label for="pp-s1"><span class="pp-reel__n">1</span><span class="pp-reel__title">Read</span><span class="pp-reel__desc">Every word, with its box, in reading order.</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <input type="radio" name="pp-step" id="pp-s2" value="2">
    <label for="pp-s2"><span class="pp-reel__n">2</span><span class="pp-reel__title">Annotate</span><span class="pp-reel__desc">Search the page and highlight each match.</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <input type="radio" name="pp-step" id="pp-s3" value="3">
    <label for="pp-s3"><span class="pp-reel__n">3</span><span class="pp-reel__title">Fill a form</span><span class="pp-reel__desc">Add a field and set it; the appearance follows.</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <input type="radio" name="pp-step" id="pp-s4" value="4">
    <label for="pp-s4"><span class="pp-reel__n">4</span><span class="pp-reel__title">Stamp</span><span class="pp-reel__desc">A rubber stamp, drawn in its own appearance.</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
  </div>
<div class="pp-reel__code" markdown>
<div class="pp-reel__panel" data-step="1" markdown>

```python title="read.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    words = page.words()
    for word in words[:4]:
        print(word.text, *(round(v) for v in word.bbox))
    print(len(words), "words")
```

<p class="pp-reel__label">Output</p>

```text
--8<-- "en/assets/hero/read.txt"
```

</div>
<div class="pp-reel__panel" data-step="2" markdown>

```python title="annotate.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("highlight totals"):
        hits = page.search("total")
        page.annotations.add(pyropdf.Highlight(hits))
    doc.save("output.pdf", incremental=True)
```

</div>
<div class="pp-reel__panel" data-step="3" markdown>

```python title="fill.py"
import pyropdf
from pyropdf import TextField

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("approve"):
        field = TextField("approver", (30, 316, 170, 334))
        page.widgets.add(field)
        doc.form["approver"].value = "Ada Lovelace"
    doc.save("output.pdf", incremental=True)
```

</div>
<div class="pp-reel__panel" data-step="4" markdown>

```python title="stamp.py"
import pyropdf
from pyropdf import Stamp, StampIcon

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("stamp"):
        box = (196, 300, 310, 346)
        stamp = Stamp(box, icon=StampIcon.APPROVED)
        page.annotations.add(stamp)
    doc.save("output.pdf", incremental=True)
```

</div>
</div>
<div class="pp-viewer" role="group" aria-label="A PDF viewer showing pyropdf's renders of each revision of the invoice">
  <div class="pp-viewer__bar">
    <span class="pp-viewer__file"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 1.5h5.5L13 5v9.5H4z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/><path d="M9.5 1.5V5H13" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg>invoice.pdf</span>
    <span class="pp-viewer__revs"><span class="pp-reel__rev" data-step="1">rev 1 · opened</span><span class="pp-reel__rev" data-step="2">rev 2 · highlight totals</span><span class="pp-reel__rev" data-step="3">rev 3 · approve</span><span class="pp-reel__rev" data-step="4">rev 4 · stamp</span></span>
    <span class="pp-viewer__page" aria-label="Page 1 of 1">1 / 1</span>
    <a class="pp-viewer__open" href="/assets/hero/reviewed.pdf" title="Open reviewed.pdf" aria-label="Open reviewed.pdf"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M9 2.5h4.5V7M13.5 2.5 7.5 8.5M12 9.5v4H2.5V4h4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg></a>
  </div>
  <div class="pp-viewer__canvas">
    <figure class="pp-viewer__sheet">
      <img class="pp-reel__frame" data-step="1" src="/assets/hero/revision-1.png" width="1057" height="1375" alt="The invoice as pyropdf renders it, with the boxes of the words that page.words() found">
      <img class="pp-reel__frame" data-step="2" src="/assets/hero/revision-2.png" width="1057" height="1375" alt="The invoice with its Total highlighted, as pyropdf renders it">
      <img class="pp-reel__frame" data-step="3" src="/assets/hero/revision-3.png" width="1057" height="1375" alt="The invoice with an Approved by field filled in, as pyropdf renders it">
      <img class="pp-reel__frame" data-step="4" src="/assets/hero/revision-4.png" width="1057" height="1375" alt="The invoice with an Approved stamp, as pyropdf renders it">
      <img class="pp-reel__words" data-step="1" src="/assets/hero/words.svg" width="340" height="442" alt="">
    </figure>
  </div>
</div>
<p class="pp-stage__caption">Every frame is pyropdf's own render. <a href="https://github.com/yhay81/pyropdf/blob/main/tools/hero_invoice.py">tools/hero_invoice.py</a> runs these four snippets in turn, each on the last one's output, and <a href="/assets/hero/reviewed.pdf">reviewed.pdf</a> keeps all four revisions.</p>
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
