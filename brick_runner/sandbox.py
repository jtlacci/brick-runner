"""Sandboxed worker processes.

Each worker runs in new user, network and mount namespaces:

- no network;
- its own workspace copy, mounted at /mnt, is the only tree it works in;
- /tmp, /dev/shm, /var/tmp, the runner's state folder and the brick
  repository are hidden behind empty private tmpfs mounts, so a worker cannot
  read the signing key, the store, the ledger or another worker's workspace;
- every other mount, including the runner's own source and the Python
  environment, is remounted read-only, so a worker cannot change the host.

The workers also install an audit hook that refuses sockets and subprocesses.
Without namespaces the runner refuses to run, unless BRICK_RUNNER_UNSANDBOXED=1
is set (for development hosts only; results then say so).
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

WORKERS = Path(__file__).resolve().parent / "workers"
MARKER = "\x1ebrick-runner-result\x1e"
MOUNT_POINT = "/mnt"
WRAPPER = r'''
set -e
ws="$1"; shift
mount --rbind "$ws" /mnt
while [ "$1" != "--" ]; do
  case "$1" in
    hide:*) p="${1#hide:}"
            if [ -d "$p" ]; then mount -t tmpfs -o size=4m tmpfs "$p"
            elif [ -e "$p" ]; then mount --bind /dev/null "$p"; fi ;;
  esac
  shift
done
shift
# Everything except the worker's own tree becomes read-only: the runner's
# source, the Python environment, /root, /etc, /dev/shm (now a private tmpfs).
# In a user namespace the kernel locks nosuid, nodev, noexec and the atime
# flags of inherited mounts, so a remount must repeat them (snap mounts, /run).
# The last line for a mount point is the mount on top, the one a remount changes.
# (The loop runs in a subshell of the pipe; the mounts are the same namespace,
# and set -e stops the wrapper when the loop exits 97.)
awk '{o[$2]=$4} END {for (m in o) print m, o[m]}' /proc/self/mounts | while read -r m opts; do
  case "$m" in /mnt|/mnt/*|/proc|/proc/*|/sys|/sys/*|/dev|/dev/pts) continue ;; esac
  # A mount that is already read-only stays so: the kernel locks that flag too.
  case ",$opts," in *,ro,*) continue ;; esac
  keep=""
  for o in $(echo "$opts" | tr , ' '); do
    case "$o" in nosuid|nodev|noexec|noatime|nodiratime|relatime|strictatime) keep="$keep,$o" ;; esac
  done
  mount -o "remount,bind,ro$keep" "$m" 2>/dev/null || { echo "sandbox: cannot make $m read-only" >&2; exit 97; }
done
cd /mnt
exec "$@"
'''


class SandboxError(RuntimeError):
    def __init__(self, message: str, log: str = "") -> None:
        super().__init__(message)
        self.log = log


@functools.lru_cache(maxsize=1)
def isolation() -> bool:
    if not shutil.which("unshare"):
        return False
    try:
        probe = subprocess.run(
            ["unshare", "--net", "--mount", "--map-root-user", "--propagation", "private",
             "sh", "-c", "mount -t tmpfs tmpfs /mnt && mount -o remount,bind,ro /"],
            capture_output=True, timeout=10)
        return probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def network_isolation() -> bool:
    return isolation()


class Sandbox:
    """What every worker hides and protects."""

    def __init__(self, hide: list[Path]) -> None:
        self.hide = [Path(p).resolve() for p in hide]
        self.unsandboxed = not isolation()
        if self.unsandboxed and os.environ.get("BRICK_RUNNER_UNSANDBOXED") != "1":
            raise SandboxError("this host cannot create user, network and mount namespaces; "
                               "the runner refuses to evaluate without them")

    def command(self, script: str, workspace: Path) -> tuple[list[str], str]:
        python = [sys.executable, "-I", "-B", str(WORKERS / script)]
        if self.unsandboxed:
            return [*python, str(workspace)], str(workspace)
        keep = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), WORKERS}
        hide = {Path(tempfile.gettempdir()).resolve(), Path("/dev/shm"), Path("/var/tmp"), *self.hide}
        args = [f"hide:{p}" for p in sorted(hide) if not any(_inside(k, p) for k in keep)]
        return (["unshare", "--net", "--mount", "--map-root-user", "--propagation", "private",
                 "sh", "-c", WRAPPER, "sandbox", str(workspace), *args, "--", *python, MOUNT_POINT],
                MOUNT_POINT)

    @staticmethod
    def env() -> dict[str, str]:
        return {"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONHASHSEED": "0", "LANG": "C.UTF-8",
                "HOME": "/mnt", "TMPDIR": "/mnt"}

    def run(self, script: str, workspace: Path, payload: dict[str, Any], *, timeout: float = 600) -> tuple[dict[str, Any], str]:
        """A one-shot worker: payload on stdin, one result after the marker on stdout."""
        command, _ = self.command(script, workspace)
        try:
            done = subprocess.run(command, input=json.dumps(payload), capture_output=True, text=True,
                                  timeout=timeout, cwd=workspace, env=self.env())
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(f"{script} exceeded {timeout:.0f}s", _text(exc.stderr)) from exc
        log = done.stderr[-8000:]
        if MARKER not in done.stdout:
            raise SandboxError(f"{script} exited {done.returncode} without a result", log)
        result = json.loads(done.stdout.rsplit(MARKER, 1)[1])
        if "fatal" in result:
            raise SandboxError(f"{script} failed: {result['fatal']}", log)
        return result, log

    def start(self, script: str, workspace: Path) -> "Process":
        command, _ = self.command(script, workspace)
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, cwd=workspace, env=self.env(), bufsize=1)
        return Process(process)


class Process:
    """A long-lived worker that speaks one JSON object per line."""

    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process
        self.log: list[str] = []
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        threading.Thread(target=self._drain, daemon=True).start()
        threading.Thread(target=self._read, daemon=True).start()

    def _drain(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.log.append(line)
            if len(self.log) > 400:
                del self.log[:200]

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue  # stray output; only well-formed protocol lines count
            if isinstance(message, dict) and "op" in message:
                self.messages.put(message)
        self.messages.put(None)

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except BrokenPipeError as exc:
            raise SandboxError("the worker stopped", self.log_text()) from exc

    def receive(self, deadline: float) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        try:
            message = self.messages.get(timeout=max(remaining, 0.001))
        except queue.Empty:
            self.stop()
            raise SandboxError("the worker exceeded its time limit", self.log_text()) from None
        if message is None:
            self.messages.put(None)
            time.sleep(0.05)
            raise SandboxError("the worker stopped", self.log_text())
        return message

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def log_text(self) -> str:
        return "".join(self.log)[-8000:]


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")[-8000:]
    return (value or "")[-8000:]
