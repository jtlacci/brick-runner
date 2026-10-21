"""Live model calls in an evaluation (live.py), end to end, on a real repository."""

from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture  # noqa: E402
from brick_runner.engine import Runner, RunnerError  # noqa: E402

WRITER = {
    "contract.py": '''
        """writer: one sentence about a topic, from a model."""
        from typing import TypedDict

        CONTRACT_VERSION = 1
        LANE = "strict"
        DETERMINISTIC = False
        SIBLING_DEPENDENCIES: dict[str, str] = {}
        OWNED_STATE: tuple[str, ...] = ()


        class BrickInput(TypedDict):
            topic: str


        class BrickOutput(TypedDict):
            text: str
    ''',
    "input/adapters/anthropic.py": '''
        """The model. In production the key is added outside the recorded request."""


        def ask(request: dict, ctx: dict) -> dict:
            return ctx["io"].external("anthropic", request, _never)


        def _never(request: dict) -> dict:
            raise RuntimeError("no live call in these tests")
    ''',
    "src/logic.py": '''
        from ..input.adapters import anthropic

        PROMPT = "Write one sentence about "
        MODEL = "claude-sonnet-5-5"


        def execute(inputs, ctx):
            answer = anthropic.ask({"model": MODEL, "max_tokens": 50,
                                    "messages": [{"role": "user", "content": PROMPT + inputs["topic"]}]}, ctx)
            return {"text": "".join(b.get("text", "") for b in answer.get("content", []))}
    ''',
    "evaluation/evaluate.py": '''
        """writer is correct when it writes some text."""
        GATES = (("wrote", ">=", 1.0),)


        def build(record):
            return record["input"]


        def score(record, output, later):
            return {"wrote": 1.0 if output and output.get("text") else 0.0}


        def generate(seed):
            return [{"input": {"topic": t}, "exchanges": []} for t in ("rain", "bread", "trains", "ink", "maps")]
    ''',
}


class FakeAnthropic(http.server.BaseHTTPRequestHandler):
    calls: list[dict] = []
    status = 200

    def log_message(self, *_args) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        FakeAnthropic.calls.append({"body": body, "key": self.headers.get("x-api-key")})
        if FakeAnthropic.status != 200:
            self.send_response(FakeAnthropic.status)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        text = "A sentence about " + body["messages"][0]["content"].rsplit(" ", 1)[-1] + "."
        answer = json.dumps({"content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                             "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)


class LiveScenario(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="brick-live-"))
        cls.root = fixture.make_repo(cls.tmp / "repo")
        target = cls.root / "bricks/writer"
        shutil.copytree(fixture.FRAMEWORK / "bricks/example_brick", target, ignore=shutil.ignore_patterns("__pycache__"))
        (target / "input/adapters/example_source.py").unlink()
        for relative, text in WRITER.items():
            fixture.write(target, relative, text)
        policy = target / "evaluation/policy.yml"
        policy.write_text(policy.read_text().replace("metric: score", "metric: wrote"))
        fixture.commit(cls.root, "the writer brick")
        cls.runner = Runner(cls.root, cls.tmp / "home")
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAnthropic)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        (cls.tmp / "key").write_text("test-key\n")
        cls.env = {"BRICK_RUNNER_JUDGE_URL": f"http://127.0.0.1:{cls.server.server_address[1]}/v1/messages",
                   "BRICK_RUNNER_JUDGE_KEY": str(cls.tmp / "key")}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def policy(self, name: str, extra: str, swap: tuple[str, str] | None = None) -> str:
        import re
        fixture.git(self.root, "checkout", "-q", "-B", name, "main")
        path = self.root / "bricks/writer/evaluation/policy.yml"
        text = path.read_text()
        if "budget:" in extra:
            text = re.sub(r"\nbudget:\n(  .*\n)*", "\n", text)
        path.write_text(text + "\n" + extra)
        if swap:
            logic = self.root / "bricks/writer/src/logic.py"
            logic.write_text(logic.read_text().replace(*swap))
        commit = fixture.commit(self.root, name)
        fixture.git(self.root, "checkout", "-q", "main")
        return self.runner.repo.commit(commit)

    def evaluate(self, base: str) -> dict:
        with mock.patch.dict(os.environ, self.env):
            return self.runner.evaluate("writer", "development", [], base=base, cache=False)

    def test_1_without_live_externals_the_cases_are_out_of_evidence(self) -> None:
        result = self.evaluate(self.runner.repo.commit("main"))
        self.assertFalse(result["passed"])
        self.assertEqual(result["counts"]["out_of_evidence"], result["counts"]["cases"])

    def test_2_live_answers_are_used_and_cached(self) -> None:
        base = self.policy("live", "live_externals: [anthropic]\n")
        FakeAnthropic.calls.clear()
        result = self.evaluate(base)
        self.assertTrue(result["passed"], result.get("reasons"))
        made = len(FakeAnthropic.calls)
        self.assertGreater(made, 0)
        self.assertEqual(result["counts"]["model_calls"], made)
        self.assertEqual(FakeAnthropic.calls[0]["key"], "test-key")
        self.assertNotIn("key", json.dumps(FakeAnthropic.calls[0]["body"]).lower())
        again = self.evaluate(base)
        self.assertEqual(len(FakeAnthropic.calls), made, "the second run uses the cache")
        self.assertEqual(again["counts"]["model_calls"], 0)
        self.assertEqual(again["counts"]["model_cached"], result["counts"]["model_calls"] + result["counts"]["model_cached"])

    def test_3_the_model_budget_stops_the_run(self) -> None:
        base = self.policy("live-budget", "live_externals: [anthropic]\nbudget:\n  max_model_calls: 1\n",
                           swap=('PROMPT = "Write one sentence about "', 'PROMPT = "Write one short sentence about "'))
        result = self.evaluate(base)
        self.assertFalse(result["passed"])
        self.assertTrue(any("budget: more than 1 live model calls" in r for r in result["reasons"]), result["reasons"])

    def test_4_a_model_that_is_not_allowed_fails_the_case(self) -> None:
        base = self.policy("live-model", "live_externals: [anthropic]\n", swap=('MODEL = "claude-sonnet-5-5"', 'MODEL = "gpt-x"'))
        FakeAnthropic.calls.clear()
        result = self.evaluate(base)
        self.assertFalse(result["passed"])
        self.assertEqual(result["counts"]["run_errors"], result["counts"]["cases"])
        self.assertEqual(FakeAnthropic.calls, [])

    def test_5_an_outage_is_not_recorded(self) -> None:
        base = self.policy("live-down", "live_externals: [anthropic]\n", swap=('PROMPT = "Write one sentence about "', 'PROMPT = "Tell me about "'))
        FakeAnthropic.status = 503
        try:
            with mock.patch("brick_runner.judge.time.sleep"):
                with self.assertRaises(RunnerError):
                    self.evaluate(base)
        finally:
            FakeAnthropic.status = 200

    def test_6_an_unknown_provider_fails_the_run(self) -> None:
        base = self.policy("live-unknown", "live_externals: [openai]\n")
        result = self.evaluate(base)
        self.assertFalse(result["passed"])
        self.assertIn("unknown provider", " ".join(result["reasons"]))


if __name__ == "__main__":
    unittest.main()
