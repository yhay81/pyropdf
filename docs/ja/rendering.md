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

`clip` でページの一部を、`matrix` で変換したページを、`colorspace="gray"` で
グレーの画素を、`annotations=False` で注釈なしのページを描画します。

## まとめて描画する

`doc.render` は複数ページを並列に描画し、`pages` の順にエンコード済みの画像を返します。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    images = doc.render(dpi=96, format="png", workers=4)
    print(len(images), "pages rendered")
```

## SVG

`page.svg()` はページを 72 dpi の SVG 文字列で返します。テキストはパスとして描きます。

```python
import pyropdf
from pathlib import Path

with pyropdf.open("input.pdf") as doc:
    Path("page-1.svg").write_text(doc.pages[0].svg(), encoding="utf-8")
```
