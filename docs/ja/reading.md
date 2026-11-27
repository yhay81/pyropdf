---
title: 内容を読む
description: テキスト、単語、文字、レイアウト、検索、表、Markdown、JSON、検索チャンク、タグ付き読み上げ順、オフライン OCR。
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
既定では marked-content の `/ActualText` が内包するグリフのテキストを置き換えますが、
`page.text()`・`page.words()`・`page.layout()`・`page.chars()`・`page.search()`・
`annotation.text()` に `actual_text=False` を渡すと、元のグリフの Unicode 値を直接取り出せます。

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

文書がタグ付きで信頼性チェック（`/MarkInfo << /Marked true >>`、`/Suspects` なし、
正常な構造ツリー、非アーティファクト本文の十分な網羅率）を満たす場合、既定の
`use_structure="auto"` で構造ツリーから見出し（`H1`〜`H6`・`H`・`Title`）、箇条書き
（`L`/`LI`/`Lbl`/`LBody`）、表（`Table`/`THead`/`TBody`/`TFoot`/`TR`/`TH`/`TD`）、
引用、コード、`/Alt` つき図、`/ActualText`、論理読み上げ順を取り出し、`Artifact` の
ヘッダ・フッタを自動で除外します。独自ロールは `/RoleMap` と PDF 2.0 名前空間で解決
されます。`/E` による略語展開を使うときは `expand_abbreviations=True`、信頼性チェックを
省いて構造ツリーを読むときは `use_structure="always"`、幾何レイアウトを強制するときは
`use_structure="never"` を渡します。`"auto"` では、未タグページや未解決の Form XObject
を含むページは自動的に幾何レイアウトへフォールバックします。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for table in doc.pages[0].tables():
        print(table.markdown())
    print(doc.markdown())
    print(doc.markdown(use_structure="never"))  # 幾何レイアウトを強制
```

## JSON と検索チャンク {#json-and-retrieval-chunks}

`doc.to_json()` は同じページ要素（見出し、段落、箇条書き項目、表、画像、引用、コードブロック）を
ページ番号と表示座標の矩形つきで JSON として出力します。構造ツリーから抽出した場合は、
要素の `alt_text` や継承された BCP 47 の `language` も含めます。`doc.to_chunks()` は
連続する要素を文字数上限（`max_chars`、既定は 2000）以内の Markdown 検索チャンクに
まとめます。見出しごとに新しいチャンクが始まり、各 `Chunk` は上位見出しの `section`、
対象ページの `pages`、主要言語 `language`、出典つきの `elements` を保持します。表・画像・
見出し・コードブロックは分割されず（上限を超えると `chunk.oversized` が `True` になります）、
どちらのメソッドも `pages=`、`table_strategy=`、`use_structure=`、`expand_abbreviations=`
を受け取ります。

```python
import json
import pyropdf

with pyropdf.open("input.pdf") as doc:
    data = json.loads(doc.to_json())
    print(len(data["pages"]), [elem["kind"] for elem in data["pages"][0]["elements"]])
    for chunk in doc.to_chunks(max_chars=500, expand_abbreviations=True):
        print(chunk.index, chunk.pages, chunk.section, chunk.language, chunk.text)
```

## タグ付き読み上げ順 {#tagged-reading-order}

タグ付き PDF では、`doc.structure.reading_order()` がページをまたいで構造ツリーの論理順に
テキストを取り出します。ページの marked-content 並びを読み取り、`Artifact` 要素を省き、
タグのない文書では空のタプル `()` を返します。各 `StructureText` は元の構造要素 `element`、
内容参照 `item`、テキスト `text`（代替テキストのない Form XObject など解決できない参照では
`None`）を保持します。要素の `actual_text` や `alt`（`Figure`・`Formula`・`Form`）は描画
グリフに優先し、`expand_abbreviations=True` で `/E` の略語展開も使います。`page.draw()` は
Form XObject として描画するため、描画内容にタグを付けて論理テキストを取り出すときは
`canvas.tag()` に `actual_text`・`alt`・`expanded` を指定してください。構造ツリーの編集は
[構造ツリーとアクセシビリティ](editing.md#structure-and-accessibility)を参照してください。

```python
import pyropdf
from pyropdf import TagRole

with pyropdf.Document() as doc:
    page = doc.pages.new((360, 200))
    with page.draw() as canvas:
        with canvas.tag(TagRole.HEADING_1, actual_text="Summary"):
            canvas.text("Summary", (36, 40), size=18)
        with canvas.tag(TagRole.SPAN, actual_text="PDF", expanded="Portable Document Format"):
            canvas.text("PDF", (36, 70), size=12)
        with canvas.tag(TagRole.FIGURE, alt="Revenue chart"):
            canvas.rect((36, 90, 140, 140), fill=pyropdf.Color(0.2, 0.4, 0.8))
        with canvas.tag(TagRole.ARTIFACT):
            canvas.text("Page 1", (36, 180), size=9)
    for item in doc.structure.reading_order(expand_abbreviations=True):
        print(item.element.standard_role, item.text)
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

`page.paths()` は同じパスを `Paths` の列として返します。1 行が 1 つのパスで、各パスのセグメントは
別の列に並び、パスやセグメントごとの Python オブジェクトは作らないので、数十万本の線の図面も
ページを描画するのとほぼ同じ時間で読めます。`column()` は NumPy がコピーなしで読める読み取り専用の
`memoryview` を返し、pyarrow・polars・DuckDB は Arrow PyCapsule interface で表全体を取り込めます。
行は必要に応じて `Path` レコードになります。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    paths = doc.pages[0].paths()
    print(len(paths), "paths of", len(paths.column("verb")), "segments")
    # numpy.asarray(paths.column("stroke_red")) は同じバッファを読む
    red = paths.column("stroke_red")
    print([path.bbox for path, value in zip(paths, red) if value == 1])
```

`page.chars()`（および `page.layout(chars=True).chars`）も文字を同じ列指向の
`Chars` として返し、グリフごとの Python オブジェクトを作りません。`chars.names` に
列名が並び、グリフ矩形（`x0`・`y0`・`x1`・`y1`）、ベースライン原点
（`origin_x`・`origin_y`）、UTF-8 テキストバッファ（`text_offsets`・`text_data`）、
レイアウト位置（`block`・`line`・`span`）、`is_synthetic`、描画元情報
（`stream`・`operation`、該当なしは `0`）を持ちます。`chars.column(name)` は
NumPy がコピーなしで読める読み取り専用の `memoryview` を返し、`chars[start:stop]` は
コピーなしのスライスビューを作ります。pyarrow・polars・DuckDB は Arrow PyCapsule
interface（`__arrow_c_schema__`・`__arrow_c_array__`。`text` は 1 つの UTF-8 列、
`stream`・`operation` は NULL 許容）で表全体を取り込め、添字アクセスや反復では
必要に応じて `Char` レコードが生成されます。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    chars = doc.pages[0].chars()
    x0 = chars.column("x0")
    offsets = chars.column("text_offsets")
    data = bytes(chars.column("text_data"))
    # numpy.asarray(x0) は同じバッファを読み、pyarrow.array(chars) は Arrow PyCapsule を使う
    print(len(chars), chars.names)
    print([(data[offsets[i] : offsets[i + 1]].decode(), x0[i]) for i in range(min(3, len(chars)))])
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

## オフライン OCR {#offline-ocr}

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
