"""Shared by both workers. Standard library only; runs inside the sandbox."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

MARKER = "\x1ebrick-runner-result\x1e"
BLOCKED = ("socket.connect", "socket.bind", "socket.sendto", "subprocess.Popen", "os.system",
           "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "ctypes.dlopen")


def guard(event: str, args: tuple) -> None:
    if event.startswith(BLOCKED):
        raise PermissionError(f"the evaluation sandbox does not allow {event}")


def start() -> tuple[dict[str, Any], Any]:
    """Read the payload, keep the real stdout for the result, and arm the guard."""
    payload = json.loads(sys.stdin.read())
    result_stream = os.fdopen(os.dup(1), "w", encoding="utf-8")
    sys.stdout = sys.stderr  # anything the code under evaluation prints goes to the log
    sys.addaudithook(guard)
    return payload, result_stream


def finish(stream: Any, result: dict[str, Any]) -> None:
    stream.write(MARKER + json.dumps(result))
    stream.flush()
