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

増分保存は既存の署名が対象とするバイト列を保ちます。`report.signatures` は、そのバイト範囲内のオブジェクトに
加えられた編集を特定します。全体保存は署名済みバイト列を書き直すため、開いたときのすべての署名を挙げます。

増分保存では、`report.permissions` が各署名の DocMDP 証明権限や FieldMDP ロックのもとで各編集の可否を示します。
各項目には署名、変換種別、編集の種類、オブジェクト番号、影響を受けるフォームフィールドが含まれます。`permitted` は
判定できた編集で `True` または `False` となり、`None` は追加確認が必要なことを示します。このレポートは文書を開いた後の
編集だけを対象とし、元の署名の検証や過去リビジョンの評価とは別の操作です。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].rotation = 90
    report = doc.changes(incremental=True)
    for effect in report.permissions:
        print(effect.signature, effect.transform, effect.field, effect.permitted, effect.reason)
```

P=1 はフォーム入力を禁止し、P=2 はフォーム入力と署名を許可し、P=3 はマークアップ注釈の変更も許可します。
ただし DocMDP が許可する入力であっても、FieldMDP ロックで禁止されることがあります。このレポートは助言用であり、
検証器によってはより厳しい方針を適用します（たとえば pyHanko の既定差分ポリシーは P=3 でもマークアップ変更を警告します。
[pyHanko 差分解析](https://docs.pyhanko.eu/en/latest/api-docs/pyhanko.sign.diff_analysis.html)を参照）。

`report.claims` は、編集によって崩れうる PDF/A や PDF/UA の適合宣言を列挙します。

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

`Encryption` または `RecipientEncryption` で `method="aes-gcm"` を指定すると、ISO/TS 32003:2023 拡張付きの
PDF 2.0 として保存します。ストアは暗号化された文字列とストリームを返す前に認証タグを検証し、破損があれば
`MalformedPdfError` を出します。増分更新でも `Encryption.KEEP` 付きの全体保存でも、未編集オブジェクトの暗号文は
そのまま保たれます。別の方式へ切り替えるには全体保存が必要です。AES-GCM ファイルの直接描画とテキスト抽出は、
hayro 側の認証済みオブジェクトソース対応待ちです（[エンジン統合 #968](https://github.com/yhay81/pyropdf/issues/968)）。

256 ビットのパスワード暗号化・受信者暗号化では `integrity=True` で ISO/TS 32004:2024 の文書 MAC を付与できます。
出力を開いて `doc.verify_integrity()` を呼ぶと元のバイト列を検証し、状態として `"valid"`・`"invalid"`・`"absent"`・
`"locked"` のいずれかを返します。`"valid"` の場合は単独トークンか、署名またはタイムスタンプに紐づくトークンかを
示します。署名の信頼検証は `signature.verify()` で行います。

有効な MAC がある文書を増分編集すると、元のリビジョンを保ったまま新しい単独トークンを追記します。署名やタイムスタンプでは
新しいトークンが署名へ付与されます。`Encryption.KEEP` による全体保存は単独保護を更新し、新しい 256 ビット暗号化でも
MAC を再発行できます。署名付き MAC リビジョンの全体保存、保護の解除、MAC 保護出力のリニアライズは書き込み前に拒否されます。

### 標準パスワード暗号化 {#standard-password-encryption}

下表は標準パスワードハンドラの対応範囲です。`V` と `R` は暗号化辞書のアルゴリズムバージョンとリビジョンを表します。
いずれの形式もユーザーパスワードとオーナーパスワードの両方で開けます。ユーザーパスワードで報告される権限はファイル由来で、
オーナーパスワードはすべての権限を許可します。

| 既存ハンドラ | V / R | 全体保存での新規パスワード暗号化 | 直接ページ描画 |
| --- | --- | --- | --- |
| RC4, 40 bit | 1 / 2 | 非対応 | ユーザーまたはオーナーパスワード |
| RC4, 128 bit | 2 / 3 | 非対応 | ユーザーまたはオーナーパスワード |
| RC4, V2 暗号フィルタ | 4 / 4 | 非対応 | ユーザーまたはオーナーパスワード |
| AES-128, AESV2 | 4 / 4 | `method="aes-128"` | ユーザーまたはオーナーパスワード |
| AES-256, AESV3 | 5 / 5 | 非対応 | ユーザーまたはオーナーパスワード |
| AES-256, AESV3 | 5 / 6 | `method="aes-256"`（既定） | ユーザーまたはオーナーパスワード |
| AES-GCM, AESV4 | 6 / 7 | `method="aes-gcm"` | [エンジン対応待ち #968](https://github.com/yhay81/pyropdf/issues/968) |

これら 7 方式のいずれも、`Encryption.KEEP` 付きの全体保存は未編集オブジェクトの暗号文をそのまま複製し、新しい文字列と
ストリームだけを既存ハンドラで暗号化します。増分保存も既定で同じ挙動をとり、元のファイルを先頭に保ちます（編集がなければ
元のバイト列そのままです）。既定の `encryption=None` で全体保存すると暗号化を取り除きます。

バイトリーダーは UTF-8 資格情報を受け付けます。リビジョン 2〜4 では、実効ユーザーパスワードを UTF-8 として渡せる場合、
オーナー認証でも動作します。非 UTF-8 ユーザーパスワードを持つ古いファイルもオーナー認証後に編集・保存できますが、
直接描画とテキスト抽出はその符号化に対して `EncryptedDocumentError` を出します（[バイト符号化資格情報 #1043](https://github.com/yhay81/pyropdf/issues/1043)）。

## 署名 {#signing}

### 署名アルゴリズム {#signature-algorithms}

ISO/TS 32002:2022 の表 3・表 4 に基づく PDF 2.0 の楕円曲線と文書ダイジェストの組み合わせ、および
ISO/TS 32001:2022 が追加した SHA3-256・SHA3-384・SHA3-512・SHAKE256 に対応します。ダイジェストは
署名者または `Document.timestamp` の `digest_algorithm=` で指定でき、拡張アルゴリズムを使うと必要な
ISO 拡張エントリを自動で書き込みます。SHAKE256 は 64 バイトを出力します。

| 署名鍵 | 利用できる文書ダイジェスト | 秘密鍵操作 |
| --- | --- | --- |
| NIST P-256 | `sha256`, `sha3_256` | PEM、PKCS#12、コールバック |
| NIST P-384 | `sha384`, `sha3_384` | PEM、PKCS#12、コールバック |
| NIST P-521 | `sha512`, `sha3_512` | PEM、PKCS#12、コールバック |
| Brainpool P256r1 | `sha256`, `sha384`, `sha512`, `sha3_256`, `sha3_384`, `sha3_512` | PEM、PKCS#12、コールバック |
| Brainpool P384r1 | `sha384`, `sha512`, `sha3_384`, `sha3_512` | PEM、PKCS#12、コールバック |
| Brainpool P512r1 | `sha512`, `sha3_512` | PEM、PKCS#12、コールバック |
| Ed25519 | `sha512` | PEM、PKCS#12、コールバック（Pure Ed25519） |
| Ed448 | `shake256` | PEM、PKCS#12、コールバック（Pure Ed448） |

`Signature.verify` は PDF バイト範囲のダイジェストと署名属性に対する署名を検証します。P-521・Brainpool・Edwards 鍵の
読み込みと検証には `cryptography` をインストールしてください。RSA コールバックでも PKCS#1 v1.5（RFC 9688）で 3 種の
SHA-3 ダイジェストを使えます。Ed448 では、属性付き CMS は整数パラメータ 512 を持つ `id-shake256-len`、属性なし CMS は
パラメータなしの `id-shake256` を指定します。Ed25519 のダイジェストはパラメータなしの SHA-512 です。ISO/TS 32001 は
署名フィールドシード辞書の `/DigestMethod` 名を拡張しており、正誤表 236 は署名参照辞書への提案変更を削除しています。

`tests/test_signing.py` と `tests/test_verify.py` の再現可能なフィクスチャが全行の鍵と証明書を生成し、20 通りの
組み合わせすべてについて既存フィールドへの入力・再署名・pyHanko 0.38.1 での両リビジョン検証・独立署名済み PDF の検証を
行っています。pyHanko・ASN.1 コーデック・暗号バックエンドのバージョンは `uv.lock` に固定されています。

`Signer.from_pem` と `Signer.from_pkcs12` は上表の鍵と証明書チェーンを読み込み、
`Signer.callback` は外部の鍵・HSM・PKCS#11 トークンに署名を委ねます。`doc.sign(field, signer)` は
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

## リビジョン {#revisions}

`doc.revisions` は開いた PDF の増分リビジョンを古い順（`0 .. n - 1`、`sig.revision` と同じ番号）に並べます。
各 `Revision` は `end` と `added`・`updated`・`freed`・`objects` の間接オブジェクト番号を持ち、
`rev.open()` はファイルのバイト列を共有した独立の `Document` としてそのリビジョンを開きます。たとえば
`doc.revisions[sig.revision].open()` で署名 `sig` が対象とした時点の文書をそのまま確認できます。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].rotation = 90
    updated = doc.to_bytes(incremental=True)

with pyropdf.open(updated) as doc:
    for rev in doc.revisions:
        with rev.open() as earlier:
            print(rev.index, rev.updated, earlier.pages[0].rotation)
```
