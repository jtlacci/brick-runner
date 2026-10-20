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
- observe:  {"inputs"} -> {"observations": [{"output"} | {"error"}]}   (web bricks)
            The built app (``.web-build/`` in this workspace) is served on
            127.0.0.1, Chromium starts, then evaluate.observe(input, browser,
            base_url) returns what it saw. The guard is armed after the server
            and the browser start, before any evaluation code runs.
- judge_requests: {"items": [{"record", "output"}]}
            -> {"judge": JUDGE, "requests": [[request, ...] | {"error"}]}
            The runner, outside the sandbox, sends each request to the pinned
            judge model; score() then gets the answers as ``judgments``.
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
from _common import arm, finish, start  # noqa: E402

BUILD_FOLDER = ".web-build"


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


def serve(root: Path):
    """A static server for the built app on 127.0.0.1; unknown paths get index.html (a single-page app)."""
    import functools
    import http.server
    import threading

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def send_head(self):
            target = Path(self.translate_path(self.path))
            if not target.exists() and "." not in Path(self.path.split("?", 1)[0]).name:
                self.path = "/index.html"
            return super().send_head()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def observe(stream, module, workspace: Path, payload: dict) -> None:
    root = workspace / BUILD_FOLDER
    if not (root / "index.html").is_file():
        finish(stream, {"fatal": "the built app has no index.html"})
        return
    if not hasattr(module, "observe"):
        finish(stream, {"fatal": "a web brick's evaluate.py must define observe(input, browser, base_url)"})
        return
    from playwright.sync_api import sync_playwright
    server = serve(root)
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    driver = sync_playwright().start()
    browser = driver.chromium.launch(args=["--disable-dev-shm-usage"])
    arm()
    observations = []
    try:
        for item in payload["inputs"]:
            try:
                seen = module.observe(item, browser, base_url)
                json.dumps(seen)
                observations.append({"output": seen})
            except Exception:
                observations.append(failure())
    finally:
        browser.close()
        driver.stop()
        server.shutdown()
    finish(stream, {"observations": observations})


def main() -> None:
    payload, stream = start(armed=False)
    command = payload["command"]
    if command != "observe":
        arm()
    try:
        module = load(Path(sys.argv[1]), payload["brick"])
    except Exception:
        finish(stream, {"fatal": "cannot load evaluate.py:\n" + traceback.format_exc(limit=5)})
        return
    if command == "observe":
        observe(stream, module, Path(sys.argv[1]), payload)
    elif command == "judge_requests":
        if not hasattr(module, "judge_requests"):
            finish(stream, {"judge": None, "requests": None})
            return
        judge = getattr(module, "JUDGE", None)
        if not isinstance(judge, dict) or not isinstance(judge.get("model"), str):
            finish(stream, {"fatal": "judge_requests() needs JUDGE = {\"model\": \"<model id>\"}"})
            return
        requests = []
        for item in payload["items"]:
            try:
                made = [dict(r) for r in module.judge_requests(item["record"], item["output"])]
                json.dumps(made)
                requests.append(made)
            except Exception:
                requests.append(failure())
        finish(stream, {"judge": judge, "requests": requests})
    elif command == "gates":
        try:
            gates = [list(g) for g in getattr(module, "GATES", ())]
            json.dumps(gates)
        except Exception:
            finish(stream, {"fatal": "GATES is not a list of (term, operator, threshold)"})
            return
        finish(stream, {"gates": gates, "judged": hasattr(module, "judge_requests")})
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
                extra = {"judgments": item["judgments"]} if "judgments" in item else {}
                scored = module.score(item["record"], item["output"], item["later"], **extra)
                terms = {str(k): float(v) for k, v in dict(scored).items()}
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
