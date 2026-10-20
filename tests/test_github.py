"""The GitHub client against a local HTTP server: App auth, rate limits, pagination."""

from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brick_runner.github import AppAuth, GitHub, GitHubError, git_auth_env  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    calls: list[tuple[str, str, dict, str]] = []
    limited = False

    def log_message(self, *args) -> None:
        pass

    def _reply(self, code: int, body, headers: dict | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        Handler.calls.append(("POST", self.path, dict(self.headers), body))
        if self.path.startswith("/app/installations/7/access_tokens"):
            self._reply(201, {"token": "ghs_installation_token", "expires_at": "2099-01-01T00:00:00Z"})
        elif Handler.limited:
            self._reply(403, {"message": "secondary rate limit"}, {"Retry-After": "42"})
        else:
            self._reply(201, {"id": 5})

    def do_GET(self) -> None:
        Handler.calls.append(("GET", self.path, dict(self.headers), ""))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        page = int(query.get("page", ["1"])[0])
        self._reply(200, [{"number": i} for i in range(100)] if page == 1 else [{"number": 100}])


class GitHubClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.api = f"http://127.0.0.1:{cls.server.server_port}"
        cls.tmp = Path(tempfile.mkdtemp())
        cls.key = cls.tmp / "app.pem"
        subprocess.run(["openssl", "genrsa", "-out", str(cls.key), "2048"], check=True, capture_output=True)
        subprocess.run(["openssl", "rsa", "-in", str(cls.key), "-pubout", "-out", str(cls.tmp / "app.pub")],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def setUp(self) -> None:
        Handler.calls.clear()
        Handler.limited = False

    def test_app_jwt_is_signed_with_the_app_key(self) -> None:
        auth = AppAuth("123", self.key, "7", api=self.api)
        header, payload, signature = auth.jwt().split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
        self.assertEqual(claims["iss"], "123")
        (self.tmp / "input").write_bytes(f"{header}.{payload}".encode())
        (self.tmp / "sig").write_bytes(base64.urlsafe_b64decode(signature + "=="))
        verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(self.tmp / "app.pub"),
                                   "-signature", str(self.tmp / "sig"), str(self.tmp / "input")],
                                  capture_output=True, text=True)
        self.assertIn("Verified OK", verified.stdout)

    def test_requests_use_the_installation_token_and_cache_it(self) -> None:
        client = GitHub("o/r", AppAuth("123", self.key, "7", api=self.api), api=self.api)
        client.set_status("abc", "success", "ok")
        client.set_status("abc", "success", "ok")
        token_calls = [c for c in Handler.calls if "access_tokens" in c[1]]
        self.assertEqual(len(token_calls), 1)
        status_calls = [c for c in Handler.calls if "/statuses/" in c[1]]
        self.assertTrue(all(c[2]["Authorization"] == "Bearer ghs_installation_token" for c in status_calls))

    def test_rate_limits_carry_retry_after(self) -> None:
        Handler.limited = True
        client = GitHub("o/r", "token", api=self.api)
        with self.assertRaises(GitHubError) as caught:
            client.post_comment(1, "x")
        self.assertEqual(caught.exception.retry_after, 42.0)

    def test_pagination(self) -> None:
        self.assertEqual(len(GitHub("o/r", "token", api=self.api).open_pulls()), 101)

    def test_git_token_never_appears_on_a_command_line(self) -> None:
        env = git_auth_env("secret-token-value")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        self.assertNotIn("secret-token-value", env["GIT_CONFIG_VALUE_0"])


if __name__ == "__main__":
    unittest.main()
