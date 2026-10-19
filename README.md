# brick-runner

The trusted evaluation runner for repositories built on [brick-framework](https://github.com/jtlacci/brick-framework).

The runner is the only component that reads evaluation data, runs evaluations, records trials and promotes bricks. Agents give it a commit hash. They never give it files, and they never get access to its state.

## What it does

| Step | What happens |
| --- | --- |
| Pin | Every input comes from Git: the base commit gives the framework, every other brick and all human-only files. The candidate gives only the files of its own loop, classified by the policy on the base. |
| Data | Each case is a recorded brick run. Its exchanges (external calls and sibling calls) are replayed. A new brick with `data.lock = unpinned` uses `generate(seed)`. |
| Build | `build()` runs in the evaluator worker, which never imports the brick. |
| Run | The brick runs in the candidate worker: no network (`unshare --net` when the host allows it, plus an audit hook), a clean environment, a time limit. |
| Score | `score()` runs in the evaluator worker again, separate from the candidate. |
| Record | Every trial goes to the MLflow ledger with its evaluation ID, epoch and result. |

### Replay rules

| Call | Recorded? | Result |
| --- | --- | --- |
| External | Yes | The recorded response |
| External | No | The case is out of evidence: not a pass, not a fail |
| Deterministic sibling | Hash | The sibling runs again; a different hash is a determinism mismatch, which fails the trial |
| Nondeterministic sibling | Response | The recorded response |
| Nondeterministic sibling | No (request or contract changed) | The sibling runs again, and the new response is stored as `regenerated` |

### What each split shows

| Split | Output |
| --- | --- |
| Development | Everything: terms, gates, counts, counterexamples, log |
| Validation | Terms, gates and counts |
| Holdout | Pass or fail |

## Commands

```sh
brick-runner --repo <brick repo> --home <runner state> <command>

submit --brick B --commit C [--split development|validation]   # code loop
evaluation-change --brick B --commit C                         # evaluation loop
holdout --brick B                                              # best validation trial, one holdout run
promote --brick B                                              # signed holdout, defect check, dependents
ingest --spool DIR                                             # captured records into the store
dataset --brick B [--include-synthetic]                        # prints a data.lock value
sync                                                           # DVC add and push of the store
trials --brick B
```

Each command prints one JSON object. The exit code is 0 for a pass, 1 for a fail and 2 for an error.

## The two loops

**Code loop.** `submit` evaluates the candidate's code against the pinned evaluation. `holdout` takes the best passing validation trial of the epoch (by the policy's metric, higher is better) and runs its one holdout. A candidate never gets a second holdout run. For a `time` split, the holdout is data captured after the candidate's freeze time, so `holdout` answers `waiting` until enough new data exists. `promote` needs a signed passing holdout. It also runs the defect check if the epoch has none, and evaluates every dependent brick with the candidate. If any check fails, production does not change.

**Evaluation loop.** `evaluation-change` runs the defect check: seeded mutations of the production code (`src/` and `input/adapters/`) at strengths 1, 2 and 4. The candidate evaluation passes only when scores fall as defects grow, and the strongest defects are detected. It also scores production under the old and the new evaluation. A human approves the change; when it merges, a new epoch starts.

An **epoch** is the Git tree hash of `bricks/<brick>/evaluation/` on the base.

## State

Everything lives under `--home`:

| Path | Contents |
| --- | --- |
| `store/` | Payloads named by SHA-256, record batches, datasets. A DVC project: `sync` pins it in Git and pushes it. DVC uses MD5 internally; the runner uses only the SHA-256 names. |
| `mlflow.db`, `mlartifacts/` | The MLflow ledger: every trial, defect check and promotion |
| `bundles/` | Promotion bundles |
| `runner.key` | The key that signs holdout results and bundles |

Neither MLflow nor the store is append-only. The runner must be the only writer, and agents must have no access to `--home`.

## Limits

- The runner runs evaluations one at a time (a file lock). There is no queue service.
- The sandbox blocks the network and subprocesses. It does not stop a candidate from writing files on the runner machine, so run the runner on its own machine.
- The brick repository's third-party dependencies must be installed in the runner's Python environment. The environment lock file is pinned in the evaluation ID, but the runner does not install it.
- Selection is `best` only. Score correction for many trials is later work; the ledger keeps every trial, so it can be applied to old epochs.

## Tests

```sh
pip install -e .
BRICK_FRAMEWORK=<path to a brick-framework checkout> python -m unittest discover -s tests
```
