# pyropdf

pyropdf is the planned successor of [pylopdf](https://github.com/yhay81/pylopdf):
a Python library, implemented in Rust, for complete control of PDF as
ISO 32000-2 defines it. It keeps pylopdf's concept and replaces its
foundation with one versioned object store that every engine reads.

**Status:** design. Nothing is implemented or published yet.
[DESIGN.md](DESIGN.md) describes the foundation that comes first, the
evidence from pylopdf behind it, and how pylopdf's users and code move over.
[evidence/pylopdf-baseline](evidence/pylopdf-baseline) holds the scripts
behind that evidence.

## Safety claims

No pyropdf release claims safety for untrusted input before hayro's bounded
construction (I2, [#15](https://github.com/yhay81/pyropdf/issues/15)) is
released. `DocumentLimits` bounds the work of an operation, but it is not
that claim.
