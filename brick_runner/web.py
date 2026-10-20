"""Web bricks: a browser app, built in the sandbox and evaluated in a browser.

A brick whose contract.py says ``KIND = "web"`` keeps its app in ``app/``:
``package.json``, ``package-lock.json`` and the sources. The runner builds it
in two steps:

1. Prefetch, OUTSIDE the sandbox, with the network: only package.json and
   package-lock.json are copied, every package must come from the npm
   registry with an integrity hash, and no install script runs
   (``--ignore-scripts``). This fills an npm cache and runs no candidate code.
2. Build, INSIDE the sandbox, with no network: ``npm ci --offline`` from that
   cache, then ``npm run build``. The build must write ``app/dist/``.

The evaluator then serves ``dist/`` on 127.0.0.1 in its own sandbox and drives
Chromium (see workers/evaluator.py, command ``observe``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .sandbox import Sandbox, SandboxError

APP = "app"
DIST = "dist"
BUILD_FOLDER = ".web-build"   # in the evaluation workspace: the built app, as data
REGISTRY = "https://registry.npmjs.org/"


class BuildError(RuntimeError):
    def __init__(self, message: str, log: str = "") -> None:
        super().__init__(message)
        self.log = log


def check_lock(lock: dict) -> list[str]:
    """Problems with package-lock.json: a package from another host, or without integrity."""
    problems = []
    if lock.get("lockfileVersion", 0) < 2:
        problems.append("package-lock.json must be lockfileVersion 2 or later")
    for name, entry in (lock.get("packages") or {}).items():
        if not name or entry.get("link"):
            continue  # the root package, or a workspace link
        resolved = entry.get("resolved", "")
        if not resolved.startswith(REGISTRY):
            problems.append(f"{name}: resolved from {resolved or 'nowhere'}; only {REGISTRY} is allowed")
        if not str(entry.get("integrity", "")).startswith("sha512-"):
            problems.append(f"{name}: no sha512 integrity hash")
    return problems


def _npm_env(home: Path) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LANG": "C.UTF-8",
            "npm_config_registry": REGISTRY, "npm_config_userconfig": str(home / "npmrc"),
            "npm_config_globalconfig": str(home / "npmrc-global"), "npm_config_update_notifier": "false"}


def prefetch(app: Path, cache: Path, timeout: float = 900) -> None:
    """Fill an npm cache for the lockfile, with no candidate code and no install scripts."""
    lock_path = app / "package-lock.json"
    if not (app / "package.json").is_file() or not lock_path.is_file():
        raise BuildError("a web brick needs app/package.json and app/package-lock.json")
    try:
        lock = json.loads(lock_path.read_text())
    except ValueError as exc:
        raise BuildError(f"app/package-lock.json is not JSON: {exc}") from exc
    problems = check_lock(lock)
    if problems:
        raise BuildError("package-lock.json refused:\n" + "\n".join(problems[:20]))
    if not any(name for name in (lock.get("packages") or {}) if name):
        cache.mkdir(parents=True, exist_ok=True)
        return  # no dependencies: nothing to fetch
    with tempfile.TemporaryDirectory(prefix="brick-npm-") as scratch:
        work = Path(scratch) / "app"
        work.mkdir()
        shutil.copy2(app / "package.json", work / "package.json")
        shutil.copy2(lock_path, work / "package-lock.json")
        command = ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--cache", str(cache)]
        try:
            done = subprocess.run(command, cwd=work, capture_output=True, text=True, timeout=timeout,
                                  env=_npm_env(Path(scratch)))
        except subprocess.TimeoutExpired as exc:
            raise BuildError("npm prefetch exceeded its time limit") from exc
        if done.returncode != 0:
            raise BuildError("npm prefetch failed", (done.stdout + done.stderr)[-6000:])


def build(sandbox: Sandbox, code_root: Path, brick: str, into: Path, timeout: float = 900) -> str:
    """Build bricks/<brick>/app from the candidate tree; copy dist/ into ``into``. Returns the log."""
    if not shutil.which("npm"):
        raise BuildError("npm is not installed on the runner")
    source = code_root / "bricks" / brick / APP
    if not source.is_dir():
        raise BuildError(f"bricks/{brick}/{APP}/ is missing")
    with tempfile.TemporaryDirectory(prefix="brick-build-") as scratch:
        work = Path(scratch)
        shutil.copytree(source, work / APP, ignore=shutil.ignore_patterns("node_modules", DIST))
        prefetch(work / APP, work / ".npm-cache")
        script = ("cd app && npm ci --offline --no-audit --no-fund --cache ../.npm-cache "
                  "&& npm run build")
        try:
            code, log = sandbox.run_command(["sh", "-c", script], work, timeout=timeout)
        except SandboxError as exc:
            raise BuildError(str(exc), exc.log) from exc
        if code != 0:
            raise BuildError(f"the build exited {code}", log)
        dist = work / APP / DIST
        if not dist.is_dir() or not any(dist.iterdir()):
            raise BuildError(f"the build wrote no {APP}/{DIST}/", log)
        if into.exists():
            shutil.rmtree(into)
        # Copy files only: a symlink in dist/ could point outside it.
        shutil.copytree(dist, into, symlinks=True, ignore_dangling_symlinks=True)
        for path in list(into.rglob("*")):
            if path.is_symlink():
                path.unlink()
        return log
