---
title: 編集
description: ページ、注釈、リンク、フォーム、しおり、添付ファイルを編集します。どの変更も新しい版になります。
---

# 編集

以下の編集は、どれも文書の新しい版を書きます。関係する編集を
`doc.transaction(name)` で包むと、履歴の一歩にまとまります。詳しくは
[版と保存](versions.md)を見てください。

## ページ {#pages}

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

`doc.pages.new_from_image(source)` は画像のフレームごとに、解像度に合わせた大きさ
（または均一な `size=(width, height)` に縦横比を保って中央配置）のページを加えます。
JPEG と JPEG 2000 は再圧縮せずに埋め込み、Exif の向き（1〜8）も再圧縮なしで反映します。
PNG は Pillow なしで直接読んでアルファをソフトマスクに保ち、TIFF や GIF などは Pillow があれば
Pillow を通して読みます。複数の画像列や CBZ（`.cbz` / ZIP）アーカイブを 1 回の取り消し可能な
トランザクションでページ化するには、`doc.pages.new_from_images(sources)` と
`doc.pages.new_from_cbz(source)` を使います。CBZ ではエントリを自然順で並べ、`__MACOSX` や
メタデータファイルを除き、チャンク読み込みで `DocumentLimits` を厳格に適用します：

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pixmaps = [page.pixmap(dpi=72) for page in doc.pages]
    scan = pyropdf.Document()
    scan.pages.new_from_image(pixmaps[0])            # 1 枚の画像（パス・バイト列・Pixmap）
    scan.pages.new_from_images(pixmaps, size=(595, 842))  # 複数画像を A4 に収めて一括追加
    scan.save("scan.pdf")
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

## 作図 {#drawing}

`page.draw()` が返す `Canvas` は、パス、画像、他のページ、テキストを表示空間で描きます。
ページの内容の上に描き、`underlay=True` なら下に描きます。`with` ブロックが一つの
トランザクションで一つの取り消し単位になり、中で例外が起きると作図は捨てられます。

```python
import pyropdf
from pyropdf import Color, PathBuilder, Stroke

with pyropdf.open("input.pdf") as doc, pyropdf.open("input.pdf") as other:
    page = doc.pages[0]
    with page.draw() as canvas:
        canvas.rect((36, 36, 236, 136), fill=Color(1, 1, 0, 0.4))
        canvas.circle((300, 100), 40, stroke=Stroke(paint=Color(0, 0, 1), width=2))
        with canvas.clip(PathBuilder().rect((36, 200, 236, 300), radius=12)), canvas.opacity(0.5):
            canvas.page((36, 200, 236, 300), other.pages[0])
        canvas.image((300, 200, 400, 300), page.pixmap(dpi=36))
```

`canvas.image` は JPEG、PNG、JPEG 2000 のデータ、パス、`Pixmap`、文書内の画像を
受け取ります。`canvas.page` は開いている任意の文書のページを、切り抜きや回転を付けて
配置します。何度も描いた画像やページは一度だけ保存されます。`clip`、`clip_text`、`transform`、
`opacity`、`blend`、`layer`、`tag` は、その中で描くもの全体に効くスコープです。

### グラデーション

`LinearGradient` と `RadialGradient` は、パス・図形・テキストで `Color` が使える場所、
つまり `fill` と `Stroke` の `paint` で使えます。座標は描くものと同じ空間にあるので、
`canvas.transform` に合わせて動きます。

```python
import pyropdf
from pyropdf import Color, ColorStop, GradientExtend, LinearGradient, Point, RadialGradient, Stroke

sky = LinearGradient(
    start=Point(0, 0),
    end=Point(0, 200),
    stops=(ColorStop(0, Color(0.2, 0.4, 0.9)), ColorStop(1, Color(1, 1, 1))),
)
sun = RadialGradient(
    start=Point(100, 100),
    start_radius=0,
    end=Point(100, 100),
    end_radius=40,
    # ストップのアルファはソフトマスクを通して色をフェードアウトさせる
    stops=(ColorStop(0, Color(1, 0.8, 0)), ColorStop(1, Color(1, 0.8, 0, 0))),
)
stripes = LinearGradient(
    start=Point(0, 0),
    end=Point(20, 0),
    stops=(ColorStop(0, Color(0, 0, 0)), ColorStop(1, Color(1, 1, 1))),
    extend=GradientExtend.REFLECT,
)

doc = pyropdf.Document()
page = doc.pages.new((200, 200))
with page.draw() as canvas:
    canvas.rect((0, 0, 200, 200), fill=sky)
    canvas.circle((100, 100), 40, fill=sun)
    canvas.line((0, 180), (200, 180), stroke=Stroke(paint=stripes, width=6))
```

`extend` はグラデーションの線の外側に何を描くかを決めます。両端の色を伸ばす `PAD`（既定）、
始点または終点の色だけを伸ばす `PAD_START` と `PAD_END`、何も描かない `NONE`、
線を繰り返す `REPEAT`、往復させる `REFLECT` があります。同じ関数とシェーディングは
1 つの canvas の中で一度だけ書かれます。`page.content()` と `page.paths()` は軸方向と放射状のシェーディングを色関数つきの
`ShadingPattern` として、タイリングパターンをセル操作つきの `TilingPattern` として読み戻し、
`canvas.draw` はそれらの塗りと線を描き直します（軸方向・放射状シェーディングは上限つきの
サンプル近似、タイリングパターンは上限つきの `/PatternType 1` ストリームとして出力します。
関数・メッシュシェーディングは描き直しません）。`canvas.draw` はインラインのラスタ画像や
間接・インラインのステンシル画像マスク（`Image.fill` の塗りを使用）も描き直します。
ソフトマスクのためにテキストの塗りと線を分けて描く場合は、
二度目に空の `ActualText` を付け、文字抽出では一度だけ読み取れるようにします。

### テキスト

`canvas.text` はベースラインの一点から行を描き、`canvas.textbox` は矩形の中で
テキストを折り返します。どちらも `font` と `size` を指定しなければ Helvetica の
11 ポイントで描きます。`Font.standard` は `"Times-Roman"` や `"Courier-Bold"` の
ように名前で標準 14 フォントを返します。標準 14 フォントは埋め込まないので
ファイルは小さく保てます。12 のテキスト用フォントは WinAnsi（おおむね Latin-1）の
文字に対応し、それ以外の文字は `PdfError` で拒否します。Symbol と ZapfDingbats は
内蔵の符号化を使うため、WinAnsi のバイトが選ぶ字形は入力文字と異なることがあります。
他の文字体系には `Font.from_file` で
OpenType フォントを読み込みます。文書はそれを一度だけ埋め込みます。
TrueType の可変フォントでは、`weight=700` のようにウェイト軸上のインスタンスを選べます。
COLR で塗れるグリフと、`sbix` や CBDT の PNG ストライクがあるグリフは、塗りの色によらず
そのグリフ自身の色で描き、文字として読み取れます。それ以外のカラーグリフは単色の代替アウトラインで描きます。
アウトラインのないものは、読み取れる文字の下に何も描かないよう、描くときに `PdfError` で
拒否します。

```python
import pyropdf
from pyropdf import Color, Font

doc = pyropdf.Document()
for _ in range(2):
    doc.pages.new((300, 200))
note = "Totals include tax. Prices are in euros and valid until the end of the month."
helvetica = Font.standard("Helvetica")
for number, page in enumerate(doc.pages, start=1):
    with page.draw() as canvas:
        canvas.text("Invoice", (20, 40), font=Font.standard("Helvetica-Bold"), size=18)
        box = (20, 60, 160, 80)
        if not canvas.textbox(box, note, size=10, align="justify").fits:
            height = helvetica.text_height(note, 10, width=140)
            canvas.textbox((20, 60, 160, 60 + height), note, size=10, align="justify")
        canvas.text(f"Page {number} of {len(doc.pages)}", (240, 190), size=8, fill=Color(0.4, 0.4, 0.4))
```

テキストボックスは収まるか、何も描かないかのどちらかです。返る `TextFit` は収まった
かどうかを示し、その `spare_height` は最後の行の下に残る高さ、収まらなければ負の
不足分です。`Font.text_height` と `Font.text_width` はキャンバスと同じ配置で
テキストを測るので、描く前に箱の大きさを決められます。行は Unicode が許す位置で
折り返し、箱より幅の広い単語は書記素クラスタの間で分けます。一つのクラスタも
幅に収まらないときは `PdfError` になります。`align` は `"left"`、`"center"`、`"right"`、`"justify"` のいずれかで、
`line_height` は行の間隔を `size` の倍数で指定します。テキストは既定で黒く塗ります。
`stroke` で輪郭を描き、`fill=None` で中を塗らずに残し、`render_mode=TextRenderMode.INVISIBLE`
（`3 Tr`）を指定するとインクを描かずに検索・抽出できる不可視テキストを埋め込めます。
グリフの輪郭で後続の図形や画像をクリップするには（`4 Tr`〜`7 Tr`）、
`with canvas.clip_text(...):` スコープを使います（既定はクリップのみ、`fill` や `stroke`
を渡すと塗りや線とクリップを同時に行います）。`rotate` は直角単位で、`canvas.transform` は
任意の角度でテキストを回します。

埋め込み OpenType フォントで `direction="vertical"` を指定すると、`harfrust` の縦組みシェーピングと
`Identity-V`・`/DW2`・`/W2` を用いて縦書き（上から下へ進み、行は右から左へ並ぶ）を描画します。
複数スクリプトが混在する文字列には、`fallback_fonts` に明示的な `Font` 列（または `doc.fallback_fonts`）
を渡すと、プロセス全体のグローバルなフォントレジストリを持たずに書記素クラスタ単位でフォールバックし、
実際に使用した書体だけを文書内に一度だけ埋め込んで保存時にサブセット化します。

```python
import pyropdf
from pyropdf import Color, Font, TextRenderMode

doc = pyropdf.Document()
page = doc.pages.new((300, 200))
latin = Font.standard("Helvetica")
with page.draw() as canvas:
    canvas.text("Hidden OCR layer", (20, 30), render_mode=TextRenderMode.INVISIBLE)
    with canvas.clip_text("CLIP", (20, 120), font=Font.standard("Helvetica-Bold"), size=48):
        canvas.rect((0, 70, 220, 140), fill=Color(0.2, 0.5, 0.9))
    if "ja" in doc.fallback_fonts and (jp := doc.fallback_fonts["ja"].sans) is not None:
        canvas.text("Hello 日本語", (20, 160), font=latin, fallback_fonts=(jp,), size=14)
        canvas.text("縦書き\n二行目", (260, 30), font=jp, size=16, direction="vertical")
```

テキストは既定では一回の呼び出しで UTF-8 で 1 MiB、4,096 行までで、超えると何も描く前に
`LimitError` で拒否します。信頼できる入力には `max_text_size=None` を渡します。
[制限とエラー](limits.md)を参照してください。

PDF にすでに描かれているテキストを複製するには、抽出したテキストランをキャンバスに渡します。
`draw` を呼んでいる間は元の文書を開いたままにしておいてください。

```python
import pyropdf

with pyropdf.open("input.pdf") as source, pyropdf.Document() as target:
    page = source.pages[0]
    copy = target.pages.new((page.rect.width, page.rect.height))
    with copy.draw() as canvas:
        for run in page.content(paths=False, images=False).text_runs:
            canvas.draw(run)
    target.save("copied-text.pdf")
```

各グリフは元の PDF 文字コード・テキスト行列・フォント辞書を保持します。キャンバスはランを抽出した版から
対象フォントを複製するため、その後に元の符号化やフォントプログラムを変更しても描画結果は変わりません。
同じ `content()` の結果から得たフォントは複製した資源を共有します。埋め込み単純フォント、標準 14 フォント、
埋め込み Identity-H / Identity-V フォント、独自資源を持つ Type 3 フォントを複製できます。利用できない書体、
直接埋め込まれたフォント辞書、コード幅が不明な複合符号化、特異なグリフ変換、置換テキスト内包、パターン塗り、
またはソフトマスクを持つランは `ValueError` になります。元の文書が閉じている場合は `DocumentClosedError`、
キャンバスが制限を超えた場合は描画全体をロールバックします。

15 文書の往復コーパスでは 17,246 件のテキストランを再描画します。`mhlw-doc.pdf` の 8 ランは未埋め込みの
非標準フォントを使っており、コーパステストは残りのページ画素を 72 dpi で RGB 各チャネル 16 階調以内に収まるか検証し、
除外件数と理由が変わらないことも確認します。

### テキストの置換 {#replacing-text}

`page.replace_text(old, new, *, count=None, fallback=None)` は、ページ本体と
ページが描く Form XObject 内のテキストを置換します。Type 1・TrueType・Type 3 に加え、
横書き（`WMode = 0`）と縦書き（`WMode = 1`）の複合フォント（Type 0 / CID：`Identity-H`・
`Identity-V`・既定 CMap・埋め込みカスタム CMap ストリーム）に対応します。進行軸方向の
`TJ` 補正により後続テキストの表示位置を保ち、外側の構造タグ（`/MCID`）を保持したまま
一致する `/ActualText` スパンも同時に更新します。

未サブセットの埋め込み Type 0 書体に置換先のグリフが含まれる場合は、`/W`・`/W2`・
`/ToUnicode` を自動で拡張します。サブセット済みフォント、Type 3 フォントの `/CharProcs`、
または符号化に欠落グリフがある場合は、`fallback=Font(...)` を渡すとフォールバック書体に
切り替えて後続の配置を保ったまま置換できます。`fallback` を渡さない場合は `ValueError`
を送出し、文書を一切変更しません。

```python
import pyropdf

doc = pyropdf.Document()
page = doc.pages.new((300, 120))
with page.draw() as canvas:
    canvas.text("Invoice #001 total", (20, 60), size=14)
page.replace_text("#001", "#002")
if "ja" in doc.fallback_fonts and (jp := doc.fallback_fonts["ja"].sans) is not None:
    page.replace_text("total", "合計", fallback=jp)
```

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

テキストマークアップ（ハイライト、下線、波線、取り消し線）、メモ、フリーテキスト（`FreeText`）、
スタンプ（`Stamp`）、キャレット（`Caret`）、線、四角、円、多角形、折れ線、手書き、
音声（`Sound`）、添付ファイルを使えます。オプションは
[API リファレンス](/api/annotations/)にあります。

## リンク

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.links.add((72, 72, 200, 90), "https://pyropdf.haya.works/ja/")
    page.links.add((72, 100, 200, 118), pyropdf.Destination(0))
```

## フォーム {#forms}

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

`doc.form.to_fdf()` と `doc.form.to_xfdf()` は FDF（ISO 32000-2 §12.7.8）および
XFDF（ISO 19444-1）でフィールド値を書き出し、`parse_fdf()` と `parse_xfdf()` は
`FdfData` として読み取り、`doc.form.import_fdf()` と `doc.form.import_xfdf()` は
一致するフィールドを 1 回の取り消し可能なトランザクションで反映します。

```python
import pyropdf

with pyropdf.Document() as source, pyropdf.Document() as target:
    for doc in (source, target):
        page = doc.pages.new((400, 200))
        page.widgets.add(pyropdf.TextField("name", (40, 40, 240, 64)))
        page.widgets.add(pyropdf.CheckBox("agree", (40, 80, 58, 98)))
    source.form["name"].value = "山田 花子"
    source.form["agree"].value = True

    fdf = source.form.to_fdf(file="form.pdf")
    assert pyropdf.parse_fdf(fdf).fields["name"] == "山田 花子"
    assert target.form.import_fdf(fdf) == ["agree", "name"]

    xfdf = target.form.to_xfdf(include_empty=False)
    assert pyropdf.parse_xfdf(xfdf).fields == {"agree": "Yes", "name": "山田 花子"}
```

## アクション・スクリプト・トリガー {#actions-and-triggers}

リンク、しおり、プレゼンテーションステップ、`doc.open_action`、`page.triggers`、
`widget.triggers`、`Screen` 注釈（`annotation.action` と `annotation.triggers`）はすべて
`Target` アクション型（`EmbeddedDestination`、`HideAction`、`SetLayerStateAction`、
`JavaScriptAction`、`SubmitFormAction`、`ResetFormAction`、`ImportDataAction` を含む）を共有します。
文書レベルのスクリプトは `doc.scripts`（`/Names /JavaScript`）、カタログの追加アクションは
`doc.triggers`（`Catalog["AA"]`）で読み書きでき、pyropdf がスクリプトを実行することはありません。

```python
import pyropdf
from pyropdf import (
    AnnotationTrigger,
    Destination,
    DocumentTrigger,
    EmbeddedDestination,
    EmbeddedTarget,
    HideAction,
    NamedAction,
    PageTrigger,
    PushButton,
    ResetFormAction,
    SubmitFormAction,
    SubmitFormFlags,
    TextField,
)

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.widgets.add(TextField("email", (72, 300, 260, 324)))
    send = page.widgets.add(PushButton("send", (270, 300, 340, 324), caption="Send"))
    doc.open_action = Destination(0)
    doc.scripts["init"] = "var ready = true;"
    doc.triggers[DocumentTrigger.WILL_SAVE] = "console.println('saving');"
    page.triggers[PageTrigger.OPEN] = NamedAction("FirstPage")
    send.triggers[AnnotationTrigger.ACTIVATE] = SubmitFormAction(
        "https://example.com/submit",
        fields=("email",),
        flags=SubmitFormFlags.EXPORT_FORMAT,
    )
    send.triggers[AnnotationTrigger.DOWN] = HideAction("email", hide=False)
    page.links.add(
        (72, 340, 220, 358),
        EmbeddedDestination(page=0, target=EmbeddedTarget(embedded_file="attachment.pdf")),
    )
    page.links.add((230, 340, 340, 358), ResetFormAction(("email",)))
    assert not pyropdf.open(doc.to_bytes()).check()
    print(doc.open_action, list(doc.scripts), send.triggers[AnnotationTrigger.ACTIVATE].url)
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

## 記事スレッド {#article-threads}

`doc.threads` は文書の記事スレッド（`/Threads`）を `Thread` ハンドルの列として返します。
各スレッドはページの表示座標で `rect` を持つ `Bead` ハンドルの順序付きリング（`beads`）と、
`/I` 情報辞書のプロパティ（`title`、`author`、`subject`、`keywords`、`created`、`modified`）を持ちます。
`ThreadAction` を使うと、リンク、しおり、プレゼンテーションのナビゲーションステップから
スレッドや特定のビーズへ移動できます。

```python
import pyropdf
from pyropdf import BeadSpecification, OutlineEntry, ThreadAction

with pyropdf.open("input.pdf") as doc:
    article = doc.threads.add(
        [
            BeadSpecification(0, (36, 72, 260, 400)),
            BeadSpecification(1, (36, 72, 260, 400)),
        ],
        title="巻頭特集",
        author="編集部",
    )
    article.beads.add(1, (280, 72, 500, 400))
    doc.pages[0].links.add((36, 40, 180, 60), ThreadAction(0, 1))
    doc.outline = [OutlineEntry(1, "巻頭特集を読む", ThreadAction("巻頭特集"))]
    print(article.title, len(article.beads), doc.pages[0].links[-1].target)
```

## 構造ツリーとアクセシビリティ {#structure-and-accessibility}

`doc.structure` は論理構造ツリーの最上位要素を `StructureElement` ハンドルの列として返し、
`walk()` は全要素を行きがけ順にたどり、`doc.structure.role_map` は `/RoleMap` を読み書きします。
各要素の `role`・`title`・`language`（BCP 47 言語タグ）・`alt`・`actual_text`・
`expanded`（`/E` 略語展開）・`page` は代入と `del` で編集でき、`children` と `move()` は
`/P`・`/K`・`/ParentTree`・`/IDTree` の整合性を保ったまま要素を追加・並べ替え・移動・削除します。

```python
import pyropdf
from pyropdf import TagRole

with pyropdf.open("input.pdf") as doc:
    doc.structure.role_map["Callout"] = TagRole.NOTE
    root = doc.structure.add("Document")
    heading = root.children.add(TagRole.HEADING_1, page=0, title="Summary")
    abbr = heading.children.add(TagRole.SPAN, expanded="portable document format")
    note = root.children.add("Callout", page=0, language="en-US", alt="Payment terms")
    print([elem.standard_role for elem in doc.structure.walk()])
    print(abbr.expanded, note.language, note.alt)
```

## 添付ファイルとポータブルコレクション {#attachments}

`doc.attachments` は埋め込みファイル（`/Names /EmbeddedFiles`）を管理し、各添付ファイルの
`collection_item` は `/CI` の `CollectionItem`（表示接頭辞付きの `CollectionSubitem` を含む）を
読み書きします。`doc.collection` はポータブルコレクションのスキーマ・並び順・配色・画面分割・
カスタム `Navigator`・`CollectionFolder` 階層を設定します。

埋め込みファイルには関連ファイルを設定できます。`att.related_files` に、`/EF` 辞書にあるファイル名のキー（`F` または `UF`）と関連ファイルの列を渡します。列の順序は保存され、複数の名前から同じストリームを参照できます。ファイル添付注釈でも `annotation.file.related_files` を使えます。

```python
import pyropdf
from pyropdf import (
    Collection,
    CollectionField,
    CollectionFieldKind,
    CollectionFolder,
    CollectionSortKey,
    CollectionSubitem,
    CollectionView,
    Navigator,
)

with pyropdf.open("input.pdf") as doc:
    att = doc.attachments.add("data.csv", b"a,b\n1,2\n", description="元データ")
    legend = doc.objects.add_stream(b"units: metres", {"Type": pyropdf.Name("EmbeddedFile")})
    att.related_files = {"F": [pyropdf.RelatedFile(filename="legend.txt", stream=doc.objects[legend])]}
    att.collection_item = {
        "Department": "Finance",
        "Total": CollectionSubitem(42, "$"),
    }
    doc.collection = Collection(
        view=CollectionView.CUSTOM,
        initial_document="data.csv",
        schema={
            "Department": CollectionField(CollectionFieldKind.STRING, "Department", order=1),
            "Total": CollectionField(CollectionFieldKind.NUMBER, "Total", order=2),
        },
        sort=(CollectionSortKey("Department"),),
        navigator=Navigator("FilmStrip"),
        root_folder=CollectionFolder(0, "Portfolio", children=(CollectionFolder(1, "Q1"),)),
    )
    print(doc.attachments["data.csv"].read(), doc.collection.navigator.layout)
```

## Web Capture {#web-capture}

`doc.web_capture.by_url(url)` と `by_id(source_digest)` は、ソースごとに記録されたページセットと
画像セットを書き込み可能な Arlington ビューとして返します。ページセットの `O` はページを、画像セットの `O` は
画像ストリームを指し、`SI` には URL エイリアスや保存コマンドなどのソース情報が入ります。これらの読み取りは
オフラインで動作します。

ソース位置を変更するには、`typed.SourceInformation` ビューまたはそのタプルを渡して
`doc.web_capture.set_sources(content, source)` を呼びます。URL インデックスが更新され、旧 URL や新 URL を
共有する他のセットも保たれます。`remove(content)` はセットをインデックスから外し、メンバーの親識別子を消去します。
セットに含まれるページや、その画像を使うページを削除・複製する前に該当セットを取り除いてください。ページの並べ替えでは
セットの所属は保たれます。

PDF 1.3–1.7 で間接格納された `typed.WebCapturePageSet` や `typed.WebCaptureImageSet` を登録するには
`doc.web_capture.add(content)` を呼びます。16 バイトのソースダイジェスト、メンバー参照、ソース辞書、
画像の場合はページ参照数を指定します。PDF 2.0 では新規 Web Capture レコードは非推奨ですが、既存レコードの
読み取り・編集・削除は引き続き行えます。

## ページラベルと名前付きページ {#page-labels}

`doc.page_labels` は文書の `PageLabelRange` の列を、`page.label` は 1 ページのラベルを返します。
リストを代入すると置き換わり、最初の範囲はページ 0 から始めます。ページの挿入・移動・削除の後も、
各ページは同じラベルを保ちます。`doc.named_pages` は `/Names /Pages` の文字列キーから
`Page` ハンドルへの対応を読み書きし、削除されたページを指す項目は同じトランザクションで取り除きます。

```python
import pyropdf
from pyropdf import PageLabelRange, PageLabelStyle

with pyropdf.open("input.pdf") as doc:
    doc.page_labels = [
        PageLabelRange(0, PageLabelStyle.LOWER_ROMAN),   # i, ii, iii, ...
        PageLabelRange(1, prefix="A-"),                  # A-1, A-2, ...
    ]
    doc.named_pages["summary"] = 0
    doc.named_pages["appendix"] = doc.pages[1]
    print(doc.pages[1].label, doc.pages.labeled("A-1"), doc.named_pages["appendix"].index)
```

## プレゼンテーションとサムネイル

`page.duration` はプレゼンテーション中に自動的に次へ進むまでの表示秒数、`page.transition`
はページ切り替えの `Transition` 効果、`page.navigation_steps` はページ内ナビゲーションの
`NavigationStep` の列（`/PresSteps`）を表します。`TransitionAction` はナビゲーションステップ、
リンク、しおりから切り替え効果を実行します。`page.thumbnail` はページに埋め込まれた `/Thumb`
プレビューを `Pixmap` として読み書きし、いずれも `del` で削除できます。

```python
import pyropdf
from pyropdf import NavigationStep, Transition, TransitionAction, TransitionStyle

with pyropdf.open("input.pdf") as doc:
    slide = doc.pages[0]
    slide.duration = 5.0
    slide.transition = Transition(style=TransitionStyle.WIPE, duration=0.8, direction=0)
    slide.navigation_steps = [
        NavigationStep(
            next_action=TransitionAction(Transition(style=TransitionStyle.FADE)),
            duration=2.0,
        ),
    ]
    slide.thumbnail = slide.pixmap(fit=96)
    assert slide.thumbnail is not None
    print(slide.transition.style, len(slide.navigation_steps), slide.thumbnail.width)
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

カタログの表示設定やアクセシビリティのプロパティ（`doc.page_mode`、`doc.page_layout`、
BCP 47 言語タグの `doc.language`、`doc.mark_info`、`doc.viewer_preferences`）も
代入と `del` で読み書きできます。

```python
import pyropdf
from pyropdf import Duplex, MarkInfo, PageLayout, PageMode, ReadingDirection, ViewerPreferences

with pyropdf.open("input.pdf") as doc:
    doc.page_mode = PageMode.USE_OUTLINES
    doc.page_layout = PageLayout.TWO_PAGE_LEFT
    doc.language = "ja-JP"
    doc.mark_info = MarkInfo(marked=True)
    doc.viewer_preferences = ViewerPreferences(
        display_title=True,
        direction=ReadingDirection.R2L,
        duplex=Duplex.FLIP_LONG_EDGE,
        print_page_ranges=((0, 1),),
    )
    print(doc.page_mode, doc.language, doc.viewer_preferences.display_title)
    del doc.viewer_preferences
```

## 署名構造と暗号化ペイロードのラッパー {#signature-structures}

[署名](versions.md#signing)そのものに加えて、`doc.sign` と `doc.prepare_signature` は
DocMDP 証明権限（`DocMdpPermission`、`doc.certification` と `sig.doc_mdp` で読み取り）と
FieldMDP フィールドロック（`FieldLock` と `FieldMdpAction`、`field.lock` と
`sig.field_locks` で読み取り）を設定します。`doc.security_store` は文書セキュリティストア
（`/DSS` および `vri_key` をキーとする `/VRI`）を重複排除された間接ストリームの
`SecurityStore` として読み書きし、増分保存でも既存の署名を壊しません。
`doc.timestamp(timestamper)` は RFC 3161 の `/DocTimeStamp`（`sig.is_timestamp`）を追記し、
`doc.legal_attestation` は `Catalog["Legal"]`（`LegalAttestation`）を編集します。また
独自の暗号化ハンドラ向けに、`wrap_encrypted_payload` は PDF 2.0 の非暗号化ラッパー文書
（ISO 32000-2 §7.6.7）を作成し、`doc.encrypted_payload` が `EncryptedPayload` として読み戻します。
この関数で作成した文書では、表示用のファイル名はファイル仕様辞書に保存されます。添付ファイルを編集するには、
`doc.collection.initial_document` を `doc.attachments` のキーに使います。
この識別子で、ほかの添付ファイルがある場合も対象を特定できます。

```python
import pyropdf
from pyropdf import (
    DocMdpPermission,
    EncryptedPayload,
    Encryption,
    FieldLock,
    FieldMdpAction,
    LegalAttestation,
    TextField,
    wrap_encrypted_payload,
)

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].widgets.add(TextField("amount", (72, 300, 220, 324)))
    doc.prepare_signature(
        "author_sig",
        doc_mdp=DocMdpPermission.FORM_FILLING_AND_SIGNATURES,
        field_lock=FieldLock(FieldMdpAction.INCLUDE, ("amount",)),
        reason="請求書の証明",
    )
    doc.legal_attestation = LegalAttestation(annotations=0, optional_content=False)
    assert doc.form["author_sig"].lock == FieldLock(FieldMdpAction.INCLUDE, ("amount",))
    locked_bytes = doc.to_bytes(
        encryption=Encryption(user_password="reader", owner_password="owner"),
    )

with wrap_encrypted_payload(EncryptedPayload(locked_bytes, filename="invoice.pdf")) as wrapper:
    assert wrapper.encrypted_payload is not None
    print(wrapper.encrypted_payload.filename, wrapper.encrypted_payload.subtype)
```

DSS には PDF 2.0 が必要です。古い PDF のバージョンを上げると構造上の欠陥が生じる場合、設定時に `PdfError` が発生し、元のリビジョンが保たれます。次の例では、新規文書にサンプルの署名データを使います。既存の署名には実際の CMS データを `vri_key` に渡し、署名対象のリビジョンを保つために差分保存します。

```python
import pyropdf
from pyropdf import SecurityStore, ValidationInfo, vri_key

with pyropdf.Document() as doc:
    doc.pages.new((612, 792))
    key = vri_key(b"cms-contents")
    info = ValidationInfo(created="D:20260101000000Z")
    doc.security_store = SecurityStore(vri={key: info})
    assert doc.security_store.vri[key] == info
```

## ビューポート・地理空間計測・文書要件 {#viewports-and-requirements}

`page.viewports` は `/CropBox`・`/Rotate`・`/UserUnit` を反映した表示座標でページの計測ビューポート
（`/VP`）を読み書きします。CAD 縮尺の単位連鎖を表す `RectilinearMeasure`（`NumberFormat`、
`NumberFormatFraction`）と、EPSG コードや WKT 座標系を表す `GeospatialMeasure`
（`CoordinateSystem`、`PointData`）を扱え、座標値や換算係数は `decimal.Decimal` として保持し、
`float` の丸めなしに正確な十進桁のまま PDF オブジェクトストアへ書き込みます。`doc.requirements` は
`Catalog["Requirements"]`（`DocumentRequirement`、`RequirementType`、`RequirementHandler`）を管理し、
`del page.viewports` や `del doc.requirements` は 1 つのトランザクションで項目を取り除いて未参照オブジェクトを解放します。

ビューポート辞書、画像・フォーム XObject のストリーム、3D・RichMedia 注釈の
`obj` では、`owner.geospatial_measure` で地理空間情報を読み書きできます。
2D では、単位正方形内の各点に対応する地上座標を指定します。3D 注釈では
`RequirementType.GEOSPATIAL_3D` を宣言し、`dimensions=3` でワールド座標と標高を指定します。
`owner.geospatial_point(index)` は、所有オブジェクトの PDF 座標と対応する地上座標を返します。
地理座標の順序は緯度・経度、投影座標の順序は東距・北距です。投影座標では
投影変換行列が地上座標配列に優先します。PDF 1.7 で Adobe 拡張レベル 3 以上を宣言していれば、
2D の地理座標をその版のまま書けます。PDF 2.0 への昇格で既存オブジェクトに構造上の問題が
生じる場合は、編集前の状態を保ちます。形式の合わない登録も PDF 辞書から直接確認できます。
指定した Decimal 値は全桁を保存します。既存 PDF の数値を読むときの精度は
パーサーに依存します（[パーサーの対応状況 #16](https://github.com/yhay81/pyropdf/issues/16)）。

```python
from decimal import Decimal
import pyropdf
from pyropdf import (
    CoordinateSystem,
    DocumentRequirement,
    GeospatialMeasure,
    NumberFormat,
    NumberFormatFraction,
    PointData,
    RectilinearMeasure,
    RequirementHandler,
    RequirementType,
    Viewport,
)

lat = Decimal("35.6812361234567890123456789")
lon = Decimal("139.7671249876543210987654321")

with pyropdf.Document() as doc:
    page = doc.pages.new()
    page.viewports = (
        Viewport(
            (36, 36, 260, 220),
            name="Floor plan",
            measure=RectilinearMeasure(
                "1 in = 10 ft",
                x=(
                    NumberFormat(
                        "ft",
                        Decimal("0.1388888888888889"),
                        fraction=NumberFormatFraction.FRACTION,
                        denominator=16,
                    ),
                ),
                distance=(NumberFormat("ft", Decimal("0.1388888888888889")),),
                area=(NumberFormat("sq ft", Decimal("0.0192901234567901")),),
            ),
        ),
        Viewport(
            (36, 240, 260, 460),
            name="Site map",
            measure=GeospatialMeasure(
                CoordinateSystem(epsg=4326),
                (lat, lon, lat, lon),
                lpts=(0, 0, 1, 1),
                pdu=("M", "SQM", "DEG"),
            ),
            point_data=PointData(("LAT", "LON", "ALT"), ((lat, lon, Decimal("42.5")),)),
        ),
    )
    doc.requirements = (
        DocumentRequirement(
            RequirementType.GEOSPATIAL_2D,
            handlers=(RequirementHandler("JS", script="app.alert('Geospatial viewer required');"),),
            penalty=50,
        ),
    )
    saved = doc.to_bytes()
    assert b"35.6812361234567890123456789" in saved

with pyropdf.open(saved) as reopened:
    assert not reopened.check()
    assert len(reopened.pages[0].viewports) == 2
    assert reopened.requirements[0].type is RequirementType.GEOSPATIAL_2D
```

## 文書交換と構造名前空間 {#interchange-and-structure-namespaces}

`doc.extensions` は直接辞書・配列の拡張宣言（`DeveloperExtension`、`ISO_EXTENSIONS`）を管理し、
`associated_files` は文書・ページ・構造要素・注釈・文書パートに `AssociatedFileRelationship` 付きの関連ファイルを結びつけ、
`piece_info` はカタログとページに `/PieceInfo` アプリケーション辞書を格納し、
`doc.parts` は `DocumentPartAction` が参照できる `/DPartRoot` ページ範囲ツリーを構築し、
`doc.structure.namespaces` は PDF 1.7（`PDF_1_7_NAMESPACE`）・PDF 2.0（`PDF_2_0_NAMESPACE`）・独自名前空間を 1 つの構造ツリーで混在させます。

```python
import pyropdf
from pyropdf import (
    PDF_1_7_NAMESPACE,
    PDF_2_0_NAMESPACE,
    AssociatedFileRelationship,
    DeveloperExtension,
    DocumentPartAction,
    OutlineEntry,
)

with pyropdf.Document() as doc:
    first = doc.pages.new()
    second = doc.pages.new()
    doc.extensions.add(DeveloperExtension("ISO_", "2.0", 32001))
    doc.associated_files.add(
        "source.xml",
        b"<invoice><total>42</total></invoice>",
        media_type="application/xml",
        relationship=AssociatedFileRelationship.SOURCE,
    )
    first.piece_info.set("ACME", {"stage": "approved"})
    part = doc.parts.add_part(first, second, dpm={"Record": "INV-1"})
    doc.outline = [OutlineEntry(1, "Invoice part", DocumentPartAction(part))]

    ns20 = doc.structure.namespaces.add(PDF_2_0_NAMESPACE)
    ns17 = doc.structure.namespaces.add(PDF_1_7_NAMESPACE)
    custom = doc.structure.namespaces.add("https://example.com/ns", role_map={"Callout": ("Aside", ns20)})
    root_elem = doc.structure.add("Document", namespace=ns20)
    root_elem.children.add("TOC", namespace=ns17)
    note = root_elem.children.add("Callout", namespace=custom)
    assert note.resolved_role == ("Aside", PDF_2_0_NAMESPACE)
    saved = doc.to_bytes()

with pyropdf.open(saved) as reopened:
    assert not reopened.check()
    assert reopened.associated_files[0].filename == "source.xml"
    assert isinstance(reopened.outline[0].destination, DocumentPartAction)
```

## サニタイズ {#sanitizing}

`doc.sanitize(path)` は JavaScript・アクション・添付ファイル・メタデータ・コメント・不可視テキスト・
未使用オブジェクトを種類ごとのスイッチに従って取り除き、文書を全体保存するので、ファイルのリビジョンは 1 つになります。
戻り値の `SanitizeReport` は種類ごとに取り除いた数と、結果が壊す署名とプロファイルの主張、
そして結果がもう宣言しない主張を示します。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    report = doc.sanitize("clean.pdf", comments=False)
    print(dict(report.removed), report.changes.signatures, report.dropped_claims)
```

## フォントのサブセット化

`doc.subset_fonts()` は埋め込まれた TrueType・OpenType・CFF の各プログラムを、
すべてのオプショナルコンテンツグループを表示にした状態でページが描くグリフだけに、1 つのトランザクションで絞ります。
グリフ ID はそのままで、使わないアウトラインだけを空にするので、テキスト抽出と幅は変わりません。
注釈の外観やフォームの既定リソースから届くプログラムはそのまま残ります。
戻り値の `SubsetFont` は、それぞれ小さくなったプログラムを 1 つ表します。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for font in doc.subset_fonts():
        print(font.name, font.kept, font.glyphs, font.size_before, font.size_after)
    doc.save("smaller.pdf")
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

## 印刷製版データ {#print-production-data}

`page.box_styles` は CropBox・BleedBox・TrimBox・ArtBox に保存されたガイド線の設定を表します。
省略されたガイドはビューア側の設定に従い、実際の適用有無はページボックス自体で決まります。
`page.separations` は分版済みシートのページハンドルを版の順に並べます。

```python
import pyropdf

with pyropdf.Document() as doc:
    cyan, black = doc.pages.new(), doc.pages.new()
    cyan.separations = {
        cyan: pyropdf.SeparationPlate(colorant=pyropdf.Name("Cyan")),
        black: pyropdf.SeparationPlate(colorant=pyropdf.Name("Black")),
    }
    assert list(black.separations) == [cyan, black]
```

構成要素や順序を変えるにはグループを代入し直します。`del cyan.separations` で両ページからグループが
消去されます。版の移動では同一性が保たれますが、グループの一部だけを削除・取り込みしたり、個別の版を複製したりする
前にグループから切り離す必要があります。

`PrinterMark` と `Watermark` は同じ文書内の既存 Form XObject を受け取ります。注釈の `print_appearance` は
選択された通常外観とその資源を解決します。`Watermark` の `fixed_print` には印刷媒体に対する行列と平行移動の比率を
保存します。レガシーのトラップネットワークでは、選択した Form からプロセスカラーモデル・版名・トラップ領域を参照でき、
注釈辞書には元のトラッピング時点のスナップショットが残ります。画像や Form の `opi` は既存プロキシファイル仕様と
バージョン固有のパラメータを公開しており、レガシーデータの編集に使えます。

これらの保存パラメータは意図された出力仕様を記述するものです。デバイス分版、トラップ生成、OPI 置換、物理的な
面付け配置はレンダリングエンジン側の担当です（[描画タスク #248](https://github.com/yhay81/pyropdf/issues/248)・
[エンジントラッカー #7](https://github.com/yhay81/pyropdf/issues/7)）。
