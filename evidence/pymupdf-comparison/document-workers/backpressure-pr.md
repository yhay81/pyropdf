**Title**: Bound outstanding batches in the CPU dispatcher

A producer can submit raster tasks faster than workers finish them. The
dispatcher tries to record available results after each submission, but
the task and result channels are unbounded. Completed results can also
wait behind one earlier unfinished batch in the ordered receiver.

Count a batch until its result has been recorded in submission order.
When the count reaches two batches per worker, wait for the next ordered
result before letting the producer continue. Below that limit, consume
ready results without waiting. Flush drains the receiver until the worker
senders close. This bounds the number of outstanding batches; individual
shapes and the finished scene still have variable size.

The regression test explicitly submits small batches, checks the bound
after each submission, then verifies that flush clears the count. Removing
the wait at the limit made the test fail. The full vello_cpu library suite
and Clippy passed with multithreading enabled. A local PDF-renderer trial
with this limit matched the baseline on all 2,340 pixel comparisons.

In the PDF integration's interleaved corpus trial, adding the bound
increased the best 72 dpi total by 8% and reduced the best 144 dpi total
by 5%. Median totals changed by +92% and -5% respectively on the shared
Windows host. In a separate paired run with both starting orders, best
totals ranged from unchanged to 4.8% slower and medians from 1.6% faster
to 2.2% slower. The cap trades producer run-ahead for bounded outstanding
work. These observations are from a four-worker PDF workload.

I used OpenAI Codex to draft the code, tests and this description.
