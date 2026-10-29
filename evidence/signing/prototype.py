# ruff: noqa: INP001, T201, S603, S607 - a script that runs openssl and the prototype and prints its findings.
"""Sign one corpus document as PAdES B-B and B-T, and validate both with pyHanko.

Run from the repository root:
``uv run --with pyhanko==0.38.0 python evidence/signing/prototype.py``.
It builds ``prototype/`` with cargo, makes a test CA, a signer, and a
time-stamping authority with openssl, adds a signature field with pyropdf,
saves incrementally with a placeholder, signs the byte ranges with the
RustCrypto ``cms`` crate, and prints what pyHanko reports for each profile.
Nothing leaves the machine: the time-stamp comes from pyHanko's local
dummy TSA.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
import subprocess
import tempfile
from pathlib import Path

from pyhanko.keys import load_cert_from_pemder, load_private_key_from_pemder
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.timestamps.dummy_client import DummyTimeStamper
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko.sign.validation.ades import ades_basic_validation, ades_with_time_validation
from pyhanko.sign.validation.policy_decl import SignatureValidationSpec
from pyhanko_certvalidator import ValidationContext
from pyhanko_certvalidator.context import CertValidationPolicySpec
from pyhanko_certvalidator.policy_decl import NO_REVOCATION, CertRevTrustPolicy
from pyhanko_certvalidator.registry import SimpleTrustManager

import pyropdf
from pyropdf import Name

HERE = Path(__file__).resolve().parent
DOCUMENT = HERE.parents[1] / "tests" / "assets" / "real_world" / "pdf20-simple.pdf"
#: Bytes reserved for the CMS signature; its hex form fills twice as many.
RESERVED = 8192
#: A ByteRange placeholder wide enough for any offset of the file.
PLACEHOLDER = 9_999_999_999

OPENSSL_CONFIG = """\
[ca]
basicConstraints = critical, CA:true
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
[signer]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature, nonRepudiation
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
[tsa]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, timeStamping
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
"""


def _openssl(*args: str, cwd: Path) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def _certificates(work: Path) -> None:
    """Write a test CA, and a signer and a TSA certificate that it issues."""
    (work / "ext.cnf").write_text(OPENSSL_CONFIG)
    for name in ("ca", "signer"):
        _openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", f"{name}.key", cwd=work)
    # pyHanko's dummy TSA signs with RSA only.
    _openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", "tsa.key", cwd=work)
    _openssl(
        "req", "-x509", "-new", "-key", "ca.key", "-subj", "/CN=pyropdf test CA", "-days", "30",
        "-extensions", "ca", "-config", "ext.cnf", "-out", "ca.pem", cwd=work,
    )  # fmt: skip
    for name, subject in (("signer", "/CN=pyropdf test signer"), ("tsa", "/CN=pyropdf test TSA")):
        _openssl("req", "-new", "-key", f"{name}.key", "-subj", subject, "-out", f"{name}.csr", cwd=work)
        _openssl(
            "x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
            "-days", "30", "-extfile", "ext.cnf", "-extensions", name, "-out", f"{name}.pem", cwd=work,
        )  # fmt: skip
        _openssl("x509", "-in", f"{name}.pem", "-outform", "DER", "-out", f"{name}.der", cwd=work)


def _prepared() -> bytes:
    """Return the document with an empty signature field, saved incrementally with placeholders."""
    doc = pyropdf.open(DOCUMENT.read_bytes())
    page = doc.pages[0].obj
    signature = doc.objects.add(
        {
            "Type": Name("Sig"),
            "Filter": Name("Adobe.PPKLite"),
            "SubFilter": Name("ETSI.CAdES.detached"),
            "ByteRange": [0, PLACEHOLDER, PLACEHOLDER, PLACEHOLDER],
            "Contents": b"0" * (2 * RESERVED),
            "M": "D:20261029000000Z",
        }
    )
    widget = doc.objects.add(
        {
            "Type": Name("Annot"),
            "Subtype": Name("Widget"),
            "FT": Name("Sig"),
            "T": "Signature1",
            "Rect": [0, 0, 0, 0],
            "F": 132,
            "P": page,
            "V": doc.objects[signature],
        }
    )
    page["Annots"] = [*page.get("Annots", []), doc.objects[widget]]
    doc.catalog["AcroForm"] = {"Fields": [doc.objects[widget]], "SigFlags": 3}
    return doc.to_bytes(incremental=True)


def _byte_ranges(data: bytes) -> tuple[bytearray, slice]:
    """Fill the ByteRange placeholder; return the file and the slice of the Contents string, delimiters included."""
    contents = re.search(rb"/Contents\s*(\(0{%d}\))" % (2 * RESERVED), data)
    byte_range = re.search(rb"/ByteRange\s*(\[\s*0\s+%d\s+%d\s+%d\s*\])" % ((PLACEHOLDER,) * 3), data)
    if contents is None or byte_range is None:
        msg = "the incremental save does not hold the placeholders as written"
        raise SystemExit(msg)
    start, end = contents.span(1)
    width = byte_range.end(1) - byte_range.start(1)
    out = bytearray(data)
    # The same width, padded with spaces, so that no offset moves.
    ranges = f"[0 {start} {end} {len(data) - end}".encode()
    out[byte_range.start(1) : byte_range.end(1)] = ranges.ljust(width - 1) + b"]"
    return out, slice(start, end)


def _sign(work: Path, prototype: Path, data: bytearray, contents: slice, *, timestamp: bool) -> bytes:
    (work / "ranges.bin").write_bytes(bytes(data[: contents.start] + data[contents.stop :]))
    subprocess.run([prototype, "sign", "signer.key", "signer.der", "ranges.bin", "cms.der"], cwd=work, check=True)
    signed = "cms.der"
    if timestamp:
        value = subprocess.run([prototype, "signature", "cms.der"], cwd=work, check=True, capture_output=True)
        digest = hashlib.sha256(bytes.fromhex(value.stdout.decode().strip())).digest()
        stamper = DummyTimeStamper(
            tsa_cert=load_cert_from_pemder(str(work / "tsa.pem")),
            tsa_key=load_private_key_from_pemder(str(work / "tsa.key"), passphrase=None),
        )
        token = asyncio.run(stamper.async_timestamp(digest, "sha256"))
        (work / "token.der").write_bytes(token.dump())
        subprocess.run([prototype, "timestamp", "cms.der", "token.der", "stamped.der"], cwd=work, check=True)
        signed = "stamped.der"
    der = (work / signed).read_bytes()
    if len(der) > RESERVED:
        msg = f"the signature needs {len(der)} bytes, over the {RESERVED} reserved"
        raise SystemExit(msg)
    out = bytearray(data)
    out[contents] = b"<" + der.hex().encode().ljust(2 * RESERVED, b"0") + b">"
    return bytes(out), len(der)


def _report(name: str, data: bytes, work: Path, size: int) -> None:
    root = load_cert_from_pemder(str(work / "ca.pem"))
    context = ValidationContext(trust_roots=[root], allow_fetching=False, revocation_mode="soft-fail")
    [signature] = PdfFileReader(io.BytesIO(data)).embedded_signatures
    status = validate_pdf_signature(signature, context)
    print(f"## {name}")
    print()
    print(f"- CMS size: {size} bytes of {RESERVED} reserved")
    print(f"- intact: {status.intact}, valid: {status.valid}, trusted: {status.trusted}")
    print(f"- bottom line: {status.bottom_line}")
    print(f"- coverage: {status.coverage.name}, modification level: {status.modification_level.name}")
    print(f"- docmdp ok: {status.docmdp_ok}, seed values ok: {status.seed_value_ok}")
    print(f"- signing certificate: {status.signing_cert.subject.human_friendly}")
    # ETSI EN 319 102-1: basic validation (5.3), and with time (5.5) for B-T.
    policy = CertValidationPolicySpec(
        trust_manager=SimpleTrustManager.build(trust_roots=[root]),
        revinfo_policy=CertRevTrustPolicy(NO_REVOCATION),
    )
    spec = SignatureValidationSpec(cert_validation_policy=policy)
    timestamp = status.timestamp_validity
    ades = ades_basic_validation if timestamp is None else ades_with_time_validation
    result = asyncio.run(ades(signature.signed_data, spec, raw_digest=signature.compute_digest()))
    print(f"- AdES ({ades.__name__}): {result.ades_subindic.status.name} / {result.ades_subindic.name}")
    if timestamp is not None:
        print(f"- signature time-stamp: valid {timestamp.valid}, trusted {timestamp.trusted}, at {timestamp.timestamp}")
    print()


def main() -> None:
    """Build the prototype, sign as B-B and B-T, and print pyHanko's reports."""
    manifest = HERE / "prototype" / "Cargo.toml"
    subprocess.run(["cargo", "build", "--release", "-q", "--manifest-path", manifest], check=True)
    prototype = HERE / "prototype" / "target" / "release" / "signing-prototype"
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        _certificates(work)
        prepared, contents = _byte_ranges(_prepared())
        print(f"# Signing {DOCUMENT.name} ({DOCUMENT.stat().st_size} bytes), pyropdf {pyropdf.__version__}")
        print()
        for name, timestamp in (("PAdES B-B", False), ("PAdES B-T", True)):
            data, size = _sign(work, prototype, prepared, contents, timestamp=timestamp)
            _report(name, data, work, size)


if __name__ == "__main__":
    main()
