---
title: Versions and saving
description: Transactions, undo and redo, change reports, incremental and full saves, encryption, and PAdES signing.
---

# Versions and saving

See [Concepts](concepts.md) for the version model behind edits and handles.

## Transactions

`doc.transaction(name)` groups the edits of its block into one step. If the
block raises, the document returns to the version before it, without a copy.

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

An edit outside a transaction is a step of its own.

## Undo and redo

`doc.history` lists the steps by name; `undo` and `redo` move between them.

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

## Saving

`doc.save(path)` and `doc.to_bytes()` write the current version.

- **Incremental** (`incremental=True`) appends only the objects that the
  edits wrote, after the original bytes, which stay as they were. Digital
  signatures over the original bytes stay valid.
- **Full** (the default) rewrites the whole file. `compress`,
  `object_streams`, `prune`, `renumber`, and `dedupe` shape the result.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].rotation = 90
    doc.save("appended.pdf", incremental=True)
    doc.save("rewritten.pdf", compress=True, object_streams=True, prune=True)
```

A document saved without edits reproduces its input: an incremental save
byte for byte, a full save in every value.

## Change reports

An incremental save preserves the bytes that existing signatures cover.
`report.signatures` identifies edits to objects in those byte ranges. A
full save rewrites the signed bytes and lists every opened signature.

For an incremental save, `report.permissions` describes each proposed edit
under each applicable signature's DocMDP certification or FieldMDP lock.
Each entry names the signature, transform, kind of edit, object number and
affected form field. `permitted` is true or false for a classified edit.
A value of `None` calls for further review. The report describes edits since
opening. Verifying the original signature and evaluating previous revisions
are separate operations.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].rotation = 90
    report = doc.changes(incremental=True)
    for effect in report.permissions:
        print(effect.signature, effect.transform, effect.field, effect.permitted, effect.reason)
```

P=1 forbids a form fill, P=2 allows filling and signing, and P=3 also
allows markup annotation changes. A FieldMDP lock can still forbid a fill
that DocMDP permits. The report is advisory. Validators can apply a stricter
policy. For example, pyHanko's default diff policy flags markup changes at
P=3. [pyHanko difference analysis](https://docs.pyhanko.eu/en/latest/api-docs/pyhanko.sign.diff_analysis.html)

`report.claims` lists PDF/A and PDF/UA claims that the edits may break.

## Encryption

`Encryption` sets passwords, permissions, and the method, AES-256 by
default. `pyropdf.open(..., password=...)` or `doc.unlock` opens an
encrypted document.

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

Use `method="aes-gcm"` on `Encryption` or `RecipientEncryption` to save
a PDF 2.0 file with the ISO/TS 32003:2023 extension. The store authenticates
each encrypted string and stream before returning it. A damaged authentication
tag raises `MalformedPdfError`. Both an incremental update and a full
`Encryption.KEEP` save preserve the ciphertext of untouched objects.
Selecting a new method requires a full save. Direct page rendering and text
extraction from an AES-GCM file await the authenticated object source in
hayro. [Engine integration #968](https://github.com/yhay81/pyropdf/issues/968)

A 256-bit password or recipient encryption option accepts `integrity=True`
to add an ISO/TS 32004:2024 document MAC. Open the output and call
`doc.verify_integrity()` to check the original bytes. Its status is
`"valid"`, `"invalid"`, `"absent"` or `"locked"`. A valid result identifies
a standalone token or a token attached to a signature or document timestamp.
Signature trust is reported by `signature.verify()`.

With a valid MAC, an incremental edit writes a new standalone token and
retains the original revision. Signing and timestamping attach the new token
to the signature. A full save with `Encryption.KEEP` renews standalone
protection, and a replacement 256-bit encryption can request a new MAC.
Full saves of signed MAC revisions, removal of protection, and linearization
of MAC-protected output are refused before writing.

## Signing

`Signer.from_pem` and `Signer.from_pkcs12` load ECDSA P-256 or P-384 keys with
their certificate chains, and `Signer.callback` delegates signing to an
external RSA key, HSM, or PKCS#11 token. `doc.sign(field, signer)` writes a CMS
/ PAdES signature (`B-B`, or `B-T` when a `timestamper` callback returns an
RFC 3161 token) in a new incremental revision, so earlier signed bytes stay
unchanged, and returns the signed PDF bytes. `doc.signatures` lists the signed
fields of the opened file, and `sig.verify(trust=...)` checks each signature,
its timestamp, and later modifications offline.

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

## Revisions

`doc.revisions` lists the incremental file revisions of an opened PDF, oldest
first (`0 .. n - 1`), matching `sig.revision`. Each `Revision` reports `end`
along with the indirect object numbers in `added`, `updated`, `freed`, and
`objects`, and `rev.open()` opens that revision as an independent `Document`
sharing the file's bytes—for example, `doc.revisions[sig.revision].open()`
opens the exact state that `sig` signed.

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
