"""Generate the public-key fixture with pyHanko. All keys are public test data."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.crypt.api import CryptFilterConfiguration, SecurityHandlerVersion
from pyhanko.pdf_utils.crypt.permissions import PubKeyPermissions
from pyhanko.pdf_utils.crypt.pubkey import PubKeyAdbeSubFilter, PubKeyAESCryptFilter, PubKeySecurityHandler
from pyhanko.pdf_utils.writer import PageObject, PdfFileWriter

ROOT = Path(__file__).resolve().parent


def issue(label: str) -> asn1_x509.Certificate:
    """Create a fixture-only key and certificate, or reuse the old pair."""
    cert_path = ROOT / f"recipient-{label}.cer"
    if cert_path.exists():
        return asn1_x509.Certificate.load(cert_path.read_bytes())
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"pyropdf public test key {label}")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2026, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2036, 1, 1, tzinfo=UTC))
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    der = certificate.public_bytes(serialization.Encoding.DER)
    cert_path.write_bytes(der)
    (ROOT / f"recipient-{label}.key").write_bytes(
        key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return asn1_x509.Certificate.load(der)


def generate(mode: str) -> None:
    """Write a two-envelope AESV3 file with a known content stream and page string."""
    editor, owner = issue("editor"), issue("owner")
    writer = PdfFileWriter(stream_xrefs=False)
    contents = writer.add_object(generic.StreamObject(stream_data=b"0 g 2 3 12 8 re f\n"))
    page = PageObject(contents, [0, 0, 50, 40])
    page[generic.pdf_name("/FixtureValue")] = generic.ByteStringObject(b"recipient\x00value\xff")
    writer.insert_page(page)
    rights = PubKeyPermissions.ALLOW_MODIFICATION_GENERIC | PubKeyPermissions.TOLERATE_MISSING_PDF_MAC
    if mode == "identity-strings":
        cf_name = generic.pdf_name("/DefaultCryptFilter")
        config = CryptFilterConfiguration(
            {cf_name: PubKeyAESCryptFilter(keylen=32, acts_as_default=True, encrypt_metadata=False)},
            default_stream_filter=cf_name,
        )
        handler = PubKeySecurityHandler(
            SecurityHandlerVersion.AES256,
            PubKeyAdbeSubFilter.S5,
            32,
            encrypt_metadata=False,
            crypt_filter_config=config,
            compat_entries=False,
        )
        handler.add_recipients([editor], perms=rights)
    elif mode in ("s3", "s4"):
        s3 = mode == "s3"
        handler = PubKeySecurityHandler(
            SecurityHandlerVersion.RC4_40 if s3 else SecurityHandlerVersion.RC4_LONGER_KEYS,
            PubKeyAdbeSubFilter.S3 if s3 else PubKeyAdbeSubFilter.S4,
            5 if s3 else 16,
        )
        handler.add_recipients([editor], perms=rights)
    else:
        version = SecurityHandlerVersion.AES256 if mode == "recipients" else SecurityHandlerVersion.RC4_OR_AES128
        handler = PubKeySecurityHandler.build_from_certs(
            [editor], version=version, use_aes=mode != "v2", perms=rights, pdf_mac=False, compat_entries=False
        )
    handler.add_recipients([owner])
    writer._assign_security_handler(handler)  # noqa: SLF001 - attach both recipient groups before serialisation.
    writer.output_version = (2, 0)
    with (ROOT / f"pyhanko-{mode}.pdf").open("wb") as output:
        writer.write(output)


if __name__ == "__main__":
    for variant in ("recipients", "identity-strings", "recipients-aes128", "s3", "s4", "v2"):
        generate(variant)
