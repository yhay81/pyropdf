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

Each checkout had a separate Cargo target directory. The workflow restored no compiler output cache. Both builds requested Rust 1.100.0, maturin 1.16.0, two Cargo build jobs and automatic manylinux selection. Cargo incremental compilation and sccache were disabled. The action's total time includes setup, dependency retrieval, Cargo's release compilation and wheel assembly.

Cargo downloaded 193 packages on each Linux build. The median interval from the first source update to the last recorded download was 4.9 seconds on the x86-64 head and 3.7 seconds on the arm64 head. macOS and Windows reused the registry sources on the head and still compiled all packages. The [recorded data](hosted-timings.json) includes these intervals for both builds on each runner.

## Same-input hosted comparison

The [trial workflow](https://github.com/yhay81/pyropdf/actions/runs/44994564240) built the tested merge `66977363` and its first parent `07569871`. Their only differences were the workflow, its regression tests and measurement files. Each runner therefore built the same release sources, manifests, lockfile and packaging rules twice, first from an empty Cargo target and then with the base compilation available to the head.

| Five-target measure | Historical median | Trial |
| --- | ---: | ---: |
| Paired build steps, sum | 78.36 min | 64.55 min |
| Base builds, sum | 41.18 min | 43.37 min |
| Head builds, sum | 37.18 min | 21.18 min |
| Total occupied runner time | 86.37 min | 72.85 min |
| Workflow elapsed | 48 min 2 s | 22 min 50 s |
| Longest runner queue | 20 min 55 s | 4 s |

The trial used 17.6% fewer paired-build runner-minutes. Head build steps took 43.0% less time than the historical median. The trial also had a much shorter queue, which contributes separately to the observed change in workflow elapsed time.

| Target | Queue (s) | Clean base build (s) | Reused head build (s) | Base / head packages compiled | Wheel size change (bytes) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Linux x86-64 | 2 | 280 | 154 | 197 / 4 | +2 |
| Linux arm64 | 4 | 398 | 215 | 197 / 4 | 0 |
| macOS Intel | 3 | 857 | 380 | 197 / 4 | +2 |
| macOS arm64 | 3 | 446 | 215 | 197 / 4 | +4 |
| Windows x86-64 | 2 | 621 | 307 | 199 / 4 | +37 |

All five jobs logged compilation of the same four head packages (`pyropdf-store`, `pyropdf-schema`, `pyropdf-engine` and `pyropdf-python`). Aggregate head tool setup took 44 seconds in the trial and the historical median was also 44 seconds. The two Linux heads each downloaded 193 crate sources again, taking about four seconds per job. Checking out and preparing the second source tree took 18 seconds across all five jobs. The trial's member inspection and extra artifact uploads added another 18 seconds to total runner occupation.

The [archived wheel-member comparison](paired-builds/hosted-contents.json) records how the clean base and reused head artifacts differed for these inputs. The [controlled build experiments](paired-builds/README.md) additionally compare a clean and a reused build of the exact same head when the inputs change.

## Reproduction

With authenticated `gh`, run the scraper from the repository. It reads the first attempt's job metadata and archived job logs. The [recorded data](hosted-timings.json) includes the PR head, the tested merge and its exact base, each runner label, native wheel tag, post-packaging size and SHA-256. Workflow elapsed time runs from creation to the completion update. Runner-minutes are unweighted wall-clock occupation across the five machines. They exclude the queue and have no billing multiplier.

```sh
python3 evidence/wheel-size/hosted_timings.py \
  44980129230 44984902949 44984907881 44984908627 44984912331 44984915735 \
  44994564240
```
