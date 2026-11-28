---
title: API stability
description: What counts as pyropdf's public API, what may change before and after 1.0, and how deprecations, limits, extras, and capability claims are governed.
---

# API stability

pyropdf follows [Semantic Versioning](https://semver.org/). This document states
both the current pre-1.0 rule and the 1.0 stability contract that will be frozen
when pyropdf 1.0 is released ([DESIGN.md §13](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md#13-phases)).

## Pre-1.0 status {#current-status}

pyropdf is currently **pre-1.0 (0.x)**. The stability contract below is **not**
yet frozen:

- Minor releases (`0.x.0`) may rename, reshape, or remove public APIs without a
  deprecation window. Per [DESIGN.md §10.1](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md#101-rules)
  (*One way*), pre-1.0 redesigns replace the old API in the same release rather
  than carrying compatibility aliases.
- Patch releases (`0.x.y`) aim to preserve API compatibility and focus on bug
  fixes, security fixes, and packaging updates.
- Every breaking change, together with its replacement, is recorded in the
  [changelog](https://github.com/yhay81/pyropdf/blob/main/CHANGELOG.md).
- Pin a minor series (for example `pyropdf~=0.4.0`) when you want to adopt
  minor-release changes on your own schedule.

The 1.0 stability guarantees in the sections below take effect when `1.0.0` is
released.

## Public API boundary {#public-api-boundary}

| Surface | Stability | Notes |
| --- | --- | --- |
| `pyropdf.__all__`, `pyropdf.__version__`, and `pyropdf.typed.__all__` | Public | Import from `pyropdf` or `pyropdf.typed`, not from internal submodules |
| Documented classes, methods, properties, functions, enums, and type aliases | Public | Parameter names, defaults, and positional vs. keyword-only kinds are part of the contract |
| Frozen record and specification dataclasses | Public | Field names and types are public; positional construction is not guaranteed unless documented |
| Exception classes, their hierarchy, `LimitError.code`, and `LIMIT_CODES` / `LimitCode` | Public | Programmatic error handling should rely on types and `LimitError.code`, not message wording |
| `METADATA_KEYS` and documented `Literal` option values | Public | New optional values may be added in minor releases |
| Private `_`-prefixed names, `pyropdf._core`, and undocumented modules | Internal | May change or be removed in any release without notice |
| `repr()` output, error message prose, and warning emission order | Internal | Intended for diagnostics, not machine parsing |
| Exact serialized bytes of saved PDFs, PNGs, and SVGs | Implementation detail | Visual rendering, extracted text, structural validity, incremental-save prefix preservation, and untouched-object losslessness are the guarantees |

## Semantic versioning after 1.0 {#after-v1}

From `1.0.0` onward:

- **Major releases (`X.0.0`)** may remove deprecated APIs or make breaking
  changes to signatures, types, or documented contracts.
- **Minor releases (`1.Y.0`)** add backwards-compatible capabilities, optional
  keyword-only parameters with defaults, new enum members or `Literal` option
  values, new `LIMIT_CODES` entries, or additional dataclass fields with
  defaults.
- **Patch releases (`1.Y.Z`)** fix bugs, regressions, and security or packaging
  defects without breaking public APIs.

Removing or renaming a public symbol, adding a required parameter, tightening
accepted input types, removing a field from a returned record, or altering the
exception inheritance hierarchy requires a major release. Type annotations are
part of the public API: breaking static-typing compatibility in public
signatures counts as a breaking change.

## Deprecation policy {#deprecation-lifecycle}

Once `1.0.0` is released, any public API scheduled for removal follows a four-step
lifecycle:

1. The documentation marks the API as deprecated, names its replacement, and
   states the earliest release eligible to remove it.
2. Calling or using the API emits Python's standard `DeprecationWarning`
   wherever practical.
3. The deprecated API remains functional for at least **two minor releases and
   six months** after the release that deprecates it.
4. Removal happens only in a subsequent **major** release.

`PyropdfWarning` reports issues in PDF files (such as unmapped glyphs or
repaired structures) and is never used for API deprecations. A security, legal,
or critical upstream emergency may require a shorter path; any such exception
is highlighted explicitly in the changelog and release notes.

## Limit codes and result changes {#limits-and-behavior}

Resource limits are registered in
[`limits.toml`](https://github.com/yhay81/pyropdf/blob/main/limits.toml),
exposed at runtime through `pyropdf.LIMIT_CODES` and `pyropdf.LimitCode`, and
documented in [Limits and errors](limits.md).

- Existing `LimitError.code` identifiers keep their names and documented
  meanings across minor and patch releases.
- New limit codes may be added in minor or patch releases when closing a
  resource-exhaustion gap.
- Tightening a default limit or adding a new guard may refuse a malformed or
  excessively expensive PDF that an earlier release attempted to process. Handle
  refusals by catching `LimitError` (or its base class `PdfError`) and
  checking `error.code` against `LIMIT_CODES`, never by matching message prose.
- Bug fixes may change extracted text, reading order, table cells, glyph boxes,
  rendered pixels, or damaged-file repair output in any release when the prior
  result was incorrect. Notable behavior changes are listed in the changelog.

## Optional extras and data packages {#extras-and-data}

Core `pyropdf` has zero runtime dependencies. Optional extras install separately
versioned dependencies or data packages:

| Extra | Package(s) | Versioning and compatibility |
| --- | --- | --- |
| `recipients` | `asn1crypto`, `cryptography` | Follows upstream SemVer ranges declared in `pyproject.toml`; required only for public-key CMS recipient encryption |
| `ocr` | `pylopdf-ocr-models` | Separately versioned model package pinned to a compatible series; model refreshes may improve recognition accuracy and boxes |
| `jp`, `ko`, `zh-cn`, `zh-tw` | `pylopdf-fonts-jp`, `pylopdf-fonts-ko`, `pylopdf-fonts-zh-cn`, `pylopdf-fonts-zh-tw` | Separately versioned Noto CJK fallback font packages; glyph updates may change fallback rendering of non-embedded CJK fonts |
| `layout` *(planned)* | Under evaluation ([#1199](https://github.com/yhay81/pyropdf/issues/1199)) | Will ship as a separately versioned optional extra if adopted, without adding required dependencies to the core wheel |

The core wheel keeps its own SemVer cadence independent of data-package updates;
incompatible data-package format changes require a compatible version range bump
in `pyproject.toml` and a release note.

## Supported Python versions and targets {#python-and-platforms}

- **Python floor:** `requires-python = ">=3.11"` (Python 3.11 through 3.15 are
  supported). Raising the minimum Python version happens only in a minor or
  major release after an upstream Python series reaches end of life, and is
  announced in the release notes.
- **Native wheels:** `abi3-py311` wheels for Linux (`x86_64`, `aarch64`),
  macOS (`arm64`, `x86_64`), and Windows (`x86_64`), plus a source distribution
  (`sdist`).
- **Free-threaded CPython:** Dedicated wheels for `3.14t` and `3.15t` on the
  five native targets (because `abi3` wheels do not load on free-threaded
  interpreters).
- **WebAssembly:** A `PyEmscripten` (`wasm32`) wheel targeting Pyodide and
  Cloudflare Python Workers; promoted to a 1.0 release gate under
  [DESIGN.md §13](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md#13-phases).

## Capability declarations and security claims {#capabilities-and-security}

- **Release capability declarations:** Each release that publishes a capability
  claim attaches a reproducible `capabilities.zip` archive bound to that exact
  tag and commit, naming its verified scope, operations, exclusions, external
  engines, optional runtimes, and checks requiring human review. See
  [Status and safety](status.md) and [Specification coverage](specification.md).
- **Input safety claim:** No pyropdf release claims safety for untrusted input
  until hayro's bounded construction
  ([#15](https://github.com/yhay81/pyropdf/issues/15)) is released. Until then,
  `DocumentLimits` and `DocumentLimits.web()` bound store and API work, while
  rendering and interpretation of PDFs from unknown sources should still run
  inside an isolated worker process
  ([Limits and errors](limits.md#reading-a-pdf-in-a-worker)).
- **Security disclosures:** Report vulnerabilities privately via
  [SECURITY.md](https://github.com/yhay81/pyropdf/blob/main/SECURITY.md).
- **Automated compatibility checks:** CI enforces public exports, signatures,
  generated API docs (`tools/api_reference.py --check`), `limits.toml` parity
  (`tests/test_limit_codes.py`), and specification-map evidence
  (`tests/test_spec_map.py`) on every pull request.
