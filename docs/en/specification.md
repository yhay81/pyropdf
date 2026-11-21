---
title: Specification coverage
description: How much of ISO 32000-2 pyropdf reads, interprets, creates, edits, and preserves, clause by clause.
---

# Specification coverage

A dimension is `done` only when a test marked `@pytest.mark.spec` proves it, so
`planned` includes implemented features whose specification tests have not been
tagged yet.

For a released version, select its tag on the [releases page](https://github.com/yhay81/pyropdf/releases)
and open `capabilities.zip`. The release notes link the same archive. Its report names the
processor role, the tested operations, exclusions and limitations. Input profiles, external
engines and optional runtimes are identified separately. Version 0.3.1 predates these
archives and has no published verified scope.

--8<-- "spec-coverage.md:3:"
