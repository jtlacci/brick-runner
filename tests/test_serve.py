"""The polling service against a fake GitHub API and a local Git remote."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture  # noqa: E402
from brick_runner.serve import MARKER, Service  # noqa: E402

REPO = "jtlacci/bricks"


class FakeGitHub:
    repository = REPO

    def __init__(self) -> None:
        self.pulls: list[dict] = []
        self.statuses: dict[str, list[tuple[str, str]]] = {}
        self.comments: dict[int, dict] = {}      # id -> {"number", "body"}
        self.incoming: list[dict] = []           # comments people post
        self.next_id = 100

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
        return list(self.incoming)

    def clone_url(self):
        raise AssertionError("tests fetch from a local remote")

    def git_auth(self):
        return []

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
        cls.service = Service(cls.github, cls.tmp / "home", allowed={"jtlacci"}, remote=str(cls.origin))

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def open_pull(self, number: int, files: dict[str, str], *, base: str = "main", fork: bool = False) -> str:
        fixture.git(self.work, "checkout", "-q", "-B", f"pr{number}", "main")
        for relative, text in files.items():
            fixture.write(self.work, relative, text)
        sha = fixture.commit(self.work, f"pr {number}")
        fixture.git(self.work, "checkout", "-q", "main")
        fixture.git(self.work, "push", "-q", str(self.origin), f"{sha}:refs/pull/{number}/head")
        self.github.pulls.append({"number": number, "head": {"sha": sha, "repo": {
            "full_name": "someone/fork" if fork else REPO}}, "base": {"ref": base}})
        return sha

    def test_1_a_correct_code_change_passes(self) -> None:
        sha = self.open_pull(1, {"bricks/signal/src/logic.py": fixture.SIGNAL_CORRECT})
        self.service.cycle()
        self.assertEqual(self.github.statuses[sha][0][0], "pending")
        self.assertEqual(self.github.final(sha)[0], "success", self.github.bodies(1))
        [body] = self.github.bodies(1)
        self.assertIn(MARKER, body)
        self.assertIn("`signal` (code loop)", body)
        self.assertIn("| validation | ✅ passed", body)

    def test_2_a_head_is_evaluated_once(self) -> None:
        before = {sha: len(states) for sha, states in self.github.statuses.items()}
        self.service.cycle()
        self.assertEqual({sha: len(states) for sha, states in self.github.statuses.items()}, before)

    def test_3_a_new_push_updates_the_same_comment(self) -> None:
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

    def test_4_a_mixed_change_fails_without_evaluation(self) -> None:
        evaluate = (self.work / "bricks/signal/evaluation/evaluate.py").read_text() + "\n# tweak\n"
        sha = self.open_pull(4, {"bricks/signal/src/logic.py": fixture.SIGNAL_CORRECT,
                                 "bricks/signal/evaluation/evaluate.py": evaluate})
        self.service.cycle()
        self.assertEqual(self.github.final(sha), ("failure", "mixes code-loop and evaluation-loop files"))

    def test_5_an_evaluation_change_runs_the_defect_check(self) -> None:
        text = (self.work / "bricks/signal/evaluation/evaluate.py").read_text()
        start, end = text.index("def score("), text.index("def generate(")
        weak = text[:start] + fixture.SCORE_ALWAYS_PASSES.strip() + "\n\n\n" + text[end:]
        sha = self.open_pull(5, {"bricks/signal/evaluation/evaluate.py": weak})
        self.service.cycle()
        self.assertEqual(self.github.final(sha)[0], "failure")
        [body] = self.github.bodies(5)
        self.assertIn("(evaluation loop)", body)
        self.assertIn("Defect check: ❌ failed", body)

    def test_6_forks_other_bases_and_human_only_changes(self) -> None:
        fork = self.open_pull(6, {"bricks/signal/src/logic.py": fixture.SIGNAL_CORRECT}, fork=True)
        other = self.open_pull(7, {"bricks/signal/src/logic.py": fixture.SIGNAL_CORRECT}, base="develop")
        docs = self.open_pull(8, {"README.md": "changed\n"})
        self.service.cycle()
        self.assertIn("forks", self.github.final(fork)[1])
        self.assertIn("base is not main", self.github.final(other)[1])
        self.assertEqual(self.github.final(docs), ("success", "nothing to evaluate"))
        self.assertIn("Human-only files changed", self.github.bodies(8)[0])

    def test_7_only_allowed_humans_can_run_holdout_and_promote(self) -> None:
        # PR 1's first commit (correct) passed validation in test 1.
        issue = f"https://api.github.com/repos/{REPO}/issues/1"
        self.github.incoming = [
            {"id": 1, "body": "/brick holdout signal", "user": {"login": "claude[bot]", "type": "Bot"}, "issue_url": issue},
            {"id": 2, "body": "/brick holdout signal", "user": {"login": "jtlacci", "type": "User"}, "issue_url": issue},
            {"id": 3, "body": "/brick promote signal", "user": {"login": "jtlacci", "type": "User"}, "issue_url": issue},
        ]
        self.service.cycle()
        replies = [b for b in self.github.bodies(1) if "Requested by" in b or "not allowed" in b]
        self.assertIn("not allowed", replies[0])
        self.assertIn("✅ passed", replies[1])
        self.assertIn("Promoted `signal`", replies[2])
        # The same comments are never run twice.
        count = len(self.github.comments)
        self.service.cycle()
        self.assertEqual(len(self.github.comments), count)


if __name__ == "__main__":
    unittest.main()
