# Signing spike (#366)

Can pyropdf sign a PDF as PAdES (ETSI EN 319 142-1) with a pure-Rust CMS
stack, and which stack? This directory holds a prototype that signs one
corpus document as PAdES B-B and B-T and validates both with pyHanko.

## Run

```bash
uv run --with pyhanko==0.38.0 python evidence/signing/prototype.py
```

[`prototype.py`](prototype.py) makes a test CA, a P-256 signer, and an
RSA time-stamping authority with openssl; adds a signature field to
`tests/assets/real_world/pdf20-simple.pdf` with pyropdf; saves
incrementally with a placeholder; and signs the byte ranges with
[`prototype/`](prototype), a separate cargo project on the RustCrypto
`cms` crate. Nothing leaves the machine: the time-stamp token comes from
pyHanko's local dummy TSA. [`results.md`](results.md) is its output, and
[`audit.txt`](audit.txt) is `cargo-audit audit --deny warnings` on the
prototype's `Cargo.lock`.

## Results

Both profiles validate in pyHanko 0.38.0, which applies Acrobat's rules
for the byte ranges, the incremental update, and DocMDP:

| Check | B-B | B-T |
| --- | --- | --- |
| intact, valid, trusted | yes | yes |
| bottom line | yes | yes |
| coverage | ENTIRE_FILE | ENTIRE_FILE |
| modification level | NONE | NONE |
| DocMDP, seed values | ok | ok |
| ETSI EN 319 102-1 | basic validation PASSED | validation with time PASSED |
| signature time-stamp | | valid, trusted |
| CMS size (of 8,192 reserved) | 769 bytes | 2,209 bytes |

The signer writes SigningCertificateV2 and the CMS builder adds the
content-type and message-digest attributes; it writes no signing-time,
as PAdES B-B requires. B-T adds the RFC 3161 token as the unsigned
`id-aa-timeStampToken` attribute.

## What pyropdf needs

The store already does the PDF half: `to_bytes(incremental=True)` appends
the signature dictionary, the field, and the AcroForm as one update, and
the original bytes stay intact. Two things are missing, and the
prototype works around them in Python:

- **A reserved `Contents`.** pyropdf writes no hex string, so the
  prototype reserves `Contents` as a literal of `0`s and replaces it with a
  hex string of the same length after signing.
- **The byte ranges.** The prototype writes a wide `ByteRange`
  placeholder, finds both placeholders in the saved bytes with a regular
  expression, and pads the real values with spaces. The writer should
  report the offsets of the `Contents` string instead.

A sketch of the API, for the owner to accept or change:

```python
doc.sign("out.pdf", signer, field="Signature1", timestamper=None, reserve=8192)
doc.signatures  # fields, byte ranges, coverage; no trust decision
```

`signer` would wrap a key and its certificate chain, and `timestamper` a
TSA client. Verification of trust stays with pyHanko or another validator.

## Crates

Every crate the prototype pulls in is MIT OR Apache-2.0, except
`flagset` (Apache-2.0), `generic-array`, `libm`, and `spin` (MIT),
`subtle` (BSD-3-Clause), and `unicode-ident` (adds Unicode-3.0).

| Crate | Version | Line | Note |
| --- | --- | --- | --- |
| `cms` | 0.2.3 | stable | `builder` feature requires `rsa` |
| `cms` | 0.3.0-rc.0 | next | still a release candidate; `builder` still requires `rsa` |
| `x509-cert` | 0.2.5 / 0.3.0 | stable / next | |
| `der`, `spki` | 0.7 / 0.8 | stable / next | |
| `p256` | 0.13.2 / 0.14.0 | stable / next | 0.13 uses `crypto-bigint` 0.5 |
| `rsa` | 0.9.10 / 0.10.0-rc.19 | | no release fixes the advisory |
| `sha2`, `const-oid` | 0.10, 0.9 / 0.11, 0.10 | | pyropdf's workspace already has 0.11 and 0.10 |

**cargo-audit fails on the stable line** with two advisories:

- RUSTSEC-2023-0071, `rsa` 0.9.10: Marvin attack, a timing side channel in
  private-key operations. No version fixes it. `cms` pulls `rsa` in
  through its `builder` feature even when no RSA key is used.
- RUSTSEC-2026-0387, `crypto-bigint` 0.5.5 (through `p256` 0.13): timing
  variability; fixed in 0.7.6, which the `p256` 0.14 line uses.

The workspace audit job checks only the root `Cargo.lock`, so this
prototype does not fail CI; shipping it as is would.

Alternatives considered, not prototyped:

- `cryptographic-message-syntax` (Apache-2.0, on `bcder`): a different
  DER stack from the one `x509-cert` and hayro's dependencies share.
- `openssl` or `aws-lc-rs`: audited and fast, with RSA that has no Marvin
  advisory, but C code and a native build in every wheel.
- `rasn`: a general ASN.1 framework with CMS types; not checked further.

## Recommendation

Use the RustCrypto **next** line (`cms` 0.3, `x509-cert` 0.3, `der` 0.8,
`p256` 0.14, `sha2` 0.11), with `cms` **without its `builder` feature**:
pyropdf assembles the one SignerInfo itself (the signed attributes, their
DER, and the signature), which removes `rsa` from the tree. The
prototype's builder use is about 30 lines; the hand-written version is
of the same order. Support ECDSA keys first; leave RSA keys until a
constant-time `rsa` release, or take them through a pluggable signer
callback that does the private-key operation outside pyropdf.

## Owner decisions needed

1. **Dependencies (DESIGN.md section 11).** Approve the RustCrypto stack
   above in the engine, and whether to wait for `cms` 0.3.0 or accept a
   release candidate.
2. **Keys.** ECDSA only at first, or RSA too, and through which path:
   the `rsa` crate with its open advisory, a C library, or a callback to
   the caller's key (which also covers HSMs and PKCS#11).
3. **Time-stamps.** Whether pyropdf ships an RFC 3161 client, which means
   network access in the library, or takes a callback that returns a
   token.
