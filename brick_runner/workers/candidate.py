"""Brick worker: runs one brick's run() for each case the parent sends, inside the sandbox.

The parent starts it with the workspace path as argument and sends JSON lines:

    {"brick": "<name>"}                       first line: which brick to load
    {"op": "case", "input": {...}}            run once
    {"op": "exit"}

For every io call the brick makes, the worker asks the parent and waits:

    worker -> {"op": "external", "name", "request"}  /  {"op": "sibling", "brick", "request"}
    parent -> {"response": ...}  or  {"raise": "out_of_evidence" | "error", "message": ...}

When a case ends the worker sends {"op": "done", "output": ...} or {"op": "done", "error": {...}}.

The replay data, the out-of-evidence decision, sibling calls and the
determinism check all live in the parent. Nothing this process reports
about io is trusted; it only reports outputs.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import guard  # noqa: E402


class Channel:
    def __init__(self) -> None:
        self.inbox = sys.stdin
        self.outbox = os.fdopen(os.dup(1), "w", encoding="utf-8")
        sys.stdout = sys.stderr  # anything the brick prints goes to the log

    def send(self, message: dict) -> None:
        self.outbox.write(json.dumps(message) + "\n")
        self.outbox.flush()

    def receive(self) -> dict:
        line = self.inbox.readline()
        if not line:
            raise SystemExit(0)
        return json.loads(line)


class RemoteError(RuntimeError):
    """A replayed call failed the way it failed when it was recorded, or a sibling failed."""


class BrokerIO:
    def __init__(self, channel: Channel, runtime) -> None:
        self.channel = channel
        self.runtime = runtime

    def _ask(self, message: dict):
        self.channel.send(message)
        reply = self.channel.receive()
        if "raise" in reply:
            if reply["raise"] == "out_of_evidence":
                raise self.runtime.OutOfEvidence(reply.get("message", ""))
            raise RemoteError(reply.get("message", ""))
        return reply["response"]

    def external(self, name, request, call):
        return self._ask({"op": "external", "name": name, "request": request})

    def sibling(self, brick, request, call):
        return self._ask({"op": "sibling", "brick": brick, "request": request})


def main() -> None:
    channel = Channel()
    sys.addaudithook(guard)
    workspace = Path(sys.argv[1])
    sys.path.insert(0, str(workspace))
    first = channel.receive()
    try:
        from bricks import _runtime
        run = importlib.import_module(f"bricks.{first['brick']}").run
    except Exception:
        channel.send({"op": "fatal", "message": "cannot import the brick:\n" + traceback.format_exc(limit=5)})
        return
    channel.send({"op": "ready"})
    io = BrokerIO(channel, _runtime)
    while True:
        message = channel.receive()
        if message.get("op") != "case":
            return
        token = _runtime.use_io(io)
        try:
            output = run(message["input"])
            json.dumps(output)
            channel.send({"op": "done", "output": output})
        except Exception as exc:
            channel.send({"op": "done", "error": {"type": type(exc).__name__, "message": str(exc)[:500],
                                                  "traceback": traceback.format_exc(limit=8)[-2000:]}})
        finally:
            _runtime._io.reset(token)


if __name__ == "__main__":
    main()
