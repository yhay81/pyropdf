---
title: pyropdf
description: PDF を、Python の書き心地と Rust の芯で。Python から PDF を読み、描画し、編集し、署名します。すべての編集は取り消せる版になり、失敗した編集は文書を元のまま残します。
home: true
hide:
  - navigation
  - toc
---

<section class="pp-hero pp-reel" aria-labelledby="pp-hero-title" markdown>
<div class="pp-hero__text">
  <h1 id="pp-hero-title">PDF を、<span class="pp-accent">Python の書き心地</span>と <span class="pp-accent">Rust の芯</span>で。</h1>
  <p class="pp-lede">
    pyropdf は PDF を読み、描画し、編集し、署名します。ページはシーケンス、フィールドはマッピング、編集はトランザクション。その下で Rust の版付きストアが、どの変更も取り消せるようにします。
  </p>
  <div class="pp-hero__start">
    <div class="pp-install" aria-label="インストールのコマンド">
    <span class="pp-install__prompt" aria-hidden="true">$</span>
    <code tabindex="0">pip install pyropdf</code>
    <button type="button" data-copy-command="pip install pyropdf" data-copied-label="コピーしました" aria-label="インストールのコマンドをコピー"><span data-copy-label>コピー</span></button>
    </div>
    <p class="pp-actions">
    <a class="pp-button pp-button--primary" href="getting-started/">はじめる</a>
    <a class="pp-button pp-button--video" href="#pp-overview" data-jump-video><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.25 12.5 8 5 12.75z" fill="currentColor"/></svg>30秒動画</a>
    <a class="pp-button" href="https://pyropdf.haya.works/api/" hreflang="en">API リファレンス</a>
  </p>
  </div>
</div>
  <div class="pp-reel__steps" role="radiogroup" aria-label="手順">
    <input type="radio" name="pp-step" id="pp-s1" value="1" checked>
    <label for="pp-s1"><span class="pp-reel__n">1</span><span class="pp-reel__title">読む</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <input type="radio" name="pp-step" id="pp-s2" value="2">
    <label for="pp-s2"><span class="pp-reel__n">2</span><span class="pp-reel__title">注釈</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <input type="radio" name="pp-step" id="pp-s3" value="3">
    <label for="pp-s3"><span class="pp-reel__n">3</span><span class="pp-reel__title">フォーム</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <input type="radio" name="pp-step" id="pp-s4" value="4">
    <label for="pp-s4"><span class="pp-reel__n">4</span><span class="pp-reel__title">スタンプ</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <button type="button" class="pp-reel__toggle" data-reel-toggle data-state="playing" data-pause-label="デモの自動切り替えを一時停止" data-play-label="デモの自動切り替えを再開" aria-label="デモの自動切り替えを一時停止" title="デモの自動切り替えを一時停止">
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

<p class="pp-reel__label">出力</p>

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
<div class="pp-viewer" role="group" aria-label="請求書の各リビジョンを pyropdf で描画して表示する PDF ビューア">
  <div class="pp-viewer__bar">
    <span class="pp-viewer__file"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 1.5h5.5L13 5v9.5H4z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/><path d="M9.5 1.5V5H13" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg>invoice.pdf</span>
    <span class="pp-viewer__revs"><span class="pp-reel__rev" data-step="1">rev 1 · 開いた直後</span><span class="pp-reel__rev" data-step="2">rev 2 · highlight totals</span><span class="pp-reel__rev" data-step="3">rev 3 · approve</span><span class="pp-reel__rev" data-step="4">rev 4 · stamp</span></span>
    <span class="pp-viewer__page" aria-label="1 / 1 ページ">1 / 1</span>
    <a class="pp-viewer__open" href="/assets/hero/reviewed.pdf" title="reviewed.pdf を開く" aria-label="reviewed.pdf を開く"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M9 2.5h4.5V7M13.5 2.5 7.5 8.5M12 9.5v4H2.5V4h4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg></a>
  </div>
  <div class="pp-viewer__canvas">
    <figure class="pp-viewer__sheet">
      <img class="pp-reel__frame" data-step="1" src="/assets/hero/revision-1.png" width="1057" height="1375" alt="pyropdf が描画した請求書と、page.words() が見つけた単語の矩形">
      <img class="pp-reel__frame" data-step="2" src="/assets/hero/revision-2.png" width="1057" height="1375" alt="Total をハイライトした請求書を pyropdf で描画したもの">
      <img class="pp-reel__frame" data-step="3" src="/assets/hero/revision-3.png" width="1057" height="1375" alt="Approved by のフィールドを入力した請求書を pyropdf で描画したもの">
      <img class="pp-reel__frame" data-step="4" src="/assets/hero/revision-4.png" width="1057" height="1375" alt="Approved のスタンプを押した請求書を pyropdf で描画したもの">
      <img class="pp-reel__words" data-step="1" src="/assets/hero/words.svg" width="340" height="442" alt="">
    </figure>
  </div>
</div>
<div class="pp-hero__foot">
  <ul class="pp-facts" aria-label="プロジェクトの概要">
    <li>MIT ライセンス</li>
    <li>必須の依存なし</li>
    <li>CPython 3.11–3.15（free-threaded 対応）</li>
  </ul>
<p class="pp-stage__caption">どのコマも pyropdf 自身の描画です。<a href="https://github.com/yhay81/pyropdf/blob/main/tools/hero_invoice.py">tools/hero_invoice.py</a> がこの四つのコードを順に、前の出力に対して実行し、<a href="/assets/hero/reviewed.pdf">reviewed.pdf</a> は四つのリビジョンをすべて保っています。</p>
</div>
</section>

<section class="pp-section pp-showcase">
  <div class="pp-showcase__head">
    <header class="pp-section__head">
      <p class="pp-eyebrow">紹介動画</p>
      <h2 id="pp-overview">30 秒でわかる pyropdf</h2>
      <p>テキスト・表の読み取り、注釈、フォーム入力、スタンプ、即座の undo/redo、増分保存まで — 一つの版付きストアの上で動く様子を英語ナレーションで紹介しています。</p>
    </header>
    <p class="pp-showcase__meta"><span class="pp-showcase__dot" aria-hidden="true"></span>720p · 30 fps · 英語音声・英語字幕</p>
  </div>
  <div class="pp-showcase__stage">
    <figure class="pp-video-figure">
      <div class="pp-video-frame">
        <div class="pp-video-frame__bar">
          <span class="pp-video-frame__title"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M13.5 0.67s.74 2.65.74 4.8c0 2.06-1.35 3.73-3.41 3.73-2.07 0-3.63-1.67-3.63-3.73l.03-.36C5.21 7.51 4 10.62 4 14c0 4.42 3.58 8 8 8s8-3.58 8-8C20 8.61 17.41 3.8 13.5.67zM11.71 19c-1.78 0-3.22-1.4-3.22-3.14 0-1.62 1.05-2.76 2.81-3.12 1.77-.36 3.6-1.21 4.62-2.58.39 1.29.59 2.65.59 4.04 0 2.65-2.15 4.8-4.8 4.8z"/></svg>pyropdf 紹介動画</span>
          <span class="pp-video-frame__spec">1280×720 · 30 fps · 0:34</span>
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
          <p>お使いのブラウザは動画の埋め込み再生に対応していません。<a href="/assets/video/pyropdf-intro.mp4">pyropdf 紹介動画（MP4）をダウンロード</a>してください。</p>
        </video>
      </div>
      <figcaption>pyropdf 自身による実際の請求書レンダリング、トランザクションによる undo/redo とロールバック、元のバイト列と署名を保つ増分保存を約 30 秒で紹介しています（英語音声・英語字幕付き）。</figcaption>
    </figure>
    <div class="pp-showcase__rail">
      <p class="pp-rail__heading">チャプターから再生</p>
      <div class="pp-chapters" role="group" aria-label="動画のチャプター">
        <button type="button" class="pp-chapter is-active" data-video-seek="2.89">
          <span class="pp-chapter__time">0:03</span>
          <span class="pp-chapter__body">
            <strong>Python の書き心地と Rust の芯</strong>
            <span>断片化したツールを一つに統合</span>
          </span>
        </button>
        <button type="button" class="pp-chapter" data-video-seek="6.53">
          <span class="pp-chapter__time">0:06</span>
          <span class="pp-chapter__body">
            <strong>読む・注釈・フォーム・スタンプ</strong>
            <span>一つの版付きストアで編集と描画が同期</span>
          </span>
        </button>
        <button type="button" class="pp-chapter" data-video-seek="14.55">
          <span class="pp-chapter__time">0:14</span>
          <span class="pp-chapter__body">
            <strong>アトミックな undo とロールバック</strong>
            <span>複製なしの履歴移動と例外時の保護</span>
          </span>
        </button>
        <button type="button" class="pp-chapter" data-video-seek="22.38">
          <span class="pp-chapter__time">0:22</span>
          <span class="pp-chapter__body">
            <strong>増分保存と MIT ライセンス</strong>
            <span>元のバイト列と PAdES 署名を維持</span>
          </span>
        </button>
      </div>
      <details id="pp-intro-transcript" class="pp-transcript">
        <summary>書き起こし（英語）を表示</summary>
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
    <p class="pp-eyebrow">機能一覧</p>
    <h2 id="pp-features">文書のすべてを、一つのパッケージで</h2>
    <p>読み取り、描画、編集は一つのストアを共有します。読むのはいつも、いま編集した版です。</p>
  </header>
  <div class="pp-cards">
    <a class="pp-card" href="reading/">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3.5h7l3.5 3.5v9.5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-12a1 1 0 0 1 1-1Z"/><path d="M12 3.5V7h3.5M7 10.5h6M7 13.5h4.5"/></svg></span>
        <h3>読む</h3>
        <p>読む順のテキスト、単語、文字、レイアウト、検索、表、Markdown。</p>
        <code>page.markdown()</code>
      </article>
    </a>
    <a class="pp-card" href="reading/#offline-ocr">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 7V4.5a1 1 0 0 1 1-1H7M13 3.5h2.5a1 1 0 0 1 1 1V7M16.5 13v2.5a1 1 0 0 1-1 1H13M7 16.5H4.5a1 1 0 0 1-1-1V13"/><path d="M6.5 10h7M8 7.5h4M8 12.5h4"/></svg></span>
        <h3>スキャンを認識する</h3>
        <p><code>ocr</code> extra の PP-OCR モデルによるオフライン OCR。結果は見えないテキスト層として書き戻します。</p>
        <code>page.apply_ocr(engine=OcrEngine())</code>
      </article>
    </a>
    <a class="pp-card" href="rendering/">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="14" height="12" rx="2"/><circle cx="7.5" cy="8.25" r="1.25"/><path d="m17 13-3.5-3.5L7.5 15.5"/></svg></span>
        <h3>描画する</h3>
        <p>hayro による pixmap・PNG・JPEG・SVG と、複数ページの並列描画。</p>
        <code>page.pixmap(dpi=144)</code>
      </article>
    </a>
    <a class="pp-card" href="editing/#pages">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="8.5" height="11" rx="1.5"/><path d="M8.5 4V3a1 1 0 0 1 1-1H15a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1h-2.5"/></svg></span>
        <h3>ページを編集する</h3>
        <p>ページの挿入・取り込み・移動・回転・切り抜き・平坦化。メタデータ、ページラベル、しおり、レイヤーの編集。</p>
        <code>page.rotation = 90</code>
      </article>
    </a>
    <a class="pp-card" href="editing/#drawing">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="m13.5 3.5 3 3-8.5 8.5-3.5.5.5-3.5 8.5-8.5Z"/><path d="m11.5 5.5 3 3"/></svg></span>
        <h3>作図し、注釈を付ける</h3>
        <p>パス、画像、ページ、テキストの作図。ハイライト、メモ、図形、リンク、フリーテキスト、スタンプ。</p>
        <code>page.draw()</code>
      </article>
    </a>
    <a class="pp-card" href="editing/#forms">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="14" height="12" rx="2"/><path d="M6.5 8.5h7M6.5 12h4.5"/></svg></span>
        <h3>フォームを入力する</h3>
        <p>フィールドの値を読み書きし、ウィジェットを加え、外観を最新に保ちます。</p>
        <code>doc.form["name"].value = "Ada"</code>
      </article>
    </a>
    <a class="pp-card" href="versions/#saving">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2.75 16 5v4.75c0 3.7-2.55 6.45-6 7.5-3.45-1.05-6-3.8-6-7.5V5l6-2.25Z"/><path d="m7.5 10 1.75 1.75L13 8"/></svg></span>
        <h3>保存し、署名する</h3>
        <p>変わった分だけを追記する増分保存、AES-256 暗号化、新しいリビジョンへの PAdES 署名。</p>
        <code>doc.sign("approval", signer)</code>
      </article>
    </a>
    <a class="pp-card" href="editing/#sanitizing">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 3v3M10 14v3M3 10h3M14 10h3M5.2 5.2l2.1 2.1M12.7 12.7l2.1 2.1M14.8 5.2l-2.1 2.1M7.3 12.7l-2.1 2.1"/></svg></span>
        <h3>クリーンにし、軽くする</h3>
        <p>共有の前に JavaScript、添付ファイル、メタデータ、隠れたテキストを取り除き、フォントをサブセット化します。</p>
        <code>doc.sanitize("shared.pdf")</code>
      </article>
    </a>
  </div>
</section>

<section class="pp-section pp-split" markdown>
<div class="pp-split__text">
  <header class="pp-section__head">
    <p class="pp-eyebrow">アーキテクチャ</p>
    <h2 id="pp-versions">版の上に立つ</h2>
    <p>どの編集も、解析したコピーをその場で書き換えるのではなく、文書の新しい版を書きます。保証はすべてここから来ています。</p>
  </header>
  <ul class="pp-points">
    <li><span class="pp-points__n" aria-hidden="true">01</span><span class="pp-points__body"><strong>編集の代価は触れた分だけ。</strong>エンジンは書き直したファイルではなくストアを読みます。ページを回転すれば、書くのは一つのオブジェクトだけです。</span></li>
    <li><span class="pp-points__n" aria-hidden="true">02</span><span class="pp-points__body"><strong>どの変更も取り消せる。</strong>トランザクションは <code>doc.history</code> の名前付きの一歩で、undo は文書を複製せずに版のあいだを移ります。</span></li>
    <li><span class="pp-points__n" aria-hidden="true">03</span><span class="pp-points__body"><strong>失敗は跡を残さない。</strong>例外を出した編集は始まったときの版を保ち、制限はどの操作の仕事も型付きの <code>LimitError</code> で止めます。</span></li>
  </ul>
  <p class="pp-more"><a href="versions/">版と保存</a><a href="concepts/">概念</a></p>
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
      <p class="pp-eyebrow">現状と前提</p>
      <h2 id="pp-adopt">採用する前に</h2>
      <p>API はまだマイナーバージョンのあいだで変わることがあり、描画は MuPDF より遅いです。hayro の bounded construction がリリースされるまで、pyropdf のどのリリースも、信頼できない入力に対する安全性を主張しません。制限が何を約束し、何を約束しないかは<a href="status/">状態と安全性</a>に、PyMuPDF・pypdf・pikepdf との時間を含めた比較は<a href="choosing/">pyropdf を選ぶ</a>にあります。</p>
    </div>
    <div class="pp-closing__links">
      <a class="pp-button pp-button--primary" href="getting-started/">ガイドを読む</a>
      <a class="pp-button" href="choosing/">他ライブラリと比較</a>
    </div>
  </div>
</section>
