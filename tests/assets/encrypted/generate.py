"""Write user-rc4-128-indirect-length.pdf, an encrypted file whose streams have an indirect /Length.

Each page's content stream refers to its length: page one's to an uncompressed
integer, and page two's to an integer in an object stream. The encrypted data
of each ends in a line feed, which a search for ``endstream`` would trim. The
standard security handler is V2 R3 128-bit RC4 (ISO 32000-1 section 7.6.3),
with the user password ``userpw`` and the owner password ``ownerpw``, so the
script needs no library, and its output is the same on every run:

    uv run python tests/assets/encrypted/generate.py
"""

from __future__ import annotations

import hashlib
import zlib
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent / "user-rc4-128-indirect-length.pdf"

#: The padding string of ISO 32000-1 section 7.6.3.3, Algorithm 2.
PAD = bytes.fromhex("28bf4e5e4e758a4164004e56fffa01082e2e00b6d0683e802f0ca9fe6453697a")
USER = b"userpw"
OWNER = b"ownerpw"
PERMISSIONS = -4
FILE_ID = bytes(range(16))

#: The text of each page, which every fixture of this directory begins with.
TEXTS = (b"Encrypted page one", b"Encrypted page two")

#: The white-space byte that the encrypted data of each content stream ends in.
LAST = 0x0A


def md5(data: bytes) -> bytes:
    """Return the MD5 digest of ``data``, which the handler uses as PDF defines."""
    return hashlib.md5(data, usedforsecurity=False).digest()


def rc4(key: bytes, data: bytes) -> bytes:
    """Return ``data`` encrypted, or decrypted, with RC4 and ``key``."""
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        out.append(byte ^ state[(state[i] + state[j]) % 256])
    return bytes(out)


def padded(password: bytes) -> bytes:
    """Return ``password`` padded to 32 bytes."""
    return (password + PAD)[:32]


def rc4_rounds(key: bytes, data: bytes) -> bytes:
    """Return ``data`` encrypted with ``key`` and then with 19 keys derived from it (R3)."""
    for round_ in range(20):
        data = rc4(bytes(byte ^ round_ for byte in key), data)
    return data


def owner_entry() -> bytes:
    """Return the /O entry: Algorithm 3."""
    digest = md5(padded(OWNER))
    for _ in range(50):
        digest = md5(digest)
    return rc4_rounds(digest[:16], padded(USER))


def file_key(owner: bytes) -> bytes:
    """Return the file encryption key: Algorithm 2."""
    digest = md5(padded(USER) + owner + PERMISSIONS.to_bytes(4, "little", signed=True) + FILE_ID)
    for _ in range(50):
        digest = md5(digest[:16])
    return digest[:16]


def user_entry(key: bytes) -> bytes:
    """Return the /U entry: Algorithm 5, with 16 bytes of zeros after the 16 that it checks."""
    return rc4_rounds(key, md5(PAD + FILE_ID)) + bytes(16)


def object_key(key: bytes, number: int) -> bytes:
    """Return the key of object ``number`` of generation 0: Algorithm 1."""
    return md5(key + number.to_bytes(3, "little") + bytes(2))[:16]


def content(key: bytes, number: int, text: bytes) -> bytes:
    """Return the plain content of object ``number``, which shows ``text``.

    A comment ends the content with the byte that makes its encrypted data
    end in ``LAST``.
    """
    plain = b"BT /F1 24 Tf 72 720 Td (" + text + b") Tj ET\n%"
    stream = rc4(object_key(key, number), plain + b"\0")
    return plain + bytes([stream[-1] ^ LAST])


def write() -> bytes:
    """Return the bytes of the fixture."""
    owner = owner_entry()
    key = file_key(owner)
    contents = {6: content(key, 6, TEXTS[0]), 8: content(key, 8, TEXTS[1])}
    encrypted = {number: rc4(object_key(key, number), plain) for number, plain in contents.items()}
    assert all(data[-1] == LAST for data in encrypted.values())
    members = b"9 0 " + str(len(encrypted[8])).encode()
    member_data = rc4(object_key(key, 10), zlib.compress(members))
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources 5 0 R /Contents 6 0 R >>",
        4: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources 5 0 R /Contents 8 0 R >>",
        5: b"<< /Font << /F1 " + font + b" >> >>",
        6: b"<< /Length 7 0 R >>\nstream\n" + encrypted[6] + b"\nendstream",
        7: str(len(encrypted[6])).encode(),
        8: b"<< /Length 9 0 R >>\nstream\n" + encrypted[8] + b"\nendstream",
        10: b"<< /Type /ObjStm /N 1 /First 4 /Filter /FlateDecode /Length "
        + str(len(member_data)).encode()
        + b" >>\nstream\n"
        + member_data
        + b"\nendstream",
        11: b"<< /Filter /Standard /V 2 /R 3 /Length 128 /P "
        + str(PERMISSIONS).encode()
        + b" /O <"
        + owner.hex().encode()
        + b"> /U <"
        + user_entry(key).hex().encode()
        + b"> >>",
    }
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for number, body in objects.items():
        offsets[number] = len(out)
        out += b"%d 0 obj\n%b\nendobj\n" % (number, body)
    # Each entry: type, offset or object stream, generation or index.
    rows = [
        (0, 0, 0xFFFF),
        *((2, 10, 0) if number == 9 else (1, offsets[number], 0) for number in range(1, 12)),
        (1, len(out), 0),
    ]
    table = b"".join(kind.to_bytes(1) + field.to_bytes(4) + last.to_bytes(2) for kind, field, last in rows)
    file_id = FILE_ID.hex().encode()
    out += (
        b"12 0 obj\n<< /Type /XRef /Size 13 /W [1 4 2] /Root 1 0 R /Encrypt 11 0 R /ID [<%b> <%b>] /Length %d >>\n"
        b"stream\n%b\nendstream\nendobj\nstartxref\n%d\n%%%%EOF\n"
    ) % (file_id, file_id, len(table), table, len(out))
    return bytes(out)


def main() -> None:
    """Write the fixture."""
    OUTPUT.write_bytes(write())


if __name__ == "__main__":
    main()
