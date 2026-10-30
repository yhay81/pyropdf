---
title: pyropdf
description: PythonからPDFを完全に扱う。Rustの版付きオブジェクトストアの上で、すべての編集は版になり、どの変更も取り消せ、失敗したトランザクションは文書を元のまま残します。
hide:
  - navigation
  - toc
---

<section class="pp-hero" aria-labelledby="pp-hero-title">
  <div>
    <p class="pp-eyebrow">0.2 alpha</p>
    <h1 id="pp-hero-title">PDF の編集は、すべて<em>版</em>になる。</h1>
    <p class="pp-lede">
      pyropdf は、Rust で書かれた一つの版付きオブジェクトストアの上で、Python から PDF を読み、描画し、編集します。
      編集の代価は触れた分だけ。どの変更も取り消せ、失敗したトランザクションは文書を元のまま残します。
    </p>
    <div class="pp-actions">
      <a class="pp-button pp-button--primary" href="getting-started/">はじめる →</a>
      <a class="pp-button" href="recipes/">レシピ</a>
      <a class="pp-button" href="https://pyropdf.haya.works/api/">API リファレンス</a>
    </div>
    <div class="pp-install" aria-label="インストールのコマンド">
      <span class="pp-install__prompt" aria-hidden="true">$</span>
      <code tabindex="0">pip install pyropdf</code>
      <button type="button" data-copy-command="pip install pyropdf" data-copied-label="コピーしました" aria-label="インストールのコマンドをコピー"><span data-copy-label>コピー</span></button>
    </div>
    <ul class="pp-facts" aria-label="プロジェクトの概要">
      <li>MIT ライセンス</li>
      <li>必須の依存なし</li>
      <li>Python 3.11–3.15（free-threaded 対応）</li>
    </ul>
  </div>
  <div class="pp-stack" role="img" aria-label="文書の三つの版が重なり、それぞれにその版を作った編集と、最新の版を取り消す undo が添えられている図">
    <div class="pp-version" aria-hidden="true">
      <div class="pp-version__head"><span><b>v1</b> · 開いた直後</span><span>12 ページ</span></div>
      <span class="pp-version__line">trailer /Root 1 0 R</span>
      <span class="pp-version__line">xref · 214 objects</span>
    </div>
    <div class="pp-version" aria-hidden="true">
      <div class="pp-version__head"><span><b>v2</b> · "rotate"</span><span>1 オブジェクト</span></div>
      <span class="pp-version__line pp-version__line--change">7 0 obj /Rotate 90</span>
      <span class="pp-version__line">pages[0].rotation = 90</span>
    </div>
    <div class="pp-version" aria-hidden="true">
      <div class="pp-version__head"><span><b>v3</b> · "annotate"</span><span>2 オブジェクト</span></div>
      <span class="pp-version__line pp-version__line--add">215 0 obj /Subtype /Highlight</span>
      <span class="pp-version__line pp-version__line--change">7 0 obj /Annots [215 0 R]</span>
      <span class="pp-version__line">page.annotations.add(Highlight(hits))</span>
      <div class="pp-version__foot">
        <span class="pp-chip pp-chip--ember">history.undo()</span>
        <span class="pp-chip pp-chip--moss">save(incremental=True)</span>
        <span class="pp-chip">+2 オブジェクト</span>
      </div>
    </div>
  </div>
</section>

<section class="pp-principles" aria-label="オブジェクトストアが保証すること">
  <article class="pp-principle">
    <span class="pp-principle__number">01</span>
    <h2>編集の代価は触れた分だけ</h2>
    <p>エンジンは書き直したファイルではなくストアを読みます。ページを回転すれば書くのは一つのオブジェクトだけで、次にページを読むときも直列化するのは編集が書いた分だけです。</p>
  </article>
  <article class="pp-principle">
    <span class="pp-principle__number">02</span>
    <h2>どの変更も取り消せる</h2>
    <p>トランザクションは <code>doc.history</code> に名前付きの一歩として残ります。undo と redo は文書を複製せずに版のあいだを移ります。</p>
  </article>
  <article class="pp-principle">
    <span class="pp-principle__number">03</span>
    <h2>失敗は跡を残さない</h2>
    <p>例外を出した編集は、現在の版を元のまま残します。制限はすべての操作の仕事量に上限を設け、型付きの <code>LimitError</code> で止めます。</p>
  </article>
</section>

<div class="pp-section-title">
<h2>いまできること</h2>
<p>0.2 は文書の読み取り・描画・編集・クリーンアップ・軽量化に対応します。<code>page.draw</code> による作図は後のリリースです。</p>
</div>

<div class="pp-cards">
  <a class="pp-card" href="reading/">
    <h3>テキストと構造を読む</h3>
    <p>読む順のテキスト、単語、文字、レイアウト、検索、表、Markdown。</p>
    <code>page.search("total")</code>
  </a>
  <a class="pp-card" href="reading/#offline-ocr">
    <h3>オフライン OCR</h3>
    <p><code>ocr</code> extra の PP-OCR モデルでスキャンしたページを認識し、見えないテキスト層を加えます。</p>
    <code>page.ocr(engine=OcrEngine())</code>
  </a>
  <a class="pp-card" href="rendering/">
    <h3>描画</h3>
    <p>hayro による pixmap・PNG・JPEG・SVG。複数ページの並列描画もできます。</p>
    <code>page.pixmap(dpi=144)</code>
  </a>
  <a class="pp-card" href="editing/">
    <h3>ページと注釈を編集する</h3>
    <p>ページの挿入・取り込み・移動・回転・切り抜き・平坦化。ハイライト、メモ、図形、リンクの追加。メタデータ、ページラベル、レイヤーの編集。</p>
    <code>doc.pages.insert(0, other.pages)</code>
  </a>
  <a class="pp-card" href="editing/#forms">
    <h3>フォームを入力し、作る</h3>
    <p>フィールドの値を読み書きし、ウィジェットを加え、外観を整えたまま保ちます。</p>
    <code>doc.form["name"].value = "Ada"</code>
  </a>
  <a class="pp-card" href="versions/">
    <h3>保存し、クリーンにし、軽くする</h3>
    <p>変わったオブジェクトだけを追記する増分保存、AES-256 暗号化、共有前のサニタイズ、フォントのサブセット化。</p>
    <code>doc.save(path, incremental=True)</code>
  </a>
</div>

<div class="pp-split" markdown>
<div class="pp-section-title">
<h2>スナップショットではなく、トランザクション</h2>
<p>編集を名前付きの一歩にまとめます。ブロックが例外を出せば何も残らず、最後まで進めば一度の undo で戻せます。</p>
<p><a href="versions/">版と保存 →</a></p>
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
  <p><strong>Alpha 版です。</strong>API はマイナーバージョンのあいだで変わることがあります。hayro の bounded construction がリリースされるまで、pyropdf のどのリリースも、信頼できない入力に対する安全性を主張しません。制限が何を約束し、何を約束しないかは<a href="status/">状態と安全性</a>にあります。</p>
</div>
