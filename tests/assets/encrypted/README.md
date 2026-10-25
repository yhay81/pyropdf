# Encrypted fixtures

pylopdf's tests/assets/encrypted at 884af0dbf21973cfd434a9eca4e769de62fcc25c,
which [its `generate.py`](https://github.com/yhay81/pylopdf/blob/884af0dbf21973cfd434a9eca4e769de62fcc25c/tests/assets/encrypted/generate.py) wrote with pypdf and pikepdf.
The passwords are user `userpw` and owner `ownerpw`; `owneronly-*` has an
empty user password. Each file has two pages, whose text begins
"Encrypted page one" and "Encrypted page two".
