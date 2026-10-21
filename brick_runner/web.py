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
a browser (see workers/evaluator.py, command ``observe``).

Headers: ``app/headers.json`` may declare the security headers the app is
served with in production ({"Content-Security-Policy": "...", ...}). The
evaluation server sends the same headers, so a page that a policy breaks in
production also breaks in the evaluation. Only the names in ALLOWED_HEADERS
are accepted.

Browser: BRICK_RUNNER_BROWSER picks it. ``chromium`` (the default) is
Playwright's headless Chromium. ``chrome`` is Google Chrome, headed, on a
virtual screen (Xvfb) inside the evaluator's sandbox: the browser people use,
with real input events, focus and native dialogs.
"""

from __future__ import annotations

import hashlib
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
HEADERS_FILE = "headers.json"   # in app/: the headers the app is served with
HEADERS_COPY = ".web-headers.json"   # in the evaluation workspace, beside BUILD_FOLDER
ALLOWED_HEADERS = ("Content-Security-Policy", "X-Content-Type-Options", "Referrer-Policy", "Permissions-Policy",
                   "X-Frame-Options", "Cross-Origin-Opener-Policy", "Cross-Origin-Embedder-Policy",
                   "Cross-Origin-Resource-Policy")
BROWSERS = ("chromium", "chrome")
CHROME = Path("/opt/google/chrome/chrome")
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


def read_headers(app: Path) -> dict[str, str]:
    """The headers app/headers.json declares; {} when there is no file."""
    path = app / HEADERS_FILE
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise BuildError(f"{APP}/{HEADERS_FILE} must be a plain file")
    try:
        headers = json.loads(path.read_text())
    except ValueError as exc:
        raise BuildError(f"{APP}/{HEADERS_FILE} is not JSON: {exc}") from exc
    allowed = {name.lower(): name for name in ALLOWED_HEADERS}
    if not isinstance(headers, dict):
        raise BuildError(f"{APP}/{HEADERS_FILE} must be an object of header names to values")
    clean = {}
    for name, value in headers.items():
        if not isinstance(name, str) or name.lower() not in allowed:
            raise BuildError(f"{APP}/{HEADERS_FILE}: {name!r} is not an allowed header ({', '.join(ALLOWED_HEADERS)})")
        if not isinstance(value, str) or len(value) > 4000 or not all(32 <= ord(c) < 127 for c in value):
            raise BuildError(f"{APP}/{HEADERS_FILE}: the value of {name} must be one line of printable ASCII")
        clean[allowed[name.lower()]] = value
    return clean


def browser_settings() -> dict[str, object]:
    """The browser the evaluator starts: {"name", "headed", "executable"?}."""
    name = os.environ.get("BRICK_RUNNER_BROWSER", "chromium")
    if name not in BROWSERS:
        raise BuildError(f"BRICK_RUNNER_BROWSER must be one of {', '.join(BROWSERS)}, not {name!r}")
    if name == "chromium":
        return {"name": "chromium", "headed": False}
    if not CHROME.is_file():
        raise BuildError(f"BRICK_RUNNER_BROWSER=chrome, and Google Chrome is not at {CHROME}")
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        raise BuildError("BRICK_RUNNER_BROWSER=chrome needs Xvfb for its virtual screen")
    return {"name": "chrome", "headed": True, "executable": str(CHROME), "xvfb": xvfb}


def workers() -> int:
    """How many evaluator workers (each with its own browser) observe at once: BRICK_RUNNER_WEB_WORKERS."""
    try:
        count = int(os.environ.get("BRICK_RUNNER_WEB_WORKERS", "1"))
    except ValueError:
        raise BuildError("BRICK_RUNNER_WEB_WORKERS must be a number") from None
    if not 1 <= count <= 16:
        raise BuildError("BRICK_RUNNER_WEB_WORKERS must be from 1 to 16")
    return count


def toolchain() -> dict[str, str]:
    """Versions that change a web evaluation: Node, npm, Playwright, its browsers, and the browser used."""
    from importlib import metadata

    def version(command: list[str]) -> str:
        try:
            return subprocess.run(command, capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "missing"

    browsers = os.environ.get("BRICK_RUNNER_BROWSERS") or os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or ""
    try:
        playwright = metadata.version("playwright")
    except metadata.PackageNotFoundError:
        playwright = "missing"
    try:
        browser = browser_settings()
        used = (version([str(browser["executable"]), "--version"]) + " headed") if browser["name"] == "chrome" \
            else "chromium headless"
    except BuildError as exc:
        used = f"unavailable: {exc}"   # the build step then fails with this reason
    # The runner's own browser and agent code: a change to it is a new evaluation.
    here = Path(__file__).resolve().parent
    harness = hashlib.sha256(b"".join((here / name).read_bytes() for name in
                                      ("agent.py", "workers/_agent.py", "workers/evaluator.py"))).hexdigest()[:16]
    return {"node": version(["node", "--version"]), "npm": version(["npm", "--version"]),
            "playwright": playwright, "browser": used, "harness": harness,
            # Workers share the CPU, so their number changes timings: it is part of the ID.
            "workers": os.environ.get("BRICK_RUNNER_WEB_WORKERS", "1"),
            "browsers": ",".join(sorted(p.name for p in Path(browsers).iterdir())) if browsers and Path(browsers).is_dir() else ""}


def build(sandbox: Sandbox, code_root: Path, brick: str, into: Path, timeout: float = 900) -> str:
    """Build bricks/<brick>/app from the candidate tree; copy dist/ into ``into``. Returns the log."""
    if not shutil.which("npm"):
        raise BuildError("npm is not installed on the runner")
    source = code_root / "bricks" / brick / APP
    if not source.is_dir():
        raise BuildError(f"bricks/{brick}/{APP}/ is missing")
    with tempfile.TemporaryDirectory(prefix="brick-build-") as scratch:
        work = Path(scratch)
        links = [p for p in source.rglob("*") if p.is_symlink()]
        if links:
            raise BuildError("symlinks are not allowed in app/: " + ", ".join(p.name for p in links[:5]))
        shutil.copytree(source, work / APP, symlinks=True, ignore=shutil.ignore_patterns("node_modules", DIST))
        headers = read_headers(source)
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
        (into.parent / HEADERS_COPY).write_text(json.dumps(headers))
        return log
