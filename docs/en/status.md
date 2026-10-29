---
title: Status and safety
description: What the 0.1 alpha covers, what it does not yet, and what pyropdf does and does not claim about input safety.
---

# Status and safety

## Status

pyropdf is in **alpha**. The API may change between minor versions, and the
[changelog](https://github.com/yhay81/pyropdf/blob/main/CHANGELOG.md) lists
each change. It is the successor of
[pylopdf](https://github.com/yhay81/pylopdf), whose concept it keeps on a new
foundation, described in
[DESIGN.md](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md).

Not yet in 0.1:

- drawing on a page with `page.draw` and creating fonts, which wait for
  changes in krilla;
- free text and stamp annotations, which need drawing;
- the content provenance and interpreter fuel that depend on hayro changes
  under review.

Progress is tracked in the
[issue tree](https://github.com/yhay81/pyropdf/issues/1).

## Safety

No pyropdf release claims safety for untrusted input before hayro's bounded
construction ([#15](https://github.com/yhay81/pyropdf/issues/15)) is
released. Until then, treat a PDF from an unknown source as able to exhaust
memory or time in the rendering and interpretation paths.

`DocumentLimits` bounds the work of each operation that pyropdf itself
performs, and its codes are listed under
[Limits and errors](limits.md#limit-codes). It is not that claim.

Report a vulnerability privately through
[GitHub security advisories](https://github.com/yhay81/pyropdf/security/advisories/new).
