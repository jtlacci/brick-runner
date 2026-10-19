"""Candidate worker: runs the brick under evaluation, inside the sandbox.

Input (stdin): {"brick", "cases": [{"input", "exchanges": [...]}]}
Output: {"results": [{"output" | "error" | "out_of_evidence", "mismatches", "regenerated"}]}

For each case the worker installs a replay io in the framework runtime:

- an external call returns the recorded response, or the case is out of evidence;
- a sibling call returns the recorded response when one was stored (a
  nondeterministic sibling); a deterministic sibling is computed again and
  checked against its recorded hash; a nondeterministic sibling with no
  record for this request (its contract or the request changed) is
  regenerated and reported, so the runner can store the new record.

This process never sees the scorer: build() and score() run in another process.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import finish, start  # noqa: E402


class RecordedError(RuntimeError):
    """The recorded call failed; the replay fails the same way."""


class Replay:
    def __init__(self, exchanges: list[dict], runtime) -> None:
        self.runtime = runtime
        self.table: dict[tuple[str, str], list[dict]] = {}
        for exchange in exchanges:
            self.table.setdefault((exchange["name"], exchange["request_hash"]), []).append(exchange)
        self.used: dict[tuple[str, str], int] = {}
        self.mismatches: list[dict] = []
        self.regenerated: list[dict] = []

    def _take(self, key: tuple[str, str]) -> dict | None:
        records = self.table.get(key)
        if not records:
            return None
        index = self.used.get(key, 0)
        self.used[key] = index + 1
        return records[min(index, len(records) - 1)]

    def external(self, name, request, call):
        record = self._take((f"external:{name}", self.runtime.digest(request)))
        if record is None:
            raise self.runtime.OutOfEvidence(f"no recorded response for external:{name}")
        if "error" in record:
            raise RecordedError(f"{record['error'].get('type')}: {record['error'].get('message')}")
        return record["response"]

    def sibling(self, brick, request, call):
        name = f"sibling:{brick}"
        record = self._take((name, self.runtime.digest(request)))
        if record is not None and "response" in record:
            return record["response"]
        if record is not None and "error" in record:
            raise RecordedError(f"{record['error'].get('type')}: {record['error'].get('message')}")
        response = call(request)
        if self.runtime.is_deterministic(brick):
            expected = record.get("response_hash") if record else None
            if expected and self.runtime.digest(response) != expected:
                self.mismatches.append({"name": name, "expected": expected, "actual": self.runtime.digest(response)})
        else:
            self.regenerated.append({"name": name, "request": request, "response": response})
        return response


def main() -> None:
    payload, stream = start()
    workspace = Path(sys.argv[1])
    sys.path.insert(0, str(workspace))
    try:
        from bricks import _runtime
        run = importlib.import_module(f"bricks.{payload['brick']}").run
    except Exception:
        finish(stream, {"fatal": "cannot import the brick:\n" + traceback.format_exc(limit=5)})
        return
    results = []
    for case in payload["cases"]:
        replay = Replay(case.get("exchanges", []), _runtime)
        token = _runtime.use_io(replay)
        result: dict = {}
        try:
            output = run(case["input"])
            json.dumps(output)
            result["output"] = output
        except _runtime.OutOfEvidence as exc:
            result["out_of_evidence"] = str(exc)
        except Exception as exc:
            result["error"] = {"type": type(exc).__name__, "message": str(exc)[:500],
                               "traceback": traceback.format_exc(limit=8)[-2000:]}
        finally:
            _runtime._io.reset(token)
        result["mismatches"] = replay.mismatches
        result["regenerated"] = replay.regenerated
        results.append(result)
    finish(stream, {"results": results})


if __name__ == "__main__":
    main()
