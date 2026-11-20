"""Tests for DocMDP/FieldMDP permissions, DSS/VRI, DocTimeStamp, LegalAttestation, and PDF 2.0 wrappers."""

# ruff: noqa: S106 - test passwords are fixture data.

from __future__ import annotations

import hashlib
import io
import sysconfig
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import pyropdf
from pyropdf import (
    Collection,
    CollectionView,
    DocMdpPermission,
    EncryptedPayload,
    Encryption,
    FieldLock,
    FieldMdpAction,
    LegalAttestation,
    LimitError,
    MalformedPdfError,
    Name,
    PdfError,
    SecurityStore,
    SignatureField,
    Signer,
    TextField,
    ValidationInfo,
    vri_key,
    wrap_encrypted_payload,
)

if TYPE_CHECKING:
    from pyropdf._signing import Timestamper

DOCUMENT = Path(__file__).parent / "assets" / "real_world" / "pdf20-simple.pdf"
PEM = serialization.Encoding.PEM
DER = serialization.Encoding.DER
PKCS8 = serialization.PrivateFormat.PKCS8


def _pdf_with_version(version: str = "1.7") -> bytes:
    """Build a minimal valid 1-page PDF of ``version``."""
    parts = [
        f"%PDF-{version}\n".encode("ascii"),
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << >> >>\nendobj\n",
    ]
    body = b"".join(parts)
    offsets = []
    pos = 0
    for obj_num in (1, 2, 3):
        idx = body.index(f"{obj_num} 0 obj".encode("ascii"), pos)
        offsets.append(idx)
        pos = idx + 1
    xref_offset = len(body)
    xref = [b"xref\n0 4\n0000000000 65535 f \n"]
    xref.extend(f"{off:010d} 00000 n \n".encode("ascii") for off in offsets)
    trailer = f"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    return body + b"".join(xref) + trailer


def _der_tlv(tag: int, body: bytes) -> bytes:
    n = len(body)
    if n < 0x80:
        header = bytes([tag, n])
    else:
        length_bytes = n.to_bytes((n.bit_length() + 7) // 8, "big")
        header = bytes([tag, 0x80 | len(length_bytes)]) + length_bytes
    return header + body


def _der_oid(dotted: str) -> bytes:
    arcs = [int(part) for part in dotted.split(".")]
    out = bytearray([40 * arcs[0] + arcs[1]])
    for arc in arcs[2:]:
        if arc == 0:
            out.append(0)
            continue
        chunks: list[int] = []
        val = arc
        while val:
            chunks.append(val & 0x7F)
            val >>= 7
        chunks.reverse()
        for idx, chunk in enumerate(chunks):
            out.append(chunk | (0x80 if idx < len(chunks) - 1 else 0))
    return _der_tlv(0x06, bytes(out))


def _der_int(value: int) -> bytes:
    length = max(1, (value.bit_length() + 8) // 8)
    return _der_tlv(0x02, value.to_bytes(length, "big", signed=True))


def _der_attr(oid: str, encoded_val: bytes) -> bytes:
    return _der_tlv(0x30, _der_oid(oid) + _der_tlv(0x31, encoded_val))


def _build_ecdsa_timestamp_token(
    tsa_key: ec.EllipticCurvePrivateKey,
    tsa_cert: x509.Certificate,
    ca_cert: x509.Certificate,
    digest: bytes,
    algorithm: str,
) -> bytes:
    """Build a DER RFC 3161 ``TimeStampToken`` signed with ECDSA P-256 without importing pyHanko."""
    alg_oids = {
        "sha256": "2.16.840.1.101.3.4.2.1",
        "sha384": "2.16.840.1.101.3.4.2.2",
    }
    digest_oid = alg_oids[algorithm]
    sha256_alg_id = _der_tlv(0x30, _der_oid("2.16.840.1.101.3.4.2.1"))
    imprint_alg_id = _der_tlv(0x30, _der_oid(digest_oid))
    imprint = _der_tlv(0x30, imprint_alg_id + _der_tlv(0x04, digest))
    gen_time = datetime.now(UTC).strftime("%Y%m%d%H%M%SZ").encode("ascii")
    tst_info = _der_tlv(
        0x30,
        _der_int(1) + _der_oid("1.2.3.4.1") + imprint + _der_int(42) + _der_tlv(0x18, gen_time),
    )

    tst_hash = hashlib.sha256(tst_info).digest()
    tsa_der = tsa_cert.public_bytes(DER)
    ca_der = ca_cert.public_bytes(DER)
    cert_hash = hashlib.sha256(tsa_der).digest()
    ess_cert_id_v2 = _der_tlv(0x30, _der_tlv(0x30, _der_tlv(0x30, _der_tlv(0x04, cert_hash))))

    attrs = sorted(
        [
            _der_attr("1.2.840.113549.1.9.3", _der_oid("1.2.840.113549.1.9.16.1.4")),
            _der_attr("1.2.840.113549.1.9.4", _der_tlv(0x04, tst_hash)),
            _der_attr("1.2.840.113549.1.9.16.2.47", ess_cert_id_v2),
        ]
    )
    signed_attrs_set = _der_tlv(0x31, b"".join(attrs))
    sig_bytes = tsa_key.sign(signed_attrs_set, ec.ECDSA(hashes.SHA256()))

    issuer_der = tsa_cert.issuer.public_bytes()
    sid = _der_tlv(0x30, issuer_der + _der_int(tsa_cert.serial_number))
    signer_info = _der_tlv(
        0x30,
        _der_int(1)
        + sid
        + sha256_alg_id
        + _der_tlv(0xA0, b"".join(attrs))
        + _der_tlv(0x30, _der_oid("1.2.840.10045.4.3.2"))
        + _der_tlv(0x04, sig_bytes),
    )

    certs = sorted([tsa_der, ca_der])
    encap = _der_tlv(
        0x30,
        _der_oid("1.2.840.1.113549.1.9.16.1.4")
        if False
        else _der_oid("1.2.840.113549.1.9.16.1.4") + _der_tlv(0xA0, _der_tlv(0x04, tst_info)),
    )
    signed_data = _der_tlv(
        0x30,
        _der_int(3)
        + _der_tlv(0x31, sha256_alg_id)
        + encap
        + _der_tlv(0xA0, b"".join(certs))
        + _der_tlv(0x31, signer_info),
    )
    return _der_tlv(0x30, _der_oid("1.2.840.113549.1.7.2") + _der_tlv(0xA0, signed_data))


@dataclass(frozen=True)
class LocalPki:
    """Thread-safe ECDSA test CA, signer, and TSA without module-level pyHanko imports."""

    root: x509.Certificate
    signer_key: ec.EllipticCurvePrivateKey
    signer_cert: x509.Certificate
    tsa_key: ec.EllipticCurvePrivateKey
    tsa_cert: x509.Certificate

    @property
    def signer(self) -> Signer:
        """Return a :class:`Signer` backed by the local ECDSA signer key and chain."""
        pem = self.signer_key.private_bytes(PEM, PKCS8, serialization.NoEncryption())
        return Signer.from_pem(pem, self.signer_cert.public_bytes(PEM) + self.root.public_bytes(PEM))

    @property
    def trust(self) -> list[bytes]:
        """Return DER bytes of the local root CA trust anchor."""
        return [self.root.public_bytes(DER)]

    @property
    def timestamper(self) -> Timestamper:
        """Return an RFC 3161 timestamper callback backed by the local ECDSA TSA."""

        def stamp(digest: bytes, algorithm: str) -> bytes:
            return _build_ecdsa_timestamp_token(self.tsa_key, self.tsa_cert, self.root, digest, algorithm)

        return stamp


def _issue_cert(
    common_name: str,
    public_key: ec.EllipticCurvePublicKey | rsa.RSAPublicKey,
    issuer: tuple[ec.EllipticCurvePrivateKey, x509.Name],
    *,
    ca: bool = False,
    tsa: bool = False,
) -> x509.Certificate:
    now = datetime.now(UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer[1])
        .public_key(public_key)
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
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
    )
    if tsa:
        builder = builder.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), critical=True)
    return builder.sign(issuer[0], hashes.SHA256())


@pytest.fixture(scope="module")
def pki() -> LocalPki:
    ca_key = ec.generate_private_key(ec.SECP256R1())
    issuer = (ca_key, x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pyropdf local CA")]))
    root = _issue_cert("pyropdf local CA", ca_key.public_key(), issuer, ca=True)
    signer_key = ec.generate_private_key(ec.SECP256R1())
    signer_cert = _issue_cert("pyropdf local signer", signer_key.public_key(), issuer)
    tsa_key = ec.generate_private_key(ec.SECP256R1())
    tsa_cert = _issue_cert("pyropdf local TSA", tsa_key.public_key(), issuer, tsa=True)
    return LocalPki(root=root, signer_key=signer_key, signer_cert=signer_cert, tsa_key=tsa_key, tsa_cert=tsa_cert)


def test_public_exports_and_input_validation() -> None:
    for cls in (
        DocMdpPermission,
        EncryptedPayload,
        FieldLock,
        FieldMdpAction,
        LegalAttestation,
        SecurityStore,
        ValidationInfo,
    ):
        assert cls.__module__ == "pyropdf"
    assert vri_key.__module__ == "pyropdf"
    assert wrap_encrypted_payload.__module__ == "pyropdf"

    with pytest.raises(ValueError, match="ALL"):
        FieldLock(FieldMdpAction.ALL, ("Name",))
    with pytest.raises(ValueError, match="INCLUDE"):
        FieldLock(FieldMdpAction.INCLUDE, ())
    with pytest.raises(ValueError, match="EXCLUDE"):
        FieldLock(FieldMdpAction.EXCLUDE, ())
    with pytest.raises(ValueError, match="empty"):
        FieldLock(FieldMdpAction.INCLUDE, ("",))
    with pytest.raises(TypeError, match="sequence"):
        FieldLock(FieldMdpAction.INCLUDE, "Name")
    with pytest.raises(ValueError, match="4096"):
        FieldLock(FieldMdpAction.INCLUDE, [f"f{i}" for i in range(4097)])

    with pytest.raises(TypeError, match="bytes-like"):
        vri_key("not-bytes")  # ty: ignore[invalid-argument-type]
    assert vri_key(b"abc") == "A9993E364706816ABA3E25717850C26C9CD0D89D"
    assert vri_key(memoryview(bytearray(b"abc"))) == "A9993E364706816ABA3E25717850C26C9CD0D89D"  # ty: ignore[invalid-argument-type]

    with pytest.raises(ValueError, match="both timestamp"):
        ValidationInfo(timestamp=b"\x30\x00", created="D:20260101120000Z")
    with pytest.raises(TypeError, match="iterable"):
        ValidationInfo(certificates=b"single")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="40 uppercase"):
        SecurityStore(vri={"abcd": ValidationInfo()})
    with pytest.raises(ValueError, match="40 uppercase"):
        SecurityStore(vri={"a" * 40: ValidationInfo()})
    with pytest.raises(TypeError, match="ValidationInfo"):
        SecurityStore(vri={"A" * 40: "bad"})  # ty: ignore[invalid-argument-type]

    with pytest.raises(TypeError, match="non-negative int"):
        LegalAttestation(annotations=True)
    with pytest.raises(ValueError, match="non-negative"):
        LegalAttestation(annotations=-1)
    with pytest.raises(ValueError, match="empty"):
        EncryptedPayload(b"")
    with pytest.raises(ValueError, match="PDF name"):
        EncryptedPayload(b"%PDF", subtype="/leading")


def _exercise_field_lock_lifecycle(doc: pyropdf.Document) -> None:
    doc.pages[0].widgets.add(TextField("Name", (72, 700, 272, 724)))
    doc.pages[0].widgets.add(TextField("Notes", (72, 660, 272, 684)))
    doc.pages[0].widgets.add(SignatureField("AuthorSig", (72, 600, 272, 640)))
    assert doc.form["Name"].lock is None
    with pytest.raises(ValueError, match="not a signature field"):
        doc.form["Name"].lock = FieldLock()

    sig_field = doc.form["AuthorSig"]
    assert sig_field.lock is None
    sig_field.lock = FieldLock(FieldMdpAction.ALL)
    assert doc.catalog["Version"] == Name("1.5")
    first_lock_xref = sig_field.obj["Lock"].xref
    assert first_lock_xref is not None
    assert sig_field.lock == FieldLock(FieldMdpAction.ALL)

    sig_field.lock = FieldLock(FieldMdpAction.INCLUDE, ("Name",))
    second_lock_xref = sig_field.obj["Lock"].xref
    assert second_lock_xref != first_lock_xref
    assert first_lock_xref not in doc.objects
    assert sig_field.lock == FieldLock(FieldMdpAction.INCLUDE, ("Name",))

    doc.history.undo()
    assert sig_field.lock == FieldLock(FieldMdpAction.ALL)
    doc.history.redo()
    assert sig_field.lock == FieldLock(FieldMdpAction.INCLUDE, ("Name",))

    del sig_field.lock
    assert sig_field.lock is None
    assert second_lock_xref not in doc.objects


@pytest.mark.spec("signature-transforms", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.spec("permissions", "read", "interpret", "create", "edit", "preserve")
def test_doc_mdp_field_mdp_field_lock_and_prepare_signature(pki: LocalPki) -> None:
    with pyropdf.open(_pdf_with_version("1.4")) as doc:
        _exercise_field_lock_lifecycle(doc)
        sig_field = doc.form["AuthorSig"]
        prepared_xref = doc.prepare_signature(
            "AuthorSig",
            doc_mdp=DocMdpPermission.FORM_FILLING_AND_SIGNATURES,
            field_lock=FieldLock(FieldMdpAction.INCLUDE, ("Name",)),
            reason="Author certification",
        )
        assert prepared_xref == sig_field.xref
        assert sig_field.lock == FieldLock(FieldMdpAction.INCLUDE, ("Name",))
        prepared_bytes = doc.to_bytes()

    with pyropdf.open(prepared_bytes) as prepared:
        assert not prepared.check()
        assert prepared.certification is None
        certified_bytes = prepared.sign("AuthorSig", pki.signer)
        assert prepared.certification is DocMdpPermission.FORM_FILLING_AND_SIGNATURES
        [cert_sig] = prepared.signatures
        assert not cert_sig.is_timestamp
        assert cert_sig.doc_mdp is DocMdpPermission.FORM_FILLING_AND_SIGNATURES
        assert cert_sig.field_locks == (FieldLock(FieldMdpAction.INCLUDE, ("Name",)),)
        assert prepared.form["AuthorSig"].obj["V"]["Reason"].text == "Author certification"
        assert cert_sig.verify(pki.trust)
        assert not prepared.check()

        with pytest.raises(ValueError, match="certified"):
            prepared.sign("SecondCert", pki.signer, doc_mdp=DocMdpPermission.NO_CHANGES)
        with pytest.raises(ValueError, match="certified"):
            prepared.prepare_signature("SecondCert", doc_mdp=DocMdpPermission.NO_CHANGES)
        with pytest.raises(TypeError, match="DocMdpPermission"):
            prepared.sign("BadMdp", pki.signer, doc_mdp=True)

        prepared.form["Notes"].value = "Reviewed"
        second_bytes = prepared.sign(
            "ReviewerSig",
            pki.signer,
            field_lock=FieldLock(FieldMdpAction.EXCLUDE, ("Notes",)),
        )
        assert second_bytes.startswith(certified_bytes)

    with pyropdf.open(second_bytes) as final_doc:
        assert not final_doc.check()
        assert final_doc.certification is DocMdpPermission.FORM_FILLING_AND_SIGNATURES
        author_sig, reviewer_sig = final_doc.signatures
        assert author_sig.doc_mdp is DocMdpPermission.FORM_FILLING_AND_SIGNATURES
        assert author_sig.field_locks == (FieldLock(FieldMdpAction.INCLUDE, ("Name",)),)
        assert reviewer_sig.doc_mdp is None
        assert reviewer_sig.field_locks == (FieldLock(FieldMdpAction.EXCLUDE, ("Notes",)),)
        author_ver = author_sig.verify(pki.trust)
        reviewer_ver = reviewer_sig.verify(pki.trust)
        assert (author_ver.intact, author_ver.valid, author_ver.modifications) == (True, True, "form filling")
        assert (reviewer_ver.intact, reviewer_ver.valid, reviewer_ver.modifications) == (True, True, "none")

        assert author_sig.xref is not None
        del final_doc.objects[author_sig.xref]["Reference"][0]["TransformParams"]["P"]
        assert final_doc.certification is DocMdpPermission.FORM_FILLING_AND_SIGNATURES
        assert author_sig.doc_mdp is DocMdpPermission.FORM_FILLING_AND_SIGNATURES


@pytest.mark.spec("document-security-store", "read", "interpret", "create", "edit", "preserve")
def test_document_security_store_deduplication_and_signature_preservation(pki: LocalPki) -> None:
    # 1. Unsigned document: create, deduplicate streams, replace, delete, undo/redo.
    cert_a = pki.signer_cert.public_bytes(DER)
    cert_b = pki.root.public_bytes(DER)
    ocsp_a = b"\x30\x06\x0a\x01\x00\xa0\x01\x05"
    crl_a = b"\x30\x03\x02\x01\x01"
    fake_contents = b"\x30\x82\x01\x00signature-bytes"
    key = vri_key(fake_contents)
    created_dt = datetime(2026, 6, 15, 10, 30, 0, tzinfo=UTC)

    with pyropdf.open(_pdf_with_version("1.7")) as doc:
        assert doc.security_store is None
        store = SecurityStore(
            certificates=(cert_a, cert_b),
            ocsps=(ocsp_a,),
            crls=(crl_a,),
            vri={
                key: ValidationInfo(
                    certificates=(cert_a, cert_b),
                    ocsps=(ocsp_a,),
                    crls=(crl_a,),
                    created=created_dt,
                )
            },
        )
        doc.security_store = store
        dss_obj = doc.catalog["DSS"]
        # Identical DER blobs in /Certs and /VRI/<key>/Cert share the exact same indirect stream xrefs!
        top_cert_xrefs = [s.xref for s in dss_obj["Certs"]]
        vri_cert_xrefs = [s.xref for s in dss_obj["VRI"][key]["Cert"]]
        assert top_cert_xrefs == vri_cert_xrefs
        assert len(set(top_cert_xrefs)) == 2
        assert doc.security_store == SecurityStore(
            certificates=(cert_a, cert_b),
            ocsps=(ocsp_a,),
            crls=(crl_a,),
            vri={
                key: ValidationInfo(
                    certificates=(cert_a, cert_b),
                    ocsps=(ocsp_a,),
                    crls=(crl_a,),
                    created="D:20260615103000Z",
                )
            },
        )
        assert not pyropdf.open(doc.to_bytes()).check()

        del doc.security_store
        assert doc.security_store is None
        for xref in top_cert_xrefs:
            assert xref not in doc.objects
        doc.history.undo()
        assert doc.security_store is not None

    # 2. Signed PDF 1.7 document: incremental DSS + VRI updates keep prior signature modifications == "none"!
    with pyropdf.open(_pdf_with_version("1.7")) as signed_doc:
        rev1 = signed_doc.sign("Sig1", pki.signer)

    with pyropdf.open(rev1) as ltv_doc:
        [sig1] = ltv_doc.signatures
        assert sig1.xref is not None
        sig1_contents = bytes(ltv_doc.objects[sig1.xref]["Contents"])
        sig1_key = vri_key(sig1_contents)
        ts_token = pki.timestamper(hashlib.sha256(sig1_contents).digest(), "sha256")
        ltv_doc.security_store = SecurityStore(
            certificates=(cert_a, cert_b),
            ocsps=(ocsp_a,),
            crls=(crl_a,),
            vri={
                sig1_key: ValidationInfo(
                    certificates=(cert_a, cert_b),
                    ocsps=(ocsp_a,),
                    crls=(crl_a,),
                    timestamp=ts_token,
                )
            },
        )
        assert ltv_doc.changes(incremental=True).signatures[0].objects == (1,)
        rev2 = ltv_doc.to_bytes(incremental=True)
        assert rev2.startswith(rev1)

    with pyropdf.open(rev2) as reopened:
        assert not reopened.check()
        assert reopened.security_store is not None
        assert reopened.security_store.vri[sig1_key].timestamp == ts_token
        [ver1] = [s.verify(pki.trust) for s in reopened.signatures]
        assert ver1, ver1.problems
        assert ver1.modifications == "none"

        # Second incremental DSS update adding a second VRI entry reuses existing streams and stays "none".
        other_key = "B" * 40
        reopened.security_store = SecurityStore(
            certificates=(cert_a, cert_b),
            ocsps=(ocsp_a,),
            crls=(crl_a,),
            vri={
                sig1_key: reopened.security_store.vri[sig1_key],
                other_key: ValidationInfo(certificates=(cert_b,), created="D:20261120000000Z"),
            },
        )
        rev3 = reopened.to_bytes(incremental=True)

    with pyropdf.open(rev3) as reopened3:
        assert not reopened3.check()
        assert reopened3.security_store is not None
        assert set(reopened3.security_store.vri) == {sig1_key, other_key}
        assert reopened3.signatures[0].verify(pki.trust).modifications == "none"


def _assert_timestamp_failure_modes(pki: LocalPki) -> None:
    with pyropdf.open(DOCUMENT.read_bytes()) as err_doc:
        before = err_doc.to_bytes(incremental=True)
        with pytest.raises(TypeError, match="callable"):
            err_doc.timestamp("not-callable")  # ty: ignore[invalid-argument-type]
        with pytest.raises(TypeError, match="must return bytes"):
            err_doc.timestamp(lambda _d, _a: "not-bytes")  # ty: ignore[invalid-argument-type]
        with pytest.raises(ValueError, match="RFC 3161"):
            err_doc.timestamp(lambda _d, _a: b"\x30\x00")
        with pytest.raises(ValueError, match="sha256 digest"):
            err_doc.timestamp(lambda _d, a: pki.timestamper(b"\x00" * 32, a))
        with pytest.raises(ValueError, match="reserved"):
            err_doc.timestamp(pki.timestamper, reserve=32)
        with err_doc.transaction("outer"), pytest.raises(PdfError, match="inside a transaction"):
            err_doc.timestamp(pki.timestamper)
        with pytest.raises(PdfError, match="changed while it was being timestamped"):
            err_doc.timestamp(lambda d, a: (err_doc.pages.new(), pki.timestamper(d, a))[1])
        assert err_doc.to_bytes(incremental=True) == before


@pytest.mark.spec("document-timestamps", "read", "interpret", "create", "edit", "preserve")
def test_document_timestamps_sign_verify_and_preserve_earlier_signatures(pki: LocalPki) -> None:
    with pyropdf.open(DOCUMENT.read_bytes()) as doc:
        signed_bytes = doc.sign(
            "AuthorSig",
            pki.signer,
            doc_mdp=DocMdpPermission.NO_CHANGES,
        )
        stamped_bytes = doc.timestamp(pki.timestamper, field="ArchiveTS1")
        assert stamped_bytes.startswith(signed_bytes)
        assert doc.to_bytes(incremental=True) == stamped_bytes
        assert not doc.check()

        doc.prepare_signature("ArchiveTS2", is_timestamp=True)
        prepared_ts = doc.to_bytes(incremental=True)

    with pyropdf.open(prepared_ts) as doc2:
        stamped2 = doc2.timestamp(pki.timestamper, field="ArchiveTS2")
        assert not doc2.check()

    with pyropdf.open(stamped2) as verified:
        assert not verified.check()
        sig_ts1, sig_ts2, sig_author = verified.signatures
        assert (sig_author.field, sig_ts1.field, sig_ts2.field) == ("AuthorSig", "ArchiveTS1", "ArchiveTS2")
        assert (sig_author.is_timestamp, sig_ts1.is_timestamp, sig_ts2.is_timestamp) == (False, True, True)

        v_author = sig_author.verify(pki.trust)
        v_ts1 = sig_ts1.verify(pki.trust)
        v_ts2 = sig_ts2.verify(pki.trust)
        for v in (v_author, v_ts1, v_ts2):
            assert v, v.problems
            assert (v.intact, v.covers_revision, v.valid, v.trusted, v.modifications) == (
                True,
                True,
                True,
                True,
                "none",
            )
        assert v_ts1.timestamp_valid is True
        assert v_ts1.timestamp is not None
        assert v_ts2.timestamp_valid is True
        assert v_ts2.timestamp is not None
        assert v_ts1.signer == "CN=pyropdf local TSA"

    tampered = bytearray(stamped_bytes)
    tampered[120] ^= 0x01
    with pyropdf.open(bytes(tampered)) as bad_doc:
        [bad_ts] = [s for s in bad_doc.signatures if s.is_timestamp]
        bad_ver = bad_ts.verify(pki.trust)
        assert not bad_ver
        assert not bad_ver.intact
        assert bad_ver.timestamp_valid is False

    _assert_timestamp_failure_modes(pki)


@pytest.mark.spec("legal-attestations", "read", "interpret", "create", "edit", "preserve")
def test_legal_attestation_round_trip_edit_delete_and_signing(pki: LocalPki) -> None:
    full = LegalAttestation(
        javascript_actions=0,
        launch_actions=1,
        uri_actions=2,
        movie_actions=0,
        sound_actions=0,
        hide_annotation_actions=0,
        goto_remote_actions=1,
        alternate_images=0,
        external_streams=0,
        truetype_fonts=3,
        external_ref_xobjects=0,
        external_opi_dicts=0,
        non_embedded_fonts=1,
        dev_dep_gs_op=0,
        dev_dep_gs_ht=0,
        dev_dep_gs_tr=0,
        dev_dep_gs_ucr=0,
        dev_dep_gs_bg=0,
        dev_dep_gs_fl=0,
        annotations=4,
        optional_content=True,
        attestation="Contains external URI and GoToR links reviewed by author.",
    )
    with pyropdf.open(_pdf_with_version("1.4")) as doc:
        assert doc.legal_attestation is None
        doc.legal_attestation = full
        assert doc.catalog["Version"] == Name("1.5")
        first_xref = doc.catalog["Legal"].xref
        assert doc.legal_attestation == full

        updated = LegalAttestation(uri_actions=5, optional_content=False, attestation="Updated attestation.")
        doc.legal_attestation = updated
        assert first_xref not in doc.objects
        assert doc.legal_attestation == updated

        doc.history.undo()
        assert doc.legal_attestation == full
        saved = doc.to_bytes()

    with pyropdf.open(saved) as reopened:
        assert not reopened.check()
        assert reopened.legal_attestation == full
        certified = reopened.sign("CertSig", pki.signer, doc_mdp=DocMdpPermission.FORM_FILLING_AND_SIGNATURES)

    with pyropdf.open(certified) as signed_doc:
        assert not signed_doc.check()
        assert signed_doc.legal_attestation == full
        assert signed_doc.signatures[0].verify(pki.trust)

    with pyropdf.open(saved) as editable:
        del editable.legal_attestation
        assert editable.legal_attestation is None
        assert "Legal" not in editable.catalog
        editable.catalog["Legal"] = {"Annotations": -5}
        with pytest.raises(MalformedPdfError, match="Annotations"):
            _ = editable.legal_attestation


def test_encrypted_payload_wrapper_round_trip_and_conformance() -> None:
    with pyropdf.Document() as inner:
        page = inner.pages.new((300, 200))
        with page.draw() as canvas:
            canvas.text("Top-secret contract clause", (36, 100))
        encrypted_bytes = inner.to_bytes(
            encryption=Encryption(user_password="secret-pass", owner_password="owner-pass"),
        )

    payload = EncryptedPayload(
        encrypted_bytes,
        subtype="adbe.pkcs7.s5",
        filename="contract-encrypted.pdf",
        version="2.0",
        description="PKCS#7 encrypted contract",
    )
    with wrap_encrypted_payload(payload, cover_text="Open this wrapper in a crypto-capable viewer.") as wrapper:
        assert wrapper.pages[0].text().strip() == "Open this wrapper in a crypto-capable viewer."
        assert wrapper.encrypted_payload == payload
        wrapper_bytes = wrapper.to_bytes()

    with pyropdf.open(wrapper_bytes) as reopened:
        assert not reopened.check()
        extracted = reopened.encrypted_payload
        assert extracted == payload
        assert extracted is not None
        with pyropdf.open(extracted.data, password="secret-pass") as unlocked:
            assert "Top-secret contract clause" in unlocked.pages[0].text()

    # Ordinary documents and hidden collections without /EP return None; DocumentLimits bounds stream decode.
    with pyropdf.open(_pdf_with_version("1.7")) as plain:
        assert plain.encrypted_payload is None
        plain.attachments.add("plain.pdf", _pdf_with_version("1.7"))
        plain.collection = Collection(view=CollectionView.HIDDEN, initial_document="plain.pdf")
        assert plain.encrypted_payload is None

    with (
        pyropdf.open(wrapper_bytes, limits=pyropdf.DocumentLimits(max_total_decompressed_size=32)) as bounded,
        pytest.raises(LimitError),
    ):
        _ = bounded.encrypted_payload


def test_additional_permissions_dss_malformed_and_encrypted_edges(pki: LocalPki) -> None:
    # DocMdpPermission.ANNOTATIONS_FORM_FILLING_AND_SIGNATURES (3) + None-clearing setters.
    with pyropdf.open(_pdf_with_version("1.7")) as doc:
        doc.prepare_signature("AnnotCert", doc_mdp=DocMdpPermission.ANNOTATIONS_FORM_FILLING_AND_SIGNATURES)
        doc.sign("AnnotCert", pki.signer)
        assert doc.certification is DocMdpPermission.ANNOTATIONS_FORM_FILLING_AND_SIGNATURES
        assert doc.signatures[0].doc_mdp is DocMdpPermission.ANNOTATIONS_FORM_FILLING_AND_SIGNATURES
        assert not doc.check()

    with pyropdf.open(_pdf_with_version("1.7")) as doc:
        doc.pages[0].widgets.add(SignatureField("SigField", (72, 600, 272, 640)))
        doc.form["SigField"].lock = FieldLock()
        doc.form["SigField"].lock = None
        assert doc.form["SigField"].lock is None
        doc.security_store = SecurityStore(certificates=(pki.root.public_bytes(DER),))
        with (
            pyropdf.open(doc.to_bytes(), limits=pyropdf.DocumentLimits(max_total_decompressed_size=32)) as bounded_dss,
            pytest.raises(LimitError),
        ):
            _ = bounded_dss.security_store
        doc.security_store = None
        assert doc.security_store is None
        doc.legal_attestation = LegalAttestation(annotations=1)
        doc.legal_attestation = None
        assert doc.legal_attestation is None

        # Malformed DSS and VRI dictionaries raise MalformedPdfError.
        doc.catalog["DSS"] = 42
        with pytest.raises(MalformedPdfError, match="DSS"):
            _ = doc.security_store
        doc.catalog["DSS"] = {"Certs": [42]}
        with pytest.raises(MalformedPdfError, match="Certs"):
            _ = doc.security_store
        doc.catalog["DSS"] = {"VRI": {"not-hex": {}}}
        with pytest.raises(MalformedPdfError, match="VRI"):
            _ = doc.security_store
        ts_stream = doc.objects[doc.objects.add_stream(b"\x30\x00")]
        doc.catalog["DSS"] = {"VRI": {"A" * 40: {"TS": ts_stream, "TU": "D:20260101000000Z"}}}
        with pytest.raises(MalformedPdfError, match="both /TS and /TU"):
            _ = doc.security_store

    # Encrypted documents refuse Document.timestamp just as Document.sign does.
    with pyropdf.Document() as plain_doc:
        plain_doc.pages.new((200, 200))
        enc_bytes = plain_doc.to_bytes(encryption=Encryption(user_password="u", owner_password="o"))
    with pyropdf.open(enc_bytes, password="u") as enc_doc, pytest.raises(ValueError, match="encrypted"):
        enc_doc.timestamp(pki.timestamper)


def _build_pyhanko_stamped_doc(
    pyro_signer: Signer,
    sig_cert: x509.Certificate,
    root: x509.Certificate,
    rsa_stamp: Timestamper,
) -> bytes:
    with pyropdf.open(_pdf_with_version("1.7")) as doc:
        doc.prepare_signature("CounterSig")
        base = doc.to_bytes()

    with pyropdf.open(base) as doc:
        doc.sign(
            "CertSig",
            pyro_signer,
            doc_mdp=DocMdpPermission.FORM_FILLING_AND_SIGNATURES,
            field_lock=FieldLock(FieldMdpAction.EXCLUDE, ("CounterSig",)),
        )
        sig_xref = doc.signatures[0].xref
        assert sig_xref is not None
        contents = bytes(doc.objects[sig_xref]["Contents"])
        doc.security_store = SecurityStore(
            certificates=(sig_cert.public_bytes(DER), root.public_bytes(DER)),
            vri={
                vri_key(contents): ValidationInfo(
                    certificates=(sig_cert.public_bytes(DER), root.public_bytes(DER)),
                )
            },
        )
        return doc.timestamp(rsa_stamp, field="DocTS")


def _prepare_external_cert_pdf() -> bytes:
    with pyropdf.open(_pdf_with_version("1.7")) as prep_doc:
        prep_doc.prepare_signature("LockedField")
        prep_doc.prepare_signature(
            "ExternalCert",
            doc_mdp=DocMdpPermission.NO_CHANGES,
            field_lock=FieldLock(FieldMdpAction.INCLUDE, ("LockedField",)),
            reason="Prepared in pyropdf",
        )
        return prep_doc.to_bytes()


def _make_interop_certs() -> tuple[
    x509.Certificate, ec.EllipticCurvePrivateKey, x509.Certificate, rsa.RSAPrivateKey, x509.Certificate
]:
    ca_key = ec.generate_private_key(ec.SECP256R1())
    issuer = (ca_key, x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "interop CA")]))
    root = _issue_cert("interop CA", ca_key.public_key(), issuer, ca=True)
    sig_key = ec.generate_private_key(ec.SECP256R1())
    sig_cert = _issue_cert("interop signer", sig_key.public_key(), issuer)
    tsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    tsa_cert = _issue_cert("interop TSA", tsa_key.public_key(), issuer, tsa=True)
    return root, sig_key, sig_cert, tsa_key, tsa_cert


def test_pyhanko_interop_certification_prepared_fields_dss_and_timestamps() -> None:
    if sysconfig.get_config_var("Py_GIL_DISABLED") == 1:
        pytest.skip("pyHanko enables the GIL of a free-threaded build")

    import asyncio  # noqa: PLC0415

    from asn1crypto import keys as asn1_keys  # noqa: PLC0415
    from asn1crypto import x509 as asn1_x509  # noqa: PLC0415
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter  # noqa: PLC0415
    from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: PLC0415
    from pyhanko.sign import fields as ph_fields  # noqa: PLC0415
    from pyhanko.sign import signers  # noqa: PLC0415
    from pyhanko.sign.diff_analysis import ModificationLevel  # noqa: PLC0415
    from pyhanko.sign.general import SigningError  # noqa: PLC0415
    from pyhanko.sign.timestamps.dummy_client import DummyTimeStamper  # noqa: PLC0415
    from pyhanko.sign.validation import (  # noqa: PLC0415
        read_certification_data,
        validate_pdf_signature,
        validate_pdf_timestamp,
    )
    from pyhanko_certvalidator import ValidationContext  # noqa: PLC0415
    from pyhanko_certvalidator.registry import SimpleCertificateStore  # noqa: PLC0415

    root, sig_key, sig_cert, tsa_key, tsa_cert = _make_interop_certs()
    root_der = root.public_bytes(DER)
    sig_der = sig_cert.public_bytes(DER)
    sig_key_der = sig_key.private_bytes(DER, PKCS8, serialization.NoEncryption())

    pyro_signer = Signer.from_pem(
        sig_key.private_bytes(PEM, PKCS8, serialization.NoEncryption()),
        sig_cert.public_bytes(PEM) + root.public_bytes(PEM),
    )
    dummy_tsa = DummyTimeStamper(
        tsa_cert=asn1_x509.Certificate.load(tsa_cert.public_bytes(DER)),
        tsa_key=asn1_keys.PrivateKeyInfo.load(tsa_key.private_bytes(DER, PKCS8, serialization.NoEncryption())),
    )
    vc = ValidationContext(
        trust_roots=[asn1_x509.Certificate.load(root_der)], allow_fetching=False, revocation_mode="soft-fail"
    )

    stamped = _build_pyhanko_stamped_doc(
        pyro_signer,
        sig_cert,
        root,
        lambda digest, alg: asyncio.run(dummy_tsa.async_timestamp(digest, alg)).dump(),
    )
    reader = PdfFileReader(io.BytesIO(stamped))
    cert_info = read_certification_data(reader)
    assert cert_info is not None
    assert cert_info.permission.value == 2
    sig_status = validate_pdf_signature(reader.embedded_regular_signatures[0], vc)
    ts_status = validate_pdf_timestamp(reader.embedded_timestamp_signatures[0], vc)
    assert (sig_status.intact, sig_status.valid, sig_status.trusted) == (True, True, True)
    assert sig_status.modification_level == ModificationLevel.LTA_UPDATES
    assert (ts_status.intact, ts_status.valid, ts_status.trusted) == (True, True, True)

    with pyropdf.open(stamped) as check_doc:
        assert not check_doc.check()
        s0, s1 = check_doc.signatures
        assert s0.verify([root_der]).modifications == "none"
        v1 = s1.verify([root_der])
        assert (bool(v1), v1.timestamp_valid) == (True, True)

    ph_signer = signers.SimpleSigner(
        signing_cert=asn1_x509.Certificate.load(sig_der),
        signing_key=asn1_keys.PrivateKeyInfo.load(sig_key_der),
        cert_registry=SimpleCertificateStore.from_certs([asn1_x509.Certificate.load(root_der)]),
    )
    ph_out = signers.sign_pdf(
        IncrementalPdfFileWriter(io.BytesIO(_prepare_external_cert_pdf())),
        signers.PdfSignatureMetadata(
            field_name="ExternalCert",
            certify=True,
            docmdp_permissions=ph_fields.MDPPerm.NO_CHANGES,
            subfilter=ph_fields.SigSeedSubFilter.PADES,
        ),
        signer=ph_signer,
        existing_fields_only=True,
    ).getvalue()

    with pyropdf.open(ph_out) as after_ph:
        assert not after_ph.check()
        assert after_ph.certification is DocMdpPermission.NO_CHANGES
        [ph_sig] = after_ph.signatures
        assert (ph_sig.doc_mdp, ph_sig.field_locks, bool(ph_sig.verify([root_der]))) == (
            DocMdpPermission.NO_CHANGES,
            (FieldLock(FieldMdpAction.INCLUDE, ("LockedField",)),),
            True,
        )

    with pytest.raises(SigningError, match="forbids all changes"):
        signers.sign_pdf(
            IncrementalPdfFileWriter(io.BytesIO(ph_out)),
            signers.PdfSignatureMetadata(field_name="Forbidden"),
            signer=ph_signer,
        )
