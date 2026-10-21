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
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

from .agent import allowed_models
from .hashing import digest
from .judge import Judge, JudgeError

PROVIDERS = ("anthropic",)
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
                 max_calls: int | None = None, max_tokens: int | None = None) -> None:
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

    def answer(self, name: str, request: Any) -> dict[str, Any]:
        if name not in self.names:
            raise KeyError(name)
        body = clean(request)
        key = digest({"provider": name, "body": body})[7:]
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
        self.tokens += int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"request": body, "answer": answer, "time": time.time()}))
        tmp.replace(path)
        return answer

    def usage(self) -> dict[str, int]:
        return {"model_calls": self.calls, "model_cached": self.cached, "model_tokens": self.tokens}


def clean(request: Any) -> dict[str, Any]:
    """The request as the runner sends it, or LiveError (not transient: the candidate's request is wrong)."""
    if not isinstance(request, dict):
        raise LiveError("a live model request must be a Messages API body (an object)")
    body = {k: request[k] for k in FIELDS if k in request}
    if body.get("model") not in allowed_models():
        raise LiveError(f"model {body.get('model')!r} is not allowed on this runner")
    if not isinstance(body.get("messages"), list) or not body["messages"]:
        raise LiveError("a live model request needs messages")
    tokens = body.get("max_tokens")
    if not isinstance(tokens, int) or isinstance(tokens, bool) or not 1 <= tokens <= MAX_TOKENS:
        raise LiveError(f"max_tokens must be from 1 to {MAX_TOKENS}")
    return body
