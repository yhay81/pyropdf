# Security policy

## Reporting a vulnerability

Report a vulnerability privately through
[GitHub security advisories](https://github.com/yhay81/pyropdf/security/advisories/new),
not in a public issue. Include the affected version, what an attacker can
do, and a document or code that shows it.

## Supported versions

pyropdf is in alpha: fixes go into the latest release only.

## Untrusted input

No pyropdf release claims safety for untrusted input before hayro's bounded
construction ([#15](https://github.com/yhay81/pyropdf/issues/15)) is
released. Until then, a PDF from an unknown source may exhaust memory or
time in the rendering and interpretation paths. `DocumentLimits` bounds the
work of each operation that pyropdf itself performs, but it is not that
claim. A way to make pyropdf exceed a limit that it does enforce, to crash
the interpreter, or to read or write outside a document is a vulnerability.
