---
title: 内容を読む
description: テキスト、単語、文字、レイアウト、検索、表、Markdown、オフライン OCR。
---

# 内容を読む

どの読み取りも文書の現在の版に対して働くので、直前の編集を保存せずに読めます。

## テキストと単語

`page.text()` は読む順のテキストを、レイアウトに沿った空白と改行つきで返します。
`page.words()` は矩形つきの `Word`、`page.chars()` はグリフの箱つきの文字を返します。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    print(page.text())
    print([word.text for word in page.words(clip=(0, 0, 300, 200))])
    print(doc.text())  # 全ページを順につなげたもの
```

`clip` は読み取りを表示座標の矩形に絞ります。表示座標の原点はページの左上です。

## レイアウト

`page.layout()` はテキストをブロック・行・スパンにまとめ、フォント・サイズ・色を添えます。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    layout = doc.pages[0].layout()
    for block in layout.blocks:
        for line in block.lines:
            print([span.text for span in line.spans])
```

## 検索

`page.search` は一致した箇所の矩形を返します。`quads=True` なら四辺形で返し、
そのままハイライトに使えます。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    hits = page.search("total")
    if hits:
        page.annotations.add(pyropdf.Highlight(hits))
```

## 表と Markdown

`page.tables()` は罫線から表を見つけます。`strategy="text"` ならテキストの揃いから
見つけます。`page.markdown()` と `doc.markdown()` は表も含めてページを Markdown にします。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for table in doc.pages[0].tables():
        print(table.markdown())
    print(doc.markdown())
```

## 描画される要素

`page.content()` はページが描くもの（グリフの並び、パス、画像、クリップ）を、
それを囲む marked content とともに返します。`page.operations` は content stream を
演算子として読み書きします。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    print(len(page.content()), "items")
    print(page.operations[:3])
```

## 画像

`page.images()` はページが描く画像を `page.content()` と同じ順で返します。`pixmap()` は描画と同じように
画像をデコードし、ソフトマスクをアルファにします。`stored()` は JPEG・JPEG 2000・JBIG2・CCITT のデータを
文書に格納されたまま返します。`doc.replace_image(xref, data)` は JPEG・JPEG 2000・PNG に差し替え、
`doc.remove_image(xref)` は画像を消します。どちらもその画像を描く全ページに及びます。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for image in doc.pages[0].images():
        stored = image.stored()
        if stored is not None:
            print(image.item.xref, stored.extension, len(stored.data))
        else:
            image.pixmap().save(f"image-{image.index}.png")
```

## オフライン OCR

`ocr` extra を入れると、`OcrEngine()` が PP-OCR モデルを読み込み、`page.ocr` が
認識した単語を返します。`page.apply_ocr` はそれを見えないテキスト層として加えるので、
`text()` や `search()` で見つかるようになります。

```python
import pyropdf

engine = pyropdf.OcrEngine()
with pyropdf.open("input.pdf") as doc:
    words = doc.pages[0].ocr(engine=engine, dpi=150)
    print([word.text for word in words])
```

エンジンは一度作って使い回してください。時間がかかるのはモデルの読み込みです。
