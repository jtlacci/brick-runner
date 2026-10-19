"""Evaluator worker: runs the brick's evaluation code, inside the sandbox.

It loads ``bricks/<brick>/evaluation/evaluate.py`` WITHOUT importing the brick:
the brick package is replaced by a stub whose ``run`` refuses to work, so no
candidate code executes in this process. Only ``contract.py`` (human-only,
from the base commit) and the evaluation files are loaded.

Commands (stdin {"command", "brick", ...}):
- gates:    {} -> {"gates": [[term, op, threshold], ...]}
- build:    {"records"} -> {"inputs": [{"input"} | {"error"}]}
- score:    {"items": [{"record", "output", "later"}]} -> {"scores": [{"terms"} | {"error"}]}
- generate: {"seeds"} -> {"generated": {seed: [records]}}
"""

from __future__ import annotations

import importlib
import json
import math
from pathlib import Path
import sys
import traceback
import types

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import finish, start  # noqa: E402


def load(workspace: Path, brick: str):
    bricks = types.ModuleType("bricks")
    bricks.__path__ = [str(workspace / "bricks")]
    sys.modules["bricks"] = bricks
    package = types.ModuleType(f"bricks.{brick}")
    package.__path__ = [str(workspace / "bricks" / brick)]

    def run(*_args, **_kwargs):
        raise RuntimeError("evaluation code may not call run(); the runner runs the brick")

    package.run = run
    sys.modules[f"bricks.{brick}"] = package
    return importlib.import_module(f"bricks.{brick}.evaluation.evaluate")


def failure() -> dict:
    return {"error": traceback.format_exc(limit=6)[-2000:]}


def main() -> None:
    payload, stream = start()
    try:
        module = load(Path(sys.argv[1]), payload["brick"])
    except Exception:
        finish(stream, {"fatal": "cannot load evaluate.py:\n" + traceback.format_exc(limit=5)})
        return
    command = payload["command"]
    if command == "gates":
        try:
            gates = [list(g) for g in getattr(module, "GATES", ())]
            json.dumps(gates)
        except Exception:
            finish(stream, {"fatal": "GATES is not a list of (term, operator, threshold)"})
            return
        finish(stream, {"gates": gates})
    elif command == "build":
        inputs = []
        for record in payload["records"]:
            try:
                built = module.build(record)
                json.dumps(built)
                inputs.append({"input": built})
            except Exception:
                inputs.append(failure())
        finish(stream, {"inputs": inputs})
    elif command == "score":
        scores = []
        for item in payload["items"]:
            try:
                terms = {str(k): float(v) for k, v in dict(module.score(item["record"], item["output"], item["later"])).items()}
                bad = [k for k, v in terms.items() if not math.isfinite(v)]
                if bad:
                    raise ValueError(f"score() returned non-finite terms: {bad}")
                scores.append({"terms": terms})
            except Exception:
                scores.append(failure())
        finish(stream, {"scores": scores})
    elif command == "generate":
        generated = {}
        for seed in payload["seeds"]:
            try:
                generated[str(seed)] = list(module.generate(seed))
            except Exception:
                finish(stream, {"fatal": f"generate({seed}) failed:\n" + traceback.format_exc(limit=5)})
                return
        finish(stream, {"generated": generated})
    else:
        finish(stream, {"fatal": f"unknown command {command!r}"})


if __name__ == "__main__":
    main()
