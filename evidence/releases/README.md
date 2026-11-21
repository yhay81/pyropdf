# Qualifying the ten 1.0 workflows

Use the [workflow manifest](../../tools/spec_map/minimum.toml) and the [approved minimum](../../DESIGN.md#minimum-workflows-for-10) to plan a candidate run. Each entry names its guide, reproducible input or input generator, operation, required CI tests and independent readers, expected outcome, runtime and save mode. Its issue numbers trace missing implementation or evidence. The implementation issues can close before release. Evidence from a different build cannot qualify the candidate.

1. Freeze the tag and full commit. Use a successful CI run of that commit and the actual distribution wheels from the tag build. Download them and record their filenames and SHA-256 digests. Run each specified case with the recorded wheel. Retain the generated input and observed output (PDF, rendered image, or serialized extraction), external comparison and pre-save report at a durable evidence URL. For a refusal, retain the typed error and the demonstration that no output was written.
2. Record the external observations as JSON in `evidence/releases/<tag>/workflows.json` on the default branch. This file can be committed after the candidate has been built. The release job pins and logs the commit containing the observations, downloads them as data and runs the checker from the candidate itself. When a first attempt stops for missing evidence, retry the publish job using the same build artifacts. If a wheel is rebuilt, repeat the observations whose digest changed.
3. Review the combined report before publishing. The job checks the candidate CI, both release gates, the actual wheel bytes and the live GitHub completion state of #15 and #243. It retains the report, the records, their source commit, the CI evidence and the issue state snapshot in the `minimum-workflows` artifact. Whole-map exceptions remain in `decisions.toml`. The separate [release declaration](https://github.com/yhay81/pyropdf/issues/966) owns publication of capability claims.

## Observation format

The top-level keys are `tag`, `revision`, `manifest_sha256`, `artifacts`, `upstream` and `workflows`. Hash the bytes of the candidate's `tools/spec_map/minimum.toml`. In `artifacts`, key the observed wheel by runtime (`native`, `server`, `cloudflare-worker`) and supply `wheel`, `sha256`, `target` (one wheel platform tag), `interpreter` including its version, and `extras` (a list). In `upstream`, record the `version` and `source` of hayro from the candidate Cargo.lock and the `release` and `adoption` URLs that establish the released input bounds. The currently open [upstream work](https://github.com/yhay81/pyropdf/issues/15) prevents qualifying untrusted processing.

Each entry in `workflows` has the manifest workflow ID as its key. Each case identifies the installed wheel with its own `artifact_sha256`. Each case has the exact case ID from the manifest, such as `native/incremental`. Record the following fields.

| Field | Observation to record |
| --- | --- |
| `input_sha256`, `observation`, `proof` | Actual input bytes, a description of the expected and observed result, and an HTTPS link to the retained evidence. |
| `outcome` | Use `delivered` with `output_sha256`. Use `refused` with a `refusal` object giving `type`, `code` and `output_written: false`. |
| `checks` | One entry per required external tool, with `result: "passed"`, the checked `sha256` and a `report` URL. For a refusal, inspect the original input and check that it was not modified. |
| `guarantees` | For a save, record individual passing reports for `untouched-values-and-streams`, `settled-edit` and `pre-save-signatures-and-profiles`. Incremental saves also require `original-prefix` and `identical-no-op`. Refusals require `atomic-refusal`. Each entry has `result` and a `report` URL. |
| `deployment` | For both server and Cloudflare Worker requests, give the actual `deployment_id`, `artifact_sha256` and `receipt` URL. Include the request, response and observed runtime in the receipt. The refusal control exercises the documented `LimitError` with code `file_size`. |
| `profile_checks`, `human_review` | For each archival fixture and save, validate each stated level on the output. Key `profile_checks` by level with `result`, `sha256` and `report`. Give the human review the same three fields plus `levels`, `reviewer`, `reader_and_version`, `reading_order` and `alternative_text`. Record what the reviewer observed on that output. |

Evaluate the same inputs offline from the candidate checkout:

```bash
python3 tools/spec_map/claim.py --release "$tag" --revision "$revision" \
  --evidence release-evidence.json --workflow-results workflows.json \
  --artifacts dist --issue-states issue-states.json
```

The issue-state snapshot is keyed by the issue number, with the GitHub `state` and `stateReason` fields. A qualifying synthetic example, including missing and skipped controls, is built by `qualifying_workflows` in [the release tests](../../tests/test_spec_map.py). The synthetic observations exercise the checker only.
