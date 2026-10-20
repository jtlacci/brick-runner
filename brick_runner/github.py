"""A small GitHub REST client, standard library only.

The runner needs only these permissions on the brick repository:
Contents: read, Pull requests: read, Commit statuses: read and write,
Issues: read and write (pull-request comments are issue comments).
"""

from __future__ import annotations

import base64
import json
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, repository: str, token: str, api: str = API) -> None:
        if repository.count("/") != 1:
            raise GitHubError(f"repository must be owner/name, not {repository!r}")
        self.repository = repository
        self.token = token
        self.api = api.rstrip("/")

    # ------------------------------------------------------------ transport

    def _request(self, method: str, path: str, body: Any = None, query: dict[str, Any] | None = None) -> Any:
        url = f"{self.api}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "brick-runner",
            **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as exc:
            raise GitHubError(f"{method} {path}: HTTP {exc.code} {exc.read().decode(errors='replace')[:300]}") from exc
        except urllib.error.URLError as exc:
            raise GitHubError(f"{method} {path}: {exc.reason}") from exc
        return json.loads(text) if text else None

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

    def git_auth(self) -> list[str]:
        """Git options that send the token for github.com only, without storing it."""
        basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]
