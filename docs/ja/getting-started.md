---
title: はじめに
description: pyropdf をインストールし、文書を開き、ページを読んで描画し、トランザクションで編集して保存します。
---

# はじめに

## インストール

```bash
pip install pyropdf
```

wheel は Linux・macOS・Windows の CPython 3.11〜3.15 に対応し、free-threaded
ビルド用の wheel も別にあります。必須の依存はありません。オフライン OCR には
モデルが必要で、extra で入ります。

```bash
pip install "pyropdf[ocr]"
```

## 開いて閉じる

`pyropdf.open` はパスか文書のバイト列を受け取ります。文書は context manager で、
ブロックを抜けると閉じます。`pyropdf.Document()` は空の文書を作ります。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    print(len(doc.pages), doc.pages[0].rect)

doc = pyropdf.Document()
doc.pages.new((595, 842))
data = doc.to_bytes()
doc.close()
```

`pyropdf.probe` は文書全体を読み込まずに、ページ数・バージョン・メタデータを読みます。

```python
import pyropdf

info = pyropdf.probe("input.pdf")
print(info.page_count, info.version, info.needs_password)
```

## ページを読む

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    print(page.text())
    for word in page.words():
        print(word.text, word.bbox)
    print(page.search("total"))
```

レイアウト、文字、表、Markdown、OCR は[内容を読む](reading.md)にあります。

## ページを描画する

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].pixmap(dpi=144).save("page-1.png")
```

## 編集して保存する

編集はそのたびに文書の新しい版を作ります。トランザクションは編集を名前付きの一歩に
まとめ、`doc.history` で取り消せるようにします。ブロックが例外を出したときは、
文書を元のまま残します。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("回転とメモ"):
        page.rotation = 90
        page.annotations.add(pyropdf.Note((40, 40), contents="確認済み"))
    doc.save("output.pdf", incremental=True)
```

増分保存は、編集が書いたオブジェクトだけを追記します。既定の全体保存はファイルを
書き直します。違いは[版と保存](versions.md)にあります。

## 次に読む

- [内容を読む](reading.md)：テキスト、単語、レイアウト、表、Markdown、OCR。
- [描画](rendering.md)：pixmap、まとめての描画、SVG。
- [編集](editing.md)：ページ、注釈、リンク、フォーム、しおり、添付ファイル。
- [版と保存](versions.md)：トランザクション、undo、変更レポート、暗号化。
- [制限とエラー](limits.md)：操作ごとの仕事量に上限を設ける。
