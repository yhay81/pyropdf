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

import pikepdf
import pytest

if sysconfig.get_config_var("Py_GIL_DISABLED") == 1:
    # pyHanko's dependencies have C extensions without free-threading
    # support, whose import enables the GIL for the rest of the run, which
    # tests/test_threads.py checks that pyropdf keeps disabled.
    pytest.skip("pyHanko enables the GIL of a free-threaded build", allow_module_level=True)

from asn1crypto import cms
from asn1crypto import keys as asn1_keys
from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, rsa
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
    return make_pki()


def make_pki() -> Pki:
    """Return a new test CA, its signers on P-256, P-384, and RSA, and its time-stamping authority."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    issuer = (ca_key, _name("pyropdf test CA"))
    root = _certificate("pyropdf test CA", ca_key.public_key(), issuer, ca=True)
    signers: dict[str, tuple[Any, x509.Certificate]] = {}
    keys: dict[str, Any] = {
        "p256": ec.generate_private_key(ec.SECP256R1()),
        "p384": ec.generate_private_key(ec.SECP384R1()),
        "p521": ec.generate_private_key(ec.SECP521R1()),
        "bp256": ec.generate_private_key(ec.BrainpoolP256R1()),
        "bp384": ec.generate_private_key(ec.BrainpoolP384R1()),
        "bp512": ec.generate_private_key(ec.BrainpoolP512R1()),
        "ed25519": ed25519.Ed25519PrivateKey.generate(),
        "ed448": ed448.Ed448PrivateKey.generate(),
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
        override_md="sha256",
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


def _blank_pdf(version: str) -> bytes:
    data = io.BytesIO()
    with pikepdf.new() as pdf:
        pdf.add_blank_page()
        pdf.save(data, force_version=version)
    return data.getvalue()


@pytest.mark.parametrize("version", ["1.4", "1.7", "2.0"])
@pytest.mark.parametrize("certified", [False, True], ids=["approval", "certification"])
def test_signing_declares_its_subfilter_in_the_effective_pdf_edition(
    pki: Pki, version: str, *, certified: bool
) -> None:
    data = _blank_pdf(version)
    with pyropdf.open(data) as doc:
        if certified:
            doc.pages[0].widgets.add(SignatureField("Approval", (10, 10, 100, 50)))
        signed = doc.sign(
            "Approval",
            _pem_signer(pki),
            doc_mdp=pyropdf.DocMdpPermission.FORM_FILLING_AND_SIGNATURES if certified else None,
        )
    assert signed.startswith(data)
    _assert_whole_file(_statuses(pki, signed)[0])
    with pikepdf.open(io.BytesIO(signed)) as pdf:
        assert pdf.Root.AcroForm.Fields[0].V.SubFilter == pikepdf.Name("/ETSI.CAdES.detached")
        if version == "2.0":
            assert "/Extensions" not in pdf.Root
        else:
            assert pdf.pdf_version == version
            if version == "1.4":
                assert pdf.Root.Version == pikepdf.Name("/1.7")
            extension = pdf.Root.Extensions.ESIC
            assert extension.BaseVersion == pikepdf.Name("/1.7")
            assert extension.ExtensionLevel == 2
            assert "/ETSI_PAdES" not in pdf.Root.Extensions


@pytest.mark.parametrize(
    ("previous", "expected"),
    [
        ({"ESIC": 4}, {"ESIC": 4}),
        ({"ADBE": 8}, {"ADBE": 8}),
        ({"ESIC": 1}, {"ESIC": 1, "ADBE": 8}),
        ({"ESIC": 1, "ADBE": 3}, {"ESIC": 1, "ADBE": 8}),
        ({"ETSI_PAdES": 2}, {"ETSI_PAdES": 2, "ESIC": 2}),
    ],
)
def test_signing_preserves_existing_developer_extensions(
    pki: Pki, previous: dict[str, int], expected: dict[str, int]
) -> None:
    with pyropdf.open(_blank_pdf("1.7")) as doc:
        doc.catalog["Extensions"] = {
            key: {"BaseVersion": pyropdf.Name("1.7"), "ExtensionLevel": level, "XXNote": b"keep"}
            for key, level in {**previous, "XXVendor": 7}.items()
        }
        signed = doc.sign("Approval", _pem_signer(pki))
    with pikepdf.open(io.BytesIO(signed)) as pdf:
        extensions = pdf.Root.Extensions
        assert set(extensions.keys()) == {"/" + key for key in {*expected, "XXVendor"}}
        for key, level in {**expected, "XXVendor": 7}.items():
            assert extensions["/" + key].ExtensionLevel == level
            if key in previous or key == "XXVendor":
                assert bytes(extensions["/" + key].XXNote) == b"keep"


@pytest.mark.parametrize("existing", [None, {"ESIC": None}])
def test_a_null_extension_entry_is_equivalent_to_an_absent_entry(pki: Pki, existing: dict[str, None] | None) -> None:
    with pyropdf.open(_blank_pdf("1.7")) as doc:
        doc.catalog["Extensions"] = existing
        signed = doc.sign("Approval", _pem_signer(pki))
    with pikepdf.open(io.BytesIO(signed)) as pdf:
        assert pdf.Root.Extensions.ESIC.BaseVersion == pikepdf.Name("/1.7")
        assert pdf.Root.Extensions.ESIC.ExtensionLevel == 2


@pytest.mark.parametrize("malformed", ["catalog", "indirect", "indirect-entry", "level", "base"])
def test_a_bad_extension_declaration_refuses_signing_without_mutating_the_input(pki: Pki, malformed: str) -> None:
    with pyropdf.open(_blank_pdf("1.4")) as doc:
        entry = {"BaseVersion": pyropdf.Name("1.7" if malformed != "base" else "2.0"), "ExtensionLevel": 2}
        if malformed == "level":
            entry["ExtensionLevel"] = True
        if malformed == "catalog":
            doc.catalog["Extensions"] = 0
        elif malformed == "indirect":
            doc.catalog["Extensions"] = doc.objects[doc.objects.add({"ESIC": entry})]
        elif malformed == "indirect-entry":
            doc.catalog["Extensions"] = {"ESIC": doc.objects[doc.objects.add(entry)]}
        else:
            doc.catalog["Extensions"] = {"ESIC": entry}
        before = doc.to_bytes(incremental=True)
        with pytest.raises(PdfError, match=r"Extensions|ESIC"):
            doc.sign("Approval", _pem_signer(pki))
        assert doc.to_bytes(incremental=True) == before
        assert "Version" not in doc.catalog
        assert "Approval" not in doc.form


@pytest.mark.spec("digital-signatures", "create")
@pytest.mark.spec("ecc-signatures", "create")
@pytest.mark.spec("signature-fields", "create")
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


@pytest.mark.spec("digital-signatures", "preserve")
@pytest.mark.spec("ecc-signatures", "preserve")
@pytest.mark.spec("signature-fields", "preserve")
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


@pytest.mark.spec("digital-signatures", "edit")
@pytest.mark.spec("ecc-signatures", "edit")
@pytest.mark.spec("signature-fields", "edit")
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


# ISO/TS 32002:2022 Tables 3 and 4. Each pair is exercised in both directions.
PDF_SIGNATURE_CASES = [
    (curve, digest)
    for curve, digests in {
        "p256": ("sha256", "sha3_256"),
        "p384": ("sha384", "sha3_384"),
        "p521": ("sha512", "sha3_512"),
        "bp256": ("sha256", "sha384", "sha512", "sha3_256", "sha3_384", "sha3_512"),
        "bp384": ("sha384", "sha512", "sha3_384", "sha3_512"),
        "bp512": ("sha512", "sha3_512"),
        "ed25519": ("sha512",),
        "ed448": ("shake256",),
    }.items()
    for digest in digests
]


def _extended_signer(pki: Pki, curve: str, digest: str) -> Signer:
    key, certificate = pki.signers[curve]
    if curve in ("p256", "p384"):
        pem = key.private_bytes(PEM, PKCS8, serialization.NoEncryption())
        return Signer.from_pem(pem, certificate.public_bytes(PEM) + pki.root.public_bytes(PEM), digest_algorithm=digest)
    if curve in ("ed448", "ed25519"):
        callback = key.sign
    else:
        algorithm = getattr(hashes, digest.upper())()

        def callback(data: bytes) -> bytes:
            return key.sign(data, ec.ECDSA(algorithm))

    return Signer.callback(
        [certificate.public_bytes(DER), pki.root.public_bytes(DER)], callback, digest_algorithm=digest
    )


@pytest.mark.spec("sha3-hashes", "create", "edit", "preserve")
@pytest.mark.spec("ecc-signatures", "create", "edit", "preserve")
@pytest.mark.parametrize(("curve", "digest"), PDF_SIGNATURE_CASES)
def test_pdf_signature_profile_creation_and_preservation(pki: Pki, curve: str, digest: str) -> None:
    doc = _document()
    doc.prepare_signature("Approval")
    previous = doc.to_bytes(incremental=True)
    doc = pyropdf.open(previous)
    signer = _extended_signer(pki, curve, digest)
    assert signer.digest_algorithm == digest
    signed = doc.sign("Approval", signer)
    assert signed.startswith(previous)
    reader = PdfFileReader(io.BytesIO(signed))
    ext = reader.root.get("/Extensions", {}).get("/ISO_", [])
    levels = {int(item["/ExtensionLevel"]) for item in ext}
    if digest.startswith("sha3_") or digest == "shake256":
        assert 32001 in levels
    if curve not in ("p256", "p384") or digest.startswith("sha3_"):
        assert 32002 in levels
    for item in ext:
        assert item["/BaseVersion"] == "/2.0"
        assert item["/ExtensionRevision"] == ":2022"
    [status] = _statuses(pki, signed)
    _assert_whole_file(status)
    later = doc.sign("Second", signer)
    assert later.startswith(signed)
    assert [item.intact and item.valid for item in _statuses(pki, later)] == [True, True]
    assert len(PdfFileReader(io.BytesIO(later)).root.get("/Extensions", {}).get("/ISO_", [])) == len(levels)


@pytest.mark.parametrize(
    ("curve", "digest"),
    [("p256", "sha3_512"), ("bp384", "sha256"), ("ed25519", "shake256"), ("ed448", "sha512"), ("p384", "sha1")],
)
def test_pdf_signature_profile_refuses_incompatible_digest(pki: Pki, curve: str, digest: str) -> None:
    _, cert = pki.signers[curve]
    with pytest.raises(ValueError, match="not a permitted PDF signature digest"):
        Signer.callback([cert.public_bytes(DER)], lambda _: b"", digest_algorithm=digest)


def test_mandatory_digest_seed_refuses_before_signing(pki: Pki) -> None:
    doc = _document()
    doc.prepare_signature("Approval")
    doc.form["Approval"].obj["SV"] = {"Ff": 64, "DigestMethod": [pyropdf.Name("SHA3-256")]}
    before = doc.to_bytes(incremental=True)
    with pytest.raises(ValueError, match="DigestMethod"):
        doc.sign("Approval", _pem_signer(pki))
    assert doc.to_bytes(incremental=True) == before


@pytest.mark.parametrize("digest", ["sha3_256", "sha3_384", "sha3_512", "shake256"])
def test_timestamp_with_iso_extension_hash(pki: Pki, digest: str) -> None:
    from pyhanko.sign.validation import validate_pdf_timestamp  # noqa: PLC0415

    doc = _document()
    timestamped = doc.timestamp(_timestamper(pki), digest_algorithm=digest)
    [embedded] = PdfFileReader(io.BytesIO(timestamped)).embedded_signatures
    status = validate_pdf_timestamp(embedded, _context(pki))
    assert status.intact
    assert status.valid
    [signature] = doc.signatures
    report = signature.verify([pki.root.public_bytes(DER)])
    assert report.timestamp_valid, report.problems


@pytest.mark.parametrize(("curve", "digest"), [("p256", "sha3_256"), ("p384", "sha3_384"), ("bp512", "sha3_512")])
def test_standard_digest_name_in_required_seed(pki: Pki, curve: str, digest: str) -> None:
    doc = _document()
    doc.prepare_signature("Approval")
    seed_name = pyropdf.Name(digest.upper().replace("_", "-"))
    doc.form["Approval"].obj["SV"] = {"Ff": 64, "DigestMethod": [seed_name]}
    signed = doc.sign("Approval", _extended_signer(pki, curve, digest))
    assert doc.form["Approval"].obj["SV"]["DigestMethod"][0] == seed_name
    [report] = _statuses(pki, signed)
    assert report.intact
    assert report.valid


@pytest.mark.parametrize(("digest", "signature_oid"), [("sha3_256", 14), ("sha3_384", 15), ("sha3_512", 16)])
def test_rsa_sha3_callbacks_use_rfc9688_identifiers(pki: Pki, digest: str, signature_oid: int) -> None:
    key, cert = pki.signers["rsa"]
    algorithm = getattr(hashes, digest.upper())()
    signer = Signer.callback(
        [cert.public_bytes(DER), pki.root.public_bytes(DER)],
        lambda data: key.sign(data, padding.PKCS1v15(), algorithm),
        digest_algorithm=digest,
    )
    doc = _document()
    signed = doc.sign("Approval", signer)
    embedded = PdfFileReader(io.BytesIO(signed)).embedded_signatures[0]
    info = embedded.signer_info
    assert info["signature_algorithm"]["algorithm"].dotted == f"2.16.840.1.101.3.4.3.{signature_oid}"
    assert info["signature_algorithm"]["parameters"].dump() == b"\x05\x00"
    assert info["digest_algorithm"]["parameters"].dump() == b""
    # asn1crypto 1.5.1 does not name RFC 9688 RSA signature OIDs. Check the
    # CMS wire values and both hashes with the independent OpenSSL backend.
    raw = cms.CMSAttributes(info["signed_attrs"]).dump()
    public = cert.public_key()
    assert isinstance(public, rsa.RSAPublicKey)
    public.verify(info["signature"].native, raw, padding.PKCS1v15(), algorithm)
    digestor = hashes.Hash(algorithm)
    _, first, second, length = embedded.byte_range
    digestor.update(signed[:first])
    digestor.update(signed[second : second + length])
    assert (
        next(a for a in info["signed_attrs"] if a["type"].native == "message_digest")["values"][0].native
        == digestor.finalize()
    )
    assert doc.signatures[0].verify([pki.root.public_bytes(DER)])


@pytest.mark.parametrize("container", ["pem", "pkcs12"])
@pytest.mark.parametrize(("curve", "digest"), PDF_SIGNATURE_CASES)
def test_local_key_containers_sign_each_profile(pki: Pki, curve: str, digest: str, container: str) -> None:
    key, cert = pki.signers[curve]
    password = b"test private key password"
    protection = serialization.BestAvailableEncryption(password)
    if container == "pem":
        signer = Signer.from_pem(
            key.private_bytes(PEM, PKCS8, protection),
            cert.public_bytes(PEM) + pki.root.public_bytes(PEM),
            password=password,
            digest_algorithm=digest,
        )
    else:
        encoded = pkcs12.serialize_key_and_certificates(b"Signer", key, cert, [pki.root], protection)
        signer = Signer.from_pkcs12(encoded, password.decode(), digest_algorithm=digest)
    assert signer.certificates[0] == cert.public_bytes(DER)
    assert signer.digest_algorithm == digest
    signed = _document().sign("Approval", signer)
    [status] = _statuses(pki, signed)
    _assert_whole_file(status)


def test_extended_pem_key_must_match_first_certificate(pki: Pki) -> None:
    key, _ = pki.signers["ed448"]
    _, other = pki.signers["p521"]
    with pytest.raises(ValueError, match="public key"):
        Signer.from_pem(key.private_bytes(PEM, PKCS8, serialization.NoEncryption()), other.public_bytes(PEM))


def test_iso_declarations_preserve_indirect_extensions_and_legacy_entry(pki: Pki) -> None:
    doc = _document()
    existing = doc.objects.add(
        {"BaseVersion": pyropdf.Name("2.0"), "ExtensionLevel": 32003, "ExtensionRevision": ":2023"}
    )
    extensions = doc.objects.add(
        {"ISO_": doc.objects[existing], "ADBE": {"BaseVersion": pyropdf.Name("1.7"), "ExtensionLevel": 3}}
    )
    doc.trailer["Root"]["Extensions"] = doc.objects[extensions]
    doc.sign("Approval", _extended_signer(pki, "ed448", "shake256"))
    updated = doc.trailer["Root"]["Extensions"]
    assert updated.xref == extensions
    assert updated["ISO_"][0].xref == existing
    assert [item["ExtensionLevel"] for item in updated["ISO_"]] == [32003, 32001, 32002]
    assert updated["ADBE"]["ExtensionLevel"] == 3


@pytest.mark.parametrize("existing", [False, True], ids=["no-extensions", "existing-extensions"])
def test_algorithm_promotion_preserves_pre_pdf20_pades_declarations(pki: Pki, *, existing: bool) -> None:
    with pyropdf.open(_blank_pdf("1.7")) as doc:
        if existing:
            doc.catalog["Extensions"] = {
                key: {"BaseVersion": pyropdf.Name("1.7"), "ExtensionLevel": level, "XXNote": b"keep"}
                for key, level in (("ESIC", 1), ("ADBE", 3))
            }
        original = doc.to_bytes(incremental=True)
    with pyropdf.open(original) as doc:
        signed = doc.sign("Approval", _extended_signer(pki, "ed448", "shake256"))
    assert signed.startswith(original)
    _assert_whole_file(_statuses(pki, signed)[0])
    with pikepdf.open(io.BytesIO(signed)) as pdf:
        assert pdf.Root.Version == pikepdf.Name("/2.0")
        extensions = pdf.Root.Extensions
        assert {entry.ExtensionLevel for entry in extensions.ISO_} == {32001, 32002}
        if existing:
            assert extensions.ESIC.ExtensionLevel == 1
            assert extensions.ADBE.ExtensionLevel == 3
            assert bytes(extensions.ESIC.XXNote) == bytes(extensions.ADBE.XXNote) == b"keep"
        else:
            assert "/ESIC" not in extensions
            assert "/ADBE" not in extensions


def test_sha3_does_not_add_digest_method_to_signature_references(pki: Pki) -> None:
    doc = _document()
    doc.sign(
        "Approval",
        _extended_signer(pki, "p256", "sha3_256"),
        doc_mdp=pyropdf.DocMdpPermission.FORM_FILLING_AND_SIGNATURES,
    )
    [reference] = doc.form["Approval"].obj["V"]["Reference"]
    assert reference["TransformMethod"] == "DocMDP"
    assert "DigestMethod" not in reference
    assert "DigestMethod" not in reference["TransformParams"]


@pytest.mark.parametrize(("curve", "digest"), [("ed25519", "sha512"), ("ed448", "shake256")])
def test_seed_allows_the_digest_fixed_by_the_signature_mechanism(pki: Pki, curve: str, digest: str) -> None:
    doc = _document()
    doc.prepare_signature("Approval")
    doc.form["Approval"].obj["SV"] = {"Ff": 64, "DigestMethod": [pyropdf.Name("SHA3-384")]}
    doc.sign("Approval", _extended_signer(pki, curve, digest))
    assert doc.signatures[0].verify([pki.root.public_bytes(DER)])
