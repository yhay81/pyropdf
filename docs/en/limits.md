---
title: Limits and errors
description: See which work DocumentLimits checks, how to read a PDF in a bounded worker, and how to handle pyropdf's errors.
---

# Limits and errors

## Limits

`DocumentLimits` checks file size, page and object counts, and the work
pyropdf meters during each operation. When a check refuses the operation,
`LimitError.code` identifies the limit.

```python
import pyropdf

limits = pyropdf.DocumentLimits(max_pages=1_000, max_decompressed_size=64 * 2**20, max_duration=10.0)
try:
    with pyropdf.open("input.pdf", limits=limits) as doc:
        print(doc.pages[0].text())
except pyropdf.LimitError as error:
    print("refused:", error.code)
```

`DocumentLimits.web()` checks every object when the document opens. It
sets a 10 MiB limit for a page content stream and a 64 MiB limit for an
individual stream decoded through the store. A PDF can pass those checks
and still expand to a much larger size during text extraction or rendering.
The profile leaves `max_memory_size` and `max_duration` unset.

A `CancellationToken` lets another thread request cancellation. pyropdf
checks the token when it polls the operation's budget.

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

## Reading and rendering

`page.text()`, `page.words()`, `page.paths()`, `page.content()`, and
`page.pixmap()` all use hayro to interpret the page. The table applies to
all five methods.

| Field | What pyropdf checks | What can exceed the limit |
| --- | --- | --- |
| `max_page_content_size` | The size of a page content stream after the store decodes its filters, for example for `page.operations`. | hayro decodes the page's content directly for these five methods. |
| `max_decompressed_size` | The decoded size of an individual stream read through the store. A filter can allocate its output before the store checks its size. | Streams decoded directly by hayro bypass this check, including images decoded during rendering. |
| `max_memory_size` | Allocations that pyropdf charges to the operation's memory budget. | hayro's working memory and raster buffers can take the process over this limit. |
| `max_duration` | Elapsed time at pyropdf's budget checks. | The deadline can pass while hayro is decoding, interpreting, or painting. pyropdf can report it only when it next checks. |

The glyph and text limits on `text()` and `words()`, the element count on
`paths()` and `content()`, and the raster dimensions on `pixmap()` limit
what each call collects or returns. Even a page that produces little
output can take substantial work to interpret. Upstream work covers
[bounded stream decoding (#48)](https://github.com/yhay81/pyropdf/issues/48),
[interpreter budgets and cancellation (#105)](https://github.com/yhay81/pyropdf/issues/105),
and [bounded construction (#15)](https://github.com/yhay81/pyropdf/issues/15).

## Reading a PDF in a worker

When a user sends a PDF to your application, open it and perform the page
operation in a separate process. This Linux example counts the words on
the first page of `input.pdf`. `prlimit`
(util-linux) caps the worker's virtual address space at 1 GiB before Python
starts. GNU `timeout` kills the worker after 10 seconds of wall time.

```sh
--8<-- "includes/read-in-worker.sh"
```

Choose limits for your workload and treat a nonzero exit as a failed
request. The same worker can call `pixmap()` when the request needs a
rendered page. See also [Status and safety](status.md#safety).

## Limit codes

--8<-- "limit-codes.md"

## Errors

Every error that pyropdf raises derives from `PdfError`:

| Error | Raised when |
| --- | --- |
| `MalformedPdfError` | The input is not a readable PDF. |
| `InternalError` | Rust panics in pyropdf or an engine; the message retains the panic. Please report this library defect. |
| `PasswordError` | A password is wrong. |
| `EncryptedDocumentError` | A locked document is read before `unlock`. |
| `LimitError` | An operation reaches a limit or is cancelled; `code` names it. |
| `StalePageError`, `StaleObjectError` | A handle outlives its page or object. |
| `DocumentClosedError` | A document is used after `close`. |
| `OcrError` | OCR models are missing or fail. |

`InternalError` is caught by both `except PdfError` and `except Exception`.
Failed transactions are discarded, and the same document remains usable.
The process panic hook is unchanged: its default diagnostics still go to
stderr. The boundary catches unwinding panics; it cannot recover from an
aborting runtime.
