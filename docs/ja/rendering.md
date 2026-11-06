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
| `hinting` | `True` で FreeType の light hinting のように文字をヒンティングし、小さな文字をくっきりさせます。既定ではヒンティングしません。 |
| `glyph_cache` | `True` で各グリフをサイズごとに一度だけラスタ化して使い回し、文字を速く描きます。ただし文字は縦に最大で半画素ずれます。キャッシュのメモリは `DocumentLimits` の外にあり、スレッドがその文書の版に持つ描画キャッシュと同じだけ残ります。既定では使いません。 |

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

ラスタは幅 65,408 画素、高さ 65,532 画素、合計 64,000,000 画素までで、超えると `ValueError` になります。
`clip` を指定すると、この上限は切り抜いたラスタに適用されます。そのため小さな領域は、ページ全体では描けない倍率でも描けます。
たとえば A1 ページの隅の 100 × 100 ポイントは 20 倍で 2,000 画素四方に描けますが、ページ全体なら 16 億画素になります。
行列で変換したページは各辺 524,288 画素までです（`LimitError`、`code="clip_page_side"`）。
この範囲なら、ページの反対側からクリップに届く図形も、本来の位置から 1/16 画素以内に描かれます。

## エンコード

`pixmap.save(path)` は拡張子が示す形式で書き、`pixmap.to_bytes("png")` や
`to_bytes("jpeg", quality=...)` はエンコードしたバイト列を返します。どちらも PNG と JPEG のほか、
PAM、PBM、PGM、PPM、PNM、PSD、PostScript を書けます。アルファを保つのは PNG、PAM、PSD だけで、
ほかの形式はアルファを拒むので、不透明な pixmap をエンコードしてください。

## ピクセル操作

どの操作も新しい pixmap を返します：`invert(rect=None)`、`gamma(value)`、
`tint(black, white)`、`to_gray()`、`to_rgb()`、`with_alpha()`、
`without_alpha(background)`、`crop(rect)`、`scale(width, height)`、`rotate(degrees)`、
`flip(axis)`、`warp(quad, width, height)`、`fill(value, rect=None)`、`with_pixel(x, y, value)`。
`pixel(x, y)`、`colors(rect=None)`、`color_count(rect=None)`、`top_color(rect=None)` は
ピクセルを読みます。矩形はピクセル単位です。`Pixmap.from_bytes(data)` と `Pixmap.open(path)` は
PNG、JPEG、PBM、PGM、PPM、PAM、BMP、GIF、TIFF、JPEG 2000 を読み、`to_pil()` は
Pillow があれば Pillow の画像を返します。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pixmap = doc.pages[0].pixmap(dpi=72)
    thumbnail = pixmap.to_gray().scale(pixmap.width // 4, pixmap.height // 4)
    thumbnail.invert((0, 0, 10, 10)).save("thumbnail.png")
```

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

## 埋め込まれていないフォント {#fonts-that-a-document-does-not-embed}

PDF はフォントのプログラムを埋め込まずに名前だけで指定することがあり、日本語・中国語・
韓国語の文書の MS ゴシック、MS 明朝、SimSun、Batang などによく見られます。hayro は
そうしたフォントを標準 14 フォントで描きますが、標準 14 フォントには CJK のグリフがないので、
`page.text()` では読めるテキストが描画では空白になります。フォントの extra を入れると、
代わりに描く書体が入ります。

=== "pip"

    ```bash
    pip install "pyropdf[jp]"
    ```

=== "uv"

    ```bash
    uv add "pyropdf[jp]"
    ```

=== "Poetry"

    ```bash
    poetry add "pyropdf[jp]"
    ```

`jp`・`ko`・`zh-cn`・`zh-tw` は、日本語・韓国語・簡体字中国語・繁体字中国語の Noto Sans と
Noto Serif を入れます。すると `page.pixmap()`・`page.svg()`・`doc.render()`・
`annotation.pixmap()` のどの描画も、Adobe-Japan1・Korea1・GB1・CNS1 の文字コレクションの
フォントをその言語の書体で描き、明朝体のように名前がセリフを示すフォントはセリフ体で描きます。
書体はページが初めて必要としたときに読み込まれます。

`doc.fallback_fonts` は言語 `"ja"`・`"ko"`・`"zh-CN"`・`"zh-TW"` から `FallbackFont` への
マッピングで、編集するまではインストール済みの extra を並べます。`Font.from_file` で読み込んだ
書体で `FallbackFont(sans=..., serif=...)` を代入するとその言語を別の書体で描き、言語を `del`
するとその言語を代替なしで描き、`{}` を代入するとどの言語も代替なしで描きます。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    print(list(doc.fallback_fonts))  # インストール済みの extra（例: ['ja']）
    doc.fallback_fonts = {}  # 代替フォントなしで描く
    plain = doc.pages[0].pixmap()
    del doc.fallback_fonts  # インストール済みの extra に戻す
```

これらのフォントは `Document` オブジェクトの設定で、保存されません。テキスト抽出はこれらを使わず、
文字コレクションから文字を読みます。使うのは `exact_bboxes=True` の正確なボックスと
`page.content()` のテキストランだけで、レンダリングが描くものを測ります。
