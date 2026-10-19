"""A real brick repository for the runner's tests, built on the framework branch.

Two bricks:
- market_data: a source brick. One external source (prices). Nondeterministic.
- signal: deterministic. Depends on market_data. Buys below a threshold.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import textwrap

FRAMEWORK = Path(os.environ.get("BRICK_FRAMEWORK", "/tmp/brick-framework"))
GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@localhost"]

MARKET_DATA = {
    "contract.py": '''
        """market_data: latest price for a symbol."""
        from typing import TypedDict

        CONTRACT_VERSION = 1
        LANE = "strict"
        DETERMINISTIC = False
        SIBLING_DEPENDENCIES: dict[str, str] = {}
        OWNED_STATE: tuple[str, ...] = ()


        class BrickInput(TypedDict):
            symbol: str


        class BrickOutput(TypedDict):
            symbol: str
            price: float
    ''',
    "input/adapters/prices.py": '''
        """The prices source. In these tests the live call reads FAKE_PRICES."""
        import json
        import os


        def fetch(symbol: str, ctx: dict) -> dict:
            return ctx["io"].external("prices", {"symbol": symbol}, _fetch_live)


        def _fetch_live(request: dict) -> dict:
            return {"price": json.loads(os.environ["FAKE_PRICES"])[request["symbol"]]}
    ''',
    "src/logic.py": '''
        from ..input.adapters import prices


        def execute(inputs, ctx):
            raw = prices.fetch(inputs["symbol"], ctx)
            return {"symbol": inputs["symbol"], "price": float(raw["price"])}
    ''',
    "evaluation/evaluate.py": '''
        """market_data is correct when it returns the price the source gave."""
        from ..contract import BrickInput

        GATES = (("matches_source", ">=", 1.0),)


        def build(record: dict) -> BrickInput:
            return record["input"]


        def score(record: dict, output: dict | None, later: list[dict]) -> dict[str, float]:
            source = [e for e in record["exchanges"] if e["name"] == "external:prices" and "response" in e]
            ok = output is not None and bool(source) and output["price"] == source[0]["response"]["price"]
            return {"matches_source": 1.0 if ok else 0.0}


        def generate(seed: int) -> list[dict]:
            import random

            rng = random.Random(seed)
            items = []
            for _ in range(20):
                symbol = rng.choice(["BTC", "ETH", "SOL"])
                items.append({"input": {"symbol": symbol}, "exchanges": [
                    {"name": "external:prices", "request": {"symbol": symbol},
                     "response": {"price": round(rng.uniform(10, 100), 2)}}]})
            return items
    ''',
}

SIGNAL = {
    "contract.py": '''
        """signal: buy when the price is below a threshold."""
        from typing import TypedDict

        CONTRACT_VERSION = 1
        LANE = "strict"
        DETERMINISTIC = True
        SIBLING_DEPENDENCIES = {"market_data": "eventual"}
        OWNED_STATE: tuple[str, ...] = ()


        class BrickInput(TypedDict):
            symbol: str
            threshold: float


        class BrickOutput(TypedDict):
            action: str
    ''',
    "input/adapters/market_data.py": '''
        from bricks.market_data import run


        def latest_price(symbol: str, ctx: dict) -> float:
            response = ctx["io"].sibling("market_data", {"symbol": symbol}, run)
            return float(response["price"])
    ''',
    "src/logic.py": '''
        def execute(inputs, ctx):
            raise NotImplementedError
    ''',
    "evaluation/evaluate.py": '''
        """signal is correct when it buys exactly when the recorded price is below the threshold."""
        from ..contract import BrickInput

        GATES = (("accuracy", ">=", 0.95),)


        def build(record: dict) -> BrickInput:
            return record["input"]


        def _price(record: dict) -> float:
            for exchange in record["exchanges"]:
                if exchange["name"] == "sibling:market_data" and "response" in exchange:
                    return exchange["response"]["price"]
            raise LookupError("no recorded price")


        def score(record: dict, output: dict | None, later: list[dict]) -> dict[str, float]:
            expected = "buy" if _price(record) < record["input"]["threshold"] else "hold"
            return {"accuracy": 1.0 if output is not None and output.get("action") == expected else 0.0}


        def generate(seed: int) -> list[dict]:
            import random

            rng = random.Random(seed)
            items = []
            for _ in range(40):
                symbol = rng.choice(["BTC", "ETH", "SOL"])
                price = round(rng.uniform(10, 100), 2)
                items.append({"input": {"symbol": symbol, "threshold": round(rng.uniform(10, 100), 2)},
                              "exchanges": [{"name": "sibling:market_data", "request": {"symbol": symbol},
                                             "response": {"symbol": symbol, "price": price}}]})
            return items
    ''',
}

SIGNAL_CORRECT = '''
    from ..input.adapters import market_data


    def execute(inputs, ctx):
        price = market_data.latest_price(inputs["symbol"], ctx)
        if price < inputs["threshold"]:
            return {"action": "buy"}
        return {"action": "hold"}
'''

SIGNAL_BUGGY = SIGNAL_CORRECT.replace("price < inputs", "price > inputs")
SIGNAL_OTHER_SYMBOL = SIGNAL_CORRECT.replace('latest_price(inputs["symbol"], ctx)', 'latest_price(inputs["symbol"] + "-PERP", ctx)')
SIGNAL_NETWORK = '''
    import socket

    from ..input.adapters import market_data


    def execute(inputs, ctx):
        socket.create_connection(("example.com", 80), timeout=1)
        return {"action": "hold"}
'''
SCORE_ALWAYS_PASSES = '''
    def score(record: dict, output: dict | None, later: list[dict]) -> dict[str, float]:
        return {"accuracy": 1.0}
'''


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


def git(root: Path, *args: str) -> str:
    return subprocess.run([*GIT, *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def commit(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def make_repo(root: Path) -> Path:
    for name in ("AGENTS.md", "README.md", "bricks/AGENTS.md", "bricks/__init__.py", "bricks/_runtime.py", "bricks/_yaml.py"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FRAMEWORK / name, root / name)
    shutil.copytree(FRAMEWORK / "tools", root / "tools", ignore=shutil.ignore_patterns("__pycache__", "tests"))
    for brick, files in (("market_data", MARKET_DATA), ("signal", SIGNAL)):
        target = root / "bricks" / brick
        shutil.copytree(FRAMEWORK / "bricks/example_brick", target, ignore=shutil.ignore_patterns("__pycache__"))
        (target / "input/adapters/example_source.py").unlink()
        for relative, text in files.items():
            write(target, relative, text)
        policy = target / "evaluation/policy.yml"
        policy.write_text(policy.read_text().replace("metric: score", "metric: " + (
            "accuracy" if brick == "signal" else "matches_source")).replace("min_holdout: 20", "min_holdout: 10"))
    git(root, "init", "-q", "-b", "main")
    commit(root, "two bricks")
    return root


def lint(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["python3", "tools/lint_bricks.py", "--root", "."], cwd=root, capture_output=True, text=True)
