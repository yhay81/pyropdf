# Encrypted fixtures

pylopdf's tests/assets/encrypted at 884af0dbf21973cfd434a9eca4e769de62fcc25c,
which [its `generate.py`](https://github.com/yhay81/pylopdf/blob/884af0dbf21973cfd434a9eca4e769de62fcc25c/tests/assets/encrypted/generate.py) wrote with pypdf and pikepdf.
The passwords are user `userpw` and owner `ownerpw`; `owneronly-*` has an
empty user password. Each file has two pages, whose text begins
"Encrypted page one" and "Encrypted page two".

`user-rc4-128-indirect-length.pdf` is pyropdf's own, which `generate.py`
writes with no library: V2 R3 128-bit RC4, with the same passwords and page
texts. Each content stream's `/Length` refers to an object, one uncompressed
and one in an object stream, and its encrypted data ends in a line feed.
