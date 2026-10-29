---
title: Versions and saving
description: Versions, transactions, undo and redo, change reports, incremental and full saves, and encryption.
---

# Versions and saving

pyropdf keeps a document as a base, the file it opened, and a chain of
versions above it. An edit writes the objects it changes into a new version
and leaves the others where they are. Every reader, renderer, and save works
from the current version.

## Transactions

`doc.transaction(name)` groups the edits of its block into one step. If the
block raises, the document returns to the version before it, without a copy.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    try:
        with doc.transaction("rotate"):
            page.rotation = 90
            raise RuntimeError("changed my mind")
    except RuntimeError:
        pass
    print(page.rotation)  # 0: nothing of the block stayed
```

An edit outside a transaction is a step of its own.

## Undo and redo

`doc.history` lists the steps by name; `undo` and `redo` move between them.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("rotate"):
        page.rotation = 90
    with doc.transaction("note"):
        page.annotations.add(pyropdf.Note((40, 40), contents="Later"))
    print(doc.history.steps, doc.history.position)  # ('rotate', 'note') 2
    doc.history.undo()
    doc.history.undo()
    print(page.rotation, len(page.annotations))      # 0 0
    doc.history.redo()
    print(page.rotation)                             # 90
```

## Saving

`doc.save(path)` and `doc.to_bytes()` write the current version.

- **Incremental** (`incremental=True`) appends only the objects that the
  edits wrote, after the original bytes, which stay as they were. Digital
  signatures over the original bytes stay valid.
- **Full** (the default) rewrites the whole file. `compress`,
  `object_streams`, `prune`, `renumber`, and `dedupe` shape the result.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].rotation = 90
    doc.save("appended.pdf", incremental=True)
    doc.save("rewritten.pdf", compress=True, object_streams=True, prune=True)
```

A document saved without edits reproduces its input: an incremental save
byte for byte, a full save in every value.

## Change reports

Before saving, `doc.changes` says what the edits would invalidate: the
signatures whose signed bytes change, and the PDF/A, PDF/UA, and similar
claims that the edits may break.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].rotation = 90
    report = doc.changes(incremental=False)
    print(bool(report), report.signatures, report.claims)
```

## Encryption

`Encryption` sets passwords, permissions, and the method, AES-256 by
default. `pyropdf.open(..., password=...)` or `doc.unlock` opens an
encrypted document.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.save(
        "locked.pdf",
        encryption=pyropdf.Encryption(user_password="reader", owner_password="owner"),
    )

with pyropdf.open("locked.pdf", password="reader") as doc:
    print(doc.encryption_method, doc.permissions)
```
