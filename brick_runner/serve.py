"""The polling service: evaluate pull requests and post the results on GitHub.

The runner machine pulls work from GitHub; GitHub never pushes code to it. No
workflow file and no pull-request code runs on this machine outside the
sandbox. Each cycle:

1. Fetch the repository into a runner-owned bare clone (``<home>/repo.git``).
2. For every open pull request into the protected branch (default ``main``)
   from the same repository, whose head commit is new:
   - plan the change with the policy on the base;
   - code-loop change: ``submit`` on development and validation;
   - evaluation-loop change: ``evaluation-change``;
   - a change with both: fail, as the framework rule says;
   - post a commit status (``brick-runner``) and one comment, updated in place.
3. Run ``/brick holdout <brick>`` and ``/brick promote <brick>`` comments, only
   from GitHub logins on the allow list. A second holdout run needs a reason:
   ``/brick holdout <brick> rerun: <reason>``.
4. Back up to the DVC remote, at most once per ``sync_every`` seconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
import traceback
from typing import Any, Callable

from . import backup
from .engine import Runner, RunnerError
from .gitrepo import GitError, Repo
from .github import GitHub, GitHubError
from .plan import make_plan
from .policy import PolicyError
from .sandbox import SandboxError
from .store import StoreError

MARKER = "<!-- brick-runner -->"
EXPECTED = (RunnerError, GitError, PolicyError, StoreError, SandboxError)
COMMENT_LIMIT = 60_000


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(event: str, **fields: Any) -> None:
    print(json.dumps({"time": now(), "event": event, **fields}, default=str), flush=True)


@dataclass
class Service:
    github: GitHub
    home: Path
    allowed: set[str] = field(default_factory=set)
    branch: str = "main"
    sync_every: float = 600.0
    remote: str | None = None  # where to fetch from; default: the GitHub clone URL
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        self.home = Path(self.home).resolve()
        self.repo_path = self.home / "repo.git"
        self.state_path = self.home / "serve-state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {
            "pulls": {}, "comments_since": now(), "handled_comments": []}
        self.last_sync = -float("inf")
        self.dirty = False

    def save(self) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2, sort_keys=True))
        os.replace(temporary, self.state_path)

    # ------------------------------------------------------------ git

    def fetch(self) -> Repo:
        url = self.remote or self.github.clone_url()
        auth = self.github.git_auth() if url.startswith("https://github.com/") else []
        if not (self.repo_path / "HEAD").exists():
            subprocess.run(["git", "init", "-q", "--bare", str(self.repo_path)], check=True, capture_output=True)
        refspecs = ["+refs/heads/*:refs/remotes/origin/*", "+refs/pull/*/head:refs/pull/*"]
        done = subprocess.run(["git", *auth, "fetch", "-q", "--prune", url, *refspecs], cwd=self.repo_path,
                              capture_output=True, text=True)
        if done.returncode:
            raise GitError(f"fetch failed: {done.stderr.strip()[:300]}")
        return Repo(self.repo_path)

    def runner(self) -> Runner:
        return Runner(self.repo_path, self.home, f"refs/remotes/origin/{self.branch}")

    # ------------------------------------------------------------ one cycle

    def cycle(self) -> None:
        repo = self.fetch()
        for pull in self.github.open_pulls():
            try:
                self.handle_pull(repo, pull)
            except GitHubError as exc:
                log("github-error", pull=pull.get("number"), error=str(exc))
        self.handle_commands()
        if self.dirty and self.clock() - self.last_sync >= self.sync_every and backup.status(self.home).get("configured"):
            try:
                log("sync", **backup.sync(self.home))
                self.dirty = False
            except StoreError as exc:
                log("sync-error", error=str(exc))
            self.last_sync = self.clock()

    def run_forever(self, interval: float) -> None:
        while True:
            try:
                self.cycle()
            except EXPECTED + (GitHubError, OSError) as exc:
                log("cycle-error", error=str(exc))
            time.sleep(interval)

    # ------------------------------------------------------------ pull requests

    def handle_pull(self, repo: Repo, pull: dict[str, Any]) -> None:
        number, head = pull["number"], pull["head"]["sha"]
        record = self.state["pulls"].get(str(number), {})
        if record.get("sha") == head:
            return
        if (pull["head"].get("repo") or {}).get("full_name") != self.github.repository:
            self._finish(number, head, record, None, "not evaluated: pull requests from forks are not evaluated")
            return
        if pull["base"]["ref"] != self.branch:
            self._finish(number, head, record, None, f"not evaluated: the base is not {self.branch}")
            return
        self.github.set_status(head, "pending", "evaluating")
        log("evaluate", pull=number, sha=head)
        try:
            state, summary, body = self.evaluate(repo, head)
        except EXPECTED as exc:
            state, summary, body = "error", f"runner error: {exc}"[:140], f"**Runner error:** `{exc}`"
        except Exception as exc:  # noqa: BLE001 -- report, never crash the service
            state, summary = "error", f"runner crashed: {type(exc).__name__}"
            body = f"**Runner crashed:**\n```\n{_fence(traceback.format_exc(limit=6))}\n```"
        self.github.set_status(head, state, summary)
        self._finish(number, head, record, f"{MARKER}\n### brick-runner: `{head[:12]}`\n\n{body}"[:COMMENT_LIMIT], summary)
        self.dirty = True

    def _finish(self, number: int, head: str, record: dict[str, Any], body: str | None, summary: str) -> None:
        if body is None:
            self.github.set_status(head, "success", summary)
        else:
            if record.get("comment"):
                try:
                    self.github.edit_comment(record["comment"], body)
                except GitHubError:
                    record["comment"] = self.github.post_comment(number, body)
            else:
                record["comment"] = self.github.post_comment(number, body)
        record["sha"] = head
        record["summary"] = summary
        self.state["pulls"][str(number)] = record
        self.save()
        log("done", pull=number, sha=head, summary=summary)

    def evaluate(self, repo: Repo, head: str) -> tuple[str, str, str]:
        base = repo.commit(f"refs/remotes/origin/{self.branch}")
        plan = make_plan(repo, base, head)
        lines = []
        if plan.human:
            lines.append(f"Human-only files changed (CODEOWNERS approval): {', '.join(f'`{p}`' for p in plan.human[:20])}"
                         + (" …" if len(plan.human) > 20 else ""))
        if plan.new_bricks:
            lines.append(f"New bricks, evaluated after a human merges them: {', '.join(plan.new_bricks)}")
        if plan.mixed:
            lines.append("**Failed:** one change may contain code-loop files or evaluation-loop files, never both. "
                         f"Code: {', '.join(plan.code)}. Evaluation: {', '.join(plan.evaluation)}.")
            return "failure", "mixes code-loop and evaluation-loop files", "\n\n".join(lines)
        if not plan.code and not plan.evaluation:
            lines.append("No brick code or evaluation changed, so there is nothing to evaluate.")
            return "success", "nothing to evaluate", "\n\n".join(lines)
        runner = self.runner()
        passed = True
        for brick in plan.code:
            development = runner.submit(brick, head, "development")
            validation = runner.submit(brick, head, "validation")
            passed &= bool(development.get("passed")) and bool(validation.get("passed"))
            lines.append(render_code(brick, development, validation))
        for brick in plan.evaluation:
            change = runner.evaluation_change(brick, head)
            passed &= bool(change["defect_check"].get("passed"))
            lines.append(render_evaluation(brick, change))
        names = plan.code + plan.evaluation
        summary = f"{'passed' if passed else 'failed'}: {', '.join(names)}"
        return ("success" if passed else "failure"), summary, "\n\n".join(lines)

    # ------------------------------------------------------------ commands

    def handle_commands(self) -> None:
        since = self.state.get("comments_since", now())
        started = now()
        handled = set(self.state.get("handled_comments", []))
        for comment in self.github.comments_since(since):
            if comment["id"] in handled:
                continue
            handled.add(comment["id"])
            words = (comment.get("body") or "").strip().split(None, 3)
            if len(words) < 3 or words[0] != "/brick" or words[1] not in ("holdout", "promote"):
                continue
            login = (comment.get("user") or {}).get("login", "")
            number = int(comment["issue_url"].rstrip("/").rsplit("/", 1)[1])
            if login not in self.allowed or (comment.get("user") or {}).get("type") == "Bot":
                log("command-refused", login=login, command=words[1])
                self.github.post_comment(number, f"{MARKER}\n@{login} is not allowed to run `/brick {words[1]}`.")
                continue
            action, brick = words[1], words[2]
            reason = words[3][len("rerun:"):].strip() if len(words) > 3 and words[3].startswith("rerun:") else None
            log("command", login=login, action=action, brick=brick)
            try:
                self.fetch()
                runner = self.runner()
                result = runner.holdout(brick, reason) if action == "holdout" else runner.promote(brick)
                text = render_command(action, brick, result)
            except EXPECTED as exc:
                text = f"`/brick {action} {brick}` failed: `{exc}`"
            self.github.post_comment(number, f"{MARKER}\n{text}\n\nRequested by @{login}.")
            self.dirty = True
        self.state["handled_comments"] = sorted(handled)[-2000:]
        self.state["comments_since"] = started
        self.save()


# ------------------------------------------------------------ rendering

def _fence(text: str) -> str:
    return text.replace("```", "`​``")


def _number(value: Any) -> str:
    return f"{value:.4g}" if isinstance(value, (int, float)) else "–"


def _gates(result: dict[str, Any]) -> str:
    return ", ".join(f"{'✅' if g['passed'] else '❌'} {g['term']} {g['operator']} {g['threshold']} "
                     f"({_number(g['value'])})" for g in result.get("gates", [])) or "no gates"


def render_code(brick: str, development: dict[str, Any], validation: dict[str, Any]) -> str:
    rows = ["| Split | Result | Terms | Gates | Cases | Out of evidence |", "| --- | --- | --- | --- | --- | --- |"]
    for name, result in (("development", development), ("validation", validation)):
        counts = result.get("counts", {})
        terms = ", ".join(f"{k} {_number(v)}" for k, v in sorted(result.get("terms", {}).items()))
        rows.append(f"| {name} | {'✅ passed' if result.get('passed') else '❌ failed'} | {terms or '–'} | "
                    f"{_gates(result)} | {counts.get('scored', 0)}/{counts.get('cases', 0)} | "
                    f"{counts.get('out_of_evidence', 0)} |")
    parts = [f"#### `{brick}` (code loop)", "\n".join(rows)]
    reasons = list(dict.fromkeys(development.get("reasons", []) + validation.get("reasons", [])))
    if reasons:
        parts.append("Reasons: " + "; ".join(reasons))
    examples = development.get("counterexamples", [])[:5]
    if examples:
        parts.append("<details><summary>Development counterexamples</summary>\n\n```json\n"
                     + _fence(json.dumps(examples, indent=1, default=str)[:6000]) + "\n```\n</details>")
    if development.get("log"):
        parts.append("<details><summary>Candidate log (development)</summary>\n\n```\n"
                     + _fence(development["log"][-2000:]) + "\n```\n</details>")
    return "\n\n".join(parts)


def render_evaluation(brick: str, change: dict[str, Any]) -> str:
    check = change["defect_check"]
    rows = ["| Defect strength | Median score | Detected |", "| --- | --- | --- |"]
    for strength, item in sorted(check.get("strengths", {}).items(), key=lambda kv: int(kv[0])):
        rows.append(f"| {strength} | {_number(item['median'])} | {item['detected']}/{item['of']} |")
    old, new = change["production_under_old"], change["production_under_new"]
    return "\n\n".join([
        f"#### `{brick}` (evaluation loop)",
        f"Defect check: {'✅ passed' if check.get('passed') else '❌ failed'}: {check.get('reason')}. "
        f"Baseline {check.get('metric')}: {_number(check.get('baseline'))}.",
        "\n".join(rows),
        f"Production under the current evaluation: {'passed' if old.get('passed') else 'failed'} "
        f"({', '.join(f'{k} {_number(v)}' for k, v in sorted((old.get('terms') or {}).items()))}). "
        f"Under this change: {'passed' if new.get('passed') else 'failed'} "
        f"({', '.join(f'{k} {_number(v)}' for k, v in sorted((new.get('terms') or {}).items()))}).",
        f"Next: {change['next']}."])


def render_command(action: str, brick: str, result: dict[str, Any]) -> str:
    if action == "holdout":
        if result.get("status") == "waiting":
            return f"Holdout for `{brick}` is waiting for data: {result.get('have')} of {result.get('need')} cases."
        if result.get("note"):
            return f"Holdout for `{brick}`: {result['note']}."
        return (f"Holdout for `{brick}` at `{result.get('candidate', '')[:12]}`: "
                f"{'✅ passed' if result.get('passed') else '❌ failed'}.")
    return (f"Promoted `{brick}` at `{result['commit'][:12]}`. Bundle `{result['bundle'][:19]}`"
            + (" (synthetic holdout: collect real data before you rely on it)" if result.get("synthetic") else "") + ".")
