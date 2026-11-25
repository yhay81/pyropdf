---
title: レシピ
description: よくある PDF の作業を短い完結したプログラムで：結合、分割、サムネイル描画、画像とテキストの抽出、スタンプ、透かし、フォーム入力と平坦化、墨消しマーク、サニタイズ、保護。
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

## ページを残す・並べ替える・回転する・削除する

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages.select([1, 0])    # このページだけを、この順に残す
    doc.pages[0].rotation = 90  # 新しい先頭ページを時計回りに回転する
    del doc.pages[-1]           # 最後のページを削除する
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

## サムネイルを描画して埋め込み画像を取り出す

```python
from pathlib import Path

import pyropdf

with pyropdf.open("input.pdf") as doc:
    for number, thumb in enumerate(doc.render(dpi=36, format="png"), start=1):
        Path(f"thumb-{number}.png").write_bytes(thumb)
    for page_number, page in enumerate(doc.pages, start=1):
        for image in page.images():
            stored = image.stored()
            if stored is not None:
                Path(f"page-{page_number}-image-{image.index}.{stored.extension}").write_bytes(stored.data)
            else:
                image.pixmap().save(f"page-{page_number}-image-{image.index}.png")
```

`doc.render(dpi=36, format="png")` は低解像度のサムネイルを並列に描画します（代わりに
`fit=256` を渡すと長辺をピクセル上限に収めます）。`page.images()` はページ上に描かれた
埋め込み画像を一覧します。`stored()` は JPEG・JPEG 2000・JBIG2・CCITT の元データを再圧縮せず
そのまま返し、それ以外の画像は `pixmap()` がソフトマスクをアルファとしてデコードして PNG に
書き出せます。PDF 自体に `/Thumb` プレビューを埋め込むには `page.thumbnail = page.pixmap(fit=96)`
を代入します。

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

## レビューコメントをページと作成者つきで一覧する

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for number, page in enumerate(doc.pages, start=1):
        for annotation in page.annotations:
            if comment := (annotation.contents or "").strip():
                author = (annotation.author or "").strip() or "Unknown"
                print(f"Page {number} ({author}): {comment}")
```

`page.annotations` はメモやハイライトなどの注釈を返し、リンク・フォームウィジェット・
ポップアップは除きます。

## スタンプと斜めの透かしを入れる

```python
import pyropdf

font = pyropdf.Font.standard("Helvetica-Bold")
with pyropdf.open("input.pdf") as doc:
    with doc.transaction("stamp and watermark"):
        doc.pages[0].annotations.add(
            pyropdf.Stamp((36, 36, 180, 76), icon=pyropdf.StampIcon.CONFIDENTIAL)
        )
        label = "CONFIDENTIAL"
        width = font.text_width(label, 48)
        for page in doc.pages:
            cx, cy = page.rect.width / 2, page.rect.height / 2
            place = pyropdf.Matrix(-35) * pyropdf.Matrix(1, 0, 0, 1, cx, cy)
            with page.draw() as canvas, canvas.opacity(0.25), canvas.transform(place):
                canvas.text(label, (-width / 2, 0), font=font, size=48, fill=pyropdf.Color(0.7, 0.1, 0.1))
    doc.save("watermarked.pdf")
```

`Stamp` は固有の外観ストリームを持つ標準スタンプ注釈を追加し、`page.draw()` は半透明の
斜め透かしを各ページの表示空間に直接描きます（ページ本文の背面に描くには `underlay=True` を
渡します）。両方を `doc.transaction` で包むと、操作全体が 1 回のやり直し単位になります。

## 辞書からフォームを入力する

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:  # 入力するフォームを作る
    page = doc.pages[0]
    page.widgets.add(pyropdf.TextField("name", (72, 300, 300, 324)))
    page.widgets.add(pyropdf.CheckBox("approved", (72, 340, 90, 358)))
    page.widgets.add(pyropdf.ComboBox("status", (72, 372, 220, 396), choices=["Draft", "Final"]))
    doc.save("form.pdf")

values = {"name": "Ada Lovelace", "approved": True, "status": "Final"}
with pyropdf.open("form.pdf") as doc:
    with doc.transaction("fill"):
        for name, value in values.items():
            doc.form[name].value = value
    doc.save("filled-editable.pdf", incremental=True)
    doc.flatten()  # 任意：値をページに描き込んで固定する
    doc.save("filled.pdf")
```

`doc.form[name].value` に代入すると各ウィジェットの外観が更新されます。`incremental=True` で
保存すると元のバイト列を保ったまま再編集でき、`doc.flatten()` を呼ぶと入力済みの外観をページ内容に
描き込んで対話フィールドを取り除きます。

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

## 墨消し箇所をマークしてサニタイズする

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    with doc.transaction("mark for redaction"):
        for page in doc.pages:
            for quad in page.search("total", quads=True):
                page.annotations.add(
                    pyropdf.RedactionMark(
                        quad.rect,
                        quads=(quad,),
                        overlay_text="REDACTED",
                        default_appearance=b"/Helv 10 Tf 1 1 1 rg",
                        interior_color=pyropdf.Color(0, 0, 0),
                    )
                )
    report = doc.sanitize("marked-clean.pdf", comments=False)
    print(dict(report.removed))
```

`comments=False` を指定すると `/Redact` のレビュー用マークを残したまま、`sanitize` が
JavaScript・アクション・添付ファイル・メタデータ・隠しテキスト・未使用オブジェクトを 1 回の
全体保存で取り除きます。`RedactionMark` は対象領域をレビュー用にマークするだけで、下の描画内容
自体は削除しません。内容そのものを除去する完全な墨消しは
[#362](https://github.com/yhay81/pyropdf/issues/362) で追跡しています。

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

## アクセシブルな長期保存用のグラフを作る

[グラフ作成プログラムをダウンロード](https://raw.githubusercontent.com/yhay81/pyropdf/main/docs/includes/accessible-archive.py)できます。
空の文書から、タグ付きのグラフを1ページ作成します。表示する文字を収録した埋め込み可能なフォントと、
描画で使うRGB色に対応したICCプロファイルを指定します。リポジトリに含まれる見本で新旧両方の規格を試せます。

```sh
uv run python -c "import runpy; from pathlib import Path; Path('chart.icc').write_bytes(runpy.run_path('tests/assets/pdfa/generate.py')['icc']())"
FONT=crates/pyropdf-engine/src/fonts/testdata/NotoSansJP-subset.otf
uv run python docs/includes/accessible-archive.py "$FONT" chart.icc chart.pdf
uv run python docs/includes/accessible-archive.py "$FONT" chart.icc earlier.pdf --earlier
```

| 出力 | 保存とアクセシビリティの規格 | 作例で指定する値 |
| --- | --- | --- |
| `chart.pdf` | PDF/A-4 と PDF/UA-2 | PDF 2.0 のヘッダー、XMPに各規格の改訂年（2020年と2024年）、文書と3つの子要素にPDF 2.0の標準名前空間 |
| `earlier.pdf` | PDF/A-2a と PDF/UA-1 | PDF 1.7 のヘッダー、適合レベルA、XMPにPDF/UA識別用の拡張スキーマ、PDF 1.7の暗黙の構造名前空間 |

どちらもフォントとRGB出力プロファイルを埋め込み、本文言語を米国英語に指定します。
ビューアにはXMPの文書タイトルを表示するよう指定しています。
次のコマンドで、ページの見た目とPopplerで取得した読み順を照合できます。

```sh
pdfinfo -struct-text chart.pdf
pdftoppm -png -singlefile -scale-to 520 chart.pdf chart
```

見出し「Air flow」、本文「Air flow fell to 1」、図の順に読み上げられることを確認します。
図では上の棒が2、下の棒が1で、下の棒の長さは上の半分です。
図の代替説明からも、1回目から2回目にかけてこの変化があったと理解できるか確認します。
機械判定できる規則は、それぞれの規格を明示して検証します。

```sh
verapdf --flavour 4 chart.pdf
verapdf --flavour ua2 chart.pdf
verapdf --flavour 2a earlier.pdf
verapdf --flavour ua1 earlier.pdf
```

テストでは図の代替説明を書き換え、全体保存と増分保存の両方を検証します。
説明を削除するとPDF/UA-2の規則 `8.2.5.28.2-1` とPDF/UA-1の規則 `7.3-1` に違反します。
この場合も、両方のPDF/A検証には合格します。
