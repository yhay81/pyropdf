---
title: Limits and errors
description: Bound the work of every operation with DocumentLimits, cancel it, and handle pyropdf's typed errors.
---

# Limits and errors

## Limits

`DocumentLimits` bounds the work that a document's operations may do: the
file size, the page and object counts, decoded stream sizes, the input to
the interpreter, and time. An operation that would pass a limit stops and
raises `LimitError`, whose `code` names the limit.

```python
import pyropdf

limits = pyropdf.DocumentLimits(max_pages=1_000, max_decompressed_size=64 * 2**20, max_duration=10.0)
try:
    with pyropdf.open("input.pdf", limits=limits) as doc:
        print(doc.pages[0].text())
except pyropdf.LimitError as error:
    print("refused:", error.code)
```

`DocumentLimits.web()` is a stricter profile for documents that arrive from
outside, and it checks every object when the document opens.

A `CancellationToken` stops the operations of a document from another
thread:

```python
import pyropdf

token = pyropdf.CancellationToken()
limits = pyropdf.DocumentLimits(cancellation=token)
with pyropdf.open("input.pdf", limits=limits) as doc:
    token.cancel()
    try:
        doc.pages[0].text()
    except pyropdf.LimitError as error:
        print(error.code)  # cancelled
```

Limits bound work. They are not a claim that pyropdf is safe for any input;
[Status and safety](status.md) says what is and is not promised.

## Limit codes

--8<-- "limit-codes.md"

## Errors

Every error that pyropdf raises derives from `PdfError`:

| Error | Raised when |
| --- | --- |
| `MalformedPdfError` | The input is not a readable PDF. |
| `PasswordError` | A password is wrong. |
| `EncryptedDocumentError` | A locked document is read before `unlock`. |
| `LimitError` | An operation reaches a limit or is cancelled; `code` names it. |
| `StalePageError`, `StaleObjectError` | A handle outlives its page or object. |
| `DocumentClosedError` | A document is used after `close`. |
| `OcrError` | OCR models are missing or fail. |
