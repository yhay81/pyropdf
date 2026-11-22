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
import sysconfig
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pikepdf
import pytest
from test_pages import build_raw_pdf, numbered

import pyropdf
from pyropdf import Access, Encryption, Permissions

if TYPE_CHECKING:
    from pyhanko.pdf_utils.reader import PdfFileReader

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


def test_recipient_wheel_declares_its_optional_schema_and_crypto_provider() -> None:
    from importlib.metadata import requires  # noqa: PLC0415
    from importlib.util import find_spec  # noqa: PLC0415

    from packaging.requirements import Requirement  # noqa: PLC0415

    requirements = requires("pyropdf")
    assert requirements is not None
    providers = {r.name: r.marker for r in map(Requirement, requirements) if r.name in {"asn1crypto", "cryptography"}}
    assert set(providers) == {"asn1crypto", "cryptography"}
    for marker in providers.values():
        assert marker is not None
        assert marker.evaluate({"extra": "recipients"})
        assert not marker.evaluate({"extra": ""})
    assert find_spec("asn1crypto.cms") is not None
    assert find_spec("cryptography.hazmat.primitives.serialization.pkcs7") is not None


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
        ({"method": "aes"}, ValueError, "method must be 'aes-256', 'aes-128' or 'aes-gcm'"),
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
    from cryptography.hazmat.primitives.asymmetric import ec, rsa  # noqa: PLC0415

    key = serialization.load_der_private_key((RECIPIENT_ASSETS / f"recipient-{label}.key").read_bytes(), None)
    assert isinstance(key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey))
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


@pytest.fixture(scope="module")
def _pyhanko_oracle() -> None:
    if sysconfig.get_config_var("Py_GIL_DISABLED") == 1:
        pytest.skip("pyHanko imports enable the GIL on free-threaded Python")
    # Register RFC 3161 before asn1crypto caches the reverse CMS content-type map.
    from asn1crypto import tsp  # noqa: F401, PLC0415


@pytest.mark.spec("public-key-security-handler", "create")
@pytest.mark.parametrize("encrypt_metadata", [True, False])
@pytest.mark.parametrize("method", ["aes-256", "aes-128", "aes-gcm"])
@pytest.mark.usefixtures("_pyhanko_oracle")
def test_public_key_output_read_by_pyhanko(
    method: Literal["aes-256", "aes-128", "aes-gcm"], *, encrypt_metadata: bool
) -> None:

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


@pytest.mark.spec("public-key-security-handler", "read", "edit", "preserve")
@pytest.mark.parametrize(
    "mode",
    ["oaep", "ecdh", "ecdh384", "ecdh521", "aes192-oaep", "aes128-oaep-default", "3des", "des", "rc4", "rc2-128"],
)
@pytest.mark.parametrize("incremental", [False, True])
def test_independent_cms_algorithms_preserve_ciphertext(mode: str, *, incremental: bool) -> None:
    source = (RECIPIENT_ASSETS / f"cms-{mode}.pdf").read_bytes()
    credential = recipient(mode.replace("ecdh", "ec") if mode.startswith("ecdh") else "owner")
    with pyropdf.open(source) as doc:
        with pytest.raises(pyropdf.PasswordError):
            doc.unlock(recipient("editor"))
        assert doc.needs_password
        assert doc.unlock(credential) is Access.OWNER
        assert doc.permissions == Permissions.ALL
        page = doc.pages[0]
        assert page.obj["Marker"] == b"CMS fixture string"
        contents = page.obj["Contents"]
        assert contents.read() == b"0 g 4 5 14 19 re f\n"
        ciphertext = source.split(f"{contents.xref} 0 obj\n".encode(), 1)[1].split(b"\nendobj", 1)[0]
        envelope = bytes(doc.trailer["Encrypt"]["CF"]["DefaultCryptFilter"]["Recipients"][0])
        page.rotation = 90
        saved = doc.to_bytes(encryption=Encryption.KEEP, incremental=incremental)
    assert saved.startswith(source) is incremental
    assert ciphertext in saved
    with pyropdf.open(saved, credential=credential) as reopened:
        assert reopened.pages[0].rotation == 90
        assert reopened.pages[0].obj["Contents"].read() == b"0 g 4 5 14 19 re f\n"
        assert reopened.trailer["Encrypt"]["CF"]["DefaultCryptFilter"]["Recipients"][0] == envelope


@pytest.mark.spec("public-key-security-handler", "create")
@pytest.mark.parametrize("cipher", ["aes-128", "aes-192", "aes-256"])
@pytest.mark.parametrize("transport", ["pkcs1v15", "oaep"])
@pytest.mark.parametrize("ec_label", ["ec", "ec384", "ec521"])
def test_independent_reader_checks_each_cms_writer(
    cipher: Literal["aes-128", "aes-192", "aes-256"], transport: Literal["pkcs1v15", "oaep"], ec_label: str
) -> None:
    import io  # noqa: PLC0415
    import sysconfig  # noqa: PLC0415

    if sysconfig.get_config_var("Py_GIL_DISABLED") == 1:
        pytest.skip("pyHanko imports enable the GIL on free-threaded Python")
    from asn1crypto import cms, keys, x509  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt.pubkey import SimpleEnvelopeKeyDecrypter  # noqa: PLC0415
    from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: PLC0415

    rsa, ec = recipient("owner"), recipient(ec_label)
    settings = pyropdf.RecipientEncryption(
        recipients={rsa.certificate: None, ec.certificate: Permissions.PRINT},
        cms_cipher=cipher,
        rsa_padding=transport,
    )
    with pyropdf.open(RECIPIENT_ASSETS / "cms-oaep.pdf", credential=rsa) as doc:
        saved = doc.to_bytes(encryption=settings)
    for label, expected, scheme in (
        ("owner", int(Permissions.ALL) | 2, "ktri"),
        (ec_label, int(Permissions.PRINT), "kari"),
    ):
        reader = PdfFileReader(io.BytesIO(saved))
        encrypt = reader.encrypt_dict
        assert encrypt is not None
        envelopes = encrypt["/CF"]["/DefaultCryptFilter"]["/Recipients"]
        content = [cms.ContentInfo.load(bytes(envelope))["content"] for envelope in envelopes]
        message = next(m for m in content if m["recipient_infos"][0].name == scheme)
        algo = message["encrypted_content_info"]["content_encryption_algorithm"]["algorithm"].native
        assert algo == cipher.replace("-", "") + "_cbc"
        if scheme == "ktri":
            assert message["recipient_infos"][0].chosen["key_encryption_algorithm"]["algorithm"].native == (
                "rsaes_oaep" if transport == "oaep" else "rsaes_pkcs1v15"
            )
        key = keys.PrivateKeyInfo.load((RECIPIENT_ASSETS / f"recipient-{label}.key").read_bytes())
        cert = x509.Certificate.load((RECIPIENT_ASSETS / f"recipient-{label}.cer").read_bytes())
        auth = reader.decrypt_pubkey(SimpleEnvelopeKeyDecrypter(cert, key))
        assert auth.permission_flags is not None
        assert auth.permission_flags.as_uint32() & (int(Permissions.ALL) | 2) == expected
        page = reader.root["/Pages"]["/Kids"][0].get_object()
        assert page["/Contents"].data == b"0 g 4 5 14 19 re f\n"
        assert page["/Marker"] == "CMS fixture string"


@pytest.mark.parametrize("problem", ["too-many", "bad-transport", "bad-digest", "bad-content", "trailing"])
def test_invalid_cms_algorithms_refuse_before_unlocking(problem: str) -> None:
    from asn1crypto import cms  # noqa: PLC0415

    credential = recipient("owner")
    with pyropdf.open(RECIPIENT_ASSETS / "cms-oaep.pdf", credential=credential) as original:
        envelope = cms.ContentInfo.load(bytes(original.trailer["Encrypt"]["CF"]["DefaultCryptFilter"]["Recipients"][0]))
    message = envelope["content"]
    info = message["recipient_infos"][0]
    if problem == "too-many":
        # Short wrapped-key placeholders keep this control within the outer 64 KiB envelope bound.
        info.chosen["encrypted_key"] = b"x"
        message["recipient_infos"] = [info] * 129
    elif problem == "bad-transport":
        info.chosen["key_encryption_algorithm"] = {"algorithm": "1.2.3.4"}
    elif problem == "bad-digest":
        info.chosen["key_encryption_algorithm"]["parameters"]["hash_algorithm"] = {"algorithm": "md5"}
    elif problem == "bad-content":
        message["encrypted_content_info"]["content_encryption_algorithm"] = {"algorithm": "1.2.3.4"}
    encoded = envelope.dump(force=True) + (b"junk" if problem == "trailing" else b"")
    source = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [] /Count 0 >>",
            3: "<< /Filter /Adobe.PubSec /SubFilter /adbe.pkcs7.s4 /V 2 /Length 128 "
            f"/Recipients [<{encoded.hex()}>] >>",
        }
    ).replace(b"/Root 1 0 R >>", b"/Root 1 0 R /Encrypt 3 0 R >>")
    with pyropdf.open(source) as doc:
        if problem == "too-many":
            with pytest.raises(pyropdf.LimitError) as refusal:
                doc.unlock(credential)
            assert refusal.value.code == "recipient_count"
        else:
            with pytest.raises(pyropdf.PasswordError):
                doc.unlock(credential)
        assert doc.needs_password
        with pytest.raises(pyropdf.EncryptedDocumentError):
            _ = doc.catalog


@pytest.mark.parametrize("name", ["rc4", "rc2", "des", "3des"])
def test_obsolete_cms_ciphers_are_read_only(name: str) -> None:
    from typing import cast  # noqa: PLC0415

    with pytest.raises(ValueError, match="CMS content encryption"):
        pyropdf.RecipientEncryption(
            recipients={recipient("owner").certificate: None}, cms_cipher=cast("Literal['aes-256']", name)
        )


@pytest.mark.parametrize("label", ["owner", "ec"])
def test_cms_recipient_key_usage_must_allow_the_key_operation(label: str) -> None:
    from cryptography import x509  # noqa: PLC0415
    from cryptography.hazmat.primitives import hashes  # noqa: PLC0415

    original = recipient(label)
    cert = original.certificate
    wrong_usage = x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=False,
        crl_sign=False,
        encipher_only=False,
        decipher_only=False,
    )
    signing_only = (
        x509.CertificateBuilder()
        .subject_name(cert.subject)
        .issuer_name(cert.issuer)
        .public_key(cert.public_key())
        .serial_number(93601)
        .not_valid_before(cert.not_valid_before_utc)
        .not_valid_after(cert.not_valid_after_utc)
        .add_extension(wrong_usage, critical=True)
        .sign(original.private_key, hashes.SHA256())
    )
    with pytest.raises(ValueError, match="key usage"):
        pyropdf.RecipientEncryption(recipients={signing_only: None})
    with pytest.raises(ValueError, match="key usage"):
        pyropdf.RecipientCredential(certificate=signing_only, private_key=original.private_key)


def test_mixed_transport_recipients_can_share_permissions() -> None:
    rsa, ec = recipient("owner"), recipient("ec")
    settings = pyropdf.RecipientEncryption(
        recipients={rsa.certificate: Permissions.COPY, ec.certificate: Permissions.COPY}
    )
    with pyropdf.open(RECIPIENT_ASSETS / "cms-oaep.pdf", credential=rsa) as doc:
        saved = doc.to_bytes(encryption=settings)
    for credential in (rsa, ec):
        with pyropdf.open(saved, credential=credential) as reopened:
            assert reopened.permissions == Permissions.COPY
            assert len(reopened.trailer["Encrypt"]["CF"]["DefaultCryptFilter"]["Recipients"]) == 1
            assert reopened.pages[0].obj["Marker"] == b"CMS fixture string"


def test_recipient_open_before_timestamp_providers_register_their_content_types() -> None:
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    # A fresh process reproduces applications that first use CMS through the recipient API.
    program = """
from pathlib import Path
import sys
import pyropdf
from cryptography import x509
from cryptography.hazmat.primitives.serialization import load_der_private_key
root = Path(sys.argv[1])
cert = x509.load_der_x509_certificate((root / "recipient-owner.cer").read_bytes())
key = load_der_private_key((root / "recipient-owner.key").read_bytes(), None)
credential = pyropdf.RecipientCredential(certificate=cert, private_key=key)
with pyropdf.open(root / "cms-oaep.pdf", credential=credential) as doc:
    assert len(doc.pages) == 1
from asn1crypto import cms, tsp
assert cms.ContentType("tst_info").dotted == "1.2.840.113549.1.9.16.1.4"
"""
    subprocess.run([sys.executable, "-c", program, str(RECIPIENT_ASSETS)], check=True)  # noqa: S603


GCM_SOURCE = ASSETS / "user-aes-gcm.pdf"
GCM_CONTENT = b"0 0 1 rg 1 1 6 5 re f\n"


@pytest.mark.spec("aes-gcm", "read", "interpret")
def test_gcm_authenticates_independent_password_and_recipient_inputs() -> None:
    for password, access in (("ownerpw", Access.OWNER), ("userpw", Access.USER)):
        with pyropdf.open(GCM_SOURCE, password=password) as doc:
            assert doc.access is access
            assert doc.encryption_method == "Standard V6 R7 256-bit AES-GCM"
            assert doc.pages[0].obj["XXLabel"] == b"independent GCM label"
            assert doc.pages[0].obj["Contents"].read() == GCM_CONTENT
            with pytest.raises(pyropdf.EncryptedDocumentError, match=r"hayro.*AES-GCM"):
                doc.pages[0].pixmap(dpi=36)
    with pytest.raises(pyropdf.PasswordError):
        pyropdf.open(GCM_SOURCE, password="wrong")
    with pyropdf.open(RECIPIENT_ASSETS / "pyhanko-gcm.pdf", credential=recipient("owner")) as doc:
        assert doc.encryption_method == "Adobe.PubSec adbe.pkcs7.s5 V6 256-bit AES-GCM"
        assert doc.objects[3].read() == RECIPIENT_STREAM
        assert doc.objects[4]["FixtureValue"] == RECIPIENT_STRING


@pytest.mark.spec("aes-gcm", "interpret")
@pytest.mark.parametrize("kind", ["string", "stream", "short-stream"])
def test_gcm_rejects_bad_tags_before_releasing_data(kind: str, tmp_path: Path) -> None:

    source = GCM_SOURCE.read_bytes()
    if kind == "string":
        match = re.search(rb"/XXLabel <([0-9a-fA-F]+)>", source)
        assert match
        at = match.end(1) - 1
        corrupted = source[:at] + (b"1" if source[at : at + 1] == b"0" else b"0") + source[at + 1 :]
    else:
        start = source.index(b"3 0 obj\n")
        at = source.index(b"\nendstream", start) - 1
        corrupted = source[:at] + bytes([source[at] ^ 1]) + source[at + 1 :]
        if kind == "short-stream":
            corrupted = corrupted.replace(b"/Length 50\n", b"/Length 10\n", 1)
    target = tmp_path / "output.pdf"
    target.write_bytes(b"previous output")
    with pyropdf.open(corrupted, password="ownerpw") as doc:
        with pytest.raises(pyropdf.MalformedPdfError, match="AES-GCM authentication failed"):
            _ = doc.objects[4] if kind == "string" else doc.objects[3]
        with pytest.raises(pyropdf.MalformedPdfError, match="AES-GCM authentication failed"):
            doc.save(target)
    assert target.read_bytes() == b"previous output"


@pytest.mark.spec("aes-gcm", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_gcm_edit_keeps_original_ciphertext(*, incremental: bool) -> None:
    source = GCM_SOURCE.read_bytes()
    untouched = source.split(b"3 0 obj\n")[1].split(b"\nendobj")[0]
    with pyropdf.open(source, password="ownerpw") as doc:
        assert doc.to_bytes(incremental=True) == source
        doc.pages[0].obj["XXLabel"] = b"edited GCM label"
        out = doc.to_bytes(encryption=Encryption.KEEP, incremental=incremental)
        assert out.startswith(source) is incremental
        assert untouched in out
        assert b"edited GCM label" not in out
        with pyropdf.open(out, password="userpw") as after:
            assert after.pages[0].obj["Contents"].read() == GCM_CONTENT
            assert after.pages[0].obj["XXLabel"] == b"edited GCM label"


@pytest.mark.spec("aes-gcm", "create")
@pytest.mark.parametrize("layout", ["table", "object-stream", "hybrid", "packed-hybrid", "linearized"])
def test_gcm_new_save_interoperates_with_pyhanko(layout: str, tmp_path: Path) -> None:

    if sysconfig.get_config_var("Py_GIL_DISABLED") == 1:
        pytest.skip("pyHanko imports enable the GIL on free-threaded Python")
    from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: PLC0415

    config = Encryption(user_password="customer", owner_password="owner", method="aes-gcm")
    with pyropdf.open(numbered(1)) as doc:
        # Exercise other owners of an indirect /Extensions dictionary.
        base = {"Private": {"BaseVersion": pyropdf.Name("1.7"), "ExtensionLevel": 4}}
        old = doc.objects[doc.objects.add(base)]
        doc.catalog["Extensions"] = old
        doc.catalog["XXOther"] = old
        doc.pages[0].obj["XXLabel"] = b"repeated"
        doc.pages[0].obj["XXOther"] = b"repeated"
        data = doc.to_bytes(
            encryption=config,
            object_streams=layout in {"object-stream", "packed-hybrid"},
            linearize=layout == "linearized",
            cross_reference="hybrid" if layout.endswith("hybrid") else None,
        )
        assert data.startswith(b"%PDF-2.0")
        assert b"repeated" not in data
        assert "ISO_" not in old
        assert doc.catalog.get("Version") != pyropdf.Name("2.0")
        target = tmp_path / "existing.pdf"
        target.write_bytes(b"original")
        with pytest.raises(ValueError, match="incremental save keeps"):
            doc.save(target, incremental=True, encryption=config)
        assert target.read_bytes() == b"original"
    reader = PdfFileReader(io.BytesIO(data))
    assert reader.decrypt("customer").status.name == "USER"
    security = reader.trailer_view["/Encrypt"]
    assert (security["/V"], security["/R"], security["/CF"]["/StdCF"]["/CFM"]) == (6, 7, "/AESV4")
    extensions = reader.root["/Extensions"]
    assert extensions["/Private"]["/ExtensionLevel"] == 4
    assert "/ISO_" not in reader.root["/XXOther"]
    assert len(extensions["/ISO_"]) == 1
    iso = extensions["/ISO_"][0]
    assert (iso["/BaseVersion"], iso["/ExtensionLevel"], iso["/ExtensionRevision"]) == ("/2.0", 32003, ":2023")
    page = reader.root["/Pages"]["/Kids"][0].get_object()
    assert page["/XXLabel"].original_bytes == page["/XXOther"].original_bytes == b"repeated"
    if layout == "table":
        values = [re.search(b"/" + key + rb"\s*<([0-9a-fA-F]+)>", data) for key in (b"XXLabel", b"XXOther")]
        assert all(values)
        assert len({bytes.fromhex(item[1].decode())[:12] for item in values if item}) == 2


@pytest.mark.parametrize(
    ("before", "after", "reason"),
    [
        (b"/R 7", b"/R 6", "V6 and R7"),
        (b"/AESV4", b"/AESV3", "declare an AESV4"),
        (b"/Length 256", b"/Length 128", "256-bit file encryption key"),
        (b"/Length 32", b"/Length 16", "32-byte crypt filter key"),
        (b"/P -4", b"/P -8", "encrypted permissions"),
    ],
)
def test_gcm_refuses_inconsistent_declarations(before: bytes, after: bytes, reason: str) -> None:
    source = GCM_SOURCE.read_bytes()
    assert source.count(before) == 1
    with pytest.raises(pyropdf.MalformedPdfError, match=reason):
        pyropdf.open(source.replace(before, after), password="ownerpw")


def test_gcm_keeps_other_iso_extensions_and_repairs_matching_declaration() -> None:
    with pyropdf.open(numbered(1)) as doc:

        def extension(level: int) -> pyropdf.Dictionary:
            return doc.objects[
                doc.objects.add({"BaseVersion": pyropdf.Name("2.0"), "ExtensionLevel": level, "XXKey": 15})
            ]

        other, incomplete = extension(32001), extension(32003)
        doc.catalog["Extensions"] = {"ISO_": [other, incomplete]}
        doc.catalog["XXBackup"] = incomplete
        other_xref = other.xref
        out = doc.to_bytes(encryption=Encryption(method="aes-gcm"))
    with pyropdf.open(out) as result:
        declarations = result.catalog["Extensions"]["ISO_"]
        assert len(declarations) == 2
        assert declarations[0].xref == other_xref
        assert declarations[0]["ExtensionLevel"] == 32001
        assert declarations[1]["ExtensionRevision"] == b":2023"
        assert declarations[1]["URL"] == b"https://www.iso.org/standard/45876.html"
        assert declarations[1]["XXKey"] == result.catalog["XXBackup"]["XXKey"] == 15
        assert "ExtensionRevision" not in result.catalog["XXBackup"]


def test_gcm_repeated_incremental_updates_have_fresh_nonces_for_the_same_key() -> None:

    source = GCM_SOURCE.read_bytes()
    with pyropdf.open(source, password="ownerpw") as doc:
        doc.pages[0].obj["XXLabel"] = b"same plaintext"
        outputs = [doc.to_bytes(incremental=True) for _ in range(2)]
    nonces = set()
    for data in outputs:
        assert data.startswith(source)
        match = re.search(rb"/XXLabel\s*<([A-Fa-f0-9]+)>", data[len(source) :])
        assert match
        encrypted = bytes.fromhex(match[1].decode())
        assert len(encrypted) == 12 + len(b"same plaintext") + 16
        nonces.add(encrypted[:12])
        with pyropdf.open(data, password="userpw") as doc:
            assert doc.pages[0].obj["XXLabel"] == b"same plaintext"
    assert len(nonces) == 2


STANDARD_REVISIONS = {
    "standard-rc4-r2": (1, 2, ("user-rc4-40.pdf",)),
    "standard-rc4-r3": (2, 3, ("user-rc4-128.pdf", "user-rc4-128-indirect-length.pdf")),
    "standard-rc4-r4": (4, 4, ("user-r4-rc4-128.pdf",)),
    "standard-aes-r4": (4, 4, ("user-aes-128.pdf", "user-aes-128-plain-metadata.pdf")),
    "standard-aes-r5": (5, 5, ("user-aes-256-r5.pdf",)),
    "standard-aes-r6": (5, 6, ("user-aes-256.pdf", "user-aes-256-object-streams.pdf", "owneronly-aes-256.pdf")),
}


@pytest.mark.spec("{profile}", "interpret")
@pytest.mark.spec("standard-security-handler", "interpret")
@pytest.mark.parametrize("profile", ["standard-rc4-r2", "standard-rc4-r3", "standard-rc4-r4", "standard-aes-r4"])
def test_legacy_password_roles_interpret_the_same_authenticated_version(profile: str) -> None:
    for name in STANDARD_REVISIONS[profile][2]:
        data = (ASSETS / name).read_bytes()
        with (
            pyropdf.open(data, password="ownerpw") as owner,
            pyropdf.open(data, password="userpw") as user,
            pyropdf.open(owner.to_bytes()) as plain,
            pikepdf.open(io.BytesIO(data), password="ownerpw") as independent,
        ):
            rights = user.permissions
            assert independent.owner_password_matched
            for rotation in (0, 90):
                for doc in (owner, user, plain):
                    if rotation:
                        doc.pages[0].rotation = rotation
                expected = plain.pages[0]
                assert "Encrypted page one" in expected.text()
                for doc in (owner, user):
                    page = doc.pages[0]
                    assert page.text() == expected.text()
                    assert bytes(memoryview(page.pixmap(dpi=36))) == bytes(memoryview(expected.pixmap(dpi=36)))
                    if rotation == 0:
                        assert doc.to_bytes(incremental=True) == data
                assert owner.access is Access.OWNER
                assert owner.permissions == Permissions.ALL
                assert user.access is Access.USER
                assert user.permissions == rights


@pytest.mark.parametrize("user", ["", "café", "x" * 31 + "猫", "x" * 30 + "🥝", "x" * 32 + "truncated"])
def test_legacy_owner_uses_the_effective_user_bytes_within_the_reader(user: str) -> None:
    with pyropdf.open(ASSETS / "user-aes-128.pdf", password="ownerpw") as source:
        plain = source.to_bytes()
        data = source.to_bytes(encryption=Encryption(method="aes-128", owner_password="文書管理", user_password=user))
    with (
        pyropdf.open(data, password="文書管理") as owner,
        pyropdf.open(data, password=user) as by_user,
        pyropdf.open(plain) as baseline,
    ):
        for doc in (owner, by_user):
            assert doc.pages[0].text() == baseline.pages[0].text()
            assert bytes(memoryview(doc.pages[0].pixmap(dpi=36))) == bytes(memoryview(baseline.pages[0].pixmap(dpi=36)))
            assert doc.to_bytes(incremental=True) == data
        assert owner.access is Access.OWNER
        assert by_user.access is Access.USER
    if user:
        with pytest.raises(pyropdf.PasswordError):
            pyropdf.open(data, password="wrong")


def test_legacy_pdfdocencoded_user_password_remains_a_typed_reader_boundary() -> None:
    data = io.BytesIO()
    with pikepdf.open(ASSETS / "user-aes-128.pdf", password="ownerpw") as independent:
        independent.save(data, encryption=pikepdf.Encryption(owner="ownerpw", user="é", R=4, aes=True))
    raw = data.getvalue()
    with (
        pyropdf.open(raw, password="ownerpw") as doc,
        pikepdf.open(io.BytesIO(raw), password="ownerpw") as independent,
    ):
        assert independent.owner_password_matched
        assert doc.access is Access.OWNER
        assert doc.pages[0].obj["Contents"].read() == independent.pages[0].Contents.read_bytes()
        for operation in (doc.pages[0].text, doc.pages[0].pixmap):
            with pytest.raises(pyropdf.EncryptedDocumentError, match="UTF-8"):
                operation()
        assert doc.to_bytes(incremental=True) == raw
        assert doc.permissions == Permissions.ALL


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
                assert bytes(memoryview(doc.pages[0].pixmap(dpi=36))) == expected
                assert doc.pages[0].text() == plain.pages[0].text()
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


def _independent_crypt_reader(data: bytes, handler: str) -> PdfFileReader:
    """Open the result with pyHanko's independent crypt-filter implementation."""
    from asn1crypto import keys, x509  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt.pubkey import SimpleEnvelopeKeyDecrypter  # noqa: PLC0415
    from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: PLC0415

    reader = PdfFileReader(io.BytesIO(data))
    if handler == "password":
        reader.decrypt("owner")
    else:
        key = keys.PrivateKeyInfo.load((RECIPIENT_ASSETS / "recipient-owner.key").read_bytes())
        cert = x509.Certificate.load((RECIPIENT_ASSETS / "recipient-owner.cer").read_bytes())
        reader.decrypt_pubkey(SimpleEnvelopeKeyDecrypter(cert, key))
    return reader


def _add_crypt_streams(
    doc: pyropdf.Document, selected: str, attachment: bytes, clear: bytes, private: bytes
) -> tuple[int, int, int, int]:
    name = pyropdf.Name
    doc.attachments.add("file.bin", attachment)
    ef = doc.catalog["Names"]["EmbeddedFiles"]["Names"][1]["EF"]["F"]
    ef.write(attachment, filter=None)
    ef_ref = ef.xref
    public_ref = doc.objects.add_stream(clear, filter=None)
    public = doc.objects[public_ref]
    public["Filter"] = name("Crypt")
    # A missing /Name means /Identity. Dictionary strings still use StrF.
    public["Marker"] = b"a protected PDF string"
    private_ref = doc.objects.add_stream(private)
    protected = doc.objects[private_ref]
    protected["Filter"] = [name("Crypt"), name("FlateDecode")]
    protected["DecodeParms"] = [{"Name": name(selected)}, None]
    ordinary_ref = doc.objects.add_stream(b"unrelated private stream", filter=None)
    return ef_ref, public_ref, private_ref, ordinary_ref


@pytest.mark.parametrize("handler", ["password", "recipient"])
@pytest.mark.parametrize("method", ["aes-128", "aes-256", "aes-gcm"])
@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("crypt-filter", "read", "create", "edit", "preserve")
@pytest.mark.spec("crypt-filters", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.usefixtures("_pyhanko_oracle")
def test_explicit_stream_crypt_and_embedded_defaults(
    handler: str, method: Literal["aes-128", "aes-256", "aes-gcm"], *, incremental: bool
) -> None:
    from pyhanko.pdf_utils import generic  # noqa: PLC0415

    name = pyropdf.Name
    password = "owner" if handler == "password" else None
    credential = recipient("owner") if handler == "recipient" else None
    options = (
        pyropdf.RecipientEncryption(recipients={credential.certificate: None}, method=method)
        if credential is not None
        else Encryption(user_password="user", owner_password="owner", method=method)
    )
    selected = "StdCF" if handler == "password" else "DefaultCryptFilter"
    attachment = b"data governed by the embedded-file default"
    clear = b"explicitly public ordinary stream"
    private = b"a distinct named and compressed stream"
    with pyropdf.open(numbered(1)) as doc:
        ef_ref, public_ref, private_ref, ordinary_ref = _add_crypt_streams(doc, selected, attachment, clear, private)
        initial = doc.to_bytes(encryption=options)
    assert clear in initial
    assert private not in initial
    assert attachment not in initial
    reader = _independent_crypt_reader(initial, handler)
    assert reader.get_object(generic.Reference(private_ref, 0, reader)).data == private
    assert reader.get_object(generic.Reference(public_ref, 0, reader)).data == clear
    untouched = initial.split(f"{ordinary_ref} 0 obj\n".encode(), 1)[1].split(b"\nendobj", 1)[0]
    with pyropdf.open(initial, password=password, credential=credential) as doc:
        assert doc.objects[private_ref].read() == private
        assert doc.objects[public_ref]["Marker"] == b"a protected PDF string"
        assert doc.attachments["file.bin"].read() == attachment
        doc.trailer["Encrypt"]["EFF"] = name("Identity")
        doc.objects[private_ref]["DecodeParms"][0]["Name"] = name("Identity")
        # The old public stream is now protected by the explicit named filter.
        doc.objects[public_ref]["DecodeParms"] = {"Name": name(selected)}
        changed = doc.to_bytes(encryption=Encryption.KEEP, incremental=incremental)
    assert untouched in changed
    if incremental:
        assert changed.startswith(initial)
    reader = _independent_crypt_reader(changed, handler)
    for reference, expected in ((public_ref, clear), (private_ref, private), (ef_ref, attachment)):
        assert reader.get_object(generic.Reference(reference, 0, reader)).data == expected
    assert attachment in changed
    with pyropdf.open(changed, password=password, credential=credential) as doc:
        assert doc.objects[public_ref].read() == clear
        assert doc.objects[private_ref].read() == private
        assert doc.attachments["file.bin"].read() == attachment
        assert doc.objects[ordinary_ref].read() == b"unrelated private stream"
        if incremental:
            with doc.revisions[0].open() as old:
                assert old.attachments["file.bin"].read() == attachment
                assert old.objects[private_ref].read() == private
                assert "EFF" not in old.trailer["Encrypt"]
        plain = doc.to_bytes()
    with pyropdf.open(plain) as doc:
        assert doc.objects[private_ref].read() == private
        assert name("Crypt") not in doc.objects[private_ref]["Filter"]
        assert "Filter" not in doc.objects[public_ref]


@pytest.mark.parametrize(
    ("filters", "params", "message"),
    [
        (["FlateDecode", "Crypt"], [None, None], "first"),
        (["Crypt", "Crypt"], [None, None], "only"),
        (["Crypt", "FlateDecode"], [None], "correspond"),
        (["Crypt"], [{"Name": 12}], "must name"),
        (["Crypt"], [{"Type": pyropdf.Name("Wrong")}], "parameter /Type"),
        (["Crypt"], [{"Name": pyropdf.Name("Absent")}], "undefined"),
    ],
)
@pytest.mark.spec("crypt-filter", "create", "edit")
def test_malformed_explicit_stream_crypt_refuses_save(
    filters: list[str], params: list[object], message: str, tmp_path: Path
) -> None:
    path = tmp_path / "unchanged.pdf"
    path.write_bytes(b"untouched")
    with pyropdf.open(numbered(1)) as doc:
        ref = doc.objects.add_stream(b"secret", filter=None)
        doc.objects[ref]["Filter"] = [pyropdf.Name(f) for f in filters]
        doc.objects[ref]["DecodeParms"] = params
        with pytest.raises(pyropdf.MalformedPdfError, match=message):
            doc.save(path, encryption=Encryption(user_password="u"))
    assert path.read_bytes() == b"untouched"


@pytest.mark.spec("crypt-filters", "create")
def test_encryption_requires_a_resolved_filter_selector(tmp_path: Path) -> None:
    path = tmp_path / "existing.pdf"
    path.write_bytes(b"unchanged")
    source = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /Fixture 3 0 R >>",
            2: "<< /Type /Pages /Kids [] /Count 0 >>",
            3: "<< /Filter 4 1 R /Length 9 >>\nstream\ntest data\nendstream",
            4: "[/Crypt]",
        }
    )
    with pyropdf.open(source) as doc:
        with pytest.raises(pyropdf.MalformedPdfError, match="unresolved /Filter"):
            doc.save(path, encryption=Encryption(user_password="reader"))
        assert doc.to_bytes(incremental=True) == source
    assert path.read_bytes() == b"unchanged"


def _private_recipient_control(*, attachment_only: bool, implicit: bool, mac: bool = False, gcm: bool = False) -> bytes:
    """Produce two independent recipient seeds with pyHanko (ISO Table 27)."""
    from unittest.mock import patch  # noqa: PLC0415

    from asn1crypto import x509  # noqa: PLC0415
    from pyhanko.pdf_utils import generic  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt.api import CryptFilterConfiguration, SecurityHandlerVersion  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt.pubkey import (  # noqa: PLC0415
        PubKeyAdbeSubFilter,
        PubKeyAESCryptFilter,
        PubKeyAESGCMCryptFilter,
        PubKeySecurityHandler,
        RecipientEncryptionPolicy,
    )
    from pyhanko.pdf_utils.embed import EmbeddedFileObject  # noqa: PLC0415
    from pyhanko.pdf_utils.writer import PageObject, PdfFileWriter  # noqa: PLC0415

    name = generic.pdf_name
    writer = PdfFileWriter(stream_xrefs=False)
    public, private = name("/PublicDefault"), name("/DefaultCryptFilter" if mac else "/PrivateContent")
    default = name("/Identity") if attachment_only else public
    if gcm:
        normal = PubKeyAESGCMCryptFilter(acts_as_default=True)
        restricted = PubKeyAESGCMCryptFilter(acts_as_default=False)
    else:
        normal = PubKeyAESCryptFilter(keylen=32, acts_as_default=True)
        restricted = PubKeyAESCryptFilter(keylen=32, acts_as_default=False)
    config = CryptFilterConfiguration(
        {private: restricted} if attachment_only else {public: normal, private: restricted},
        default_stream_filter=default,
        default_string_filter=default,
        default_file_filter=private,
    )
    crypto = PubKeySecurityHandler(
        SecurityHandlerVersion.AES_GCM if gcm else SecurityHandlerVersion.AES256,
        PubKeyAdbeSubFilter.S5,
        32,
        crypt_filter_config=config,
        compat_entries=False,
        kdf_salt=bytes(range(32)) if mac else None,
    )
    certs = [
        x509.Certificate.load((RECIPIENT_ASSETS / f"recipient-{label}.cer").read_bytes())
        for label in ("owner", "editor")
    ]
    if not attachment_only:
        normal.add_recipients(certs, policy=RecipientEncryptionPolicy())
    restricted.add_recipients(certs[:1], policy=RecipientEncryptionPolicy())
    writer._assign_security_handler(crypto)  # noqa: SLF001 - pyHanko accepts a custom crypt-filter configuration here.
    page = PageObject(writer.add_object(generic.StreamObject(stream_data=b"0 g 1 2 3 4 re f\n")), [0, 0, 40, 50])
    page[name("/FixtureName")] = generic.TextStringObject("fixture string")
    writer.insert_page(page)
    named = generic.StreamObject(stream_data=b"Named private data")
    named.compress()
    named.add_crypt_filter(private, handler=crypto)
    writer.root[name("/FixtureNamed")] = writer.add_object(named)
    embedded = EmbeddedFileObject.from_file_data(writer, b"Embedded private data", compress=False)
    writer.root[name("/FixtureEmbedded")] = embedded.ef_stream_ref
    spec = generic.DictionaryObject(
        {
            name("/Type"): name("/Filespec"),
            name("/F"): generic.TextStringObject("file.bin"),
            name("/EF"): generic.DictionaryObject({name("/F"): embedded.ef_stream_ref}),
        }
    )
    writer.root[name("/Names")] = generic.DictionaryObject(
        {
            name("/EmbeddedFiles"): generic.DictionaryObject(
                {name("/Names"): generic.ArrayObject([generic.TextStringObject("file.bin"), writer.add_object(spec)])}
            )
        }
    )
    output = io.BytesIO()
    if mac:
        # ISO/TS 32004 derives a public-key MAC from /DefaultCryptFilter.
        # pyHanko normally uses /StmF, which this fixture deliberately separates.
        with patch.object(crypto, "get_file_encryption_key", return_value=restricted.shared_key):
            writer.write(output)
    else:
        writer.write(output)
    data = output.getvalue()
    if implicit:
        # pyHanko explicitly spells its /EFF on embedded-file writes. Remove
        # just those entries without disturbing any offsets or ciphertext.
        start = data.index(b"/Type /EmbeddedFile")
        end = data.index(b"stream", start)
        head = data[start:end]
        for pattern in (rb"/Filter\s*/Crypt\b", rb"/DecodeParms\s*<<[^>]*>>"):
            head, count = re.subn(pattern, lambda m: b" " * len(m[0]), head)
            assert count == 1
        data = data[:start] + head + data[end:]
    return data


@pytest.mark.spec("crypt-filter", "read", "preserve")
@pytest.mark.spec("crypt-filters", "read", "interpret")
@pytest.mark.parametrize("attachment_only", [False, True])
@pytest.mark.parametrize("implicit", [False, True])
@pytest.mark.usefixtures("_pyhanko_oracle")
def test_distinct_cms_filter_keys_and_credential_upgrade(*, attachment_only: bool, implicit: bool) -> None:
    data = _private_recipient_control(attachment_only=attachment_only, implicit=implicit)
    owner, editor = recipient("owner"), recipient("editor")
    with pyropdf.open(data, credential=None if attachment_only else editor) as doc:
        assert doc.pages[0].obj["Contents"].read() == b"0 g 1 2 3 4 re f\n"
        assert doc.pages[0].obj["FixtureName"] == b"fixture string"
        with pytest.raises(pyropdf.PasswordError):
            doc.catalog["FixtureNamed"].read()
        with pytest.raises(pyropdf.PasswordError):
            doc.catalog["FixtureEmbedded"].read()
        doc.catalog["KeepThisEdit"] = 1
        doc.unlock(owner)
        assert doc.catalog["FixtureNamed"].read() == b"Named private data"
        assert doc.catalog["FixtureEmbedded"].read() == b"Embedded private data"
        assert doc.catalog["KeepThisEdit"] == 1
        for incremental in (True, False):
            output = doc.to_bytes(encryption=Encryption.KEEP, incremental=incremental)
            with pyropdf.open(output, credential=owner) as reopened:
                assert reopened.catalog["FixtureNamed"].read() == b"Named private data"
                assert reopened.catalog["FixtureEmbedded"].read() == b"Embedded private data"
                assert reopened.catalog["KeepThisEdit"] == 1
            with pyropdf.open(output, credential=editor) as limited, pytest.raises(pyropdf.PasswordError):
                limited.catalog["FixtureEmbedded"].read()


@pytest.mark.parametrize("use_aes", [True, False], ids=["AESV2", "V2"])
@pytest.mark.spec("crypt-filter", "read")
@pytest.mark.usefixtures("_pyhanko_oracle")
def test_explicit_legacy_filter_uses_the_global_key_from_an_independent_writer(*, use_aes: bool) -> None:

    from pyhanko.pdf_utils import generic  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt.standard import StandardSecurityHandler, StandardSecuritySettingsRevision  # noqa: PLC0415
    from pyhanko.pdf_utils.writer import PdfFileWriter  # noqa: PLC0415

    writer = PdfFileWriter(stream_xrefs=False)
    handler = StandardSecurityHandler.build_from_pw_legacy(
        StandardSecuritySettingsRevision.RC4_OR_AES128,
        writer.document_id[0],
        "owner",
        "user",
        use_aes128=use_aes,
    )
    writer._assign_security_handler(handler)  # noqa: SLF001 - supply the independent writer's exact R4 configuration.
    payload = b"a stream with the shared key"
    stream = generic.StreamObject(stream_data=payload)
    stream.compress()
    stream.add_crypt_filter(generic.pdf_name("/StdCF"), handler=handler)
    writer.root[generic.pdf_name("/WithCrypt")] = writer.add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    original = buffer.getvalue()
    with pyropdf.open(original, password="user") as doc:
        assert doc.catalog["WithCrypt"].read() == payload
        saved = doc.to_bytes(incremental=True, encryption=Encryption.KEEP)
        assert saved == original


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("crypt-filters", "edit")
def test_invalid_embedded_file_default_refuses_atomically(tmp_path: Path, *, incremental: bool) -> None:
    with pyropdf.open(numbered(1)) as doc:
        initial = doc.to_bytes(encryption=Encryption(owner_password="owner"))
    with pyropdf.open(initial, password="owner") as doc:
        path = tmp_path / "keep.pdf"
        path.write_bytes(initial)
        doc.trailer["Encrypt"]["EFF"] = pyropdf.Name("Undefined")
        with pytest.raises(pyropdf.MalformedPdfError, match="undefined"):
            doc.save(path, incremental=incremental, encryption=Encryption.KEEP)
        assert path.read_bytes() == initial


@pytest.mark.spec("crypt-filter", "read")
def test_undefined_crypt_name_on_an_existing_stream_refuses_its_data() -> None:
    with pyropdf.open(numbered(1)) as doc:
        ref = doc.objects.add_stream(b"declared clear data", filter=None)
        doc.objects[ref]["Filter"] = pyropdf.Name("Crypt")
        doc.objects[ref]["DecodeParms"] = {"Name": pyropdf.Name("Identity")}
        original = doc.to_bytes(encryption=Encryption(user_password="pw"))
    assert original.count(b"/Name /Identity") == 1
    damaged = original.replace(b"/Name /Identity", b"/Name /UnknownX")
    with pyropdf.open(damaged, password="pw") as doc, pytest.raises(pyropdf.MalformedPdfError, match="undefined"):
        doc.objects[ref].read()


@pytest.mark.parametrize("selected", ["Identity", "StdCF"])
@pytest.mark.parametrize("repeat_count", [False, True])
@pytest.mark.parametrize("indirect_filter", [False, True])
@pytest.mark.spec("crypt-filter", "read")
@pytest.mark.usefixtures("_pyhanko_oracle")
def test_object_stream_members_obey_the_containers_explicit_crypt_filter(
    monkeypatch: pytest.MonkeyPatch, selected: str, *, repeat_count: bool, indirect_filter: bool
) -> None:

    from pyhanko.pdf_utils import generic  # noqa: PLC0415
    from pyhanko.pdf_utils.writer import PdfFileWriter  # noqa: PLC0415
    from pyhanko.pdf_utils.xref import ObjectStream  # noqa: PLC0415

    writer = PdfFileWriter()
    writer.encrypt("owner", "user", pdf_mac=False)
    container = writer.prepare_object_stream()
    materialise = container.as_pdf_object

    def with_filter() -> generic.StreamObject:
        result = materialise()
        result.add_crypt_filter(generic.pdf_name(f"/{selected}"), handler=writer.security_handler)
        if repeat_count:
            result[generic.pdf_name("/X")] = generic.NumberObject(1)
        return result

    assert isinstance(container, ObjectStream)
    monkeypatch.setattr(container, "as_pdf_object", with_filter)
    filters = writer.add_object(generic.ArrayObject([generic.pdf_name("/Crypt"), generic.pdf_name("/FlateDecode")]))
    ref = writer.add_object(generic.TextStringObject("object stream member"), obj_stream=container)
    writer.root[generic.pdf_name("/Packed")] = ref
    output = io.BytesIO()
    writer.write(output)
    data = output.getvalue()
    if indirect_filter:
        inline = b"/Filter [ /Crypt /FlateDecode ]"
        assert data.count(inline) == 1
        data = data.replace(inline, f"/Filter {filters.idnum} 0 R".encode("ascii").ljust(len(inline)))
    if repeat_count:
        # The last /N supplies the member count. Equal-width replacements keep xref offsets valid.
        assert data.count(b"/N 1\n/First") == data.count(b"/X 1\n/Length") == 1
        data = data.replace(b"/N 1\n/First", b"/N 0\n/First").replace(b"/X 1\n/Length", b"/N 1\n/Length")
    with pyropdf.open(data, password="user") as doc:
        assert doc.catalog["Packed"] == b"object stream member"


@pytest.mark.spec("crypt-filters", "interpret")
@pytest.mark.parametrize("handler", ["password", "recipient"])
@pytest.mark.parametrize("selected", ["default", "Identity"])
def test_aes256_named_page_content_matches_the_clear_render(handler: str, selected: str) -> None:
    with pyropdf.open(numbered(1)) as doc:
        content = doc.objects[doc.objects.add_stream(b"0 g 11 12 18 33 re f\n", filter=None)]
        doc.pages[0].obj["Contents"] = content
        expected = doc.pages[0].pixmap(dpi=20).to_bytes()
        default = "StdCF" if handler == "password" else "DefaultCryptFilter"
        content["Filter"] = pyropdf.Name("Crypt")
        content["DecodeParms"] = {"Name": pyropdf.Name(default if selected == "default" else selected)}
        if handler == "password":
            saved = doc.to_bytes(encryption=Encryption(user_password="pw"))
            reopened = pyropdf.open(saved, password="pw")
        else:
            owner = recipient("owner")
            saved = doc.to_bytes(encryption=pyropdf.RecipientEncryption(recipients={owner.certificate: None}))
            reopened = pyropdf.open(saved, credential=owner)
    with reopened:
        assert reopened.pages[0].pixmap(dpi=20).to_bytes() == expected


@pytest.mark.parametrize("role", ["EF", "RF"])
@pytest.mark.parametrize("incremental", [True, False])
@pytest.mark.spec("crypt-filters", "read", "edit", "preserve")
@pytest.mark.usefixtures("_pyhanko_oracle")
def test_untyped_file_streams_follow_their_current_file_specification(role: str, *, incremental: bool) -> None:

    from pyhanko.pdf_utils import generic  # noqa: PLC0415
    from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: PLC0415

    raw_a, raw_b = b"first untyped payload", b"second untyped payload"
    with pyropdf.open(numbered(1)) as doc:
        doc.attachments.add("file.bin", raw_a)
        spec = doc.catalog["Names"]["EmbeddedFiles"]["Names"][1]
        original = spec["EF"]["F"]
        original.write(raw_a, filter=None)
        del original["Type"]
        other = doc.objects[doc.objects.add_stream(raw_b, filter=None)]
        if role == "RF":
            links = doc.objects[doc.objects.add([b"related.bin", original])]
            spec["RF"] = doc.objects[doc.objects.add({"F": links})]
            base = doc.objects[doc.objects.add_stream(b"primary file", filter=None)]
            base["Type"] = pyropdf.Name("EmbeddedFile")
            spec["EF"] = {"F": base}
        else:
            spec["EF"] = doc.objects[doc.objects.add({"F": original})]
        first, second = original.xref, other.xref
        protected = doc.to_bytes(
            encryption=Encryption(user_password="user", owner_password="owner"), object_streams=True
        )
    with pyropdf.open(protected, password="owner") as doc:
        doc.trailer["Encrypt"]["EFF"] = pyropdf.Name("Identity")
        initial = doc.to_bytes(incremental=incremental, encryption=Encryption.KEEP)
    with pyropdf.open(initial, password="owner") as doc:
        assert doc.objects[first].read() == raw_a
        assert doc.objects[second].read() == raw_b
        spec = doc.catalog["Names"]["EmbeddedFiles"]["Names"][1]
        if role == "EF":
            spec["EF"] = {"F": doc.objects[second]}
        else:
            spec["RF"] = {"F": [b"related.bin", doc.objects[second]]}
        changed = doc.to_bytes(incremental=incremental, encryption=Encryption.KEEP)
    for data, clear_id, secret_id, clear_data, secret_data in (
        (initial, first, second, raw_a, raw_b),
        (changed, second, first, raw_b, raw_a),
    ):
        reader = PdfFileReader(io.BytesIO(data))
        reader.decrypt("user")
        # Inspect the actual active objects, including the last revision of
        # an incremental edit, independently of any reader's type heuristic.
        raw_clear = reader.get_object(generic.Reference(clear_id, 0, reader), never_decrypt=True).encoded_data
        raw_secret = reader.get_object(generic.Reference(secret_id, 0, reader), never_decrypt=True).encoded_data
        assert raw_clear == clear_data
        assert raw_secret != secret_data
        with pyropdf.open(data, password="user") as doc:
            assert doc.objects[clear_id].read() == clear_data
            assert doc.objects[secret_id].read() == secret_data


@pytest.mark.spec("crypt-filters", "edit")
def test_invalid_related_file_pairs_refuse_a_new_default_without_overwriting_the_original(tmp_path: Path) -> None:
    with pyropdf.open(numbered(1)) as doc:
        doc.attachments.add("file.bin", b"payload")
        initial = doc.to_bytes(encryption=Encryption(owner_password="owner"))
    with pyropdf.open(initial, password="owner") as doc:
        spec = doc.catalog["Names"]["EmbeddedFiles"]["Names"][1]
        spec["RF"] = {"F": [b"missing stream"]}
        doc.trailer["Encrypt"]["EFF"] = pyropdf.Name("Identity")
        path = tmp_path / "original.pdf"
        path.write_bytes(initial)
        with pytest.raises(pyropdf.MalformedPdfError, match="name and stream pairs"):
            doc.save(path, incremental=True, encryption=Encryption.KEEP)
        assert path.read_bytes() == initial


@pytest.mark.parametrize("operation", ["raw", "flate", "attachment"])
@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("crypt-filter", "edit")
@pytest.mark.spec("crypt-filters", "edit")
@pytest.mark.usefixtures("_pyhanko_oracle")
def test_payload_replacement_keeps_a_private_recipient_audience(operation: str, *, incremental: bool) -> None:
    import io  # noqa: PLC0415

    from pyhanko.pdf_utils import generic  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt.pubkey import SimpleEnvelopeKeyDecrypter  # noqa: PLC0415
    from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: PLC0415

    source = _private_recipient_control(attachment_only=False, implicit=False)
    owner, editor = recipient("owner"), recipient("editor")
    payload = b"replacement only the additional recipient may see"
    with pyropdf.open(source, credential=owner) as doc:
        doc.trailer["Encrypt"]["EFF"] = pyropdf.Name("Identity")
        initial = doc.to_bytes(encryption=Encryption.KEEP)
    with pyropdf.open(initial, credential=owner) as doc:
        target = doc.catalog["FixtureEmbedded"]
        target["DecodeParms"]["OwnerNote"] = pyropdf.Name("KeepMe")
        target["Unrelated"] = 42
        if operation == "attachment":
            doc.attachments["file.bin"].write(payload)
        else:
            target.write(payload, filter=None if operation == "raw" else "FlateDecode")
        saved = doc.to_bytes(encryption=Encryption.KEEP, incremental=incremental)
    assert payload not in saved
    with pyropdf.open(saved, credential=owner) as doc:
        assert doc.attachments["file.bin"].read() == payload
        target = doc.catalog["FixtureEmbedded"]
        assert target["Unrelated"] == 42
        params = target["DecodeParms"] if operation == "raw" else target["DecodeParms"][0]
        assert params["Name"] == pyropdf.Name("PrivateContent")
        assert params["OwnerNote"] == pyropdf.Name("KeepMe")
    with pyropdf.open(saved, credential=editor) as doc, pytest.raises(pyropdf.PasswordError):
        doc.catalog["FixtureEmbedded"].read()
    reader = PdfFileReader(io.BytesIO(saved))
    credential = SimpleEnvelopeKeyDecrypter.load(
        str(RECIPIENT_ASSETS / "recipient-owner.key"), str(RECIPIENT_ASSETS / "recipient-owner.cer")
    )
    crypto = reader.security_handler
    assert crypto is not None
    assert crypto.get_stream_filter().authenticate(credential).status.value > 0
    assert crypto.get_stream_filter(generic.pdf_name("/PrivateContent")).authenticate(credential).status.value > 0
    assert reader.root["/FixtureEmbedded"].data == payload


@pytest.mark.parametrize("compress", [True, False])
@pytest.mark.parametrize("implicit_name", [True, False])
def test_identity_selector_survives_data_encoding_changes(*, compress: bool, implicit_name: bool) -> None:
    settings = {} if implicit_name else {"Name": pyropdf.Name("Identity")}
    with pyropdf.open(numbered(1)) as doc:
        ref = doc.objects.add_stream(
            b"before", {"Filter": pyropdf.Name("Crypt"), "DecodeParms": settings}, filter="FlateDecode"
        )
        doc.objects[ref].write(b"after", filter="FlateDecode" if compress else None)
        stored = doc.to_bytes(encryption=Encryption(user_password="user"))
    with pyropdf.open(stored, password="user") as doc:
        stream = doc.objects[ref]
        assert stream.read() == b"after"
        if compress:
            assert list(stream["Filter"]) == [pyropdf.Name("Crypt"), pyropdf.Name("FlateDecode")]
            params = stream["DecodeParms"][0]
        else:
            assert stream["Filter"] == pyropdf.Name("Crypt")
            params = stream["DecodeParms"]
        assert params.get("Name") == (None if implicit_name else pyropdf.Name("Identity"))


def test_a_malformed_security_selector_does_not_partially_replace_stream_data() -> None:
    with pyropdf.open(numbered(1)) as doc:
        ref = doc.objects.add_stream(b"original", filter=None)
        stream = doc.objects[ref]
        stream["Filter"] = [pyropdf.Name("FlateDecode"), pyropdf.Name("Crypt")]
        with pytest.raises(pyropdf.MalformedPdfError, match="first"):
            stream.write(b"replacement")
        assert stream.read(decode=False) == b"original"
        assert list(stream["Filter"]) == [pyropdf.Name("FlateDecode"), pyropdf.Name("Crypt")]


def test_stream_replacement_resolves_a_shared_identity_selector() -> None:
    with pyropdf.open(numbered(1)) as doc:
        target = doc.objects[doc.objects.add_stream(b"original", filter=None)]
        shared = doc.objects[doc.objects.add([pyropdf.Name("Crypt")])]
        target["Filter"] = shared
        target.write(b"replacement")
        assert target.read() == b"replacement"
        assert list(shared) == [pyropdf.Name("Crypt")]


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("compress", [False, True])
@pytest.mark.usefixtures("_pyhanko_oracle")
@pytest.mark.spec("crypt-filter", "read", "create", "edit", "preserve")
@pytest.mark.spec("crypt-filters", "read", "create", "edit", "preserve")
def test_indirect_private_stream_parameters_remain_shared_after_replacement(
    *, incremental: bool, compress: bool
) -> None:
    import io  # noqa: PLC0415

    from pyhanko.pdf_utils import generic  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt.pubkey import SimpleEnvelopeKeyDecrypter  # noqa: PLC0415
    from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: PLC0415

    source = _private_recipient_control(attachment_only=False, implicit=False)
    owner = recipient("owner")
    with pyropdf.open(source, credential=owner) as doc:
        stream = doc.catalog["FixtureNamed"]
        options = doc.objects[doc.objects.add({"Name": pyropdf.Name("PrivateContent")})]
        number = options.xref
        stream["Filter"] = doc.objects[doc.objects.add([pyropdf.Name("Crypt"), pyropdf.Name("FlateDecode")])]
        stream["DecodeParms"] = doc.objects[doc.objects.add([options, None])]
        filter_id, params_id = stream["Filter"].xref, stream["DecodeParms"].xref
        shared = doc.to_bytes(encryption=Encryption.KEEP)
    with pyropdf.open(shared, credential=owner) as doc:
        stream = doc.catalog["FixtureNamed"]
        assert stream.read() == b"Named private data"
        sibling = doc.objects[doc.objects.add_stream(b"new private sibling", filter="FlateDecode")]
        sibling["Filter"] = stream["Filter"]
        sibling["DecodeParms"] = stream["DecodeParms"]
        doc.catalog["NewPrivateSibling"] = sibling
        stream.write(b"revised private stream", filter="FlateDecode" if compress else None)
        saved = doc.to_bytes(encryption=Encryption.KEEP, incremental=incremental)
    assert saved.startswith(shared) is incremental
    with pyropdf.open(saved, credential=owner) as doc:
        stream = doc.catalog["FixtureNamed"]
        assert stream.read() == b"revised private stream"
        selected = stream["DecodeParms"][0] if compress else stream["DecodeParms"]
        assert selected.xref == number
        assert doc.catalog["NewPrivateSibling"].read() == b"new private sibling"
    with pyropdf.open(saved, credential=recipient("editor")) as doc:
        for key in ("FixtureNamed", "NewPrivateSibling"):
            with pytest.raises(pyropdf.PasswordError):
                doc.catalog[key].read()
    reader = PdfFileReader(io.BytesIO(saved))
    credential = SimpleEnvelopeKeyDecrypter.load(
        str(RECIPIENT_ASSETS / "recipient-owner.key"), str(RECIPIENT_ASSETS / "recipient-owner.cer")
    )
    handler = reader.security_handler
    assert handler is not None
    assert handler.get_stream_filter().authenticate(credential).status.value > 0
    assert handler.get_stream_filter(generic.pdf_name("/PrivateContent")).authenticate(credential).status.value > 0
    stream = reader.root["/FixtureNamed"]
    assert stream.data == b"revised private stream"
    sibling = reader.root["/NewPrivateSibling"]
    assert sibling.data == b"new private sibling"
    assert sibling.raw_get("/Filter").idnum == filter_id
    assert sibling.raw_get("/DecodeParms").idnum == params_id
    assert sibling["/DecodeParms"].raw_get(0).idnum == number


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.usefixtures("_pyhanko_oracle")
@pytest.mark.spec("crypt-filters", "interpret", "edit")
def test_changing_only_shared_crypt_parameters_selects_each_revision_key(*, incremental: bool) -> None:
    source = _private_recipient_control(attachment_only=False, implicit=False)
    owner, editor = recipient("owner"), recipient("editor")
    with pyropdf.open(source, credential=owner) as doc:
        shared = doc.objects[doc.objects.add({"Name": pyropdf.Name("PrivateContent")})]
        stream = doc.catalog["FixtureNamed"]
        stream["DecodeParms"] = [shared, None]
        initial = doc.to_bytes(encryption=Encryption.KEEP)
    with pyropdf.open(initial, credential=owner) as doc:
        doc.catalog["FixtureNamed"]["DecodeParms"][0]["Name"] = pyropdf.Name("Identity")
        updated = doc.to_bytes(encryption=Encryption.KEEP, incremental=incremental)
    assert updated.startswith(initial) is incremental
    with pyropdf.open(initial, credential=editor) as doc, pytest.raises(pyropdf.PasswordError):
        doc.catalog["FixtureNamed"].read()
    with pyropdf.open(updated, credential=editor) as doc:
        assert doc.catalog["FixtureNamed"].read() == b"Named private data"


@pytest.mark.parametrize(
    "layout", ["plain", "decompress", "renumber", "packed", "linearized", "plain-linearized", "decompress-renumbered"]
)
def test_normalized_saves_follow_the_indirect_identity_filter(layout: str) -> None:
    import io  # noqa: PLC0415

    import pikepdf  # noqa: PLC0415

    payload = b"0 g 12 14 6 4 re f\n"
    with pyropdf.open(numbered(1)) as doc:
        target = doc.objects[doc.objects.add_stream(payload, filter="FlateDecode")]
        doc.pages[0].obj["Contents"] = target
        selector = doc.objects[doc.objects.add({"Name": pyropdf.Name("Identity")})]
        target["Filter"] = doc.objects[doc.objects.add([pyropdf.Name("Crypt"), pyropdf.Name("FlateDecode")])]
        target["DecodeParms"] = doc.objects[doc.objects.add([selector, None])]
        protected = doc.to_bytes(encryption=Encryption(user_password="viewer", owner_password="owner"))
    with pyropdf.open(protected, password="owner") as doc:
        assert doc.pages[0].obj["Contents"].read() == payload
        keep = layout in {"renumber", "packed", "linearized"}
        saved = doc.to_bytes(
            encryption=Encryption.KEEP if keep else None,
            renumber=layout in {"renumber", "decompress-renumbered"},
            object_streams=layout == "packed",
            linearize=layout in {"linearized", "plain-linearized"},
            decompress=layout.startswith("decompress"),
        )
    with pyropdf.open(saved, password="viewer" if keep else None) as doc:
        assert doc.pages[0].obj["Contents"].read() == payload
    with pikepdf.Pdf.open(io.BytesIO(saved), password="viewer" if keep else "") as reader:
        content = reader.pages[0].obj.Contents
        assert content.read_bytes() == payload
        if layout.startswith("decompress"):
            assert "/Filter" not in content
        elif not keep:
            assert "/Crypt" not in [str(name) for name in content.Filter]


@pytest.mark.spec("crypt-filter", "edit", "preserve")
@pytest.mark.parametrize(
    ("names", "parameters"),
    [
        (["ASCII85Decode", "FlateDecode"], [None, {"Predictor": 12, "Columns": 4}]),
        (["ASCIIHexDecode", "LZWDecode"], [None, {"EarlyChange": 0}]),
        (["RunLengthDecode"], [None]),
    ],
)
@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.usefixtures("_pyhanko_oracle")
def test_private_filter_precedes_the_new_data_chain(
    names: list[str], parameters: list[dict[str, int] | None], *, incremental: bool
) -> None:
    import io  # noqa: PLC0415

    import pikepdf  # noqa: PLC0415
    from pyhanko.pdf_utils import generic  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt.pubkey import SimpleEnvelopeKeyDecrypter  # noqa: PLC0415
    from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: PLC0415

    source = _private_recipient_control(attachment_only=False, implicit=False)
    payload = bytes(range(256)) * 8
    with pyropdf.open(source, credential=recipient("owner")) as doc:
        target = doc.catalog["FixtureNamed"]
        target.write(payload, filter=names, decode_parms=parameters)
        saved = doc.to_bytes(encryption=Encryption.KEEP, incremental=incremental)
    with pyropdf.open(saved, credential=recipient("editor")) as doc, pytest.raises(pyropdf.PasswordError):
        doc.catalog["FixtureNamed"].read()
    with pyropdf.open(saved, credential=recipient("owner")) as doc:
        assert doc.catalog["FixtureNamed"].read() == payload

    # pyHanko decrypts the explicit recipient filter. qpdf applies the ordinary
    # data filters, including LZW and RunLength, directly to that plaintext.
    reader = PdfFileReader(io.BytesIO(saved))
    private_name = generic.pdf_name("/PrivateContent")
    handler = reader.security_handler
    assert handler is not None
    credential = SimpleEnvelopeKeyDecrypter.load(
        str(RECIPIENT_ASSETS / "recipient-owner.key"), str(RECIPIENT_ASSETS / "recipient-owner.cer")
    )
    assert handler.get_stream_filter().authenticate(credential).status.value > 0
    private_filter = handler.get_stream_filter(private_name)
    assert private_filter.authenticate(credential).status.value > 0
    target = reader.root["/FixtureNamed"]
    assert list(target["/Filter"]) == ["/Crypt", *["/" + name for name in names]]
    actual = target["/DecodeParms"]
    assert actual[0]["/Name"] == private_name
    assert len(actual) == 1 + len(names)
    observed = [
        None if isinstance(item, generic.NullObject) else {key[1:]: int(val) for key, val in item.items()}
        for item in (actual[index] for index in range(1, len(actual)))
    ]
    assert observed == parameters
    encoded = private_filter.decrypt(private_filter.shared_key, target.encoded_data, actual[0])
    with pikepdf.Pdf.new() as clear:
        stream = pikepdf.Stream(clear, encoded)
        stream.Filter = pikepdf.Array([pikepdf.Name("/" + name) for name in names])
        stream.DecodeParms = pikepdf.Array(
            [
                None if params is None else pikepdf.Dictionary({"/" + key: value for key, value in params.items()})
                for params in observed
            ]
        )
        assert stream.read_bytes(pikepdf.StreamDecodeLevel.all) == payload


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.usefixtures("_pyhanko_oracle")
def test_compound_stream_encoding_preserves_shared_security_declarations(*, incremental: bool) -> None:
    source = _private_recipient_control(attachment_only=False, implicit=False)
    encoders = ["ASCII85Decode", "FlateDecode"]
    original = bytes(range(80))
    replacement = original[::-1]
    with pyropdf.open(source, credential=recipient("owner")) as doc:
        stream = doc.catalog["FixtureNamed"]
        stream.write(original, filter=encoders, decode_parms=[None, {"Predictor": 12, "Columns": 4}])
        selector = doc.objects[doc.objects.add({"Name": pyropdf.Name("PrivateContent")})]
        predictor = doc.objects[doc.objects.add({"Predictor": 12, "Columns": 4})]
        shared_names = doc.objects[doc.objects.add([pyropdf.Name("Crypt"), *map(pyropdf.Name, encoders)])]
        shared_options = doc.objects[doc.objects.add([selector, None, predictor])]
        numbers = shared_names.xref, shared_options.xref, selector.xref, predictor.xref
        stream["Filter"], stream["DecodeParms"] = shared_names, shared_options
        first = doc.to_bytes(encryption=Encryption.KEEP)
    with pyropdf.open(first, credential=recipient("owner")) as doc:
        stream = doc.catalog["FixtureNamed"]
        assert stream.read() == original
        stream.write(replacement, filter=encoders, decode_parms=[None, {"Columns": 4, "Predictor": 12}])
        second = doc.to_bytes(encryption=Encryption.KEEP, incremental=incremental)
    with pyropdf.open(second, credential=recipient("owner")) as doc:
        stream = doc.catalog["FixtureNamed"]
        assert stream.read() == replacement
        assert (stream["Filter"].xref, stream["DecodeParms"].xref) == numbers[:2]
        assert (stream["DecodeParms"][0].xref, stream["DecodeParms"][2].xref) == numbers[2:]
        stream.write(original, filter="RunLengthDecode")
        changed = doc.to_bytes(encryption=Encryption.KEEP, incremental=incremental)
    with pyropdf.open(changed, credential=recipient("owner")) as doc:
        stream = doc.catalog["FixtureNamed"]
        assert stream.read() == original
        assert list(stream["Filter"]) == [pyropdf.Name("Crypt"), pyropdf.Name("RunLengthDecode")]
        assert stream["DecodeParms"][0].xref == numbers[2]
    for saved in (first, second, changed):
        with pyropdf.open(saved, credential=recipient("editor")) as doc, pytest.raises(pyropdf.PasswordError):
            doc.catalog["FixtureNamed"].read()


@pytest.mark.spec("aes-gcm", "create")
@pytest.mark.parametrize("linearized", [False, True])
def test_gcm_promotes_only_the_output_header_of_a_new_document(*, linearized: bool) -> None:
    with pyropdf.Document(version="1.4") as doc:
        doc.pages.new((100, 100))
        cfg = Encryption(user_password="reader", owner_password="owner", method="aes-gcm")
        sealed = doc.to_bytes(encryption=cfg, linearize=linearized)
        assert pyropdf.probe(sealed).version == "2.0"
        with pyropdf.open(sealed, password="reader") as reopened:
            assert reopened.catalog["Extensions"]["ISO_"][0]["ExtensionLevel"] == 32003
        assert pyropdf.probe(doc.to_bytes(linearize=linearized)).version == "1.4"
