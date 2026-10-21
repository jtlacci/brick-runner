"""The polling service: evaluate pull requests and post the results on GitHub.

The runner machine pulls work from GitHub; GitHub never pushes code to it. No
workflow file and no pull-request code runs on this machine outside the
sandbox. Each cycle:

1. Run new ``/brick holdout <brick>`` and ``/brick promote <brick>`` comments.
   A command runs only if its author is on the allow list, is not a bot, and
   the comment was never edited. A second holdout run needs a reason:
   ``/brick holdout <brick> rerun: <reason>``.
2. List open pull requests, then fetch the repository into a runner-owned
   bare clone (``<home>/repo.git``).
3. For every open pull request into the protected branch (default ``main``)
   from the same repository, evaluate it when its head or the branch tip
   changed: plan the change by the policy on the branch tip; a code-loop
   change runs ``submit`` on development and validation; an evaluation-loop
   change runs ``evaluation-change``; a change with both fails. Post a
   ``brick-runner`` commit status and one comment, updated in place. Other
   pull requests (forks, other bases) get no status at all.
4. Back up to the DVC remote, at most once per ``sync_every`` seconds.

Everything posted to GitHub is HTML-escaped and stripped of known secrets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import html
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
from .github import GitHub, GitHubError, redact
from .plan import make_plan
from .policy import PolicyError
from .sandbox import SandboxError
from .store import StoreError

MARKER = "<!-- brick-runner -->"
EXPECTED = (RunnerError, GitError, PolicyError, StoreError, SandboxError)
COMMENT_LIMIT = 60_000
OVERLAP = timedelta(minutes=5)   # re-read this much of the comment window each cycle (clock skew)
MAX_ATTEMPTS = 3                 # a runner error is retried this many times before it is final
SECRET_ENV = ("BRICK_RUNNER_GITHUB_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def log(event: str, **fields: Any) -> None:
    print(json.dumps({"time": iso(utcnow()), "event": event, **fields}, default=str), flush=True)


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
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.allowed = {login.casefold() for login in self.allowed}
        self.repo_path = self.home / "repo.git"
        self.state_path = self.home / "serve-state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.state.setdefault("pulls", {})
        self.state.setdefault("results", {})
        self.state.setdefault("handled_comments", [])
        self.state.setdefault("window", iso(utcnow() - OVERLAP))
        self.last_sync = -float("inf")
        self.dirty = False
        self.warned_backup = False
        self.backoff_until = 0.0

    def save(self) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2, sort_keys=True))
        os.replace(temporary, self.state_path)

    def secrets(self) -> list[str]:
        values = [os.environ.get(name, "") for name in SECRET_ENV]
        try:
            values.append(self.github.token())
        except GitHubError:
            pass
        return values

    def clean(self, text: str) -> str:
        return redact(text, self.secrets())

    # ------------------------------------------------------------ git

    def fetch(self) -> Repo:
        url = self.remote or self.github.clone_url()
        env = {**os.environ, **(self.github.git_env() if url.startswith("https://github.com/") else {})}
        if not (self.repo_path / "HEAD").exists():
            subprocess.run(["git", "init", "-q", "--bare", str(self.repo_path)], check=True, capture_output=True)
        refspecs = ["+refs/heads/*:refs/remotes/origin/*", "+refs/pull/*/head:refs/pull/*"]
        done = subprocess.run(["git", "fetch", "-q", "--prune", url, *refspecs], cwd=self.repo_path,
                              capture_output=True, text=True, env=env)
        if done.returncode:
            raise GitError(self.clean(f"fetch failed: {done.stderr.strip()[:300]}"))
        return Repo(self.repo_path)

    def has_commit(self, repo: Repo, sha: str) -> bool:
        return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo.path,
                              capture_output=True).returncode == 0

    def runner(self, base: str) -> Runner:
        return Runner(self.repo_path, self.home, base)

    # ------------------------------------------------------------ one cycle

    def cycle(self) -> None:
        if self.clock() < self.backoff_until:
            return
        try:
            self.handle_commands()
            pulls = self.github.open_pulls()
            repo = self.fetch()
            base = repo.commit(f"refs/remotes/origin/{self.branch}")
            for pull in pulls:
                self.handle_pull(repo, pull, base)
        except GitHubError as exc:
            log("github-error", error=self.clean(str(exc)), retry_after=exc.retry_after)
            if exc.retry_after:
                self.backoff_until = self.clock() + exc.retry_after
        self.maybe_sync()

    def maybe_sync(self) -> None:
        if not self.dirty or self.clock() - self.last_sync < self.sync_every:
            return
        if not backup.status(self.home).get("configured"):
            if not self.warned_backup:
                log("backup-not-configured", note="run `brick-runner remote --url s3://...` to back up")
                self.warned_backup = True
            return
        try:
            log("sync", **backup.sync(self.home))
            self.dirty = False
        except StoreError as exc:
            log("sync-error", error=self.clean(str(exc)))
        self.last_sync = self.clock()

    def run_forever(self, interval: float) -> None:
        while True:
            try:
                self.cycle()
            except Exception as exc:  # noqa: BLE001 -- the service must keep running
                log("cycle-error", error=self.clean(f"{type(exc).__name__}: {exc}"))
            time.sleep(interval)

    # ------------------------------------------------------------ pull requests

    def eligible(self, pull: dict[str, Any]) -> str | None:
        """Why a pull request is not evaluated, or None when it is."""
        source = ((pull.get("head") or {}).get("repo") or {}).get("full_name") or ""
        if source.casefold() != self.github.repository.casefold():
            return "from a fork"
        if pull["base"]["ref"] != self.branch:
            return f"not into {self.branch}"
        return None

    def handle_pull(self, repo: Repo, pull: dict[str, Any], base: str) -> None:
        number, head = pull["number"], pull["head"]["sha"]
        record = self.state["pulls"].setdefault(str(number), {})
        reason = self.eligible(pull)
        if reason:
            # No status at all: a status is per commit, and a success here could cover a
            # failure the same commit got in another pull request.
            if record.get("skipped") != head:
                log("skip", pull=number, sha=head, reason=reason)
                record["skipped"] = head
                self.save()
            return
        key = f"{head}:{base}"
        if record.get("key") == key:
            return
        if not self.has_commit(repo, head):
            log("not-fetched-yet", pull=number, sha=head)
            return
        result = self.state["results"].get(key)
        if result is None:
            attempts = record.get("attempts", {}).get(key, 0) + 1
            record.setdefault("attempts", {})[key] = attempts
            self.save()
            self.github.set_status(head, "pending", "evaluating")
            log("evaluate", pull=number, sha=head, base=base, attempt=attempts)
            try:
                state, summary, body = self.evaluate(repo, head, base)
            except EXPECTED as exc:
                state, summary, body = "error", f"runner error: {exc}", f"**Runner error:** {_code(str(exc))}"
            except Exception as exc:  # noqa: BLE001 -- report, never crash the service
                state, summary = "error", f"runner crashed: {type(exc).__name__}"
                body = f"**Runner crashed:**\n\n{_pre(traceback.format_exc(limit=6))}"
            body = self.clean(f"{MARKER}\n### brick-runner: <code>{head[:12]}</code> on <code>{base[:12]}</code>\n\n{body}")
            result = {"state": state, "summary": self.clean(summary)[:140], "body": body[:COMMENT_LIMIT]}
            if state != "error" or attempts >= MAX_ATTEMPTS:
                self.state["results"][key] = result   # saved before anything is posted
            self.save()
            self.dirty = True
        self.github.set_status(head, result["state"], result["summary"])
        if record.get("comment"):
            try:
                self.github.edit_comment(record["comment"], result["body"])
            except GitHubError:
                record["comment"] = self.github.post_comment(number, result["body"])
        else:
            record["comment"] = self.github.post_comment(number, result["body"])
        if key in self.state["results"]:
            record["key"] = key
        self.save()
        log("done", pull=number, sha=head, summary=result["summary"])

    def evaluate(self, repo: Repo, head: str, base: str) -> tuple[str, str, str]:
        plan = make_plan(repo, base, head)
        lines = []
        if plan.human:
            shown = ", ".join(_code(p) for p in plan.human[:20])
            lines.append(f"Human-only files changed (CODEOWNERS approval): {shown}" + (" …" if len(plan.human) > 20 else ""))
        if plan.new_bricks:
            lines.append("New bricks, evaluated after a human merges them: " + ", ".join(_code(b) for b in plan.new_bricks))
        if plan.removed_bricks:
            lines.append("Removed bricks (CODEOWNERS approval): " + ", ".join(_code(b) for b in plan.removed_bricks))
        if plan.mixed:
            lines.append("**Failed:** one change may contain code-loop files or evaluation-loop files, never both. "
                         f"Code: {', '.join(_code(b) for b in plan.code)}. "
                         f"Evaluation: {', '.join(_code(b) for b in plan.evaluation)}.")
            return "failure", "mixes code-loop and evaluation-loop files", "\n\n".join(lines)
        if not plan.code and not plan.evaluation:
            lines.append("No brick code or evaluation changed, so there is nothing to evaluate.")
            return "success", "nothing to evaluate", "\n\n".join(lines)
        runner = self.runner(base)
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
        summary = f"{'passed' if passed else 'failed'}: {', '.join(plan.code + plan.evaluation)}"
        return ("success" if passed else "failure"), summary, "\n\n".join(lines)

    # ------------------------------------------------------------ commands

    def handle_commands(self) -> None:
        window = parse(self.state["window"])
        handled = set(self.state["handled_comments"])
        newest = window
        for comment in self.github.comments_since(iso(window)):
            created = parse(comment["created_at"])
            newest = max(newest, created)
            if comment["id"] in handled or created < window:
                continue
            words = (comment.get("body") or "").strip().split(None, 3)
            if len(words) < 3 or words[0] != "/brick" or words[1] not in ("holdout", "promote"):
                continue
            # Persist first: a command never runs twice, even if the cycle fails after it.
            handled.add(comment["id"])
            self.state["handled_comments"] = sorted(handled)[-5000:]
            self.save()
            user = comment.get("user") or {}
            login = str(user.get("login", ""))
            if comment.get("updated_at") != comment.get("created_at"):
                log("command-refused", login=login, reason="the comment was edited")
                continue
            if login.casefold() not in self.allowed or user.get("type") != "User":
                log("command-refused", login=login, reason="not on the allow list")
                continue
            try:
                self.run_command(comment, login, words)
            except GitHubError:
                raise
            except Exception as exc:  # noqa: BLE001 -- one bad command must not stop the others
                log("command-error", error=self.clean(f"{type(exc).__name__}: {exc}"))
        self.state["window"] = iso(max(window, newest - OVERLAP))
        self.save()

    def run_command(self, comment: dict[str, Any], login: str, words: list[str]) -> None:
        number = int(str(comment["issue_url"]).rstrip("/").rsplit("/", 1)[1])
        action, brick = words[1], words[2]
        reason = words[3][len("rerun:"):].strip() if len(words) > 3 and words[3].startswith("rerun:") else None
        log("command", login=login, action=action, brick=brick)
        try:
            repo = self.fetch()
            runner = self.runner(repo.commit(f"refs/remotes/origin/{self.branch}"))
            result = runner.holdout(brick, reason) if action == "holdout" else runner.promote(brick)
            text = render_command(action, brick, result)
        except EXPECTED as exc:
            text = f"<code>/brick {_escape(action)} {_escape(brick)}</code> failed: {_code(str(exc))}"
        self.github.post_comment(number, self.clean(f"{MARKER}\n{text}\n\nRequested by {_escape(login)}."))
        self.dirty = True


# ------------------------------------------------------------ rendering (everything escaped)

_MARKDOWN = {ord(c): f"&#{ord(c)};" for c in "\\`*_{}[]()#+-.!|~:@<>&\"'"}


def _escape(text: Any) -> str:
    """Text that GitHub shows literally: no markdown, no HTML, no mentions, no emoji codes."""
    return str(text).translate(_MARKDOWN).replace("\r", " ").replace("\n", " ")


def _code(text: Any) -> str:
    return f"<code>{_escape(text)}</code>"


def _pre(text: Any) -> str:
    return "<pre>" + "\n".join(_escape(line) for line in str(text).splitlines()) + "</pre>"


def _number(value: Any) -> str:
    return f"{value:.4g}" if isinstance(value, (int, float)) else "–"


def _gates(result: dict[str, Any]) -> str:
    return ", ".join(f"{'✅' if g['passed'] else '❌'} {_escape(g['term'])} {_escape(g['operator'])} "
                     f"{_number(g['threshold'])} ({_number(g['value'])})" for g in result.get("gates", [])) or "no gates"


def _terms(terms: dict[str, Any]) -> str:
    return ", ".join(f"{_escape(k)} {_number(v)}" for k, v in sorted(terms.items())) or "–"


def _cost(result: dict[str, Any]) -> str:
    """What one run cost: cases, time and agent use."""
    counts = result.get("counts") or {}
    parts = [f"{_number(counts.get('cases', 0))} cases"]
    if isinstance(result.get("seconds"), (int, float)):
        parts.append(f"{result['seconds']:.0f} s")
    if counts.get("agent_calls"):
        tokens = counts.get("agent_input_tokens", 0) + counts.get("agent_output_tokens", 0)
        parts.append(f"{_number(counts['agent_calls'])} agent calls, {tokens / 1e6:.2f} M tokens")
    return ", ".join(parts)


def _delta(old: dict[str, Any], new: dict[str, Any]) -> str:
    def value(result, key):
        counts = result.get("counts") or {}
        if key == "seconds":
            return result.get("seconds")
        if key == "tokens":
            return counts.get("agent_input_tokens", 0) + counts.get("agent_output_tokens", 0)
        return counts.get(key, 0)
    parts = []
    for key, unit in (("cases", " cases"), ("seconds", " s"), ("agent_calls", " agent calls"), ("tokens", " tokens")):
        a, b = value(old, key), value(new, key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and round(b - a):
            parts.append(f"{b - a:+.0f}{unit}")
    return ", ".join(parts) or "no change"


def render_code(brick: str, development: dict[str, Any], validation: dict[str, Any]) -> str:
    rows = ["| Split | Result | Terms | Gates | Cases | Out of evidence |", "| --- | --- | --- | --- | --- | --- |"]
    for name, result in (("development", development), ("validation", validation)):
        counts = result.get("counts", {})
        rows.append(f"| {name} | {'✅ passed' if result.get('passed') else '❌ failed'} | {_terms(result.get('terms', {}))} | "
                    f"{_gates(result)} | {_number(counts.get('scored', 0))}/{_number(counts.get('cases', 0))} | "
                    f"{_number(counts.get('out_of_evidence', 0))} |")
    parts = [f"#### {_code(brick)} (code loop)", "\n".join(rows),
             f"Cost: development {_escape(_cost(development))}; validation {_escape(_cost(validation))}."]
    reasons = list(dict.fromkeys(development.get("reasons", []) + validation.get("reasons", [])))
    if reasons:
        parts.append("Reasons: " + "; ".join(_escape(r) for r in reasons))
    examples = development.get("counterexamples", [])[:5]
    if examples:
        parts.append("<details><summary>Development counterexamples</summary>\n\n"
                     + _pre(json.dumps(examples, indent=1, default=str)[:6000]) + "\n\n</details>")
    if development.get("log"):
        parts.append("<details><summary>Candidate log (development)</summary>\n\n"
                     + _pre(development["log"][-2000:]) + "\n\n</details>")
    return "\n\n".join(parts)


def render_evaluation(brick: str, change: dict[str, Any]) -> str:
    check = change["defect_check"]
    rows = ["| Defect strength | Median score | Detected |", "| --- | --- | --- |"]
    for strength, item in sorted(check.get("strengths", {}).items(), key=lambda kv: int(kv[0])):
        rows.append(f"| {_escape(strength)} | {_number(item['median'])} | {_number(item['detected'])}/{_number(item['of'])} |")
    old, new = change["production_under_old"], change["production_under_new"]
    cases = check.get("cases") or {}
    parts = [
        f"#### {_code(brick)} (evaluation loop)",
        f"Defect check: {'✅ passed' if check.get('passed') else '❌ failed'}: {_escape(check.get('reason'))}. "
        f"Baseline {_escape(check.get('metric'))}: {_number(check.get('baseline'))}.",
        "\n".join(rows)]
    if cases:
        parts.append(f"Cases that catch a mutant: {_number(cases.get('caught'))} of {_number(cases.get('scored'))} "
                     f"(development, {_number(cases.get('mutants'))} mutants).")
    parts += [
        f"Production under the current evaluation: {'passed' if old.get('passed') else 'failed'} "
        f"({_terms(old.get('terms') or {})}). Under this change: {'passed' if new.get('passed') else 'failed'} "
        f"({_terms(new.get('terms') or {})}).",
        f"Cost of one development run: before {_escape(_cost(old))}; after {_escape(_cost(new))} "
        f"({_escape(_delta(old, new))})."]
    if change.get("bloat"):
        parts.append("⚠️ Bloat: " + "; ".join(_escape(n) for n in change["bloat"]) + ".")
    parts.append(f"Next: {_escape(change['next'])}.")
    return "\n\n".join(parts)


def render_command(action: str, brick: str, result: dict[str, Any]) -> str:
    name = _code(brick)
    if action == "holdout":
        if result.get("status") == "waiting":
            return f"Holdout for {name} is waiting for data: {_number(result.get('have'))} of {_number(result.get('need'))} cases."
        if result.get("note"):
            return f"Holdout for {name}: {_escape(result['note'])}."
        return f"Holdout for {name} at {_code(str(result.get('candidate', ''))[:12])}: " \
               f"{'✅ passed' if result.get('passed') else '❌ failed'}."
    return (f"Promoted {name} at {_code(result['commit'][:12])}. Bundle {_code(result['bundle'][:19])}"
            + (" (synthetic holdout: collect real data before you rely on it)" if result.get("synthetic") else "") + ".")
