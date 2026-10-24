# pyropdf foundation design

Status: proposal, 2026-10-24. Nothing described here is implemented.

This document fixes what pyropdf builds before any feature: one versioned
object store that every engine reads, the contracts it needs from the
engines, and the order of work. It records the evidence from pylopdf that
motivates the rebuild and the plan for moving pylopdf's users and code.

## 1. Concept

pyropdf keeps the concept of [pylopdf](https://github.com/yhay81/pylopdf)
and changes its foundation.

- **Complete control.** Every feature of PDF as ISO 32000-2:2020 defines it,
  with its errata and extensions and the PDF/A and PDF/UA profiles, is read
  without loss, interpreted, created, edited, and preserved. A specification
  map records each feature in each of these five dimensions with the tests
  that prove it. pymupdf's capabilities are the floor, not the goal.
- **Consistent edits.** An edit keeps every structure that depends on what it
  changes valid, or refuses. What it cannot keep valid, such as a signature
  or a conformance claim, it reports before the document is saved.
- **An essential API.** One way to do each task, typed values, standard
  Python protocols, and one coordinate space: display space, measured from
  the top-left corner of the visible page with `/Rotate` and the CropBox
  applied. pylopdf's API rules
  ([api/DESIGN.md](https://github.com/yhay81/pylopdf/blob/main/api/DESIGN.md),
  section 1) are pyropdf's starting rules.
- **Engine-faithful.** hayro interprets and renders, krilla writes, and work
  that an engine owns lands upstream first. pyropdf builds the store, the
  binding, the resource policy, the consistency of edits, and document
  understanding.
- **Trustworthy.** Hostile input meets typed, bounded refusal. Results carry
  their provenance, independent validators check what pyropdf writes, and
  benchmarks publish losses with wins.
- **Everywhere.** GIL-enabled and free-threaded CPython, Pyodide and
  Cloudflare Python Workers, small wheels, and the MIT license.

What changes is underneath. pylopdf holds three object models: lopdf's
editable graph, hayro's parse of file bytes, and the PDFs that krilla
generates. It reconciles them after the fact. pyropdf has one store, and
every engine reads and writes through it.

## 2. Evidence from pylopdf

### 2.1 Measurements

Two runs on 2026-10-24, on macOS arm64 with CPython 3.14.8 and the pylopdf
0.15.0 extension of a development environment; where they differ, both
results are given. A single fill is the best of seven rounds, and loops ran
once per size in each run. The code paths named in the last column are
unchanged on pylopdf's main at `884af0db`. The scripts are in
[evidence/pylopdf-baseline](evidence/pylopdf-baseline). The larger
documents repeat pylopdf's `tests/assets/real_world/bill-hr815.pdf` (110
pages) through `doc.pages.extend`, and each is written with `to_bytes()` and
opened again before measuring.

| Workload | Result | Cause in pylopdf |
| --- | --- | --- |
| Rotate every page, then read its text | 110 pages: 0.16 to 0.17 s, against 0.09 to 0.10 s for reading alone. 440 pages: 1.48 to 1.87 s, against 0.29 to 0.32 s. 1,760 pages: 28.68 to 30.41 s, against 1.29 to 1.59 s, 18 to 24 times as long. | Every edit drops hayro's snapshot, the content guard's results, and every derived cache (`invalidate_hayro_pdf_keeping_placements`), and the next read serializes the whole lopdf graph and parses it again (`hayro_view`). |
| Fill one text field | 2.17 to 2.19 ms with 2,626 objects, 6.39 to 6.58 ms with 8,646 objects, when only pages without fields were added | Field edits clone the whole lopdf document so that they can roll back on failure (seven sites in `forms.rs`). |
| Read the value of every field | 7.7 ms for 100 fields, 118 to 126 ms for 400, and 2.18 to 2.24 s for 1,600, while one walk of the form takes 0.07, 0.27 to 0.31, and 1.22 to 1.25 ms | A `Field` handle walks the whole form on every property access to find itself (`Field._facts`). |
| Stamp text in one OpenType face on 100 pages | 100 embedded font programs and 330 KiB. `dedupe=True` merges them only when every page has the same text (29.6 KiB); with different text it leaves 82.7 KiB | Each `page.draw()` commit generates its own krilla document, so each commit embeds its own subset. |

### 2.2 Structure

Counted on pylopdf's main at `884af0db` (2026-10-24):

- About 5,500 lines of Rust exist because an engine lacks a bound, an API,
  or linear-time behavior, as pylopdf's roadmap counts them. They include
  the nesting guards and the scanners behind them (2,477 lines), the
  reconciliation of how lopdf and hayro read malformed files
  (`object_recovery.rs`, 1,498 lines), and a parser for krilla's output on
  the calling thread (`generated_pdf.rs`, 756 lines).
- Three tokenizers read content streams. hayro-syntax serves rendering,
  extraction, and the content engine, which keeps each operation's bytes
  since yhay81/pylopdf#405. lopdf's content parser serves text
  replacement. And `content_scan.rs` models hayro's tokenizer in order to
  refuse what would crash it.
- 67 call sites choose by hand how much an edit invalidates.
- Incremental save (yhay81/pylopdf#403, 2026-10-24) reads the original
  bytes again and compares every object of the graph with that baseline,
  because no editing method reports what it changed. Transactions and undo
  (yhay81/pylopdf#415, 2026-10-24) keep complete copies of the graph.
- One `#[pymethods]` block holds the 186 methods of `_Document`.
- About 60 `LimitError` codes and 421 hand-placed fallible reservations
  implement the resource policy.
- lopdf stores real numbers as `f32`, so every save rewrites `35.6812345`
  as `35.681236`.
- The development context (`AGENTS.md`) is 249 KB, much of it rules for
  keeping the engines and caches consistent.

On damaged input, hayro alone rendered 103 of 112 inputs (14 files under 8
kinds of damage), while pylopdf with lopdf 0.46 read the first page of 78
(pylopdf's roadmap, evidence log).

### 2.3 Diagnosis

pylopdf's costs come from one decision. lopdf's graph is the editable source
of truth, hayro reads an immutable snapshot of it, and every edit discards
that snapshot. As a result:

- each edit costs as much as the document;
- each difference between the two readers becomes pylopdf code;
- each optimization must prove which caches an edit may keep.

Incremental save diffs the whole graph, transactions copy it, and
invalidation drops everything derived from it. These are separate
mechanisms, and each pays for the whole document. The items on pylopdf's
roadmap that would fix this are central change tracking, exact real
numbers, interpretation over a source that is not file bytes, and
provenance. They are parts of one mechanism, and pyropdf builds that
mechanism first.

## 3. Foundation principles

- **F1 One store.** Every engine reads the current version of the document
  through the store. Nothing parses a serialized copy of it.
- **F2 Versions, not snapshots.** A version is immutable. A transaction
  produces the next version at a cost proportional to what it writes; a
  failed transaction is discarded.
- **F3 Lossless by construction.** A save writes every object that no edit
  touched with the bytes it was read from. An edited object keeps the
  lexemes of the values that the edit did not change.
- **F4 Derived results record what they read.** A cached result stays valid
  while nothing it read has changed. No editing method declares what it
  invalidates.
- **F5 One parser, one interpreter.** One iterative, bounded parser reads
  objects and content, and one interpreter, whose items carry provenance,
  interprets them. A bound refuses with an error; it never truncates silently
  or exhausts the stack.
- **F6 One budget per operation.** Bytes, elements, decoded amplification,
  interpreter fuel, a deadline, and cancellation are counted in one place.
- **F7 Identity, not position.** Handles name objects, and indexes and
  positions are computed from the current version.
- **F8 Schema-driven objects.** The Arlington PDF Model generates the typed
  object layer and the structural check.
- **F9 Evidence in the repository.** Property tests, differential oracles,
  fuzzers, and benchmarks are committed and run in CI.

## 4. The store

### 4.1 Base, versions, and transactions

A document is a base and a chain of versions.

- The **base** holds:
  - the original bytes, shared and never copied;
  - an index from object number to location: a byte offset, or a member of
    an object stream;
  - the security handler;
  - the revisions that the file contains;
  - the repair report.

  Opening a document builds the index and nothing else.
- A **version** holds:
  - the base;
  - a persistent map from object number to a written object (its serialized
    bytes) or a free entry;
  - the trailer and the next free object number;
  - the object numbers that its transaction wrote.

  Versions share structure, so a new version costs time and memory in
  proportion to what its transaction wrote.
- A **transaction** starts from the current version, collects writes, and
  commits by publishing the new version. A refusal or an error drops the
  transaction; nothing is cloned and nothing needs rolling back. Undo
  republishes an earlier version, within a memory budget.
- The **change set** between two versions is the union of the object numbers
  that their transactions wrote. Saving, invalidation, and reports all read
  it.

hayro-syntax parses every object on demand from its bytes, in the base or in
a version (F5), so the editor and the interpreter see the same object for
every number. A parsed object is cached by object number. Base objects never
change, and a written object is immutable.

### 4.2 Values and losslessness

Objects reach the editing API as owned values: null, booleans, integers,
reals, names, strings, arrays, dictionaries, streams, and references. Each
value read from bytes keeps its source span, so an unchanged value is
written back as its original lexeme:

- a real stays `35.6812345`;
- a string keeps its literal or hexadecimal form;
- a dictionary keeps its key order.

A changed real is written with the shortest representation that round-trips
its `f64`. A stream keeps its encoded data until a caller replaces the data
or asks for recompression.

### 4.3 Index and repair

Opening checks every uncompressed in-use cross-reference entry against the
object header it points to. When the cross-reference data cannot be read or
an entry fails the check, the store rebuilds the index once by scanning
object headers, and records the repair. An object stream's member table is
checked against the entries that name the stream when the stream is first
decoded, and a mismatch resolves by one fixed rule. Repair therefore never
depends on the order of reads, and no consumer repairs on its own. A
repaired base refuses incremental save, because its offsets are not the
file's.

### 4.4 Saving

- **Incremental save** writes the base bytes unchanged, then the objects of
  the change set since the base, a cross-reference section that links to the
  base's, and a trailer. The bytes that existing signatures cover stay
  unchanged, and before anything is written the change set tells which
  signed objects an edit has touched.
- **Full save** writes every live object. An untouched base object is copied
  from its bytes (from its decoded member bytes for object streams), and a
  written object is copied as written. Renumbering, deduplication, object
  streams, and recompression happen only when an option asks for them.
- Saving with the same encryption copies encrypted bytes as they are.
  Changing or removing encryption decrypts and encrypts values again, which
  keeps every value exactly.

### 4.5 Encryption

The store decrypts strings and streams lazily as they are read, through the
base's security handler, and encrypts them on write. It writes AES-256
(revision 6) and AES-128, and reads RC4 but never writes it. New objects stay
plaintext in memory until a save encrypts them.

### 4.6 Concurrency

A `Document` holds its current version behind an atomic pointer. A read takes
the current version and works on it without locks. Writes serialize through
one writer lock and publish new versions. On every runtime with threads,
free-threaded CPython included, reads on one document can run in parallel,
and a long render keeps reading the version it started with while edits
publish newer ones.

## 5. Derived results

Every derived result is memoized together with the set of object numbers
that computing it read, which the store's resolver records. Derived results
include:

- page interpretations, and text and table pages;
- rendering caches (fonts, images, and glyphs);
- the page index and the form index;
- outline and name-tree indexes;
- link resolution and placement reuse.

A result computed on version X is valid on version Y when the change set from
X to Y is disjoint from its read set. Settings that change what a computation
produces, such as fallback fonts, are part of its memo key.

This replaces pylopdf's hand-chosen invalidation, its 67 call sites and the
rules that classify each editing method, with one mechanism that is correct
for a new editing method without any declaration. Versions (section 4), read
sets (this section), document resources (section 7), and identity handles
(section 10) together make each workload of section 2.1 cost in proportion to
what it changes or reads, not to the size of the document, once hayro reads
versions directly (I1 in section 6).

## 6. Interpretation contract

pyropdf interprets through hayro-interpret and parses through hayro-syntax.
It needs the following from them, and contributes each item upstream first.
pylopdf's guards and scanners record precisely what hayro must bound; that
analysis goes into upstream patches, not into pyropdf.

| Item | Requirement |
| --- | --- |
| I1 Object source | hayro-syntax resolves objects through a source that the caller supplies (a store version), not only through file bytes. |
| I2 Bounded construction | Colour spaces, functions, Type 4 programs, and nested content and objects are built and read without unbounded recursion, and refuse with an error beyond their bounds instead of truncating. |
| I3 No panics | Malformed input yields errors, not panics (LaurenzV/hayro#1546 and the rest). |
| I4 Provenance | Begin and end of every content stream, Form XObject, annotation appearance, tiling pattern, and Type 3 glyph procedure; the operator index and byte span of every painted item; the resource names it used; a callback for `sh`; marked-content properties; colour spaces and components; font and image identity; image-mask streams. |
| I5 Fuel and cancellation | A budget charged per operator, per function evaluation, and per rasterized area, with a cancellation check. |
| I6 Linear time | Object stream member tables parsed once (LaurenzV/hayro#1545), a linear search for the end of inline image data, and an operand stack that holds the 33 operands of a DeviceN `scn`. |
| I7 Spans | The byte span of every parsed object and dictionary entry, which F3 needs. yhay81/pylopdf#405 already keeps each content operation's bytes over hayro's tokenizer. |
| I8 Object numbers | Object numbers read as `u32`. |

hayro-syntax 0.8 keeps its cross-reference reader, its repair, and its
decryption crate-private (`root_xref`, `fallback`, `repair`, and the `crypto`
module), and `Pdf::new` resolves the whole page tree. Its `FromBytes` trait,
which parses an object from a byte slice, is public. Until I1, the store
therefore reads cross-reference data and decrypts on its own, and parses
every object through `FromBytes`.

Until I1 lands, the store hands hayro its current version as an in-memory
incremental update: the base bytes, followed by the written objects (already
serialized) and a cross-reference section. hayro-syntax reads that format
natively. When the base was repaired, that section lists every object at the
offsets the store chose, so hayro never repairs on its own. A new version
then costs hayro a fresh read of the cross-reference data and the page tree,
but no serialization or parse of unchanged objects. That remaining cost still
grows with the document; I1 removes it. For an encrypted base, this bridge
must encrypt the written objects with the document's key.

No pyropdf release claims safety for untrusted input before I2 is released.
If the upstream patch cannot land, pylopdf's guards come over as a listed
workaround that links the upstream discussion.

## 7. Writing contract and settling

| Item | Requirement |
| --- | --- |
| W1 Object sink | krilla writes content, fonts, images, and Form XObjects into a sink that the caller supplies, with object numbers that the sink allocates, instead of a complete PDF that pyropdf parses and imports. |
| W2 Document resources | A long-lived resource context per document: a font or image is registered once, glyphs accumulate across drawings, and fonts are subset when the caller finalizes them, which pyropdf does at save. Glyph IDs stay stable across finalization, so content written earlier needs no rewrite. |
| W3 Standard 14 | The Standard 14 fonts drawn without embedding. |
| W4 Instance names | Subset fonts named after the variable-font instance they embed. |

Until a save, a font in use is a provisional object with its complete
program, so rendering and extraction see the right glyphs; the save replaces
it with the subset. Stamping one face on 100 pages therefore embeds one
program.

Dependent structures are settled rather than rebuilt by every call. A
transaction marks what its writes make stale: appearance streams, outline
counts, the structure tree's parent tree, name- and number-tree limits, and
font subsets. Settling runs:

- before any read that depends on a stale structure;
- at the end of every top-level transaction;
- before a save, which is the only point where font subsets settle.

A program never observes an unsettled structure, so pylopdf's rule that
setters apply immediately keeps its meaning, while three setters on one
annotation draw its appearance once.

## 8. Typed objects and referential rules

A generator reads a pinned release of the Arlington PDF Model (Apache-2.0,
credited in a NOTICE file) and produces three things:

- a descriptor for every object type: its keys and their types, which keys
  are required or inheritable, the PDF version that introduced or deprecated
  each key, the possible values, and the types each key links to;
- Python typed views with stubs;
- the structural check.

The check evaluates the descriptors and the predicates that can be decided
without rendering, and reports each departure with its object and clause.

The typed layer fills the specification map's Read dimension, and the plain
cases of Create and Edit, mechanically. Hand-written code is left for
meaning: appearances, forms, the structure tree, and page operations.

The same link information builds a reverse-reference index per version,
maintained from change sets. It records which objects refer to an object,
under which key, and in which role. A structural edit runs the rules
registered for the roles that refer to what it removes or moves:

- remove the entry;
- make the destination unresolved;
- remove the referrer;
- point the reference elsewhere.

pylopdf's page cleanup, structure-tree pruning, and form import (3,654 lines
of operation-specific code) become such rules, and a new feature adds rules
instead of revisiting every structural operation.

## 9. Budgets and errors

- An operation's `Budget` comes from `DocumentLimits` and the call's `max_*`
  options. It counts allocated bytes, elements, decoded bytes, and
  interpreter fuel, and checks a deadline and a cancellation token.
  Collections allocate from budgeted arenas, so fallible growth is the
  default instead of hundreds of hand-placed reservations.
- Limit codes come from one registry. It generates the Rust enum, the stable
  `LimitError.code` strings, and the security guide's table.
- The binding borrows Python buffers and strings and checks their sizes once,
  in Rust, before any copy.
- Opening reads only the index, so damage and limits surface when a part is
  first read. `doc.validate()` runs the complete checks up front, and
  `DocumentLimits.web()` runs them at open, which keeps pylopdf's guarantee
  for untrusted uploads.
- Core crates return typed errors. The binding maps them to pylopdf's
  hierarchy, which carries over: `PdfError`, `MalformedPdfError`,
  `PasswordError`, `EncryptedDocumentError`, `LimitError`,
  `StaleObjectError`, `DocumentClosedError`, and `OcrError`.

## 10. Python API changes

pylopdf's API rules carry over. The foundation changes these behaviors:

- **Handles.** A handle is a document and an object number. Its facts are
  cached per version, and when the version moves it reads its own object
  again, never the whole structure around it. A `Page` names its page
  object, so it stays valid when other pages are inserted, moved, or deleted,
  and `page.index` costs constant time.
- **Transactions.** `with doc.transaction():` groups edits into one version
  and one undo step, at a cost that does not depend on the document's size.
- **Revisions.** `save(incremental=True)` writes only the change set, and the
  file's revisions open as documents of their own.
- **Change reports.** Before a save, a report lists what the edits since
  opening invalidate: the signatures whose objects they touched, and the
  PDF/A or PDF/UA claims they break.
- **Glyph-level results.** Characters and content items are columnar buffers
  that NumPy and Arrow read without copying; records are views over them.
- **Typed objects.** The layer of section 8 sits between the raw object model
  and the domain APIs.
- **Threads.** Several threads may read one document.
- **Limits.** `DocumentLimits` adds a time budget and cancellation.

A sketch, with provisional names:

```python
doc = pyropdf.open("contract.pdf")         # reads the cross-reference data only
page = doc.pages[3]
doc.pages.insert(0, cover.pages[0])
assert page.index == 4                      # the handle follows its page object

with doc.transaction():                     # one version, one undo step
    page.rotation = 90
    doc.form["name"].value = "Ada Lovelace"

print(doc.changes().signatures)             # signed objects that the edits touched
doc.save("contract.pdf", incremental=True)  # writes only what changed
```

## 11. Layout

```
crates/
  pyropdf-store    objects, index and repair, versions, change sets,
                   writer, security handler                        (hayro-syntax)
  pyropdf-schema   typed object descriptors generated from the Arlington PDF Model
  pyropdf-engine   content model and provenance, rendering, writing,
                   understanding, budgets                          (hayro, krilla, RTen)
  pyropdf-python   the PyO3 binding, the only crate that depends on PyO3
python/pyropdf     the Python API
tests              Python tests: the public contract
fuzz, oracles      fuzz targets and differential oracles, run in CI
evidence           measurements and the scripts that produce them
```

Dependencies point down the list and never up. The store depends on no
interpreter or writer, and no crate but the binding names PyO3. A crate
splits further only when compile times or dependency boundaries call for it.
Packaging follows pylopdf: maturin, one `abi3-py311` wheel per platform,
free-threaded wheels for 3.14t and 3.15t, and a PyEmscripten wheel. Fonts and
OCR models stay separate data packages.

## 12. Verification

- Python tests are the public contract, as in pylopdf.
- Property tests for the store:
  - without edits, an incremental save reproduces the input byte for byte,
    and a full save reproduces every value exactly;
  - random sequences of transactions keep the invariants of versions: change
    sets, undo, and atomicity on failure.
- Differential tests against frozen pylopdf (section 14) on the corpus cover
  every ported capability, and each difference is recorded as an
  improvement or a regression. pymupdf, pdf.js, and pypdfium2 serve as
  development-only oracles where a comparison means something.
- Fuzz targets cover the index, repair, the hayro bridge, the writer, and
  every new parser.
- The four workloads of section 2.1 become regression benchmarks, each at
  three document sizes.
- pylopdf's real-document corpus comes over with its license records.

## 13. Phases

### Phase 0: Foundation

Scope:
- the store, index and repair, versions, transactions and undo, and change
  sets;
- incremental and full save, and the security handler;
- read-set memoization, budgets, typed errors, and the hayro bridge;
- a binding with `open`, `Document()`, `probe`, the low-level object model,
  `doc.pages` with page identity, `page.pixmap`, `save`, and `to_bytes`;
- upstream: I2 and I7 released, and I1, I6, and I8 proposed.

Exit:
- without edits, saves reproduce every corpus document: incremental saves
  byte for byte, full saves in every value;
- an edit followed by a page read serializes and parses only the objects
  the edit wrote, apart from hayro's re-read of the cross-reference data and
  the page tree (section 6), and the benchmark publishes its cost at 110,
  440, and 1,760 pages;
- a failed transaction leaves the current version unchanged without copying
  the document;
- the fuzz targets run in CI;
- I2 is released, or releases withhold the untrusted-input claim.

### Phase 1: Reading

Scope:
- the content model with provenance (I4), and fuel (I5);
- text, words, layout, search, tables, Markdown, and OCR ported from
  pylopdf;
- rendering and SVG.

Exit:
- differential parity with frozen pylopdf on the corpus, with differences
  documented;
- every content item names its stream and operators.

### Phase 2: Writing and interaction

Scope:
- the canvas on W1 and W2, and settling;
- appearances, annotations, links, and forms and widgets with identity
  handles;
- page operations with referential rules.

Exit:
- stamping one face on 100 pages embeds one font program;
- reading every value of a 1,600-field form costs about one walk of the
  form;
- pylopdf's drawing, annotation, form, and page-operation tests pass after
  migration.

### Phase 3: Typed objects

Scope: the Arlington generator, the structural check, and the specification
map.

Exit:
- every Arlington object maps to a descriptor;
- the check finds every defect of a corpus broken on purpose.

### Phase 4: Specification

Scope: pylopdf's remaining roadmap milestones, on this foundation.

Exit: pylopdf's v1.0 criteria.

## 14. Moving from pylopdf

1. **Freeze.** pylopdf takes no new features after a freeze commit, which is
   the reference for differential tests. It takes security fixes until it is
   archived, and its README and roadmap point here.
2. **Name.** The first real alpha of `pyropdf` is published early through
   Trusted Publishing, because a pending publisher does not reserve a name
   until it publishes.
3. **Issues.** The open pylopdf issues worth keeping move here by issue
   transfer before pylopdf is archived, because an archived repository is
   read-only. They include the roadmap tree (yhay81/pylopdf#325) and engine
   work (yhay81/pylopdf#324).
4. **Porting.** A module comes over with its Python tests and names the
   pylopdf commit it came from.
   - Ported:
     - the understanding layer (reading order, tables, Markdown, and OCR);
     - canvas recording and textbox layout;
     - the annotation appearance engine and form semantics;
     - the content syntax that keeps each operation's bytes
       (yhay81/pylopdf#405);
     - geometry and `Pixmap`;
     - the migration tooling, the documentation, the benchmarks, and the CI
       and release workflows.
   - Not ported:
     - the guards;
     - the reconciliation between lopdf and hayro;
     - the replacements for lopdf's helpers;
     - the rules of pylopdf's AGENTS.md. Its measured facts move into this
       repository's evidence.
5. **Data packages.** `pylopdf-fonts-*` and `pylopdf-ocr-models` hold data
   only and can serve as dependencies at first. They are republished under
   `pyropdf-` names when a release changes them.
6. **Migration.** pylopdf's codemod learns to rename imports, so code
   written for any pylopdf release, and pymupdf code, migrates in one step.
7. **Archive.** pylopdf is archived when two conditions hold:
   - pyropdf opens, extracts, renders, reorganizes pages, and saves at least
     as well as frozen pylopdf;
   - the codemod migrates pylopdf's example programs.

   pylopdf then publishes a final release whose description points here,
   and its repository and PyPI project are archived. PyPI's archived status
   keeps existing releases installable, blocks new uploads, and can be
   reversed. The documentation site becomes a migration notice.

## 15. Risks

- **A second system.** A rebuild delays features and can stall both
  projects. Phase 0 is small and gated by measurable exits, and pylopdf
  remains the oracle and the fallback until the archive gate.
- **Upstream dependence.** I1, I2, and W1 to W4 need the maintainers'
  agreement. The bridge removes the need to wait for I1, and pylopdf's
  guards are the fallback for I2.
- **Later failures.** Lazy opening moves some refusals from `open` to first
  use. `validate()` and the web profile keep rejection at open available.
- **Overhead.** Persistent maps and read-set recording add constant costs,
  which the benchmarks track.
- **WebAssembly.** Under Pyodide a stack overflow is fatal to the runtime,
  and there are no threads that could give interpretation a larger stack, so
  I2 matters most there.
- **Name.** Searches for "pyro" find Pyro (Python Remote Objects) and Pyro
  (probabilistic programming). As of 2026-10-24, "pyropdf" itself is unused
  on PyPI and GitHub.

## 16. Open questions

- Whether the store's write side belongs upstream in hayro-syntax or stays
  in pyropdf.
- The persistent map: a library such as `rpds` or `im`, or copy-on-write
  chunks of a flat table.
- How Python sees reals whose lexemes are kept: a `float` subclass, or a
  plain `float` with the lexeme kept behind it.
- Which Arlington predicates the structural check implements first.
- How the bridge handles encrypted bases before I1.
- What the change report is: a method, a value that `save` returns, or a
  refusal that an option turns into a report.
- The memory budget of undo, and how handles behave across undo.
