"""The model judge: the runner, outside the sandbox, asks a pinned model for a judgement.

evaluate.py pins the model (``JUDGE = {"model": ...}``) and builds each request
(``judge_requests(record, output)``); a request is ``{"system"?, "messages" |
"prompt", "max_tokens"?}``. The runner sends it to the Anthropic Messages API
and gives the text answers to ``score(..., judgments=[...])``.

Every answer is cached by a hash of (evaluation version, model, request), so the same evaluation on
the same evidence gets the same judgements, and a rerun costs nothing. A judge
answer is nondeterministic, so the cache keeps it in full.

The API key is read from the file in BRICK_RUNNER_JUDGE_KEY (a systemd
credential). BRICK_RUNNER_JUDGE_URL overrides the endpoint (for tests).
"""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import time
from typing import Any
import urllib.error
import urllib.request

from .hashing import digest

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 1024
MAX_TOKENS_LIMIT = 8192
MAX_REQUESTS_PER_CASE = 8
MAX_REQUESTS_PER_EVALUATION = 500
ATTEMPTS = 4


class JudgeError(RuntimeError):
    """``transient``: the API failed (overload, network), not the evaluation; try again later."""

    def __init__(self, message: str, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


def normalise(request: dict[str, Any]) -> dict[str, Any]:
    """The request as sent: only the fields the runner allows, with defaults."""
    if "messages" in request:
        messages = request["messages"]
    elif "prompt" in request:
        messages = [{"role": "user", "content": str(request["prompt"])}]
    else:
        raise JudgeError("a judge request needs 'messages' or 'prompt'")
    if not isinstance(messages, list) or not messages:
        raise JudgeError("'messages' must be a non-empty list")
    clean = {"messages": [{"role": str(m["role"]), "content": m["content"]} for m in messages],
             "max_tokens": min(int(request.get("max_tokens", DEFAULT_MAX_TOKENS)), MAX_TOKENS_LIMIT)}
    if request.get("system"):
        clean["system"] = str(request["system"])
    return clean


class Judge:
    def __init__(self, cache: Path) -> None:
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)
        self.calls = 0
        self.hits = 0

    def _key(self) -> str:
        path = os.environ.get("BRICK_RUNNER_JUDGE_KEY", "")
        if not path or not Path(path).is_file():
            raise JudgeError("this evaluation needs the model judge, and the runner has no judge key "
                             "(set BRICK_RUNNER_JUDGE_KEY to the key file)")
        key = Path(path).read_text().strip()
        if not key:
            raise JudgeError("this evaluation needs the model judge, and the judge key file is empty "
                             "(put the key in SSM and restart the runner)")
        return key

    def ask(self, model: str, request: dict[str, Any], scope: str = "") -> str:
        """``scope`` (the evaluation's hash) keeps answers from one evaluation version out of another."""
        body = {"model": model, **normalise(request)}
        name = digest({"scope": scope, "body": body})[7:]
        cached = self.cache / f"{name}.json"
        if cached.exists():
            self.hits += 1
            return json.loads(cached.read_text())["text"]
        text = self._post(body)
        self.calls += 1
        tmp = cached.with_suffix(".tmp")
        tmp.write_text(json.dumps({"request": body, "text": text, "time": time.time()}))
        tmp.replace(cached)
        return text

    def _post(self, body: dict[str, Any]) -> str:
        answer = self.post(body)
        return "".join(block.get("text", "") for block in answer.get("content", []) if block.get("type") == "text")

    def post(self, body: dict[str, Any]) -> dict[str, Any]:
        """One Messages API call; the whole answer. Retries on overload."""
        url = os.environ.get("BRICK_RUNNER_JUDGE_URL", API_URL)
        data = json.dumps(body).encode()
        headers = {"content-type": "application/json", "anthropic-version": API_VERSION, "x-api-key": self._key()}
        delay = 2.0
        for attempt in range(ATTEMPTS):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=180) as r:
                    answer = json.load(r)
                if not isinstance(answer, dict):
                    raise ValueError("the answer is not an object")
                return answer
            except urllib.error.HTTPError as exc:
                retry = exc.code in (429, 500, 502, 503, 529)
                if not retry or attempt == ATTEMPTS - 1:
                    # The key is in a header, never in the URL or the body, so the message is safe.
                    raise JudgeError(f"the judge API answered HTTP {exc.code}", transient=retry) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, ValueError) as exc:
                if attempt == ATTEMPTS - 1:
                    raise JudgeError(f"the judge API cannot be reached: {exc.__class__.__name__}", transient=True) from None
            time.sleep(delay)
            delay *= 2
        raise JudgeError("the judge API failed", transient=True)
