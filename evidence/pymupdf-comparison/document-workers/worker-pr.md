**Title**: Let CPU workers outlive a render context

pyropdf renders successive pages of a document through vello_cpu. Reusing a
RenderContext amortizes thread startup but keeps buffers sized for the last
scene. Creating a context per page releases those buffers and starts another
thread pool. A partial document read can leave the first approach holding
threads and scene storage after the document closes.

This adds a fallible `RasterWorkers::new` and two multithreading-only methods.
`RenderContext::with_workers` takes ownership of the workers; `into_workers`
consumes the scene, finishes its outstanding work, and returns the workers.
The pool can be kept by a document and dropped on close. Exclusive ownership
means two contexts cannot submit blocking work to the same pool at once.
Existing constructors keep their behavior.

The lifetime test recovers workers from an unfinished red scene, renders a
blue rectangle in a differently sized scene, and checks the pixels and
worker thread IDs. The vello_cpu library suite passed with multithreading
enabled, as did Clippy and a serial build with `libm,u8_pipeline`.

In a local pyropdf integration, all 2,340 corpus pixel comparisons matched.
Closing a partially rendered document on another thread returned the process
thread count from seven to three. Another document could then acquire the
workers. Dropping it also returned the count to three. The integration
combines the API with a four-worker process cap and a serial path for short
jobs, so its timings describe that combination.

I used OpenAI Codex to draft the code, tests and this description.
