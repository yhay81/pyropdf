---
title: 制限とエラー
description: DocumentLimits の検査範囲、制限を設けた別プロセスでアップロードを読む方法、エラーの扱い方を説明します。
---

# 制限とエラー

## 制限

`DocumentLimits` は、ファイルサイズ、ページ数、オブジェクト数と、pyropdf が
操作ごとに計測する仕事量を検査します。制限に達して操作を中断したときは、
`LimitError.code` でどの制限に達したかを確認できます。

```python
import pyropdf

limits = pyropdf.DocumentLimits(max_pages=1_000, max_decompressed_size=64 * 2**20, max_duration=10.0)
try:
    with pyropdf.open("input.pdf", limits=limits) as doc:
        print(doc.pages[0].text())
except pyropdf.LimitError as error:
    print("refused:", error.code)
```

`DocumentLimits.web()` は文書を開くときにすべてのオブジェクトを検査します。
ストア経由で展開するストリームには 1 本あたり 64 MiB、ページのコンテンツストリームには
さらに 1 本あたり 10 MiB の上限を設けます。この検査を通った PDF でも、テキスト抽出や
描画でデータを展開するとサイズが大きくなることがあります。このプロファイルでは
`max_memory_size` と `max_duration` は未設定です。

`CancellationToken` を使うと、別のスレッドから操作の取り消しを要求できます。
pyropdf は操作の予算を確認する際に、このトークンを検査します。

```python
import pyropdf

token = pyropdf.CancellationToken()
limits = pyropdf.DocumentLimits(cancellation=token)
with pyropdf.open("input.pdf", limits=limits) as doc:
    token.cancel()
    try:
        doc.pages[0].text()
    except pyropdf.LimitError as error:
        print(error.code)  # cancelled
```

## 読み取りと描画 {#reading-and-rendering}

`page.text()`、`page.words()`、`page.paths()`、`page.content()`、
`page.pixmap()` は、いずれも hayro でページを解釈します。下表の検査範囲は
5 つのメソッドに共通です。

| 項目 | pyropdf が検査する範囲 | 制限を超えうる処理 |
| --- | --- | --- |
| `max_page_content_size` | `page.operations` などでストアがフィルタを使って展開した、ページのコンテンツストリーム 1 本あたりのサイズ。 | 上記 5 メソッドでは hayro がページのコンテンツを直接展開する。 |
| `max_decompressed_size` | ストア経由で読み取るストリーム 1 本あたりの展開後のサイズ。フィルタが出力用のメモリを確保してから検査する場合がある。 | 描画時の画像など、hayro が直接展開するストリームはこの検査を通らない。 |
| `max_memory_size` | pyropdf が操作の予算に計上するメモリ。 | hayro の作業用メモリや描画バッファにより、プロセスの使用量は上限を超えうる。 |
| `max_duration` | pyropdf が予算を確認した時点での経過時間。 | hayro が展開・解釈・描画を続けている間に期限が過ぎることがある。pyropdf が期限超過を検出できるのは、次に予算を確認したとき。 |

`text()` と `words()` のグリフ数とテキスト量、`paths()` と `content()` の要素数、
`pixmap()` の出力寸法は、それぞれのメソッドが収集・出力する量を制限します。
結果が小さくても、解釈に多くの処理を要するページがあります。上流での対応は
[ストリーム展開の制限 (#48)](https://github.com/yhay81/pyropdf/issues/48)、
[インタプリタの予算と取り消し (#105)](https://github.com/yhay81/pyropdf/issues/105)、
[内部データ構造の構築時の制限 (#15)](https://github.com/yhay81/pyropdf/issues/15) で追跡しています。

## アップロードされた PDF を別プロセスで読む {#reading-a-pdf-in-a-worker}

アップロードされた PDF を開くところからページの処理までを別プロセスで行います。
次の Linux 向けの例は、`input.pdf` の 1 ページ目の単語数を返します。
util-linux の `prlimit` で Python が起動する前に仮想アドレス空間の上限を 1 GiB に設定し、
実時間で 10 秒が経過したら GNU `timeout` がプロセスを強制終了します。

```sh
--8<-- "includes/read-in-worker.sh"
```

上限は処理内容に合わせて選び、終了コードが 0 以外ならそのリクエストを失敗として扱います。
ページの描画が必要な場合は、同じプロセス内で `pixmap()` を呼べます。[状態と安全性](status.md)も参照してください。

## 制限コード {#limit-codes}

表は英語のまま載せています。

--8<-- "limit-codes.md"

## エラー

pyropdf が出すエラーは、どれも `PdfError` から派生します。

| エラー | 出るとき |
| --- | --- |
| `MalformedPdfError` | 入力が読める PDF ではない。 |
| `InternalError` | pyropdf またはエンジンで Rust の panic が起きた。メッセージには panic の内容が残るため、ライブラリの不具合として報告できる。 |
| `PasswordError` | パスワードが違う。 |
| `EncryptedDocumentError` | ロックされた文書を `unlock` の前に読んだ。 |
| `LimitError` | 操作が制限に達したか、取り消された。`code` がどれかを示す。 |
| `StalePageError`、`StaleObjectError` | ハンドルがそのページやオブジェクトより長く使われた。 |
| `DocumentClosedError` | `close` の後に文書を使った。 |
| `OcrError` | OCR モデルが見つからないか、失敗した。 |

`InternalError` は `except PdfError` と `except Exception` のどちらでも捕捉できます。
失敗したトランザクションは破棄され、同じ文書を引き続き利用できます。
プロセスの panic hook は変更しないため、既定の診断は標準エラー出力に残ります。
この境界が捕捉するのは unwind する panic であり、ランタイムが abort する場合は復旧できません。
