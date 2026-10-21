"""Unit tests: store, splits, mutations, hashes."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brick_runner import mutate, splits  # noqa: E402
from brick_runner.broker import Broker, CaseState  # noqa: E402
from brick_runner.hashing import digest  # noqa: E402
from brick_runner.store import Store, StoreError  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.store = Store(self.root)

    def test_payloads_are_content_addressed(self) -> None:
        key = self.store.put({"b": 1, "a": [1, 2]})
        self.assertEqual(key, digest({"a": [1, 2], "b": 1}))
        self.assertEqual(self.store.get(key), {"a": [1, 2], "b": 1})

    def test_tampered_payload_is_refused(self) -> None:
        key = self.store.put({"a": 1})
        path = self.store._payload_path(key)
        path.write_text('{"a":2}')
        with self.assertRaises(StoreError):
            self.store.get(key)

    def test_records_dedupe_and_expand(self) -> None:
        record = {"kind": "run", "brick": "x", "trace_id": "t", "time": "2026-01-01", "input": {"q": 1}}
        first = self.store.add_records([record])
        second = Store(self.root).add_records([record])
        self.assertEqual(first, second)
        self.assertEqual(len(list((self.root / "records").glob("*.jsonl"))), 1)
        self.assertEqual(Store(self.root).record(first[0])["input"], {"q": 1})

    def test_datasets_pin_runs_and_their_exchanges(self) -> None:
        runs = self.store.add_records([{"kind": "run", "trace_id": f"t{i}", "time": f"2026-01-0{i + 1}"} for i in range(3)])
        [pinned_exchange] = self.store.add_records([{"kind": "exchange", "trace_id": "t0", "time": "2026-01-01"}])
        key = self.store.create_dataset(reversed(runs))
        self.assertEqual(self.store.dataset(key), sorted(runs + [pinned_exchange]))
        # An exchange added to the same trace later is not part of the pinned dataset.
        [late] = self.store.add_records([{"kind": "exchange", "trace_id": "t0", "time": "2026-02-01"}])
        self.assertNotIn(late, self.store.dataset(key))
        self.assertEqual([e["id"] for e in self.store.exchanges("t0", set(self.store.dataset(key)))], [pinned_exchange])
        with self.assertRaises(StoreError):
            self.store.create_dataset(["sha256:" + "0" * 64])

    def test_the_caller_sets_the_source_and_times_are_normalised(self) -> None:
        [rid] = self.store.add_records([{"kind": "run", "source": "live", "time": "2026-01-01T05:00:00+05:00"}],
                                       source="synthetic")
        record = self.store.record(rid)
        self.assertEqual(record["source"], "synthetic")
        self.assertEqual(record["time"], "2026-01-01T00:00:00.000000+00:00")


class DuplicateCallTests(unittest.TestCase):
    def test_two_identical_calls_in_one_case_are_both_kept(self) -> None:
        store = Store(Path(tempfile.mkdtemp(prefix="brick-dup-")))
        self.addCleanup(shutil.rmtree, store.root, ignore_errors=True)
        call = {"kind": "exchange", "name": "sibling:store", "request": {"op": "get", "key": "u"},
                "request_hash": digest({"op": "get", "key": "u"}), "response": {"ok": True},
                "response_hash": digest({"ok": True}), "trace_id": "t", "run_id": "t", "time": "2026-10-20T15:00:00+00:00"}
        ids = store.add_records([{**call, "seq": 0}, {**call, "seq": 1}], source="synthetic")
        self.assertEqual(len(set(ids)), 2)
        state = CaseState.from_exchanges(store.exchanges("t"))
        self.assertIsNotNone(state.take("sibling:store", {"op": "get", "key": "u"}))
        self.assertIsNotNone(state.take("sibling:store", {"op": "get", "key": "u"}))


class SplitTests(unittest.TestCase):
    def records(self, n: int, source: str = "live") -> list[dict]:
        return [{"id": digest(i), "time": f"2026-01-01T00:00:{i:02d}+00:00", "source": source} for i in range(n)]

    def test_hash_split_is_stable_and_disjoint(self) -> None:
        records = self.records(50)
        first = splits.assign(records, {"kind": "hash", "validation": 0.2, "holdout": 0.2})
        second = splits.assign(list(reversed(records)), {"kind": "hash", "validation": 0.2, "holdout": 0.2})
        for name in splits.SPLITS:
            self.assertEqual({r["id"] for r in first[name]}, {r["id"] for r in second[name]})
        ids = [r["id"] for name in splits.SPLITS for r in first[name]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 50)

    def test_time_split_holds_out_only_data_after_the_freeze(self) -> None:
        records = self.records(40)
        freeze = records[29]["time"]
        parts = splits.assign(records, {"kind": "time", "validation": 0.2}, freeze)
        self.assertEqual(len(parts[splits.HOLDOUT]), 10)
        self.assertTrue(all(r["time"] > freeze for r in parts[splits.HOLDOUT]))
        self.assertTrue(max(r["time"] for r in parts[splits.DEVELOPMENT]) < min(r["time"] for r in parts[splits.VALIDATION]))

    def test_synthetic_records_use_the_hash_rule(self) -> None:
        parts = splits.assign(self.records(30, "synthetic"), {"kind": "time", "validation": 0.2}, "2000-01-01")
        self.assertTrue(parts[splits.HOLDOUT])


class BrokerTests(unittest.TestCase):
    """The parent-side replay rules. Dependency workers are replaced by a stand-in."""

    class FakeBroker(Broker):
        def __init__(self, sibling_output: dict) -> None:
            super().__init__(sandbox=None, dependencies=Path("."), deterministic=lambda b: b == "det",
                             deadline=0)
            self.sibling_output = sibling_output

        def dependency(self, brick):
            return "dependency-worker"

        def run_case(self, worker, payload, state):
            assert worker == "dependency-worker"
            return {"output": self.sibling_output}

    def state(self, *exchanges: dict) -> CaseState:
        return CaseState.from_exchanges([{**e, "request_hash": digest(e["request"])} for e in exchanges])

    def test_external_replays_then_runs_out(self) -> None:
        broker = self.FakeBroker({})
        state = self.state({"name": "external:p", "request": {"s": 1}, "response": {"v": 2}})
        self.assertEqual(broker._external(state, {"name": "p", "request": {"s": 1}}), {"response": {"v": 2}})
        self.assertFalse(state.out_of_evidence)
        # One recording answers one call; a second identical call is out of evidence.
        self.assertEqual(broker._external(state, {"name": "p", "request": {"s": 1}})["raise"], "out_of_evidence")
        self.assertTrue(state.out_of_evidence)

    def test_deterministic_sibling_is_recomputed_and_checked(self) -> None:
        broker = self.FakeBroker({"a": 2})
        state = self.state({"name": "sibling:det", "request": {"q": 1}, "response_hash": digest({"a": 1})})
        self.assertEqual(broker._sibling(state, {"brick": "det", "request": {"q": 1}}), {"response": {"a": 2}})
        self.assertEqual(state.mismatches, 1)

    def test_nondeterministic_sibling_replays_or_regenerates(self) -> None:
        broker = self.FakeBroker({"p": 0.9})
        state = self.state({"name": "sibling:model", "request": {"q": 1}, "response": {"p": 0.3}})
        self.assertEqual(broker._sibling(state, {"brick": "model", "request": {"q": 1}}), {"response": {"p": 0.3}})
        self.assertEqual(broker._sibling(state, {"brick": "model", "request": {"q": 2}}), {"response": {"p": 0.9}})
        self.assertEqual(state.regenerated, 1)


class MutateTests(unittest.TestCase):
    def test_seeded_and_bounded(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        source = '"""Doc."""\ndef f(a, b):\n    if a < b:\n        return {"k": a + 1}\n    return b * 2\n'
        for name in ("one", "two"):
            (root / name / "src").mkdir(parents=True)
            (root / name / "src/logic.py").write_text(source)
        # compare, add, 1, "k", mult, 2 -- and never the docstring
        self.assertEqual(len(mutate.points(root / "one")), 6)
        mutate.mutate(root / "one", 2, seed=7)
        mutate.mutate(root / "two", 2, seed=7)
        self.assertEqual((root / "one/src/logic.py").read_text(), (root / "two/src/logic.py").read_text())
        self.assertNotEqual((root / "one/src/logic.py").read_text(), source)
        self.assertIn("Doc.", (root / "one/src/logic.py").read_text())


if __name__ == "__main__":
    unittest.main()


class SandboxTmpTests(unittest.TestCase):
    def test_tmp_is_private_and_writable(self) -> None:
        from brick_runner.sandbox import Sandbox, isolation
        if not isolation():
            self.skipTest("this host cannot create namespaces")
        planted = Path("/tmp") / "brick-runner-host-secret"
        planted.write_text("host")
        work = Path(tempfile.mkdtemp(prefix="brick-tmp-"))
        try:
            code, log = Sandbox([]).run_command(
                ["sh", "-c", "ls -A /tmp; echo ok > /tmp/mine && cat /tmp/mine"], work, timeout=60)
            self.assertEqual(code, 0, log)
            self.assertNotIn(planted.name, log)
            self.assertIn("ok", log)
        finally:
            planted.unlink()
            shutil.rmtree(work, ignore_errors=True)
