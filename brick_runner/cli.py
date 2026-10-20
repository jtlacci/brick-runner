"""Command line for the evaluation runner. Every command prints one JSON object.

    brick-runner submit   --brick B --commit C [--split development|validation]   # code loop
    brick-runner evaluation-change --brick B --commit C                           # evaluation loop
    brick-runner holdout  --brick B                                               # one holdout run
    brick-runner promote  --brick B
    brick-runner ingest   --spool DIR
    brick-runner dataset  --brick B [--include-synthetic]                         # prints a data.lock value
    brick-runner trials   --brick B
    brick-runner plan     --head C                                                # what a change asks for

    brick-runner remote   --url s3://bucket/prefix [--region R] [--git URL]       # set the backup remotes
    brick-runner sync                                                             # back up to the remotes
    brick-runner restore  --git URL                                               # rebuild --home on a new machine

    brick-runner serve    --github owner/name --allow login[,login] [--interval 60] [--once]

Global options: --repo (the brick repository), --home (runner state; default
$BRICK_RUNNER_HOME), --base (the ref that holds the pinned evaluation; default main).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from . import backup
from .engine import Runner, RunnerError
from .github import GitHub, GitHubError
from .gitrepo import GitError
from .plan import make_plan
from .policy import PolicyError
from .sandbox import SandboxError
from .serve import Service
from .store import Store, StoreError, ingest_spool


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brick-runner", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("BRICK_RUNNER_HOME", "~/.brick-runner")).expanduser())
    parser.add_argument("--base", default="main")
    commands = parser.add_subparsers(dest="command", required=True)

    submit = commands.add_parser("submit", help="evaluate a code-loop candidate")
    submit.add_argument("--brick", required=True)
    submit.add_argument("--commit", required=True)
    submit.add_argument("--split", default="development", choices=["development", "validation"])

    change = commands.add_parser("evaluation-change", help="check an evaluation-loop candidate")
    change.add_argument("--brick", required=True)
    change.add_argument("--commit", required=True)

    holdout = commands.add_parser("holdout", help="run the holdout for the best candidate")
    holdout.add_argument("--brick", required=True)
    holdout.add_argument("--rerun-reason", help="a human's recorded reason to run a candidate's holdout again")

    for name, text in (("promote", "promote the holdout winner"), ("trials", "list this brick's trials")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--brick", required=True)

    ingest = commands.add_parser("ingest", help="move captured records from a spool into the store")
    ingest.add_argument("--spool", type=Path, required=True)

    dataset = commands.add_parser("dataset", help="pin every stored run of a brick as a dataset")
    dataset.add_argument("--brick", required=True)
    dataset.add_argument("--include-synthetic", action="store_true")

    plan = commands.add_parser("plan", help="show which loop a change belongs to")
    plan.add_argument("--head", required=True)

    remote = commands.add_parser("remote", help="set the DVC remote (S3) and an optional Git remote")
    remote.add_argument("--url", help="for example s3://my-bucket/brick-runner; omit to show the current remotes")
    remote.add_argument("--region")
    remote.add_argument("--git", help="a private Git repository for the store's pointer files")

    sync = commands.add_parser("sync", help="snapshot the ledger, pin the store with DVC, push to the remotes")
    sync.add_argument("--no-push", action="store_true")

    restore = commands.add_parser("restore", help="rebuild --home from the Git remote and the DVC remote")
    restore.add_argument("--git", required=True)

    serve = commands.add_parser("serve", help="poll GitHub, evaluate pull requests, post results")
    serve.add_argument("--github", required=True, help="owner/name of the brick repository")
    serve.add_argument("--allow", default="", help="GitHub logins that may run /brick holdout and /brick promote")
    serve.add_argument("--branch", default="main", help="the protected branch; only pull requests into it run")
    serve.add_argument("--interval", type=float, default=60.0)
    serve.add_argument("--sync-every", type=float, default=600.0)
    serve.add_argument("--once", action="store_true", help="run one cycle and stop")

    args = parser.parse_args(argv)
    try:
        result = dispatch(args)
    except (RunnerError, GitError, PolicyError, StoreError, SandboxError, GitHubError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("passed", True) is not False else 1


def dispatch(args: argparse.Namespace) -> dict:
    home = args.home.resolve()
    if args.command == "ingest":
        return {"ingested": ingest_spool(Store(home / "store"), args.spool)}
    if args.command == "remote":
        if not args.url:
            return backup.status(home)
        return backup.configure(home, args.url, region=args.region, git=args.git)
    if args.command == "sync":
        return backup.sync(home, push=not args.no_push)
    if args.command == "restore":
        return backup.restore(home, args.git)
    if args.command == "serve":
        token = os.environ.get("BRICK_RUNNER_GITHUB_TOKEN") or _read_token(home)
        service = Service(GitHub(args.github, token), home, allowed={a for a in args.allow.split(",") if a},
                          branch=args.branch, sync_every=args.sync_every)
        if args.once:
            service.cycle()
            return {"cycle": "done"}
        service.run_forever(args.interval)
    if args.command == "dataset":
        store = Store(home / "store")
        sources = {"live"} | ({"synthetic"} if args.include_synthetic else set())
        ids = [r["id"] for r in store.query(kind="run", brick=args.brick) if r.get("source") in sources]
        if not ids:
            raise StoreError(f"no stored runs for {args.brick}")
        return {"data.lock": store.create_dataset(ids), "records": len(ids)}
    runner = Runner(args.repo, home, args.base)
    if args.command == "plan":
        repo = runner.repo
        return make_plan(repo, runner.base(), repo.commit(args.head)).as_dict()
    if args.command == "submit":
        return runner.submit(args.brick, args.commit, args.split)
    if args.command == "evaluation-change":
        return runner.evaluation_change(args.brick, args.commit)
    if args.command == "holdout":
        return runner.holdout(args.brick, args.rerun_reason)
    if args.command == "promote":
        return runner.promote(args.brick)
    if args.command == "trials":
        entries = runner.ledger.find(args.brick)
        return {"trials": [{"run": e.run_id,
                            "metrics": {} if e.tag("split") == "holdout" else e.metrics,
                            **{k: v for k, v in e.tags.items() if not k.startswith("mlflow.")}}
                           for e in entries]}
    raise RunnerError(f"unknown command {args.command}")


def _read_token(home: Path) -> str:
    path = home / "github-token"
    if not path.exists():
        raise GitHubError("set BRICK_RUNNER_GITHUB_TOKEN or write the token to <home>/github-token")
    return path.read_text(encoding="utf-8").strip()


if __name__ == "__main__":
    sys.exit(main())
