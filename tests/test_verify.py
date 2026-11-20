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

from asn1crypto import cms
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
from pyropdf import Caret, Signer, TextField, _core

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
@pytest.mark.spec("digital-signatures", "read", "interpret")
@pytest.mark.spec("ecc-signatures", "read", "interpret")
@pytest.mark.spec("signature-fields", "read", "interpret")
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
    der = bytes.fromhex(signed[start : signature.byte_range[2] - 1].decode())
    # Locate the signature value through the CMS structure rather than by a
    # byte pattern, which random certificates and digests can also contain.
    content = cms.ContentInfo.load(der, strict=False)
    signer = content["content"]["signer_infos"][0]
    value = signer["signature"].native
    # Alter the low bit of s, preserving the ECDSA value's DER structure.
    signer["signature"] = value[:-1] + bytes([value[-1] ^ 1])
    encoded = content.dump()
    # The CMS re-encoding must change exactly the signature byte and leave the
    # reserved zero padding and every other byte untouched.
    changed = [i for i, (before, after) in enumerate(zip(der, encoded, strict=False)) if before != after]
    assert len(encoded) <= len(der)
    assert len(changed) == 1
    assert der[len(encoded) :] == bytes(len(der) - len(encoded))
    offset = start + 2 * changed[0]
    tampered = signed[:offset] + encoded[changed[0] : changed[0] + 1].hex().encode() + signed[offset + 2 :]
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


def _certify(pki: Pki, level: int, lock: pyropdf.FieldLock) -> pyropdf.Document:
    doc = _document()
    doc.pages[0].widgets.add(TextField("Name", (72, 200, 272, 220)))
    doc.prepare_signature("Reviewer")
    doc = pyropdf.open(doc.to_bytes())
    doc.sign("Author", _pem_signer(pki), doc_mdp=pyropdf.DocMdpPermission(level), field_lock=lock)
    return doc


@pytest.mark.spec("signature-transforms", "interpret")
@pytest.mark.parametrize(
    ("level", "action", "doc_allows", "lock_allows"),
    [(1, "Exclude", False, True), (2, "Include", True, False), (2, "Exclude", True, True), (3, "All", True, False)],
)
def test_field_permissions_predict_independent_verdict(
    pki: Pki, level: int, action: str, *, doc_allows: bool, lock_allows: bool
) -> None:
    doc = _certify(pki, level, pyropdf.FieldLock(pyropdf.FieldMdpAction(action), () if action == "All" else ("Name",)))
    original = doc.to_bytes(incremental=True)
    assert not doc.changes(incremental=True)
    field = doc.form["Name"]
    field.value = "Alice"
    report = doc.changes(incremental=True)
    assert [(e.transform, e.permitted) for e in report.permissions] == [
        (f"DocMDP P={level}", doc_allows),
        (f"FieldMDP {action}", lock_allows),
    ]
    assert all(
        (e.signature, e.xref, e.edit, e.field, e.object)
        == ("Author", doc.form["Author"].xref, "form filling", "Name", field.xref)
        and e.reason
        for e in report.permissions
    )
    [affected] = report.signatures
    assert affected.signed_bytes_preserved
    assert affected.objects == (field.xref,)
    saved = doc.to_bytes(incremental=True)
    assert saved.startswith(original)
    [status] = _statuses(pki, saved)
    assert (status.intact, status.valid) == (True, True)
    assert status.docmdp_ok is (doc_allows and lock_allows)
    rewritten = doc.changes(incremental=False)
    assert not rewritten.signatures[0].signed_bytes_preserved
    assert rewritten.permissions == ()
    doc.history.undo()
    assert not doc.changes(incremental=True)
    assert doc.to_bytes(incremental=True) == original


@pytest.mark.spec("signature-transforms", "interpret")
@pytest.mark.parametrize("level", [1, 2, 3])
def test_annotation_permission_and_pyhanko_annotation_policy(pki: Pki, level: int) -> None:
    doc = _certify(pki, level, pyropdf.FieldLock())
    _annotate(doc, pki)
    report = doc.changes(incremental=True)
    certification = [e for e in report.permissions if e.transform.startswith("DocMDP")]
    assert {e.object for e in certification} == {doc.pages[0].obj.xref, doc.pages[0].annotations[-1].xref}
    assert all(e.edit == "annotation" and e.permitted is (level == 3) for e in certification)
    assert all(e.permitted for e in report.permissions if e.transform == "FieldMDP All")
    [status] = _statuses(pki, doc.to_bytes(incremental=True))
    assert (status.intact, status.valid) == (True, True)
    # pyHanko's default difference policy treats even P=3 markup as OTHER.
    assert status.modification_level == ModificationLevel.OTHER
    assert status.docmdp_ok is False


@pytest.mark.spec("signature-transforms", "interpret")
@pytest.mark.parametrize("field", ["Reviewer", "NewReviewer"])
def test_second_signature_preflight_and_unrelated_edit(pki: Pki, field: str) -> None:
    doc = _certify(pki, 2, pyropdf.FieldLock(pyropdf.FieldMdpAction.EXCLUDE, ("Name", field)))
    preflight: list[pyropdf.ChangeReport] = []
    key, certificate = pki.signers["rsa"]

    def approve(attributes: bytes) -> bytes:
        preflight.append(doc.changes(incremental=True))
        return key.sign(attributes, padding.PKCS1v15(), hashes.SHA256())

    signer = Signer.callback([certificate.public_bytes(DER), pki.root.public_bytes(DER)], approve)
    signed = doc.sign(field, signer, field_lock=pyropdf.FieldLock(pyropdf.FieldMdpAction.INCLUDE, ("Name",)))
    [report] = preflight
    assert {e.transform for e in report.permissions} == {"DocMDP P=2", "FieldMDP Exclude"}
    assert all(
        e.signature == "Author" and e.edit == "signing" and e.field in (None, field) and e.permitted
        for e in report.permissions
    )
    assert any(e.field == field for e in report.permissions)
    assert all(status.intact and status.valid and status.docmdp_ok for status in _statuses(pki, signed))
    assert not doc.changes(incremental=True)

    doc.form["Name"].value = "Ada"
    by_signature = {(e.signature, e.transform): e.permitted for e in doc.changes(incremental=True).permissions}
    assert by_signature == {
        ("Author", "DocMDP P=2"): True,
        ("Author", "FieldMDP Exclude"): True,
        (field, "FieldMDP Include"): False,
    }
    author, reviewer = _statuses(pki, doc.to_bytes(incremental=True))
    assert (author.intact, author.valid, reviewer.intact, reviewer.valid) == (True, True, True, True)
    assert (author.docmdp_ok, reviewer.docmdp_ok) == (True, False)

    doc.history.undo()

    def discard_rotation() -> None:
        with doc.transaction():
            doc.pages[0].rotation = 90
            permissions = doc.changes(incremental=True).permissions
            [entry] = [e for e in permissions if e.transform == "DocMDP P=2"]
            assert all(e.permitted for e in permissions if e.transform.startswith("FieldMDP"))
            assert (entry.edit, entry.permitted, entry.object) == ("document change", False, doc.pages[0].obj.xref)
            assert entry.reason
            msg = "discard"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="discard"):
        discard_rotation()
    assert not doc.changes(incremental=True)
    assert doc.to_bytes(incremental=True) == signed
    doc = pyropdf.open(signed)
    doc.pages[0].rotation = 90
    assert all(
        status.intact and status.valid and not status.docmdp_ok
        for status in _statuses(pki, doc.to_bytes(incremental=True))
    )


def test_permission_report_budget_refusal_keeps_edit_and_output(pki: Pki) -> None:
    original = _certify(pki, 2, pyropdf.FieldLock()).to_bytes(incremental=True)
    limit = 4_000_000
    doc = pyropdf.open(original, limits=pyropdf.DocumentLimits(max_memory_size=limit))
    doc.form["Name"].value = "Alice"
    before = doc.history.save(), doc.to_bytes(incremental=True)
    core = doc._core()  # noqa: SLF001 - exercise the enclosing operation's remaining budget.
    operation = _core.begin_operation(core, "preflight")
    try:
        operation.charge("memory_size", limit - operation.used("memory_size") - 512)
        with pytest.raises(pyropdf.LimitError) as refused:
            doc.changes(incremental=True)
        assert refused.value.code == "memory_size"
    finally:
        _core.abort_canvas(core, operation)
    assert (doc.history.save(), doc.to_bytes(incremental=True)) == before
    assert any(e.permitted is False for e in doc.changes(incremental=True).permissions)


def test_parent_field_lock_covers_descendants_and_reports_unclassified_objects(pki: Pki) -> None:
    doc = _document()
    doc.pages[0].widgets.add(TextField("Name", (72, 200, 272, 220)))
    child = doc.form["Name"].obj
    parent = doc.objects[doc.objects.add({"T": "Customer", "Kids": [child]})]
    child["Parent"] = parent
    doc.catalog["AcroForm"]["Fields"] = [parent]
    doc = pyropdf.open(doc.to_bytes())
    doc.sign(
        "Author",
        _pem_signer(pki),
        doc_mdp=pyropdf.DocMdpPermission.FORM_FILLING_AND_SIGNATURES,
        field_lock=pyropdf.FieldLock(pyropdf.FieldMdpAction.INCLUDE, ("Customer",)),
    )
    doc.form["Customer.Name"].value = "Ada"
    number = doc.objects.add({"Note": "unclassified object"})
    permissions = doc.changes(incremental=True).permissions
    [lock] = [e for e in permissions if e.transform == "FieldMDP Include" and e.field == "Customer.Name"]
    assert lock.permitted is False
    assert {e.transform for e in permissions if e.object == number and e.permitted is None} == {
        "DocMDP P=2",
        "FieldMDP Include",
    }
