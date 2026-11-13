---
title: Status and safety
description: What pyropdf 0.3 covers, what it does not yet, and what pyropdf does and does not claim about input safety.
---

# Status and safety

## Status

The API may change between minor versions, and the
[changelog](https://github.com/yhay81/pyropdf/blob/main/CHANGELOG.md) lists
each change. [DESIGN.md](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md)
describes its foundation, and [Specification coverage](specification.md)
how much of ISO 32000-2 each part covers.

Not yet in 0.3.0:

- redaction, and removing the content outside a rectangle, which need the
  content provenance of a hayro change under review;
- the interpreter fuel and cancellation that would bound rendering and
  interpretation, also a hayro change under review;
- pixmaps in CMYK, and selectable text in SVG, which wait on hayro.

Progress is tracked in the
[issue tree](https://github.com/yhay81/pyropdf/issues/1).

## Safety

No pyropdf release claims safety for untrusted input before hayro's bounded
construction ([#15](https://github.com/yhay81/pyropdf/issues/15)) is
released. Until then, treat a PDF from an unknown source as able to exhaust
memory or time in the rendering and interpretation paths.

For documents supplied by users, see [which limits apply during page reading and rendering](limits.md#reading-and-rendering)
and [how to enforce memory and time limits in a worker](limits.md#reading-a-pdf-in-a-worker).

Report a vulnerability privately through
[GitHub security advisories](https://github.com/yhay81/pyropdf/security/advisories/new).
