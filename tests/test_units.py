"""Unit tests: store, splits, mutations, hashes."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brick_runner import mutate, splits  # noqa: E402
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

    def test_datasets_pin_record_ids(self) -> None:
        ids = self.store.add_records([{"kind": "run", "n": i, "time": str(i)} for i in range(3)])
        key = self.store.create_dataset(reversed(ids))
        self.assertEqual(self.store.dataset(key), sorted(ids))
        with self.assertRaises(StoreError):
            self.store.create_dataset(["sha256:" + "0" * 64])


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


class ReplayTests(unittest.TestCase):
    """The candidate worker's replay rules, with a stand-in runtime."""

    def setUp(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "brick_runner/workers"))
        import candidate

        class Runtime:
            class OutOfEvidence(LookupError):
                pass

            digest = staticmethod(digest)

            @staticmethod
            def is_deterministic(brick: str) -> bool:
                return brick == "det"

        self.runtime = Runtime
        self.Replay = candidate.Replay

    def replay(self, *exchanges: dict):
        return self.Replay([{**e, "request_hash": digest(e["request"])} for e in exchanges], self.runtime)

    def test_external_replays_or_is_out_of_evidence(self) -> None:
        replay = self.replay({"name": "external:p", "request": {"s": 1}, "response": {"v": 2}})
        self.assertEqual(replay.external("p", {"s": 1}, None), {"v": 2})
        with self.assertRaises(self.runtime.OutOfEvidence):
            replay.external("p", {"s": 9}, None)

    def test_deterministic_sibling_is_recomputed_and_checked(self) -> None:
        replay = self.replay({"name": "sibling:det", "request": {"q": 1}, "response_hash": digest({"a": 1})})
        self.assertEqual(replay.sibling("det", {"q": 1}, lambda r: {"a": 2}), {"a": 2})
        self.assertEqual(len(replay.mismatches), 1)

    def test_nondeterministic_sibling_replays_or_regenerates(self) -> None:
        replay = self.replay({"name": "sibling:model", "request": {"q": 1}, "response": {"p": 0.3}})
        self.assertEqual(replay.sibling("model", {"q": 1}, lambda r: {"p": 0.9}), {"p": 0.3})
        self.assertEqual(replay.sibling("model", {"q": 2}, lambda r: {"p": 0.9}), {"p": 0.9})
        self.assertEqual(replay.regenerated, [{"name": "sibling:model", "request": {"q": 2}, "response": {"p": 0.9}}])


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
