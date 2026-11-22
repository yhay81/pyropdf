# Hosted wheel comparison times

The six most recent successful five-target pull-request comparisons available when [#1115](https://github.com/yhay81/pyropdf/issues/1115) opened took a median of **48.0 minutes** to finish. They occupied a median of **86.4 runner-minutes** across five machines. The paired wheel builds accounted for **78.4 runner-minutes**. Every head recompiled all 197 Cargo packages reported on Linux and macOS and all 199 on Windows.

## Historical clean builds

All times in this table are minutes. Occupied time is the sum of job start to completion for the five native jobs. Queue time runs from creation of each job to its runner start. Five of these six workflows started within eleven seconds of each other, so queue conditions differed substantially from the run started an hour earlier.

| Workflow | Elapsed | Runner-minutes | Base builds, sum | Head builds, sum | Longest queue |
| --- | ---: | ---: | ---: | ---: | ---: |
| [44980129230](https://github.com/yhay81/pyropdf/actions/runs/44980129230) | 63.2 | 85.3 | 39.9 | 36.3 | 38.2 |
| [44984902949](https://github.com/yhay81/pyropdf/actions/runs/44984902949) | 23.4 | 85.8 | 40.0 | 37.8 | 0.1 |
| [44984907881](https://github.com/yhay81/pyropdf/actions/runs/44984907881) | 45.6 | 86.9 | 42.3 | 36.5 | 18.6 |
| [44984908627](https://github.com/yhay81/pyropdf/actions/runs/44984908627) | 30.9 | 91.5 | 43.3 | 39.5 | 13.7 |
| [44984912331](https://github.com/yhay81/pyropdf/actions/runs/44984912331) | 50.4 | 84.3 | 40.0 | 36.2 | 23.2 |
| [44984915735](https://github.com/yhay81/pyropdf/actions/runs/44984915735) | 58.9 | 96.7 | 46.8 | 41.3 | 30.5 |

The following times are median seconds per job across those six runs. The setup columns count the action's timed groups for pulling the manylinux image, installing Rust or its target, and installing maturin. These intervals are included in the build steps.

| Target | Runner queue | Base build | Head build | Base tool setup | Head tool setup |
| --- | ---: | ---: | ---: | ---: | ---: |
| Linux x86-64 | 135.5 | 331.0 | 307.0 | 37.6 | 20.9 |
| Linux arm64 | 48.0 | 384.5 | 357.0 | 42.2 | 17.1 |
| macOS Intel | 1,255.0 | 754.0 | 702.0 | 21.3 | 1.8 |
| macOS arm64 | 468.5 | 469.5 | 372.5 | 14.8 | 1.8 |
| Windows x86-64 | 13.0 | 614.0 | 564.0 | 22.5 | 2.4 |

Each checkout had a separate Cargo target directory. The workflow restored no compiler output cache. Both builds requested Rust 1.100.0, maturin 1.16.0 and automatic manylinux selection, with sccache disabled. The logs report Cargo's release timer separately from the action's wall clock. Their difference includes tool startup, acquiring the container and tools, and assembling the wheel. Source downloads that Cargo performs itself are included in Cargo's timer.

## Reproduction

With authenticated `gh`, run the scraper from the repository. It reads the first attempt's job metadata and archived job logs. The [recorded data](hosted-timings.json) includes the PR head, the tested merge and its exact base, each runner label, native wheel tag, post-packaging size and SHA-256. Workflow elapsed time runs from creation to the completion update. Runner-minutes are unweighted wall-clock occupation across the five machines. They exclude the queue and have no billing multiplier.

```sh
python3 evidence/wheel-size/hosted_timings.py \
  44980129230 44984902949 44984907881 44984908627 44984912331 44984915735
```
