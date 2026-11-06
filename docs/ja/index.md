---
title: pyropdf
description: PDF を、Python の書き心地と Rust の芯で。Python から PDF を読み、描画し、編集し、署名します。すべての編集は取り消せる版になり、失敗した編集は文書を元のまま残します。
hide:
  - navigation
  - toc
---

<section class="pp-hero" aria-labelledby="pp-hero-title" markdown>
<div class="pp-hero__text">
  <h1 id="pp-hero-title">PDF を、Python の書き心地と Rust の芯で。</h1>
  <p class="pp-lede">
    pyropdf は PDF 文書を読み、描画し、編集し、署名します。ページはシーケンス、フォームのフィールドはマッピング、編集はトランザクションとして書けます。
    その下では Rust の版付きオブジェクトストアがすべての版を保ちます。どの変更も取り消せ、失敗した編集は文書を元のまま残します。
  </p>
  <div class="pp-install" aria-label="インストールのコマンド">
    <code tabindex="0">pip install pyropdf</code>
    <button type="button" data-copy-command="pip install pyropdf" data-copied-label="コピーしました" aria-label="インストールのコマンドをコピー"><span data-copy-label>コピー</span></button>
  </div>
  <p class="pp-actions">
    <a class="pp-button pp-button--primary" href="getting-started/">はじめる</a>
    <a class="pp-button" href="recipes/">レシピ</a>
    <a class="pp-button" href="https://pyropdf.haya.works/api/">API リファレンス（英語）</a>
  </p>
  <p class="pp-facts">MIT ライセンス · 必須の依存なし · CPython 3.11–3.15（free-threaded 対応）</p>
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

## できること

<dl class="pp-index">
  <div>
    <dt><a href="reading/">読む</a></dt>
    <dd>読む順のテキスト、単語、文字、レイアウト、検索、表、Markdown。</dd>
    <dd><code>page.markdown()</code></dd>
  </div>
  <div>
    <dt><a href="reading/#offline-ocr">スキャンを認識する</a></dt>
    <dd><code>ocr</code> extra の PP-OCR モデルによるオフライン OCR。結果は見えないテキスト層として書き戻します。</dd>
    <dd><code>page.ocr(engine=OcrEngine())</code></dd>
  </div>
  <div>
    <dt><a href="rendering/">描画する</a></dt>
    <dd>hayro による pixmap・PNG・JPEG・SVG と、複数ページの並列描画。</dd>
    <dd><code>page.pixmap(dpi=144)</code></dd>
  </div>
  <div>
    <dt><a href="editing/">ページを編集する</a></dt>
    <dd>ページの挿入・取り込み・移動・回転・切り抜き・平坦化。メタデータ、ページラベル、しおり、レイヤーの編集。</dd>
    <dd><code>doc.pages.insert(0, other.pages)</code></dd>
  </div>
  <div>
    <dt><a href="editing/">作図し、注釈を付ける</a></dt>
    <dd>パス、画像、ページ、テキストの作図。ハイライト、メモ、図形、リンク、フリーテキスト、スタンプ。</dd>
    <dd><code>page.draw()</code></dd>
  </div>
  <div>
    <dt><a href="editing/#forms">フォームを入力する</a></dt>
    <dd>フィールドの値を読み書きし、ウィジェットを加え、外観を最新に保ちます。</dd>
    <dd><code>doc.form["name"].value = "Ada"</code></dd>
  </div>
  <div>
    <dt><a href="versions/">保存し、署名する</a></dt>
    <dd>変わった分だけを追記する増分保存、AES-256 暗号化、新しいリビジョンへの PAdES 署名。</dd>
    <dd><code>doc.sign("approval", signer)</code></dd>
  </div>
  <div>
    <dt><a href="editing/#sanitizing">クリーンにし、軽くする</a></dt>
    <dd>共有の前に JavaScript、添付ファイル、メタデータ、隠れたテキストを取り除き、フォントをサブセット化します。</dd>
    <dd><code>doc.sanitize("shared.pdf")</code></dd>
  </div>
</dl>

## 版の上に立つ

<div class="pp-split" markdown>
<div class="pp-split__text">
  <p>どの編集も、解析したコピーをその場で書き換えるのではなく、文書の新しい版を書きます。保証はすべてこの一つの決定から来ています。</p>
  <ul>
    <li><strong>編集の代価は触れた分だけ。</strong>エンジンは書き直したファイルではなくストアを読みます。ページを回転すれば、書くのは一つのオブジェクトだけです。</li>
    <li><strong>どの変更も取り消せる。</strong>トランザクションは <code>doc.history</code> の名前付きの一歩で、undo は文書を複製せずに版のあいだを移ります。</li>
    <li><strong>失敗は跡を残さない。</strong>例外を出した編集は始まったときの版を保ち、制限はどの操作の仕事も型付きの <code>LimitError</code> で止めます。</li>
  </ul>
  <p><a href="versions/">版と保存</a> · <a href="concepts/">概念</a></p>
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

## 採用する前に

<div class="pp-notes">
  <p>API はまだマイナーバージョンのあいだで変わることがあり、描画は MuPDF より遅いです。hayro の bounded construction がリリースされるまで、pyropdf のどのリリースも、信頼できない入力に対する安全性を主張しません。制限が何を約束し、何を約束しないかは<a href="status/">状態と安全性</a>にあります。</p>
  <p><a href="choosing/">pyropdf を選ぶ</a>では、PyMuPDF・pypdf・pikepdf と時間を含めて比べています。</p>
</div>
