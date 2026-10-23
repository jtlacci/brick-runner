"""Hermes as a live external (hermes.py, live.py): the parts that need no Hermes install."""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brick_runner import hermes  # noqa: E402
from brick_runner.live import Live, LiveError, clean  # noqa: E402

COMMIT = "41dfcf2c1bd7146d4ad46aea08b7dd007900602e"


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("sandbox")
        self.path_ = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.path_)


class ConfigTest(unittest.TestCase):
    def test_only_fixture_tools(self) -> None:
        config = json.loads(hermes.config("claude-sonnet-5-5", "/py"))
        self.assertEqual(config["platform_toolsets"], {"api_server": ["chip"]})
        for name in ("terminal", "file", "code_execution", "web", "browser", "delegation", "memory", "cronjob"):
            self.assertIn(name, config["agent"]["disabled_toolsets"])
        self.assertEqual(list(config["mcp_servers"]), ["chip"])
        self.assertIn("/mnt/_hermes_tools.py", config["mcp_servers"]["chip"]["args"])
        self.assertFalse(config["model"]["streaming"])
        self.assertEqual(config["model"]["default"], "claude-sonnet-5-5")

    def test_commit_must_be_a_full_hash(self) -> None:
        for bad in ("main", "41dfcf2c", "../../etc", COMMIT.upper()):
            with self.assertRaises(hermes.HermesError):
                hermes.install(bad)

    def test_cache_key_covers_fixture_and_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            evaluate = Path(tmp) / "evaluate.py"
            evaluate.write_text("HERMES_TOOLS = []\n")
            request = {"messages": [{"role": "user", "content": "hi"}], "profile": {"commit": COMMIT}}
            a = hermes.cache_key(request, {"events": []}, evaluate)
            self.assertEqual(a, hermes.cache_key(request, {"events": []}, evaluate))
            self.assertNotEqual(a, hermes.cache_key(request, {"events": [1]}, evaluate))
            evaluate.write_text("HERMES_TOOLS = [1]\n")
            self.assertNotEqual(a, hermes.cache_key(request, {"events": []}, evaluate))


class RelayTest(unittest.TestCase):
    def test_messages_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.sock"
            server = hermes.model_relay(path, lambda body: {"echo": body["model"]})
            try:
                conn = UnixHTTPConnection(str(path))
                conn.request("POST", "/v1/messages", body=json.dumps({"model": "m"}), headers={"Content-Type": "application/json"})
                reply = conn.getresponse()
                self.assertEqual((reply.status, json.loads(reply.read())), (200, {"echo": "m"}))
                conn = UnixHTTPConnection(str(path))
                conn.request("POST", "/v1/other", body="{}")
                self.assertEqual(conn.getresponse().status, 404)
            finally:
                server.shutdown()

    def test_failure_is_an_api_error(self) -> None:
        def fail(body):
            raise LiveError("budget", fatal=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.sock"
            server = hermes.model_relay(path, fail)
            try:
                conn = UnixHTTPConnection(str(path))
                conn.request("POST", "/v1/messages", body="{}")
                reply = conn.getresponse()
                self.assertEqual(reply.status, 400)
                self.assertEqual(json.loads(reply.read())["type"], "error")
            finally:
                server.shutdown()


class LiveHermesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.evaluate = Path(self.tmp.name) / "evaluate.py"
        self.evaluate.write_text("HERMES_TOOLS = []\n")
        env = mock.patch.dict(os.environ, {"BRICK_RUNNER_AGENT_MODELS": "claude-sonnet-5-5"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)

    def live(self, answer=None) -> Live:
        live = Live(object(), ["hermes"], Path(self.tmp.name) / "cache", sandbox=object(), evaluate_py=self.evaluate)
        live.hermes = mock.Mock()
        live.hermes.answer.side_effect = answer or (lambda request, fixture: {"text": "hi", "steps": [], "tools": []})
        return live

    def request(self, model: str = "claude-sonnet-5-5") -> dict:
        return {"system": "s", "messages": [{"role": "user", "content": "hi"}],
                "profile": {"commit": COMMIT, "model": model, "soul": ""}}

    def test_needs_sandbox_and_evaluation(self) -> None:
        with self.assertRaises(LiveError):
            Live(object(), ["hermes"], Path(self.tmp.name) / "c")

    def test_refuses_a_model_that_is_not_allowed(self) -> None:
        with self.assertRaises(LiveError):
            self.live().answer("hermes", self.request("claude-opus-9"), {})

    def test_caches_whole_answers(self) -> None:
        live = self.live()
        first = live.answer("hermes", self.request(), {"events": []})
        second = live.answer("hermes", self.request(), {"events": []})
        self.assertEqual(first, second)
        self.assertEqual(live.hermes.answer.call_count, 1)
        live.answer("hermes", self.request(), {"events": ["x"]})
        self.assertEqual(live.hermes.answer.call_count, 2)
        self.assertEqual(live.usage()["hermes_cached"], 1)

    def test_an_error_fails_the_case_and_is_not_cached(self) -> None:
        live = self.live(lambda request, fixture: {"error": "boom"})
        for _ in range(2):
            with self.assertRaises(LiveError) as caught:
                live.answer("hermes", self.request(), {})
            self.assertFalse(caught.exception.fatal or caught.exception.transient)
        self.assertEqual(live.hermes.answer.call_count, 2)

    def test_a_budget_in_a_relayed_call_stops_the_run(self) -> None:
        live = self.live()

        def answer(request, fixture):
            try:
                live._relayed({"model": "claude-sonnet-5-5", "max_tokens": 10, "messages": [{"role": "user", "content": "x"}]})
            except LiveError:
                pass
            return {"error": "the model API refused"}
        live.hermes.answer.side_effect = answer
        live.max_calls = 0
        with self.assertRaises(LiveError) as caught:
            live.answer("hermes", self.request(), {})
        self.assertTrue(caught.exception.fatal)

    def test_clamp_lowers_max_tokens(self) -> None:
        body = {"model": "claude-sonnet-5-5", "max_tokens": 64000, "messages": [{"role": "user", "content": "x"}]}
        with self.assertRaises(LiveError):
            clean(body)
        self.assertEqual(clean(body, clamp=True)["max_tokens"], 8192)


if __name__ == "__main__":
    unittest.main()


class JudgeCallsTest(unittest.TestCase):
    """judge_requests(record, output, calls) sees the case's calls; the two-argument form still works."""

    def run_judge(self, source: str) -> dict:
        from brick_runner.sandbox import Sandbox
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "bricks" / "x" / "evaluation"
            folder.mkdir(parents=True)
            (folder / "evaluate.py").write_text(source)
            result, _ = Sandbox(hide=[]).run("evaluator.py", Path(tmp), {
                "command": "judge_requests", "brick": "x",
                "items": [{"record": {"id": "r"}, "output": {}, "calls": [{"name": "external:hermes"}]}]})
            return result

    def test_calls_reach_judge_requests(self) -> None:
        result = self.run_judge('JUDGE = {"model": "m"}\n'
                                'def judge_requests(record, output, calls=()):\n'
                                '    return [{"prompt": str(len(calls))}]\n')
        self.assertEqual(result["requests"], [[{"prompt": "1"}]])

    def test_two_argument_form(self) -> None:
        result = self.run_judge('JUDGE = {"model": "m"}\n'
                                'def judge_requests(record, output):\n'
                                '    return [{"prompt": "ok"}]\n')
        self.assertEqual(result["requests"], [[{"prompt": "ok"}]])


class TokenCountTest(unittest.TestCase):
    def test_cached_prompt_tokens_count(self) -> None:
        judge = mock.Mock()
        judge.post.return_value = {"content": [], "usage": {"input_tokens": 4, "output_tokens": 60,
                                                            "cache_creation_input_tokens": 100,
                                                            "cache_read_input_tokens": 2600}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"BRICK_RUNNER_AGENT_MODELS": "m"}):
            live = Live(judge, ["anthropic"], Path(tmp))
            live.answer("anthropic", {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "x"}]})
            self.assertEqual(live.usage()["model_tokens"], 2764)
