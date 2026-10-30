---
title: レシピ
description: よくある PDF の作業を短い完結したプログラムで：結合、分割、描画、抽出、ハイライト、フォーム入力、軽量化、クリーンアップ、保護。
---

# レシピ

どのレシピもそのまま動くプログラムです。`input.pdf` を自分のファイルに置き換えてください。

## 文書を結合する

```python
import pyropdf

with pyropdf.open("input.pdf") as doc, pyropdf.open("input.pdf") as other:
    doc.pages.insert(len(doc.pages), other.pages)
    doc.save("merged.pdf")
```

取り込んだページは、リソース・注釈・フォームフィールド・構造を一緒に持ってきます。

## ページごとのファイルに分割する

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for number, page in enumerate(doc.pages, start=1):
        with pyropdf.Document() as part:
            part.pages.insert(0, page)
            part.save(f"page-{number}.pdf")
```

## ページを残す・並べ替える・削除する

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages.select([1, 0])   # このページだけを、この順に残す
    del doc.pages[-1]          # 最後のページを削除する
    doc.save("reordered.pdf")
```

## 全ページを PNG に描画する

```python
from pathlib import Path

import pyropdf

with pyropdf.open("input.pdf") as doc:
    for number, data in enumerate(doc.render(dpi=150, format="png"), start=1):
        Path(f"page-{number}.png").write_bytes(data)
```

`doc.render` は並列に描画します。サムネイルは同じ呼び出しを 36 などの低い `dpi` で行います。

## テキストや Markdown を取り出す

```python
from pathlib import Path

import pyropdf

with pyropdf.open("input.pdf") as doc:
    Path("input.txt").write_text(doc.text(), encoding="utf-8")
    Path("input.md").write_text(doc.markdown(), encoding="utf-8")
```

Markdown は見出しと表を保つので、検索インデックスや言語モデルの入力に向いています。

## 語の出現すべてをハイライトする

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    with doc.transaction("highlight"):
        for page in doc.pages:
            if hits := page.search("total"):
                page.annotations.add(pyropdf.Highlight(hits))
    doc.save("highlighted.pdf", incremental=True)
```

## 辞書からフォームを入力する

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:  # 入力するフォームを作る
    doc.pages[0].widgets.add(pyropdf.TextField("name", (72, 300, 300, 324)))
    doc.save("form.pdf")

values = {"name": "Ada Lovelace"}
with pyropdf.open("form.pdf") as doc:
    with doc.transaction("fill"):
        for name, value in values.items():
            doc.form[name].value = value
    doc.flatten()  # 任意：値をページに描き込んで固定する
    doc.save("filled.pdf")
```

## テキストのないページを除く

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    blank = [index for index, page in enumerate(doc.pages) if not page.text().strip()]
    with doc.transaction("remove blank pages"):
        for index in reversed(blank):
            del doc.pages[index]
    doc.save("without-blanks.pdf")
```

## ページごとにしおりを付ける

```python
import pyropdf
from pyropdf import Destination, OutlineEntry

with pyropdf.open("input.pdf") as doc:
    doc.outline = [
        OutlineEntry(1, page.text().strip().splitlines()[0] or f"Page {index + 1}", Destination(index))
        for index, page in enumerate(doc.pages)
        if page.text().strip()
    ]
    doc.save("bookmarked.pdf")
```

## ファイルを小さくする

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.subset_fonts()
    doc.save("smaller.pdf", compress=True, object_streams=True, prune=True, dedupe=True)
```

`subset_fonts` はページが描くグリフだけを残し、`prune` は到達できないオブジェクトを落とし、
`dedupe` は同一のオブジェクトを一度だけ書きます。

## 共有する前に文書をクリーンにする

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    report = doc.sanitize("shared.pdf")
    print(dict(report.removed))
```

`sanitize` は JavaScript・アクション・添付ファイル・メタデータ・コメント・隠しテキスト・
未使用オブジェクトを取り除きます。それぞれに個別のスイッチがあります。

## パスワードで保護する

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.save("locked.pdf", encryption=pyropdf.Encryption(user_password="reader", owner_password="owner"))
```

## 仕事量の上限つきで文書を開く

```python
import pyropdf

def page_count(data: bytes) -> int | None:
    try:
        with pyropdf.open(data, limits=pyropdf.DocumentLimits.web()) as doc:
            return len(doc.pages)
    except pyropdf.LimitError as error:
        print("refused:", error.code)
    except pyropdf.PdfError as error:
        print("unreadable:", error)
    return None

with open("input.pdf", "rb") as file:
    print(page_count(file.read()))
```

制限は各操作の仕事量を抑えます。制限が約束しないことは[状態と安全性](status.md)にあります。
