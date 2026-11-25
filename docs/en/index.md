---
description: PDFs, with Python ergonomics and a Rust core. Read, render, edit, and sign PDF from Python; every edit is a version that can be undone, and a failed one leaves the document as it was.
home: true
hide:
  - navigation
  - toc
---

<section class="pp-hero pp-reel" aria-labelledby="pp-hero-title" markdown>
<div class="pp-hero__text">
  <h1 id="pp-hero-title">PDFs, with <span class="pp-accent">Python ergonomics</span> and a <span class="pp-accent">Rust core</span>.</h1>
  <p class="pp-lede">
    pyropdf reads, renders, edits, and signs PDF. Pages are a sequence, form fields a mapping, and edits a transaction; underneath, a versioned store in Rust makes every change undoable.
  </p>
  <div class="pp-hero__start">
    <div class="pp-install" aria-label="Installation command">
    <span class="pp-install__prompt" aria-hidden="true">$</span>
    <code tabindex="0">pip install pyropdf</code>
    <button type="button" data-copy-command="pip install pyropdf" data-copied-label="Copied" aria-label="Copy the installation command"><span data-copy-label>Copy</span></button>
    </div>
    <p class="pp-actions">
    <a class="pp-button pp-button--primary" href="getting-started/">Get started</a>
    <a class="pp-button pp-button--video" href="#pp-overview" data-jump-video><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.25 12.5 8 5 12.75z" fill="currentColor"/></svg>30s tour</a>
    <a class="pp-button" href="api/">API reference</a>
  </p>
  </div>
</div>
  <div class="pp-reel__steps" role="radiogroup" aria-label="Steps">
    <input type="radio" name="pp-step" id="pp-s1" value="1" checked>
    <label for="pp-s1"><span class="pp-reel__n">1</span><span class="pp-reel__title">Read</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <input type="radio" name="pp-step" id="pp-s2" value="2">
    <label for="pp-s2"><span class="pp-reel__n">2</span><span class="pp-reel__title">Annotate</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <input type="radio" name="pp-step" id="pp-s3" value="3">
    <label for="pp-s3"><span class="pp-reel__n">3</span><span class="pp-reel__title">Fill a form</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <input type="radio" name="pp-step" id="pp-s4" value="4">
    <label for="pp-s4"><span class="pp-reel__n">4</span><span class="pp-reel__title">Stamp</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <button type="button" class="pp-reel__toggle" data-reel-toggle data-state="playing" data-pause-label="Pause automatic step cycle" data-play-label="Resume automatic step cycle" aria-label="Pause automatic step cycle" title="Pause automatic step cycle">
      <svg class="pp-reel__icon-pause" viewBox="0 0 16 16" aria-hidden="true"><rect x="4" y="3" width="2.75" height="10" rx="0.8" fill="currentColor"/><rect x="9.25" y="3" width="2.75" height="10" rx="0.8" fill="currentColor"/></svg>
      <svg class="pp-reel__icon-play" viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.25 12.5 8 5 12.75z" fill="currentColor"/></svg>
    </button>
  </div>
<div class="pp-reel__code" markdown>
<div class="pp-reel__panel" data-step="1" markdown>

```python title="read.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    words = doc.pages[0].words()
    for word in words[:3]:
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

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("approve"):
        field = pyropdf.TextField("approver", (30, 316, 170, 334))
        page.widgets.add(field)
        doc.form["approver"].value = "Ada Lovelace"
    doc.save("output.pdf", incremental=True)
```

</div>
<div class="pp-reel__panel" data-step="4" markdown>

```python title="stamp.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("stamp"):
        box = (196, 300, 310, 346)
        stamp = pyropdf.Stamp(box, icon=pyropdf.StampIcon.APPROVED)
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
<div class="pp-hero__foot">
  <ul class="pp-facts" aria-label="Project facts">
    <li>MIT licensed</li>
    <li>No required dependencies</li>
    <li>CPython 3.11–3.15, free-threaded too</li>
  </ul>
<p class="pp-stage__caption">Every frame is pyropdf's own render. <a href="https://github.com/yhay81/pyropdf/blob/main/tools/hero_invoice.py">tools/hero_invoice.py</a> runs these four snippets in turn, each on the last one's output, and <a href="/assets/hero/reviewed.pdf">reviewed.pdf</a> keeps all four revisions.</p>
</div>
</section>

<section class="pp-section pp-showcase">
  <div class="pp-showcase__head">
    <header class="pp-section__head">
      <p class="pp-eyebrow">Overview video</p>
      <h2 id="pp-overview">See pyropdf in 30 seconds</h2>
      <p>Watch the versioned store in motion — from live word extraction and form stamping to O(1) undo, exception rollback, and byte-preserving saves.</p>
    </header>
  </div>
  <div class="pp-showcase__stage">
    <figure class="pp-video-figure">
      <div class="pp-video-frame">
        <div class="pp-video-frame__bar">
          <span class="pp-video-frame__title"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M13.5 0.67s.74 2.65.74 4.8c0 2.06-1.35 3.73-3.41 3.73-2.07 0-3.63-1.67-3.63-3.73l.03-.36C5.21 7.51 4 10.62 4 14c0 4.42 3.58 8 8 8s8-3.58 8-8C20 8.61 17.41 3.8 13.5.67zM11.71 19c-1.78 0-3.22-1.4-3.22-3.14 0-1.62 1.05-2.76 2.81-3.12 1.77-.36 3.6-1.21 4.62-2.58.39 1.29.59 2.65.59 4.04 0 2.65-2.15 4.8-4.8 4.8z"/></svg>pyropdf overview</span>
          <span class="pp-video-frame__spec">0:33 · English audio &amp; captions</span>
        </div>
        <video
          controls
          playsinline
          loading="lazy"
          preload="none"
          width="1280"
          height="720"
          poster="/assets/video/pyropdf-intro-poster.webp"
          aria-labelledby="pp-overview"
          aria-details="pp-intro-transcript"
        >
          <source src="/assets/video/pyropdf-intro.webm" type="video/webm">
          <source src="/assets/video/pyropdf-intro.mp4" type="video/mp4">
          <track src="/assets/video/pyropdf-intro-en.vtt" kind="captions" srclang="en" label="English">
          <p>Your browser does not support embedded video. <a href="/assets/video/pyropdf-intro.mp4">Download the pyropdf overview video (MP4)</a>.</p>
        </video>
      </div>
      <figcaption>Narrated 30-second tour featuring real pyropdf invoice renders, version DAG undo/redo, rollback on failure, and incremental byte streams.</figcaption>
    </figure>
    <div class="pp-showcase__rail">
      <p class="pp-rail__heading">Jump to a chapter</p>
      <div class="pp-chapters" role="group" aria-label="Video chapters">
        <button type="button" class="pp-chapter is-active" data-video-seek="2.89">
          <span class="pp-chapter__time">0:03</span>
          <span class="pp-chapter__body">
            <strong>Python ergonomics, Rust core</strong>
            <span>One unified architecture instead of fragile tools</span>
          </span>
        </button>
        <button type="button" class="pp-chapter" data-video-seek="6.53">
          <span class="pp-chapter__time">0:06</span>
          <span class="pp-chapter__body">
            <strong>Read, highlight, fill &amp; stamp</strong>
            <span>Live invoice edits &amp; hayro previews from one store</span>
          </span>
        </button>
        <button type="button" class="pp-chapter" data-video-seek="14.55">
          <span class="pp-chapter__time">0:14</span>
          <span class="pp-chapter__body">
            <strong>Atomic undo &amp; rollback</strong>
            <span>Zero-copy history steps and exception safety</span>
          </span>
        </button>
        <button type="button" class="pp-chapter" data-video-seek="22.38">
          <span class="pp-chapter__time">0:22</span>
          <span class="pp-chapter__body">
            <strong>Incremental saves &amp; MIT</strong>
            <span>Byte-locked originals, valid PAdES signatures</span>
          </span>
        </button>
      </div>
      <details id="pp-intro-transcript" class="pp-transcript">
        <summary>View transcript</summary>
        <div class="pp-transcript__body">
          <p class="pp-transcript__cue">[Warm ambient synth pulse begins]</p>
          <p>Working with PDFs in Python shouldn't feel fragile. Meet pyropdf — Python ergonomics, powered by a Rust core.</p>
          <p>Read text, tables, and Markdown. Highlight matches, fill forms, stamp pages, and render live previews, all from one versioned object store.</p>
          <p>Every edit runs in an atomic transaction. Undo or redo in an instant, and if anything fails, your document stays untouched.</p>
          <p>Save incrementally to keep every original byte and signature. Zero required dependencies. MIT licensed. <code>pip install pyropdf</code>.</p>
          <p class="pp-transcript__cue">[Warm synth chord resolves and fades]</p>
        </div>
      </details>
    </div>
  </div>
</section>

<section class="pp-section">
  <header class="pp-section__head">
    <p class="pp-eyebrow">Capabilities</p>
    <h2 id="pp-features">One package for the whole document</h2>
    <p>Reading, rendering, and editing share one store, so what you read is always the version you just edited.</p>
  </header>
  <div class="pp-cards">
    <a class="pp-card" href="reading/">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3.5h7l3.5 3.5v9.5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-12a1 1 0 0 1 1-1Z"/><path d="M12 3.5V7h3.5M7 10.5h6M7 13.5h4.5"/></svg></span>
        <h3>Read</h3>
        <p>Text in reading order, words, characters, layout, search, tables, and Markdown.</p>
        <code>page.markdown()</code>
      </article>
    </a>
    <a class="pp-card" href="reading/#offline-ocr">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 7V4.5a1 1 0 0 1 1-1H7M13 3.5h2.5a1 1 0 0 1 1 1V7M16.5 13v2.5a1 1 0 0 1-1 1H13M7 16.5H4.5a1 1 0 0 1-1-1V13"/><path d="M6.5 10h7M8 7.5h4M8 12.5h4"/></svg></span>
        <h3>Recognize scans</h3>
        <p>Offline OCR with the PP-OCR models of the <code>ocr</code> extra, written back as an invisible text layer.</p>
        <code>page.apply_ocr(engine=OcrEngine())</code>
      </article>
    </a>
    <a class="pp-card" href="rendering/">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="14" height="12" rx="2"/><circle cx="7.5" cy="8.25" r="1.25"/><path d="m17 13-3.5-3.5L7.5 15.5"/></svg></span>
        <h3>Render</h3>
        <p>Pixmaps, PNG, JPEG, and SVG through hayro, and batches of pages in parallel.</p>
        <code>page.pixmap(dpi=144)</code>
      </article>
    </a>
    <a class="pp-card" href="editing/#pages">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="8.5" height="11" rx="1.5"/><path d="M8.5 4V3a1 1 0 0 1 1-1H15a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1h-2.5"/></svg></span>
        <h3>Edit pages</h3>
        <p>Insert, import, move, rotate, crop, and flatten pages; edit metadata, labels, outlines, and layers.</p>
        <code>page.rotation = 90</code>
      </article>
    </a>
    <a class="pp-card" href="editing/#drawing">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="m13.5 3.5 3 3-8.5 8.5-3.5.5.5-3.5 8.5-8.5Z"/><path d="m11.5 5.5 3 3"/></svg></span>
        <h3>Draw and annotate</h3>
        <p>Paths, images, pages, and text; highlights, notes, shapes, links, free text, and stamps.</p>
        <code>page.draw()</code>
      </article>
    </a>
    <a class="pp-card" href="editing/#forms">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="14" height="12" rx="2"/><path d="M6.5 8.5h7M6.5 12h4.5"/></svg></span>
        <h3>Fill forms</h3>
        <p>Read and set field values, add widgets, and keep their appearances current.</p>
        <code>doc.form["name"].value = "Ada"</code>
      </article>
    </a>
    <a class="pp-card" href="versions/#saving">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2.75 16 5v4.75c0 3.7-2.55 6.45-6 7.5-3.45-1.05-6-3.8-6-7.5V5l6-2.25Z"/><path d="m7.5 10 1.75 1.75L13 8"/></svg></span>
        <h3>Save and sign</h3>
        <p>Incremental saves that append only what changed, AES-256 encryption, and PAdES signatures in a new revision.</p>
        <code>doc.sign("approval", signer)</code>
      </article>
    </a>
    <a class="pp-card" href="editing/#sanitizing">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 3v3M10 14v3M3 10h3M14 10h3M5.2 5.2l2.1 2.1M12.7 12.7l2.1 2.1M14.8 5.2l-2.1 2.1M7.3 12.7l-2.1 2.1"/></svg></span>
        <h3>Clean and shrink</h3>
        <p>Remove JavaScript, attachments, metadata, and hidden text before sharing, and subset fonts.</p>
        <code>doc.sanitize("shared.pdf")</code>
      </article>
    </a>
  </div>
</section>

<section class="pp-section pp-split" markdown>
<div class="pp-split__text">
  <header class="pp-section__head">
    <p class="pp-eyebrow">Architecture</p>
    <h2 id="pp-versions">Built on versions</h2>
    <p>Every edit writes a new version of the document instead of changing a parsed copy in place. The guarantees follow from that.</p>
  </header>
  <ul class="pp-points">
    <li><span class="pp-points__n" aria-hidden="true">01</span><span class="pp-points__body"><strong>An edit costs what it touches.</strong>Engines read the store, not a reserialized file: rotating a page writes one object.</span></li>
    <li><span class="pp-points__n" aria-hidden="true">02</span><span class="pp-points__body"><strong>Every change can be undone.</strong>A transaction is one named step of <code>doc.history</code>, and undo moves between versions without copying the document.</span></li>
    <li><span class="pp-points__n" aria-hidden="true">03</span><span class="pp-points__body"><strong>A failure leaves no trace.</strong>An edit that raises keeps the version it started from, and limits stop any operation's work with a typed <code>LimitError</code>.</span></li>
  </ul>
  <p class="pp-more"><a href="versions/">Versions and saving</a><a href="concepts/">Concepts</a></p>
</div>
<div class="pp-split__code pp-version-card" markdown>
<div class="pp-version-card__bar" aria-hidden="true">
  <span class="pp-vnode">v0 <em>opened</em></span>
  <span class="pp-varrow">→</span>
  <span class="pp-vnode pp-vnode--active">v1 <em>rotate</em></span>
  <span class="pp-varrow">↶</span>
  <span class="pp-vnode pp-vnode--undo">v0 <em>undone</em></span>
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
</section>

<section class="pp-section pp-closing">
  <div class="pp-closing__box">
    <div class="pp-closing__text">
      <p class="pp-eyebrow">Status &amp; transparency</p>
      <h2 id="pp-adopt">Before you adopt it</h2>
      <p>The API may still change between minor versions, and rendering is slower than MuPDF's. No pyropdf release claims safety for untrusted input before hayro's bounded construction is released; <a href="status/">Status and safety</a> says what the limits do and do not promise. <a href="choosing/">Choosing pyropdf</a> compares it with PyMuPDF, pypdf, and pikepdf, with timings.</p>
    </div>
    <div class="pp-closing__links">
      <a class="pp-button pp-button--primary" href="getting-started/">Start the guide</a>
      <a class="pp-button" href="choosing/">Compare libraries</a>
    </div>
  </div>
</section>
