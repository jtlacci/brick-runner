"""Back up the runner's data to a DVC remote, and restore it on a new machine.

What is backed up, and how:

| Data | Where it lives | Backup |
| --- | --- | --- |
| Payloads, records, datasets | ``<home>/store/`` | DVC: the data goes to the remote (S3); small pointer files go to the store's Git history |
| Ledger (MLflow) and bundles | ``<home>/mlflow.db``, ``mlartifacts/``, ``bundles/`` | A consistent snapshot is copied into ``<home>/store/ledger/`` and tracked by DVC too |
| Pointer files (``*.dvc``) | ``<home>/store/.git`` | Pushed to an optional Git remote (a private repository; use a deploy key) |
| Service state | ``<home>/serve-state.json`` | In the ledger snapshot, so a restored service does not evaluate every pull request again |
| Signing key | ``<home>/runner.key`` | NEVER backed up here. Keep a copy in a secret manager. |

Credentials are never written into any config file. DVC reads AWS credentials
the usual way: ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``, ``AWS_PROFILE``,
or an instance role.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from typing import Any

from .github import git_auth_env
from .store import DVC_TRACKED, StoreError

LEDGER = "ledger"
TRACKED = (*DVC_TRACKED, LEDGER)
REMOTE = "storage"
GIT_BRANCH = "main"
IDENTITY = ["-c", "user.name=brick-runner", "-c", "user.email=runner@localhost"]


def _dvc() -> str:
    """The dvc next to this Python (the runner's own environment), else the one on PATH."""
    beside = Path(sys.executable).parent / "dvc"
    if beside.exists():
        return str(beside)
    found = shutil.which("dvc")
    if not found:
        raise StoreError("dvc is not installed; install brick-runner with its dependencies")
    return found


def _run(command: list[str], cwd: Path, *, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    if command[0] == "dvc":
        command = [_dvc(), *command[1:]]
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                            env={**os.environ, **env} if env else None)
    if check and result.returncode:
        name = " ".join(Path(command[0]).name if i == 0 else part for i, part in enumerate(command[:3]))
        raise StoreError(f"{name} failed: {(result.stderr or result.stdout).strip()[:400]}")
    return result


def _git_env(home: Path, url: str | None) -> dict[str, str] | None:
    """For an https github.com remote, send the runner's token through the environment.

    Prefer an SSH deploy key for the evidence repository (``git@github.com:...``):
    then no token is needed here, and the key can write to that repository only.
    """
    if not url or not url.startswith("https://github.com/"):
        return None
    token = os.environ.get("BRICK_RUNNER_GITHUB_TOKEN")
    if not token and (home / "github-token").exists():
        token = (home / "github-token").read_text(encoding="utf-8").strip()
    return git_auth_env(token) if token else None


def _need_dvc() -> None:
    _dvc()


def init(store: Path) -> None:
    _need_dvc()
    store.mkdir(parents=True, exist_ok=True)
    if not (store / ".git").exists():
        _run(["git", "init", "-q", "-b", GIT_BRANCH], store)
    if not (store / ".dvc").exists():
        _run(["dvc", "init", "-q"], store)
        _commit(store, "initialise the evaluation store")


def _commit(store: Path, message: str) -> bool:
    paths = [p for p in (".dvc", ".dvcignore", ".gitignore", *(f"{n}.dvc" for n in TRACKED)) if (store / p).exists()]
    if paths:
        _run(["git", "add", "-A", "--", *paths], store)
    if _run(["git", "diff", "--cached", "--quiet"], store, check=False).returncode == 0:
        return False
    _run(["git", *IDENTITY, "commit", "-qm", message], store)
    return True


def configure(home: Path, url: str, *, region: str | None = None, git: str | None = None) -> dict[str, Any]:
    """Set the DVC remote (for example ``s3://bucket/brick-runner``) and an optional Git remote."""
    store = home / "store"
    init(store)
    _run(["dvc", "remote", "add", "--default", "--force", REMOTE, url], store)
    if region:
        _run(["dvc", "remote", "modify", REMOTE, "region", region], store)
    _commit(store, f"set the DVC remote to {url}")
    if git:
        if _run(["git", "remote", "get-url", "origin"], store, check=False).returncode == 0:
            _run(["git", "remote", "set-url", "origin", git], store)
        else:
            _run(["git", "remote", "add", "origin", git], store)
    return status(home)


def status(home: Path) -> dict[str, Any]:
    store = home / "store"
    if not (store / ".dvc").exists():
        return {"configured": False}
    url = _run(["dvc", "config", f"remote.{REMOTE}.url"], store, check=False)
    git = _run(["git", "remote", "get-url", "origin"], store, check=False)
    dvc_remote = url.stdout.strip() if url.returncode == 0 and url.stdout.strip() else None
    return {"configured": bool(dvc_remote), "dvc_remote": dvc_remote,
            "git_remote": git.stdout.strip() if git.returncode == 0 else None}


def _snapshot_ledger(home: Path) -> None:
    """Copy the ledger and bundles into the store, consistently, with the home path they need."""
    target = home / "store" / LEDGER
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    database = home / "mlflow.db"
    if database.exists():
        source = sqlite3.connect(database)
        copy = sqlite3.connect(target / "mlflow.db")
        with copy:
            source.backup(copy)
        source.close()
        copy.close()
    for folder in ("mlartifacts", "bundles"):
        if (home / folder).is_dir():
            shutil.copytree(home / folder, target / folder)
    if (home / "serve-state.json").exists():
        shutil.copy2(home / "serve-state.json", target / "serve-state.json")
    (target / "home.json").write_text(json.dumps({"home": str(home)}), encoding="utf-8")


def sync(home: Path, *, push: bool = True) -> dict[str, Any]:
    """Snapshot the ledger, pin everything with DVC, and push to the remotes that are set."""
    store = home / "store"
    init(store)
    _snapshot_ledger(home)
    _run(["dvc", "add", "-q", *TRACKED], store)
    committed = _commit(store, "sync evaluation data")
    state = status(home)
    pushed = {"dvc": False, "git": False}
    if push and state["dvc_remote"]:
        _run(["dvc", "push", "-q"], store)
        pushed["dvc"] = True
    if push and state["git_remote"]:
        _run(["git", "push", "-q", "origin", f"HEAD:{GIT_BRANCH}"], store, env=_git_env(home, state["git_remote"]))
        pushed["git"] = True
    return {"committed": committed, "pushed": pushed, **state}


def restore(home: Path, git: str) -> dict[str, Any]:
    """Rebuild ``<home>`` on a new machine from the Git remote and the DVC remote.

    Restore into the same home path as before: MLflow records absolute
    artifact paths. Restore the signing key separately, from your secret manager.
    """
    _need_dvc()
    home = home.resolve()
    store = home / "store"
    if store.exists() and any(store.iterdir()):
        raise StoreError(f"{store} is not empty; restore needs an empty runner home")
    if (home / "mlflow.db").exists():
        raise StoreError(f"{home / 'mlflow.db'} exists; restore needs an empty runner home")
    home.mkdir(parents=True, exist_ok=True)
    if store.exists():
        store.rmdir()
    _run(["git", "clone", "-q", "--branch", GIT_BRANCH, git, str(store)], home, env=_git_env(home, git))
    _run(["dvc", "pull", "-q"], store)
    snapshot = store / LEDGER
    recorded = json.loads((snapshot / "home.json").read_text(encoding="utf-8"))["home"] if (snapshot / "home.json").exists() else None
    if recorded and Path(recorded) != home:
        raise StoreError(f"the backup was made from {recorded}; restore it to that path, not {home}")
    if (snapshot / "mlflow.db").exists():
        shutil.copy2(snapshot / "mlflow.db", home / "mlflow.db")
    for folder in ("mlartifacts", "bundles"):
        if (snapshot / folder).is_dir():
            shutil.copytree(snapshot / folder, home / folder, dirs_exist_ok=True)
    if (snapshot / "serve-state.json").exists():
        shutil.copy2(snapshot / "serve-state.json", home / "serve-state.json")
    return {"restored": str(home), "signing_key": (home / "runner.key").exists(),
            "note": "copy runner.key back from your secret manager before you promote"
                    if not (home / "runner.key").exists() else "ok"}
