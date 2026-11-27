---
title: PyMuPDF・pylopdf からの移行
description: PyMuPDF（fitz / pymupdf）およびアーカイブ済み pylopdf 0.15/0.16 から pyropdf へ移行するための対比ガイドと自動コード変換ツール（codemod）の使い方。
---

# PyMuPDF・pylopdf からの移行

pyropdf は MIT ライセンスの Rust コアと単一の版付きオブジェクトストア上で、日常的な PDF 処理を独自の Pythonic な API として提供します。[DESIGN.md §10.1](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md#101-rules)（*One way*）に従い、`pyropdf` 本体パッケージには**互換エイリアスや `fitz` シムを一切含めません**。代わりに、このページで PyMuPDF や `pylopdf` 0.15 の代表的なパターンを対比しつつ、機械的な書き換えを `tools/codemod.py` で自動化できます。

## 自動コード変換（codemod） {#codemod}

リポジトリ同梱の標準ライブラリ製 AST codemod を使うと、機械的な名前変更をプレビュー・一括適用できます：

```bash
# ファイルを書き換えずに unified diff をプレビュー
python3 tools/codemod.py --dry-run --diff path/to/your_app/

# PyMuPDF（fitz / pymupdf）呼び出しをその場で書き換え
python3 tools/codemod.py --from pymupdf path/to/your_app/

# pylopdf 0.15 呼び出しをその場で書き換え
python3 tools/codemod.py --from pylopdf path/to/your_app/

# 未移行の呼び出しが残っていれば CI で非ゼロ終了
python3 tools/codemod.py --check path/to/your_app/
```

コメントや書式を保ったまま、インポート、`doc.pages` の添字・反復、保存オプション、メタデータ／添付／テキスト呼び出し、Enum 定数などを書き換えます。機械変換できない箇所や挙動差（`apply_redactions`、`Story`、`get_text("dict")`、`csCMYK`、`insert_text`、`get_toc`、不変ジオメトリの破壊的メソッド呼び出し等）は `file:line: [code] message` 形式で警告します。

## 主な設計・挙動の違い {#differences}

コードを移植する前に、意図的に異なる 4 点を確認してください：

1. **すべての座標が表示空間（display space）で統一。** ページの読み書きに使う座標（`page.rect`、`word.bbox`、`canvas.text`、注釈、リンク、ページボックス）は、CropBox と `/Rotate` 適用後の**左上原点・下向き y・ポイント単位**です。回転や y 軸反転を手動補正する必要はありません。
2. **ページ番号はすべて 0 始まり。** `doc.pages`、`doc.outline`、`doc.page_labels` はすべて 0 始まりです（PyMuPDF の `get_toc()` / `set_toc()` は 1 始まり）。
3. **アトミックなトランザクションと不変値。** `with doc.transaction():` で変更を 1 つのアンドゥ可能な版（`doc.history`）にまとめられ、例外時は丸ごと破棄されます。ジオメトリ（`Point`、`Rect`、`Matrix`）と `Pixmap` は不変値なので、破壊的メソッドの代わりに `r = r | p`、`r = r & o`、`m = ~m` を使います。
4. **PDF 専用スコープと即時反映セッタ。** `pyropdf.open()` は PDF 専用です（XPS・EPUB・HTML `Story` は対象外。画像の PDF 化は `doc.pages.new_from_image()` を使います）。ハンドル上のセッタ（`page.rotation = 90`、`doc.form["name"].value = "Ada"`）は依存構造を即時更新するため、`widget.update()` や `annot.update()` は不要です。

## 開く・調べる・保存する {#open-save}

| PyMuPDF / pylopdf 0.15 | pyropdf | 備考 |
| --- | --- | --- |
| `import fitz` / `import pymupdf` / `import pylopdf` | `import pyropdf` | 実行時エイリアスは提供しません |
| `fitz.open()` | `pyropdf.Document()` | 空の PDF を作成 |
| `fitz.open(stream=data, filetype="pdf")` | `pyropdf.open(data)` | パス・`bytes`・`bytearray`・`memoryview` を受け付けます |
| `pylopdf.peek_metadata(path)` | `pyropdf.probe(path)` | 全体を開かずにヘッダ・メタ情報を取得 |
| `doc.save(path, deflate=True, linear=True, use_objstms=True)` | `doc.save(path, compress=True, linearize=True, object_streams=True)` | `garbage=1..4` は `prune=True`・`renumber=True`・`dedupe=True` で指定 |
| `doc.saveIncr()` | `doc.save(path, incremental=True)` | 元のバイト列を先頭にそのまま保持 |
| `doc.tobytes()` | `doc.to_bytes()` | PDF バイト列をメモリ上に生成 |

```py title="移行前（PyMuPDF）"
import fitz

with fitz.open("input.pdf") as doc:
    data = doc.tobytes(deflate=True, linear=True)
    doc.save("out.pdf", deflate=True, garbage=4)
```

```python title="移行後（pyropdf）"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    data = doc.to_bytes(compress=True, linearize=True)
    doc.save("out.pdf", compress=True, prune=True, renumber=True, dedupe=True)
```

## ページ操作とジオメトリ {#pages-geometry}

| PyMuPDF | pyropdf | 備考 |
| --- | --- | --- |
| `len(doc)`, `doc.page_count` | `len(doc.pages)` | ライブなシーケンスビュー |
| `doc[i]`, `doc.load_page(i)` | `doc.pages[i]` | `Page` ハンドルは移動・挿入後もページ本体に追従 |
| `page.number`, `page.parent` | `page.index`, `page.document` | `page.index` は定数時間 |
| `doc.delete_page(i)`, `doc.move_page(a, b)` | `del doc.pages[i]`, `doc.pages.move(a, b)` | 参照整合性によりリンクや目次を維持 |
| `doc.insert_pdf(other)` | `doc.pages.extend(other.pages)` | 任意位置なら `doc.pages.insert(index, page)` |
| `page.set_rotation(90)`, `page.remove_rotation()` | `page.rotation = 90`, `page.remove_rotation()` | 表示座標の一貫性を維持 |
| `r.include_point(p)`, `r.intersect(o)`, `m.invert()` | `r = r | p`, `r = r & o`, `m = ~m` | ジオメトリは不変値 |

```py title="移行前（PyMuPDF）"
import fitz

with fitz.open("input.pdf") as doc:
    page = doc.load_page(0)
    page.set_rotation(90)
    r = fitz.Rect(10, 20, 100, 80)
    r.include_point((140, 120))
    doc.move_page(0, 1)
```

```python title="移行後（pyropdf）"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.rotation = 90
    r = pyropdf.Rect(10, 20, 100, 80) | (140, 120)
    doc.pages.move(0, 1)
    assert page.index == 1 and r.x1 == 140
```

## テキスト・単語・検索・表・Markdown {#reading}

| PyMuPDF / pymupdf4llm | pyropdf | 備考 |
| --- | --- | --- |
| `page.get_text()` | `page.text()` | 既定で表示読み順。`/ActualText` を無効にする場合は `actual_text=False` |
| `page.get_text("words")` | `page.words()` | frozen な `Word` レコード（`.bbox`, `.text`, `.block`, `.line`, `.word`）を返却 |
| `page.get_text("dict")` / `"html"` / `"xml"` | `page.layout(chars=True).to_dict()` / `.to_html()` / `.to_xml()` | 列指向の `page.chars()` も利用可能 |
| `page.search_for(q)` | `page.search(q)` | 回転テキストには `quads=True` |
| `page.find_tables()` | `page.tables()` | `.rows` と `.cells` を持つ `Table` レコードを返却 |
| `pymupdf4llm.to_markdown(doc)` / `to_chunks` | `doc.markdown()`, `doc.to_chunks()`, `doc.to_json()` | 標準搭載。構造ツリーを自動活用（`use_structure="auto"`） |

```py title="移行前（PyMuPDF）"
import fitz

with fitz.open("input.pdf") as doc:
    page = doc[0]
    text = page.get_text(sort=True)
    words = [(w[4], w[:4]) for w in page.get_text("words")]
    hits = page.search_for("Invoice")
```

```python title="移行後（pyropdf）"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    text = page.text()
    words = [(word.text, word.bbox) for word in page.words()]
    hits = page.search("Invoice")
    md = doc.markdown()
    chunks = doc.to_chunks(max_chars=1000)
```

## 描画（レンダリング）と Pixmap {#rendering}

| PyMuPDF | pyropdf | 備考 |
| --- | --- | --- |
| `page.get_pixmap(dpi=150, annots=False)` | `page.pixmap(dpi=150, annotations=False)` | `antialias=`、`hinting=`、`glyph_cache=`、`colorspace="rgb" \| "gray"` に対応 |
| `page.get_svg_image()` | `page.svg()` | 文字はパス出力（`text_as_path=False` は `#435` で追跡中） |
| `pix.save("page.png")`, `pix.tobytes("jpeg")` | `pix.save("page.png")`, `pix.to_bytes("jpeg")` | `bytes(pix)` / `memoryview(pix)` で PEP 3118 バッファ公開 |
| `pix.invert_irect()`, `pix.tint_with(...)` | `pix.invert()`, `pix.tint(...)`, `pix.rotate(...)`, `pix.crop(...)` | 不変値：各操作は新しい `Pixmap` を返却 |

```py title="移行前（PyMuPDF）"
import fitz

with fitz.open("input.pdf") as doc:
    pix = doc[0].get_pixmap(dpi=144, alpha=False, annots=True)
    pix.save("page.png")
```

```python title="移行後（pyropdf）"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pix = doc.pages[0].pixmap(dpi=144, alpha=False, annotations=True, antialias=True)
    pix.save("page.png")
    gray = pix.to_gray()
```

## Canvas による描画 {#drawing}

| PyMuPDF / pylopdf 0.15 | pyropdf | 備考 |
| --- | --- | --- |
| `page.new_shape()` / `draw_rect` / `draw_line` | `with page.draw() as canvas:` | 正常終了時に一括コミット、例外時は変更を破棄 |
| `page.insert_text(pt, text, fontsize=14)` | `canvas.text(text, pt, font=..., size=14)` | 表示座標（左上原点） |
| `page.insert_textbox(rect, text)` | `canvas.textbox(rect, text, font=..., size=12)` | `TextFit` レコードを返却 |
| `Font(fontname="Helvetica")` / `Font(fontfile=p)` | `Font.standard("Helvetica")` / `Font.from_file(p)` | 埋め込みフォントは `harfrust` で整形し保存時にサブセット化 |
| `page.insert_image(rect, filename=p)` | `canvas.image(rect, p)` | パス・bytes・`Pixmap`・`PageImage` を受け付け |
| `page.show_pdf_page(rect, src_doc, pno)` | `canvas.page(rect, src_doc.pages[pno])` | `clip=`・`rotate=`・`keep_aspect=` に対応 |

```py title="移行前（PyMuPDF）"
import fitz

doc = fitz.open()
page = doc.new_page(width=400, height=200)
page.draw_rect(fitz.Rect(20, 20, 380, 180), color=(0.1, 0.3, 0.7), width=2)
page.insert_text((36, 72), "Hello from PDF", fontsize=18)
doc.save("drawn.pdf")
```

```python title="移行後（pyropdf）"
import pyropdf

with pyropdf.Document() as doc:
    page = doc.pages.new((400, 200))
    with page.draw() as canvas:
        canvas.rect(
            (20, 20, 380, 180),
            stroke=pyropdf.Stroke(paint=pyropdf.Color(0.1, 0.3, 0.7), width=2),
        )
        canvas.text(
            "Hello from PDF",
            (36, 72),
            font=pyropdf.Font.standard("Helvetica"),
            size=18,
        )
    doc.save("drawn.pdf")
```

## 注釈・リンク・フォーム {#annotations-forms}

| PyMuPDF / pylopdf 0.15 | pyropdf | 備考 |
| --- | --- | --- |
| `page.annots()`, `page.add_highlight_annot(quads)` | `page.annotations`, `page.annotations.add(Highlight(quads))` | 仕様 dataclass から外観ストリームを自動生成 |
| `page.get_links()`, `page.insert_link(d)` | `page.links`, `page.links.add(rect, target)` | URI 文字列または型付き遷移先／アクションレコード |
| `doc.set_form_field(name, value)` / `widget.update()` | `doc.form[name].value = value` | 外観を即時再生成（`update()` 不要） |
| `page.add_widget(w)` | `page.widgets.add(TextField(name, rect, ...))` | 検証済み frozen 仕様 dataclass |
| `doc.bake(annots=True, widgets=True)` | `doc.flatten(annotations=True, widgets=True)` | ページ内容へ焼き込み |

```py title="移行前（PyMuPDF）"
import fitz

with fitz.open("input.pdf") as doc:
    page = doc[0]
    page.add_highlight_annot(page.search_for("Invoice"))
    page.add_text_annot((36, 36), "Reviewed")
    doc.save("annotated.pdf")
```

```python title="移行後（pyropdf）"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.annotations.add(pyropdf.Highlight(page.search("Invoice")))
    page.annotations.add(pyropdf.Note((36, 36), contents="Reviewed"))
    page.widgets.add(pyropdf.TextField("reviewer", (36, 120, 220, 144), value="Ada"))
    doc.form["reviewer"].value = "Ada Lovelace"
    doc.save("annotated.pdf")
```

## 暗号化・署名・トランザクション・アンドゥ {#security-transactions}

| PyMuPDF | pyropdf | 備考 |
| --- | --- | --- |
| `doc.needs_pass`, `doc.authenticate(pw)` | `doc.needs_password`, `doc.unlock(pw)` | 誤パスワード時は `0` ではなく `PasswordError` を送出 |
| `doc.save(p, encryption=..., user_pw=...)` | `doc.save(p, encryption=Encryption(user_password=...))` | 標準 AES・公開鍵受信者・AES-GCM に対応 |
| `doc.journal_undo()`, `doc.journal_redo()` | `with doc.transaction("name"):`, `doc.history.undo()`, `doc.history.redo()` | 永続化は `doc.history.save()` / `doc.history.restore(data)` |
| — | `doc.changes(incremental=True)` | 保存前に無効化される署名や PDF/A・PDF/UA 宣言を確認 |
| — | `doc.sign(...)`, `doc.signatures` | PAdES B-B / B-T 署名と署名検証を標準搭載 |

```py title="移行前（PyMuPDF）"
import fitz

with fitz.open("input.pdf") as doc:
    doc.journal_enable()
    doc.journal_start_op("rotate")
    doc[0].set_rotation(90)
    doc.journal_stop_op()
    doc.journal_undo()
    doc.save("encrypted.pdf", encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="open")
```

```python title="移行後（pyropdf）"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    with doc.transaction("rotate"):
        doc.pages[0].rotation = 90
    doc.history.undo()
    doc.history.redo()
    report = doc.changes(incremental=True)
    assert report.signatures == ()
    doc.save(
        "encrypted.pdf",
        encryption=pyropdf.Encryption(user_password="open", owner_password="admin"),
    )
```

## 既知のギャップと対象外機能 {#gaps}

移行前に、以下の未対応項目・対象外項目を確認してください（完全な比較表は [evidence/pymupdf-comparison/features.md](https://github.com/yhay81/pyropdf/blob/main/evidence/pymupdf-comparison/features.md) を参照）：

| 領域 | 現在の状態 | 追跡 Issue |
| --- | --- | --- |
| 恒久的な墨消し（`apply_redactions`） | `RedactionMark` 注釈と `doc.sanitize()` は実装済み。下層のテキスト・画像・ベクタの完全除去は provenance 対応待ち | [#362](https://github.com/yhay81/pyropdf/issues/362), [#104](https://github.com/yhay81/pyropdf/issues/104) |
| 矩形外コンテンツの削除（`clip_to_rect`） | `page.cropbox` は表示を隠すだけでストリームからは除去しません | [#420](https://github.com/yhay81/pyropdf/issues/420) |
| CMYK ラスタ出力（`csCMYK`） | `page.pixmap()` は RGB / gray 対応。CMYK ラスタ化はエンジン対応待ち | [#425](https://github.com/yhay81/pyropdf/issues/425), [#433](https://github.com/yhay81/pyropdf/issues/433) |
| SVG の選択可能テキスト（`text_as_path=False`） | `page.svg()` はグリフをパスとして出力 | [#435](https://github.com/yhay81/pyropdf/issues/435) |
| 非 PDF 入力・HTML レイアウト（`Story`、`insert_htmlbox`） | 意図的に対象外（`pyropdf` は PDF と画像の PDF 化に専念） | [#355](https://github.com/yhay81/pyropdf/issues/355) |
| 入力の安全性 | [状態と安全性](status.md#safety) と [API の安定性](stability.md) を参照 | [#15](https://github.com/yhay81/pyropdf/issues/15) |
