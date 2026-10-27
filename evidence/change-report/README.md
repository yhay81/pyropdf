# The shape of the change report (#157)

DESIGN.md section 10 lists change reports: before a save, what the edits
since opening invalidate, namely the signatures whose objects they touched
and the PDF/A or PDF/UA claims they break. Section 16 leaves the shape
open between a method, a value that `save` returns, and a refusal that an
option turns into a report. This directory holds the evidence for the
recommendation on #157. It builds nothing.

## What the report speaks about

`survey.py` reads every document of `tests/assets` with pyropdf at
`origin/main` 0040c0c; its output is [`survey.md`](survey.md). Of the 14
documents that open without a password:

| Document | What an edit can invalidate |
| --- | --- |
| `bill-hr815.pdf` | A certification signature: `/Perms /DocMDP`, with DocMDP and FieldMDP transforms, and `/SigFlags 3` (signatures exist, append only) |
| `f1040.pdf` | A usage-rights signature: `/Perms /UR3`, `/SigFlags 2` (append only); a PDF/UA-1 claim; three revisions |
| `wdl6812-manuscript.pdf` | A PDF/A-1 claim; two revisions |
| the other 11 | nothing that the report names |

Every case in the corpus depends on how the document is saved: a save
that is not incremental rewrites the signed bytes and invalidates both
signatures whatever the edits were, and the append-only flag of both
signed documents asks for incremental saves. The report is therefore a
function of the edits **and** the save mode.

## What pylopdf reports today (ebb01d3c)

pylopdf has no change report. Its tests show the pieces that one would
build on:

- `test_form_signatures.py` reads a field's signed state and
  `form.signature_flags` (`SIGNATURES_EXIST`, `APPEND_ONLY`), and never
  relates them to edits.
- `test_pdfa.py` reads `doc.pdfa_claim` from XMP, bounded; there is no
  PDF/UA claim and no check that an edit keeps the claim.
- `test_incremental_save.py` refuses with an exception only a save that
  cannot be written at all: a created document, a repaired one, RC4 or
  revision 5 encryption, options that rewrite objects. No save is refused
  because it invalidates something, so filling `f1040.pdf` and saving it in
  full silently voids its UR3 signature.

## The three shapes on the corpus

The cases are the edits of pyropdf's open work that reach these
documents: filling a field (#193), drawing or adding an OCR layer (#153,
#146), flattening (#197), and deleting pages (#174).

| Case | 1. `doc.changes(incremental=...)` | 2. value returned by `save` | 3. refusal, option to report |
| --- | --- | --- | --- |
| Fill a field of `bill-hr815.pdf`, save incrementally | Lists the DocMDP signature with the permission that the edit uses, before writing | The same list, after the file is written | Raises unless the option allows it; the error carries the list |
| Fill `f1040.pdf`, save in full | Lists the UR3 signature as invalidated by the save mode alone | Reported after the signature is already void | Raises; the caller must opt in to every full save of a signed form |
| Draw untagged content on `f1040.pdf` | Lists the PDF/UA-1 claim | After the fact | Raises |
| Fill a field of `wdl6812-manuscript.pdf` with a Standard 14 appearance, which embeds no font | Lists the PDF/A-1 claim | After the fact | Raises |
| Any edit of the 11 other documents | An empty report | An empty report on every save | Nothing raises |
| Ask before deciding how to save | Yes: call it with each mode and compare | No: the report exists only after a save | Only by attempting the save and catching the error |

## Findings

- Only shape 1 answers before anything is written, and only it lets a
  caller choose the save mode from the report: the corpus shows that the
  mode decides whether the two signatures survive.
- Shape 2 reports too late for every case that matters, and changes the
  return value of `save` and `to_bytes`, whose callers expect `None` and
  `bytes` as in pylopdf.
- Shape 3 makes `f1040.pdf`, a common filled form, raise on every full
  save until callers learn an option; it can be built later on shape 1
  (a `save` option that raises when `doc.changes(...)` is not empty),
  while shape 1 cannot be recovered from shape 3 without a trial save.
- Shape 1 is the one that DESIGN.md section 10's sketch already uses
  (`doc.changes().signatures`). Its cost is the change set since opening
  (section 4.2) against the objects that each signed revision covers, which
  the reverse-reference index (#173) answers without a walk of the
  document.

## Recommendation

Shape 1: a method `doc.changes(*, incremental: bool) -> ChangeReport`,
whose report lists the signatures that the edits or the save mode
invalidate and the PDF/A or PDF/UA claims that the edits break, each with
the reason. `save` stays as it is. A refusing option on `save` can follow
as its own issue once the report exists.
