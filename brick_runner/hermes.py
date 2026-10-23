"""Hermes as a live external: a brick that answers with the Hermes agent is evaluated with Hermes itself.

A brick's policy.yml names it:

    live_externals: [hermes]

The brick calls ``external:hermes`` with a request of this shape:

    {"system": text, "messages": [OpenAI chat messages, people as "user"],
     "profile": {"commit": <40 hex>, "model": <allowed model>, "soul": text}}

and gets {"text", "steps": [{tool, label}]} back. When a case has no recording
for that request, the runner answers it with Hermes (NousResearch/hermes-agent)
at the commit the request names:

- The runner installs that commit once (uv, its lock file) under
  BRICK_RUNNER_HERMES_DIR (default /var/cache/brick-runner/hermes).
- Hermes runs inside the runner's sandbox (workers/_hermes_driver.py): no
  network, a read-only system, the runner's state and keys hidden. Its only way
  out is the model relay: its Anthropic calls go to a Unix socket where the
  runner answers them through live.py (allowed models, budgets, cache). The key
  never enters the sandbox.
- The runner writes Hermes's config itself. The candidate chooses only the
  model, the persona (SOUL.md) and the system prompt. Hermes's only tools are
  the evaluation's fixture tools (workers/_hermes_tools.py, an MCP server named
  "chip"), which answer from the case's ``_fixture`` with ``evaluate.py``'s
  ``HERMES_TOOLS`` and ``tool_call(fixture, name, args)``. No web, terminal,
  files, code, memory, browser or delegation.
- The response also has "tools": [{name, args, result}], for score(); the
  brick's adapter ignores it.

Each whole answer is cached by (request, fixture, tools, runner config), and
each of Hermes's model calls is cached too, so a rerun costs nothing.
"""

from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable

from .hashing import digest

REPO_URL = "https://github.com/NousResearch/hermes-agent.git"
COMMIT = re.compile(r"^[0-9a-f]{40}$")
PYTHON_VERSION = "3.14"
UV_VERSION = "0.11.32"
EXTRAS = ("messaging", "mcp", "anthropic")
CONFIG_VERSION = 1                    # bump when the config below changes: it is part of every cache key
TURN_TIMEOUT = 300                    # seconds for one answer
MAX_ITERATIONS = 12
WORKERS = Path(__file__).resolve().parent / "workers"
DISABLED = ["terminal", "file", "code_execution", "delegation", "browser", "computer_use", "cronjob", "kanban",
            "image_gen", "video_gen", "tts", "skills", "connections", "vision", "todo", "clarify", "memory",
            "session_search", "web", "search", "x_search", "video", "discord", "discord_admin"]


class HermesError(RuntimeError):
    """``transient``: an install or start failed for a reason that is not the candidate's (try later)."""

    def __init__(self, message: str, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


def root() -> Path:
    return Path(os.environ.get("BRICK_RUNNER_HERMES_DIR", "/var/cache/brick-runner/hermes"))


def _run(argv: list[str], cwd: Path | None = None, env: dict | None = None, timeout: float = 1200) -> None:
    done = subprocess.run(argv, cwd=cwd, env={**os.environ, **(env or {})}, capture_output=True, text=True,
                          timeout=timeout)
    if done.returncode != 0:
        raise HermesError(f"{' '.join(argv[:3])} failed: {(done.stderr or done.stdout).strip()[-400:]}", transient=True)


def install(commit: str) -> Path:
    """The Hermes checkout at this commit, with its environment (installed once)."""
    if not COMMIT.match(commit):
        raise HermesError("profile.commit must be a full 40-character Hermes commit hash")
    base = root()
    target = base / commit
    if (target / ".ready").exists():
        return target
    base.mkdir(parents=True, exist_ok=True)
    uv = base / "uv" / "bin" / "uv"
    if not uv.exists():
        _run([sys.executable, "-m", "venv", str(base / "uv")])
        _run([str(base / "uv" / "bin" / "pip"), "install", "-q", f"uv=={UV_VERSION}"])
    work = base / f".{commit}.tmp"
    shutil.rmtree(work, ignore_errors=True)
    _run(["git", "init", "-q", str(work)])
    _run(["git", "-C", str(work), "fetch", "-q", "--depth", "1", REPO_URL, commit])
    _run(["git", "-C", str(work), "checkout", "-q", "FETCH_HEAD"])
    env = {"UV_CACHE_DIR": str(base / "uv-cache"), "UV_PYTHON_INSTALL_DIR": str(base / "python")}
    _run([str(uv), "python", "install", "-q", PYTHON_VERSION], env=env)
    extras = [arg for extra in EXTRAS for arg in ("--extra", extra)]
    _run([str(uv), "sync", "-q", "--frozen", "--no-dev", "--managed-python", "--python", PYTHON_VERSION, *extras],
         cwd=work, env=env)
    shutil.rmtree(target, ignore_errors=True)
    work.rename(target)
    (target / ".ready").write_text(commit)
    return target


def config(model: str, python: str) -> str:
    """Hermes's config.yaml for an evaluation: the runner's, never the candidate's."""
    return json.dumps({
        "model": {"default": model, "provider": "anthropic", "streaming": False},
        "platform_toolsets": {"api_server": ["chip"]},
        "agent": {"disabled_toolsets": DISABLED, "max_iterations": MAX_ITERATIONS},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "tools": {"tool_search": {"enabled": "off"}},
        "mcp_servers": {"chip": {"command": python, "args": ["-I", "/mnt/_hermes_tools.py"],
                                 "env": {"HERMES_FIXTURE": "/mnt/fixture.json", "HERMES_EVALUATE": "/mnt/evaluate.py"},
                                 "timeout": 60, "connect_timeout": 30, "trust": "full"}},
        "telemetry": {"enabled": False},
        "updates": {"check": False},
    }, indent=2)   # JSON is YAML


# ------------------------------------------------------------------ the model relay (runner side)

class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def model_relay(path: Path, answer: Callable[[dict], dict]) -> _UnixHTTPServer:
    """An Anthropic Messages endpoint on a Unix socket: POST /v1/messages -> answer(body)."""

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            pass

        def address_string(self) -> str:   # a Unix socket has no client address
            return "sandbox"

        def _reply(self, status: int, data: dict) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                request = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._reply(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad JSON"}})
            if not self.path.split("?")[0].endswith("/v1/messages"):
                return self._reply(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})
            try:
                self._reply(200, answer(request))
            except Exception as exc:  # noqa: BLE001 -- the model call failed: Hermes sees an API error
                kind = "overloaded_error" if getattr(exc, "transient", False) else "invalid_request_error"
                status = 529 if kind == "overloaded_error" else 400
                self._reply(status, {"type": "error", "error": {"type": kind, "message": str(exc)[:300]}})

        def do_GET(self) -> None:
            self._reply(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})

    if path.exists():
        path.unlink()
    server = _UnixHTTPServer(str(path), Handler)
    threading.Thread(target=server.serve_forever, name="hermes-model-relay", daemon=True).start()
    return server


# ------------------------------------------------------------------ one Hermes for one evaluation run

class Hermes:
    """Starts the driver in the sandbox for a commit and model; answers requests one at a time."""

    def __init__(self, sandbox, evaluate_py: Path, model_answer: Callable[[dict], dict]) -> None:
        self.sandbox = sandbox
        self.evaluate_py = evaluate_py
        self.model_answer = model_answer
        self.key: tuple[str, str] | None = None
        self.process: subprocess.Popen | None = None
        self.work: Path | None = None
        self.relay = None
        self.lock = threading.Lock()

    def _start(self, commit: str, model: str) -> None:
        self.close()
        checkout = install(commit)
        python = str(checkout / ".venv" / "bin" / "python")
        self.work = Path(tempfile.mkdtemp(prefix="brick-hermes-"))
        home = self.work / "home"
        home.mkdir()
        (home / "config.yaml").write_text(config(model, python))
        (home / ".env").write_text("ANTHROPIC_API_KEY=relay\nANTHROPIC_BASE_URL=http://127.0.0.1:8099\n")
        shutil.copy(WORKERS / "_hermes_driver.py", self.work / "_hermes_driver.py")
        shutil.copy(WORKERS / "_hermes_tools.py", self.work / "_hermes_tools.py")
        shutil.copy(self.evaluate_py, self.work / "evaluate.py")
        (self.work / "fixture.json").write_text("{}")
        self.relay = model_relay(self.work / "model.sock", self.model_answer)
        argv = [python, "-I", "/mnt/_hermes_driver.py", str(checkout)]
        command = argv if self.sandbox.unsandboxed else self.sandbox.wrap(argv, self.work)
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/mnt/home",
               "HERMES_HOME": "/mnt/home", "PYTHONHASHSEED": "0", "TMPDIR": "/mnt"}
        if self.sandbox.unsandboxed:   # development hosts: the same files, at their real paths
            env.update(HOME=str(home), HERMES_HOME=str(home), TMPDIR=str(self.work), HERMES_MNT=str(self.work))
            (home / "config.yaml").write_text(config(model, python).replace("/mnt/", f"{self.work}/"))
            argv[2] = str(self.work / "_hermes_driver.py")
            command = argv
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, cwd=self.work, env=env, bufsize=1)
        threading.Thread(target=self._drain, daemon=True).start()
        ready = self._read(time.monotonic() + 180)
        if ready.get("ready") is not True:
            raise HermesError(f"Hermes did not start: {ready.get('error', ready)}"[:400], transient=True)
        self.key = (commit, model)

    def _drain(self) -> None:
        self.log = []
        for line in self.process.stderr:   # Hermes logs; the last lines help when it fails
            self.log = (self.log + [line])[-60:]

    def _read(self, deadline: float) -> dict[str, Any]:
        result: dict[str, Any] = {}

        def read() -> None:
            line = self.process.stdout.readline()
            result.update(json.loads(line) if line.strip() else {"error": "Hermes stopped: " + "".join(self.log)[-300:]})

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(max(1.0, deadline - time.monotonic()))
        if reader.is_alive():
            self.close()
            return {"error": "Hermes took too long"}
        return result

    def answer(self, request: dict, fixture: Any) -> dict[str, Any]:
        profile = request.get("profile") or {}
        commit, model = str(profile.get("commit", "")), str(profile.get("model", ""))
        with self.lock:
            if self.key != (commit, model) or self.process is None or self.process.poll() is not None:
                self._start(commit, model)
            (self.work / "fixture.json").write_text(json.dumps(fixture if isinstance(fixture, dict) else {}))
            message = {"op": "reply", "system": request.get("system", ""), "messages": request.get("messages", []),
                       "soul": profile.get("soul", "")}
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
            return self._read(time.monotonic() + TURN_TIMEOUT)

    def close(self) -> None:
        if self.process is not None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
            self.process = None
        if self.relay is not None:
            self.relay.shutdown()
            self.relay = None
        if self.work is not None:
            shutil.rmtree(self.work, ignore_errors=True)
            self.work = None
        self.key = None


def cache_key(request: dict, fixture: Any, evaluate_py: Path) -> str:
    """The whole answer depends on the request, the fixture, the evaluation's tools and the runner's config."""
    return digest({"request": request, "fixture": fixture, "tools": evaluate_py.read_text(),
                   "config": CONFIG_VERSION})[7:]
