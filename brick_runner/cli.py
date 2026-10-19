"""Command line for the evaluation runner. Every command prints one JSON object.

    brick-runner submit   --brick B --commit C [--split development|validation]   # code loop
    brick-runner evaluation-change --brick B --commit C                           # evaluation loop
    brick-runner holdout  --brick B                                               # one holdout run
    brick-runner promote  --brick B
    brick-runner ingest   --spool DIR
    brick-runner dataset  --brick B [--include-synthetic]                         # prints a data.lock value
    brick-runner sync                                                             # DVC add and push
    brick-runner trials   --brick B

Global options: --repo (the brick repository), --home (runner state; default
$BRICK_RUNNER_HOME), --base (the ref that holds the pinned evaluation; default main).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .engine import Runner, RunnerError
from .gitrepo import GitError
from .policy import PolicyError
from .sandbox import SandboxError
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

    commands.add_parser("sync", help="pin the store with DVC and push it")

    args = parser.parse_args(argv)
    try:
        result = dispatch(args)
    except (RunnerError, GitError, PolicyError, StoreError, SandboxError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("passed", True) is not False else 1


def dispatch(args: argparse.Namespace) -> dict:
    home = args.home.resolve()
    if args.command == "ingest":
        return {"ingested": ingest_spool(Store(home / "store"), args.spool)}
    if args.command == "sync":
        Store(home / "store").sync()
        return {"synced": True}
    if args.command == "dataset":
        store = Store(home / "store")
        sources = {"live"} | ({"synthetic"} if args.include_synthetic else set())
        ids = [r["id"] for r in store.query(kind="run", brick=args.brick) if r.get("source") in sources]
        if not ids:
            raise StoreError(f"no stored runs for {args.brick}")
        return {"data.lock": store.create_dataset(ids), "records": len(ids)}
    runner = Runner(args.repo, home, args.base)
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


if __name__ == "__main__":
    sys.exit(main())
