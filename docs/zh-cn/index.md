---
description: 用 Python 的优雅体验与 Rust 内核处理 PDF。在 Python 中读取、渲染、编辑和签署 PDF；每次编辑都生成一个可撤销的版本，失败的操作不会改变原文档。
home: true
hide:
  - navigation
  - toc
---

<section class="pp-hero pp-reel" aria-labelledby="pp-hero-title" markdown>
<div class="pp-hero__text">
  <h1 id="pp-hero-title">用 <span class="pp-accent">Python 的优雅体验</span>与 <span class="pp-accent">Rust 内核</span>处理 PDF。</h1>
  <p class="pp-lede">
    pyropdf 可读取、渲染、编辑和签署 PDF。页面是序列，表单字段是映射，编辑是事务；底层由 Rust 实现的版本化对象存储让每次修改都可撤销。
  </p>
  <div class="pp-hero__start">
    <div class="pp-install" aria-label="安装命令">
    <span class="pp-install__prompt" aria-hidden="true">$</span>
    <code tabindex="0">pip install pyropdf</code>
    <button type="button" data-copy-command="pip install pyropdf" data-copied-label="已复制" aria-label="复制安装命令"><span data-copy-label>复制</span></button>
    </div>
    <p class="pp-actions">
    <a class="pp-button pp-button--primary" href="getting-started/">快速开始</a>
    <a class="pp-button pp-button--video" href="#pp-overview" data-jump-video><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.25 12.5 8 5 12.75z" fill="currentColor"/></svg>30 秒导览</a>
    <a class="pp-button" href="/api/" hreflang="en">API 参考</a>
  </p>
  </div>
</div>
  <div class="pp-reel__steps" role="radiogroup" aria-label="步骤">
    <input type="radio" name="pp-step" id="pp-s1" value="1" checked>
    <label for="pp-s1"><span class="pp-reel__n">1</span><span class="pp-reel__title">读取</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <input type="radio" name="pp-step" id="pp-s2" value="2">
    <label for="pp-s2"><span class="pp-reel__n">2</span><span class="pp-reel__title">批注</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <input type="radio" name="pp-step" id="pp-s3" value="3">
    <label for="pp-s3"><span class="pp-reel__n">3</span><span class="pp-reel__title">填写表单</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <input type="radio" name="pp-step" id="pp-s4" value="4">
    <label for="pp-s4"><span class="pp-reel__n">4</span><span class="pp-reel__title">盖章</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <button type="button" class="pp-reel__toggle" data-reel-toggle data-state="playing" data-pause-label="暂停自动切换步骤" data-play-label="恢复自动切换步骤" aria-label="暂停自动切换步骤" title="暂停自动切换步骤">
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

<p class="pp-reel__label">输出</p>

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
<div class="pp-viewer" role="group" aria-label="展示 pyropdf 所渲染发票各修订版的 PDF 查看器">
  <div class="pp-viewer__bar">
    <span class="pp-viewer__file"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 1.5h5.5L13 5v9.5H4z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/><path d="M9.5 1.5V5H13" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg>invoice.pdf</span>
    <span class="pp-viewer__revs"><span class="pp-reel__rev" data-step="1">rev 1 · 已打开</span><span class="pp-reel__rev" data-step="2">rev 2 · highlight totals</span><span class="pp-reel__rev" data-step="3">rev 3 · approve</span><span class="pp-reel__rev" data-step="4">rev 4 · stamp</span></span>
    <span class="pp-viewer__page" aria-label="第 1 页，共 1 页">1 / 1</span>
    <a class="pp-viewer__open" href="/assets/hero/reviewed.pdf" title="打开 reviewed.pdf" aria-label="打开 reviewed.pdf"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M9 2.5h4.5V7M13.5 2.5 7.5 8.5M12 9.5v4H2.5V4h4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg></a>
  </div>
  <div class="pp-viewer__canvas">
    <figure class="pp-viewer__sheet">
      <img class="pp-reel__frame" data-step="1" src="/assets/hero/revision-1.png" width="1057" height="1375" alt="由 pyropdf 渲染的发票页面，标注了 page.words() 识别出的单词边界框">
      <img class="pp-reel__frame" data-step="2" src="/assets/hero/revision-2.png" width="1057" height="1375" alt="由 pyropdf 渲染的发票页面，其中 Total 字样已高亮标出">
      <img class="pp-reel__frame" data-step="3" src="/assets/hero/revision-3.png" width="1057" height="1375" alt="由 pyropdf 渲染的发票页面，已填写 Approved by 表单字段">
      <img class="pp-reel__frame" data-step="4" src="/assets/hero/revision-4.png" width="1057" height="1375" alt="由 pyropdf 渲染的发票页面，已加盖 Approved 图章">
      <img class="pp-reel__words" data-step="1" src="/assets/hero/words.svg" width="340" height="442" alt="">
    </figure>
  </div>
</div>
<div class="pp-hero__foot">
  <ul class="pp-facts" aria-label="项目概览">
    <li>MIT 开源协议</li>
    <li>无必需依赖</li>
    <li>支持 CPython 3.11–3.15（含自由线程构建）</li>
  </ul>
<p class="pp-stage__caption">每一帧均由 pyropdf 亲自渲染。<a href="https://github.com/yhay81/pyropdf/blob/main/tools/hero_invoice.py">tools/hero_invoice.py</a> 依次运行以上四段代码，每一步以前一步的输出作为输入，而 <a href="/assets/hero/reviewed.pdf">reviewed.pdf</a> 完整保留了全部四个修订版。</p>
</div>
</section>

<section class="pp-section pp-showcase">
  <div class="pp-showcase__head">
    <header class="pp-section__head">
      <p class="pp-eyebrow">概览视频</p>
      <h2 id="pp-overview">30 秒了解 pyropdf</h2>
      <p>观看版本化对象存储的实际运作——从实时单词提取与表单盖章，到 O(1) 撤销、异常回滚以及保留原始字节的增量保存（英语旁白与字幕）。</p>
    </header>
  </div>
  <div class="pp-showcase__stage">
    <figure class="pp-video-figure">
      <div class="pp-video-frame">
        <div class="pp-video-frame__bar">
          <span class="pp-video-frame__title"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M13.5 0.67s.74 2.65.74 4.8c0 2.06-1.35 3.73-3.41 3.73-2.07 0-3.63-1.67-3.63-3.73l.03-.36C5.21 7.51 4 10.62 4 14c0 4.42 3.58 8 8 8s8-3.58 8-8C20 8.61 17.41 3.8 13.5.67zM11.71 19c-1.78 0-3.22-1.4-3.22-3.14 0-1.62 1.05-2.76 2.81-3.12 1.77-.36 3.6-1.21 4.62-2.58.39 1.29.59 2.65.59 4.04 0 2.65-2.15 4.8-4.8 4.8z"/></svg>pyropdf 概览</span>
          <span class="pp-video-frame__spec">0:33 · 英语音频与字幕</span>
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
          <track src="/assets/video/pyropdf-intro-en.vtt" kind="captions" srclang="en" label="English" default>
          <p>您的浏览器不支持嵌入式视频。<a href="/assets/video/pyropdf-intro.mp4">下载 pyropdf 概览视频（MP4）</a>。</p>
        </video>
      </div>
      <figcaption>30 秒旁白导览，演示 pyropdf 真实的发票渲染、基于版本 DAG 的撤销/重做、失败时回滚以及增量字节流（英语音频与英文字幕）。</figcaption>
    </figure>
    <div class="pp-showcase__rail">
      <p class="pp-rail__heading">跳转到章节</p>
      <div class="pp-chapters" role="group" aria-label="视频章节">
        <button type="button" class="pp-chapter is-active" data-video-seek="2.89">
          <span class="pp-chapter__time">0:03</span>
          <span class="pp-chapter__body">
            <strong>Python 体验，Rust 内核</strong>
            <span>一套统一架构，替代拼凑脆弱的工具链</span>
          </span>
        </button>
        <button type="button" class="pp-chapter" data-video-seek="6.53">
          <span class="pp-chapter__time">0:06</span>
          <span class="pp-chapter__body">
            <strong>读取、高亮、填表与盖章</strong>
            <span>基于同一存储实时编辑发票并由 hayro 预览</span>
          </span>
        </button>
        <button type="button" class="pp-chapter" data-video-seek="14.55">
          <span class="pp-chapter__time">0:14</span>
          <span class="pp-chapter__body">
            <strong>原子撤销与回滚</strong>
            <span>零拷贝历史步进与异常安全保障</span>
          </span>
        </button>
        <button type="button" class="pp-chapter" data-video-seek="22.38">
          <span class="pp-chapter__time">0:22</span>
          <span class="pp-chapter__body">
            <strong>增量保存与 MIT 协议</strong>
            <span>锁定原始字节，保持 PAdES 签名有效</span>
          </span>
        </button>
      </div>
      <details id="pp-intro-transcript" class="pp-transcript">
        <summary>查看字幕稿（英文）</summary>
        <div class="pp-transcript__body" lang="en">
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
    <p class="pp-eyebrow">核心能力</p>
    <h2 id="pp-features">一个包覆盖完整文档工作流</h2>
    <p>读取、渲染和编辑共用同一个对象存储，因此你读到的始终是刚刚编辑后的版本。</p>
  </header>
  <div class="pp-cards">
    <a class="pp-card" href="reading/">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3.5h7l3.5 3.5v9.5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-12a1 1 0 0 1 1-1Z"/><path d="M12 3.5V7h3.5M7 10.5h6M7 13.5h4.5"/></svg></span>
        <h3>读取</h3>
        <p>按阅读顺序提取文本、单词、字符、版面布局、搜索结果、表格与 Markdown。</p>
        <code>page.markdown()</code>
      </article>
    </a>
    <a class="pp-card" href="reading/#offline-ocr">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 7V4.5a1 1 0 0 1 1-1H7M13 3.5h2.5a1 1 0 0 1 1 1V7M16.5 13v2.5a1 1 0 0 1-1 1H13M7 16.5H4.5a1 1 0 0 1-1-1V13"/><path d="M6.5 10h7M8 7.5h4M8 12.5h4"/></svg></span>
        <h3>识别扫描件</h3>
        <p>使用 <code>ocr</code> 可选扩展提供的 PP-OCR 模型进行离线 OCR，并将结果写回为不可见文本层。</p>
        <code>page.apply_ocr(engine=OcrEngine())</code>
      </article>
    </a>
    <a class="pp-card" href="rendering/">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="14" height="12" rx="2"/><circle cx="7.5" cy="8.25" r="1.25"/><path d="m17 13-3.5-3.5L7.5 15.5"/></svg></span>
        <h3>渲染</h3>
        <p>通过 hayro 输出像素图、PNG、JPEG 与 SVG，并支持多页并行批量渲染。</p>
        <code>page.pixmap(dpi=144)</code>
      </article>
    </a>
    <a class="pp-card" href="editing/#pages">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="8.5" height="11" rx="1.5"/><path d="M8.5 4V3a1 1 0 0 1 1-1H15a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1h-2.5"/></svg></span>
        <h3>编辑页面</h3>
        <p>插入、导入、移动、旋转、裁剪与拼合页面；编辑元数据、页码标签、大纲和图层。</p>
        <code>page.rotation = 90</code>
      </article>
    </a>
    <a class="pp-card" href="editing/#drawing">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="m13.5 3.5 3 3-8.5 8.5-3.5.5.5-3.5 8.5-8.5Z"/><path d="m11.5 5.5 3 3"/></svg></span>
        <h3>绘图与批注</h3>
        <p>绘制路径、图像、页面和文字；添加高亮、便签、图形、链接、自由文本与图章。</p>
        <code>page.draw()</code>
      </article>
    </a>
    <a class="pp-card" href="editing/#forms">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="14" height="12" rx="2"/><path d="M6.5 8.5h7M6.5 12h4.5"/></svg></span>
        <h3>填写表单</h3>
        <p>读写字段值、添加表单控件，并同步保持其外观处于最新状态。</p>
        <code>doc.form["name"].value = "Ada"</code>
      </article>
    </a>
    <a class="pp-card" href="versions/#saving">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2.75 16 5v4.75c0 3.7-2.55 6.45-6 7.5-3.45-1.05-6-3.8-6-7.5V5l6-2.25Z"/><path d="m7.5 10 1.75 1.75L13 8"/></svg></span>
        <h3>保存与签名</h3>
        <p>仅追加变动对象的增量保存、AES-256 加密，以及写入新修订版的 PAdES 数字签名。</p>
        <code>doc.sign("approval", signer)</code>
      </article>
    </a>
    <a class="pp-card" href="editing/#sanitizing">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 3v3M10 14v3M3 10h3M14 10h3M5.2 5.2l2.1 2.1M12.7 12.7l2.1 2.1M14.8 5.2l-2.1 2.1M7.3 12.7l-2.1 2.1"/></svg></span>
        <h3>清理与瘦身</h3>
        <p>分享前移除 JavaScript、附件、元数据和隐藏文本，并对嵌入字体进行子集化。</p>
        <code>doc.sanitize("shared.pdf")</code>
      </article>
    </a>
  </div>
</section>

<section class="pp-section pp-split" markdown>
<div class="pp-split__text">
  <header class="pp-section__head">
    <p class="pp-eyebrow">架构设计</p>
    <h2 id="pp-versions">构建于版本之上</h2>
    <p>每次编辑都会写入文档的新版本，而非原地修改解析后的副本。所有可靠性保障均由此自然衍生。</p>
  </header>
  <ul class="pp-points">
    <li><span class="pp-points__n" aria-hidden="true">01</span><span class="pp-points__body"><strong>编辑开销只与改动范围成正比。</strong>各引擎直接读取对象存储，而不是重新序列化后的文件：旋转一页仅写入一个对象。</span></li>
    <li><span class="pp-points__n" aria-hidden="true">02</span><span class="pp-points__body"><strong>每次修改皆可撤销。</strong>一个事务就是 <code>doc.history</code> 中的一个命名步骤，撤销时在版本间切换即可，无需复制整份文档。</span></li>
    <li><span class="pp-points__n" aria-hidden="true">03</span><span class="pp-points__body"><strong>失败不留任何痕迹。</strong>抛出异常的编辑会保持操作开始前的版本不变，而限额机制会在超限时以带类型的 <code>LimitError</code> 终止任何操作的工作量。</span></li>
  </ul>
  <p class="pp-more"><a href="versions/">版本与保存</a><a href="concepts/">核心概念</a></p>
</div>
<div class="pp-split__code pp-version-card" markdown>
<div class="pp-version-card__bar" aria-hidden="true">
  <span class="pp-vnode">v0 <em>已打开</em></span>
  <span class="pp-varrow">→</span>
  <span class="pp-vnode pp-vnode--active">v1 <em>rotate</em></span>
  <span class="pp-varrow">↶</span>
  <span class="pp-vnode pp-vnode--undo">v0 <em>已撤销</em></span>
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
      <p class="pp-eyebrow">现状与透明度</p>
      <h2 id="pp-adopt">采用前须知</h2>
      <p>API 在次版本之间仍可能调整，且渲染速度慢于 MuPDF。在 hayro 的有界构造（bounded construction）发布之前，pyropdf 的任何版本均不声称对不可信输入具备安全性；关于各项限制的承诺与边界，请参见<a href="status/">状态与安全性</a>。<a href="choosing/">选择 pyropdf</a> 提供了与 PyMuPDF、pypdf 和 pikepdf 的对比及实测耗时。</p>
    </div>
    <div class="pp-closing__links">
      <a class="pp-button pp-button--primary" href="getting-started/">开始阅读指南</a>
      <a class="pp-button" href="choosing/">对比其他库</a>
    </div>
  </div>
</section>
