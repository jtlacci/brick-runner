"""End to end: both loops, holdout, promotion, capture and replay, on a real repository."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture  # noqa: E402
from brick_runner import sandbox  # noqa: E402
from brick_runner.engine import Runner, RunnerError  # noqa: E402
from brick_runner.store import ingest_spool  # noqa: E402


class Scenario(unittest.TestCase):
    """One repository and one runner home, shared by the ordered steps below."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="brick-e2e-"))
        cls.root = fixture.make_repo(cls.tmp / "repo")
        cls.runner = Runner(cls.root, cls.tmp / "home")
        fixture.git(cls.root, "checkout", "-q", "-b", "dev")
        fixture.write(cls.root, "bricks/signal/src/logic.py", fixture.SIGNAL_CORRECT)
        cls.correct = fixture.commit(cls.root, "implement signal")
        fixture.write(cls.root, "bricks/signal/src/logic.py", fixture.SIGNAL_BUGGY)
        cls.buggy = fixture.commit(cls.root, "a bug")
        fixture.git(cls.root, "checkout", "-q", "main")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def branch(self, name: str, files: dict[str, str], start: str = "main") -> str:
        fixture.git(self.root, "checkout", "-q", "-B", name, start)
        for relative, text in files.items():
            fixture.write(self.root, relative, text)
        commit = fixture.commit(self.root, name)
        fixture.git(self.root, "checkout", "-q", "main")
        return commit

    def test_01_fixture_lints_clean(self) -> None:
        result = fixture.lint(self.root)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_02_code_loop_development_shows_detail(self) -> None:
        view = self.runner.submit("signal", self.correct, "development")
        self.assertTrue(view["passed"], view)
        self.assertGreater(view["counts"]["scored"], 0)
        self.assertIn("counterexamples", view)
        self.assertEqual(view["counts"]["determinism_mismatches"], 0)

    def test_03_validation_shows_scores_only(self) -> None:
        view = self.runner.submit("signal", self.correct, "validation")
        self.assertTrue(view["passed"])
        self.assertNotIn("counterexamples", view)
        self.assertNotIn("log", view)

    def test_04_buggy_candidate_fails_with_counterexamples(self) -> None:
        view = self.runner.submit("signal", self.buggy, "development")
        self.assertFalse(view["passed"])
        self.assertTrue(any(c["stage"] == "gate" for c in view["counterexamples"]))
        self.assertFalse(self.runner.submit("signal", self.buggy, "validation")["passed"])

    def test_05_cache_by_evaluation_id(self) -> None:
        view = self.runner.submit("signal", self.correct, "development")
        self.assertTrue(view.get("cached"))

    def test_06_holdout_runs_once_for_the_best_candidate(self) -> None:
        result = self.runner.holdout("signal")
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["candidate"], self.correct)
        self.assertEqual(set(result) - {"passed", "candidate", "signature", "holdout_runs_this_epoch"}, set())
        again = self.runner.holdout("signal")
        self.assertIn("already used", again["note"])

    def test_07_promotion_checks_defects_and_writes_a_bundle(self) -> None:
        bundle = self.runner.promote("signal")
        self.assertEqual(bundle["commit"], self.correct)
        self.assertTrue((self.tmp / "home/bundles/signal" / f"{bundle['bundle'][7:]}.json").exists())
        [check] = self.runner.ledger.find("signal", kind="defect-check")
        report = self.runner.ledger.envelope(check.run_id)
        self.assertTrue(report["passed"], report)
        self.assertLess(report["strengths"]["4"]["median"], report["baseline"])

    def test_08_evaluation_loop_rejects_an_evaluation_that_cannot_fail(self) -> None:
        path = self.root / "bricks/signal/evaluation/evaluate.py"
        text = path.read_text()
        start = text.index("def score(")
        end = text.index("def generate(")
        weak = text[:start] + fixture.SCORE_ALWAYS_PASSES.strip() + "\n\n\n" + text[end:]
        commit = self.branch("weak-eval", {"bricks/signal/evaluation/evaluate.py": weak})
        result = self.runner.evaluation_change("signal", commit)
        self.assertFalse(result["defect_check"]["passed"])
        self.assertIn("fix the evaluation", result["next"])

    def test_09_sandbox_has_no_network(self) -> None:
        commit = self.branch("network", {"bricks/signal/src/logic.py": fixture.SIGNAL_NETWORK})
        view = self.runner.submit("signal", commit, "development")
        self.assertFalse(view["passed"])
        self.assertEqual(view["counts"]["run_errors"], view["counts"]["cases"])
        self.assertTrue(view["sandboxed"])

    # ---- attacks a candidate might try (from the independent review)

    def test_09a_candidate_cannot_rewrite_its_scorer(self) -> None:
        targets = [str(self.tmp), "/tmp", "/mnt", str(self.root)]
        tamper = (
            "import os\n\n"
            f"for top in {targets!r}:\n"
            "    for dirpath, dirs, files in os.walk(top):\n"
            "        if 'evaluate.py' in files:\n"
            "            try:\n"
            "                with open(os.path.join(dirpath, 'evaluate.py'), 'w') as f:\n"
            "                    f.write(\"GATES = ()\\ndef build(r): return r['input']\\n"
            "def score(r, o, l): return {'accuracy': 1.0}\\ndef generate(s): return []\\n\")\n"
            "            except OSError:\n"
            "                pass\n\n\n"
            "def execute(inputs, ctx):\n"
            "    return {'action': 'hold'}\n"
        )
        commit = self.branch("tamper", {"bricks/signal/src/logic.py": tamper})
        view = self.runner.submit("signal", commit, "development")
        self.assertFalse(view["passed"], view)
        self.assertLess(view["terms"]["accuracy"], 0.95)
        self.assertIn("score(", (self.root / "bricks/signal/evaluation/evaluate.py").read_text())

    def test_09b_candidate_cannot_read_the_runner_state(self) -> None:
        key = (self.tmp / "home/runner.key").read_bytes().hex()
        spy = (
            "import os, sys\n\n"
            f"for path in [{str(self.tmp / 'home/runner.key')!r}, {str(self.tmp / 'home/mlflow.db')!r}]:\n"
            "    try:\n"
            "        print('LEAK', open(path, 'rb').read().hex(), file=sys.stderr)\n"
            "    except OSError as exc:\n"
            "        print('BLOCKED', type(exc).__name__, file=sys.stderr)\n\n\n"
            "def execute(inputs, ctx):\n"
            "    return {'action': 'hold'}\n"
        )
        commit = self.branch("spy", {"bricks/signal/src/logic.py": spy})
        view = self.runner.submit("signal", commit, "development")
        self.assertNotIn(key, view["log"])
        self.assertNotIn("LEAK", view["log"])
        self.assertIn("BLOCKED", view["log"])

    def test_09e_candidate_cannot_change_the_host(self) -> None:
        engine = Path(sys.modules["brick_runner.engine"].__file__)
        marker = self.tmp / "planted"
        before = engine.read_bytes()
        writer = (
            "import sys\n\n"
            f"for path in [{str(engine)!r}, {str(Path.home() / '.gitconfig-planted')!r}, {str(marker)!r}]:\n"
            "    try:\n"
            "        with open(path, 'a') as handle:\n"
            "            handle.write('# planted')\n"
            "        print('WROTE', path, file=sys.stderr)\n"
            "    except OSError as exc:\n"
            "        print('BLOCKED', type(exc).__name__, file=sys.stderr)\n\n\n"
            "def execute(inputs, ctx):\n"
            "    return {'action': 'hold'}\n"
        )
        commit = self.branch("writer", {"bricks/signal/src/logic.py": writer})
        view = self.runner.submit("signal", commit, "development")
        self.assertNotIn("WROTE", view["log"])
        self.assertEqual(engine.read_bytes(), before)
        self.assertFalse((Path.home() / ".gitconfig-planted").exists())
        self.assertFalse(marker.exists())

    def test_09f_candidate_cannot_read_deployment_secrets(self) -> None:
        secrets = self.tmp / "etc-brick-runner"
        secrets.mkdir()
        (secrets / "env").write_text("BRICK_RUNNER_GITHUB_TOKEN=ghs_must_not_leak_0123456789\n")
        reader = (
            "import sys\n\n"
            "try:\n"
            f"    print('LEAK', open({str(secrets / 'env')!r}).read(), file=sys.stderr)\n"
            "except OSError as exc:\n"
            "    print('BLOCKED', type(exc).__name__, file=sys.stderr)\n\n\n"
            "def execute(inputs, ctx):\n"
            "    return {'action': 'hold'}\n"
        )
        commit = self.branch("secret-reader", {"bricks/signal/src/logic.py": reader})
        with mock.patch.dict(os.environ, {"BRICK_RUNNER_HIDE": str(secrets)}):
            runner = Runner(self.root, self.tmp / "home")
        view = runner.submit("signal", commit, "development")
        self.assertNotIn("ghs_must_not_leak", view["log"])
        self.assertIn("BLOCKED", view["log"])

    def test_09c_candidate_cannot_declare_its_own_cases_out_of_evidence(self) -> None:
        fake = fixture.SIGNAL_CORRECT.replace(
            "from ..input.adapters import market_data",
            "from bricks import _runtime\n    from ..input.adapters import market_data").replace(
            'return {"action": "buy"}', 'raise _runtime.OutOfEvidence("skip the hard case")')
        commit = self.branch("fake-ooe", {"bricks/signal/src/logic.py": fake})
        view = self.runner.submit("signal", commit, "development")
        self.assertEqual(view["counts"]["out_of_evidence"], 0)
        self.assertGreater(view["counts"]["run_errors"], 0)
        self.assertFalse(view["passed"])

    def test_09d_skipping_hard_cases_through_unrecorded_requests_fails(self) -> None:
        skip = fixture.SIGNAL_CORRECT.replace(
            '        if price < inputs["threshold"]:\n            return {"action": "buy"}',
            '        if price < inputs["threshold"]:\n'
            '            market_data.latest_price(inputs["symbol"] + "-NEVER", ctx)\n'
            '            return {"action": "buy"}')
        self.assertNotEqual(skip, fixture.SIGNAL_CORRECT)
        commit = self.branch("skip-hard", {"bricks/signal/src/logic.py": skip})
        view = self.runner.submit("signal", commit, "development")
        self.assertGreater(view["counts"]["out_of_evidence"], 0)
        self.assertFalse(view["passed"])
        self.assertTrue(any("out of evidence" in r for r in view["reasons"]))

    def test_10_capture_ingest_pin_and_replay_real_data(self) -> None:
        spool = self.tmp / "spool"
        env = {**os.environ, "BRICK_CAPTURE_DIR": str(spool),
               "FAKE_PRICES": json.dumps({"BTC": 50.0, "ETH": 20.0, "SOL": 80.0})}
        fixture.git(self.root, "checkout", "-q", self.correct)
        script = ("from bricks.signal import run\n"
                  "for i in range(150):\n"
                  "    run({'symbol': ['BTC', 'ETH', 'SOL'][i % 3], 'threshold': float(i % 100)})\n")
        subprocess.run([sys.executable, "-c", script], cwd=self.root, env=env, check=True)
        fixture.git(self.root, "checkout", "-q", "main")
        self.assertGreater(ingest_spool(self.runner.store, spool), 0)
        runs = [r["id"] for r in self.runner.store.query(kind="run", brick="signal", source="live")]
        self.assertEqual(len(runs), 150)
        nested = list(self.runner.store.query(kind="run", brick="market_data", source="live"))
        self.assertEqual(len(nested), 150)
        self.assertTrue(all("output_hash" in r and "output_ref" in r for r in nested))  # nondeterministic: kept
        signal_run = self.runner.store.record(runs[0])
        self.assertNotIn("output", signal_run)  # deterministic: hash only
        # A human pins the dataset on main.
        key = self.runner.store.create_dataset(runs)
        fixture.write(self.root, "bricks/signal/evaluation/data.lock", key + "\n")
        fixture.commit(self.root, "pin signal data")
        view = self.runner.submit("signal", self.correct, "development")
        self.assertTrue(view["passed"], view)
        self.assertEqual(view["counts"]["out_of_evidence"], 0)
        # A candidate that asks for a request never recorded is out of evidence, not failed.
        other = self.branch("other-symbol", {"bricks/signal/src/logic.py": fixture.SIGNAL_OTHER_SYMBOL})
        view = self.runner.submit("signal", other, "development")
        self.assertEqual(view["counts"]["regenerated"], 0)
        self.assertEqual(view["counts"]["out_of_evidence"], view["counts"]["cases"])
        self.assertFalse(view["passed"])
        # Nothing a candidate run produces is ever stored as evidence.
        self.assertEqual(list(self.runner.store.query(source="regenerated")), [])

    def test_11_holdout_is_refused_without_validation(self) -> None:
        with self.assertRaises(RunnerError):
            self.runner.holdout("market_data")

    def test_12_promoting_a_dependency_re_evaluates_its_dependents(self) -> None:
        refactor = fixture.MARKET_DATA["src/logic.py"].replace(
            "raw = prices.fetch", "symbol = inputs['symbol']\n            raw = prices.fetch").replace(
            'prices.fetch(inputs["symbol"], ctx)', "prices.fetch(symbol, ctx)")
        commit = self.branch("market-refactor", {"bricks/market_data/src/logic.py": refactor})
        self.assertTrue(self.runner.submit("market_data", commit, "validation")["passed"])
        self.assertTrue(self.runner.holdout("market_data")["passed"])
        bundle = self.runner.promote("market_data")
        self.assertEqual(set(bundle["dependents"]), {"signal"})
        self.assertTrue(bundle["dependents"]["signal"]["passed"])
        check = self.runner.ledger.envelope(bundle["dependents"]["signal"]["trial"])
        self.assertIn("dependency:market_data", check["identity"]["hashes"])

    def test_13_a_candidate_gets_one_holdout_in_any_epoch(self) -> None:
        # The data.lock commit in test 10 started a new epoch. The correct candidate
        # already used its holdout in test 6, so a new epoch does not earn another.
        self.assertTrue(self.runner.submit("signal", self.correct, "validation")["passed"])
        again = self.runner.holdout("signal")
        self.assertIn("only a human can grant another", again["note"])
        # A human grants a second run, and the ledger records why.
        granted = self.runner.holdout("signal", rerun_reason="data.lock moved to real captured data")
        self.assertTrue(granted["passed"])
        [entry] = self.runner.ledger.find("signal", kind="trial", split="holdout",
                                         rerun_reason="data.lock moved to real captured data")
        self.assertEqual(entry.tag("candidate"), self.correct)

    def test_14_time_split_holdout_waits_for_new_data(self) -> None:
        path = self.root / "bricks/signal/evaluation/policy.yml"
        path.write_text(path.read_text().replace("kind: hash", "kind: time"))
        fixture.commit(self.root, "signal: forward holdout")
        renamed = fixture.SIGNAL_CORRECT.replace("price = market_data", "latest = market_data").replace(
            "if price <", "if latest <")
        commit = self.branch("renamed", {"bricks/signal/src/logic.py": renamed})
        self.assertTrue(self.runner.submit("signal", commit, "validation")["passed"])
        result = self.runner.holdout("signal")
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["have"], 0)


if __name__ == "__main__":
    unittest.main()
