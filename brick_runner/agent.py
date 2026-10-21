"""The computer use agent: a model that clicks through a web app, as a person does.

evaluate.py pins the model with ``AGENT = {"model": ..., "terms": [...]}``. Its
observe(input, browser, base_url, agent=...) opens a page and calls
``agent.run(page, task)``; the evaluator (in the sandbox, with no network)
sends screenshots here, and the runner, outside the sandbox, asks the model
for the next actions with the computer use toolset. The evaluator does the
actions on the page with real input events.

The model only does the task. It never gives the score: score() decides from
facts, such as the requests the app sent.

Limits keep the cost known: steps per session, sessions per evaluation, calls
per evaluation. Only the newest screenshots stay in the conversation. Answers
are not cached: a new screenshot is a new request.

The defect check does not run agent cases (see engine.py), because each
mutant would pay for every session again.
"""

from __future__ import annotations

from typing import Any

from .judge import Judge, JudgeError

TOOLSET = {"type": "computer_toolset_20260801"}
TOOLSET_NAME = "computer"
MAX_TOKENS = 2048
MAX_STEPS_PER_SESSION = 40
MAX_SESSIONS_PER_EVALUATION = 120
MAX_CALLS_PER_EVALUATION = 2500
KEEP_IMAGES = 3
SYSTEM = ("You test a web app in a browser, for an automated evaluation. The app runs on test data. "
          "Do only the task you are given, in this page; do not open other sites. "
          "When the task is done or cannot be done, answer with no tool call: the first word is DONE or FAILED, "
          "then one short sentence that says what you saw.")


class AgentError(RuntimeError):
    pass


class Agents:
    """The model side of every agent session in one evaluation."""

    def __init__(self, judge: Judge, model: str) -> None:
        self.judge = judge
        self.model = model
        self.sessions: dict[str, dict[str, Any]] = {}
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def step(self, message: dict[str, Any]) -> dict[str, Any]:
        """One worker message -> the next actions, or the end of the session."""
        name = str(message.get("session", ""))
        session = self.sessions.get(name)
        if session is None:
            if len(self.sessions) >= MAX_SESSIONS_PER_EVALUATION:
                return {"error": f"limit: more than {MAX_SESSIONS_PER_EVALUATION} agent sessions in one evaluation"}
            task = message.get("task")
            if not isinstance(task, str) or not task.strip():
                return {"error": "an agent session starts with a task"}
            session = {"messages": [], "steps": 0, "pending": set()}
            self.sessions[name] = session
            first = _results_content(message.get("results") or [])
            session["messages"].append({"role": "user", "content": [{"type": "text", "text": task[:4000]}, *first]})
        else:
            results = message.get("results") or []
            ids = {str(r.get("id")) for r in results}
            if ids != session["pending"]:
                return {"error": "the results do not answer the last actions"}
            session["messages"].append({"role": "user", "content": [_tool_result(r) for r in results]})
        if session["steps"] >= MAX_STEPS_PER_SESSION:
            return {"error": f"limit: more than {MAX_STEPS_PER_SESSION} steps in one task"}
        if self.calls >= MAX_CALLS_PER_EVALUATION:
            return {"error": f"limit: more than {MAX_CALLS_PER_EVALUATION} agent calls in one evaluation"}
        _keep_newest_images(session["messages"], KEEP_IMAGES)
        body = {"model": self.model, "max_tokens": MAX_TOKENS, "system": SYSTEM, "tools": [TOOLSET],
                "messages": session["messages"]}
        try:
            answer = self.judge.post(body)
        except JudgeError as exc:
            raise AgentError(str(exc)) from None
        self.calls += 1
        session["steps"] += 1
        usage = answer.get("usage") or {}
        self.input_tokens += int(usage.get("input_tokens", 0) or 0)
        self.output_tokens += int(usage.get("output_tokens", 0) or 0)
        content = answer.get("content") or []
        session["messages"].append({"role": "assistant", "content": content})
        actions = [{"id": str(b.get("id")), "name": str(b.get("name")), "input": b.get("input") or {}}
                   for b in content if b.get("type") == "tool_use"]
        if actions:
            session["pending"] = {a["id"] for a in actions}
            return {"actions": actions}
        session["pending"] = set()
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
        return {"done": True, "text": text[:2000]}

    def usage(self) -> dict[str, int]:
        return {"agent_sessions": len(self.sessions), "agent_calls": self.calls,
                "agent_input_tokens": self.input_tokens, "agent_output_tokens": self.output_tokens}


def _image(data: str) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def _results_content(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks = []
    for result in results:
        if isinstance(result.get("image"), str):
            blocks.append(_image(result["image"]))
        if isinstance(result.get("text"), str):
            blocks.append({"type": "text", "text": result["text"][:2000]})
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
        blocks[i] = {"type": "text", "text": "[an earlier screenshot]"}
