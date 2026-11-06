---
title: pyropdf
description: PDF を、Python の書き心地と Rust の芯で。Python から PDF を読み、描画し、編集し、署名します。すべての編集は取り消せる版になり、失敗した編集は文書を元のまま残します。
home: true
hide:
  - navigation
  - toc
---

<section class="pp-hero" aria-labelledby="pp-hero-title" markdown>
<div class="pp-hero__text">
  <h1 id="pp-hero-title">PDF を、<span class="pp-accent">Python の書き心地</span>と <span class="pp-accent">Rust の芯</span>で。</h1>
  <p class="pp-lede">
    pyropdf は PDF 文書を読み、描画し、編集し、署名します。ページはシーケンス、フォームのフィールドはマッピング、編集はトランザクションとして書けます。その下では Rust の版付きオブジェクトストアがすべての版を保ちます。どの変更も取り消せ、失敗した編集は文書を元のまま残します。
  </p>
  <div class="pp-install" aria-label="インストールのコマンド">
    <span class="pp-install__prompt" aria-hidden="true">$</span>
    <code tabindex="0">pip install pyropdf</code>
    <button type="button" data-copy-command="pip install pyropdf" data-copied-label="コピーしました" aria-label="インストールのコマンドをコピー"><span data-copy-label>コピー</span></button>
  </div>
  <p class="pp-actions">
    <a class="pp-button pp-button--primary" href="getting-started/">はじめる</a>
    <a class="pp-button" href="recipes/">レシピ</a>
    <a class="pp-button" href="https://pyropdf.haya.works/api/">API リファレンス（英語）</a>
  </p>
  <ul class="pp-facts" aria-label="プロジェクトの概要">
    <li>MIT ライセンス</li>
    <li>必須の依存なし</li>
    <li>CPython 3.11–3.15（free-threaded 対応）</li>
  </ul>
</div>
<div class="pp-stage" markdown>
<img class="pp-sheet pp-sheet--before" src="/assets/hero/invoice.png" width="1057" height="1375" alt="" aria-hidden="true">
<figure class="pp-sheet">
  <img src="/assets/hero/invoice.png" width="1057" height="1375" alt="例を実行する前の請求書を pyropdf で描画したもの">
  <img class="pp-sheet__after" src="/assets/hero/reviewed.png" width="1057" height="1375" alt="例を実行した後の請求書。Total がハイライトされ、版 2 として保存されたページを pyropdf で描画したもの">
  <span class="pp-sheet__tag">v2 · "highlight totals"</span>
</figure>
<div class="pp-stage__code" markdown>

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
<p class="pp-stage__caption">pyropdf が <a href="/assets/hero/reviewed.pdf">reviewed.pdf</a> から描画しました。この PDF は <a href="https://github.com/yhay81/pyropdf/blob/main/tools/hero_invoice.py">tools/hero_invoice.py</a> がこの例で作ります。</p>
</div>
</section>

<section class="pp-section" aria-labelledby="pp-features">
  <header class="pp-section__head">
    <h2 id="pp-features">文書のすべてを、一つのパッケージで</h2>
    <p>読み取り、描画、編集は一つのストアを共有します。読むのはいつも、いま編集した版です。</p>
  </header>
  <div class="pp-cards">
    <a class="pp-card" href="reading/">
      <h3>読む</h3>
      <p>読む順のテキスト、単語、文字、レイアウト、検索、表、Markdown。</p>
      <code>page.markdown()</code>
    </a>
    <a class="pp-card" href="reading/#offline-ocr">
      <h3>スキャンを認識する</h3>
      <p><code>ocr</code> extra の PP-OCR モデルによるオフライン OCR。結果は見えないテキスト層として書き戻します。</p>
      <code>page.ocr(engine=OcrEngine())</code>
    </a>
    <a class="pp-card" href="rendering/">
      <h3>描画する</h3>
      <p>hayro による pixmap・PNG・JPEG・SVG と、複数ページの並列描画。</p>
      <code>page.pixmap(dpi=144)</code>
    </a>
    <a class="pp-card" href="editing/">
      <h3>ページを編集する</h3>
      <p>ページの挿入・取り込み・移動・回転・切り抜き・平坦化。メタデータ、ページラベル、しおり、レイヤーの編集。</p>
      <code>page.rotation = 90</code>
    </a>
    <a class="pp-card" href="editing/">
      <h3>作図し、注釈を付ける</h3>
      <p>パス、画像、ページ、テキストの作図。ハイライト、メモ、図形、リンク、フリーテキスト、スタンプ。</p>
      <code>page.draw()</code>
    </a>
    <a class="pp-card" href="editing/#forms">
      <h3>フォームを入力する</h3>
      <p>フィールドの値を読み書きし、ウィジェットを加え、外観を最新に保ちます。</p>
      <code>doc.form["name"].value = "Ada"</code>
    </a>
    <a class="pp-card" href="versions/">
      <h3>保存し、署名する</h3>
      <p>変わった分だけを追記する増分保存、AES-256 暗号化、新しいリビジョンへの PAdES 署名。</p>
      <code>doc.sign("approval", signer)</code>
    </a>
    <a class="pp-card" href="editing/#sanitizing">
      <h3>クリーンにし、軽くする</h3>
      <p>共有の前に JavaScript、添付ファイル、メタデータ、隠れたテキストを取り除き、フォントをサブセット化します。</p>
      <code>doc.sanitize("shared.pdf")</code>
    </a>
  </div>
</section>

<section class="pp-section pp-split" aria-labelledby="pp-versions" markdown>
<div class="pp-split__text">
  <header class="pp-section__head">
    <h2 id="pp-versions">版の上に立つ</h2>
    <p>どの編集も、解析したコピーをその場で書き換えるのではなく、文書の新しい版を書きます。保証はすべてここから来ています。</p>
  </header>
  <ul class="pp-points">
    <li><strong>編集の代価は触れた分だけ。</strong>エンジンは書き直したファイルではなくストアを読みます。ページを回転すれば、書くのは一つのオブジェクトだけです。</li>
    <li><strong>どの変更も取り消せる。</strong>トランザクションは <code>doc.history</code> の名前付きの一歩で、undo は文書を複製せずに版のあいだを移ります。</li>
    <li><strong>失敗は跡を残さない。</strong>例外を出した編集は始まったときの版を保ち、制限はどの操作の仕事も型付きの <code>LimitError</code> で止めます。</li>
  </ul>
  <p class="pp-more"><a href="versions/">版と保存</a><a href="concepts/">概念</a></p>
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
  <h2 id="pp-adopt">採用する前に</h2>
  <p>API はまだマイナーバージョンのあいだで変わることがあり、描画は MuPDF より遅いです。hayro の bounded construction がリリースされるまで、pyropdf のどのリリースも、信頼できない入力に対する安全性を主張しません。制限が何を約束し、何を約束しないかは<a href="status/">状態と安全性</a>に、PyMuPDF・pypdf・pikepdf との時間を含めた比較は<a href="choosing/">pyropdf を選ぶ</a>にあります。</p>
</section>
