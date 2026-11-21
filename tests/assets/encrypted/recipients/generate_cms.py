"""Generate CMS interoperability controls with pyHanko and OpenSSL's CMS command."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from asn1crypto import cms
from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID
from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.crypt.pubkey import PubKeyCryptFilter, PubKeySecurityHandler, RecipientEncryptionPolicy
from pyhanko.pdf_utils.writer import PageObject, PdfFileWriter

ROOT = Path(__file__).parent
CONTENT = b"0 g 4 5 14 19 re f\n"
OPENSSL = {
    "aes192-oaep": ["-aes192"],
    "aes128-oaep-default": ["-aes128"],
    "3des": ["-des3"],
    "des": ["-des"],
    "rc4": ["-rc4"],
    "rc2-40": ["-rc2-40"],
    "rc2-64": ["-rc2-64"],
    "rc2-128": ["-rc2-128"],
    "rc4-104": ["-rc4"],
}


CURVES = {"ecdh": ("ec", ec.SECP256R1), "ecdh384": ("ec384", ec.SECP384R1), "ecdh521": ("ec521", ec.SECP521R1)}


def ec_credential(label: str, curve: ec.EllipticCurve) -> None:
    """Create the public test key only on the first run."""
    path = ROOT / f"recipient-{label}.key"
    if path.exists():
        return
    key = ec.generate_private_key(curve)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"pyropdf public {label} test key")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .serial_number(936)
        .public_key(key.public_key())
        .not_valid_before(datetime(2025, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2040, 1, 1, tzinfo=UTC))
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=True,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(
        key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    (ROOT / f"recipient-{label}.cer").write_bytes(cert.public_bytes(serialization.Encoding.DER))


def rc4_104(envelope: cms.ContentInfo, payload: bytes, cert: x509.Certificate) -> cms.ContentInfo:
    """Use Node's OpenSSL EVP interface for a variable key length the CMS CLI cannot select."""
    node = shutil.which("node")
    assert node is not None
    key = os.urandom(13)
    script = """
const crypto = require('node:crypto');
const input = require('node:fs').readFileSync(0);
const key = input.subarray(0, 13);
const plain = input.subarray(13);
const enc = crypto.createCipheriv('rc4', key, null);
const ciphertext = Buffer.concat([enc.update(plain), enc.final()]);
const dec = crypto.createDecipheriv('rc4', key, null);
if (!Buffer.concat([dec.update(ciphertext), dec.final()]).equals(plain)) process.exit(1);
process.stdout.write(ciphertext);
"""
    result = subprocess.run(  # noqa: S603 - fixed local OpenSSL control.
        [node, "--openssl-legacy-provider", "-e", script], input=key + payload, capture_output=True, check=True
    )
    message = envelope["content"]
    public = cert.public_key()
    assert isinstance(public, rsa.RSAPublicKey)
    message["recipient_infos"][0].chosen["encrypted_key"] = public.encrypt(key, padding.PKCS1v15())
    message["encrypted_content_info"]["encrypted_content"] = result.stdout
    return cms.ContentInfo.load(envelope.dump(force=True))


def generate(mode: str) -> None:
    """Write a real PDF, deriving its file key only after the final envelopes exist."""
    label = CURVES[mode][0] if mode in CURVES else "owner"
    raw = (ROOT / f"recipient-{label}.cer").read_bytes()
    cert = asn1_x509.Certificate.load(raw)
    writer = PdfFileWriter(stream_xrefs=False)
    page = PageObject(writer.add_object(generic.StreamObject(stream_data=CONTENT)), [0, 0, 40, 50])
    page[generic.pdf_name("/Marker")] = generic.ByteStringObject(b"CMS fixture string")
    writer.insert_page(page)
    if mode == "oaep" or mode in CURVES:
        policy = RecipientEncryptionPolicy(prefer_oaep=True, ignore_key_usage=mode in CURVES)
        writer.encrypt_pubkey([cert], policy=policy, pdf_mac=False)
    else:
        handler = PubKeySecurityHandler.build_from_certs([cert], pdf_mac=False)
        selected = handler.get_stream_filter()
        assert isinstance(selected, PubKeyCryptFilter)
        assert selected.recipients is not None
        public = x509.load_der_x509_certificate(raw)
        private = serialization.load_der_private_key((ROOT / "recipient-owner.key").read_bytes(), None)
        assert isinstance(private, rsa.RSAPrivateKey)
        seed_and_flags = pkcs7.pkcs7_decrypt_der(selected.recipients[0].dump(), public, private, [])
        with tempfile.TemporaryDirectory() as tmp:
            pem = Path(tmp, "recipient.pem")
            pem.write_bytes(public.public_bytes(serialization.Encoding.PEM))
            args = [
                "openssl",
                "cms",
                "-encrypt",
                "-binary",
                "-outform",
                "DER",
                "-provider",
                "default",
                "-provider",
                "legacy",
                *OPENSSL[mode],
                "-recip",
                str(pem),
            ]
            if "oaep" in mode:
                args.extend(["-keyopt", "rsa_padding_mode:oaep"])
                if mode == "aes192-oaep":
                    args.extend(
                        [
                            "-keyopt",
                            "rsa_oaep_md:sha384",
                            "-keyopt",
                            "rsa_mgf1_md:sha256",
                            "-keyopt",
                            "rsa_oaep_label:504446",
                        ]
                    )
            result = subprocess.run(args, input=seed_and_flags, capture_output=True, check=True)  # noqa: S603 - fixed fixture command.
            envelope = cms.ContentInfo.load(result.stdout)
            if mode == "rc4-104":
                envelope = rc4_104(envelope, seed_and_flags, public)
            elif mode in {"rc2-40", "rc2-64"}:
                private_path = Path(tmp, "fixture.key")
                private_path.write_bytes(
                    private.private_bytes(
                        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
                    )
                )
                decrypt = [
                    "openssl",
                    "cms",
                    "-decrypt",
                    "-binary",
                    "-inform",
                    "DER",
                    "-provider",
                    "default",
                    "-provider",
                    "legacy",
                    "-recip",
                    str(pem),
                    "-inkey",
                    str(private_path),
                ]
                checked = subprocess.run(decrypt, input=result.stdout, capture_output=True, check=True)  # noqa: S603
                assert checked.stdout == seed_and_flags
            selected.recipients[:] = [envelope]
        writer._assign_security_handler(handler)  # noqa: SLF001 - pyHanko now hashes the exact OpenSSL envelope.
    with (ROOT / f"cms-{mode}.pdf").open("wb") as out:
        writer.write(out)


if __name__ == "__main__":
    for label, curve in CURVES.values():
        ec_credential(label, curve())
    for variant in ("oaep", *CURVES, *OPENSSL):
        generate(variant)
