"""Incremental file revisions as documents with per-revision object changes (#818)."""

# ruff: noqa: S106 - test passwords are fixture literals.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import pyropdf
from pyropdf import (
    Access,
    DocumentClosedError,
    DocumentLimits,
    EncryptedDocumentError,
    Encryption,
    LimitError,
    MalformedPdfError,
    Note,
    Permissions,
    Revision,
    Signer,
)

ASSETS = Path(__file__).parent / "assets" / "real_world"


def _make_signer() -> tuple[Signer, bytes]:
    """Build a local ECDSA P-256 signer and DER root certificate."""
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pyropdf revisions test CA")])
    root = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    signer_key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pyropdf revisions signer")]))
        .issuer_name(ca_name)
        .public_key(signer_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key_pem = signer_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    chain_pem = cert.public_bytes(serialization.Encoding.PEM) + root.public_bytes(serialization.Encoding.PEM)
    return Signer.from_pem(key_pem, chain_pem), root.public_bytes(serialization.Encoding.DER)


def test_revision_public_export_and_detached_record_invariants() -> None:
    assert "Revision" in pyropdf.__all__
    assert Revision.__module__ == "pyropdf"

    detached = Revision(index=0, end=128, added=(3, 1), updated=(2,), freed=(4,))
    assert detached.objects == (1, 2, 3, 4)
    with pytest.raises(ValueError, match="not bound"):
        detached.open()


def test_empty_and_repaired_documents_have_no_revisions() -> None:
    with pyropdf.Document() as doc:
        assert doc.revisions == ()
        doc.pages.new((200, 200))
        assert doc.revisions == ()

    raw = bytearray((ASSETS / "pdf20-simple.pdf").read_bytes())
    marker = raw.rfind(b"startxref")
    assert marker != -1
    # Point startxref at offset 1 so opening repairs the index.
    broken = bytes(raw[:marker]) + b"startxref\n1\n%%EOF\n"
    with pyropdf.open(broken) as repaired:
        assert repaired.is_repaired
        assert repaired.revisions == ()


@pytest.mark.spec("incremental-update", "read", "interpret", "preserve")
def test_corpus_single_linearized_and_updated_revisions() -> None:
    simple_bytes = (ASSETS / "pdf20-simple.pdf").read_bytes()
    with pyropdf.open(simple_bytes) as doc:
        (rev,) = doc.revisions
        assert rev.index == 0
        assert rev.end == len(simple_bytes)
        assert rev.updated == ()
        assert rev.freed == ()
        assert rev.added == tuple(sorted(doc.objects))
        assert rev.objects == rev.added

    manuscript_bytes = (ASSETS / "wdl6812-manuscript.pdf").read_bytes()
    with pyropdf.open(manuscript_bytes) as doc:
        assert doc.is_linearized
        (rev,) = doc.revisions
        assert rev.index == 0
        assert rev.end == len(manuscript_bytes)
        assert rev.updated == ()
        assert rev.freed == ()

    f1040_bytes = (ASSETS / "f1040.pdf").read_bytes()
    with pyropdf.open(f1040_bytes) as doc:
        assert not doc.is_linearized
        rev0, rev1 = doc.revisions
        assert (rev0.index, rev0.end) == (0, 206_144)
        assert (rev1.index, rev1.end) == (1, 220_237)
        assert rev0.updated == ()
        assert rev0.freed == ()
        assert rev1.objects != ()
        with rev0.open() as first_rev:
            assert first_rev.is_linearized
            assert len(first_rev.revisions) == 1
            assert first_rev.to_bytes(incremental=True) == f1040_bytes[: rev0.end]
            assert len(first_rev.pages) == len(doc.pages)

    patent_bytes = (ASSETS / "patent-us223898.pdf").read_bytes()
    with pyropdf.open(patent_bytes) as doc:
        rev0, rev1 = doc.revisions
        assert (rev0.index, rev0.end) == (0, 232_285)
        assert (rev1.index, rev1.end) == (1, 321_048)
        with rev0.open() as earlier:
            assert earlier.to_bytes(incremental=True) == patent_bytes[: rev0.end]
            assert len(earlier.pages) >= 1


def _build_three_revision_pdf(
    mode: Literal["table", "stream"],
) -> tuple[bytes, bytes, bytes, int, tuple[int, ...]]:
    """Build a 3-revision PDF with added, updated, and freed objects."""
    with pyropdf.Document() as builder:
        page = builder.pages.new((200, 120))
        with page.draw() as canvas:
            canvas.text("Revision zero", (20, 60), size=14)
        builder.attachments.add("memo.txt", b"keep initially")
        b0 = builder.to_bytes(
            cross_reference=mode,
            object_streams=(mode == "stream"),
        )

    with pyropdf.open(b0) as step1:
        page0_xref = step1.pages[0].obj.xref
        assert page0_xref is not None
        step1.pages[0].rotation = 90
        step1.pages[0].annotations.add(Note((30, 30), contents="Added in rev 1"))
        page2 = step1.pages.new((200, 120))
        with page2.draw() as canvas:
            canvas.text("Second page", (20, 60), size=14)
        step1_objects = set(step1.objects)
        b1 = step1.to_bytes(incremental=True)

    with pyropdf.open(b1) as step2:
        del step2.attachments["memo.txt"]
        del step2.pages[1]
        step2.pages[0].rotation = 180
        step2_objects = set(step2.objects)
        b2 = step2.to_bytes(incremental=True)

    expected_freed = tuple(sorted(step1_objects - step2_objects))
    assert expected_freed != ()
    return b0, b1, b2, page0_xref, expected_freed


@pytest.mark.spec("incremental-update", "read", "interpret", "preserve")
@pytest.mark.parametrize("mode", ["table", "stream"])
def test_multi_revision_edits_track_added_updated_freed_and_open_independently(
    mode: Literal["table", "stream"],
) -> None:
    b0, b1, b2, page0_xref, expected_freed = _build_three_revision_pdf(mode)

    with pyropdf.open(b2) as doc:
        assert len(doc.revisions) == 3
        rev0, rev1, rev2 = doc.revisions

        assert [r.index for r in doc.revisions] == [0, 1, 2]
        assert [r.end for r in doc.revisions] == [len(b0), len(b1), len(b2)]

        assert rev0.updated == ()
        assert rev0.freed == ()
        assert page0_xref in rev0.added

        assert page0_xref in rev1.updated
        assert rev1.added != ()
        assert rev1.freed == ()

        assert page0_xref in rev2.updated
        assert rev2.freed == expected_freed
        assert 0 not in rev2.freed
        assert rev2.objects == tuple(sorted({*rev2.added, *rev2.updated, *rev2.freed}))

        with rev0.open() as d0:
            assert len(d0.pages) == 1
            assert d0.pages[0].rotation == 0
            assert "Revision zero" in d0.pages[0].text()
            assert len(d0.pages[0].annotations) == 0
            assert d0.attachments["memo.txt"].read() == b"keep initially"
            pix0 = d0.pages[0].pixmap(dpi=72)
            assert (pix0.width, pix0.height) == (200, 120)
            assert d0.to_bytes(incremental=True) == b0

            d0.pages[0].rotation = 270
            branched = d0.to_bytes(incremental=True)
            assert branched.startswith(b0)

        with pyropdf.open(branched) as branched_doc:
            assert len(branched_doc.revisions) == 2
            assert branched_doc.pages[0].rotation == 270

        with rev1.open() as d1:
            assert len(d1.pages) == 2
            assert d1.pages[0].rotation == 90
            assert "Second page" in d1.pages[1].text()
            assert len(d1.pages[0].annotations) == 1
            pix1 = d1.pages[0].pixmap(dpi=72)
            assert (pix1.width, pix1.height) == (120, 200)
            assert d1.to_bytes(incremental=True) == b1

        with rev2.open() as d2:
            assert len(d2.pages) == 1
            assert d2.pages[0].rotation == 180
            assert "memo.txt" not in d2.attachments
            for freed_xref in expected_freed:
                assert freed_xref not in d2.objects
            assert d2.to_bytes(incremental=True) == b2


def test_cumulative_xref_and_missing_eof_marker_are_handled_cleanly() -> None:
    out = bytearray(b"%PDF-1.7\n")
    o1 = len(out)
    out += b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    o2 = len(out)
    out += b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    o3 = len(out)
    out += b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>\nendobj\n"
    xref0 = len(out)
    # Intentionally omit %%EOF on revision 0.
    out += (
        f"xref\n0 4\n0000000000 65535 f \n{o1:010} 00000 n \n{o2:010} 00000 n \n{o3:010} 00000 n \n"
        f"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n{xref0}\n"
    ).encode()

    o3_v2 = len(out)
    out += b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Rotate 90 >>\nendobj\n"
    xref1 = len(out)
    # Cumulative table repeating unchanged objects 1 and 2 alongside updated object 3.
    out += (
        f"xref\n0 4\n0000000000 65535 f \n{o1:010} 00000 n \n{o2:010} 00000 n \n{o3_v2:010} 00000 n \n"
        f"trailer\n<< /Size 4 /Root 1 0 R /Prev {xref0} >>\nstartxref\n{xref1}\n%%EOF\n"
    ).encode()

    with pyropdf.open(bytes(out)) as doc:
        rev0, rev1 = doc.revisions
        assert rev0 == Revision(index=0, end=None, added=(1, 2, 3), updated=(), freed=())
        assert rev1 == Revision(index=1, end=len(out), added=(), updated=(3,), freed=())

        with rev0.open() as earlier:
            assert not earlier.is_repaired
            assert earlier.pages[0].rotation == 0
        with rev1.open() as latest:
            assert latest.pages[0].rotation == 90


def test_unsaved_in_memory_edits_do_not_alter_revisions() -> None:
    with pyropdf.open(ASSETS / "pdf20-simple.pdf") as doc:
        before = doc.revisions
        doc.pages[0].rotation = 90
        doc.pages.new((100, 100))
        assert doc.revisions == before
        with doc.revisions[-1].open() as opened_rev:
            assert len(opened_rev.pages) == 1
            assert opened_rev.pages[0].rotation == 0


@pytest.mark.spec("incremental-update", "read", "interpret", "preserve")
def test_signed_revision_opens_exact_signed_state_and_shows_later_changes() -> None:
    signer, root_der = _make_signer()
    with pyropdf.open(ASSETS / "pdf20-simple.pdf") as doc:
        initial_count = len(doc.revisions)
        signed_bytes = doc.sign("Approval", signer, reason="Approved")
        assert len(doc.revisions) == initial_count + 1
        (live_sig,) = doc.signatures
        assert live_sig.revision == len(doc.revisions) - 1

    with pyropdf.open(signed_bytes) as to_modify:
        note = to_modify.pages[0].annotations.add(Note((40, 40), contents="Post-signature comment"))
        note_xref = note.obj.xref
        modified_bytes = to_modify.to_bytes(incremental=True)

    with pyropdf.open(modified_bytes) as doc:
        (sig,) = doc.signatures
        assert sig.revision == 1
        assert len(doc.revisions) == 3

        check_latest = sig.verify(trust=[root_der])
        assert check_latest.intact
        assert check_latest.valid is True
        assert check_latest.trusted is True
        assert check_latest.modifications == "other"

        later = doc.revisions[sig.revision + 1 :]
        assert len(later) == 1
        assert note_xref in later[0].added

        with doc.revisions[sig.revision].open() as signed_doc:
            assert len(signed_doc.pages[0].annotations) == 0
            assert signed_doc.to_bytes(incremental=True) == signed_bytes
            (signed_only,) = signed_doc.signatures
            check_signed = signed_only.verify(trust=[root_der])
            assert check_signed.intact
            assert check_signed.valid is True
            assert check_signed.trusted is True
            assert check_signed.modifications == "none"


def test_encrypted_revisions_preserve_lock_state_and_access() -> None:
    with pyropdf.Document() as builder:
        page = builder.pages.new((200, 100))
        with page.draw() as canvas:
            canvas.text("Secret rev 0", (20, 50), size=12)
        enc0 = builder.to_bytes(
            encryption=Encryption(
                user_password="reader",
                owner_password="owner",
                permissions=Permissions.PRINT,
            )
        )

    with pyropdf.open(enc0, password="owner") as editor:
        editor.pages[0].rotation = 90
        enc1 = editor.to_bytes(incremental=True)

    with pyropdf.open(enc1) as locked:
        assert locked.needs_password
        assert len(locked.revisions) == 2
        with locked.revisions[0].open() as rev0_locked:
            assert rev0_locked.needs_password
            assert rev0_locked.access is None
            with pytest.raises(EncryptedDocumentError):
                _ = rev0_locked.objects
            assert rev0_locked.unlock("reader") == Access.USER
            assert not rev0_locked.needs_password
            assert rev0_locked.pages[0].rotation == 0
            assert "Secret rev 0" in rev0_locked.pages[0].text()

    with pyropdf.open(enc1, password="reader") as user_doc:
        assert user_doc.access == Access.USER
        with user_doc.revisions[0].open() as rev0_user:
            assert not rev0_user.needs_password
            assert rev0_user.access == Access.USER
            assert rev0_user.permissions == user_doc.permissions
            assert "Secret rev 0" in rev0_user.pages[0].text()

        assert user_doc.unlock("owner") == Access.OWNER
        with user_doc.revisions[0].open() as rev0_upgraded:
            assert rev0_upgraded.access == Access.OWNER
            assert rev0_upgraded.permissions == Permissions.ALL

    with pyropdf.open(enc1, password="owner") as owner_doc, owner_doc.revisions[1].open() as rev1_owner:
        assert rev1_owner.access == Access.OWNER
        assert rev1_owner.pages[0].rotation == 90


def test_parent_close_and_fallback_fonts_behavior() -> None:
    with pyropdf.Document() as builder:
        page = builder.pages.new((150, 100))
        with page.draw() as canvas:
            canvas.text("Independent lifetime", (10, 50), size=12)
        b0 = builder.to_bytes()

    with pyropdf.open(b0) as step:
        step.pages[0].rotation = 90
        b1 = step.to_bytes(incremental=True)

    parent = pyropdf.open(b1)
    parent.fallback_fonts = {}
    rev0, rev1 = parent.revisions
    opened_before_close = rev0.open()
    assert dict(opened_before_close.fallback_fonts) == {}

    parent.close()
    with pytest.raises(DocumentClosedError):
        _ = parent.revisions
    with pytest.raises(DocumentClosedError):
        rev1.open()

    try:
        assert opened_before_close.pages[0].rotation == 0
        assert "Independent lifetime" in opened_before_close.pages[0].text()
        assert opened_before_close.to_bytes(incremental=True) == b0
    finally:
        opened_before_close.close()


def test_revision_open_enforces_document_limits() -> None:
    raw = bytearray(b"%PDF-1.7\n")
    o1 = len(raw)
    raw += b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    o2 = len(raw)
    raw += b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    o3 = len(raw)
    raw += b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>\nendobj\n"
    o4 = len(raw)
    # Malformed dictionary body in unused object 4: lazy open succeeds, web() check_at_open refuses.
    raw += b"4 0 obj\n<< /Broken\nendobj\n"
    xref0 = len(raw)
    raw += (
        f"xref\n0 5\n0000000000 65535 f \n{o1:010} 00000 n \n{o2:010} 00000 n \n"
        f"{o3:010} 00000 n \n{o4:010} 00000 n \n"
        f"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n{xref0}\n%%EOF\n"
    ).encode()

    o4_fixed = len(raw)
    raw += b"4 0 obj\n<< /Fixed true >>\nendobj\n"
    xref1 = len(raw)
    raw += (
        f"xref\n4 1\n{o4_fixed:010} 00000 n \n"
        f"trailer\n<< /Size 5 /Root 1 0 R /Prev {xref0} >>\nstartxref\n{xref1}\n%%EOF\n"
    ).encode()

    custom_limits = DocumentLimits(max_pages=50)
    with pyropdf.open(bytes(raw), limits=custom_limits) as doc:
        rev0, rev1 = doc.revisions

        bad_limits: Any = "invalid"
        with pytest.raises(TypeError, match="DocumentLimits"):
            rev0.open(limits=bad_limits)

        with pytest.raises(LimitError) as file_exc:
            rev0.open(limits=DocumentLimits(max_file_size=16))
        assert file_exc.value.code == "file_size"

        with pytest.raises(LimitError) as obj_exc:
            rev0.open(limits=DocumentLimits(max_objects=2))
        assert obj_exc.value.code == "object_count"

        with rev0.open() as lazy_rev0:
            assert lazy_rev0.limits == custom_limits

        with pytest.raises(MalformedPdfError):
            rev0.open(limits=DocumentLimits.web())

        with rev1.open(limits=DocumentLimits.web()) as checked_rev1:
            assert checked_rev1.limits == DocumentLimits.web()
            assert checked_rev1.objects[4]["Fixed"] is True
