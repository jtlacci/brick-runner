"""The computer use agent: a model that clicks through a web app, as a person does.

evaluate.py pins the model with ``AGENT = {"model": ..., "terms": [...]}``. Its
observe(input, browser, base_url, agent=...) opens a page and calls
``agent.run(page, task)``; the evaluator (in the sandbox, with no network)
sends screenshots here, and the runner, outside the sandbox, asks the model
for the next actions with the computer use toolset. The evaluator does the
actions on the page with real input events.

The model only does the task. It never gives the score: score() decides from
facts, such as the requests the app sent.

Limits keep the cost known: steps per session, and sessions, calls and tokens
per evaluation; only the models in BRICK_RUNNER_AGENT_MODELS. Only the newest
screenshots stay in the conversation. Answers are not cached: a new
screenshot is a new request.

The defect check does not run agent cases (see engine.py), because each
mutant would pay for every session again.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from .judge import Judge, JudgeError

# Zoom is off: a crop of the page has no more detail than the screenshot.
TOOLSET = {"type": "computer_toolset_20260801", "configs": {"zoom": {"enabled": False}}}
TOOLSET_NAME = "computer"
MAX_TOKENS = 2048
MAX_STEPS_PER_SESSION = 40
MAX_SESSIONS_PER_EVALUATION = 120
MAX_CALLS_PER_EVALUATION = 2500
MAX_INPUT_TOKENS_PER_EVALUATION = 20_000_000
MAX_OUTPUT_TOKENS_PER_EVALUATION = 1_000_000
MAX_TEXT = 2000
MAX_IMAGE = 8_000_000           # base64 characters
KEEP_IMAGES = 5
REMOVED = "[An older screenshot was removed to save space. What it showed is in your earlier notes.]"
DEFAULT_MODELS = "claude-sonnet-5-5,claude-opus-5-5,claude-haiku-5-5"
SYSTEM = ("You test a web app in a browser, for an automated evaluation. The app runs on test data. "
          "Do only the task you are given, in this page; do not open other sites. "
          "When the task is done or cannot be done, answer with no tool call: the first word is DONE or FAILED, "
          "then one short sentence that says what you saw.")


class AgentError(RuntimeError):
    """The evaluation cannot go on. ``transient``: the API failed; try the evaluation again later."""

    def __init__(self, message: str, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


def allowed_models() -> set[str]:
    return {m.strip() for m in os.environ.get("BRICK_RUNNER_AGENT_MODELS", DEFAULT_MODELS).split(",") if m.strip()}


class Agents:
    """The model side of every agent session in one evaluation.

    A limit on one session ends that session (its case fails). A limit on the
    whole evaluation ends the evaluation: a case that never ran must not pass.
    """

    def __init__(self, judge: Judge, model: str, max_calls: int | None = None, max_tokens: int | None = None) -> None:
        if model not in allowed_models():
            raise AgentError(f"AGENT model {model!r} is not allowed on this runner "
                             f"(BRICK_RUNNER_AGENT_MODELS: {', '.join(sorted(allowed_models()))})")
        self.judge = judge
        self.model = model
        # The brick's budget may be lower than the runner's own limits, never higher.
        self.max_calls = min(MAX_CALLS_PER_EVALUATION, max_calls or MAX_CALLS_PER_EVALUATION)
        self.max_tokens = min(MAX_INPUT_TOKENS_PER_EVALUATION, max_tokens or MAX_INPUT_TOKENS_PER_EVALUATION)
        self.sessions: dict[str, dict[str, Any]] = {}
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        # Parallel workers call step() from their own threads. The lock keeps the
        # session table and the evaluation-wide counters right; one session is
        # only ever used by one worker.
        self._lock = threading.Lock()

    def step(self, message: dict[str, Any]) -> dict[str, Any]:
        """One worker message -> the next actions, or the end of the session."""
        results = message.get("results")
        if not isinstance(results, list) or not all(_valid_result(r) for r in results):
            return {"error": "results must be a list of {id?, image?, text?, is_error?} of limited size"}
        name = message.get("session")
        if not isinstance(name, str) or not name or len(name) > 64:
            return {"error": "a session needs a name"}
        with self._lock:
            session = self.sessions.get(name)
            if session is None:
                task = message.get("task")
                if not isinstance(task, str) or not task.strip() or len(task) > 4000:
                    return {"error": "an agent session starts with a task"}
                if len(results) != 1 or not isinstance(results[0].get("image"), str):
                    return {"error": "an agent session starts with one screenshot"}
                if len(self.sessions) >= MAX_SESSIONS_PER_EVALUATION:
                    raise AgentError(f"more than {MAX_SESSIONS_PER_EVALUATION} agent sessions in one evaluation")
                session = {"messages": [], "steps": 0, "pending": set(), "closed": False, "new": True}
                self.sessions[name] = session
        if session.pop("new", False):
            session["messages"].append({"role": "user", "content": [{"type": "text", "text": task},
                                                                     _image(results[0]["image"])]})
        else:
            if session["closed"]:
                return {"error": "this session has ended"}
            ids = [str(r.get("id")) for r in results]
            if not ids or set(ids) != session["pending"] or len(ids) != len(session["pending"]):
                return self._close(session, "the results do not answer the last actions")
            session["messages"].append({"role": "user", "content": [_tool_result(r) for r in results]})
        if session["steps"] >= MAX_STEPS_PER_SESSION:
            return self._close(session, f"limit: more than {MAX_STEPS_PER_SESSION} steps in one task")
        with self._lock:
            if self.calls >= self.max_calls:
                raise AgentError(f"budget: more than {self.max_calls} agent calls in one evaluation")
            if self.input_tokens + self.output_tokens >= self.max_tokens or self.output_tokens >= MAX_OUTPUT_TOKENS_PER_EVALUATION:
                raise AgentError(f"budget: more than {self.max_tokens} agent tokens in one evaluation")
            self.calls += 1   # counted before the call, so parallel workers cannot pass the limit
        _keep_newest_images(session["messages"], KEEP_IMAGES)
        body = {"model": self.model, "max_tokens": MAX_TOKENS, "system": SYSTEM, "tools": [TOOLSET],
                "messages": session["messages"]}
        try:
            answer = self.judge.post(body)
        except JudgeError as exc:
            raise AgentError(str(exc), transient=exc.transient) from None
        session["steps"] += 1
        usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
        with self._lock:
            self.input_tokens += int(usage.get("input_tokens", 0) or 0)
            self.output_tokens += int(usage.get("output_tokens", 0) or 0)
        content = answer.get("content") if isinstance(answer.get("content"), list) else []
        stop = answer.get("stop_reason")
        if stop not in (None, "tool_use", "end_turn", "stop_sequence"):
            # max_tokens (a cut tool call), refusal, a full context: the session cannot go on.
            return self._close(session, f"the model stopped: {stop}")
        session["messages"].append({"role": "assistant", "content": content})
        actions = [{"id": str(b.get("id")), "name": str(b.get("name")), "input": b.get("input") or {}}
                   for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text").strip()
        if actions:
            session["pending"] = {a["id"] for a in actions}
            # What the model said with its actions: why a long run went on is then in the result.
            return {"actions": actions, "note": text[:300]} if text else {"actions": actions}
        self._close(session, "")
        return {"done": True, "text": text[:MAX_TEXT]}

    @staticmethod
    def _close(session: dict[str, Any], reason: str) -> dict[str, Any]:
        session["closed"] = True
        session["pending"] = set()
        return {"error": reason} if reason else {}

    def usage(self) -> dict[str, int]:
        return {"agent_sessions": len(self.sessions), "agent_calls": self.calls,
                "agent_input_tokens": self.input_tokens, "agent_output_tokens": self.output_tokens}


def _valid_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    image, text = result.get("image"), result.get("text")
    return ((image is None or (isinstance(image, str) and len(image) <= MAX_IMAGE))
            and (text is None or (isinstance(text, str) and len(text) <= MAX_TEXT))
            and isinstance(result.get("is_error", False), bool))


def _image(data: str) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def _results_content(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks = []
    for result in results:
        if isinstance(result.get("image"), str):
            blocks.append(_image(result["image"]))
        if isinstance(result.get("text"), str) and result["text"]:
            blocks.append({"type": "text", "text": result["text"][:MAX_TEXT]})
    return blocks


def _tool_result(result: dict[str, Any]) -> dict[str, Any]:
    content = _results_content([result]) or [{"type": "text", "text": "OK"}]
    block = {"type": "tool_result", "tool_use_id": str(result.get("id")), "toolset_name": TOOLSET_NAME,
             "content": content}
    if result.get("is_error"):
        block["is_error"] = True
    return block


def _keep_newest_images(messages: list[dict[str, Any]], keep: int) -> None:
    """Replace all but the newest ``keep`` screenshots with a short note, so the cost does not grow."""
    lists = []   # every content list that can hold an image, oldest first
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            lists.append(content)
            lists.extend(b["content"] for b in content
                         if b.get("type") == "tool_result" and isinstance(b.get("content"), list))
    places = [(blocks, i) for blocks in lists for i, b in enumerate(blocks) if b.get("type") == "image"]
    for blocks, i in places[:max(0, len(places) - keep)]:
        blocks[i] = {"type": "text", "text": REMOVED}
