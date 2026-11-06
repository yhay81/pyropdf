---
title: pyropdf
description: PDF を、Python の書き心地と Rust の芯で。Python から PDF を読み、描画し、編集し、署名します。すべての編集は取り消せる版になり、失敗した編集は文書を元のまま残します。
home: true
hide:
  - navigation
  - toc
---

<section class="pp-hero" aria-labelledby="pp-hero-title">
  <h1 id="pp-hero-title">PDF を、<span class="pp-accent">Python の書き心地</span>と <span class="pp-accent">Rust の芯</span>で。</h1>
<div class="pp-hero__aside">
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
</section>

<section class="pp-demo" aria-label="一つの請求書で、pyropdf を一歩ずつ" markdown>
<div class="pp-reel" markdown>
  <div class="pp-reel__steps" role="radiogroup" aria-label="手順">
    <input type="radio" name="pp-step" id="pp-s1" value="1" checked>
    <label for="pp-s1"><span class="pp-reel__n">1</span><span class="pp-reel__title">読む</span><span class="pp-reel__desc">すべての単語を、矩形とともに読む順に。</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <input type="radio" name="pp-step" id="pp-s2" value="2">
    <label for="pp-s2"><span class="pp-reel__n">2</span><span class="pp-reel__title">注釈</span><span class="pp-reel__desc">ページを検索し、一致をハイライト。</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <input type="radio" name="pp-step" id="pp-s3" value="3">
    <label for="pp-s3"><span class="pp-reel__n">3</span><span class="pp-reel__title">フォーム</span><span class="pp-reel__desc">フィールドを加えて値を入れると、外観が付いてくる。</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <input type="radio" name="pp-step" id="pp-s4" value="4">
    <label for="pp-s4"><span class="pp-reel__n">4</span><span class="pp-reel__title">スタンプ</span><span class="pp-reel__desc">ゴム印を、それ自身の外観で描く。</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
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
<p class="pp-stage__caption">どのコマも pyropdf 自身の描画です。<a href="https://github.com/yhay81/pyropdf/blob/main/tools/hero_invoice.py">tools/hero_invoice.py</a> がこの四つのコードを順に、前の出力に対して実行し、<a href="/assets/hero/reviewed.pdf">reviewed.pdf</a> は四つのリビジョンをすべて保っています。</p>
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
