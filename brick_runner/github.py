"""A small GitHub REST client, standard library only (plus openssl for GitHub App signing).

Run the runner as its own **GitHub App**, installed on the brick repository
only, with: Contents read, Pull requests read, Commit statuses read and
write, Issues read and write. Then pin the required ``brick-runner`` check to
that App in branch protection, so no other identity can set a passing status.
A personal access token also works, for trials.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"


class GitHubError(RuntimeError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass
class AppAuth:
    """Installation tokens for a GitHub App, refreshed before they expire."""

    app_id: str
    key_path: Path
    installation_id: str
    api: str = API
    _token: str = field(default="", repr=False)
    _expires: float = 0.0

    def jwt(self) -> str:
        now = int(time.time())
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        payload = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)}).encode())
        signing_input = f"{header}.{payload}".encode()
        with tempfile.NamedTemporaryFile() as data:
            data.write(signing_input)
            data.flush()
            signed = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(self.key_path), data.name],
                                    capture_output=True, check=False)
        if signed.returncode:
            raise GitHubError("could not sign the GitHub App token with openssl")
        return f"{header}.{payload}.{_b64url(signed.stdout)}"

    def __call__(self) -> str:
        if self._token and time.time() < self._expires - 300:
            return self._token
        request = urllib.request.Request(
            f"{self.api.rstrip('/')}/app/installations/{self.installation_id}/access_tokens", data=b"{}",
            method="POST", headers={"Authorization": f"Bearer {self.jwt()}", "Accept": "application/vnd.github+json",
                                    "User-Agent": "brick-runner"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode())
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise GitHubError(f"could not get a GitHub App installation token: {getattr(exc, 'code', exc)}") from exc
        self._token = body["token"]
        self._expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00")).timestamp()
        return self._token


class GitHub:
    def __init__(self, repository: str, token: str | Callable[[], str], api: str = API) -> None:
        if repository.count("/") != 1:
            raise GitHubError(f"repository must be owner/name, not {repository!r}")
        self.repository = repository
        self._token = token if callable(token) else (lambda: token)
        self.api = api.rstrip("/")

    def token(self) -> str:
        return self._token()

    # ------------------------------------------------------------ transport

    def _request(self, method: str, path: str, body: Any = None, query: dict[str, Any] | None = None) -> Any:
        url = f"{self.api}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token()}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "brick-runner",
            **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as exc:
            retry = exc.headers.get("Retry-After") if exc.headers else None
            if retry is None and exc.headers and exc.headers.get("X-RateLimit-Remaining") == "0":
                reset = exc.headers.get("X-RateLimit-Reset")
                retry = max(0.0, float(reset) - time.time()) if reset else 60.0
            detail = exc.read().decode(errors="replace")[:200]
            raise GitHubError(f"{method} {path}: HTTP {exc.code} {detail}",
                              retry_after=float(retry) if retry is not None else None) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise GitHubError(f"{method} {path}: {getattr(exc, 'reason', exc)}") from exc
        try:
            return json.loads(text) if text else None
        except ValueError as exc:
            raise GitHubError(f"{method} {path}: the response is not JSON") from exc

    def _pages(self, path: str, query: dict[str, Any] | None = None, limit: int = 20) -> list[Any]:
        items: list[Any] = []
        for page in range(1, limit + 1):
            batch = self._request("GET", path, query={**(query or {}), "per_page": 100, "page": page})
            items += batch
            if len(batch) < 100:
                break
        return items

    # ------------------------------------------------------------ what the runner uses

    def open_pulls(self) -> list[dict[str, Any]]:
        return self._pages(f"/repos/{self.repository}/pulls", {"state": "open", "sort": "updated", "direction": "desc"})

    def set_status(self, sha: str, state: str, description: str, context: str = "brick-runner") -> None:
        self._request("POST", f"/repos/{self.repository}/statuses/{sha}",
                      {"state": state, "description": description[:140], "context": context})

    def post_comment(self, number: int, body: str) -> int:
        return self._request("POST", f"/repos/{self.repository}/issues/{number}/comments", {"body": body})["id"]

    def edit_comment(self, comment_id: int, body: str) -> None:
        self._request("PATCH", f"/repos/{self.repository}/issues/comments/{comment_id}", {"body": body})

    def comments_since(self, since: str) -> list[dict[str, Any]]:
        return self._pages(f"/repos/{self.repository}/issues/comments",
                           {"since": since, "sort": "created", "direction": "asc"})

    def clone_url(self) -> str:
        return f"https://github.com/{self.repository}.git"

    def git_env(self) -> dict[str, str]:
        """Environment that sends the token to github.com only: never on a command line, never stored."""
        return git_auth_env(self.token())


def git_auth_env(token: str) -> dict[str, str]:
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}", "GIT_TERMINAL_PROMPT": "0"}


def redact(text: str, secrets: list[str]) -> str:
    """Remove every secret value (and its base64 forms) from text that leaves the runner."""
    for secret in sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
        text = text.replace(base64.b64encode(f"x-access-token:{secret}".encode()).decode(), "[redacted]")
    return text
