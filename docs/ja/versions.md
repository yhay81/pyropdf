---
title: 版と保存
description: トランザクション、undo と redo、変更レポート、増分保存と全体保存、暗号化、PAdES 署名。
---

# 版と保存

編集とハンドルの土台になる版モデルは[概念](concepts.md)を参照してください。

## トランザクション

`doc.transaction(name)` はブロック内の編集を一つの一歩にまとめます。ブロックが
例外を出すと、文書は複製なしでその前の版に戻ります。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    try:
        with doc.transaction("rotate"):
            page.rotation = 90
            raise RuntimeError("やめておく")
    except RuntimeError:
        pass
    print(page.rotation)  # 0：ブロックの編集は何も残らない
```

トランザクションの外の編集は、それだけで一歩になります。

## undo と redo

`doc.history` は一歩ずつの名前を並べ、`undo` と `redo` でそのあいだを移ります。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("rotate"):
        page.rotation = 90
    with doc.transaction("note"):
        page.annotations.add(pyropdf.Note((40, 40), contents="あとで"))
    print(doc.history.steps, doc.history.position)  # ('rotate', 'note') 2
    doc.history.undo()
    doc.history.undo()
    print(page.rotation, len(page.annotations))      # 0 0
    doc.history.redo()
    print(page.rotation)                             # 90
```

## 保存 {#saving}

`doc.save(path)` と `doc.to_bytes()` は現在の版を書きます。

- **増分保存**（`incremental=True`）は、編集が書いたオブジェクトだけを元のバイト列の
  後ろに追記します。元のバイト列は変わらないので、そこにかかる電子署名は有効なままです。
- **全体保存**（既定）はファイル全体を書き直します。`compress`、`object_streams`、
  `prune`、`renumber`、`dedupe` で結果を調整できます。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].rotation = 90
    doc.save("appended.pdf", incremental=True)
    doc.save("rewritten.pdf", compress=True, object_streams=True, prune=True)
```

編集せずに保存した文書は入力を再現します。増分保存ならバイト単位で、全体保存なら
すべての値が一致します。

## 変更レポート

`doc.changes` は、保存する前に、編集が何を無効にするかを示します。署名されたバイト列が
変わる署名と、編集で崩れうる PDF/A・PDF/UA などの適合宣言です。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].rotation = 90
    report = doc.changes(incremental=False)
    print(bool(report), report.signatures, report.claims)
```

## 暗号化

`Encryption` でパスワード・権限・方式を決めます。既定の方式は AES-256 です。暗号化された
文書は `pyropdf.open(..., password=...)` か `doc.unlock` で開きます。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.save(
        "locked.pdf",
        encryption=pyropdf.Encryption(user_password="reader", owner_password="owner"),
    )

with pyropdf.open("locked.pdf", password="reader") as doc:
    print(doc.encryption_method, doc.permissions)
```

## 署名 {#signing}

`Signer.from_pem` と `Signer.from_pkcs12` は ECDSA（P-256・P-384）の鍵と証明書チェーンを読み込み、
`Signer.callback` は外部の RSA 鍵・HSM・PKCS#11 トークンに署名を委ねます。`doc.sign(field, signer)` は
CMS / PAdES 署名（`B-B`、`timestamper` コールバックが RFC 3161 トークンを返すときは `B-T`）を新しい
増分リビジョンとして追記するため、前の署名対象のバイト列は変わらず、署名済み PDF のバイト列が返ります。
`doc.signatures` は開いたファイルの署名済みフィールドを並べ、`sig.verify(trust=...)` は各署名・
タイムスタンプ・後続リビジョンでの変更をオフラインで検証します。

```python
from pathlib import Path

import pyropdf


def sign_pdf(key_pem: bytes, cert_pem: bytes) -> None:
    signer = pyropdf.Signer.from_pem(key_pem, cert_pem)
    with pyropdf.open("input.pdf") as doc:
        signed = doc.sign("approval", signer, reason="承認")
    Path("signed.pdf").write_bytes(signed)


with pyropdf.open("input.pdf") as doc:
    for sig in doc.signatures:
        check = sig.verify()
        print(sig.field, check.intact, check.valid, check.modifications)
```
