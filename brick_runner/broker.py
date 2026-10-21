"""The io broker: the trusted side of every replayed call.

The brick under evaluation runs in a candidate worker. Each io call it makes
comes here. The broker answers from the case's pinned exchanges:

| Call | Recorded | Answer |
| --- | --- | --- |
| external | yes | the recorded response (or the recorded failure) |
| external | no | out of evidence: the case is excluded and counted |
| external named in live_externals | no | the runner asks the provider (live.py), cached by request |
| sibling, nondeterministic | yes | the recorded response |
| sibling, deterministic | hash | the sibling runs again in a dependency worker; a different hash is a mismatch |
| sibling, nondeterministic | no | the sibling runs again in a dependency worker (regenerated, counted, never stored) |

Dependency workers run sibling bricks from a separate workspace that holds no
candidate code of the brick under evaluation, so the candidate cannot fake a
sibling's response, a mismatch or an out-of-evidence decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any, Callable

from .hashing import digest
from .sandbox import Process, Sandbox, SandboxError


@dataclass
class CaseState:
    table: dict[tuple[str, str], list[dict[str, Any]]]
    used: dict[tuple[str, str], int] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)   # recorded exchanges the case used, in order
    out_of_evidence: bool = False
    mismatches: int = 0
    regenerated: int = 0

    @classmethod
    def from_exchanges(cls, exchanges: list[dict[str, Any]]) -> "CaseState":
        table: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for exchange in sorted(exchanges, key=lambda e: (e.get("time", ""), e.get("seq", 0), e.get("id", ""))):
            table.setdefault((exchange["name"], exchange["request_hash"]), []).append(exchange)
        return cls(table)

    def take(self, name: str, request: Any) -> dict[str, Any] | None:
        """The next unused recording for this exact request, or None when there is none left."""
        key = (name, digest(request))
        records = self.table.get(key, [])
        index = self.used.get(key, 0)
        if index >= len(records):
            return None
        self.used[key] = index + 1
        self.calls.append({"name": name, "request": request})
        return records[index]


class Broker:
    def __init__(self, sandbox: Sandbox, dependencies: Path, deterministic: Callable[[str], bool],
                 deadline: float, live: Any = None) -> None:
        self.live = live
        self.sandbox = sandbox
        self.dependencies = dependencies
        self.deterministic = deterministic
        self.deadline = deadline
        self.workers: dict[str, Process] = {}

    def start(self, brick: str, workspace: Path) -> Process:
        worker = self.sandbox.start("candidate.py", workspace)
        worker.send({"brick": brick})
        reply = worker.receive(self.deadline)
        if reply.get("op") != "ready":
            worker.stop()
            raise SandboxError(f"{brick} could not start: {reply.get('message', reply)}", worker.log_text())
        return worker

    def dependency(self, brick: str) -> Process:
        if brick not in self.workers:
            self.workers[brick] = self.start(brick, self.dependencies)
        return self.workers[brick]

    def run_case(self, worker: Process, payload: Any, state: CaseState) -> dict[str, Any]:
        worker.send({"op": "case", "input": payload})
        while True:
            message = worker.receive(self.deadline)
            op = message.get("op")
            if op == "done":
                result = {k: message[k] for k in ("output", "error") if k in message}
                return result or {"error": {"type": "ProtocolError", "message": "empty result"}}
            if op == "external":
                worker.send(self._external(state, message))
            elif op == "sibling":
                worker.send(self._sibling(state, message))
            else:
                worker.send({"raise": "error", "message": f"unknown request {op!r}"})

    def _external(self, state: CaseState, message: dict[str, Any]) -> dict[str, Any]:
        name = f"external:{message.get('name')}"
        record = state.take(name, message.get("request"))
        if record is None and self.live is not None and message.get("name") in self.live.names:
            from .live import LiveError
            try:
                response = self.live.answer(str(message.get("name")), message.get("request"))
            except LiveError as exc:
                if exc.transient or exc.fatal:
                    raise   # the evaluation stops: an outage is retried, a used-up budget fails the run
                return {"raise": "error", "message": f"live {name}: {exc}"}
            state.calls.append({"name": name, "request": message.get("request"), "live": True})
            return {"response": response}
        if record is None:
            state.out_of_evidence = True
            return {"raise": "out_of_evidence", "message": f"no recorded response for {name}"}
        if "error" in record:
            return {"raise": "error", "message": f"recorded failure: {record['error']}"}
        return {"response": record["response"]}

    def _sibling(self, state: CaseState, message: dict[str, Any]) -> dict[str, Any]:
        brick, request = str(message.get("brick")), message.get("request")
        record = state.take(f"sibling:{brick}", request)
        if record is not None and "response" in record:
            return {"response": record["response"]}
        if record is not None and "error" in record:
            return {"raise": "error", "message": f"recorded failure: {record['error']}"}
        result = self.run_case(self.dependency(brick), request, state)
        if state.out_of_evidence:
            return {"raise": "out_of_evidence", "message": f"{brick} has no evidence for this request"}
        if "error" in result:
            return {"raise": "error", "message": f"{brick} failed: {result['error'].get('message')}"}
        response = result["output"]
        if self.deterministic(brick):
            expected = record.get("response_hash") if record else None
            if expected and digest(response) != expected:
                state.mismatches += 1
        else:
            state.regenerated += 1
        return {"response": response}

    def close(self) -> None:
        for worker in self.workers.values():
            try:
                worker.send({"op": "exit"})
            except SandboxError:
                pass
            worker.stop()
        self.workers.clear()


def run_cases(sandbox: Sandbox, brick: str, candidate: Path, dependencies: Path,
              deterministic: Callable[[str], bool], cases: list[dict[str, Any]],
              timeout: float = 600, live: Any = None) -> tuple[list[dict[str, Any]], str]:
    """Run every case through a fresh candidate worker. Returns (results, candidate log)."""
    broker = Broker(sandbox, dependencies, deterministic, time.monotonic() + timeout, live)
    worker = broker.start(brick, candidate)
    results = []
    try:
        for case in cases:
            state = CaseState.from_exchanges(case["exchanges"])
            result = broker.run_case(worker, case["input"], state)
            if state.out_of_evidence:
                result = {"out_of_evidence": True}
            result.update(mismatches=state.mismatches, regenerated=state.regenerated, calls=state.calls)
            results.append(result)
        worker.send({"op": "exit"})
    finally:
        worker.stop()
        broker.close()
    return results, worker.log_text()
