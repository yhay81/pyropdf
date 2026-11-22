# Encrypted fixtures

pylopdf's tests/assets/encrypted at 884af0dbf21973cfd434a9eca4e769de62fcc25c,
which [its `generate.py`](https://github.com/yhay81/pylopdf/blob/884af0dbf21973cfd434a9eca4e769de62fcc25c/tests/assets/encrypted/generate.py) wrote with pypdf and pikepdf.
The passwords are user `userpw` and owner `ownerpw`; `owneronly-*` has an
empty user password. Each file has two pages, whose text begins
"Encrypted page one" and "Encrypted page two".

`user-rc4-128-indirect-length.pdf` is pyropdf's own, which `generate.py`
writes with no library: V2 R3 128-bit RC4, with the same passwords and page
texts. Each content stream's `/Length` refers to an object, one uncompressed
and one in an object stream, and its encrypted data ends in a line feed.

## Authenticated AES

`generate_gcm.py` writes `user-aes-gcm.pdf` using pyHanko 0.38.1 and the
standard handler from ISO/TS 32003:2023. The owner and user passwords are
`ownerpw` and `userpw`. The page has a literal label and a separate painted
content stream. Every run generates fresh keys and nonces. No document MAC
is requested. The tests also use pyHanko to open pyropdf output.

## Public-key recipients

Run `uv run python tests/assets/encrypted/recipients/generate.py` to recreate
the recipient fixtures with pyHanko 0.38.1. The committed DER certificates and
PKCS#8 private keys are public test data. Fresh CMS secrets and AES IVs make
each generation different. Every file has the same page string and painted
content. The first recipient gets modification permission. The second gets
permission to change encryption.

| File in `recipients/` | Adobe.PubSec mode |
| --- | --- |
| `pyhanko-gcm.pdf` | adbe.pkcs7.s5, V6 AESV4 |
| `pyhanko-recipients.pdf` | adbe.pkcs7.s5, V5 AESV3 |
| `pyhanko-identity-strings.pdf` | V5 AESV3 streams, Identity strings and clear XMP |
| `pyhanko-recipients-aes128.pdf` | adbe.pkcs7.s5, V4 AESV2 |
| `pyhanko-s3.pdf` | adbe.pkcs7.s3, V1 RC4-40 |
| `pyhanko-s4.pdf` | adbe.pkcs7.s4, V2 RC4-128 |
| `pyhanko-v2.pdf` | adbe.pkcs7.s5, V4 with the V2 RC4-128 crypt filter |

The tests in `tests/test_encrypted.py` also read pyropdf's AES-128 and
AES-256 output with pyHanko and compare exact string, stream and XMP bytes.

## Document MAC

`integrity/generate.py` uses pyHanko 0.38.1 (pinned in `uv.lock`) to create
the standalone table, standalone xref stream, signed incremental revision and
RSA-recipient files in `integrity/`. The password fixtures use userpw and
ownerpw. The signed and recipient cases use the local recipient-owner test
key. All four contain the same unmodified blue rectangle.

`tests/test_document_mac.py` checks the six digest algorithms in ISO/TS
32004:2024 Table 8, verifies the signed and unsigned forms, and uses pyHanko
to validate pyropdf-generated and renewed MACs. The tests use the
[published 2024 edition](https://www.iso.org/standard/45877.html).
