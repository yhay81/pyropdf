---
title: 描画
description: hayro でページを pixmap・PNG・JPEG・SVG に描画します。1 ページずつでも、まとめて並列でも。
---

# 描画

pyropdf は [hayro](https://github.com/LaurenzV/hayro) で描画し、現在の版を
オブジェクトストアから直接読みます。

## Pixmap

`page.pixmap()` は `Pixmap` を返します。8 ビットの変更できない画素で、RGB か
グレー、アルファの有無を選べます。`dpi` でページを拡大し、既定は 72 です。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pixmap = doc.pages[0].pixmap(dpi=144, alpha=True)
    print(pixmap.width, pixmap.height, pixmap.channels)
    pixmap.save("page-1.png")
    jpeg = doc.pages[0].pixmap(dpi=144).to_bytes("jpeg", quality=85)  # JPEG にアルファはない
```

pixmap は buffer protocol に対応するので、NumPy や Pillow は画素を複製せずに読めます。
`numpy.asarray(pixmap)` の形は `(height, width, channels)` です。

## オプション

| オプション | 効果 |
| --- | --- |
| `dpi` | ページを一様に拡大します。既定の 72 は 1 ポイント 1 画素です。 |
| `matrix` | 4 分の 1 回転の `Matrix(90)` のような `Matrix`、または拡大・回転・せん断する 6 つの数。ラスタは変換後のページを覆うので平行移動は捨てます。 |
| `clip` | 表示座標の矩形 `(x0, y0, x1, y1)`。その画素だけをラスタ化します。 |
| `alpha` | ページを白の上に置かず、透明度を残します。 |
| `background` | ページの下に塗る 0〜255 の RGB または RGBA。`alpha` なしでは不透明でなければなりません。 |
| `annotations` | `False` で注釈とフォームウィジェットの外観を除きます。 |
| `colorspace` | `"gray"` で ITU-R BT.601 の重みでグレーに変換します。 |

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    top = page.pixmap(dpi=200, clip=(0, 0, page.rect.width, 120))
    gray = page.pixmap(dpi=96, colorspace="gray", annotations=False)
    tinted = page.pixmap(background=(255, 250, 235))
    rotated = page.pixmap(matrix=pyropdf.Matrix(90))
    print(top.height, gray.channels, tinted.channels, rotated.width)
```

`clip` は表示座標、つまり `page.rect` や `words()`・`search()` が返す箱と同じ空間で指定します。
[概念](concepts.md#display-coordinates)を参照してください。

## エンコード

`pixmap.save(path)` は拡張子に合わせて PNG か JPEG を書き、`pixmap.to_bytes("png")` や
`to_bytes("jpeg", quality=...)` はエンコードしたバイト列を返します。JPEG にアルファはないので、
不透明な pixmap をエンコードしてください。

## まとめて描画する

`doc.render` は複数ページを、GIL を手放した最大 `workers` 個のスレッドで並列に描画し、
`pages` の順にエンコード済みの画像を返します。`page.pixmap` のオプションを受け取り、別のスレッドが
編集していても、どのページも呼び出しを始めたときの版から描画します。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    images = doc.render(dpi=96, format="png", workers=4)
    print(len(images), "pages rendered")
    first_only = doc.render([0], dpi=150, format="jpeg", quality=85)
```

`max_size` はバッチ全体のエンコード後のバイト数を抑え、既定は 512 MiB です。

## SVG

`page.svg()` はページを 72 dpi の SVG 文字列で返します。テキストはパスとして描くので、どの
ブラウザでも同じに見えますが、検索はできません。`annotations=False` で注釈を除き、`max_size` が
文字列の大きさを抑えます（既定 64 MiB）。

```python
import pyropdf
from pathlib import Path

with pyropdf.open("input.pdf") as doc:
    Path("page-1.svg").write_text(doc.pages[0].svg(), encoding="utf-8")
```
