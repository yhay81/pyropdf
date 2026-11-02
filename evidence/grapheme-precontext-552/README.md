# Grapheme look back in bounded steps (#552)

Checked on 2026-11-02 against `unicode-segmentation` 1.13.3 (the version
pyropdf pins) and the `master` branch of unicode-rs/unicode-segmentation
at `afe6c8e6`.

## What the cursor reads in one call

`GraphemeCursor::next_boundary(chunk, chunk_start)` reads forward only to
the chunk's end. Its look back reads `chunk[..cursor]`:

- GB9c (`handle_incb_consonant`) reads back over `InCB=Extend` and
  `InCB=Linker` characters to the character before them.
- GB11 (`handle_emoji`) reads back over `Extend` characters to the
  `Extended_Pictographic` before the ZWJ.
- GB12 and GB13 (`handle_regional`) read back over regional indicators only
  while the cursor has no count of them, which happens only for a run at
  the text's start. Moving forward keeps the count, so this look back is
  never long.

Each look back stops at the chunk's start and asks for the text before it
with `GraphemeIncomplete::PreContext`. A caller that hands the cursor
chunks from the text's start, as #546 did, therefore lets one call read
back over a whole run.

## Why later chunks lost regional-indicator parity

When the cursor stands exactly at a chunk's start before a regional
indicator, 1.13.3 asks for pre-context even though it already counts the
indicators, and `provide_context` adds the indicators it reads to that
count. The parity is then wrong, as the committed proptest seed `"🇦🇦🇦🇦"`
with step 1 shows. Upstream reported this as unicode-rs/unicode-segmentation#174
and fixed it in #175 (merged 2026-07-28), after 1.13.3 (2026-06-01). No
release contains the fix yet.

## The local fix

`scan::grapheme_ends_with` starts each chunk after the first at the
character before the cursor, so the cursor never decides a boundary at a
chunk's start and never reaches the faulty path. It answers each
`PreContext` request with at most a step of earlier text, after a poll.
The cursor's API in 1.13.3 already suffices; pyropdf needs no new
upstream API.

`grapheme_discovery_polls_while_looking_back` measures the effect. For a
conjunct and an emoji sequence with `LONG / 2` combining accents inside,
the old scan polled 64 times, all while reading forward, and the new scan
polls at least 96 times and stops at a poll inside the look back.
`stepped_graphemes_match_the_iterator` still matches the grapheme iterator
for steps 1 to 23, with Devanagari consonants, a virama, and a prepended
concatenation mark added to the samples; it passed 40,000 cases locally.

## Upstream

No resumable or pre-context API is missing. The upstream request that
remains is optional: a release containing #175. With it, the one-character
overlap would no longer be needed, but the overlap costs one character per
step and pyropdf does not need to wait for it. No draft is published.
