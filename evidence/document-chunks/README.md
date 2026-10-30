# Document chunks

`Document.to_json` and `Document.to_chunks` (#427) compared with PyMuPDF 2.0's methods of the same
names, which delegate to pymupdf4llm 2.0. `compare.py` runs both on the two-page fixture of
`tests/test_document_chunks.py` (a heading, a two-line paragraph, a list, a bordered table, an
image, a closing line, then a smaller heading and a forty-word paragraph). `results/report.md` is
its output.

## Same

- Reading order, page indexes, and the image's display-space box (`[40, 220, 90, 250]`) agree.
- Both split at headings, carry the heading path as the section, and never split the table.
- Paragraph and list boxes agree on x; on y PyMuPDF uses the glyph boxes and pyropdf the line
  boxes of `page.layout()`, so they differ by up to 2 points.

## Different

- **Budget unit.** pyropdf counts characters (`max_chars`, default 2000); PyMuPDF counts tokens
  (`max_tokens`, default 400). With 15 tokens, PyMuPDF kept the 269-character paragraph whole; with
  60 characters pyropdf split it at whitespace into five chunks. A table or another indivisible
  element that exceeds the budget becomes one chunk marked `oversized`.
- **Table box.** pyropdf reports the table's ruled grid (`[40, 150, 240, 190]`); PyMuPDF the box of
  its cell text (`[45, 156.6, 160.6, 184.2]`). The JSON element also carries the cell rows.
- **Headings.** PyMuPDF's layout model calls the isolated line "After the image." a level-3 heading
  and starts a section with it; pyropdf uses the font-size levels of `Document.markdown`, which keep
  it a paragraph of body size.
- **Text.** pyropdf joins a paragraph's lines with spaces and separates elements with blank lines,
  as `markdown()` does; PyMuPDF keeps line breaks and writes compact table rows (`|Name|Qty|`) and a
  `[Figure f1: 50x30]` placeholder. pyropdf's image element has empty text and adds nothing to a
  chunk's text.
- **Scope.** PyMuPDF's JSON carries spans, fonts, and OCR flags per box; pyropdf's carries the
  element kind, text, box, heading level, and table rows. Spans stay available from `page.layout()`.
  pyropdf runs no OCR.

## Run

```bash
uv run python evidence/document-chunks/compare.py pyropdf target/chunks.pdf > target/chunks-pyropdf.json
uv run --no-project --python 3.13 --with 'pymupdf==2.0' --with 'pymupdf4llm==2.0' python evidence/document-chunks/compare.py pymupdf target/chunks.pdf > target/chunks-pymupdf.json
uv run python evidence/document-chunks/compare.py report target/chunks-pyropdf.json target/chunks-pymupdf.json > evidence/document-chunks/results/report.md
```
