"""The polling service against a fake GitHub API and a local Git remote."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture  # noqa: E402
from brick_runner.github import redact  # noqa: E402
from brick_runner.serve import MARKER, Service, _code, _pre  # noqa: E402

REPO = "jtlacci/bricks"
TOKEN = "ghs_FAKEtokenFAKEtokenFAKEtoken0123"


def stamp(delta: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    repository = REPO

    def __init__(self) -> None:
        self.pulls: list[dict] = []
        self.statuses: dict[str, list[tuple[str, str]]] = {}
        self.comments: dict[int, dict] = {}      # id -> {"number", "body"}
        self.incoming: list[dict] = []           # comments people post
        self.next_id = 100

    def token(self):
        return TOKEN

    def open_pulls(self):
        return list(self.pulls)

    def set_status(self, sha, state, description, context="brick-runner"):
        self.statuses.setdefault(sha, []).append((state, description))

    def post_comment(self, number, body):
        self.next_id += 1
        self.comments[self.next_id] = {"number": number, "body": body}
        return self.next_id

    def edit_comment(self, comment_id, body):
        self.comments[comment_id]["body"] = body

    def comments_since(self, since):
        return [c for c in self.incoming if c["updated_at"] >= since]

    def clone_url(self):
        raise AssertionError("tests fetch from a local remote")

    def git_env(self):
        return {}

    # helpers
    def final(self, sha):
        return self.statuses[sha][-1]

    def bodies(self, number):
        return [c["body"] for c in self.comments.values() if c["number"] == number]


class ServeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="brick-serve-"))
        cls.work = fixture.make_repo(cls.tmp / "work")
        cls.origin = cls.tmp / "origin.git"
        fixture.git(cls.tmp, "clone", "-q", "--bare", str(cls.work), str(cls.origin))
        cls.github = FakeGitHub()
        cls.service = Service(cls.github, cls.tmp / "home", allowed={"JTLacci"}, remote=str(cls.origin))

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def open_pull(self, number: int, files: dict[str, str], *, base: str = "main", source: str = REPO,
                  start: str = "main") -> str:
        fixture.git(self.work, "checkout", "-q", "-B", f"pr{number}", start)
        for relative, text in files.items():
            fixture.write(self.work, relative, text)
        sha = fixture.commit(self.work, f"pr {number}")
        fixture.git(self.work, "checkout", "-q", "main")
        fixture.git(self.work, "push", "-q", "-f", str(self.origin), f"{sha}:refs/pull/{number}/head")
        self.github.pulls.append({"number": number, "head": {"sha": sha, "repo": {"full_name": source}},
                                  "base": {"ref": base}})
        return sha

    def comment(self, cid: int, body: str, login: str = "jtlacci", kind: str = "User", edited: bool = False) -> None:
        self.github.incoming.append({
            "id": cid, "body": body, "user": {"login": login, "type": kind},
            "issue_url": f"https://api.github.com/repos/{REPO}/issues/1",
            "created_at": stamp(), "updated_at": stamp(5 if edited else 0)})

    def test_01_a_correct_code_change_passes(self) -> None:
        sha = self.open_pull(1, {"bricks/signal/src/logic.py": fixture.SIGNAL_CORRECT})
        self.service.cycle()
        self.assertEqual(self.github.statuses[sha][0][0], "pending")
        self.assertEqual(self.github.final(sha)[0], "success", self.github.bodies(1))
        [body] = self.github.bodies(1)
        self.assertIn(MARKER, body)
        self.assertIn("(code loop)", body)
        self.assertIn("| validation | ✅ passed", body)

    def test_02_a_head_is_evaluated_once(self) -> None:
        before = {sha: len(states) for sha, states in self.github.statuses.items()}
        self.service.cycle()
        self.assertEqual({sha: len(states) for sha, states in self.github.statuses.items()}, before)

    def test_03_a_new_push_updates_the_same_comment(self) -> None:
        fixture.git(self.work, "checkout", "-q", "pr1")
        fixture.write(self.work, "bricks/signal/src/logic.py", fixture.SIGNAL_BUGGY)
        sha = fixture.commit(self.work, "pr 1, second push")
        fixture.git(self.work, "checkout", "-q", "main")
        fixture.git(self.work, "push", "-q", "-f", str(self.origin), f"{sha}:refs/pull/1/head")
        self.github.pulls[0]["head"]["sha"] = sha
        self.service.cycle()
        self.assertEqual(self.github.final(sha)[0], "failure")
        [body] = self.github.bodies(1)
        self.assertIn(sha[:12], body)
        self.assertIn("Development counterexamples", body)

    def test_04_a_mixed_change_fails_without_evaluation(self) -> None:
        evaluate = (self.work / "bricks/signal/evaluation/evaluate.py").read_text() + "\n# tweak\n"
        sha = self.open_pull(4, {"bricks/signal/src/logic.py": fixture.SIGNAL_CORRECT,
                                 "bricks/signal/evaluation/evaluate.py": evaluate})
        self.service.cycle()
        self.assertEqual(self.github.final(sha), ("failure", "mixes code-loop and evaluation-loop files"))

    def test_05_an_evaluation_change_runs_the_defect_check(self) -> None:
        text = (self.work / "bricks/signal/evaluation/evaluate.py").read_text()
        start, end = text.index("def score("), text.index("def generate(")
        weak = text[:start] + fixture.SCORE_ALWAYS_PASSES.strip() + "\n\n\n" + text[end:]
        sha = self.open_pull(5, {"bricks/signal/evaluation/evaluate.py": weak})
        self.service.cycle()
        self.assertEqual(self.github.final(sha)[0], "failure")
        [body] = self.github.bodies(5)
        self.assertIn("(evaluation loop)", body)
        self.assertIn("Defect check: ❌ failed", body)

    def test_06_forks_and_other_bases_get_no_status(self) -> None:
        fork = self.open_pull(6, {"bricks/signal/src/logic.py": fixture.SIGNAL_CORRECT}, source="someone/fork")
        other = self.open_pull(7, {"bricks/signal/src/logic.py": fixture.SIGNAL_CORRECT}, base="develop")
        docs = self.open_pull(8, {"README.md": "changed\n"})
        self.service.cycle()
        self.assertNotIn(fork, self.github.statuses)
        self.assertNotIn(other, self.github.statuses)
        self.assertEqual(self.github.final(docs), ("success", "nothing to evaluate"))
        self.assertIn("Human-only files changed", self.github.bodies(8)[0])

    def test_07_a_second_pull_request_cannot_turn_a_failure_green(self) -> None:
        # PR 1's head failed in test 3. The same commit, offered into another base, gets no status.
        failing = self.github.pulls[0]["head"]["sha"]
        self.github.pulls.append({"number": 9, "head": {"sha": failing, "repo": {"full_name": REPO}},
                                  "base": {"ref": "develop"}})
        self.service.cycle()
        self.assertEqual(self.github.final(failing)[0], "failure")

    def test_08_repository_names_compare_without_case(self) -> None:
        sha = self.open_pull(10, {"bricks/signal/src/logic.py": fixture.SIGNAL_BUGGY}, source=REPO.upper())
        self.service.cycle()
        self.assertEqual(self.github.final(sha)[0], "failure")

    def test_09_only_unedited_commands_from_allowed_humans_run(self) -> None:
        # PR 1's first commit (correct) passed validation in test 1.
        self.comment(1, "/brick holdout signal", login="claude[bot]", kind="Bot")
        self.comment(2, "/brick holdout signal", login="someone-else")
        self.comment(3, "/brick promote signal", edited=True)          # an edited comment never runs
        self.comment(4, "/brick holdout signal", login="jtlacci")        # allow list ignores case
        self.service.cycle()
        replies = [b for b in self.github.bodies(1) if "Requested by" in b]
        self.assertEqual(len(replies), 1)
        self.assertIn("✅ passed", replies[0])
        self.comment(5, "/brick promote signal")
        self.service.cycle()
        replies = [b for b in self.github.bodies(1) if "Requested by" in b]
        self.assertIn("Promoted", replies[-1])
        # The same comments are never run twice.
        count = len(self.github.comments)
        self.service.cycle()
        self.assertEqual(len(self.github.comments), count)

    def test_10_a_new_main_re_evaluates_open_pull_requests(self) -> None:
        sha = self.github.pulls[0]["head"]["sha"]
        before = len(self.github.statuses[sha])
        fixture.write(self.work, "README.md", "a new main\n")
        fixture.commit(self.work, "move main")
        fixture.git(self.work, "push", "-q", str(self.origin), "main:main")
        self.service.cycle()
        self.assertGreater(len(self.github.statuses[sha]), before)
        self.assertEqual(self.github.final(sha)[0], "failure")

    def test_11_non_ascii_file_names_are_classified(self) -> None:
        evaluate = (self.work / "bricks/signal/evaluation/evaluate.py").read_text()
        # Only the non-ASCII file is a code-loop file: the change is mixed only if it is classified.
        sha = self.open_pull(11, {"bricks/signal/src/données.py": "X = 1\n",
                                  "bricks/signal/evaluation/evaluate.py": evaluate + "\n# x\n"})
        self.service.cycle()
        self.assertEqual(self.github.final(sha)[1], "mixes code-loop and evaluation-loop files")

    def test_12_a_brick_removed_whole_is_a_human_change(self) -> None:
        fixture.git(self.work, "checkout", "-q", "-B", "pr12", "main")
        fixture.git(self.work, "rm", "-rq", "bricks/signal")
        sha = fixture.commit(self.work, "pr 12: remove signal")
        fixture.git(self.work, "checkout", "-q", "main")
        fixture.git(self.work, "push", "-q", "-f", str(self.origin), f"{sha}:refs/pull/12/head")
        self.github.pulls.append({"number": 12, "head": {"sha": sha, "repo": {"full_name": REPO}},
                                  "base": {"ref": "main"}})
        self.service.cycle()
        self.assertEqual(self.github.final(sha), ("success", "nothing to evaluate"))
        self.assertIn("Removed bricks", self.github.bodies(12)[0])


class RenderingTests(unittest.TestCase):
    def test_escaping_stops_markdown_html_and_mentions(self) -> None:
        hostile = "```\n</details>| ✅ passed | **bold** @jtlacci <img src=x> :+1:"
        for rendered in (_code(hostile), _pre(hostile)):
            self.assertNotIn("```", rendered)
            self.assertNotIn("</details>", rendered)
            self.assertNotIn("@jtlacci", rendered)
            self.assertNotIn("<img", rendered)
            self.assertNotIn("**", rendered)

    def test_redaction(self) -> None:
        import base64
        basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        text = redact(f"token {TOKEN} header {basic}", [TOKEN, ""])
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(basic, text)

    def test_posted_text_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            service = Service(FakeGitHub(), Path(home))
            with mock.patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "aws-secret-value-1234"}):
                self.assertEqual(service.clean(f"x {TOKEN} aws-secret-value-1234"), "x [redacted] [redacted]")


if __name__ == "__main__":
    unittest.main()
