"""doc.signatures and Signature.verify: checking signatures as pyHanko does.

Signatures by pyropdf and by pyHanko verify as intact, valid, and trusted
with the test CA of tests/test_signing.py, and the later revisions of a
signed file are classified at pyHanko's modification level.
"""

from __future__ import annotations

import io
import sysconfig
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if sysconfig.get_config_var("Py_GIL_DISABLED") == 1:
    # As in tests/test_signing.py: pyHanko enables the GIL of a free-threaded build.
    pytest.skip("pyHanko enables the GIL of a free-threaded build", allow_module_level=True)

from asn1crypto import keys as asn1_keys
from asn1crypto import x509 as asn1_x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.sign import signers
from pyhanko.sign.diff_analysis import ModificationLevel
from pyhanko_certvalidator.registry import SimpleCertificateStore
from test_signing import (
    DER,
    PEM,
    PKCS8,
    Pki,
    _certificate,
    _document,
    _name,
    _pem_signer,
    _statuses,
    _timestamper,
    make_pki,
)

import pyropdf
from pyropdf import Caret, Signer, TextField

if TYPE_CHECKING:
    from collections.abc import Callable

#: pyHanko's levels as SignatureVerification names them.
LEVELS = {
    ModificationLevel.NONE: "none",
    ModificationLevel.LTA_UPDATES: "none",
    ModificationLevel.FORM_FILLING: "form filling",
    ModificationLevel.OTHER: "other",
}


@pytest.fixture(scope="module")
def pki() -> Pki:
    return make_pki()


def _root(pki: Pki) -> list[bytes]:
    return [pki.root.public_bytes(DER)]


def _verified(data: bytes, pki: Pki) -> list[pyropdf.SignatureVerification]:
    return [signature.verify(_root(pki)) for signature in pyropdf.open(data).signatures]


def _rsa_signer(pki: Pki) -> Signer:
    key, certificate = pki.signers["rsa"]
    return Signer.callback(
        [certificate.public_bytes(DER), pki.root.public_bytes(DER)],
        lambda data: key.sign(data, padding.PKCS1v15(), hashes.SHA256()),
    )


def _pyhanko_signed(pki: Pki, name: str, data: bytes) -> bytes:
    key, certificate = pki.signers[name]
    signer = signers.SimpleSigner(
        signing_cert=asn1_x509.Certificate.load(certificate.public_bytes(DER)),
        signing_key=asn1_keys.PrivateKeyInfo.load(key.private_bytes(DER, PKCS8, serialization.NoEncryption())),
        cert_registry=SimpleCertificateStore.from_certs([asn1_x509.Certificate.load(pki.root.public_bytes(DER))]),
    )
    out = signers.sign_pdf(
        IncrementalPdfFileWriter(io.BytesIO(data)),
        signers.PdfSignatureMetadata(field_name="Hanko"),
        signer=signer,
    )
    return out.getvalue()


@pytest.mark.parametrize("name", ["p256", "p384", "rsa", "timestamped"])
def test_pyropdf_signatures_verify(pki: Pki, name: str) -> None:
    doc = _document()
    if name == "rsa":
        signed = doc.sign("Signature1", _rsa_signer(pki))
    elif name == "timestamped":
        signed = doc.sign("Signature1", _pem_signer(pki), timestamper=_timestamper(pki))
    else:
        signed = doc.sign("Signature1", _pem_signer(pki, name))
    [signature] = pyropdf.open(signed).signatures
    assert signature.field == "Signature1"
    assert signature.revision == 1
    result = signature.verify(_root(pki))
    assert result, result.problems
    assert result.intact
    assert result.covers_revision
    assert result.valid
    assert result.trusted
    assert result.modifications == "none"
    assert result.problems == ()
    signer = "rsa" if name == "rsa" else "p256" if name == "timestamped" else name
    assert result.signer == f"CN=pyropdf test signer {signer}"
    if name == "timestamped":
        assert result.timestamp_valid
        assert result.timestamp is not None
        assert abs(result.timestamp - datetime.now(UTC)) < timedelta(minutes=5)
    else:
        assert result.timestamp is None
        assert result.timestamp_valid is None
    [status] = _statuses(pki, signed)
    assert (status.intact, status.valid, status.trusted) == (result.intact, result.valid, result.trusted)


@pytest.mark.parametrize("name", ["p256", "rsa"])
def test_pyhanko_signatures_verify(pki: Pki, name: str) -> None:
    signed = _pyhanko_signed(pki, name, _document().to_bytes())
    [result] = _verified(signed, pki)
    assert result, result.problems
    assert result.signer == f"CN=pyropdf test signer {name}"
    assert result.signing_time is not None


@pytest.mark.parametrize("offset", [200, -100])
def test_a_flipped_byte_inside_the_range_fails(pki: Pki, offset: int) -> None:
    signed = bytearray(_document().sign("Signature1", _pem_signer(pki)))
    signed[offset] ^= 0x01
    [result] = _verified(bytes(signed), pki)
    assert not result
    assert not result.intact
    assert "the signed bytes' digest does not match the signature's" in result.problems


def test_a_changed_signature_value_fails(pki: Pki) -> None:
    signed = _document().sign("Signature1", _pem_signer(pki))
    [signature] = pyropdf.open(signed).signatures
    start = signature.byte_range[1] + 1
    # The DER of a P-256 signature value sits near the end of the CMS bytes,
    # before the zero padding; flip one of its hexadecimal digits.
    end = signed.index(b"0000", start) - 2
    tampered = signed[:end] + (b"1" if signed[end : end + 1] != b"1" else b"2") + signed[end + 1 :]
    [result] = _verified(tampered, pki)
    assert result.intact
    assert result.valid is False
    assert not result


def _fill(doc: pyropdf.Document, _pki: Pki) -> None:
    doc.form["Name"].value = "Ada"


def _annotate(doc: pyropdf.Document, _pki: Pki) -> None:
    doc.pages[0].annotations.add(Caret((300, 300, 320, 320), contents="here"))


def _rotate(doc: pyropdf.Document, _pki: Pki) -> None:
    doc.pages[0].rotation = 90


def _sign_again(doc: pyropdf.Document, pki: Pki) -> None:
    doc.sign("Reviewer", _pem_signer(pki, "p384"))


def _nothing(_doc: pyropdf.Document, _pki: Pki) -> None:
    pass


@pytest.mark.parametrize(
    ("edit", "level"),
    [
        (_nothing, "none"),
        (_fill, "form filling"),
        (_annotate, "other"),
        (_rotate, "other"),
        (_sign_again, "form filling"),
    ],
)
def test_later_revisions_are_classified_as_pyhanko_classifies_them(
    pki: Pki,
    edit: Callable[[pyropdf.Document, Pki], None],
    level: str,
) -> None:
    doc = _document()
    doc.pages[0].widgets.add(TextField("Name", (72, 200, 272, 220)))
    doc = pyropdf.open(doc.to_bytes())
    doc.sign("Signature1", _pem_signer(pki))
    edit(doc, pki)
    data = doc.to_bytes(incremental=True)
    [signature] = [signature for signature in pyropdf.open(data).signatures if signature.field == "Signature1"]
    first = signature.verify(_root(pki))
    status = _statuses(pki, data)[0]
    assert first.intact
    assert first.valid
    assert first.modifications == level
    assert LEVELS[status.modification_level] == level


def test_a_trust_anchor_decides_trust(pki: Pki) -> None:
    signed = _document().sign("Signature1", _pem_signer(pki))
    [signature] = pyropdf.open(signed).signatures
    assert signature.verify().trusted is None
    assert signature.verify([pki.root.public_bytes(PEM)]).trusted
    other_key = ec.generate_private_key(ec.SECP256R1())
    other = _certificate("pyropdf test CA", other_key.public_key(), (other_key, _name("pyropdf test CA")), ca=True)
    result = signature.verify([other.public_bytes(DER)])
    assert result.trusted is False
    assert not result
    assert any("no trusted certificate issued" in problem for problem in result.problems)


def test_documents_without_signatures(pki: Pki) -> None:
    assert _document().signatures == []
    assert pyropdf.Document().signatures == []
    with pytest.raises(TypeError, match="trust must be a sequence"):
        pyropdf.open(_document().sign("Signature1", _pem_signer(pki))).signatures[0].verify(b"certificate")  # ty: ignore[invalid-argument-type]
