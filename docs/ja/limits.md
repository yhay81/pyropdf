---
title: 制限とエラー
description: DocumentLimits で操作ごとの仕事量に上限を設け、取り消し、pyropdf の型付きエラーを扱います。
---

# 制限とエラー

## 制限

`DocumentLimits` は、文書の操作がしてよい仕事量に上限を設けます。対象はファイルの
大きさ、ページ数とオブジェクト数、ストリームを展開した大きさ、インタプリタへの入力、
1 回の操作が保持するメモリ、そして時間です。上限を超えそうな操作は止まり、`LimitError` を出します。`code` が
どの制限かを示します。

```python
import pyropdf

limits = pyropdf.DocumentLimits(max_pages=1_000, max_decompressed_size=64 * 2**20, max_duration=10.0)
try:
    with pyropdf.open("input.pdf", limits=limits) as doc:
        print(doc.pages[0].text())
except pyropdf.LimitError as error:
    print("refused:", error.code)
```

`DocumentLimits.web()` は外から届く文書向けの、より厳しいプロファイルで、文書を
開くときにすべてのオブジェクトを確かめます。

`CancellationToken` を使うと、別のスレッドから文書の操作を止められます。

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

制限が抑えるのは仕事量です。どんな入力にも安全だという主張ではありません。何を
約束し、何を約束しないかは[状態と安全性](status.md)にあります。

## 制限コード {#limit-codes}

表は英語のまま載せています。

--8<-- "limit-codes.md"

## エラー

pyropdf が出すエラーは、どれも `PdfError` から派生します。

| エラー | 出るとき |
| --- | --- |
| `MalformedPdfError` | 入力が読める PDF ではない。 |
| `PasswordError` | パスワードが違う。 |
| `EncryptedDocumentError` | ロックされた文書を `unlock` の前に読んだ。 |
| `LimitError` | 操作が制限に達したか、取り消された。`code` がどれかを示す。 |
| `StalePageError`、`StaleObjectError` | ハンドルがそのページやオブジェクトより長く使われた。 |
| `DocumentClosedError` | `close` の後に文書を使った。 |
| `OcrError` | OCR モデルが見つからないか、失敗した。 |
