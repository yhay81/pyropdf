# GitHub discovery and launch experiment

Prepared on 2026-11-16 for [#806](https://github.com/yhay81/pyropdf/issues/806).
This is a strategy and a baseline, not evidence that the changes have
already increased stars.

## Audience and message

Start with Python developers who automate PDF review: read a document,
highlight findings, fill a form, preview the result, and undo an edit.
The message is **read, render, and edit PDFs in Python, with reversible
edits and an MIT license**. The working invoice is the proof.

Document extraction for search/RAG is a secondary audience. Lead that
conversation with the existing Markdown, table, and retrieval-chunk
recipes. Do not position pyropdf as a complete RAG platform or claim that
it is universally faster than another library. The current safety limits
and alpha status must remain visible in any introduction.

The sequence is discovery -> useful example -> successful trial ->
optional star or concrete feedback -> another useful example. First reach
100 stars through relevant users; consider a 1,000-star campaign only
after learning which use case actually attracts repeat use. Neither
milestone is a forecast or a promised deadline.

## Baseline

[`baseline-2026-11-16.json`](baseline-2026-11-16.json) records GitHub's
aggregate responses, captured at 02:58 UTC on 2026-11-16 (11:58 JST).
The returned daily traffic buckets run from **2026-11-01 to 2026-11-14
UTC**, not through the capture date.

| Signal | Before this change | Interpretation |
| --- | ---: | --- |
| Stars | 1 | Point-in-time total, not stars acquired during the traffic window |
| Forks | 0 | Not a measure of installed users |
| Unique repository visitors | 21 | GitHub's total for the returned window |
| Repository views | 438 | Includes repeated visits; popular paths include issues and PRs |
| Overview visitors | 13 | Unique visitors to the repository overview in popular paths |
| Clones / unique cloners | 6,854 / 653 | May include CI, bots, and development; not adoption |
| Topics | 0 | An unused discovery surface |

Reported referrers were GitHub (12 views), the documentation site (6),
and Google (1). These are not a complete attribution report. The data
suggests limited discovery, but cannot distinguish all humans from bots
or prove why a visitor did not star. Do not divide the lifetime star
total by this window's visitors and call that a conversion rate.

## First intervention

- Lead the README with uses and reversible edits, then show real invoice
  renders with links to the PDF, demo, and reproducing script.
- Provide an example that starts with an empty directory and no input PDF.
  Check its text, rendered PNG, undo/redo, and saved highlight; also run it
  with the released PyPI wheel so new visitors see the same behavior.
- Link directly to recipes, the API, the comparison guide, Japanese
  documentation, and the existing beginner contribution issues. Use
  absolute image and document URLs so the README also works on PyPI when
  a normal future release publishes it. This change does not make a release.
- Add one optional star invitation after the useful content, without
  blocking installation or documentation.
- Replace the stale repository description with:
  `Read, render, and edit PDFs in Python with a Rust core. Undo/redo, atomic transactions, and incremental saves. MIT licensed.`
- Set relevant topics: `pdf`, `python`, `rust`, `pdf-library`,
  `pdf-parser`, `pdf-generation`, `text-extraction`, `ocr`, and
  `document-processing`. The homepage remains the existing docs site.

[`metadata-after.json`](metadata-after.json) records the repository settings
read back after applying them. The README is published by the linked PR's
normal review and CI process.

## The next 30 days

Day 0 is the merge date. This is an execution sequence, not a scheduled
job. External posts need the owner's channel choice and authorization;
none are sent by this change.

| When | Action | Destination and owner | Evidence / decision |
| --- | --- | --- | --- |
| Day 0 | Publish the new README, verify the released-wheel example, and apply topics/About text | This repository; agent | Merged PR, passing checks, before/after settings |
| Days 1–3 | Demonstrate finding a total, highlighting it, undoing, and saving; explain why reversible edits matter | Owner's existing blog or developer social account; owner | One original example with a repository link; record URL and UTC time |
| Days 4–7 | Share the working project where Python/PDF users already discuss document workflows | One relevant community selected by the owner | Read that community's current rules first; ask for one concrete use-case question, answer feedback |
| Days 4–7, optional | Introduce the architecture as a Show HN, if the owner wants to discuss it personally | Hacker News; owner writes and posts personally | Human-written account of the problem, tradeoffs, runnable demo, MIT license, and current limits; no generated submission |
| Day 7 | Save another metrics snapshot and review installation/usage reports | Repository evidence; future authorized task | Separate low reach from failed trials; fix the largest observed friction first |
| Days 8–14 | Publish one complete recipe based on an actual question | Existing documentation; future issue/PR | Reproducible input/output and a public-contract test; #755 is an existing candidate for review-comment extraction |
| Day 14 | Capture metrics; compare the returned daily buckets and the net star change | Repository evidence; future authorized task | Choose the one channel and example that produced useful feedback, not just the most views |
| Days 15–21 | Follow up with the improved example in the original conversation, when welcomed | Same community; owner | Answer the original need; avoid duplicate launch posts or unrelated promotion |
| Day 30 | Capture metrics and decide the next experiment | Repository evidence; future authorized task | Keep, change, or stop the channel/message based on the criteria below |

Do not submit to third-party curated lists until their current contribution
rules and fit have been checked and the owner authorizes that publication.
Do not purchase stars, trade them, mass-message users, or ask for coordinated
votes. The aim is relevant discovery and useful feedback.

### Working targets and decision rules

For the first 30 days, use **25 net new stars**, **at least 3 independent
actionable usage reports**, and **300 unique visitors in a returned
14-day window** as experiment targets, not expected results. These numbers
are deliberately provisional because the baseline is small.

- If reach stays below 100 visitors per returned window, test another
  relevant channel before rewriting the product story again.
- If reach rises but people cannot complete the demo or installation,
  prioritize the reported obstacle. Do not interpret clones as successful
  trials.
- If reach rises above 100 but net stars remain below 5 and useful feedback
  is absent, interview willing respondents about the use case and change
  the example. This is a hypothesis check, not causal attribution.
- If a channel produces concrete reports, answer them and improve that
  recipe before adding more channels. A useful issue or contribution is
  evidence of interest even if the author never stars the repository.

## Capture and interpretation

From an authenticated checkout, run (choose a new filename each time):

```bash
python3 evidence/github-growth/capture.py /tmp/pyropdf-growth-next.json
```

The script needs Python 3.11+ and authenticated `gh` with permission to
read repository traffic. It reads metadata, views, clones, referrers,
and popular paths. It writes only aggregate data, with start/end capture
timestamps; it does not query stargazer identities or tokens. Any failed
request stops the capture without creating a snapshot. Failure means
**unknown**, never zero. Existing files are not overwritten.

Capture at least weekly while running the experiment: GitHub's traffic
window is only 14 days. Store snapshots and the corresponding publication
URL/time in the next evidence PR. Keep missing dates missing; do not invent
zero buckets. A one-off follow-up capture is not an automated schedule.

Compute net star change as `new.stargazers_count - old.stargazers_count`.
Do not add overlapping 14-day totals or sum daily unique visitors to get
period uniques. Deduplicate daily buckets by UTC date when comparing view
counts. Compare equivalent complete dates, allowing for reporting delays.
Stars may be added and removed, traffic may include automation, and these
endpoints do not connect a person, channel, trial, and star. No causal
star-conversion rate is available from these data alone. Use referrers as
clues and voluntarily supplied feedback as qualitative evidence.

## Sources checked on 2026-11-16

- [GitHub: repository topics](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics)
  explains topic-based discovery and limits repositories to 20 topics.
- [GitHub: repository traffic](https://docs.github.com/en/repositories/viewing-activity-and-data-for-your-repository/viewing-traffic-to-a-repository)
  documents the 14-day window, UTC dates, permissions, and reporting delays.
- [Show HN guidelines](https://news.ycombinator.com/showhn.html) prioritize
  work that people can actually try and a maker available for discussion.
- [Hacker News guidelines](https://news.ycombinator.com/newsguidelines.html)
  prohibit generated text, automated posting, and soliciting votes.

These sources support the channel mechanics and constraints, not a
promise that a README edit, a topic, or any post produces stars.
