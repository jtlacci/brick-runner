"""The evaluation engine: trials, holdout, promotion and the evaluation loop."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import hmac
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import statistics
import tempfile
from typing import Any, Iterator

from . import mutate, sandbox, splits, workspace
from .gitrepo import Repo
from .hashing import canonical, digest
from .ledger import Entry, Ledger, _metric_name
from .policy import CODE, EVALUATION, Policy, dependents, load_lock, load_policy
from .store import Store
from .workspace import Overlay, Workspace

SYNTHETIC_TIME = "1970-01-01T00:00:00+00:00"
DEFECT_STRENGTHS = (1, 2, 4)
DEFECT_SEEDS = (0, 1, 2, 3)
OPERATORS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, "<": lambda a, b: a < b}


class RunnerError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Runner:
    def __init__(self, repo: Path, home: Path, base_ref: str = "main") -> None:
        self.repo = Repo(Path(repo).resolve())
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.home / "store")
        self.ledger = Ledger(self.home)
        self.base_ref = base_ref
        self.key = self._key()

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

    def production(self, brick: str) -> Entry | None:
        found = self.ledger.find(brick, kind="promotion")
        return found[0] if found else None

    def production_commit(self, brick: str, base: str) -> str:
        entry = self.production(brick)
        return entry.tag("commit") if entry else base

    # ------------------------------------------------------------ data

    def dataset(self, brick: str, base: str, ws: Workspace, policy: Policy) -> tuple[str, list[dict[str, Any]]]:
        lock = load_lock(self.repo, base, brick)
        if lock == "unpinned":
            return self.synthetic(brick, ws, policy)
        ids = self.store.dataset(lock)
        records = [self.store.record(i) for i in ids]
        return lock, [r for r in records if r.get("kind") == "run" and r.get("brick") == brick]

    def synthetic(self, brick: str, ws: Workspace, policy: Policy) -> tuple[str, list[dict[str, Any]]]:
        """Records from evaluate.generate(seed), for a brick with no real data yet."""
        result, _ = sandbox.run_worker("evaluator.py", ws.path,
                                       {"command": "generate", "brick": brick, "seeds": policy.seeds})
        records: list[dict[str, Any]] = []
        for seed, items in sorted(result["generated"].items(), key=lambda kv: int(kv[0])):
            for index, item in enumerate(items):
                trace = digest({"brick": brick, "generator": ws.hashes["evaluation"],
                                "seed": int(seed), "index": index})[7:39]
                time = item.get("time", SYNTHETIC_TIME)
                records.append({"kind": "run", "brick": brick, "trace_id": trace, "run_id": trace,
                                "parent_id": None, "input": item["input"], "input_hash": digest(item["input"]),
                                "time": time, "seed": int(seed), "index": index})
                for exchange in item.get("exchanges", []):
                    name = exchange["name"] if ":" in exchange["name"] else f"external:{exchange['name']}"
                    records.append({"kind": "exchange", "name": name, "request": exchange["request"],
                                    "request_hash": digest(exchange["request"]), "response": exchange["response"],
                                    "response_hash": digest(exchange["response"]), "trace_id": trace,
                                    "run_id": trace, "time": time})
        ids = self.store.add_records(records, source="synthetic")
        run_ids = [record_id for record_id, record in zip(ids, records) if record["kind"] == "run"]
        if not run_ids:
            raise RunnerError(f"{brick}: data.lock is unpinned and generate() returned no records")
        return self.store.create_dataset(run_ids), [self.store.record(i) for i in run_ids]

    def _later(self, record: dict[str, Any], horizon: float) -> list[dict[str, Any]]:
        if horizon <= 0 or record.get("source") == "synthetic":
            return []
        start = record["time"]
        end = (datetime.fromisoformat(start) + timedelta(seconds=horizon)).isoformat(timespec="microseconds")
        return self.store.between(start, end)

    # ------------------------------------------------------------ one evaluation

    def evaluate(self, brick: str, split: str, overlays: list[Overlay], *, base: str | None = None,
                 freeze: str | None = None, kind: str = "trial", tags: dict[str, Any] | None = None,
                 cache: bool = True, ws: Workspace | None = None) -> dict[str, Any]:
        base = base or self.base()
        policy = load_policy(self.repo, base, brick)
        own = ws is None
        ws = ws or workspace.build(self.repo, base, overlays, brick)
        try:
            dataset_key, records = self.dataset(brick, base, ws, policy)
            parts = splits.assign(records, policy.split, freeze)
            chosen = parts[split]
            if split == splits.HOLDOUT and policy.split["kind"] == "time":
                chosen += [self.store.expand(r) for r in self.store.query(kind="run", brick=brick, source="live")
                           if freeze and r["time"] > freeze and r["id"] not in {c["id"] for c in chosen}]
            if split == splits.HOLDOUT and len(chosen) < policy.split["min_holdout"]:
                return {"status": "waiting", "have": len(chosen), "need": policy.split["min_holdout"]}
            for record in chosen:
                record["exchanges"] = self.store.exchanges(record["trace_id"])
            identity = {"brick": brick, "split": split, "hashes": ws.hashes, "dataset": dataset_key,
                        "records": digest(sorted(r["id"] for r in chosen)), "seeds": policy.seeds,
                        "policy": self.repo.tree(base, f"bricks/{brick}/evaluation/policy.yml")}
            eval_id = digest(identity)
            if cache:
                hits = self.ledger.find(brick, eval_id=eval_id, kind=kind)
                if hits:
                    envelope = self.ledger.envelope(hits[0].run_id)
                    envelope["cached"] = True
                    return envelope
            envelope = self._execute(brick, ws, policy, chosen)
            envelope.update({"eval_id": eval_id, "identity": identity, "split": split, "brick": brick,
                             "time": now(), "network_isolated": sandbox.network_isolation()})
            entry_tags = {"eval_id": eval_id, "split": split, "epoch": self.epoch(brick, base), "base": base,
                          "passed": envelope["passed"], "metric": policy.metric, **(tags or {})}
            for overlay in ws.overlays:
                entry_tags[f"{overlay.kind}:{overlay.brick}"] = overlay.commit
            envelope["trial"] = self.ledger.write(brick, kind, entry_tags, envelope["terms"], envelope)
            return envelope
        finally:
            if own:
                ws.cleanup()

    def _execute(self, brick: str, ws: Workspace, policy: Policy, records: list[dict[str, Any]]) -> dict[str, Any]:
        counts = {"cases": len(records), "build_errors": 0, "out_of_evidence": 0, "run_errors": 0,
                  "score_errors": 0, "determinism_mismatches": 0, "regenerated": 0, "scored": 0}
        examples: list[dict[str, Any]] = []
        gates_result, _ = sandbox.run_worker("evaluator.py", ws.path, {"command": "gates", "brick": brick})
        gates = gates_result["gates"]
        built, _ = sandbox.run_worker("evaluator.py", ws.path, {"command": "build", "brick": brick, "records": records})
        cases, kept = [], []
        for record, item in zip(records, built["inputs"]):
            if "error" in item:
                counts["build_errors"] += 1
                examples.append({"record": record["id"], "stage": "build", "error": item["error"]})
                continue
            cases.append({"input": item["input"], "exchanges": record["exchanges"]})
            kept.append(record)
        try:
            ran, log = sandbox.run_worker("candidate.py", ws.path, {"brick": brick, "cases": cases})
        except sandbox.SandboxError as exc:
            return {"passed": False, "terms": {}, "gates": [], "counts": counts, "fatal": str(exc),
                    "counterexamples": examples, "log": exc.log}
        items, outcomes, regenerated = [], [], []
        for record, result in zip(kept, ran["results"]):
            counts["determinism_mismatches"] += len(result["mismatches"])
            for new in result["regenerated"]:
                regenerated.append({"kind": "exchange", **new, "request_hash": digest(new["request"]),
                                    "response_hash": digest(new["response"]), "trace_id": record["trace_id"],
                                    "run_id": record.get("run_id"), "time": record["time"]})
            if "out_of_evidence" in result:
                counts["out_of_evidence"] += 1
                continue
            if "error" in result:
                counts["run_errors"] += 1
                examples.append({"record": record["id"], "stage": "run", "input": record.get("input"), "error": result["error"]})
            items.append({"record": record, "output": result.get("output"), "later": self._later(record, policy.horizon)})
            outcomes.append((record, result))
        if regenerated:
            self.store.add_records(regenerated, source="regenerated")
            counts["regenerated"] = len(regenerated)
        scored, _ = sandbox.run_worker("evaluator.py", ws.path, {"command": "score", "brick": brick, "items": items})
        per_case: list[tuple[dict[str, Any], dict[str, Any], dict[str, float]]] = []
        for (record, result), score in zip(outcomes, scored["scores"]):
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
            if not ok and value is not None:
                worst = sorted(per_case, key=lambda c: c[2].get(term, math.inf if operator in (">=", ">") else -math.inf),
                               reverse=operator in ("<=", "<"))[:5]
                for record, result, terms in worst:
                    examples.append({"record": record["id"], "stage": "gate", "term": term, "value": terms.get(term),
                                     "input": record.get("input"), "output": result.get("output")})
        passed = (counts["scored"] > 0 and all(g["passed"] for g in gate_results)
                  and counts["determinism_mismatches"] == 0 and counts["score_errors"] == 0 and policy.metric in means)
        return {"passed": passed, "terms": means, "gates": gate_results, "counts": counts,
                "counterexamples": examples[:50], "log": log}

    # ------------------------------------------------------------ what each split may see

    @staticmethod
    def view(envelope: dict[str, Any], split: str) -> dict[str, Any]:
        if envelope.get("status") == "waiting":
            return envelope
        if split == splits.DEVELOPMENT:
            return {k: v for k, v in envelope.items() if k != "identity"}
        if split == splits.VALIDATION:
            return {k: envelope.get(k) for k in ("passed", "terms", "gates", "counts", "trial", "eval_id", "cached", "fatal")
                    if k in envelope}
        return {"passed": envelope.get("passed")}

    # ------------------------------------------------------------ code loop

    def submit(self, brick: str, commit: str, split: str = splits.DEVELOPMENT) -> dict[str, Any]:
        """A dev-agent submission: the candidate's code, the pinned evaluation."""
        if split not in (splits.DEVELOPMENT, splits.VALIDATION):
            raise RunnerError("submissions run on development or validation; the holdout runs only through holdout()")
        commit = self.repo.commit(commit)
        with self.exclusive():
            envelope = self.evaluate(brick, split, [Overlay(brick, CODE, commit)],
                                     tags={"loop": "code", "candidate": commit, "freeze": now()})
        return self.view(envelope, split)

    def holdout(self, brick: str) -> dict[str, Any]:
        """Select the best validation trial of this epoch and run its one holdout."""
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
        used = self.ledger.find(brick, kind="trial", split="holdout", epoch=epoch, candidate=candidate)
        if used:
            return {"passed": used[0].tag("passed") == "True", "candidate": candidate,
                    "note": "this candidate already used its holdout run"}
        with self.exclusive():
            envelope = self.evaluate(brick, splits.HOLDOUT, [Overlay(brick, CODE, candidate)], base=base,
                                     freeze=best.tag("freeze"), cache=False,
                                     tags={"loop": "code", "candidate": candidate, "freeze": best.tag("freeze"),
                                           "selected_from": best.run_id})
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
        for entry in self.ledger.find(brick, kind="trial", split="holdout", epoch=epoch, passed="True"):
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
            brick_dir = ws.path / "bricks" / brick
            if base_value is None:
                report.update(passed=False, reason=f"the baseline has no {policy.metric!r} term")
            elif not mutate.points(brick_dir):
                report.update(passed=False, reason="the brick's code has nothing to mutate")
            else:
                tolerance = 1e-9 + 0.01 * abs(base_value)
                raw: dict[int, list[float | None]] = {}
                for strength in DEFECT_STRENGTHS:
                    raw[strength] = []
                    for seed in DEFECT_SEEDS:
                        mutant = Workspace(Path(tempfile.mkdtemp(prefix="brick-mutant-")), base, overlays)
                        try:
                            shutil.copytree(ws.path, mutant.path, dirs_exist_ok=True)
                            mutate.mutate(mutant.path / "bricks" / brick, strength, seed * 1000 + strength)
                            mutant.hashes = workspace.component_hashes(self.repo, base, mutant, brick)
                            result = self.evaluate(brick, splits.DEVELOPMENT, overlays, base=base, ws=mutant,
                                                   kind="defect-mutant")
                            raw[strength].append(result.get("terms", {}).get(policy.metric))
                        finally:
                            mutant.cleanup()
                # A mutant with no score (it crashed, or every case left the evidence) is
                # detected, but not "more" detected than the worst scored mutant.
                floor = min([v for vs in raw.values() for v in vs if v is not None] + [base_value])
                for strength in DEFECT_STRENGTHS:
                    values = [floor if v is None else v for v in raw[strength]]
                    detected = sum(1 for v in values if v < base_value - tolerance)
                    report["strengths"][str(strength)] = {"median": statistics.median(values), "values": values,
                                                          "unscored": raw[strength].count(None),
                                                          "detected": detected, "of": len(values)}
                medians = [report["strengths"][str(s)]["median"] for s in DEFECT_STRENGTHS]
                monotonic = all(b <= a + tolerance for a, b in zip(medians, medians[1:]))
                strongest = report["strengths"][str(DEFECT_STRENGTHS[-1])]
                below = medians[-1] < base_value - tolerance
                enough = strongest["detected"] * 2 >= strongest["of"]
                report["passed"] = monotonic and below and enough
                report["reason"] = ("scores fall as defects grow" if report["passed"] else
                                    "scores do not fall steadily as defects grow" if not monotonic else
                                    "the strongest defects are not detected")
        self.ledger.write(brick, "defect-check", {"eval_tree": eval_tree or "", "impl": impl_commit,
                                                  "passed": report["passed"]}, {}, report)
        return report

    def evaluation_change(self, brick: str, commit: str) -> dict[str, Any]:
        """An eval-agent submission: the production code, the candidate evaluation."""
        base = self.base()
        commit = self.repo.commit(commit)
        impl = self.production_commit(brick, base)
        report = self.defect_check(brick, impl_commit=impl, eval_commit=commit, base=base)
        with self.exclusive():
            old = self.evaluate(brick, splits.DEVELOPMENT, [Overlay(brick, CODE, impl)], base=base, kind="comparison")
            new = self.evaluate(brick, splits.DEVELOPMENT, [Overlay(brick, CODE, impl), Overlay(brick, EVALUATION, commit)],
                                base=base, kind="comparison")
        return {"defect_check": report,
                "production_under_old": {k: old.get(k) for k in ("passed", "terms", "gates", "counts")},
                "production_under_new": {k: new.get(k) for k in ("passed", "terms", "gates", "counts")},
                "next": "a human approves the change; when it merges, a new epoch starts"
                        if report["passed"] else "fix the evaluation: it does not detect controlled defects"}
