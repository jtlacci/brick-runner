# brick-runner

The trusted evaluation runner for repositories built on [brick-framework](https://github.com/jtlacci/brick-framework).

The runner is the only component that reads evaluation data, runs evaluations, records trials and promotes bricks. Agents give it a commit hash. They never give it files, and they never get access to its state.

## What it does

| Step | What happens |
| --- | --- |
| Pin | Every input comes from Git: the base commit gives the framework, every other brick and all human-only files. The candidate gives only the files of its own loop, classified by the policy on the base. |
| Trees | Each evaluation gets three separate trees: **code** (the candidate), **deps** (other bricks, for sibling calls) and **evaluation** (the scorer). No worker can see another's tree. |
| Data | Each case is a recorded brick run. The dataset pins the run and every exchange in its trace. A new brick with `data.lock = unpinned` uses `generate(seed)`. |
| Build | `build()` runs in the evaluator worker, which never imports the brick. |
| Run | The brick runs in the candidate worker. Every io call it makes goes to the parent, which answers from the pinned exchanges. |
| Score | `score()` runs in the evaluator worker again, after the runner checks that the evaluation files are unchanged. |
| Record | Every trial goes to the MLflow ledger with its evaluation ID, epoch and result. |

### The sandbox

Every worker runs in new user, network and mount namespaces (`unshare`):

- no network;
- its own tree, mounted at `/mnt`;
- `/tmp`, `/dev/shm`, `/var/tmp`, the runner's state folder and the brick repository are hidden behind empty private mounts, so a worker cannot read the signing key, the store, the ledger or another worker's tree;
- every other mount is read-only, including the runner's own source and the Python environment, so a worker cannot change the host.

An audit hook also refuses sockets and subprocesses. On a host without namespaces the runner refuses to evaluate, unless `BRICK_RUNNER_UNSANDBOXED=1` is set for development; results then say `"sandboxed": false`.

### Replay rules

The parent process decides every replayed call. The candidate reports only its outputs.

| Call | Recorded? | Result |
| --- | --- | --- |
| External | Yes | The recorded response. Each recording answers one call. |
| External | No | The case is out of evidence: excluded, and counted |
| Deterministic sibling | Hash | The sibling runs again in a dependency worker; a different hash is a determinism mismatch, which fails the trial |
| Nondeterministic sibling | Response | The recorded response |
| Nondeterministic sibling | No (request or contract changed) | The sibling runs again in a dependency worker. The result is counted as regenerated, and never stored. |

A case counts as out of evidence only when the parent refused a call. A brick that raises `OutOfEvidence` itself gets a run error. A trial fails when more than `max_out_of_evidence` of its cases (default 5%) are out of evidence, so a candidate cannot skip hard cases.

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

**Code loop.** `submit` evaluates the candidate's code against the pinned evaluation. `holdout` takes the best passing validation trial of the epoch (by the policy's metric, higher is better) and runs its one holdout. Each candidate's code (by hash) gets one holdout run, in any epoch and on any evidence: waiting for new data or a new base never earns a second run. Only a human can grant another, with `holdout --rerun-reason "..."`, and the ledger records the reason. Promotion accepts only a code's first holdout or a granted one. For a `time` split, the holdout is data captured after the candidate's freeze time, so `holdout` answers `waiting` until enough new data exists.

`promote` needs a signed passing holdout whose component hashes still match the current base; otherwise it stops, and a human decides whether to grant a new holdout run. It runs the defect check if the epoch has none, and evaluates every direct dependent with the candidate. If any check fails, production does not change. A bundle whose holdout used synthetic data says `"synthetic": true`: the dev agent can read `generate()`, so that holdout is not hidden. Use shadow or paper mode to collect real data before you rely on it.

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
- Workers run as root mapped into a user namespace. The read-only mounts stop writes, but a separate unprivileged user for the runner, on its own machine, is still the stronger setup.
- A brick can skip up to `max_out_of_evidence` of its hardest cases. Keep the cap low.
- The defect check mutates `src/` and `input/adapters/`, not `runner/` or `input/config.yml`. A brick with nothing to mutate cannot be promoted.
- Promotion checks direct dependents only. A dependent that was never promoted is checked with its base code.
- The brick repository's third-party dependencies must be installed in the runner's Python environment. The environment lock file is pinned in the evaluation ID, but the runner does not install it.
- Selection is `best` only. Score correction for many trials is later work; the ledger keeps every trial, so it can be applied to old epochs.

## Tests

```sh
pip install -e .
BRICK_FRAMEWORK=<path to a brick-framework checkout> python -m unittest discover -s tests
```
