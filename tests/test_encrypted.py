# ruff: noqa: S106 - the fixtures' passwords are test data.
"""Passwords, permissions, and encryption on save, on pylopdf's encrypted fixtures.

The cases of pylopdf's tests/test_encrypted.py at 884af0db, ported. Text
extraction comes in Phase 1, so content is checked by rendering a decrypted
save: every fixture holds the same two pages. Intended differences:

- An incremental save without ``encryption`` keeps the document's encryption,
  since an update cannot decrypt the file it appends to.
- ``object_streams``, page selection, and object handles come later.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Literal

import pikepdf
import pytest
from test_pages import build_raw_pdf, numbered

import pyropdf
from pyropdf import Access, Encryption, Permissions

ASSETS = Path(__file__).parent / "assets" / "encrypted"

#: Every standard security handler revision, by the fixture that uses it.
HANDLERS = {
    "user-rc4-40.pdf": "Standard V1 R2 40-bit RC4",
    "user-rc4-128.pdf": "Standard V2 R3 128-bit RC4",
    "user-r4-rc4-128.pdf": "Standard V4 R4 128-bit RC4",
    "user-aes-128.pdf": "Standard V4 R4 128-bit AES",
    "user-aes-128-plain-metadata.pdf": "Standard V4 R4 128-bit AES",
    "user-aes-256-r5.pdf": "Standard V5 R5 256-bit AES",
    "user-aes-256.pdf": "Standard V5 R6 256-bit AES",
    "user-aes-256-object-streams.pdf": "Standard V5 R6 256-bit AES",
}
USER = pytest.mark.parametrize("name", list(HANDLERS))


def rendered(doc: pyropdf.Document) -> list[bytes]:
    """Return the pixels of every page of ``doc``'s decrypted save."""
    with pyropdf.open(doc.to_bytes()) as plain:
        assert not plain.needs_password
        return [bytes(memoryview(page.pixmap(dpi=36))) for page in plain.pages]


@pytest.fixture(scope="module")
def reference() -> list[bytes]:
    """Return the pages that every fixture holds, rendered."""
    pixels = rendered(pyropdf.open(ASSETS / "owneronly-aes-256.pdf"))
    assert len(pixels) == 2
    assert pixels[0] != pixels[1]
    assert all(page != b"\xff" * len(page) for page in pixels)
    return pixels


@USER
def test_open_without_password_raises_on_use(name: str) -> None:
    doc = pyropdf.open(ASSETS / name)
    assert doc.needs_password
    assert doc.access is None
    assert doc.permissions == Permissions(0)
    with pytest.raises(pyropdf.EncryptedDocumentError, match=r"call unlock\(\)"):
        _ = len(doc.pages)
    with pytest.raises(pyropdf.EncryptedDocumentError):
        doc.to_bytes()


@pytest.mark.spec("crypt-filters", "read")
@pytest.mark.spec("encryption-dictionary", "read")
@pytest.mark.spec("standard-security-handler", "read")
@USER
def test_every_standard_handler_reads_with_both_passwords(name: str, reference: list[bytes]) -> None:
    for password, access in (("userpw", Access.USER), ("ownerpw", Access.OWNER)):
        with pyropdf.open(ASSETS / name, password=password) as doc:
            assert not doc.needs_password
            assert doc.access is access
            assert doc.encryption_method == HANDLERS[name]
            assert len(doc.pages) == 2
            assert rendered(doc) == reference
            if access is Access.OWNER:
                assert doc.permissions == Permissions.ALL


@USER
def test_open_with_wrong_password_raises(name: str) -> None:
    with pytest.raises(pyropdf.PasswordError, match="neither"):
        pyropdf.open(ASSETS / name, password="wrong")


@USER
def test_unlock(name: str) -> None:
    doc = pyropdf.open(ASSETS / name)
    with pytest.raises(pyropdf.PasswordError, match="neither"):
        doc.unlock("wrong")
    assert doc.needs_password
    assert doc.unlock("userpw") is Access.USER
    assert not doc.needs_password
    assert len(doc.pages) == 2
    # An unlocked document still checks the passwords of its encryption, as MuPDF does.
    with pytest.raises(pyropdf.PasswordError):
        doc.unlock("anything")
    assert doc.unlock("userpw") is Access.USER
    assert doc.unlock("ownerpw") is Access.OWNER
    assert doc.access is Access.OWNER
    assert doc.permissions == Permissions.ALL


@USER
def test_unlock_a_document_opened_from_bytes(name: str) -> None:
    doc = pyropdf.open((ASSETS / name).read_bytes())
    assert doc.unlock("ownerpw") is Access.OWNER
    assert len(doc.pages) == 2


@pytest.mark.parametrize("password", [None, "", "ownerpw", "wrong"])
def test_owner_only_opens_transparently(password: str | None, reference: list[bytes]) -> None:
    doc = pyropdf.open(ASSETS / "owneronly-aes-256.pdf", password=password)
    assert not doc.needs_password
    assert len(doc.pages) == 2
    assert rendered(doc) == reference
    # The bridge hands hayro the original, which an empty user password opens.
    assert bytes(memoryview(doc.pages[0].pixmap(dpi=36))) == reference[0]


def test_probe_reports_whether_a_password_is_needed() -> None:
    probed = pyropdf.probe(ASSETS / "user-aes-256.pdf")
    assert probed.needs_password is True
    assert probed.is_repaired is False
    assert probed.page_count == 2
    assert pyropdf.probe(ASSETS / "user-aes-256.pdf", password="userpw").needs_password is False
    with pytest.raises(pyropdf.PasswordError):
        pyropdf.probe(ASSETS / "user-aes-256.pdf", password="wrong")


@pytest.mark.spec("crypt-filters", "edit")
@pytest.mark.spec("encryption-dictionary", "edit")
@pytest.mark.spec("standard-security-handler", "edit")
def test_a_decrypted_save_produces_a_plain_pdf(reference: list[bytes]) -> None:
    doc = pyropdf.open(ASSETS / "user-aes-256.pdf", password="userpw")
    del doc.pages[1]
    data = doc.to_bytes()
    assert b"/Encrypt" not in data
    with pyropdf.open(data) as reopened:
        assert not reopened.needs_password
        assert len(reopened.pages) == 1
        assert bytes(memoryview(reopened.pages[0].pixmap(dpi=36))) == reference[0]


@pytest.mark.spec("crypt-filters", "interpret")
@pytest.mark.spec("encryption-dictionary", "interpret")
@pytest.mark.spec("standard-security-handler", "interpret")
@pytest.mark.parametrize("name", [*HANDLERS, "owneronly-aes-256.pdf"])
def test_an_encrypted_document_renders_unedited_and_edited(name: str) -> None:
    password = "" if name.startswith("owneronly") else "userpw"
    doc = pyropdf.open(ASSETS / name, password=password)
    pixels = [bytes(memoryview(page.pixmap(dpi=36))) for page in doc.pages]
    assert pixels == rendered(doc)
    doc.pages[0].rotation = 90
    edited = doc.pages[0].pixmap(dpi=36)
    with pyropdf.open(doc.to_bytes()) as plain:
        expected = plain.pages[0].pixmap(dpi=36)
        assert (edited.width, edited.height) == (expected.width, expected.height)
        assert edited.width > edited.height
        assert bytes(memoryview(edited)) == bytes(memoryview(expected))


def test_a_stream_with_an_indirect_length_decrypts_the_data_that_its_length_gives() -> None:
    # generate.py ends the encrypted data of each content stream in a line
    # feed, which a search for endstream would trim. RC4 keeps the length.
    with pyropdf.open(ASSETS / "user-rc4-128-indirect-length.pdf", password="userpw") as doc:
        for stream, length in ((6, 7), (8, 9)):
            data = doc.objects[stream].read(decode=False)
            assert len(data) == doc.objects[length]
            assert data.startswith(b"BT /F1 24 Tf 72 720 Td (Encrypted page ")


@pytest.mark.spec("standard-security-handler", "preserve")
def test_keep_copies_the_encryption() -> None:
    doc = pyropdf.open(ASSETS / "user-aes-256.pdf", password="userpw")
    doc.pages[0].rotation = 90
    for data in (doc.to_bytes(encryption=Encryption.KEEP), doc.to_bytes(incremental=True)):
        locked = pyropdf.open(data)
        assert locked.needs_password
        assert locked.unlock("ownerpw") is Access.OWNER
        assert locked.pages[0].rotation == 90
    assert repr(Encryption.KEEP) == "Encryption.KEEP"
    with pytest.raises(ValueError, match="incremental save keeps"):
        doc.to_bytes(incremental=True, encryption=Encryption(user_password="x"))


def test_an_unencrypted_document_grants_owner_access() -> None:
    doc = pyropdf.open(numbered(1))
    assert not doc.needs_password
    assert doc.access is Access.OWNER
    assert doc.permissions == Permissions.ALL
    assert doc.encryption_method is None
    assert doc.unlock("whatever") is Access.OWNER


@pytest.mark.spec("crypt-filters", "create")
@pytest.mark.spec("encryption-dictionary", "create")
@pytest.mark.spec("standard-security-handler", "create")
@pytest.mark.parametrize("method", ["aes-256", "aes-128"])
def test_save_encrypted_roundtrip(method: str) -> None:
    doc = pyropdf.open(numbered(3))
    data = doc.to_bytes(encryption=Encryption(user_password="secret", owner_password="boss", method=method))  # ty: ignore[invalid-argument-type]
    # The original in-memory document remains plaintext.
    assert not doc.needs_password
    assert len(doc.pages) == 3
    locked = pyropdf.open(data)
    assert locked.needs_password
    assert locked.unlock("secret") is Access.USER
    assert len(locked.pages) == 3
    assert locked.encryption_method == (
        "Standard V5 R6 256-bit AES" if method == "aes-256" else "Standard V4 R4 128-bit AES"
    )
    assert [page.rect for page in locked.pages] == [page.rect for page in doc.pages]
    assert pyropdf.open(data, password="boss").access is Access.OWNER


def test_to_bytes_bounds_encrypted_output() -> None:
    doc = pyropdf.open(numbered(3))
    with pytest.raises(pyropdf.LimitError) as caught:
        doc.to_bytes(encryption=Encryption(user_password="secret"), max_size=16)
    assert caught.value.code == "pdf_output_size"
    assert not doc.needs_password


def test_save_encrypted_file_and_password_arg(tmp_path: Path) -> None:
    out = tmp_path / "locked.pdf"
    # The owner password defaults to the user password.
    pyropdf.open(numbered(3)).save(out, encryption=Encryption(user_password="secret"))
    opened = pyropdf.open(out, password="secret")
    assert len(opened.pages) == 3
    assert opened.access is Access.OWNER
    with pytest.raises(pyropdf.PasswordError):
        pyropdf.open(out, password="wrong")


def test_save_encrypted_owner_only_restricts() -> None:
    data = pyropdf.open(numbered(3)).to_bytes(
        encryption=Encryption(user_password="", owner_password="boss", permissions=Permissions.PRINT)
    )
    opened = pyropdf.open(data)
    assert not opened.needs_password
    assert opened.access is Access.USER
    assert opened.permissions == Permissions.PRINT
    assert len(opened.pages) == 3
    # The empty user password lets the bridge's hayro read it.
    assert opened.pages[0].pixmap().width == 100


def test_encryption_without_passwords_encrypts_with_empty_passwords() -> None:
    data = pyropdf.open(numbered(3)).to_bytes(encryption=Encryption())
    assert b"/Encrypt" in data
    reopened = pyropdf.open(data)
    assert not reopened.needs_password
    assert len(reopened.pages) == 3


def test_the_owner_password_defaults_to_the_user_password() -> None:
    encryption = Encryption(user_password="pw", permissions=Permissions.PRINT)
    data = pyropdf.open(numbered(3)).to_bytes(encryption=encryption)
    assert pyropdf.open(data, password="pw").permissions == Permissions.ALL


def test_saving_writes_no_encryption_by_default() -> None:
    assert b"/Encrypt" not in pyropdf.open(ASSETS / "user-aes-256.pdf", password="userpw").to_bytes()


def test_permissions_flags() -> None:
    perms = Permissions.PRINT | Permissions.COPY
    assert int(perms) == (1 << 2) | (1 << 4)
    assert Permissions.MODIFY in Permissions.ALL


def test_password_input_boundary_is_exact() -> None:
    password = "é" * 63 + "a"
    data = numbered(1)
    assert len(pyropdf.open(data, password=password).pages) == 1
    assert pyropdf.probe(data, password=password).page_count == 1
    encrypted = pyropdf.open(data).to_bytes(encryption=Encryption(user_password=password))
    assert len(pyropdf.open(encrypted, password=password).pages) == 1


def test_password_input_is_bounded_before_open_unlock_and_probe() -> None:
    password = "é" * 64
    data = numbered(1)
    for call in (lambda: pyropdf.open(data, password=password), lambda: pyropdf.probe(data, password=password)):
        with pytest.raises(pyropdf.LimitError) as caught:
            call()
        assert caught.value.code == "password_input_size"
    locked = pyropdf.open(ASSETS / "user-aes-256.pdf")
    with pytest.raises(pyropdf.LimitError) as unlock_limit:
        locked.unlock(password)
    assert unlock_limit.value.code == "password_input_size"
    assert locked.needs_password


@pytest.mark.parametrize("field", ["user_password", "owner_password"])
def test_encryption_bounds_password_input(field: str) -> None:
    with pytest.raises(pyropdf.LimitError) as caught:
        Encryption(**{field: "é" * 64})  # ty: ignore[invalid-argument-type]
    assert caught.value.code == "password_input_size"


@pytest.mark.parametrize(
    ("arguments", "error", "message"),
    [
        ({"user_password": b"x"}, TypeError, "user_password must be a str"),
        ({"owner_password": 1}, TypeError, "owner_password must be a str"),
        ({"permissions": "all"}, TypeError, "permissions must be Permissions flags"),
        ({"permissions": True}, TypeError, "permissions must be Permissions flags"),
        ({"method": "rc4-128"}, ValueError, "does not write rc4-128 encryption"),
        ({"method": "RC4-40"}, ValueError, "does not write RC4-40 encryption"),
        ({"method": "aes"}, ValueError, "method must be 'aes-256' or 'aes-128'"),
        ({"method": "keep", "user_password": "u"}, ValueError, "Encryption.KEEP takes the passwords"),
    ],
)
def test_encryption_validates_its_fields(arguments: dict[str, object], error: type[Exception], message: str) -> None:
    with pytest.raises(error, match=message):
        Encryption(**arguments)  # ty: ignore[invalid-argument-type]


def test_encryption_keeps_the_defined_permission_flags() -> None:
    assert Encryption(permissions=-1).permissions == Permissions.ALL  # ty: ignore[invalid-argument-type]
    assert Encryption(permissions=4095).permissions == Permissions.ALL  # ty: ignore[invalid-argument-type]
    assert type(Encryption(permissions=4).permissions) is Permissions  # ty: ignore[invalid-argument-type]
    assert "secret" not in repr(Encryption(user_password="secret"))


@pytest.mark.parametrize("value", [True, 5, "aes-256"])
def test_saving_refuses_other_encryption_values(value: object, tmp_path: Path) -> None:
    doc = pyropdf.open(numbered(3))
    before = doc.to_bytes()
    with pytest.raises(TypeError, match="encryption must be an Encryption or RecipientEncryption or None"):
        doc.to_bytes(encryption=value)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="encryption must be an Encryption or RecipientEncryption or None"):
        doc.save(tmp_path / "out.pdf", encryption=value)  # ty: ignore[invalid-argument-type]
    assert not (tmp_path / "out.pdf").exists()
    assert doc.to_bytes() == before


def test_unlocking_an_unlocked_document_keeps_the_history() -> None:
    doc = pyropdf.open(ASSETS / "user-aes-256.pdf")
    doc.unlock("userpw")
    with doc.transaction("rotate"):
        doc.pages[0].rotation = 90
    assert doc.history.steps == ("rotate",)
    doc.unlock("ownerpw")
    assert doc.history.steps == ("rotate",)


RECIPIENT_ASSETS = ASSETS / "recipients"


def recipient(label: str) -> pyropdf.RecipientCredential:
    """Load the published fixture credential, whose key is used only by these tests."""
    from cryptography import x509  # noqa: PLC0415
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: PLC0415

    key = serialization.load_der_private_key((RECIPIENT_ASSETS / f"recipient-{label}.key").read_bytes(), None)
    assert isinstance(key, rsa.RSAPrivateKey)
    cert = x509.load_der_x509_certificate((RECIPIENT_ASSETS / f"recipient-{label}.cer").read_bytes())
    return pyropdf.RecipientCredential(certificate=cert, private_key=key)


RECIPIENT_STREAM = b"0 g 2 3 12 8 re f\n"
RECIPIENT_STRING = b"recipient\x00value\xff"


@pytest.mark.spec("public-key-security-handler", "read")
@pytest.mark.parametrize(
    ("label", "access", "flags"),
    [("editor", Access.USER, Permissions.MODIFY), ("owner", Access.OWNER, Permissions.ALL)],
)
def test_public_key_independent_envelopes(label: str, access: Access, flags: Permissions) -> None:
    source = RECIPIENT_ASSETS / "pyhanko-recipients.pdf"
    credential = recipient(label)
    doc = pyropdf.open(source)
    assert doc.needs_password
    with pytest.raises(pyropdf.EncryptedDocumentError):
        _ = doc.objects[3]
    with pytest.raises(pyropdf.PasswordError):
        doc.unlock("unrelated")
    assert doc.needs_password
    assert doc.unlock(credential) is access
    assert doc.permissions == flags
    assert doc.objects[3].read() == RECIPIENT_STREAM
    assert doc.objects[4]["FixtureValue"] == RECIPIENT_STRING
    assert doc.encryption_method == "Adobe.PubSec adbe.pkcs7.s5 V5 256-bit AES"
    assert not pyropdf.probe(source, credential=credential).needs_password


@pytest.mark.spec("public-key-security-handler", "interpret")
def test_public_key_render_uses_authenticated_version() -> None:
    with (
        pyropdf.open(RECIPIENT_ASSETS / "pyhanko-recipients.pdf", credential=recipient("owner")) as doc,
        pyropdf.open(doc.to_bytes()) as clear,
    ):
        assert doc.pages[0].pixmap().to_bytes() == clear.pages[0].pixmap().to_bytes()
        doc.objects[3].write(b"1 g 0 0 50 40 re f\n", filter=None)
        assert doc.pages[0].pixmap().to_bytes() != clear.pages[0].pixmap().to_bytes()
        assert doc.objects[3].read() == b"1 g 0 0 50 40 re f\n"
        assert b"/Standard" not in doc.history.save()


@pytest.mark.spec("public-key-security-handler", "preserve")
@pytest.mark.spec("crypt-filters", "preserve")
@pytest.mark.spec("public-key-security-handler", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_public_key_edit_keeps_untouched_ciphertext(*, incremental: bool) -> None:
    source = (RECIPIENT_ASSETS / "pyhanko-recipients.pdf").read_bytes()
    untouched = source.split(b"3 0 obj\n", 1)[1].split(b"\nendobj", 1)[0]
    with pyropdf.open(source, credential=recipient("editor")) as doc:
        doc.objects[4]["FixtureValue"] = b"edited\x00string"
        saved = doc.to_bytes(incremental=incremental, encryption=Encryption.KEEP)
        assert saved.startswith(source) is incremental
        assert untouched in saved
        assert b"/Standard" not in saved
        assert b"edited\x00string" not in saved
        with pyropdf.open(saved, credential=recipient("owner")) as reopened:
            assert reopened.objects[3].read() == RECIPIENT_STREAM
            assert reopened.objects[4]["FixtureValue"] == b"edited\x00string"
            if incremental:
                with reopened.revisions[0].open() as previous:
                    assert previous.objects[4]["FixtureValue"] == RECIPIENT_STRING


@pytest.mark.spec("public-key-security-handler", "create")
@pytest.mark.parametrize("encrypt_metadata", [True, False])
@pytest.mark.parametrize("method", ["aes-256", "aes-128"])
def test_public_key_output_read_by_pyhanko(method: Literal["aes-256", "aes-128"], *, encrypt_metadata: bool) -> None:
    import sysconfig  # noqa: PLC0415

    if sysconfig.get_config_var("Py_GIL_DISABLED") == 1:
        pytest.skip("pyHanko imports enable the GIL on free-threaded Python")
    from asn1crypto import keys, x509  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt.pubkey import SimpleEnvelopeKeyDecrypter  # noqa: PLC0415
    from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: PLC0415

    editor, owner = recipient("editor"), recipient("owner")
    with pyropdf.open(RECIPIENT_ASSETS / "pyhanko-recipients.pdf", credential=owner) as doc:
        original_xmp = doc.catalog["Metadata"].read()
        out = doc.to_bytes(
            encryption=pyropdf.RecipientEncryption(
                recipients={owner.certificate: None, editor.certificate: Permissions.PRINT},
                encrypt_metadata=encrypt_metadata,
                method=method,
            )
        )
    assert (original_xmp in out) == (not encrypt_metadata)
    for label, flags in (("editor", int(Permissions.PRINT)), ("owner", int(Permissions.ALL) | 2)):
        reader = PdfFileReader(io.BytesIO(out))
        key = keys.PrivateKeyInfo.load((RECIPIENT_ASSETS / f"recipient-{label}.key").read_bytes())
        cert = x509.Certificate.load((RECIPIENT_ASSETS / f"recipient-{label}.cer").read_bytes())
        auth = reader.decrypt_pubkey(SimpleEnvelopeKeyDecrypter(cert, key))
        assert auth.permission_flags is not None
        assert auth.permission_flags.as_uint32() & (int(Permissions.ALL) | 2) == flags
        page = reader.root["/Pages"]["/Kids"][0].get_object()
        assert page["/Contents"].data == RECIPIENT_STREAM
        assert bytes(page["/FixtureValue"]) == RECIPIENT_STRING
        assert reader.root["/Metadata"].data == original_xmp
    with pyropdf.open(out, credential=editor) as reopened:
        assert reopened.permissions == Permissions.PRINT
        assert reopened.catalog["Metadata"].read() == original_xmp
    with pytest.raises(pyropdf.PasswordError):
        pyropdf.open(
            pyropdf.open(numbered(1)).to_bytes(
                encryption=pyropdf.RecipientEncryption(recipients={editor.certificate: None})
            ),
            credential=owner,
        )


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (b"/adbe.pkcs7.s5", b"/adbe.pkcs7.s4", "adbe.pkcs7.s5"),
        (b"/AESV3", b"/AESV2", "AESV3"),
        (b"/EncryptMetadata true", b"/EncryptMetadata null", "boolean"),
        (b"/Recipients [", b"/Skipieeets [", "Recipients"),
    ],
)
def test_public_key_malformed_filter_is_atomic(old: bytes, new: bytes, message: str) -> None:
    source = (RECIPIENT_ASSETS / "pyhanko-recipients.pdf").read_bytes()
    assert old in source
    bad = source.replace(old, new)
    doc = pyropdf.open(bad)
    with pytest.raises(pyropdf.MalformedPdfError, match=message):
        doc.unlock(recipient("editor"))
    assert doc.needs_password
    with pytest.raises(pyropdf.EncryptedDocumentError):
        _ = doc.catalog


@pytest.mark.spec("public-key-security-handler", "read")
@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("identity-strings", "adbe.pkcs7.s5 V5 256-bit AES"),
        ("recipients-aes128", "adbe.pkcs7.s5 V4 128-bit AES"),
        ("s3", "adbe.pkcs7.s3 V1 40-bit RC4"),
        ("s4", "adbe.pkcs7.s4 V2 128-bit RC4"),
        ("v2", "adbe.pkcs7.s5 V4 128-bit RC4"),
    ],
)
def test_public_key_algorithms_and_filter_selection(fixture: str, expected: str) -> None:
    with pyropdf.open(RECIPIENT_ASSETS / f"pyhanko-{fixture}.pdf", credential=recipient("editor")) as doc:
        assert doc.encryption_method == f"Adobe.PubSec {expected}"
        assert not doc.check().defects
        assert doc.objects[3].read() == RECIPIENT_STREAM
        assert doc.objects[4]["FixtureValue"] == RECIPIENT_STRING
        assert doc.permissions == Permissions.MODIFY
        if fixture == "identity-strings":
            assert b"<x:xmpmeta" in doc.catalog["Metadata"].read()
            assert doc.pages[0].pixmap(dpi=72).width == 50
        else:
            with pytest.raises(pyropdf.PdfError, match="legacy public-key file key"):
                doc.pages[0].pixmap()
        doc.objects[4]["FixtureValue"] = b"edited"
        for incremental in (True, False):
            if "RC4" in expected:
                with pytest.raises(pyropdf.PdfError, match="does not write RC4"):
                    doc.to_bytes(incremental=incremental, encryption=Encryption.KEEP)
            else:
                saved = doc.to_bytes(incremental=incremental, encryption=Encryption.KEEP)
                with pyropdf.open(saved, credential=recipient("editor")) as reopened:
                    assert reopened.objects[4]["FixtureValue"] == b"edited"


def test_public_key_changing_encryption_requires_authorized_recipient(tmp_path: Path) -> None:
    with pyropdf.open(RECIPIENT_ASSETS / "pyhanko-recipients.pdf", credential=recipient("editor")) as doc:
        doc.objects[4]["FixtureValue"] = b"keep my edit"
        for encryption in (None, Encryption(user_password="new")):
            with pytest.raises(pyropdf.PdfError, match="permission to change encryption"):
                doc.to_bytes(encryption=encryption)
        target = tmp_path / "untouched.pdf"
        target.write_bytes(b"original")
        with pytest.raises(pyropdf.PdfError, match="permission to change encryption"):
            doc.save(target)
        assert target.read_bytes() == b"original"
        assert doc.unlock(recipient("owner")) is Access.OWNER
        with pyropdf.open(doc.to_bytes()) as clear:
            assert clear.objects[4]["FixtureValue"] == b"keep my edit"


@pytest.mark.parametrize(
    ("count", "envelope_bytes", "code"), [(129, 1, "recipient_count"), (1, 65_537, "recipient_envelope_size")]
)
def test_public_key_recipient_bounds_fail_before_disclosure(count: int, envelope_bytes: int, code: str) -> None:
    envelopes = ("<" + "00" * envelope_bytes + "> ") * count
    source = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [] /Count 0 >>",
            3: f"<< /Filter /Adobe.PubSec /SubFilter /adbe.pkcs7.s4 /V 2 /Length 128 /Recipients [{envelopes}] >>",
        }
    ).replace(b"/Root 1 0 R >>", b"/Root 1 0 R /Encrypt 3 0 R >>")
    doc = pyropdf.open(source)
    with pytest.raises(pyropdf.LimitError) as refusal:
        doc.unlock(recipient("owner"))
    assert refusal.value.code == code
    assert doc.needs_password
    with pytest.raises(pyropdf.EncryptedDocumentError):
        _ = doc.catalog


def test_public_key_equal_permissions_share_a_cms_envelope() -> None:
    editor, owner = recipient("editor"), recipient("owner")
    with pytest.raises(pyropdf.PasswordError, match="does not match"):
        pyropdf.RecipientCredential(certificate=editor.certificate, private_key=owner.private_key)
    settings = pyropdf.RecipientEncryption(
        recipients={editor.certificate: Permissions.COPY, owner.certificate: Permissions.COPY}
    )
    saved = pyropdf.open(numbered(1)).to_bytes(encryption=settings)
    for credential in (owner, editor):
        with pyropdf.open(saved, credential=credential) as doc:
            assert doc.permissions == Permissions.COPY
            assert len(doc.trailer["Encrypt"]["CF"]["DefaultCryptFilter"]["Recipients"]) == 1


STANDARD_REVISIONS = {
    "standard-rc4-r2": (1, 2, ("user-rc4-40.pdf",)),
    "standard-rc4-r3": (2, 3, ("user-rc4-128.pdf", "user-rc4-128-indirect-length.pdf")),
    "standard-rc4-r4": (4, 4, ("user-r4-rc4-128.pdf",)),
    "standard-aes-r4": (4, 4, ("user-aes-128.pdf", "user-aes-128-plain-metadata.pdf")),
    "standard-aes-r5": (5, 5, ("user-aes-256-r5.pdf",)),
    "standard-aes-r6": (5, 6, ("user-aes-256.pdf", "user-aes-256-object-streams.pdf", "owneronly-aes-256.pdf")),
}


def stored_stream(data: bytes, independent: pikepdf.Pdf, stream: pikepdf.Object) -> bytes:
    """Read the actual ciphertext at qpdf's active xref offset and resolved stream length."""
    offset = independent.get_xref_table()[stream.objgen].offset
    assert offset is not None
    header = re.match(rb"\d+\s+\d+\s+obj\b.*?\bstream(?:\r\n|\n)", data[offset:], re.DOTALL)
    assert header
    start = offset + header.end()
    return data[start : start + int(stream.Length)]


@pytest.mark.spec("{profile}", "read", "interpret", "edit", "preserve")
@pytest.mark.parametrize(
    "profile",
    [
        "standard-rc4-r2",
        "standard-rc4-r3",
        "standard-rc4-r4",
        "standard-aes-r4",
        "standard-aes-r5",
        "standard-aes-r6",
    ],
)
def test_password_revision_keeps_old_ciphertext_and_encrypts_new_content(profile: str) -> None:
    version, revision, names = STANDARD_REVISIONS[profile]
    marker = b"independently read new stream and string"
    for name in names:
        source = (ASSETS / name).read_bytes()
        user = "" if name.startswith("owneronly") else "userpw"
        with (
            pikepdf.open(io.BytesIO(source), password=user) as original,
            pyropdf.open(source, password="ownerpw") as doc,
        ):
            assert doc.access is Access.OWNER
            assert original.user_password_matched
            assert (version, revision) == (original.trailer.Encrypt.V, original.trailer.Encrypt.R)
            if version == 4:
                crypt = original.trailer.Encrypt
                assert str(crypt.CF[str(crypt.StmF)].CFM) == ("/AESV2" if profile == "standard-aes-r4" else "/V2")
            original_content = original.pages[0].obj.Contents
            old = stored_stream(source, original, original_content)
            assert old != original_content.read_raw_bytes()
            assert doc.to_bytes(incremental=True) == source
            doc.catalog["XXString"] = marker
            doc.catalog["XXStream"] = doc.objects[doc.objects.add_stream(marker, filter=None)]
            doc.pages[0].rotation = 90
            with pyropdf.open(doc.to_bytes()) as plain:
                assert not plain.needs_password
                expected = bytes(memoryview(plain.pages[0].pixmap(dpi=36)))
                if revision <= 4:
                    with pytest.raises(pyropdf.PdfError, match="hayro cannot read the document"):
                        doc.pages[0].pixmap(dpi=36)
                else:
                    assert bytes(memoryview(doc.pages[0].pixmap(dpi=36))) == expected
                with pyropdf.open(source, password=user) as by_user:
                    by_user.pages[0].rotation = 90
                    assert bytes(memoryview(by_user.pages[0].pixmap(dpi=36))) == expected
            for incremental in (False, True):
                result = doc.to_bytes(incremental=incremental, encryption=Encryption.KEEP)
                assert result.startswith(source) is incremental
                assert marker not in result
                with pikepdf.open(io.BytesIO(result), password=user) as saved:
                    assert saved.user_password_matched
                    assert saved.allow == original.allow
                    assert (version, revision) == (saved.trailer.Encrypt.V, saved.trailer.Encrypt.R)
                    assert saved.pages[0].Rotate == 90
                    assert stored_stream(result, saved, saved.pages[0].obj.Contents) == old
                    assert bytes(saved.Root.XXString) == saved.Root.XXStream.read_bytes() == marker
                    assert stored_stream(result, saved, saved.Root.XXStream) != marker
                with pikepdf.open(io.BytesIO(result), password="ownerpw") as owner:
                    assert owner.owner_password_matched
                with pytest.raises(pikepdf.PasswordError):
                    pikepdf.open(io.BytesIO(result), password="wrong password")


@pytest.mark.spec("{profile}", "create")
@pytest.mark.parametrize("profile", ["standard-aes-r4", "standard-aes-r6"])
def test_aes_authoring_sets_the_exact_revision_and_permissions(profile: str) -> None:
    version, revision, _ = STANDARD_REVISIONS[profile]
    method = "aes-128" if revision == 4 else "aes-256"
    with pyropdf.open(numbered(1)) as doc:
        config = Encryption(
            user_password="reader", owner_password="owner", permissions=Permissions.PRINT, method=method
        )
        doc.catalog["XXMarker"] = b"a new password-protected document"
        data = doc.to_bytes(encryption=config)
    with pikepdf.open(io.BytesIO(data), password="reader") as reader:
        assert reader.user_password_matched
        assert not reader.owner_password_matched
        assert reader.allow.print_lowres
        assert not reader.allow.extract
        assert not reader.allow.modify_other
        assert bytes(reader.Root.XXMarker) == b"a new password-protected document"
        crypt = reader.trailer.Encrypt
        assert (version, revision) == (crypt.V, crypt.R)
        assert str(crypt.CF[str(crypt.StrF)].CFM) == ("/AESV2" if revision == 4 else "/AESV3")
    with pikepdf.open(io.BytesIO(data), password="owner") as owner:
        assert owner.owner_password_matched
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(io.BytesIO(data), password="wrong password")


@pytest.mark.parametrize("length", [19, 20, 24, 25])
def test_recipient_cms_seed_shape_does_not_grant_default_permissions(length: int) -> None:
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.serialization import pkcs7  # noqa: PLC0415

    owner = recipient("owner")
    payload = bytes(20) + b"\xff" * 5
    envelope = (
        pkcs7.PKCS7EnvelopeBuilder()
        .set_data(payload[:length])
        .add_recipient(owner.certificate)
        .encrypt(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])
    )
    cms = f"<{envelope.hex()}>"
    source = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [] /Count 0 >>",
            3: f"<< /Filter /Adobe.PubSec /SubFilter /adbe.pkcs7.s4 /V 2 /Length 128 /Recipients [{cms}] >>",
        }
    ).replace(b"/Root 1 0 R >>", b"/Root 1 0 R /Encrypt 3 0 R >>")
    with pyropdf.open(source) as doc:
        with pytest.raises(pyropdf.PasswordError):
            doc.unlock(recipient("editor"))
        if length == 24:
            assert doc.unlock(owner) is Access.OWNER
            assert doc.permissions == Permissions.ALL
        else:
            message = "four permission bytes" if length == 20 else "20 or 24 bytes"
            with pytest.raises(pyropdf.MalformedPdfError, match=message):
                doc.unlock(owner)
            assert doc.needs_password
