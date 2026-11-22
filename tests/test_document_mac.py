# ruff: noqa: S106 - these passwords belong to local PDF fixtures.
"""ISO/TS 32004:2024 against the independent, locked pyHanko implementation."""

from __future__ import annotations

import io
import re
import sysconfig
from pathlib import Path
from typing import Literal

import pytest
from test_encrypted import RECIPIENT_ASSETS, _private_recipient_control, recipient

import pyropdf
from pyropdf import Encryption, MalformedPdfError, PdfError

ASSETS = Path(__file__).parent / "assets" / "encrypted" / "integrity"
GIL_BUILD = not sysconfig.get_config_var("Py_GIL_DISABLED")
if GIL_BUILD:
    # Load the timestamp oracle before other readers cache asn1crypto's CMS object identifiers.
    from test_signing import _context, _pem_signer, _timestamper, make_pki

requires_pyhanko = pytest.mark.skipif(not GIL_BUILD, reason="pyHanko enables the GIL on free-threaded builds")


@pytest.mark.spec("integrity-protection", "read", "interpret")
@pytest.mark.parametrize(
    ("filename", "location"),
    [
        ("standalone-table.pdf", "standalone"),
        ("standalone-stream.pdf", "standalone"),
        ("attached.pdf", "attached"),
    ],
)
def test_independent_password_mac(filename: str, location: Literal["standalone", "attached"]) -> None:
    with pyropdf.open(ASSETS / filename) as doc:
        assert doc.verify_integrity().status == "locked"
        doc.unlock("userpw")
        assert doc.verify_integrity() == pyropdf.IntegrityReport(status="valid", location=location, reason=None)
        assert doc.encryption_method == "Standard V5 R6 256-bit AES"
        assert len(doc.signatures) == (1 if location == "attached" else 0)


@pytest.mark.spec("integrity-protection", "read", "interpret")
def test_absent_and_invalid_mac_are_separate_from_decryption() -> None:
    with pyropdf.Document() as doc:
        assert doc.verify_integrity().status == "absent"
    with pyropdf.open(ASSETS.parent / "user-aes-256.pdf", password="userpw") as doc:
        assert doc.verify_integrity().status == "absent"
    source = (ASSETS / "standalone-table.pdf").read_bytes()
    # Change a covered comment byte without editing any PDF object or password.
    changed = source.replace(b"%PDF-2.0", b"%PDF-1.7", 1)
    assert changed != source
    with pyropdf.open(changed, password="userpw") as doc:
        assert len(doc.pages) == 1
        report = doc.verify_integrity()
        assert report.status == "invalid"
        assert report.reason is not None
        assert "digest" in report.reason
        for incremental in (True, False):
            with pytest.raises(MalformedPdfError, match="digest"):
                doc.to_bytes(incremental=incremental, encryption=Encryption.KEEP)


@pytest.mark.spec("integrity-protection", "read", "interpret")
@pytest.mark.parametrize("kind", ["salt", "range", "missing", "token", "appended", "stripped"])
def test_incomplete_or_malformed_protection_is_reported(kind: str) -> None:
    source = (ASSETS / "standalone-table.pdf").read_bytes()
    match kind:
        case "salt":
            updated = source.replace(b"/KDFSalt", b"/BadSalt")
        case "missing":
            updated = source.replace(b"/AuthCode", b"/NoAuCode")
        case "stripped":
            updated = source.replace(b"/KDFSalt", b"/BadSalt").replace(b"/AuthCode", b"/NoAuCode")
        case "token":
            updated = re.sub(rb"(/MAC <..)(.)", lambda m: m[1] + (b"F" if m[2] != b"F" else b"0"), source, count=1)
        case "range":
            updated = source.replace(b"/ByteRange [0 ", b"/ByteRange [1 ")
        case _:
            updated = source + b" "
    assert updated != source
    with pyropdf.open(updated, password="userpw") as doc:
        assert doc.verify_integrity().status == "invalid"


def _independent(data: bytes, *, rsa: bool = False, gcm: bool = False) -> str:
    from asn1crypto import keys, x509  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt import SimpleEnvelopeKeyDecrypter  # noqa: PLC0415
    from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: PLC0415

    reader = PdfFileReader(io.BytesIO(data))
    if rsa:
        credential = SimpleEnvelopeKeyDecrypter(
            x509.Certificate.load((RECIPIENT_ASSETS / "recipient-owner.cer").read_bytes()),
            keys.PrivateKeyInfo.load((RECIPIENT_ASSETS / "recipient-owner.key").read_bytes()),
        )
        result = reader.decrypt_pubkey(credential)
    else:
        result = reader.decrypt("userpw")
    assert result.mac_status is not None
    assert result.mac_status.name == "SUCCESSFUL", result.mac_failure_reason
    assert len(reader.security_handler.get_kdf_salt()) == 32
    if gcm:
        levels = {int(entry.get_object()["/ExtensionLevel"]) for entry in reader.root["/Extensions"]["/ISO_"]}
        assert {32003, 32004} <= levels
        encryption = reader.trailer_view["/Encrypt"]
        assert encryption["/V"] == 6
        assert encryption["/CF"][encryption["/StmF"]]["/CFM"] == "/AESV4"
        if not rsa:
            assert encryption["/R"] == 7
    return str(reader.trailer_view["/AuthCode"]["/MACLocation"])


@requires_pyhanko
@pytest.mark.spec("integrity-protection", "edit", "preserve")
@pytest.mark.parametrize(
    "filename", ["standalone-table.pdf", "standalone-stream.pdf", "attached.pdf", "public-key.pdf"]
)
@pytest.mark.parametrize("incremental", [True, False])
def test_update_protection_and_preserve_unaffected_bytes(filename: str, *, incremental: bool) -> None:
    source = (ASSETS / filename).read_bytes()
    rsa = filename == "public-key.pdf"
    credential = recipient("owner") if rsa else None
    password = None if rsa else "userpw"
    with pyropdf.open(source, password=password, credential=credential) as doc:
        assert doc.verify_integrity().status == "valid"
        assert doc.to_bytes(incremental=True, encryption=Encryption.KEEP) == source
        doc.pages[0].rotation = 90
        if filename == "attached.pdf" and not incremental:
            with pytest.raises(PdfError, match="incremental"):
                doc.to_bytes(encryption=Encryption.KEEP)
            return
        saved = doc.to_bytes(incremental=incremental, encryption=Encryption.KEEP)
    assert saved.startswith(source) == incremental
    source_stream = re.search(rb"3 0 obj\s*(.*?)\s*endobj", source, flags=re.DOTALL)
    assert source_stream is not None
    assert source_stream[1] in saved
    assert _independent(saved, rsa=rsa) == "/Standalone"
    with pyropdf.open(saved, password=password, credential=credential) as reopened:
        assert reopened.verify_integrity().status == "valid"
        assert reopened.pages[0].rotation == 90


@requires_pyhanko
@pytest.mark.spec("integrity-protection", "create", "interpret")
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("version", ["1.4", "2.0"])
def test_create_document_mac_on_full_save(version: str, *, stream: bool) -> None:
    with pyropdf.Document(version=version) as doc:
        doc.pages.new()
        protected = Encryption(user_password="userpw", owner_password="ownerpw", integrity=True)
        data = doc.to_bytes(encryption=protected, cross_reference="stream" if stream else "table")
        assert data.startswith(b"%PDF-2.0")
        assert _independent(data) == "/Standalone"
        assert doc.verify_integrity().status == "absent"
        assert pyropdf.probe(doc.to_bytes()).version == version
    with pyropdf.open(data, password="userpw") as doc:
        assert doc.verify_integrity().status == "valid"


@requires_pyhanko
@pytest.mark.spec("integrity-protection", "create", "interpret")
def test_create_recipient_mac() -> None:
    owner = recipient("owner")
    with pytest.raises(ValueError, match="256-bit"):
        pyropdf.RecipientEncryption(recipients={owner.certificate: None}, method="aes-128", integrity=True)
    with pyropdf.Document() as doc:
        doc.pages.new()
        protected = pyropdf.RecipientEncryption(recipients={owner.certificate: None}, integrity=True)
        data = doc.to_bytes(encryption=protected)
    assert _independent(data, rsa=True) == "/Standalone"
    with pyropdf.open(data, credential=owner) as doc:
        assert doc.verify_integrity().status == "valid"


@requires_pyhanko
@pytest.mark.spec("integrity-protection", "read", "interpret")
def test_attachment_only_recipient_permissions_do_not_imply_a_missing_mac() -> None:
    source = _private_recipient_control(attachment_only=True, implicit=False)
    with pyropdf.open(source) as doc:
        assert doc.verify_integrity().status == "absent"
        doc.unlock(recipient("owner"))
        assert doc.verify_integrity().status == "absent"


@requires_pyhanko
@pytest.mark.spec("integrity-protection", "read", "interpret", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("gcm", [False, True], ids=["AESV3", "AESV4"])
def test_recipient_mac_uses_the_named_key_after_credential_upgrade(*, incremental: bool, gcm: bool) -> None:
    from unittest.mock import patch  # noqa: PLC0415

    from pyhanko.pdf_utils import generic  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt.pdfmac import PdfMacValidationError, validate_pdf_mac  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt.pubkey import SimpleEnvelopeKeyDecrypter  # noqa: PLC0415
    from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: PLC0415

    source = _private_recipient_control(attachment_only=False, implicit=False, mac=True, gcm=gcm)
    with pyropdf.open(source, credential=recipient("editor")) as doc:
        assert doc.pages[0].obj["Contents"].read() == b"0 g 1 2 3 4 re f\n"
        assert doc.verify_integrity().status == "locked"
        doc.unlock(recipient("owner"))
        assert doc.verify_integrity().status == "valid"
        doc.pages[0].rotation = 90
        saved = doc.to_bytes(incremental=incremental, encryption=Encryption.KEEP)
    with pyropdf.open(saved, credential=recipient("owner")) as reopened:
        assert reopened.verify_integrity().status == "valid"
        assert reopened.catalog["FixtureNamed"].read() == b"Named private data"
        assert reopened.pages[0].rotation == 90

    reader = PdfFileReader(io.BytesIO(saved))
    handler = reader.security_handler
    credential = SimpleEnvelopeKeyDecrypter.load(
        str(RECIPIENT_ASSETS / "recipient-owner.key"), str(RECIPIENT_ASSETS / "recipient-owner.cer")
    )
    private = handler.get_stream_filter(generic.pdf_name("/DefaultCryptFilter"))
    assert handler.get_stream_filter().authenticate(credential).status.value > 0
    assert private.authenticate(credential).status.value > 0
    assert reader.root["/FixtureNamed"].data == b"Named private data"
    # pyHanko ordinarily selects /StmF. Only the distinct ISO/TS 32004 key verifies this token.
    with pytest.raises(PdfMacValidationError):
        validate_pdf_mac(reader)
    with patch.object(handler, "get_file_encryption_key", return_value=private.shared_key):
        validate_pdf_mac(reader)


@pytest.mark.spec("integrity-protection", "edit")
def test_disallowed_saves_refuse_before_replacing_file(tmp_path: Path) -> None:
    with pyropdf.open(ASSETS / "standalone-table.pdf", password="ownerpw") as doc:
        doc.pages[0].rotation = 90
        target = tmp_path / "existing.pdf"
        target.write_bytes(b"original file")
        with pytest.raises(PdfError, match="integrity protection"):
            doc.save(target)
        assert target.read_bytes() == b"original file"
        with pytest.raises(PdfError, match="linearization"):
            doc.save(target, encryption=Encryption.KEEP, linearize=True)
        assert target.read_bytes() == b"original file"
        with pytest.raises(ValueError, match="256-bit"):
            Encryption(method="aes-128", integrity=True)


@requires_pyhanko
@pytest.mark.spec("integrity-protection", "interpret")
@pytest.mark.parametrize("algorithm", ["sha256", "sha384", "sha512", "sha3_256", "sha3_384", "sha3_512"])
def test_all_six_published_digest_algorithms(algorithm: str) -> None:
    from pyhanko.pdf_utils import generic  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt import pdfmac  # noqa: PLC0415
    from pyhanko.pdf_utils.writer import PageObject, PdfFileWriter  # noqa: PLC0415

    writer = PdfFileWriter()
    contents = writer.add_object(generic.StreamObject(stream_data=b""))
    writer.insert_page(PageObject(contents, (0, 0, 100, 100)))
    writer.encrypt("ownerpw", "userpw", pdf_mac=True)
    data = io.BytesIO()
    pdfmac.add_standalone_mac(writer, md_algorithm=algorithm, output=data)
    with pyropdf.open(data.getvalue(), password="userpw") as doc:
        assert doc.verify_integrity().status == "valid"


@requires_pyhanko
@pytest.mark.spec("integrity-protection", "create", "edit", "preserve")
@pytest.mark.parametrize("kind", ["signature", "timestamp", "timestamped-signature"])
def test_sign_protected_revision_with_attached_mac(kind: str) -> None:
    from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: PLC0415
    from pyhanko.sign.validation import validate_pdf_signature, validate_pdf_timestamp  # noqa: PLC0415

    pki = make_pki()
    source = (ASSETS / "standalone-stream.pdf").read_bytes()
    with pyropdf.open(source, password="userpw") as doc:
        if kind == "timestamp":
            data = doc.timestamp(_timestamper(pki), field="Time")
        else:
            stamper = _timestamper(pki) if kind == "timestamped-signature" else None
            data = doc.sign("Signer", _pem_signer(pki), timestamper=stamper)
        assert data.startswith(source)
        assert doc.verify_integrity() == pyropdf.IntegrityReport(status="valid", location="attached", reason=None)
        assert not doc.needs_password
        assert _independent(data) == "/AttachedToSig"
        reader = PdfFileReader(io.BytesIO(data))
        reader.decrypt("userpw")
        [signature] = reader.embedded_signatures
        if kind == "timestamp":
            verdict = validate_pdf_timestamp(signature, _context(pki))
        else:
            verdict = validate_pdf_signature(signature, _context(pki))
        assert verdict.intact
        assert verdict.valid
        doc.pages[0].rotation = 180
        updated = doc.to_bytes(incremental=True)
        assert updated.startswith(data)
        assert _independent(updated) == "/Standalone"


@requires_pyhanko
@pytest.mark.spec("integrity-protection", "interpret")
def test_signature_bytes_have_a_separate_mac_binding() -> None:
    from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: PLC0415

    data = (ASSETS / "attached.pdf").read_bytes()
    reader = PdfFileReader(io.BytesIO(data))
    reader.decrypt("userpw")
    [signature] = reader.embedded_signatures
    encoded = signature.signer_info["signature"].native.hex().upper().encode("ascii")
    start = data.upper().index(encoded)
    damaged = data[:start] + (b"0" if data[start] != ord("0") else b"1") + data[start + 1 :]
    with pyropdf.open(damaged, password="userpw") as doc:
        report = doc.verify_integrity()
        assert report.status == "invalid"
        assert report.reason is not None
        assert "signature digest" in report.reason


@requires_pyhanko
@pytest.mark.spec("integrity-protection", "create", "edit", "preserve")
def test_packed_renumbered_creation_rekey_and_revision_reports() -> None:
    protected = Encryption(user_password="userpw", owner_password="ownerpw", integrity=True)
    with pyropdf.Document() as source:
        source.pages.new()
        data = source.to_bytes(encryption=protected, object_streams=True, renumber=True, compress=True)
    assert _independent(data) == "/Standalone"
    with pyropdf.open(data, password="ownerpw") as doc:
        doc.pages[0].rotation = 90
        updated = doc.to_bytes(incremental=True)
        rekeyed = doc.to_bytes(encryption=protected)
    assert _independent(updated) == "/Standalone"
    assert _independent(rekeyed) == "/Standalone"
    with pyropdf.open(updated, password="ownerpw") as doc:
        for revision in doc.revisions:
            with revision.open() as historical:
                assert historical.verify_integrity().status == "valid"
    with pyropdf.open(rekeyed, password="userpw") as doc:
        assert doc.pages[0].rotation == 90


@pytest.mark.spec("integrity-protection", "edit")
def test_reject_removal_of_catalog_declaration() -> None:
    with pyropdf.open(ASSETS / "standalone-table.pdf", password="ownerpw") as doc:
        del doc.objects[1]["Extensions"]
        for incremental in (False, True):
            with pytest.raises(MalformedPdfError, match="catalog extension"):
                doc.to_bytes(incremental=incremental, encryption=Encryption.KEEP)


@requires_pyhanko
@pytest.mark.spec("integrity-protection", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.parametrize("producer", ["pyhanko", "password", "recipient"])
def test_document_mac_and_aes_gcm(producer: str) -> None:
    from pyhanko.pdf_utils import generic  # noqa: PLC0415
    from pyhanko.pdf_utils.writer import PageObject, PdfFileWriter  # noqa: PLC0415

    owner = recipient("owner") if producer == "recipient" else None
    if producer == "pyhanko":
        writer = PdfFileWriter()
        contents = writer.add_object(generic.StreamObject(stream_data=b"q Q"))
        writer.insert_page(PageObject(contents, (0, 0, 100, 100)))
        writer.encrypt("ownerpw", "userpw", use_gcm=True, pdf_mac=True)
        output = io.BytesIO()
        writer.write(output)
        data = output.getvalue()
    else:
        options = (
            Encryption(user_password="userpw", owner_password="ownerpw", method="aes-gcm", integrity=True)
            if owner is None
            else pyropdf.RecipientEncryption(recipients={owner.certificate: None}, method="aes-gcm", integrity=True)
        )
        with pyropdf.Document() as doc:
            doc.pages.new()
            data = doc.to_bytes(encryption=options)
    password = "userpw" if owner is None else None
    with pyropdf.open(data, password=password, credential=owner) as doc:
        assert doc.verify_integrity().status == "valid"
        doc.pages[0].rotation = 90
        appended = doc.to_bytes(incremental=True)
        rewritten = doc.to_bytes(encryption=Encryption.KEEP)
    assert appended.startswith(data)
    with pyropdf.open(appended, password=password, credential=owner) as doc:
        signed = doc.sign("Signer", _pem_signer(make_pki()))
    assert signed.startswith(appended)
    for revision, location in (
        (data, "/Standalone"),
        (rewritten, "/Standalone"),
        (appended, "/Standalone"),
        (signed, "/AttachedToSig"),
    ):
        assert _independent(revision, rsa=owner is not None, gcm=True) == location


@requires_pyhanko
@pytest.mark.spec("integrity-protection", "create", "edit", "preserve")
@pytest.mark.parametrize("packed", [False, True])
def test_every_hybrid_revision_has_its_own_mac(*, packed: bool) -> None:

    protected = Encryption(user_password="userpw", owner_password="ownerpw", integrity=True)
    with pyropdf.Document() as doc:
        doc.pages.new()
        original = doc.to_bytes(encryption=protected, cross_reference="hybrid", object_streams=packed)
    first_end = original.index(b"\n%%EOF\n") + len(b"\n%%EOF\n")
    assert _independent(original[:first_end]) == "/Standalone"
    assert _independent(original) == "/Standalone"
    with pyropdf.open(original, password="ownerpw") as doc:
        assert len(doc.revisions) == 2
        for revision in doc.revisions:
            with revision.open() as historical:
                assert historical.verify_integrity().status == "valid"
        doc.pages[0].rotation = 90
        updated = doc.to_bytes(incremental=True)
        assert updated.startswith(original)
        assert _independent(updated) == "/Standalone"
    with pyropdf.open(updated, password="userpw") as doc:
        signed = doc.sign("Signer", _pem_signer(make_pki()))
        assert signed.startswith(updated)
        assert _independent(signed) == "/AttachedToSig"
        assert doc.verify_integrity().status == "valid"
