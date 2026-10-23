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
- observe:  {"inputs", "browser", "agent"} -> {"observations": [{"output"} | {"error"}]}   (web bricks)
            The built app (``.web-build/`` in this workspace) is served on
            127.0.0.1 with the headers the app declares, the browser starts
            (headless Chromium, or Google Chrome headed on an Xvfb screen),
            then evaluate.observe(input, browser, base_url[, agent]) returns
            what it saw. The guard is armed after the server, the screen and
            the browser start, before any evaluation code runs.
            observe speaks one JSON object per line: {"op": "agent", ...}
            asks the runner for a computer use step (see _agent.py), and
            {"op": "result", ...} ends it.
- judge_requests: {"items": [{"record", "output", "calls"}]} (calls only when judge_requests() takes them)
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
HEADERS_COPY = ".web-headers.json"
SCREEN = "1920x1200x24"


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


def serve(root: Path, headers: dict[str, str] | None = None):
    """A static server for the built app on 127.0.0.1; unknown paths get index.html (a single-page app).

    ``headers`` (from app/headers.json) go on every answer, as in production.
    """
    import functools
    import http.server
    import threading

    extra = dict(headers or {})

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def end_headers(self) -> None:
            for name, value in extra.items():
                self.send_header(name, value)
            super().end_headers()

        def send_head(self):
            target = Path(self.translate_path(self.path))
            if not target.exists() and "." not in Path(self.path.split("?", 1)[0]).name:
                self.path = "/index.html"
            return super().send_head()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def screen(xvfb: str) -> object:
    """Start Xvfb for a headed browser and point DISPLAY at it. The guard is not armed yet.

    In the sandbox /tmp and the network are the worker's own, so a fixed display
    number cannot meet another worker's X server. (Unsandboxed, for development,
    the number comes from the process ID.) Xvfb gets SIGKILL when this worker dies
    (PR_SET_PDEATHSIG), so a killed evaluation leaves no X server.
    """
    import ctypes
    import os
    import signal
    import subprocess
    import time

    libc = ctypes.CDLL(None, use_errno=True)

    def die_with_parent() -> None:
        libc.prctl(1, signal.SIGKILL)   # PR_SET_PDEATHSIG

    number = 99 if os.environ.get("BRICK_RUNNER_SANDBOXED", "1") == "1" else 100 + os.getpid() % 5000
    log_path = Path(os.environ.get("TMPDIR") or ".") / f"xvfb-{os.getpid()}.log"
    with open(log_path, "wb") as log:
        process = subprocess.Popen([xvfb, f":{number}", "-screen", "0", SCREEN, "-nolisten", "tcp"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                                   preexec_fn=die_with_parent)
    socket = Path(f"/tmp/.X11-unix/X{number}")
    deadline = time.monotonic() + 30
    while not socket.exists():
        if process.poll() is not None or time.monotonic() > deadline:
            process.kill()   # first, so the log is complete and nothing blocks
            process.wait()
            raise RuntimeError("Xvfb did not start: " + log_path.read_bytes()[-500:].decode(errors="replace"))
        time.sleep(0.05)
    os.environ["DISPLAY"] = f":{number}"
    return process


def launch(driver, settings: dict):
    if settings.get("name") == "chrome":
        return driver.chromium.launch(executable_path=settings["executable"], headless=False,
                                      args=["--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
                                            "--password-store=basic"])
    return driver.chromium.launch(args=["--disable-dev-shm-usage"])


def observe(stream, workspace: Path, payload: dict) -> None:
    import inspect

    def end(result: dict) -> None:
        stream.write(json.dumps({"op": "result", **result}) + "\n")
        stream.flush()

    root = workspace / BUILD_FOLDER
    if not (root / "index.html").is_file():
        end({"fatal": "the built app has no index.html"})
        return
    headers_file = workspace / HEADERS_COPY
    headers = json.loads(headers_file.read_text()) if headers_file.is_file() else {}
    settings = payload.get("browser") or {"name": "chromium"}
    from playwright.sync_api import sync_playwright
    from _agent import Agent, channel
    xvfb = server = driver = browser = None
    observations = []
    try:
        try:
            xvfb = screen(str(settings.get("xvfb") or "Xvfb")) if settings.get("headed") else None
            server = serve(root, headers)
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            driver = sync_playwright().start()
            browser = launch(driver, settings)
        except Exception as exc:
            end({"fatal": f"the browser did not start: {type(exc).__name__}: {str(exc)[:500]}"})
            return
        # The guard is armed before any evaluation code runs, its import included.
        arm()
        try:
            module = load(workspace, payload["brick"])
        except Exception:
            end({"fatal": "cannot load evaluate.py:\n" + traceback.format_exc(limit=5)})
            return
        if not hasattr(module, "observe"):
            end({"fatal": "a web brick's evaluate.py must define observe(input, browser, base_url)"})
            return
        try:
            takes_agent = "agent" in inspect.signature(module.observe).parameters
        except (TypeError, ValueError):
            takes_agent = False
        agent = Agent(channel(stream, sys.stdin)) if payload.get("agent") and takes_agent else None
        for item in payload["inputs"]:
            try:
                seen = module.observe(item, browser, base_url, agent=agent) if takes_agent else \
                    module.observe(item, browser, base_url)
                json.dumps(seen)
                observations.append({"output": seen})
            except Exception:
                observations.append(failure())
    finally:
        for stop in (lambda: browser.close(), lambda: driver.stop(), lambda: server.shutdown(), lambda: xvfb.kill()):
            try:
                stop()
            except Exception:
                pass
    end({"observations": observations})


def main() -> None:
    payload, stream = start(armed=False)
    command = payload["command"]
    if command == "observe":
        observe(stream, Path(sys.argv[1]), payload)
        return
    arm()
    try:
        module = load(Path(sys.argv[1]), payload["brick"])
    except Exception:
        finish(stream, {"fatal": "cannot load evaluate.py:\n" + traceback.format_exc(limit=5)})
        return
    if command == "judge_requests":
        if not hasattr(module, "judge_requests"):
            finish(stream, {"judge": None, "requests": None})
            return
        judge = getattr(module, "JUDGE", None)
        if not isinstance(judge, dict) or not isinstance(judge.get("model"), str):
            finish(stream, {"fatal": "judge_requests() needs JUDGE = {\"model\": \"<model id>\"}"})
            return
        import inspect
        try:
            takes_calls = "calls" in inspect.signature(module.judge_requests).parameters
        except (TypeError, ValueError):
            takes_calls = False
        requests = []
        for item in payload["items"]:
            try:
                extra = {"calls": item.get("calls", [])} if takes_calls else {}
                made = [dict(r) for r in module.judge_requests(item["record"], item["output"], **extra)]
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
        agent = getattr(module, "AGENT", None)
        if agent is not None:
            if not (isinstance(agent, dict) and isinstance(agent.get("model"), str)
                    and isinstance(agent.get("terms"), (list, tuple)) and all(isinstance(x, str) for x in agent["terms"])):
                finish(stream, {"fatal": 'AGENT must be {"model": "<model id>", "terms": ["<term>", ...]}'})
                return
            agent = {"model": agent["model"], "terms": list(agent["terms"])}
        finish(stream, {"gates": gates, "judged": hasattr(module, "judge_requests"), "agent": agent})
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
        import inspect
        try:
            accepted = set(inspect.signature(module.score).parameters)
        except (TypeError, ValueError):
            accepted = set()
        scores = []
        for item in payload["items"]:
            try:
                extra = {"judgments": item["judgments"]} if "judgments" in item else {}
                if "calls" in accepted:
                    extra["calls"] = item.get("calls", [])
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
