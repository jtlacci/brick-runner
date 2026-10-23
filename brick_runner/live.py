"""Live model calls in an evaluation: the answer to an external call with no recording.

A brick that uses a model (a feed that writes posts from a prompt) changes its
requests whenever its prompt changes, so recorded answers stop matching. The
brick's policy.yml (human-only) can name externals that may be answered live:

    live_externals: [anthropic]

When a case calls ``external:anthropic`` and the case has no recording for
that exact request, the runner (outside the sandbox) sends the request to the
Anthropic Messages API with the judge key and gives the answer back. Each
answer is cached by request, so the same evaluation of the same code costs
nothing the second time, and two runs see the same answer.

The request must be a Messages API body: an allowed model
(BRICK_RUNNER_AGENT_MODELS), messages, and max_tokens of at most 8192. Other
fields are dropped. The key is never in the request: the runner adds it.

Limits: budget.max_model_calls and budget.max_model_tokens in policy.yml, per
run, below the runner's own. Over a limit, the run fails. An API outage
raises a transient error: the runner records nothing and tries again later.

``live_externals: [hermes]`` answers ``external:hermes`` with the Hermes agent
(hermes.py). Hermes's own model calls go through the same budget and cache.
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

from .agent import allowed_models
from .hashing import digest
from .judge import Judge, JudgeError

PROVIDERS = ("anthropic", "hermes")
FIELDS = ("model", "max_tokens", "system", "messages", "tools", "tool_choice", "temperature", "stop_sequences")
MAX_TOKENS = 8192
MAX_CALLS = 2000
MAX_MODEL_TOKENS = 20_000_000


class LiveError(RuntimeError):
    """A live call that cannot be answered.

    ``transient``: the API failed, not the candidate (try the evaluation again).
    ``fatal``: a budget is used up (the run fails). Neither: the candidate's
    request is wrong, so that case fails.
    """

    def __init__(self, message: str, transient: bool = False, fatal: bool = False) -> None:
        super().__init__(message)
        self.transient = transient
        self.fatal = fatal


class Live:
    def __init__(self, judge: Judge, names: list[str], cache: Path,
                 max_calls: int | None = None, max_tokens: int | None = None,
                 sandbox: Any = None, evaluate_py: Path | None = None) -> None:
        unknown = [n for n in names if n not in PROVIDERS]
        if unknown:
            raise LiveError(f"live_externals names an unknown provider: {', '.join(unknown)} "
                            f"(known: {', '.join(PROVIDERS)})")
        self.judge = judge
        self.names = set(names)
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)
        self.max_calls = min(MAX_CALLS, max_calls or MAX_CALLS)
        self.max_tokens = min(MAX_MODEL_TOKENS, max_tokens or MAX_MODEL_TOKENS)
        self.calls = 0
        self.cached = 0
        self.tokens = 0
        self.hermes = None
        self.evaluate_py = evaluate_py
        if "hermes" in self.names:
            if sandbox is None or evaluate_py is None:
                raise LiveError("live_externals: [hermes] needs the sandbox and the brick's evaluate.py")
            from .hermes import Hermes
            self.hermes = Hermes(sandbox, evaluate_py, self._relayed)
            self.relay_failure: LiveError | None = None
            self.hermes_answers = 0
            self.hermes_cached = 0

    def answer(self, name: str, request: Any, fixture: Any = None) -> dict[str, Any]:
        if name not in self.names:
            raise KeyError(name)
        if name == "hermes":
            return self._hermes(request, fixture)
        return self._messages(clean(request))

    def _hermes(self, request: Any, fixture: Any) -> dict[str, Any]:
        from .hermes import HermesError, cache_key
        if not isinstance(request, dict) or not isinstance(request.get("messages"), list) or not request["messages"]:
            raise LiveError("a Hermes request needs messages")
        profile = request.get("profile") if isinstance(request.get("profile"), dict) else {}
        if profile.get("model") not in allowed_models():
            raise LiveError(f"model {profile.get('model')!r} is not allowed on this runner")
        path = self.cache / f"hermes-{cache_key(request, fixture, self.evaluate_py)}.json"
        if path.exists():
            self.hermes_cached += 1
            return json.loads(path.read_text())["answer"]
        self.relay_failure = None
        try:
            answer = self.hermes.answer(request, fixture)
        except HermesError as exc:
            raise LiveError(f"hermes: {exc}", transient=exc.transient) from None
        if self.relay_failure is not None and (self.relay_failure.fatal or self.relay_failure.transient):
            raise self.relay_failure   # a budget or an outage stops the run, as for a direct model call
        if "error" in answer:
            raise LiveError(f"hermes: {answer['error']}")
        self.hermes_answers += 1
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"request": request, "fixture": fixture, "answer": answer, "time": time.time()}))
        tmp.replace(path)
        return answer

    def _relayed(self, body: Any) -> dict[str, Any]:
        """One of Hermes's model calls (from the relay)."""
        try:
            return self._messages(clean(body, clamp=True))
        except LiveError as exc:
            self.relay_failure = exc
            raise

    def close(self) -> None:
        if self.hermes is not None:
            self.hermes.close()

    def _messages(self, body: dict[str, Any]) -> dict[str, Any]:
        key = digest({"provider": "anthropic", "body": body})[7:]
        path = self.cache / f"{key}.json"
        if path.exists():
            self.cached += 1
            return json.loads(path.read_text())["answer"]
        if self.calls >= self.max_calls:
            raise LiveError(f"budget: more than {self.max_calls} live model calls in one run", fatal=True)
        if self.tokens >= self.max_tokens:
            raise LiveError(f"budget: more than {self.max_tokens} live model tokens in one run", fatal=True)
        self.calls += 1
        try:
            answer = self.judge.post(body)
        except JudgeError as exc:
            # A refused request (HTTP 400) is the candidate's; an outage is not.
            raise LiveError(str(exc), transient=exc.transient) from None
        usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
        # Cached prompt tokens count too: with prompt caching most input tokens are in these two fields.
        self.tokens += sum(int(usage.get(k, 0) or 0) for k in ("input_tokens", "output_tokens",
                                                              "cache_creation_input_tokens", "cache_read_input_tokens"))
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"request": body, "answer": answer, "time": time.time()}))
        tmp.replace(path)
        return answer

    def usage(self) -> dict[str, int]:
        out = {"model_calls": self.calls, "model_cached": self.cached, "model_tokens": self.tokens}
        if self.hermes is not None:
            out.update(hermes_answers=self.hermes_answers, hermes_cached=self.hermes_cached)
        return out


def clean(request: Any, clamp: bool = False) -> dict[str, Any]:
    """The request as the runner sends it, or LiveError (not transient: the candidate's request is wrong).
    ``clamp``: a max_tokens over the limit is lowered to it (Hermes asks for more than the runner allows)."""
    if not isinstance(request, dict):
        raise LiveError("a live model request must be a Messages API body (an object)")
    body = {k: request[k] for k in FIELDS if k in request}
    if body.get("model") not in allowed_models():
        raise LiveError(f"model {body.get('model')!r} is not allowed on this runner")
    if not isinstance(body.get("messages"), list) or not body["messages"]:
        raise LiveError("a live model request needs messages")
    tokens = body.get("max_tokens")
    if clamp and isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > MAX_TOKENS:
        body["max_tokens"] = tokens = MAX_TOKENS
    if not isinstance(tokens, int) or isinstance(tokens, bool) or not 1 <= tokens <= MAX_TOKENS:
        raise LiveError(f"max_tokens must be from 1 to {MAX_TOKENS}")
    return body
