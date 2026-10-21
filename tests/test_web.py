"""Web bricks and the model judge, end to end, on a real repository."""

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
from brick_runner import web  # noqa: E402
from brick_runner.engine import Runner  # noqa: E402

HAS_BROWSER = shutil.which("npm") is not None and bool(os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
                                                        or os.environ.get("BRICK_RUNNER_BROWSERS"))
HAS_CHROME = web.CHROME.is_file() and shutil.which("Xvfb") is not None

WEB = {
    "contract.py": '''
        """web: the browser app."""
        CONTRACT_VERSION = 1
        KIND = "web"
        LANE = "strict"
        DETERMINISTIC = True
        SIBLING_DEPENDENCIES: dict[str, str] = {}
        OWNED_STATE: tuple[str, ...] = ()
    ''',
    "app/AGENTS.md": "# app\n\nThe browser app.\n",
    "app/package.json": '{"name": "web", "version": "1.0.0", "private": true, "scripts": {"build": "node build.js"}}\n',
    "app/package-lock.json": json.dumps({"name": "web", "version": "1.0.0", "lockfileVersion": 3, "requires": True,
                                          "packages": {"": {"name": "web", "version": "1.0.0"}}}) + "\n",
    "app/build.js": '''
        const fs = require("fs");
        fs.mkdirSync("dist", { recursive: true });
        for (const name of fs.readdirSync("src")) fs.copyFileSync("src/" + name, "dist/" + name);
    ''',
    "app/src/index.html": '<!doctype html><meta name="viewport" content="width=device-width"><h1 id="title"></h1><script src="main.js"></script>\n',
    "app/src/main.js": "document.getElementById('title').textContent = 'Feed';\n",
    "evaluation/evaluate.py": '''
        """Evaluation for the web brick: the feed title renders at phone width."""

        GATES = (("title_ok", ">=", 1.0),)


        def build(record):
            return record["input"]


        def observe(input, browser, base_url):
            page = browser.new_page(viewport={"width": input["width"], "height": 800})
            page.goto(base_url + "/")
            seen = {"title": page.inner_text("#title"),
                    "overflow": page.evaluate("document.documentElement.scrollWidth > window.innerWidth")}
            page.close()
            return seen


        def score(record, output, later):
            ok = output["title"] == "Feed" and not output["overflow"]
            return {"title_ok": 1.0 if ok else 0.0}


        def generate(seed):
            return [{"input": {"width": width}} for width in (375, 1280)]
    ''',
}

JUDGED = '''
    """Evaluation with a model judge."""

    GATES = (("title_ok", ">=", 1.0), ("quality", ">=", 0.5))
    JUDGE = {"model": "claude-test-judge"}


    def build(record):
        return record["input"]


    def observe(input, browser, base_url):
        page = browser.new_page(viewport={"width": input["width"], "height": 800})
        page.goto(base_url + "/")
        seen = {"title": page.inner_text("#title")}
        page.close()
        return seen


    def judge_requests(record, output):
        return [{"system": "Answer with one number from 0 to 10.", "prompt": "Rate this title: " + output["title"]}]


    def score(record, output, later, judgments=()):
        return {"title_ok": 1.0 if output["title"] == "Feed" else 0.0, "quality": float(judgments[0]) / 10}


    def generate(seed):
        return [{"input": {"width": 375}}]
'''


class FakeJudge(http.server.BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, *_args) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        FakeJudge.calls.append({"body": body, "key": self.headers.get("x-api-key")})
        answer = json.dumps({"content": [{"type": "text", "text": "8"}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)


HEADED = '''
    """Evaluation that needs a real, headed browser."""

    GATES = (("headed", ">=", 1.0),)


    def build(record):
        return record["input"]


    def observe(input, browser, base_url):
        page = browser.new_page()
        page.goto(base_url + "/")
        seen = {"agent": page.evaluate("navigator.userAgent"), "title": page.inner_text("#title")}
        page.close()
        return seen


    def score(record, output, later):
        ok = 1.0 if "HeadlessChrome" not in output["agent"] and output["title"] == "Feed" else 0.0
        return {"headed": ok, "title_ok": ok}


    def generate(seed):
        return [{"input": {"n": n}} for n in range(4)]
'''

AGENT_APP = {
    "bricks/web/app/src/index.html": ('<!doctype html><style>body{margin:0}</style>'
                                      '<button id="go" style="position:absolute;left:10px;top:10px;width:100px;height:40px">Go</button>'
                                      '<h1 id="title" style="margin-top:80px"></h1><script src="main.js"></script>\n'),
    "bricks/web/app/src/main.js": "document.getElementById('go').onclick = () => { document.getElementById('title').textContent = 'Clicked'; };\n",
    "bricks/web/evaluation/evaluate.py": '''
    """Evaluation with a computer use agent: it clicks Go."""

    GATES = (("rendered", ">=", 1.0), ("clicked", ">=", 1.0))
    AGENT = {"model": "claude-test-agent", "terms": ["clicked"]}


    def build(record):
        return record["input"]


    def observe(input, browser, base_url, agent=None):
        page = browser.new_page(viewport={"width": 400, "height": 300})
        page.goto(base_url + "/")
        seen = {"button": page.is_visible("#go")}
        if "task" in input:
            seen["run"] = agent.run(page, input["task"])
            seen["title"] = page.inner_text("#title")
        page.close()
        return seen


    def score(record, output, later):
        if "task" in record["input"]:
            ok = output["title"] == "Clicked" and output["run"]["status"] == "done"
            return {"clicked": 1.0 if ok else 0.0, "title_ok": 1.0}
        return {"rendered": 1.0 if output["button"] else 0.0, "title_ok": 1.0}


    def generate(seed):
        return [{"input": {"n": n}} for n in range(3)] + [{"input": {"task": "Click the Go button.", "n": n}} for n in range(3)]
''',
}


class FakeAgent(http.server.BaseHTTPRequestHandler):
    """Clicks (60, 30), then reports DONE. ``mode`` "down" answers 503; "batch" sends a bad action first."""
    calls: list[dict] = []
    mode = "ok"

    def log_message(self, *_args) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        FakeAgent.calls.append(body)
        if FakeAgent.mode == "down":
            self.send_response(503)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        last = body["messages"][-1]["content"]
        if any(b.get("type") == "tool_result" for b in last):
            content = [{"type": "text", "text": "DONE The title says Clicked."}]
        elif FakeAgent.mode == "batch":
            content = [{"type": "tool_use", "id": "toolu_0", "name": "teleport", "toolset_name": "computer", "input": {}},
                       {"type": "tool_use", "id": "toolu_1", "name": "left_click", "toolset_name": "computer",
                        "input": {"coordinate": [60, 30]}}]
        else:
            content = [{"type": "tool_use", "id": "toolu_1", "name": "left_click", "toolset_name": "computer",
                        "input": {"coordinate": [60, 30]}}]
        answer = json.dumps({"content": content, "usage": {"input_tokens": 100, "output_tokens": 10}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)


class AgentUnitTests(unittest.TestCase):
    def test_only_the_newest_screenshots_stay(self) -> None:
        from brick_runner.agent import _keep_newest_images
        image = {"type": "image", "source": {}}
        messages = [{"role": "user", "content": [{"type": "text", "text": "task"}, dict(image)]},
                    {"role": "user", "content": [{"type": "tool_result", "content": [dict(image)]}]},
                    {"role": "user", "content": [{"type": "tool_result", "content": [dict(image)]}]}]
        _keep_newest_images(messages, 2)
        self.assertEqual(messages[0]["content"][1]["type"], "text")
        self.assertEqual(messages[1]["content"][0]["content"][0]["type"], "image")
        self.assertEqual(messages[2]["content"][0]["content"][0]["type"], "image")

    def test_key_names(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "brick_runner" / "workers"))
        from _agent import key_name
        self.assertEqual(key_name("ctrl+a"), "Control+a")
        self.assertEqual(key_name("Return"), "Enter")
        self.assertEqual(key_name("shift+Tab"), "Shift+Tab")
        self.assertEqual(key_name("f5"), "F5")

    def test_a_session_needs_results_for_its_actions(self) -> None:
        from brick_runner.agent import Agents

        class Judge:
            def post(self, body):
                return {"content": [{"type": "tool_use", "id": "a1", "name": "screenshot", "input": {}}]}

        agents = Agents(Judge(), "claude-sonnet-5-5")
        self.assertIn("actions", agents.step({"session": "s", "task": "do it", "results": [{"image": "x"}]}))
        self.assertIn("error", agents.step({"session": "s", "results": [{"id": "other", "text": "OK"}]}))
        # The session ended with that error; it cannot be reopened.
        self.assertEqual(agents.step({"session": "s", "results": [{"id": "a1", "text": "OK"}]}),
                         {"error": "this session has ended"})
        self.assertIn("error", agents.step({"session": "new", "results": [{"image": "x"}]}))   # no task
        self.assertIn("error", agents.step({"session": "bad", "task": "t", "results": "x"}))
        self.assertIn("error", agents.step({"session": "big", "task": "t", "results": 5000 * [{"text": "x"}]}))

    def test_models_are_allowed_by_name(self) -> None:
        from brick_runner.agent import AgentError, Agents
        with self.assertRaises(AgentError):
            Agents(object(), "some-other-model")

    def test_the_evaluation_wide_limit_is_fatal(self) -> None:
        from brick_runner import agent as module

        class Judge:
            def post(self, body):
                return {"content": [{"type": "tool_use", "id": "a1", "name": "screenshot", "input": {}}]}

        agents = module.Agents(Judge(), "claude-sonnet-5-5")
        agents.calls = module.MAX_CALLS_PER_EVALUATION
        with self.assertRaises(module.AgentError):
            agents.step({"session": "s", "task": "do it", "results": [{"image": "x"}]})

    def test_header_values_are_printable_ascii(self) -> None:
        folder = Path(tempfile.mkdtemp(prefix="brick-headers-"))
        try:
            for value in ("a\x00b", "a\x0bb", "caf\u00e9", "a\nb"):
                (folder / "headers.json").write_text(json.dumps({"Referrer-Policy": value}))
                with self.assertRaises(web.BuildError):
                    web.read_headers(folder)
            (folder / "headers.json").write_text(json.dumps(["not", "an", "object"]))
            with self.assertRaises(web.BuildError):
                web.read_headers(folder)
            (folder / "headers.json").write_text(json.dumps({"referrer-policy": "same-origin"}))
            self.assertEqual(web.read_headers(folder), {"Referrer-Policy": "same-origin"})
        finally:
            shutil.rmtree(folder, ignore_errors=True)


def add_web_brick(root: Path) -> None:
    target = root / "bricks/web"
    for relative, text in WEB.items():
        fixture.write(target, relative, text)
    for name in ("AGENTS.md", "policy.yml", "data.lock"):
        shutil.copy(fixture.FRAMEWORK / "bricks/example_brick/evaluation" / name, target / "evaluation" / name)
    policy = target / "evaluation/policy.yml"
    policy.write_text(policy.read_text()
                      .replace("implementation: [src/**, input/adapters/**, input/config.yml, runner/**, __main__.py]",
                               "implementation: [app/**]")
                      .replace("metric: score", "metric: title_ok").replace("min_holdout: 20", "min_holdout: 1"))


class LockTests(unittest.TestCase):
    def test_only_the_npm_registry_with_integrity(self) -> None:
        lock = {"lockfileVersion": 3, "packages": {
            "": {"name": "x"},
            "node_modules/good": {"resolved": "https://registry.npmjs.org/good/-/good-1.0.0.tgz", "integrity": "sha512-abc"},
            "node_modules/evil": {"resolved": "http://169.254.169.254/latest/meta-data", "integrity": "sha512-abc"},
            "node_modules/loose": {"resolved": "https://registry.npmjs.org/loose/-/loose-1.0.0.tgz"}}}
        problems = web.check_lock(lock)
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(any("evil" in p for p in problems))
        self.assertTrue(any("loose" in p for p in problems))
        self.assertTrue(web.check_lock({"lockfileVersion": 1, "packages": {}}))


@unittest.skipUnless(HAS_BROWSER, "npm and the Playwright browsers are needed")
class WebScenario(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="brick-web-"))
        cls.root = fixture.make_repo(cls.tmp / "repo")
        add_web_brick(cls.root)
        fixture.commit(cls.root, "the web brick")
        cls.runner = Runner(cls.root, cls.tmp / "home")
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeJudge)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        (cls.tmp / "judge-key").write_text("test-key\n")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def branch(self, name: str, files: dict[str, str]) -> str:
        fixture.git(self.root, "checkout", "-q", "-B", name, "main")
        for relative, text in files.items():
            fixture.write(self.root, relative, text)
        commit = fixture.commit(self.root, name)
        fixture.git(self.root, "checkout", "-q", "main")
        return commit

    def test_01_the_fixture_lints_clean(self) -> None:
        result = fixture.lint(self.root)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_02_a_correct_app_passes(self) -> None:
        commit = self.branch("ok", {"bricks/web/app/src/main.js": WEB["app/src/main.js"] + "// v2\n"})
        view = self.runner.submit("web", commit, "development")
        self.assertTrue(view["passed"], view)
        self.assertGreater(view["counts"]["scored"], 0)

    def test_03_a_wrong_title_fails(self) -> None:
        commit = self.branch("wrong", {"bricks/web/app/src/main.js": "document.getElementById('title').textContent = 'Fed';\n"})
        view = self.runner.submit("web", commit, "development")
        self.assertFalse(view["passed"])
        self.assertIn("gate title_ok >= 1.0 failed", view["reasons"])

    def test_04_overflow_at_phone_width_fails(self) -> None:
        wide = "document.getElementById('title').textContent = 'Feed'; document.body.style.width = '900px';\n"
        view = self.runner.submit("web", self.branch("wide", {"bricks/web/app/src/main.js": wide}), "development")
        self.assertFalse(view["passed"])
        self.assertLess(view["terms"]["title_ok"], 1.0)

    def test_05_a_broken_build_fails(self) -> None:
        commit = self.branch("broken", {"bricks/web/app/build.js": "process.exit(3);\n"})
        view = self.runner.submit("web", commit, "development")
        self.assertFalse(view["passed"])
        self.assertIn("the web build failed", " ".join(view["reasons"]))

    def test_06_the_build_has_no_network(self) -> None:
        reach = ('require("https").get("https://registry.npmjs.org/", () => process.exit(0))'
                 '.on("error", () => { require("fs").mkdirSync("dist"); '
                 'require("fs").writeFileSync("dist/index.html", "<h1 id=title>Feed</h1>"); });\n')
        commit = self.branch("network", {"bricks/web/app/build.js": reach})
        view = self.runner.submit("web", commit, "development")
        # The request fails in the sandbox, so the build writes the page and the app passes.
        self.assertTrue(view["passed"], view)

    def test_07_a_lockfile_from_another_host_is_refused(self) -> None:
        lock = json.dumps({"name": "web", "version": "1.0.0", "lockfileVersion": 3, "packages": {
            "": {"name": "web", "version": "1.0.0", "dependencies": {"x": "1.0.0"}},
            "node_modules/x": {"version": "1.0.0", "resolved": "https://evil.example/x.tgz", "integrity": "sha512-a"}}})
        commit = self.branch("lock", {"bricks/web/app/package-lock.json": lock})
        view = self.runner.submit("web", commit, "development")
        self.assertFalse(view["passed"])
        self.assertIn("package-lock.json refused", " ".join(view["reasons"]))

    def test_07b_a_symlink_in_the_brick_is_refused(self) -> None:
        fixture.git(self.root, "checkout", "-q", "-B", "link", "main")
        (self.root / "bricks/web/app/src/secret").symlink_to("../../evaluation/evaluate.py")
        commit = fixture.commit(self.root, "a link")
        fixture.git(self.root, "checkout", "-q", "main")
        with self.assertRaises(Exception) as caught:
            self.runner.submit("web", commit, "development")
        self.assertIn("symlinks are not allowed", str(caught.exception))

    def test_07c_too_many_judge_requests_fail(self) -> None:
        many = JUDGED.replace("return [{\"system\"", "return 9 * [{\"system\"")
        self.assertNotEqual(many, JUDGED)
        commit = self.branch("many", {"bricks/web/evaluation/evaluate.py": many})
        env = {"BRICK_RUNNER_JUDGE_URL": f"http://127.0.0.1:{self.server.server_address[1]}/v1/messages",
               "BRICK_RUNNER_JUDGE_KEY": str(self.tmp / "judge-key")}
        with mock.patch.dict(os.environ, env):
            result = self.runner.evaluate("web", "development", [], base=self.runner.repo.commit(commit), cache=False)
        self.assertFalse(result["passed"])
        self.assertIn("judge_requests() failed on some cases", result["reasons"])

    def test_08_the_judge_scores_and_is_cached(self) -> None:
        env = {"BRICK_RUNNER_JUDGE_URL": f"http://127.0.0.1:{self.server.server_address[1]}/v1/messages",
               "BRICK_RUNNER_JUDGE_KEY": str(self.tmp / "judge-key")}
        commit = self.branch("judged", {"bricks/web/evaluation/evaluate.py": JUDGED})
        with mock.patch.dict(os.environ, env):
            view = self.runner.evaluation_change("web", commit)
        trial = view.get("trial_view") or view
        self.assertTrue(FakeJudge.calls, view)
        # The app's JavaScript is mutated, rebuilt and observed; every mutant changes the title, so all are caught.
        self.assertTrue(view["defect_check"]["passed"], view["defect_check"])
        call = FakeJudge.calls[0]
        self.assertEqual(call["key"], "test-key")
        self.assertEqual(call["body"]["model"], "claude-test-judge")
        self.assertEqual(call["body"]["system"], "Answer with one number from 0 to 10.")
        self.assertIn("Rate this title: Feed", call["body"]["messages"][0]["content"])
        before = len(FakeJudge.calls)
        with mock.patch.dict(os.environ, env):
            again = self.runner.evaluate("web", "development", [], base=self.runner.repo.commit(commit), cache=False)
        self.assertEqual(len(FakeJudge.calls), before, "a cached judgement is not asked again")
        self.assertAlmostEqual(again["terms"]["quality"], 0.8)
        self.assertIsNotNone(trial)

    def test_09_no_judge_key_fails_the_evaluation(self) -> None:
        commit = self.branch("nokey", {"bricks/web/evaluation/evaluate.py": JUDGED.replace("Rate this", "Judge this")})
        with mock.patch.dict(os.environ, {"BRICK_RUNNER_JUDGE_KEY": ""}):
            result = self.runner.evaluate("web", "development", [], base=self.runner.repo.commit(commit), cache=False)
        self.assertFalse(result["passed"])
        self.assertIn("no judge key", result["fatal"])

    def test_10_the_declared_headers_are_sent(self) -> None:
        strict = json.dumps({"Content-Security-Policy": "default-src 'self'; script-src 'none'"})
        view = self.runner.submit("web", self.branch("csp", {"bricks/web/app/headers.json": strict}), "development")
        # The policy blocks main.js, so the title stays empty, as it would in production.
        self.assertFalse(view["passed"], view)
        fine = json.dumps({"Content-Security-Policy": "default-src 'self'", "X-Content-Type-Options": "nosniff"})
        view = self.runner.submit("web", self.branch("csp-ok", {"bricks/web/app/headers.json": fine}), "development")
        self.assertTrue(view["passed"], view)

    def test_11_a_header_that_is_not_allowed_is_refused(self) -> None:
        bad = json.dumps({"Set-Cookie": "a=b"})
        view = self.runner.submit("web", self.branch("cookie", {"bricks/web/app/headers.json": bad}), "development")
        self.assertFalse(view["passed"])
        self.assertIn("not an allowed header", " ".join(view["reasons"]))

    @unittest.skipUnless(HAS_CHROME, "Google Chrome and Xvfb are needed")
    def test_12_real_chrome_runs_headed(self) -> None:
        commit = self.branch("headed", {"bricks/web/evaluation/evaluate.py": HEADED})
        with mock.patch.dict(os.environ, {"BRICK_RUNNER_BROWSER": "chrome"}):
            result = self.runner.evaluate("web", "development", [], base=self.runner.repo.commit(commit), cache=False)
        self.assertTrue(result["passed"], result)
        self.assertIn("headed", result["identity"]["toolchain"]["browser"])
        with mock.patch.dict(os.environ, {"BRICK_RUNNER_BROWSER": "chromium"}):
            result = self.runner.evaluate("web", "development", [], base=self.runner.repo.commit(commit), cache=False)
        self.assertFalse(result["passed"], "headless Chromium says HeadlessChrome")

    def test_13_the_agent_clicks_through_the_app(self) -> None:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAgent)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = {"BRICK_RUNNER_JUDGE_URL": f"http://127.0.0.1:{server.server_address[1]}/v1/messages",
               "BRICK_RUNNER_JUDGE_KEY": str(self.tmp / "judge-key"), "BRICK_RUNNER_AGENT_MODELS": "claude-test-agent"}
        try:
            commit = self.branch("agent", AGENT_APP)
            base = self.runner.repo.commit(commit)
            with mock.patch.dict(os.environ, env):
                result = self.runner.evaluate("web", "development", [], base=base, cache=False)
            self.assertTrue(result["passed"], result)
            self.assertGreaterEqual(result["counts"]["agent_calls"], 2)
            first = FakeAgent.calls[0]
            self.assertEqual(first["model"], "claude-test-agent")
            self.assertEqual(first["tools"], [{"type": "computer_toolset_20260801", "configs": {"zoom": {"enabled": False}}}])
            self.assertEqual(first["messages"][0]["content"][1]["type"], "image")
            answer = FakeAgent.calls[1]["messages"][-1]["content"][0]
            self.assertEqual(answer["type"], "tool_result")
            self.assertEqual(answer["toolset_name"], "computer")
            # The defect check runs no agent case and leaves the agent's gates out.
            before = len(FakeAgent.calls)
            with mock.patch.dict(os.environ, env):
                mutant = self.runner.evaluate("web", "development", [], base=base, cache=False, kind="defect-mutant")
            self.assertEqual(len(FakeAgent.calls), before)
            self.assertTrue(mutant["passed"], mutant)
            self.assertGreater(mutant["counts"]["agent_cases_skipped"], 0)
            self.assertNotIn("clicked", [g["term"] for g in mutant["gates"]])
        finally:
            server.shutdown()

    def _agent_env(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAgent)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = {"BRICK_RUNNER_JUDGE_URL": f"http://127.0.0.1:{server.server_address[1]}/v1/messages",
               "BRICK_RUNNER_JUDGE_KEY": str(self.tmp / "judge-key"), "BRICK_RUNNER_AGENT_MODELS": "claude-test-agent"}
        return server, env

    def test_14_an_api_outage_is_not_recorded(self) -> None:
        from brick_runner.engine import RunnerError
        server, env = self._agent_env()
        FakeAgent.mode = "down"
        try:
            commit = self.branch("agent-down", {**AGENT_APP, "bricks/web/app/src/main.js": AGENT_APP["bricks/web/app/src/main.js"] + "// down\n"})
            base = self.runner.repo.commit(commit)
            with mock.patch.dict(os.environ, env), mock.patch("brick_runner.judge.time.sleep"):
                with self.assertRaises(RunnerError):
                    self.runner.evaluate("web", "development", [], base=base)
            FakeAgent.mode = "ok"
            with mock.patch.dict(os.environ, env):
                again = self.runner.evaluate("web", "development", [], base=base)
            self.assertFalse(again.get("cached"), "the outage left no cached result")
            self.assertTrue(again["passed"], again)
        finally:
            FakeAgent.mode = "ok"
            server.shutdown()

    def test_15_after_a_failed_action_the_rest_of_the_turn_is_not_run(self) -> None:
        server, env = self._agent_env()
        FakeAgent.mode = "batch"
        FakeAgent.calls.clear()
        try:
            commit = self.branch("agent-batch", {**AGENT_APP, "bricks/web/app/src/main.js": AGENT_APP["bricks/web/app/src/main.js"] + "// batch\n"})
            with mock.patch.dict(os.environ, env):
                result = self.runner.evaluate("web", "development", [], base=self.runner.repo.commit(commit), cache=False)
            # The click after the bad action never ran, so the title never changed.
            self.assertFalse(result["passed"])
            answers = next(c["messages"][-1]["content"] for c in FakeAgent.calls
                           if any(b.get("type") == "tool_result" for b in c["messages"][-1]["content"]))
            self.assertTrue(answers[0]["is_error"])
            self.assertTrue(answers[1]["is_error"])
            self.assertIn("Not executed", answers[1]["content"][0]["text"])
        finally:
            FakeAgent.mode = "ok"
            server.shutdown()

    @unittest.skipUnless(HAS_CHROME, "Google Chrome and Xvfb are needed")
    def test_16_a_killed_worker_leaves_no_x_server(self) -> None:
        import subprocess as sp
        import time as clock
        from brick_runner.sandbox import Sandbox
        work = Path(tempfile.mkdtemp(prefix="brick-kill-"))
        try:
            (work / ".web-build").mkdir()
            (work / ".web-build/index.html").write_text("<h1>x</h1>")
            fixture.write(work / "bricks/web", "evaluation/evaluate.py", '''
                import time


                def observe(input, browser, base_url):
                    time.sleep(600)
            ''')
            fixture.write(work / "bricks/web", "contract.py", "KIND = 'web'\n")
            worker = Sandbox([]).start("evaluator.py", work)
            worker.send({"brick": "web", "command": "observe", "inputs": [{}], "agent": False,
                         "browser": web.browser_settings() if os.environ.get("BRICK_RUNNER_BROWSER") == "chrome"
                         else {"name": "chrome", "headed": True, "executable": str(web.CHROME), "xvfb": shutil.which("Xvfb")}})
            deadline = clock.monotonic() + 30
            mine = []
            while clock.monotonic() < deadline and not mine:
                clock.sleep(0.5)
                ps = sp.run(["ps", "-eo", "pid,ppid,args"], capture_output=True, text=True).stdout
                mine = [line for line in ps.splitlines()
                        if len(line.split()) > 3 and line.split()[2].endswith("Xvfb") and "-displayfd" in line]
            self.assertTrue(mine, "Xvfb started")
            pids = [line.split()[0] for line in mine]
            worker.stop()
            clock.sleep(1.0)
            ps = sp.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
            alive = [line for line in ps.splitlines() if line.split()[0] in pids and line.split()[1].endswith("Xvfb")]
            self.assertEqual(alive, [], "Xvfb died with the worker")
        finally:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
