---
title: 版本与保存
description: 事务、撤销与重做、变更报告、增量保存与完整保存、加密以及 PAdES 签名。
---

# 版本与保存

关于编辑与句柄背后的版本模型，请参阅[核心概念](concepts.md)。

## 事务

`doc.transaction(name)` 将代码块内的编辑合并为一个步骤。若代码块抛出异常，文档会回退至事务开始前的版本，且不会复制文档。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    try:
        with doc.transaction("rotate"):
            page.rotation = 90
            raise RuntimeError("changed my mind")
    except RuntimeError:
        pass
    print(page.rotation)  # 0: nothing of the block stayed
```

事务之外的每次编辑各自构成一个独立步骤。

## 撤销与重做

`doc.history` 按名称列出历史步骤；`undo` 与 `redo` 在各步骤之间前进或回退。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("rotate"):
        page.rotation = 90
    with doc.transaction("note"):
        page.annotations.add(pyropdf.Note((40, 40), contents="Later"))
    print(doc.history.steps, doc.history.position)  # ('rotate', 'note') 2
    doc.history.undo()
    doc.history.undo()
    print(page.rotation, len(page.annotations))      # 0 0
    doc.history.redo()
    print(page.rotation)                             # 90
```

## 保存 {#saving}

`doc.save(path)` 和 `doc.to_bytes()` 写出当前版本。

- **增量保存**（`incremental=True`）仅将本次编辑写入的对象追加在原始字节之后，原始字节保持原封不动，因此覆盖原始字节的数字签名依然有效。
- **完整保存**（默认）会重写整个文件，可通过 `compress`、`object_streams`、`prune`、`renumber` 和 `dedupe` 调整输出结果。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].rotation = 90
    doc.save("appended.pdf", incremental=True)
    doc.save("rewritten.pdf", compress=True, object_streams=True, prune=True)
```

未经编辑直接保存的文档会准确复现输入：增量保存逐字节一致，完整保存则在所有 PDF 值上保持等价。

## 变更报告

增量保存会保留现有签名所覆盖的原始字节。`report.signatures` 标识对这些字节范围内对象所做的编辑；完整保存会重写已签名字节，因而会列出文档打开时的全部签名。

对于增量保存，`report.permissions` 会依据每个适用签名的 DocMDP 认证权限或 FieldMDP 锁定策略描述每项待保存的编辑。每条记录列明签名、转换类型、编辑种类、对象编号及受影响的表单字段。对于已分类的编辑，`permitted` 为 `True` 或 `False`；值为 `None` 表示需要进一步人工核查。该报告仅分析自文档打开后的编辑；验证原始签名和评估先前修订版属于独立操作。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].rotation = 90
    report = doc.changes(incremental=True)
    for effect in report.permissions:
        print(effect.signature, effect.transform, effect.field, effect.permitted, effect.reason)
```

`P=1` 禁止填写表单，`P=2` 允许填写表单和签名，`P=3` 还允许修改标记批注。即使 DocMDP 允许填写，FieldMDP 锁仍可禁止修改特定字段。该报告仅供参考，具体验证器可能执行更严格的策略——例如 pyHanko 的默认差异策略在 `P=3` 下也会标记批注修改（参见 [pyHanko 差异分析](https://docs.pyhanko.eu/en/latest/api-docs/pyhanko.sign.diff_analysis.html)）。

`report.claims` 列出本次编辑可能破坏的 PDF/A 和 PDF/UA 合规声明。

## 加密

`Encryption` 用于设置密码、权限与加密算法，默认为 AES-256。打开加密文档可使用 `pyropdf.open(..., password=...)` 或 `doc.unlock`。

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

在 `Encryption` 或 `RecipientEncryption` 上指定 `method="aes-gcm"`，即可按 ISO/TS 32003:2023 扩展保存为 PDF 2.0 文件。对象存储在返回每个加密字符串和流之前都会校验其认证标签，标签受损时会抛出 `MalformedPdfError`。增量更新和使用 `Encryption.KEEP` 的完整保存均会保留未修改对象的原始密文；切换至新加密方式则必须使用完整保存。针对 AES-GCM 文件的直接页面渲染与文本提取需等待 hayro 的认证对象源支持（跟踪于[引擎集成 #968](https://github.com/yhay81/pyropdf/issues/968)）。

256 位密码加密或收件人加密选项支持传入 `integrity=True` 以附加 ISO/TS 32004:2024 文档 MAC。打开输出文件并调用 `doc.verify_integrity()` 可验证原始字节，返回状态为 `"valid"`、`"invalid"`、`"absent"` 或 `"locked"`。状态为 `"valid"` 时，结果还会区分独立令牌与附加到签名或文档时间戳的令牌。签名本身的信任状态由 `signature.verify()` 报告。

面对带有有效 MAC 的文档，增量编辑会在保留原修订版的同时写入一个新的独立令牌；签名与时间戳则会将新令牌附加到签名上。使用 `Encryption.KEEP` 执行完整保存会更新独立保护，更换为新的 256 位加密时也可申请新的 MAC。对已签名的 MAC 修订版执行完整保存、移除保护或线性化带 MAC 保护的输出，都会在写入前被拒绝。

### 标准密码加密 {#standard-password-encryption}

下表列出了标准密码处理器的支持范围。`V` 与 `R` 分别为加密字典的算法版本和修订号。表中列出的每种格式均可使用用户密码或所有者密码打开。使用用户密码打开时报告的权限来自文件设置，使用所有者密码则授予全部权限。

| 现有处理器 | V / R | 完整保存时新建密码加密 | 支持直接页面渲染的凭据 |
| --- | --- | --- | --- |
| RC4, 40 bit | 1 / 2 | 不支持 | 用户或所有者密码 |
| RC4, 128 bit | 2 / 3 | 不支持 | 用户或所有者密码 |
| RC4, V2 加密过滤器 | 4 / 4 | 不支持 | 用户或所有者密码 |
| AES-128, AESV2 | 4 / 4 | `method="aes-128"` | 用户或所有者密码 |
| AES-256, AESV3 | 5 / 5 | 不支持 | 用户或所有者密码 |
| AES-256, AESV3 | 5 / 6 | `method="aes-256"`（默认） | 用户或所有者密码 |
| AES-GCM, AESV4 | 6 / 7 | `method="aes-gcm"` | 等待[引擎支持](https://github.com/yhay81/pyropdf/issues/968) |

对于上述七种处理器，带 `Encryption.KEEP` 的完整保存都会原样复制未修改对象的密文，并沿用原有处理器加密新增的字符串与流。增量保存默认行为相同，并保留原始文件作为前缀；若无任何编辑则返回原字节。默认 `encryption=None` 的完整保存则会移除加密。

字节读取器接受 UTF-8 凭据。对于修订版 2 至 4，只要有效用户密码可以以 UTF-8 形式提供，所有者访问同样适用。带有非 UTF-8 用户密码的旧文件仍可在所有者认证后编辑和保存，但直接渲染与文本提取针对该编码会抛出 `EncryptedDocumentError`（跟踪于[字节编码凭据 #1043](https://github.com/yhay81/pyropdf/issues/1043)）。

## 签名 {#signing}

### 签名算法 {#signature-algorithms}

下表对应 ISO/TS 32002:2022 表 3 和表 4 中的 PDF 2.0 椭圆曲线与文档摘要组合。ISO/TS 32001:2022 进一步向签名和时间戳摘要添加了 SHA3-256、SHA3-384、SHA3-512 和 SHAKE256。可在签名器或 `Document.timestamp` 中通过 `digest_algorithm=` 指定摘要算法；使用扩展算法时，pyropdf 会自动写入所需的 ISO 扩展条目。SHAKE256 输出长度为 64 字节。

| 签名密钥 | 允许的文档摘要 | 私钥操作方式 |
| --- | --- | --- |
| NIST P-256 | `sha256`, `sha3_256` | PEM、PKCS#12 或回调 |
| NIST P-384 | `sha384`, `sha3_384` | PEM、PKCS#12 或回调 |
| NIST P-521 | `sha512`, `sha3_512` | PEM、PKCS#12 或回调 |
| Brainpool P256r1 | `sha256`, `sha384`, `sha512`, `sha3_256`, `sha3_384`, `sha3_512` | PEM、PKCS#12 或回调 |
| Brainpool P384r1 | `sha384`, `sha512`, `sha3_384`, `sha3_512` | PEM、PKCS#12 或回调 |
| Brainpool P512r1 | `sha512`, `sha3_512` | PEM、PKCS#12 或回调 |
| Ed25519 | `sha512` | PEM、PKCS#12 或回调（纯 Ed25519） |
| Ed448 | `shake256` | PEM、PKCS#12 或回调（纯 Ed448） |

`Signature.verify` 会验证 PDF 字节范围的摘要以及针对已签名属性的签名。加载私钥和验证 P-521、Brainpool 与 Edwards 密钥需要安装 `cryptography`。RSA 回调同样可将三种 SHA-3 摘要与 PKCS#1 v1.5（RFC 9688）配合使用。对于 Ed448，带属性的 CMS 指定整数参数为 512 的 `id-shake256-len`，无属性的 CMS 则使用省略参数的 `id-shake256`；对应的 Ed25519 摘要为省略参数的 SHA-512。ISO/TS 32001 扩展了签名字段种子字典的 `/DigestMethod` 名称，而勘误表 236 移除了针对签名引用字典的拟议变更。

`tests/test_signing.py` 与 `tests/test_verify.py` 中的可复现测试夹具会为表格每一行生成密钥和证书。全部 20 组组合都经过了填写既有字段、再次签名、在 pyHanko 0.38.2 中验证两个修订版以及验证独立签署 PDF 的完整测试。pyHanko 的 Ed25519 测试夹具编码器显式省略了 RFC 8419 要求缺失的 SHA-512 参数；另有独立测试通过本地 RFC 3161 机构校验对显式 NULL 的拒绝以及全部四种扩展时间戳摘要。pyHanko、其 ASN.1 编解码器与密码学后端的版本均锁定在 `uv.lock` 中。

`Signer.from_pem` 和 `Signer.from_pkcs12` 加载上表中的密钥及其证书链；`Signer.callback` 将私钥操作委托给外部密钥、HSM 或 PKCS#11 令牌。`doc.sign(field, signer)` 会在新的增量修订版中写入 CMS / PAdES 签名（`B-B`；当 `timestamper` 回调返回 RFC 3161 令牌时为 `B-T`），确保先前已签名的字节保持不变，并返回签名后的 PDF 字节。`doc.signatures` 列出已打开文件的全部已签名字段，`sig.verify(trust=...)` 则离线校验各签名、时间戳以及后续修订版中的修改。

```python
from pathlib import Path

import pyropdf


def sign_pdf(key_pem: bytes, cert_pem: bytes) -> None:
    signer = pyropdf.Signer.from_pem(key_pem, cert_pem)
    with pyropdf.open("input.pdf") as doc:
        signed = doc.sign("approval", signer, reason="Approved")
    Path("signed.pdf").write_bytes(signed)


with pyropdf.open("input.pdf") as doc:
    for sig in doc.signatures:
        check = sig.verify()
        print(sig.field, check.intact, check.valid, check.modifications)
```

## 修订版 {#revisions}

`doc.revisions` 按时间从早到晚（`0 .. n - 1`，与 `sig.revision` 编号一致）列出已打开 PDF 的各个增量文件修订版。每个 `Revision` 报告字节结束位置 `end`，以及 `added`、`updated`、`freed` 和 `objects` 中的间接对象编号；`rev.open()` 会将该修订版打开为一个共享文件字节的独立 `Document`——例如 `doc.revisions[sig.revision].open()` 可直接查看 `sig` 签署时的确切文档状态。

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
