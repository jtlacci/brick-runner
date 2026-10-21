"""The evaluation engine: trials, holdout, promotion and the evaluation loop."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import functools
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import statistics
import tempfile
import threading
import time
from typing import Any, Iterator

from . import broker, mutate, splits, web, workspace
from .agent import AgentError, Agents
from .live import Live, LiveError
from .gitrepo import Repo
from .hashing import canonical, digest
from .judge import MAX_REQUESTS_PER_CASE, MAX_REQUESTS_PER_EVALUATION, Judge, JudgeError
from .ledger import Entry, Ledger, _metric_name
from .policy import CODE, EVALUATION, Policy, contract_literal, dependents, load_lock, load_policy
from .sandbox import Sandbox, SandboxError
from .store import Store
from .workspace import Overlay, Workspace

SYNTHETIC_TIME = "1970-01-01T00:00:00+00:00"
DEFECT_STRENGTHS = (1, 2, 4)
DEFECT_SEEDS = (0, 1, 2, 3)
OPERATORS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, "<": lambda a, b: a < b}
LOG_LIMIT = 4000
OBSERVE_TIMEOUT = 600          # observe with no agent, as for every other worker
AGENT_STEP_TIME = 90           # each agent step gives the worker this much more time ...
OBSERVE_TIMEOUT_MAX = 3 * 3600  # ... up to this
NO_AGENT_KINDS = ("defect-baseline", "defect-mutant")   # the defect check runs no agent case


class RunnerError(RuntimeError):
    pass


def bloat(before: dict[str, Any] | None, after: dict[str, Any], old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Warnings for an evaluation change that costs more and catches no more.

    A case must earn its place: it catches a mutant that the baseline does not
    lose, or it is the only case of a gate. Warnings do not fail the change; the
    budget in policy.yml is the hard limit.
    """
    notes = []
    cases = after.get("cases") or {}
    if cases.get("dead"):
        notes.append(f"{cases['dead']} of {cases['scored']} development cases catch none of the "
                     f"{cases.get('mutants', 0)} mutants")
    old_cases = (old.get("counts") or {}).get("cases", 0)
    new_cases = (new.get("counts") or {}).get("cases", 0)
    if before and new_cases > old_cases:
        earlier = (before.get("cases") or {}).get("caught")
        caught_before = sum(s.get("detected", 0) for s in (before.get("strengths") or {}).values())
        caught_after = sum(s.get("detected", 0) for s in (after.get("strengths") or {}).values())
        if caught_after <= caught_before:
            notes.append(f"this change adds {new_cases - old_cases} cases and catches no more mutants "
                         f"({caught_after}, before {caught_before})")
        elif earlier is not None and cases.get("caught", 0) <= earlier:
            notes.append(f"this change adds {new_cases - old_cases} cases, and no more cases catch a mutant")
    seconds_old, seconds_new = old.get("seconds"), new.get("seconds")
    if isinstance(seconds_old, (int, float)) and isinstance(seconds_new, (int, float)) and seconds_old > 0 \
            and seconds_new > 1.5 * seconds_old:
        notes.append(f"a run takes {seconds_new:.0f} s, before {seconds_old:.0f} s")
    return notes


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# Where the deployment keeps credentials. Every worker sees these as empty folders.
DEFAULT_SECRET_PATHS = ("/etc/brick-runner", "/run/credentials", "~/.aws", "~/.ssh", "~/.config", "~/.git-credentials")


def secret_paths() -> list[Path]:
    """Folders hidden from every worker: the defaults, plus BRICK_RUNNER_HIDE (colon-separated)."""
    extra = [p for p in os.environ.get("BRICK_RUNNER_HIDE", "").split(":") if p]
    return [Path(p).expanduser() for p in (*DEFAULT_SECRET_PATHS, *extra)]


class Runner:
    def __init__(self, repo: Path, home: Path, base_ref: str = "main") -> None:
        self.repo = Repo(Path(repo).resolve())
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.home / "store")
        self.ledger = Ledger(self.home)
        self.base_ref = base_ref
        self.key = self._key()
        # Workers never see the runner's state or the repository.
        self.sandbox = Sandbox(hide=[self.home, self.repo.path, *secret_paths()])
        self.judge = Judge(self.home / "judgments")

    # ------------------------------------------------------------ basics

    def _key(self) -> bytes:
        path = self.home / "runner.key"
        if not path.exists():
            path.write_bytes(secrets.token_bytes(32))
            path.chmod(0o600)
        return path.read_bytes()

    def sign(self, value: Any) -> str:
        return hmac.new(self.key, canonical(value), hashlib.sha256).hexdigest()

    @contextlib.contextmanager
    def exclusive(self) -> Iterator[None]:
        """One evaluation at a time: later submissions wait their turn."""
        with open(self.home / "runner.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def base(self) -> str:
        return self.repo.commit(self.base_ref)

    def epoch(self, brick: str, base: str) -> str:
        """An epoch is one pinned evaluation: the Git tree of evaluation/ on the base."""
        tree = self.repo.tree(base, f"bricks/{brick}/evaluation")
        if tree is None:
            raise RunnerError(f"bricks/{brick} has no evaluation/ at {base[:12]}")
        return tree

    @functools.lru_cache(maxsize=None)
    def _deterministic(self, base: str, brick: str) -> bool:
        return contract_literal(self.repo, base, brick, "DETERMINISTIC", False) is True

    @functools.lru_cache(maxsize=None)
    def kind(self, base: str, brick: str) -> str:
        """"python" (the default) or "web" (KIND in contract.py)."""
        return str(contract_literal(self.repo, base, brick, "KIND", "python"))

    def production(self, brick: str) -> Entry | None:
        found = self.ledger.find(brick, kind="promotion")
        return found[0] if found else None

    def production_commit(self, brick: str, base: str) -> str:
        entry = self.production(brick)
        return entry.tag("commit") if entry else base

    def _evaluator(self, ws: Workspace, payload: dict[str, Any]) -> dict[str, Any]:
        workspace.verify_evaluation(self.repo, ws)
        result, _ = self.sandbox.run("evaluator.py", ws.evaluation, {"brick": ws.target, **payload})
        return result

    # ------------------------------------------------------------ data

    def dataset(self, brick: str, base: str, ws: Workspace, policy: Policy) -> tuple[str, set[str], list[dict[str, Any]]]:
        """(dataset key, every pinned record ID, the brick's pinned runs)."""
        lock = load_lock(self.repo, base, brick)
        key = self.synthetic(brick, ws, policy) if lock == "unpinned" else lock
        pinned = set(self.store.dataset(key))
        runs = [self.store.record(i) for i in sorted(pinned)]
        return key, pinned, [r for r in runs if r.get("kind") == "run" and r.get("brick") == brick]

    def synthetic(self, brick: str, ws: Workspace, policy: Policy) -> str:
        """A dataset from evaluate.generate(seed), for a brick with no real data yet."""
        result = self._evaluator(ws, {"command": "generate", "seeds": policy.seeds})
        records: list[dict[str, Any]] = []
        for seed, items in sorted(result["generated"].items(), key=lambda kv: int(kv[0])):
            for index, item in enumerate(items):
                trace = digest({"brick": brick, "generator": ws.hashes["evaluation"],
                                "seed": int(seed), "index": index})[7:39]
                time = item.get("time", SYNTHETIC_TIME)
                records.append({"kind": "run", "brick": brick, "trace_id": trace, "run_id": trace,
                                "parent_id": None, "input": item["input"], "input_hash": digest(item["input"]),
                                "time": time, "seed": int(seed), "index": index})
                for seq, exchange in enumerate(item.get("exchanges", [])):
                    name = exchange["name"] if ":" in exchange["name"] else f"external:{exchange['name']}"
                    # seq keeps two identical calls in one case apart: a record's ID is the hash of
                    # its content, so without it the store would keep only one of them.
                    records.append({"kind": "exchange", "seq": seq, "name": name, "request": exchange["request"],
                                    "request_hash": digest(exchange["request"]), "response": exchange["response"],
                                    "response_hash": digest(exchange["response"]), "trace_id": trace,
                                    "run_id": trace, "time": time})
        ids = self.store.add_records(records, source="synthetic")
        run_ids = [record_id for record_id, record in zip(ids, records) if record["kind"] == "run"]
        if not run_ids:
            raise RunnerError(f"{brick}: data.lock is unpinned and generate() returned no records")
        return self.store.create_dataset(run_ids)

    def _later(self, record: dict[str, Any], horizon: float) -> list[dict[str, Any]]:
        """Live records within the label horizon after a case. Never synthetic, never candidate-made."""
        if horizon <= 0 or record.get("source") != "live":
            return []
        start = record["time"]
        end = (datetime.fromisoformat(start) + timedelta(seconds=horizon)).isoformat(timespec="microseconds")
        return [r for r in self.store.between(start, end) if r.get("source") == "live"]

    # ------------------------------------------------------------ one evaluation

    def evaluate(self, brick: str, split: str, overlays: list[Overlay], *, base: str | None = None,
                 freeze: str | None = None, kind: str = "trial", tags: dict[str, Any] | None = None,
                 cache: bool = True, ws: Workspace | None = None, rerun_reason: str | None = None) -> dict[str, Any]:
        base = base or self.base()
        policy = load_policy(self.repo, base, brick)
        own = ws is None
        ws = ws or workspace.build(self.repo, base, overlays, brick)
        try:
            dataset_key, pinned, runs = self.dataset(brick, base, ws, policy)
            chosen = splits.assign(runs, policy.split, freeze)[split]
            if split == splits.HOLDOUT and policy.split["kind"] == "time":
                seen = {c["id"] for c in chosen}
                chosen += [self.store.expand(r) for r in self.store.query(kind="run", brick=brick, source="live")
                           if freeze and r["time"] > freeze and r["id"] not in seen]
            for record in chosen:
                within = pinned if record["id"] in pinned else None
                record["exchanges"] = [e for e in self.store.exchanges(record["trace_id"], within)
                                       if e.get("source") in ("live", "synthetic")]
            if split == splits.HOLDOUT and len(chosen) < policy.split["min_holdout"]:
                return {"status": "waiting", "have": len(chosen), "need": policy.split["min_holdout"]}
            evidence = digest(sorted([r["id"] for r in chosen] + [e["id"] for r in chosen for e in r["exchanges"]]))
            identity = {"brick": brick, "split": split, "hashes": ws.hashes, "dataset": dataset_key,
                        "evidence": evidence, "seeds": policy.seeds,
                        "policy": self.repo.tree(base, f"bricks/{brick}/evaluation/policy.yml")}
            if self.kind(base, brick) == "web":
                identity["toolchain"] = web.toolchain()
            eval_id = digest(identity)
            if split == splits.HOLDOUT:
                # One holdout run per candidate code, in any epoch and on any evidence.
                # Waiting for new data or a new base never earns a second run; only a
                # human, with a recorded reason, can grant one.
                used = self.ledger.find(brick, kind="trial", split=splits.HOLDOUT, code=ws.hashes["code"])
                if used and not rerun_reason:
                    return {"status": "used", "passed": used[0].tag("passed") == "True", "trial": used[0].run_id}
            if cache:
                hits = self.ledger.find(brick, eval_id=eval_id, kind=kind)
                if hits:
                    envelope = self.ledger.envelope(hits[0].run_id)
                    envelope["cached"] = True
                    return envelope
            envelope = self._execute(brick, ws, policy, chosen, base, agent=kind not in NO_AGENT_KINDS)
            envelope.update({"eval_id": eval_id, "identity": identity, "split": split, "brick": brick,
                             "time": now(), "sandboxed": not self.sandbox.unsandboxed,
                             "synthetic": load_lock(self.repo, base, brick) == "unpinned"})
            entry_tags = {"eval_id": eval_id, "split": split, "epoch": self.epoch(brick, base), "base": base,
                          "passed": envelope["passed"], "metric": policy.metric,
                          "code": ws.hashes["code"], **(tags or {})}
            if rerun_reason:
                entry_tags["rerun_reason"] = rerun_reason
            for overlay in ws.overlays:
                entry_tags[f"{overlay.kind}:{overlay.brick}"] = overlay.commit
            envelope["trial"] = self.ledger.write(brick, kind, entry_tags, envelope["terms"], envelope)
            return envelope
        finally:
            if own:
                ws.cleanup()

    def _execute(self, brick: str, ws: Workspace, policy: Policy, records: list[dict[str, Any]], base: str,
                 agent: bool = True) -> dict[str, Any]:
        counts = {"cases": len(records), "build_errors": 0, "out_of_evidence": 0, "run_errors": 0,
                  "score_errors": 0, "determinism_mismatches": 0, "regenerated": 0, "scored": 0}
        examples: list[dict[str, Any]] = []
        reasons: list[str] = []
        budget = policy.budget
        clock = time.monotonic()
        if "max_cases" in budget and len(records) > budget["max_cases"]:
            # Over budget before anything runs: no time is spent on it.
            return {"passed": False, "terms": {}, "gates": [], "counts": counts, "seconds": 0.0,
                    "reasons": [f"budget: {len(records)} cases; the policy allows {budget['max_cases']} in one split"],
                    "counterexamples": [], "log": ""}
        declared = self._evaluator(ws, {"command": "gates"})
        gates = declared["gates"]
        if not all(isinstance(g, list) and len(g) == 3 and isinstance(g[0], str) and g[1] in OPERATORS
                   and isinstance(g[2], (int, float)) and not isinstance(g[2], bool) for g in gates):
            raise RunnerError("GATES must be a tuple of (term, operator, threshold)")
        declared_agent = declared.get("agent")
        skipped_agent = 0
        if declared_agent and not agent:
            # The defect check: no agent case runs, and the gates only agent cases give are left out.
            gates = [g for g in gates if g[0] not in set(declared_agent["terms"])]
        if not gates:
            reasons.append("evaluate.py declares no GATES")
        built = self._evaluator(ws, {"command": "build", "records": records})["inputs"]
        cases, kept = [], []
        for record, item in zip(records, built):
            if "error" in item:
                counts["build_errors"] += 1
                examples.append({"record": record["id"], "stage": "build", "error": item["error"]})
                continue
            if declared_agent and not agent and isinstance(item["input"], dict) and "task" in item["input"]:
                skipped_agent += 1
                continue
            cases.append({"input": item["input"], "exchanges": record["exchanges"]})
            kept.append(record)
        if skipped_agent:
            counts["agent_cases_skipped"] = skipped_agent
        agents = None
        live = None
        try:
            if policy.live_externals:
                live = Live(self.judge, policy.live_externals, self.home / "live",
                            max_calls=budget.get("max_model_calls"), max_tokens=budget.get("max_model_tokens"))
            if declared_agent and agent:
                agents = Agents(self.judge, declared_agent["model"], max_calls=budget.get("max_agent_calls"),
                                max_tokens=budget.get("max_agent_tokens"))
            if self.kind(base, brick) == "web":
                results, log = self._run_web(brick, ws, cases, agents)
            else:
                results, log = broker.run_cases(self.sandbox, brick, ws.code, ws.deps,
                                                functools.partial(self._deterministic, base), cases, live=live)
        except web.BuildError as exc:
            return {"passed": False, "terms": {}, "gates": [], "counts": counts, "fatal": str(exc),
                    "reasons": [f"the web build failed: {exc}"], "counterexamples": examples,
                    "log": exc.log[-LOG_LIMIT:]}
        except SandboxError as exc:
            return {"passed": False, "terms": {}, "gates": [], "counts": counts, "fatal": str(exc),
                    "reasons": [f"the candidate worker failed: {exc}"], "counterexamples": examples,
                    "log": exc.log[-LOG_LIMIT:]}
        except LiveError as exc:
            if exc.transient:
                raise RunnerError(f"the live model API failed; try again: {exc}") from None
            counts.update(live.usage() if live else {})
            return {"passed": False, "terms": {}, "gates": [], "counts": counts, "fatal": str(exc),
                    "reasons": [f"live model calls stopped: {exc}"], "counterexamples": examples, "log": ""}
        except AgentError as exc:
            if exc.transient:
                # The API failed, not the candidate: no result is recorded, so a retry runs it again.
                raise RunnerError(f"the computer use agent's API failed; try again: {exc}") from None
            counts.update(agents.usage() if agents else {})
            return {"passed": False, "terms": {}, "gates": [], "counts": counts, "fatal": str(exc),
                    "reasons": [f"the computer use agent failed: {exc}"], "counterexamples": examples,
                    "log": ""}
        if agents:
            counts.update(agents.usage())
        if live:
            counts.update(live.usage())
        items, outcomes = [], []
        for record, result in zip(kept, results):
            counts["determinism_mismatches"] += result["mismatches"]
            counts["regenerated"] += result["regenerated"]
            if result.get("out_of_evidence"):
                counts["out_of_evidence"] += 1
                continue
            if "error" in result:
                counts["run_errors"] += 1
                examples.append({"record": record["id"], "stage": "run", "input": record.get("input"),
                                 "error": result["error"]})
            items.append({"record": record, "output": result.get("output"), "calls": result.get("calls", []),
                          "later": self._later(record, policy.horizon)})
            outcomes.append((record, result))
        if declared.get("judged") and items:
            try:
                self._judge(ws, items, counts, examples)
            except JudgeError as exc:
                if exc.transient:
                    raise RunnerError(f"the judge API failed; try again: {exc}") from None
                return {"passed": False, "terms": {}, "gates": [], "counts": counts, "fatal": str(exc),
                        "reasons": [f"the model judge failed: {exc}"], "counterexamples": examples[:50],
                        "log": log[-LOG_LIMIT:]}
        scores = self._evaluator(ws, {"command": "score", "items": items})["scores"]
        per_case: list[tuple[dict[str, Any], dict[str, Any], dict[str, float]]] = []
        for (record, result), score in zip(outcomes, scores):
            if "error" in score:
                counts["score_errors"] += 1
                examples.append({"record": record["id"], "stage": "score", "error": score["error"]})
            else:
                per_case.append((record, result, score["terms"]))
        counts["scored"] = len(per_case)
        names = sorted({name for _, _, terms in per_case for name in terms})
        means = {name: statistics.fmean(t[name] for _, _, t in per_case if name in t) for name in names}
        gate_results = []
        for term, operator, threshold in gates:
            value = means.get(term)
            ok = value is not None and OPERATORS[operator](value, threshold)
            gate_results.append({"term": term, "operator": operator, "threshold": threshold, "value": value, "passed": ok})
            if not ok:
                reasons.append(f"gate {term} {operator} {threshold} failed")
                worst = sorted(per_case, key=lambda c: c[2].get(term, math.inf if operator in (">=", ">") else -math.inf),
                               reverse=operator in ("<=", "<"))[:5]
                for record, result, terms in worst:
                    examples.append({"record": record["id"], "stage": "gate", "term": term, "value": terms.get(term),
                                     "input": record.get("input"), "output": result.get("output")})
        share = counts["out_of_evidence"] / max(1, len(cases))
        if share > policy.max_out_of_evidence:
            reasons.append(f"{share:.0%} of cases are out of evidence; the policy allows {policy.max_out_of_evidence:.0%}")
        if counts["scored"] == 0:
            reasons.append("no case was scored")
        if counts["determinism_mismatches"]:
            reasons.append("a deterministic dependency gave a different result than recorded")
        if counts["score_errors"]:
            reasons.append("score() failed on some cases")
        if counts.get("judge_errors"):
            reasons.append("judge_requests() failed on some cases")
        if policy.metric not in means:
            reasons.append(f"no {policy.metric!r} term to rank by")
        seconds = time.monotonic() - clock
        if "max_seconds" in budget and seconds > budget["max_seconds"]:
            reasons.append(f"budget: the run took {seconds:.0f} s; the policy allows {budget['max_seconds']} s")
        # Each case's metric, so the defect check can tell which cases catch which defects.
        case_metric = {record["id"]: terms.get(policy.metric) for record, _, terms in per_case}
        return {"passed": not reasons, "terms": means, "gates": gate_results, "counts": counts, "seconds": seconds,
                "per_case": case_metric, "reasons": reasons, "counterexamples": examples[:50], "log": log[-LOG_LIMIT:]}

    def _run_web(self, brick: str, ws: Workspace, cases: list[dict[str, Any]],
                 agents: Agents | None = None) -> tuple[list[dict[str, Any]], str]:
        """Build the candidate's app in the sandbox, then let evaluate.observe() look at it in a browser."""
        log = web.build(self.sandbox, ws.code, brick, ws.evaluation / web.BUILD_FOLDER)
        if not cases:
            return [], log
        observed, observe_log = self._observe(ws, [c["input"] for c in cases], agents)
        results = []
        for item in observed:
            result = {"output": item["output"]} if "output" in item else {"error": item.get("error", "no observation")}
            results.append({**result, "mismatches": 0, "regenerated": 0})
        return results, log + observe_log

    def _observe(self, ws: Workspace, inputs: list[Any], agents: Agents | None) -> tuple[list[dict[str, Any]], str]:
        """observe over BRICK_RUNNER_WEB_WORKERS evaluator workers, each with its own browser.

        Case i goes to worker i mod n, so slow cases (scrolling, agent tasks)
        spread over the workers. The observations come back in case order.
        """
        workspace.verify_evaluation(self.repo, ws)
        count = max(1, min(web.workers(), len(inputs)))
        if count == 1:
            return self._observe_one(ws, inputs, agents, "", [])
        chunks = [list(range(i, len(inputs), count)) for i in range(count)]
        started: list[Any] = []   # every worker process, so a failure can stop the others
        results: list[Any] = [None] * count
        errors: list[BaseException] = []

        def work(index: int) -> None:
            if errors:
                return   # another worker already failed
            try:
                results[index] = self._observe_one(ws, [inputs[i] for i in chunks[index]], agents, f"w{index}.", started)
            except BaseException as exc:  # noqa: BLE001 -- passed to the caller below
                errors.append(exc)
                for process in list(started):
                    process.stop()

        threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            first = next((e for e in errors if isinstance(e, AgentError)), errors[0])
            raise first
        observations: list[Any] = [None] * len(inputs)
        logs = []
        for index, (observed, log) in enumerate(results):
            for case, observation in zip(chunks[index], observed):
                observations[case] = observation
            logs.append(log)
        return observations, "".join(logs)

    def _observe_one(self, ws: Workspace, inputs: list[Any], agents: Agents | None, prefix: str,
                     started_list: list[Any]) -> tuple[list[dict[str, Any]], str]:
        """One worker's observe, over a line channel: the worker may ask for agent steps before its result."""
        worker = self.sandbox.start("evaluator.py", ws.evaluation)
        started_list.append(worker)
        started = time.monotonic()
        deadline = started + OBSERVE_TIMEOUT
        try:
            worker.send({"brick": ws.target, "command": "observe", "inputs": inputs,
                         "browser": web.browser_settings(), "agent": agents is not None})
            while True:
                message = worker.receive(deadline)
                if message.get("op") == "result":
                    if "fatal" in message:
                        raise SandboxError(f"evaluator.py failed: {message['fatal']}", worker.log_text())
                    observations = message.get("observations")
                    if not isinstance(observations, list) or len(observations) != len(inputs) \
                            or not all(isinstance(o, dict) for o in observations):
                        raise SandboxError("evaluator.py gave no observation for each case", worker.log_text())
                    return observations, worker.log_text()
                if message.get("op") == "agent":
                    if isinstance(message.get("session"), str):
                        message["session"] = prefix + message["session"]   # names are per worker
                    reply = agents.step(message) if agents else {"error": "this evaluation runs no agent"}
                    if agents:
                        deadline = min(deadline + AGENT_STEP_TIME, started + OBSERVE_TIMEOUT_MAX)
                    worker.send(reply)
                else:
                    worker.send({"error": f"unknown request {message.get('op')!r}"})
        finally:
            worker.stop()

    def _judge(self, ws: Workspace, items: list[dict[str, Any]], counts: dict[str, int],
               examples: list[dict[str, Any]]) -> None:
        """Ask the pinned judge model for each request evaluate.judge_requests() makes."""
        made = self._evaluator(ws, {"command": "judge_requests",
                                    "items": [{"record": i["record"], "output": i["output"]} for i in items]})
        model = made["judge"]["model"]
        counts.setdefault("judge_errors", 0)
        before = self.judge.calls
        total = 0
        for item, requests in zip(items, made["requests"]):
            total += len(requests) if isinstance(requests, list) else 0
            if isinstance(requests, dict) or len(requests) > MAX_REQUESTS_PER_CASE or total > MAX_REQUESTS_PER_EVALUATION:
                counts["judge_errors"] += 1
                error = requests.get("error") if isinstance(requests, dict) else (
                    f"more than {MAX_REQUESTS_PER_CASE} judge requests for one case, "
                    f"or more than {MAX_REQUESTS_PER_EVALUATION} in one evaluation")
                examples.append({"record": item["record"]["id"], "stage": "judge", "error": error})
                item["judgments"] = []
                continue
            item["judgments"] = [self.judge.ask(model, request, ws.hashes["evaluation"]) for request in requests]
        counts["judge_calls"] = self.judge.calls - before

    # ------------------------------------------------------------ what each split may see

    @staticmethod
    def view(envelope: dict[str, Any], split: str) -> dict[str, Any]:
        if envelope.get("status") in ("waiting", "used"):
            return {k: v for k, v in envelope.items() if k in ("status", "have", "need", "passed")}
        if split == splits.DEVELOPMENT:
            return {k: v for k, v in envelope.items() if k != "identity"}
        if split == splits.VALIDATION:
            return {k: envelope.get(k) for k in ("passed", "terms", "gates", "counts", "reasons", "trial",
                                                 "eval_id", "cached", "fatal") if k in envelope}
        return {"passed": envelope.get("passed")}

    # ------------------------------------------------------------ code loop

    def submit(self, brick: str, commit: str, split: str = splits.DEVELOPMENT) -> dict[str, Any]:
        """A dev-agent submission: the candidate's code, the pinned evaluation."""
        if split not in (splits.DEVELOPMENT, splits.VALIDATION):
            raise RunnerError("submissions run on development or validation; the holdout runs only through holdout()")
        commit = self.repo.commit(commit)
        freeze = now()
        with self.exclusive():
            envelope = self.evaluate(brick, split, [Overlay(brick, CODE, commit)], freeze=freeze,
                                     tags={"loop": "code", "candidate": commit, "freeze": freeze})
        return self.view(envelope, split)

    def holdout(self, brick: str, rerun_reason: str | None = None) -> dict[str, Any]:
        """Select the best validation trial of this epoch and run its one holdout.

        ``rerun_reason`` is a human decision to run a candidate's holdout a second
        time (for example after a real change of the base). It is recorded.
        """
        with self.exclusive():
            base = self.base()
            epoch = self.epoch(brick, base)
            policy = load_policy(self.repo, base, brick)
            metric = _metric_name(policy.metric)
            trials = [t for t in self.ledger.find(brick, kind="trial", split="validation", epoch=epoch, loop="code")
                      if t.tag("passed") == "True" and metric in t.metrics]
            if not trials:
                raise RunnerError("no passing validation trial in this epoch")
            best = max(trials, key=lambda t: (t.metrics[metric], -t.start_time))
            candidate = best.tag("candidate")
            envelope = self.evaluate(brick, splits.HOLDOUT, [Overlay(brick, CODE, candidate)], base=base,
                                     freeze=best.tag("freeze"), cache=False, rerun_reason=rerun_reason,
                                     tags={"loop": "code", "candidate": candidate, "freeze": best.tag("freeze"),
                                           "selected_from": best.run_id})
            if envelope.get("status") == "used":
                return {"passed": envelope["passed"], "candidate": candidate,
                        "note": "this candidate already used its holdout run; only a human can grant another"}
            if envelope.get("status") == "waiting":
                return {**envelope, "candidate": candidate}
            claim = {"trial": envelope["trial"], "eval_id": envelope["eval_id"], "passed": envelope["passed"],
                     "candidate": candidate, "epoch": epoch}
            signature = self.sign(claim)
            self.ledger.set_tag(envelope["trial"], "signature", signature)
            holdout_runs = len(self.ledger.find(brick, kind="trial", split="holdout", epoch=epoch))
        return {"passed": envelope["passed"], "candidate": candidate, "signature": signature,
                "holdout_runs_this_epoch": holdout_runs}

    def _verified_holdout(self, brick: str, epoch: str) -> tuple[Entry, dict[str, Any]]:
        """The newest signed, passing holdout of this epoch that counts.

        A holdout counts only if it was its code's first holdout run, or a
        human granted the run with a recorded reason.
        """
        for entry in self.ledger.find(brick, kind="trial", split="holdout", epoch=epoch, passed="True"):
            runs = self.ledger.find(brick, kind="trial", split="holdout", code=entry.tag("code"))
            first = min(runs, key=lambda r: r.start_time).run_id == entry.run_id
            if not (first or entry.tag("rerun_reason")):
                continue
            envelope = self.ledger.envelope(entry.run_id)
            claim = {"trial": entry.run_id, "eval_id": envelope["eval_id"], "passed": True,
                     "candidate": entry.tag("candidate"), "epoch": epoch}
            if hmac.compare_digest(self.sign(claim), entry.tag("signature")):
                return entry, envelope
        raise RunnerError("no signed, passing holdout in this epoch")

    def promote(self, brick: str) -> dict[str, Any]:
        """Promote the candidate that passed this epoch's holdout, as one immutable bundle."""
        base = self.base()
        epoch = self.epoch(brick, base)
        policy = load_policy(self.repo, base, brick)
        holdout, envelope = self._verified_holdout(brick, epoch)
        candidate = holdout.tag("candidate")
        with workspace.build(self.repo, base, [Overlay(brick, CODE, candidate)], brick) as ws:
            current = ws.hashes
        if current != envelope["identity"]["hashes"]:
            changed = sorted(k for k in set(current) | set(envelope["identity"]["hashes"])
                             if current.get(k) != envelope["identity"]["hashes"].get(k))
            raise RunnerError(f"the base changed since the holdout ({', '.join(changed)}); "
                              "a human may grant a new holdout run with a recorded reason")
        if not self.ledger.find(brick, kind="defect-check", eval_tree=epoch, passed="True"):
            report = self.defect_check(brick, impl_commit=candidate, eval_commit=base)
            if not report["passed"]:
                raise RunnerError(f"the evaluation of this epoch cannot detect defects: {report['reason']}")
        checks = {}
        for dependent in dependents(self.repo, base, brick):
            overlays = [Overlay(brick, CODE, candidate)]
            production = self.production_commit(dependent, base)
            if production != base:
                overlays.append(Overlay(dependent, CODE, production))
            with self.exclusive():
                result = self.evaluate(dependent, splits.VALIDATION, overlays, base=base, kind="dependent-check",
                                       tags={"dependency": brick, "candidate": candidate})
            checks[dependent] = {"passed": result.get("passed"), "trial": result.get("trial")}
        failed = [name for name, check in checks.items() if not check["passed"]]
        if failed:
            raise RunnerError(f"promotion stopped: dependents fail with this candidate: {', '.join(failed)}")
        bundle = {"brick": brick, "commit": candidate, "base": base, "epoch": epoch,
                  "hashes": envelope["identity"]["hashes"], "dataset": envelope["identity"]["dataset"],
                  "synthetic": envelope.get("synthetic", False),
                  "split": policy.split, "seeds": policy.seeds, "gates": envelope["gates"],
                  "holdout_trial": holdout.run_id, "holdout_signature": holdout.tag("signature"),
                  "dependents": checks, "time": now()}
        bundle_hash = digest(bundle)
        bundle["signature"] = self.sign({"bundle": bundle_hash})
        folder = self.home / "bundles" / brick
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{bundle_hash[7:]}.json").write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")
        self.ledger.write(brick, "promotion", {"commit": candidate, "bundle": bundle_hash, "epoch": epoch}, {}, bundle)
        return {"bundle": bundle_hash, **bundle}

    # ------------------------------------------------------------ evaluation loop

    def defect_check(self, brick: str, *, impl_commit: str, eval_commit: str, base: str | None = None) -> dict[str, Any]:
        """Does the evaluation detect controlled defects, more as they grow stronger?"""
        base = base or self.base()
        policy = load_policy(self.repo, base, brick)
        overlays = [Overlay(brick, CODE, impl_commit), Overlay(brick, EVALUATION, eval_commit)]
        eval_tree = self.repo.tree(eval_commit, f"bricks/{brick}/evaluation")
        with self.exclusive(), workspace.build(self.repo, base, overlays, brick) as ws:
            baseline = self.evaluate(brick, splits.DEVELOPMENT, overlays, base=base, ws=ws, kind="defect-baseline")
            base_value = baseline["terms"].get(policy.metric)
            report: dict[str, Any] = {"brick": brick, "impl_commit": impl_commit, "eval_commit": eval_commit,
                                      "eval_tree": eval_tree, "metric": policy.metric, "baseline": base_value,
                                      "strengths": {}}
            brick_dir = ws.code / "bricks" / brick
            if base_value is None:
                report.update(passed=False, reason=f"the baseline has no {policy.metric!r} term")
            elif not mutate.points(brick_dir):
                report.update(passed=False, reason="the brick's code has nothing to mutate")
            else:
                tolerance = 1e-9 + 0.01 * abs(base_value)
                raw: dict[int, list[float | None]] = {}
                unbuildable = {strength: 0 for strength in DEFECT_STRENGTHS}
                base_cases = {k: v for k, v in (baseline.get("per_case") or {}).items() if v is not None}
                caught: dict[str, int] = {case: 0 for case in base_cases}   # mutants each case catches
                mutants = 0
                for strength in DEFECT_STRENGTHS:
                    raw[strength] = []
                    for seed in DEFECT_SEEDS:
                        code = Path(tempfile.mkdtemp(prefix="brick-mutant-"))
                        mutant = ws.with_code(code)
                        try:
                            shutil.copytree(ws.code, code, dirs_exist_ok=True)
                            mutate.mutate(code / "bricks" / brick, strength, seed * 1000 + strength)
                            mutant.hashes = workspace.component_hashes(self.repo, mutant)
                            result = self.evaluate(brick, splits.DEVELOPMENT, overlays, base=base, ws=mutant,
                                                   kind="defect-mutant")
                            if any(r.startswith("the web build failed") for r in result.get("reasons", [])):
                                unbuildable[strength] += 1   # neither caught nor missed
                                continue
                            raw[strength].append(result.get("terms", {}).get(policy.metric))
                            seen = result.get("per_case") or {}
                            mutants += 1 if seen else 0
                            # A run with no case scores (a fatal error) catches the mutant as a whole,
                            # but says nothing about which cases would catch it.
                            for case, value in (base_cases.items() if seen else ()):
                                # Lower than in the baseline, or no score at all (it crashed): caught.
                                got = seen.get(case)
                                if got is None or got < value - tolerance:
                                    caught[case] += 1
                        finally:
                            mutant.cleanup()
                # A mutant with no score (it crashed, or every case left the evidence) is
                # detected, but not "more" detected than the worst scored mutant.
                floor = min([v for vs in raw.values() for v in vs if v is not None] + [base_value])
                for strength in DEFECT_STRENGTHS:
                    values = [floor if v is None else v for v in raw[strength]] or [base_value]
                    detected = sum(1 for v in values if v < base_value - tolerance)
                    report["strengths"][str(strength)] = {"median": statistics.median(values), "values": values,
                                                          "unscored": raw[strength].count(None),
                                                          "unbuildable": unbuildable[strength],
                                                          "detected": detected, "of": len(values)}
                medians = [report["strengths"][str(s)]["median"] for s in DEFECT_STRENGTHS]
                monotonic = all(b <= a + tolerance for a, b in zip(medians, medians[1:]))
                strongest = report["strengths"][str(DEFECT_STRENGTHS[-1])]
                below = medians[-1] < base_value - tolerance
                enough = strongest["detected"] * 2 >= strongest["of"]
                # Most mutants must build: otherwise the check measures the build, not the evaluation.
                built = all(len(raw[s]) * 2 >= len(DEFECT_SEEDS) for s in DEFECT_STRENGTHS)
                dead = sorted(case for case, n in caught.items() if n == 0)
                report["cases"] = {"scored": len(base_cases), "mutants": mutants, "dead": len(dead),
                                   "dead_ids": dead[:200], "caught": sum(1 for n in caught.values() if n)}
                report["passed"] = monotonic and below and enough and built
                report["reason"] = ("scores fall as defects grow" if report["passed"] else
                                    "most mutants do not build, so the check cannot measure the evaluation" if not built else
                                    "scores do not fall steadily as defects grow" if not monotonic else
                                    "the strongest defects are not detected")
        self.ledger.write(brick, "defect-check", {"eval_tree": eval_tree or "", "impl": impl_commit,
                                                  "passed": report["passed"]}, {}, report)
        return report

    def _last_defect_check(self, brick: str, base: str) -> dict[str, Any] | None:
        """The last passed defect check of the evaluation on the base commit, if the ledger has one."""
        tree = self.repo.tree(base, f"bricks/{brick}/evaluation")
        found = self.ledger.find(brick, kind="defect-check", eval_tree=tree or "", passed="True")
        return self.ledger.envelope(found[0].run_id) if found else None

    def evaluation_change(self, brick: str, commit: str) -> dict[str, Any]:
        """An eval-agent submission: the production code, the candidate evaluation."""
        base = self.base()
        commit = self.repo.commit(commit)
        impl = self.production_commit(brick, base)
        before = self._last_defect_check(brick, base)
        report = self.defect_check(brick, impl_commit=impl, eval_commit=commit, base=base)
        with self.exclusive():
            old = self.evaluate(brick, splits.DEVELOPMENT, [Overlay(brick, CODE, impl)], base=base, kind="comparison")
            new = self.evaluate(brick, splits.DEVELOPMENT, [Overlay(brick, CODE, impl), Overlay(brick, EVALUATION, commit)],
                                base=base, kind="comparison")
        keys = ("passed", "terms", "gates", "counts", "seconds")
        return {"defect_check": report, "bloat": bloat(before, report, old, new),
                "production_under_old": {k: old.get(k) for k in keys},
                "production_under_new": {k: new.get(k) for k in keys},
                "next": "a human approves the change; when it merges, a new epoch starts"
                        if report["passed"] else "fix the evaluation: it does not detect controlled defects"}
