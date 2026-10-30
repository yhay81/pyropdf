"""doc.sign and Signer: PAdES B-B and B-T signatures that pyHanko validates.

The test PKI comes from Python's ``cryptography``: an ECDSA CA, signers on
P-256, P-384, and RSA, and an RSA time-stamping authority, which pyHanko's
local dummy TSA signs with. Nothing leaves the machine.
"""

# ruff: noqa: S106 - the fixtures' passwords are test data.

from __future__ import annotations

import asyncio
import io
import sysconfig
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if sysconfig.get_config_var("Py_GIL_DISABLED") == 1:
    # pyHanko's dependencies have C extensions without free-threading
    # support, whose import enables the GIL for the rest of the run, which
    # tests/test_threads.py checks that pyropdf keeps disabled.
    pytest.skip("pyHanko enables the GIL of a free-threaded build", allow_module_level=True)

from asn1crypto import keys as asn1_keys
from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.diff_analysis import ModificationLevel
from pyhanko.sign.timestamps.dummy_client import DummyTimeStamper
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko.sign.validation.ades import ades_with_time_validation
from pyhanko.sign.validation.errors import SignatureValidationError
from pyhanko.sign.validation.policy_decl import SignatureValidationSpec
from pyhanko.sign.validation.status import SignatureCoverageLevel
from pyhanko_certvalidator import ValidationContext
from pyhanko_certvalidator.context import CertValidationPolicySpec
from pyhanko_certvalidator.policy_decl import NO_REVOCATION, CertRevTrustPolicy
from pyhanko_certvalidator.registry import SimpleTrustManager

import pyropdf
from pyropdf import FieldType, PdfError, SignatureField, Signer

if TYPE_CHECKING:
    from pyropdf._signing import Timestamper

DOCUMENT = Path(__file__).parent / "assets" / "real_world" / "pdf20-simple.pdf"
PEM = serialization.Encoding.PEM
DER = serialization.Encoding.DER
PKCS8 = serialization.PrivateFormat.PKCS8


@dataclass(frozen=True)
class Pki:
    """A CA and the keys and certificates that it issues."""

    root: x509.Certificate
    signers: dict[str, tuple[Any, x509.Certificate]]
    tsa: tuple[rsa.RSAPrivateKey, x509.Certificate]


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _certificate(
    subject: str,
    key: Any,  # noqa: ANN401 - any public key that cryptography issues for.
    issuer: tuple[ec.EllipticCurvePrivateKey, x509.Name],
    *,
    ca: bool = False,
    tsa: bool = False,
) -> x509.Certificate:
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(issuer[1])
        .public_key(key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=not ca,
                content_commitment=not ca and not tsa,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca,
                crl_sign=ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key), critical=False)
    )
    if tsa:
        builder = builder.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), critical=True)
    return builder.sign(issuer[0], hashes.SHA256())


@pytest.fixture(scope="module")
def pki() -> Pki:
    ca_key = ec.generate_private_key(ec.SECP256R1())
    issuer = (ca_key, _name("pyropdf test CA"))
    root = _certificate("pyropdf test CA", ca_key.public_key(), issuer, ca=True)
    signers: dict[str, tuple[Any, x509.Certificate]] = {}
    keys: dict[str, Any] = {
        "p256": ec.generate_private_key(ec.SECP256R1()),
        "p384": ec.generate_private_key(ec.SECP384R1()),
        "rsa": rsa.generate_private_key(public_exponent=65537, key_size=2048),
    }
    for name, key in keys.items():
        signers[name] = (key, _certificate(f"pyropdf test signer {name}", key.public_key(), issuer))
    tsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    tsa = (tsa_key, _certificate("pyropdf test TSA", tsa_key.public_key(), issuer, tsa=True))
    return Pki(root=root, signers=signers, tsa=tsa)


def _pem_signer(pki: Pki, name: str = "p256") -> Signer:
    key, certificate = pki.signers[name]
    pem = key.private_bytes(PEM, PKCS8, serialization.NoEncryption())
    return Signer.from_pem(pem, certificate.public_bytes(PEM) + pki.root.public_bytes(PEM))


def _timestamper(pki: Pki) -> Timestamper:
    key, certificate = pki.tsa
    stamper = DummyTimeStamper(
        tsa_cert=asn1_x509.Certificate.load(certificate.public_bytes(DER)),
        tsa_key=asn1_keys.PrivateKeyInfo.load(key.private_bytes(DER, PKCS8, serialization.NoEncryption())),
    )

    def stamp(digest: bytes, algorithm: str) -> bytes:
        return asyncio.run(stamper.async_timestamp(digest, algorithm)).dump()

    return stamp


def _context(pki: Pki) -> ValidationContext:
    root = asn1_x509.Certificate.load(pki.root.public_bytes(DER))
    return ValidationContext(trust_roots=[root], allow_fetching=False, revocation_mode="soft-fail")


def _statuses(pki: Pki, data: bytes) -> list[Any]:
    reader = PdfFileReader(io.BytesIO(data))
    return [validate_pdf_signature(signature, _context(pki)) for signature in reader.embedded_signatures]


def _assert_whole_file(status: Any) -> None:  # noqa: ANN401 - pyHanko's status classes.
    assert status.intact
    assert status.valid
    assert status.trusted
    assert status.bottom_line
    assert status.coverage is SignatureCoverageLevel.ENTIRE_FILE
    assert status.modification_level is ModificationLevel.NONE


def _document() -> pyropdf.Document:
    return pyropdf.open(DOCUMENT.read_bytes())


def test_a_p256_signature_validates_over_the_entire_file(pki: Pki) -> None:
    doc = _document()
    original = DOCUMENT.read_bytes()
    signed = doc.sign("Signature1", _pem_signer(pki), reason="Approved", location="Tokyo", contact="test@example.com")
    assert signed.startswith(original)
    [status] = _statuses(pki, signed)
    _assert_whole_file(status)
    assert status.signing_cert.subject.human_friendly == "Common Name: pyropdf test signer p256"
    # The document reads the signed revision, which an incremental save reproduces.
    field = doc.form["Signature1"]
    assert field.type is FieldType.SIGNATURE
    assert field.is_signed
    signature = field.obj["V"]
    assert signature["Reason"].text == "Approved"
    assert signature["Location"].text == "Tokyo"
    assert signature["ContactInfo"].text == "test@example.com"
    assert str(signature["SubFilter"]) == "ETSI.CAdES.detached"
    assert doc.to_bytes(incremental=True) == signed
    assert doc.form.signature_flags == pyropdf.SignatureFlags(3)


def test_a_timestamper_makes_the_signature_b_t(pki: Pki) -> None:
    calls: list[tuple[int, str]] = []
    stamp = _timestamper(pki)

    def timestamper(digest: bytes, algorithm: str) -> bytes:
        calls.append((len(digest), algorithm))
        return stamp(digest, algorithm)

    signed = _document().sign("Signature1", _pem_signer(pki), timestamper=timestamper)
    assert calls == [(32, "sha256")]
    [status] = _statuses(pki, signed)
    _assert_whole_file(status)
    assert status.timestamp_validity is not None
    assert status.timestamp_validity.intact
    assert status.timestamp_validity.valid
    assert status.timestamp_validity.trusted
    [embedded] = PdfFileReader(io.BytesIO(signed)).embedded_signatures
    root = asn1_x509.Certificate.load(pki.root.public_bytes(DER))
    policy = CertValidationPolicySpec(
        trust_manager=SimpleTrustManager.build(trust_roots=[root]),
        revinfo_policy=CertRevTrustPolicy(NO_REVOCATION),
    )
    result = asyncio.run(
        ades_with_time_validation(
            embedded.signed_data,
            SignatureValidationSpec(cert_validation_policy=policy),
            raw_digest=embedded.compute_digest(),
        )
    )
    assert result.ades_subindic.status.name == "PASSED"


def test_a_callback_signs_with_an_rsa_key(pki: Pki) -> None:
    key, certificate = pki.signers["rsa"]
    calls: list[bytes] = []

    def sign(data: bytes) -> bytes:
        calls.append(data)
        return key.sign(data, padding.PKCS1v15(), hashes.SHA256())

    signer = Signer.callback([certificate.public_bytes(DER), pki.root.public_bytes(DER)], sign)
    assert signer.digest_algorithm == "sha256"
    assert signer.certificates == (certificate.public_bytes(DER), pki.root.public_bytes(DER))
    signed = _document().sign("Signature1", signer)
    assert len(calls) == 1
    assert calls[0][0] == 0x31  # the DER SET of the signed attributes
    [status] = _statuses(pki, signed)
    _assert_whole_file(status)


def test_earlier_signatures_stay_valid_after_a_second(pki: Pki) -> None:
    doc = _document()
    first = doc.sign("Author", _pem_signer(pki))
    second = doc.sign("Reviewer", _pem_signer(pki, "p384"), timestamper=_timestamper(pki))
    assert second.startswith(first)
    earlier, later = _statuses(pki, second)
    assert earlier.intact
    assert earlier.valid
    assert earlier.trusted
    assert earlier.coverage is SignatureCoverageLevel.ENTIRE_REVISION
    assert earlier.modification_level <= ModificationLevel.FORM_FILLING
    assert earlier.bottom_line
    _assert_whole_file(later)
    assert sorted(doc.form) == ["Author", "Reviewer"]


def test_an_existing_signature_field_is_filled(pki: Pki) -> None:
    doc = _document()
    doc.pages[0].widgets.add(SignatureField("Approval", (72, 72, 272, 122)))
    prepared = doc.to_bytes(incremental=True)
    doc = pyropdf.open(prepared)
    signed = doc.sign("Approval", _pem_signer(pki))
    [status] = _statuses(pki, signed)
    _assert_whole_file(status)
    assert list(doc.form) == ["Approval"]
    assert len(doc.form["Approval"].widgets) == 1


def test_pkcs12_and_p384(pki: Pki) -> None:
    key, certificate = pki.signers["p384"]
    data = pkcs12.serialize_key_and_certificates(
        b"signer", key, certificate, [pki.root], serialization.BestAvailableEncryption(b"secret")
    )
    signer = Signer.from_pkcs12(data, "secret")
    assert signer.digest_algorithm == "sha384"
    assert signer.certificates[0] == certificate.public_bytes(DER)
    [status] = _statuses(pki, _document().sign("Signature1", signer))
    _assert_whole_file(status)
    with pytest.raises(ValueError, match="password"):
        Signer.from_pkcs12(data, "wrong")


def test_encrypted_and_sec1_pem_keys(pki: Pki) -> None:
    key, certificate = pki.signers["p256"]
    chain = certificate.public_bytes(PEM)
    encrypted = key.private_bytes(PEM, PKCS8, serialization.BestAvailableEncryption(b"secret"))
    signer = Signer.from_pem(encrypted, chain, password="secret")
    [status] = _statuses(pki, _document().sign("Signature1", signer))
    assert status.intact
    assert status.valid
    with pytest.raises(ValueError, match="password"):
        Signer.from_pem(encrypted, chain)
    with pytest.raises(ValueError, match="decrypt"):
        Signer.from_pem(encrypted, chain, password="wrong")
    sec1 = key.private_bytes(PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
    assert Signer.from_pem(sec1.decode(), chain.decode()).digest_algorithm == "sha256"


def test_keys_that_pyropdf_does_not_sign_with(pki: Pki) -> None:
    rsa_key, rsa_certificate = pki.signers["rsa"]
    pem = rsa_key.private_bytes(PEM, PKCS8, serialization.NoEncryption())
    with pytest.raises(ValueError, match=r"Signer\.callback"):
        Signer.from_pem(pem, rsa_certificate.public_bytes(PEM))
    key, _ = pki.signers["p256"]
    other = pki.signers["p384"][1]
    with pytest.raises(ValueError, match="public key"):
        Signer.from_pem(key.private_bytes(PEM, PKCS8, serialization.NoEncryption()), other.public_bytes(PEM))
    with pytest.raises(ValueError, match=r"X\.509"):
        Signer.callback([b"not a certificate"], bytes)
    with pytest.raises(TypeError):
        Signer()
    with pytest.raises(TypeError, match="sequence"):
        Signer.callback(rsa_certificate.public_bytes(DER), bytes)  # ty: ignore[invalid-argument-type]


def test_a_failed_signature_leaves_the_document_as_it_was(pki: Pki) -> None:
    doc = _document()
    before = doc.to_bytes(incremental=True)
    with pytest.raises(ValueError, match="reserved"):
        doc.sign("Signature1", _pem_signer(pki), reserve=64)
    assert doc.to_bytes(incremental=True) == before
    assert "Signature1" not in doc.form

    def broken(_data: bytes) -> bytes:
        msg = "the HSM is offline"
        raise RuntimeError(msg)

    _, certificate = pki.signers["rsa"]
    with pytest.raises(RuntimeError, match="offline"):
        doc.sign("Signature1", Signer.callback([certificate.public_bytes(DER)], broken))
    assert doc.to_bytes(incremental=True) == before
    with pytest.raises(TypeError, match="must return bytes"):
        doc.sign("Signature1", Signer.callback([certificate.public_bytes(DER)], str))  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="time-stamp token"):
        doc.sign("Signature1", _pem_signer(pki), timestamper=lambda _digest, _algorithm: b"\x30\x00")
    assert doc.to_bytes(incremental=True) == before


def test_a_failure_leaves_no_field_without_a_history(pki: Pki) -> None:
    doc = _document()
    doc.history.max_size = 0
    before = doc.to_bytes(incremental=True)

    def edit_then_fail(_data: bytes) -> bytes:
        doc.catalog["Lang"] = "en"
        msg = "the HSM is offline"
        raise RuntimeError(msg)

    _, certificate = pki.signers["rsa"]
    with pytest.raises(RuntimeError, match="offline"):
        doc.sign("Signature1", Signer.callback([certificate.public_bytes(DER)], edit_then_fail))
    assert doc.to_bytes(incremental=True) == before
    assert "Signature1" not in doc.form
    assert "Lang" not in doc.catalog


def test_a_callback_that_edits_the_document_is_refused(pki: Pki) -> None:
    doc = _document()
    before = doc.to_bytes(incremental=True)
    key, certificate = pki.signers["p256"]

    def sign_and_add_a_page(data: bytes) -> bytes:
        doc.pages.new()
        return key.sign(data, ec.ECDSA(hashes.SHA256()))

    with pytest.raises(PdfError, match="changed while it was being signed"):
        doc.sign("Signature1", Signer.callback([certificate.public_bytes(DER)], sign_and_add_a_page))
    assert doc.to_bytes(incremental=True) == before
    assert len(doc.pages) == 1


def test_signing_inside_a_transaction_is_refused(pki: Pki) -> None:
    doc = _document()
    before = doc.to_bytes(incremental=True)
    with doc.transaction("outer"):
        doc.catalog["Lang"] = "en"
        with pytest.raises(PdfError, match="inside a transaction"):
            doc.sign("Signature1", _pem_signer(pki))
        assert "Lang" in doc.catalog
    assert "Lang" in doc.catalog
    assert "Signature1" not in doc.form
    assert doc.history.steps[doc.history.position - 1] == "outer"

    def discard() -> None:
        with doc.transaction("discarded"):
            doc.catalog["PageMode"] = pyropdf.Name("UseOutlines")
            with pytest.raises(PdfError, match="inside a transaction"):
                doc.sign("Signature1", _pem_signer(pki))
            msg = "discarded"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="discarded"):
        discard()
    assert "PageMode" not in doc.catalog
    doc.history.undo()
    assert doc.to_bytes(incremental=True) == before


def test_another_threads_edit_waits_for_the_signature(pki: Pki) -> None:
    doc = _document()
    key, certificate = pki.signers["p256"]
    errors: list[BaseException] = []

    def add_a_page() -> None:
        try:
            doc.pages.new()
        except BaseException as error:  # noqa: BLE001 - reported by the assertion below.
            errors.append(error)

    thread = threading.Thread(target=add_a_page)

    def sign_while_another_thread_edits(data: bytes) -> bytes:
        thread.start()
        # The thread's edit waits for the signing transaction's writer lock.
        time.sleep(0.2)
        return key.sign(data, ec.ECDSA(hashes.SHA256()))

    signed = doc.sign("Signature1", Signer.callback([certificate.public_bytes(DER)], sign_while_another_thread_edits))
    thread.join()
    assert errors == []
    assert len(doc.pages) == 2
    after = doc.to_bytes(incremental=True)
    assert after.startswith(signed)
    assert len(after) > len(signed)
    (status,) = _statuses(pki, signed)
    _assert_whole_file(status)


def test_fields_that_cannot_take_a_signature(pki: Pki) -> None:
    doc = _document()
    doc.sign("Signature1", _pem_signer(pki))
    with pytest.raises(ValueError, match="already signed"):
        doc.sign("Signature1", _pem_signer(pki))
    with pytest.raises(ValueError, match="without dots"):
        doc.sign("a.b", _pem_signer(pki))
    doc.pages[0].widgets.add(pyropdf.TextField("Name", (72, 72, 272, 92)))
    with pytest.raises(ValueError, match="not a signature field"):
        doc.sign("Name", _pem_signer(pki))
    with pytest.raises(ValueError, match="pages"):
        pyropdf.Document().sign("Signature1", _pem_signer(pki))
    with pytest.raises(ValueError, match="reserve"):
        doc.sign("Other", _pem_signer(pki), reserve=0)


def test_a_tampered_signature_does_not_validate(pki: Pki) -> None:
    signed = bytearray(_document().sign("Signature1", _pem_signer(pki)))
    # A byte inside the first signed range changes.
    signed[200] ^= 0x01
    reader = PdfFileReader(io.BytesIO(bytes(signed)))
    try:
        statuses = [validate_pdf_signature(signature, _context(pki)) for signature in reader.embedded_signatures]
    except SignatureValidationError:
        return
    assert not all(status.intact for status in statuses)
