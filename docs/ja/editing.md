---
title: 編集
description: ページ、注釈、リンク、フォーム、しおり、添付ファイルを編集します。どの変更も新しい版になります。
---

# 編集

以下の編集は、どれも文書の新しい版を書きます。関係する編集を
`doc.transaction(name)` で包むと、履歴の一歩にまとまります。詳しくは
[版と保存](versions.md)を見てください。

## ページ

`doc.pages` は `Page` ハンドルの列です。ハンドルはページが動いても同じページを
指し続け、ページが削除されると `StalePageError` を出します。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pages = doc.pages
    pages.new((595, 842))           # A4 のページを末尾に足す
    pages.insert(0, pages[-1])      # それを先頭に複製する
    pages.move(0, len(pages) - 1)   # そして末尾へ動かす
    pages[0].rotation = 90
    del pages[-1]
```

別の文書のページは、リソース・注釈・フォームのフィールド・構造ごと取り込めます。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc, pyropdf.open("input.pdf") as other:
    doc.pages.insert(len(doc.pages), other.pages)
    doc.pages.select([0, 1])        # この 2 ページだけを、この順で残す
```

`page.media_box`・`crop_box`・`bleed_box`・`trim_box`・`art_box` はページボックスを、
継承して MediaBox に収めたうえで、テキストや描画と同じ表示座標で返します。`(0, 0)` は
見えるページの左上です。`Rect` を代入するとそのページだけにボックスを設定し、MediaBox
からはみ出すボックスは `ValueError` になります。描画とテキストは新しい CropBox に従います。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.crop_box = (36, 36, page.rect.width - 36, page.rect.height - 36)
    page.pixmap(dpi=72).save("cropped.png")
```

`page.flatten()` は注釈とウィジェットの外観をページの内容に描き込み、ページから取り除きます。

## 注釈

`page.annotations.add` は仕様を受け取り、`Annotation` ハンドルを返します。注釈は
外観つきで書かれるので、どのビューアでも同じように描かれます。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.annotations.add(pyropdf.Highlight(page.search("total")))
    page.annotations.add(pyropdf.Note((40, 40), contents="ここを確認"))
    page.annotations.add(pyropdf.Square((60, 300, 200, 360), color=pyropdf.Color(0.8, 0.2, 0.1)))
    for annotation in page.annotations:
        print(annotation.type, annotation.rect)
```

テキストマークアップ（ハイライト、下線、波線、取り消し線）、メモ、線、四角、円、
多角形、折れ線、手書き、添付ファイルを使えます。オプションは
[API リファレンス](https://pyropdf.haya.works/api/#annotations)にあります。

## リンク

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.links.add((72, 72, 200, 90), "https://pyropdf.haya.works/ja/")
    page.links.add((72, 100, 200, 118), pyropdf.Destination(0))
```

## フォーム

`doc.form` は完全なフィールド名から `Field` ハンドルへの対応です。値を設定すると
フィールドが更新され、ウィジェットの外観も整います。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.widgets.add(pyropdf.TextField("name", (72, 300, 300, 324)))
    page.widgets.add(pyropdf.CheckBox("agree", (72, 340, 90, 358)))
    doc.form["name"].value = "山田 花子"
    doc.form["agree"].value = True
    print({name: field.value for name, field in doc.form.items()})
```

## しおり

```python
import pyropdf
from pyropdf import Destination, OutlineEntry

with pyropdf.open("input.pdf") as doc:
    doc.outline = [
        OutlineEntry(1, "はじめに", Destination(0)),
        OutlineEntry(2, "詳細", Destination(0)),
    ]
    print([item.title for item in doc.outline])
```

## 添付ファイル

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.attachments.add("data.csv", b"a,b\n1,2\n", description="元データ")
    print(doc.attachments["data.csv"].read())
```

## ページラベル

`doc.page_labels` は文書の `PageLabelRange` の列を、`page.label` は 1 ページのラベルを返します。
リストを代入すると置き換わり、最初の範囲はページ 0 から始めます。ページの挿入・移動・削除の後も、
各ページは同じラベルを保ちます。

```python
import pyropdf
from pyropdf import PageLabelRange, PageLabelStyle

with pyropdf.open("input.pdf") as doc:
    doc.page_labels = [
        PageLabelRange(0, PageLabelStyle.LOWER_ROMAN),   # i, ii, iii, ...
        PageLabelRange(1, prefix="A-"),                  # A-1, A-2, ...
    ]
    print(doc.pages[1].label, doc.pages.labeled("A-1"))
```

## レイヤー

`doc.layers` はオプショナルコンテンツのグループを `Layer` の列として、名前と既定の構成で表示されるかどうかを返し、
`doc.layers.configs` は構成の一覧を返します。`switch` でグループを表示・非表示にし、`use` で構成を適用すると、
レンダリングとテキスト抽出はその両方に従います。`visible` はグループやメンバーシップ辞書の可視性を、
可視性式も含めて評価します。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for layer in doc.layers:
        print(layer.name, layer.on)
        if layer.name == "Draft":
            doc.layers.switch(layer.xref, on=False)
    print(doc.pages[0].text())
```

## メタデータ

`doc.metadata` は Info 辞書をそのまま読み書きするマッピングです。文書に
XMP パケットがあれば、同じトランザクションで対応する XMP のプロパティも
書き換えるので、PDF/A の文書でも 2 つの写しが一致したままになります。

```python
from datetime import UTC, datetime

import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.metadata.update(title="請求書 42", author="経理部")
    doc.metadata["modified"] = datetime.now(UTC)
    print(doc.metadata["title"])                       # 請求書 42
    print(pyropdf.parse_pdf_date(doc.metadata["modified"]).year > 2000)  # True
    print(doc.xmp is None)                             # True: XMP パケットなし
```

## オブジェクト

`doc.objects` はオブジェクト番号から間接オブジェクトへの対応で、値はどれも型付きの
Python オブジェクト（`Dictionary`、`Array`、`Name`、`Stream` など）です。書き換えると
新しい版として書き戻されます。`doc.check()` は文書が Arlington PDF モデルから
外れている箇所を報告します。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    catalog = doc.catalog
    print(catalog["Type"], doc.pages[0].obj["MediaBox"])
    print(doc.check().defects)
```
