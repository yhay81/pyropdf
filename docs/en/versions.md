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
  `object_streams`, `prune`, `renumber`, `dedupe`, `clean`, `ascii`,
  `pretty`, and `stream_filter` shape the result.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].rotation = 90
    doc.save("appended.pdf", incremental=True)
    doc.save("rewritten.pdf", compress=True, object_streams=True, prune=True)
    doc.save("inspect.pdf", clean=True, ascii=True, pretty=True)
    doc.save("filtered.pdf", stream_filter="ASCII85Decode")
```

- `clean=True` normalizes every page content stream and annotation appearance
  stream and prunes unnamed resources in a private save view without mutating
  the live document or its undo history.
- `ascii=True` emits only printable ASCII bytes (omitting the binary header
  comment, escaping strings and names, defaulting `cross_reference` to a
  classic table, and wrapping binary streams in `ASCII85Decode`), and
  `pretty=True` formats dictionaries and arrays with deterministic multi-line
  indentation for inspection and diffing.
- `stream_filter` selects an explicit filter for eligible non-image streams
  (`"keep"`, `"none"`, `"FlateDecode"`, `"ASCII85Decode"`,
  `"ASCIIHexDecode"`, or `"RunLengthDecode"`), or accepts a mapping
  `{xref: filter}` to override individual streams while leaving unlisted
  streams untouched.

`clean`, `ascii`, `pretty`, and `stream_filter` require a full save and are
refused when `incremental=True`. `ascii=True` is refused for encrypted or
linearized saves.

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
P=3 (see [pyHanko difference analysis](https://docs.pyhanko.eu/en/latest/api-docs/pyhanko.sign.diff_analysis.html)).

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
hayro (tracked in [engine integration #968](https://github.com/yhay81/pyropdf/issues/968)).

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

### Standard password encryption

The table covers the standard password handler. `V` and `R` are the
encryption dictionary's algorithm version and revision. Both passwords can
open each listed format. Permissions reported for a user password come from
the file. The owner password grants all permissions.

| Existing handler | V / R | New password encryption on full save | Direct page rendering with |
| --- | --- | --- | --- |
| RC4, 40 bit | 1 / 2 | Unavailable | User or owner password |
| RC4, 128 bit | 2 / 3 | Unavailable | User or owner password |
| RC4, V2 crypt filter | 4 / 4 | Unavailable | User or owner password |
| AES-128, AESV2 | 4 / 4 | `method="aes-128"` | User or owner password |
| AES-256, AESV3 | 5 / 5 | Unavailable | User or owner password |
| AES-256, AESV3 | 5 / 6 | `method="aes-256"` (default) | User or owner password |
| AES-GCM, AESV4 | 6 / 7 | `method="aes-gcm"` | Awaiting [engine support](https://github.com/yhay81/pyropdf/issues/968) |

Across these seven handlers, a full save with `Encryption.KEEP` copies
untouched ciphertext and encrypts new strings and streams with the existing
handler. An incremental save does the same by default and retains the
original file as its prefix. With no edits it returns the original bytes.
A full save with the default `encryption=None` removes encryption.

The byte reader accepts UTF-8 credentials. For revisions 2 through 4,
owner access also works when the effective user password can be supplied
as UTF-8. An older file with a non-UTF-8 user password can still be edited
and saved after owner authentication. Direct rendering and text extraction
raise `EncryptedDocumentError` for that encoding (tracked in
[byte-encoded credentials #1043](https://github.com/yhay81/pyropdf/issues/1043)).

## Signing

### Signature algorithms

These are the PDF 2.0 curve and document digest combinations in
ISO/TS 32002:2022 Tables 3 and 4. ISO/TS 32001:2022 adds SHA3-256,
SHA3-384, SHA3-512 and SHAKE256 to signature and timestamp digests.
Select a digest with `digest_algorithm=` on a signer or on
`Document.timestamp`. pyropdf writes the required ISO extension entries
when it uses an extended algorithm. SHAKE256 produces 64 bytes.

| Signing key | Allowed document digests | Private-key operation |
| --- | --- | --- |
| NIST P-256 | `sha256`, `sha3_256` | PEM, PKCS#12 or callback |
| NIST P-384 | `sha384`, `sha3_384` | PEM, PKCS#12 or callback |
| NIST P-521 | `sha512`, `sha3_512` | PEM, PKCS#12 or callback |
| Brainpool P256r1 | `sha256`, `sha384`, `sha512`, `sha3_256`, `sha3_384`, `sha3_512` | PEM, PKCS#12 or callback |
| Brainpool P384r1 | `sha384`, `sha512`, `sha3_384`, `sha3_512` | PEM, PKCS#12 or callback |
| Brainpool P512r1 | `sha512`, `sha3_512` | PEM, PKCS#12 or callback |
| Ed25519 | `sha512` | PEM, PKCS#12 or callback, pure Ed25519 |
| Ed448 | `shake256` | PEM, PKCS#12 or callback, pure Ed448 |

`Signature.verify` checks the digest of the PDF byte ranges and the
signature over the signed attributes. Install `cryptography` to load private keys and verify
P-521, Brainpool and Edwards keys. RSA callbacks can also use the
three SHA-3 digests with PKCS#1 v1.5 (RFC 9688). For Ed448, CMS with attributes
specifies `id-shake256-len` with an integer parameter of 512. CMS
without attributes uses `id-shake256` with absent parameters. The
corresponding Ed25519 digest is SHA-512 with absent parameters.
ISO/TS 32001 extends the signature-field seed dictionary's
`/DigestMethod` names. Erratum 236 removes the proposed change to
signature-reference dictionaries.

The reproducible fixtures in `tests/test_signing.py` and
`tests/test_verify.py` generate keys and certificates for each row.
Each of the 20 pairs is used to fill an existing field, sign again,
validate both revisions in pyHanko 0.38.2 and verify an independently
signed PDF. pyHanko's Ed25519 fixture encoder explicitly omits the
SHA-512 parameters required to be absent by RFC 8419. Separate tests
check the explicit-NULL rejection and all four extension timestamp
digests with a local RFC 3161 authority. The versions of pyHanko,
its ASN.1 codec and its crypto backend are pinned in `uv.lock`.

`Signer.from_pem` and `Signer.from_pkcs12` load keys from the table with
their certificate chains. `Signer.callback` delegates the private operation
to an external key, HSM, or PKCS#11 token. `doc.sign(field, signer)` writes a CMS
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
