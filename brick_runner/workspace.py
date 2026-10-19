"""Build the tree an evaluation runs in, from pinned commits only.

The base commit gives everything: the framework runtime, every other brick,
and every human-only file. Overlays then replace one class of one brick's
files (code or evaluation) with that class from another commit. Classes come
from the brick's policy on the BASE commit, so a candidate cannot change what
counts as its own files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shutil
import tempfile

from .gitrepo import Repo
from .hashing import digest, file_digest, tree_digest
from .policy import CODE, EVALUATION, HUMAN, load_policy

ENV_LOCKS = ("requirements.lock", "requirements.txt", "uv.lock", "poetry.lock")
FRAMEWORK_FILES = ("bricks/__init__.py", "bricks/_runtime.py", "bricks/_yaml.py")


@dataclass(frozen=True)
class Overlay:
    brick: str
    kind: str  # CODE or EVALUATION
    commit: str


@dataclass
class Workspace:
    path: Path
    base: str
    overlays: list[Overlay]
    hashes: dict[str, str] = field(default_factory=dict)

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *exc: object) -> None:
        self.cleanup()


def build(repo: Repo, base: str, overlays: list[Overlay], target: str) -> Workspace:
    path = Path(tempfile.mkdtemp(prefix="brick-ws-"))
    repo.export(base, path)
    for overlay in overlays:
        policy = load_policy(repo, base, overlay.brick)
        prefix = f"bricks/{overlay.brick}/"
        for name in repo.files(base, prefix):
            if policy.classify(name[len(prefix):]) == overlay.kind:
                (path / name).unlink(missing_ok=True)
        wanted = [name for name in repo.files(overlay.commit, prefix)
                  if policy.classify(name[len(prefix):]) == overlay.kind]
        if wanted:
            repo.export(overlay.commit, path, wanted)
    for cache in path.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    workspace = Workspace(path, base, overlays)
    workspace.hashes = component_hashes(repo, base, workspace, target)
    return workspace


def component_hashes(repo: Repo, base: str, workspace: Workspace, target: str) -> dict[str, str]:
    """A hash for every part an evaluation depends on, from the files as built."""
    from .policy import dependencies

    root = workspace.path
    policy = load_policy(repo, base, target)
    brick_dir = root / "bricks" / target
    files = [p for p in brick_dir.rglob("*") if p.is_file()]
    by_class: dict[str, list[Path]] = {CODE: [], EVALUATION: [], HUMAN: []}
    for p in files:
        by_class[policy.classify(p.relative_to(brick_dir).as_posix())].append(p)
    hashes = {
        "code": tree_digest(brick_dir, by_class[CODE]),
        "evaluation": tree_digest(brick_dir, by_class[EVALUATION]),
        "human": tree_digest(brick_dir, by_class[HUMAN]),
        "framework": digest([file_digest(root / f) if (root / f).exists() else None for f in FRAMEWORK_FILES]),
        "environment": digest([file_digest(root / f) if (root / f).exists() else None for f in ENV_LOCKS]),
    }
    for name in dependencies(repo, base, target):
        dep = root / "bricks" / name
        hashes[f"dependency:{name}"] = tree_digest(dep, [p for p in dep.rglob("*") if p.is_file()])
    return hashes
