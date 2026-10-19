"""Run a worker script in a sandbox: no network, a clean environment, a time limit.

Network isolation uses a new network namespace (``unshare --net``) when the
host allows it. The workers also install an audit hook that refuses sockets
and subprocesses, as a second layer. The result says whether the namespace
was available.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

WORKERS = Path(__file__).resolve().parent / "workers"
MARKER = "\x1ebrick-runner-result\x1e"


class SandboxError(RuntimeError):
    def __init__(self, message: str, log: str = "") -> None:
        super().__init__(message)
        self.log = log


@functools.lru_cache(maxsize=1)
def network_isolation() -> bool:
    if not shutil.which("unshare"):
        return False
    try:
        return subprocess.run(["unshare", "--net", "--map-root-user", "true"],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def run_worker(script: str, workspace: Path, payload: dict[str, Any], *, timeout: float = 600) -> tuple[dict[str, Any], str]:
    """Run ``workers/<script>`` on a workspace. Returns (result, log)."""
    command = [sys.executable, "-I", "-B", str(WORKERS / script), str(workspace)]
    if network_isolation():
        command = ["unshare", "--net", "--map-root-user", *command]
    with tempfile.TemporaryDirectory(prefix="brick-home-") as home:
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONHASHSEED": "0", "HOME": home,
               "LANG": "C.UTF-8", "TMPDIR": home}
        try:
            done = subprocess.run(command, input=json.dumps(payload), capture_output=True, text=True,
                                  timeout=timeout, cwd=workspace, env=env)
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(f"{script} exceeded {timeout:.0f}s", _text(exc.stderr)) from exc
    log = done.stderr[-8000:]
    if MARKER not in done.stdout:
        raise SandboxError(f"{script} exited {done.returncode} without a result", log)
    result = json.loads(done.stdout.rsplit(MARKER, 1)[1])
    if "fatal" in result:
        raise SandboxError(f"{script} failed: {result['fatal']}", log)
    return result, log


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")[-8000:]
    return (value or "")[-8000:]
