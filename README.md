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
- every other mount is read-only, including the runner's own source and the Python environment, so a worker cannot change the host;
- then `/tmp` is a new, empty, writable tmpfs of the worker's own (Xvfb needs it).

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

## How work reaches the runner

The runner machine **pulls** work from GitHub with `brick-runner serve`. GitHub never pushes code to it, and no GitHub workflow runs on it, so a pull request cannot run its own code there outside the sandbox. To set up a machine, follow [deploy/SETUP.md](deploy/SETUP.md).

Every minute the service:

1. runs new `/brick holdout <brick>`, `/brick holdout <brick> rerun: <reason>` and `/brick promote <brick>` comments, only from the GitHub logins in `--allow`, never from bots, and never from a comment that was edited;
2. fetches the brick repository into its own bare clone;
3. for each open pull request into `main` from the same repository, evaluates it when its head or `main` changed: it plans the change by the policy on `main`, then runs the code loop (`submit` on development and validation) or the evaluation loop (`evaluation-change`). A change with both fails. Other pull requests (forks, other bases) get no status at all, so they cannot cover a failure;
4. posts a `brick-runner` commit status and one comment, updated in place. Everything it posts is HTML-escaped and stripped of known secrets;
5. backs up to S3 at most once every ten minutes.

Run the service as its own **GitHub App**, make `brick-runner` a required check on `main` with that App as its expected source, and require branches to be up to date. [deploy/SETUP.md](deploy/SETUP.md) has the steps.

## Commands

```sh
brick-runner --repo <brick repo> --home <runner state> <command>

serve --github owner/name --allow login[,login] [--app-id ID --app-key PEM --installation-id ID]
submit --brick B --commit C [--split development|validation]   # code loop
evaluation-change --brick B --commit C                         # evaluation loop
holdout --brick B [--rerun-reason R]                           # best validation trial, one holdout run
promote --brick B                                              # signed holdout, defect check, dependents
plan --head C                                                  # which loop a change belongs to
ingest --spool DIR                                             # captured records into the store
dataset --brick B [--include-synthetic]                        # prints a data.lock value
trials --brick B
remote --url s3://bucket/prefix [--region R] [--git URL]       # set the backup remotes
sync [--no-push]                                               # back up now
restore --git URL                                              # rebuild --home on a new machine
```

Each command prints one JSON object. The exit code is 0 for a pass, 1 for a fail and 2 for an error.

## Backup

| Data | Backup |
| --- | --- |
| Payloads, records, datasets (`store/`) | DVC: data to S3, pointer files to a private Git repository |
| Ledger and bundles | A consistent snapshot goes into `store/ledger/`, tracked by DVC too |
| `runner.key` | Never backed up here. Keep it in a secret manager. |

Credentials are never written into a config file: DVC reads the usual AWS variables, profile or instance role, and Git uses the runner's GitHub token. `deploy/iam-policy.json` allows reads and writes but no deletes; turn on bucket versioning.

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
| `repo.git` | The service's bare clone of the brick repository |
| `serve-state.json` | The head commit and comment of each pull request, and the commands already run |
| `github-token` | Optional: the GitHub token, if it is not in `BRICK_RUNNER_GITHUB_TOKEN` |

Neither MLflow nor the store is append-only, but the S3 policy allows no deletes and bucket versioning keeps old objects. The runner must be the only writer, and agents must have no access to `--home`.

## Web bricks and the model judge

A brick with `KIND = "web"` is a browser app in `app/`. For each evaluation the runner:

1. checks `app/package-lock.json` (every package from registry.npmjs.org, with a sha512 integrity hash) and fills an npm cache **outside** the sandbox, with only `package.json` and the lockfile and no install scripts;
2. runs `npm ci --offline` and `npm run build` **inside** the sandbox, with no network;
3. copies `app/dist/` into the evaluation workspace as data, serves it on 127.0.0.1 in the evaluator's sandbox with the headers in `app/headers.json`, starts the browser, and calls `evaluate.observe(input, browser, base_url)` for each case. `score()` then scores what `observe` saw.

**Headers.** `app/headers.json` (`{"Content-Security-Policy": "...", ...}`) declares the security headers the app has in production. The evaluation server sends the same headers, so a page that a policy breaks in production breaks in the evaluation too. Let the production server read the same file. Only these names are allowed: `Content-Security-Policy`, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, `X-Frame-Options` and the three `Cross-Origin-*` policies.

**Workers.** `BRICK_RUNNER_WEB_WORKERS` (default 1) observe workers run at once, each in its own sandbox with its own browser; case *i* goes to worker *i* mod *n*. They share the CPU, so the number is part of the evaluation ID; give each worker about one vCPU, or the timing gates get noisy.

**Browser.** `BRICK_RUNNER_BROWSER=chrome` runs Google Chrome, headed, on an Xvfb screen in the evaluator's sandbox: the browser people use, with real input events, focus and native dialogs. `chromium` (the default) is Playwright's headless Chromium. The browser and its version are part of the evaluation ID.

**Computer use agent.** An `evaluate.py` with `AGENT = {"model": ..., "terms": [...]}` and `observe(input, browser, base_url, agent=None)` can give a case a task for a model that clicks through the app, as a person does: `agent.run(page, task, files=...)` returns `{"status": "done" | "failed" | "error", "text", "steps", "actions", "pickers"}`. The evaluator sends screenshots over its line channel; the runner, outside the sandbox, asks the model for the next actions with the computer use toolset (`computer_toolset_20260801`) and the judge key; the evaluator does them on the page with real mouse and keyboard events. A file picker that the page opens gets the task's `files`. The model does the task only: `score()` decides from facts, such as the requests the app sent. A case is an agent case when its input has `"task"`. The defect check runs no agent case and leaves out the gates on `AGENT["terms"]`, because each mutant would pay for every session again. Limits: 40 steps a task (then that case fails); 120 sessions, 2,500 calls and 20 M input tokens an evaluation (then the evaluation fails, so a case that never ran cannot pass); only the models in `BRICK_RUNNER_AGENT_MODELS` (default: the Claude 5.5 models). Only the newest 3 screenshots stay in a conversation. When one action of a turn fails, the rest of that turn is not done. Agent answers are not cached; a whole evaluation is (by its ID, which includes the runner's agent and browser code), so a rerun of the same change costs nothing. The observe step has 600 s, and 90 s more for each agent step, up to 3 h.

An API failure (overload, network) is not a result: the runner records nothing, the pull request gets an error status, and the service tries again. So an outage cannot use up a candidate's one holdout run. Make `policy.metric` a term that cases with no agent also give: the defect check runs no agent case.

Make `app/headers.json` human-only (leave it out of every surface in `policy.yml`): human-only files come from the base commit, so a candidate cannot change the headers its own evaluation uses.

The defect check mutates the app's JavaScript and TypeScript in `app/src/` (operators, booleans, numbers, strings), rebuilds and observes each mutant. A mutant that no longer builds counts as caught.

`score()` may also take `calls`: the recorded exchanges the case used, in order, as `{"name", "request"}` (for example `sibling:store` with its request). An evaluation checks with it that a brick made the writes it must make, and in the right order.

An `evaluate.py` with `JUDGE = {"model": ...}` and `judge_requests(record, output)` gets model judgements: the runner, outside the sandbox, sends each request to the Anthropic Messages API with the key in `BRICK_RUNNER_JUDGE_KEY` (a file), caches each answer by request in `<home>/judgments/`, and calls `score(..., judgments=[...])`. Without a key, an evaluation that needs the judge fails with that reason.

Setup: `npm` on the runner's PATH, and Chromium from `playwright install --with-deps chromium` in a folder the service can read, named by `BRICK_RUNNER_BROWSERS`. For `BRICK_RUNNER_BROWSER=chrome`: `playwright install --with-deps chrome` (Google Chrome in `/opt/google/chrome`) and the `xvfb` package.

## Evaluation budgets

An evaluation gains from each new case, and nothing else makes it pay for one. So:

- **Budget.** `budget` in `policy.yml` (`max_cases`, `max_seconds`, `max_agent_calls`, `max_agent_tokens`) limits one run of one split. Over `max_cases`, the run fails before it starts; over the others, it fails with a `budget:` reason. `policy.yml` is human-only and comes from the base commit, so a change cannot raise its own budget. An agent budget can only be lower than the runner's limits.
- **Cost line.** Each comment says what a run cost: cases, seconds, agent calls and tokens. An evaluation change shows the cost before and after, and the difference.
- **Dead cases.** The defect check records which development cases catch which mutants (a case catches a mutant when its metric falls, or it gives no score). The comment says how many cases catch one. A change gets a bloat warning when cases catch nothing, when it adds cases and catches no more mutants than the last passed defect check, or when a run takes more than 1.5 times as long. A warning does not fail the change; the budget is the hard limit.

## Limits

- The runner runs evaluations one at a time (a file lock). There is no queue service.
- Workers run as root mapped into a user namespace. The read-only mounts stop writes, but a separate unprivileged user for the runner, on its own machine, is still the stronger setup.
- A brick can skip up to `max_out_of_evidence` of its hardest cases. Keep the cap low.
- The defect check mutates `src/` and `input/adapters/`, not `runner/` or `input/config.yml`. A brick with nothing to mutate cannot be promoted.
- Promotion checks direct dependents only. A dependent that was never promoted is checked with its base code.
- The brick repository's third-party dependencies must be installed in the runner's Python environment. The environment lock file is pinned in the evaluation ID, but the runner does not install it.
- The candidate's web app runs in Chromium in the same sandbox as the evaluator, with Chromium's own sandbox off. A Chromium exploit could change the observation. Measure what matters from outside the page (Playwright and DevTools), not from values the page reports.
- A judge prompt contains the output under evaluation, so a candidate can try to talk the judge into a high score. Keep hard gates on checks that need no judge.
- The agent sees the candidate's page, so a page can show text that tries to steer it. The agent only drives the page in the sandbox, and the score comes from facts, so the worst a page can do is make its own task fail.
- Agent cases are not part of the defect check. Their gates show what a person can do with the app; the defect check shows that the other gates can fail.
- Selection is `best` only. Score correction for many trials is later work; the ledger keeps every trial, so it can be applied to old epochs.

## Tests

```sh
pip install -e .
BRICK_FRAMEWORK=<path to a brick-framework checkout> python -m unittest discover -s tests
```
