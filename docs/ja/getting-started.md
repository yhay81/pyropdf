---
title: はじめに
description: pyropdf をインストールし、文書を開き、ページを読んで描画し、トランザクションで編集して保存します。
---

# はじめに

## インストール

=== "pip"

    ```bash
    pip install pyropdf
    ```

=== "uv"

    ```bash
    uv add pyropdf
    ```

=== "Poetry"

    ```bash
    poetry add pyropdf
    ```

wheel は Linux・macOS・Windows の CPython 3.11〜3.15 に対応し、free-threaded
ビルド用の wheel も別にあります。必須の依存はありません。オフライン OCR には
モデルが必要で、extra で入ります。

=== "pip"

    ```bash
    pip install "pyropdf[ocr]"
    ```

=== "uv"

    ```bash
    uv add "pyropdf[ocr]"
    ```

=== "Poetry"

    ```bash
    poetry add "pyropdf[ocr]"
    ```

`jp`・`ko`・`zh-cn`・`zh-tw` の extra は、文書が埋め込んでいない日本語・韓国語・中国語の
フォントを描く書体を入れます。[描画](rendering.md#fonts-that-a-document-does-not-embed)を参照してください。

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

```python title="read.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    print(page.text())  # (1)!
    for word in page.words():
        print(word.text, word.bbox)  # (2)!
    print(page.search("total"))  # (3)!
```

1. 読む順のテキスト。段組、表、見出しは、コンテンツストリームが描く順ではなく、
   人が読む順に出てきます。
2. 矩形は表示座標です。ページの回転にかかわらず、ビューアが見せるページの左上からの
   ポイントで表します。[概念](concepts.md#display-coordinates)を参照してください。
3. 一致ごとに一つの矩形のリスト。そのまま `pyropdf.Highlight` などの注釈に渡せます。

レイアウト、文字、表、Markdown、OCR は[内容を読む](reading.md)にあります。

## ページを描画する

```python title="render.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].pixmap(dpi=144).save("page-1.png")
```

## 編集して保存する

編集はそのたびに文書の新しい版を作ります。トランザクションは編集を名前付きの一歩に
まとめ、`doc.history` で取り消せるようにします。ブロックが例外を出したときは、
文書を元のまま残します。

```python title="edit.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("回転とメモ"):  # (1)!
        page.rotation = 90
        page.annotations.add(pyropdf.Note((40, 40), contents="確認済み"))
    doc.save("output.pdf", incremental=True)  # (2)!
```

1. 二つの編集は `doc.history` の名前付きの一歩になります。ブロックが例外を出せば
   どちらも残らず、文書は前の版のままです。
2. 編集が書いたオブジェクトだけを元のバイト列の後ろに追記するので、
   元のバイト列に対する署名は有効なままです。

増分保存は、編集が書いたオブジェクトだけを追記します。既定の全体保存はファイルを
書き直します。違いは[版と保存](versions.md)にあります。

## 次に読む

- [内容を読む](reading.md)：テキスト、単語、レイアウト、表、Markdown、OCR。
- [描画](rendering.md)：pixmap、まとめての描画、SVG。
- [編集](editing.md)：ページ、注釈、リンク、フォーム、しおり、添付ファイル。
- [版と保存](versions.md)：トランザクション、undo、変更レポート、暗号化。
- [制限とエラー](limits.md)：操作ごとの仕事量に上限を設ける。
