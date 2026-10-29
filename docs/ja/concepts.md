---
title: 概念
description: pyropdf の API の背後にあるモデル：表示座標、版、ハンドル、スレッド、操作のコスト。
---

# 概念

## 表示座標 {#display-coordinates}

API に出てくる矩形と点は、単語や検索結果の箱、注釈の矩形、`clip`、ページボックスのいずれも
**表示座標**です。単位は 1/72 インチのポイントで、原点は見えているページの左上、`y` は下向きに
増えます。見えているページとは MediaBox で切り取った CropBox にページの回転を適用したもので、
`page.rect` はビューアが表示するとおりの `(0, 0, width, height)` です。

PDF 本来の座標は MediaBox の左下が原点で、回転しません。pyropdf が変換するので、`search` で
見つけた矩形をそのまま注釈に渡せます。描画結果の画素に対応させるには `dpi / 72` を掛けます。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    dpi = 144
    pixmap = page.pixmap(dpi=dpi)
    x0, y0, x1, y1 = (value * dpi / 72 for value in page.words()[0].bbox)
    print(pixmap.width, round(x0), round(y0), round(x1), round(y1))
```

## 版

文書は、開いたファイル（ベース）と、その上に積まれた版の連なりです。編集は変えたオブジェクトだけを
新しい版に書き、残りは下の版と共有します。読み取り・描画・保存はどれも現在の版を相手にするので、
編集は保存しなくても次の `text()` や `pixmap()` に反映され、版だけで決まる結果は版ごとに一度だけ
計算されます。

`doc.history` は undo と redo がたどる手順を保持します。各手順は置き換えたオブジェクトを持ち、
`doc.history.size` がそのバイト数を数えます。`doc.history.max_size` で上限を決められ、
`doc.history.clear()` は文書をそのままにして手順を忘れます。

## ハンドル

`Page`・`Annotation`・`Field`・オブジェクトのハンドルは、位置ではなくものを指します。ページの
ハンドルはページが移動しても追いかけ、undo の後も含めて、現在の版がそのページについて言うことを
読みます。ページがなくなった後に使うと `StalePageError` になり、オブジェクトも同じように
`StaleObjectError` になります。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    first = doc.pages[0]
    doc.pages.move(0, 1)
    print(first.index)           # 1: ハンドルはページを追いかけた
    del doc.pages[1]
    try:
        first.text()
    except pyropdf.StalePageError:
        print("the page is gone")
```

## トランザクション

`doc.transaction(name)` はブロック内の編集を一つの手順にします。ブロックが例外を出すと文書は
その前の版に戻り、そのための複製はしません。トランザクションの外の編集は一つずつが手順です。
両方の例は[版と保存](versions.md)にあります。

## スレッド

複数のスレッドが一つの文書を同時に読めます。読み取りがロックを持つのは現在の版を取る間だけで、
その後はロックなしで進みます。書き込みは直列化され、完成した版を公開するので、長い描画は別の
スレッドが編集している間も、始めたときの版を読み続けます。描画やテキストなどの重い読み取りは
GIL を手放し、CPython の free-threaded ビルド用の wheel もあります。

```python
from concurrent.futures import ThreadPoolExecutor

import pyropdf

with pyropdf.open("input.pdf") as doc, ThreadPoolExecutor() as pool:
    texts = list(pool.map(lambda page: page.text(), doc.pages))
    print(len(texts))
```

`doc.render` はもともと複数のスレッドでページを描画します。

## コスト

- 開くときは相互参照データを読み、すべてのオブジェクトは読みません。`probe` はさらに少なく読みます。
- 編集のコストは触れた分だけです。ページの回転は一つのオブジェクトを書きます。
- 増分保存は編集が書いたオブジェクトだけを追記し、全体保存はファイルを書き直します。
- `DocumentLimits` が各操作の仕事量を抑えます。[制限とエラー](limits.md)を参照してください。
