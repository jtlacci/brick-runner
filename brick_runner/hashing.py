"""Canonical JSON and SHA-256 hashes, identical to the framework runtime's."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(value: Any) -> str:
    """``sha256:<hex>`` of a JSON value. The runtime's ``digest`` gives the same."""
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(root: Path, files: Iterable[Path]) -> str:
    """One hash for a set of files: their paths relative to ``root`` and contents."""
    entries = sorted((p.relative_to(root).as_posix(), file_digest(p)) for p in files if p.is_file())
    return digest(entries)
