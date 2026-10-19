"""Build the trees an evaluation runs in, from pinned commits only.

The base commit gives everything: the framework runtime, every other brick,
and every human-only file. Overlays then replace one class of one brick's
files (code or evaluation) with that class from another commit. Classes come
from the brick's policy on the BASE commit, so a candidate cannot change what
counts as its own files.

Each evaluation gets three separate trees, so no worker can touch another's:

| Tree | Contents | Used by |
| --- | --- | --- |
| code | base + every code overlay; no evaluation/ folders | the candidate worker |
| deps | base + code overlays of OTHER bricks; no evaluation/ folders | dependency workers |
| evaluation | base + evaluation overlays | the evaluator worker |
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import shutil
import tempfile

from .gitrepo import Repo
from .hashing import digest, file_digest, tree_digest
from .policy import CODE, EVALUATION, HUMAN, dependencies, load_policy

ENV_LOCKS = ("requirements.lock", "requirements.txt", "uv.lock", "poetry.lock")
FRAMEWORK_FILES = ("bricks/__init__.py", "bricks/_runtime.py", "bricks/_yaml.py")


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Overlay:
    brick: str
    kind: str  # CODE or EVALUATION
    commit: str


@dataclass
class Workspace:
    code: Path
    deps: Path
    evaluation: Path
    base: str
    overlays: list[Overlay]
    target: str
    hashes: dict[str, str] = field(default_factory=dict)
    owned: list[Path] = field(default_factory=list)

    def cleanup(self) -> None:
        for path in self.owned:
            shutil.rmtree(path, ignore_errors=True)

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *exc: object) -> None:
        self.cleanup()

    def with_code(self, code: Path) -> "Workspace":
        """The same evaluation over a different code tree (a mutant). Owns only that tree."""
        return replace(self, code=code, hashes=dict(self.hashes), owned=[code])


def _apply(repo: Repo, base: str, root: Path, overlay: Overlay) -> None:
    policy = load_policy(repo, base, overlay.brick)
    prefix = f"bricks/{overlay.brick}/"
    for name in repo.files(base, prefix):
        if policy.classify(name[len(prefix):]) == overlay.kind:
            (root / name).unlink(missing_ok=True)
    wanted = [name for name in repo.files(overlay.commit, prefix)
              if policy.classify(name[len(prefix):]) == overlay.kind]
    if wanted:
        repo.export(overlay.commit, root, wanted)


def _without_evaluation(root: Path) -> None:
    for folder in (root / "bricks").glob("*/evaluation"):
        shutil.rmtree(folder, ignore_errors=True)


def build(repo: Repo, base: str, overlays: list[Overlay], target: str) -> Workspace:
    parent = Path(tempfile.mkdtemp(prefix="brick-ws-"))
    pristine = parent / "base"
    repo.export(base, pristine)
    trees = {}
    for name, chosen in (
        ("code", [o for o in overlays if o.kind == CODE]),
        ("deps", [o for o in overlays if o.kind == CODE and o.brick != target]),
        ("evaluation", [o for o in overlays if o.kind == EVALUATION]),
    ):
        root = parent / name
        shutil.copytree(pristine, root)
        for overlay in chosen:
            _apply(repo, base, root, overlay)
        if name != "evaluation":
            _without_evaluation(root)
        for cache in root.rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
        trees[name] = root
    shutil.rmtree(pristine)
    workspace = Workspace(trees["code"], trees["deps"], trees["evaluation"], base, overlays, target, owned=[parent])
    workspace.hashes = component_hashes(repo, workspace)
    return workspace


def _classified(repo: Repo, workspace: Workspace, root: Path, kind: str) -> list[Path]:
    policy = load_policy(repo, workspace.base, workspace.target)
    brick = root / "bricks" / workspace.target
    return [p for p in brick.rglob("*") if p.is_file() and policy.classify(p.relative_to(brick).as_posix()) == kind]


def evaluation_hash(repo: Repo, workspace: Workspace) -> str:
    brick = workspace.evaluation / "bricks" / workspace.target
    return tree_digest(brick, _classified(repo, workspace, workspace.evaluation, EVALUATION))


def component_hashes(repo: Repo, workspace: Workspace) -> dict[str, str]:
    """A hash for every part an evaluation depends on, from the files as built."""
    target = workspace.target
    code_brick = workspace.code / "bricks" / target
    eval_brick = workspace.evaluation / "bricks" / target
    hashes = {
        "code": tree_digest(code_brick, _classified(repo, workspace, workspace.code, CODE)),
        "evaluation": evaluation_hash(repo, workspace),
        "human": tree_digest(eval_brick, _classified(repo, workspace, workspace.evaluation, HUMAN)),
        "framework": digest([file_digest(workspace.code / f) if (workspace.code / f).exists() else None
                             for f in FRAMEWORK_FILES]),
        "environment": digest([file_digest(workspace.code / f) if (workspace.code / f).exists() else None
                               for f in ENV_LOCKS]),
    }
    for name in dependencies(repo, workspace.base, target):
        dep = workspace.deps / "bricks" / name
        hashes[f"dependency:{name}"] = tree_digest(dep, [p for p in dep.rglob("*") if p.is_file()])
    return hashes


def verify_evaluation(repo: Repo, workspace: Workspace) -> None:
    """Refuse to score with evaluation files that changed since the workspace was built."""
    if evaluation_hash(repo, workspace) != workspace.hashes["evaluation"]:
        raise WorkspaceError("the evaluation files changed during the evaluation")
