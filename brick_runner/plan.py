"""Decide what a change asks the runner to do, from the policy on the BASE commit."""

from __future__ import annotations

from dataclasses import dataclass, field

from .gitrepo import Repo
from .policy import CODE, EVALUATION, HUMAN, PolicyError, load_policy


@dataclass
class Plan:
    code: list[str] = field(default_factory=list)          # bricks whose code-loop files changed
    evaluation: list[str] = field(default_factory=list)    # bricks whose evaluation-loop files changed
    human: list[str] = field(default_factory=list)         # human-only paths (need CODEOWNERS approval)
    new_bricks: list[str] = field(default_factory=list)    # bricks with no valid policy on the base
    removed_bricks: list[str] = field(default_factory=list)  # bricks with no file left on the head

    @property
    def mixed(self) -> bool:
        """The framework rule: one change is code-loop or evaluation-loop, never both."""
        return bool(self.code and self.evaluation)

    def as_dict(self) -> dict:
        return {"code": self.code, "evaluation": self.evaluation, "human": self.human,
                "new_bricks": self.new_bricks, "removed_bricks": self.removed_bricks, "mixed": self.mixed}


def changed_files(repo: Repo, base: str, head: str) -> list[str]:
    # -z: names exactly as stored, never C-quoted, so a non-ASCII name is classified like any other.
    # --no-renames: a rename is a deletion and an addition, and both paths count.
    out = str(repo.git("diff", "--name-only", "-z", "--no-renames", f"{base}...{head}"))
    return sorted({name for name in out.split("\0") if name})


def make_plan(repo: Repo, base: str, head: str) -> Plan:
    plan = Plan()
    code, evaluation, new, removed = set(), set(), set(), set()
    policies: dict[str, object] = {}
    gone: dict[str, bool] = {}
    for path in changed_files(repo, base, head):
        parts = path.split("/")
        if len(parts) < 3 or parts[0] != "bricks" or parts[1].startswith(("_", ".")):
            plan.human.append(path)
            continue
        brick, inside = parts[1], "/".join(parts[2:])
        # The framework rule: a brick removed whole (its contract too) is a
        # human change. It belongs to neither loop and has nothing to evaluate.
        if brick not in gone:
            gone[brick] = not str(repo.git("ls-tree", "-r", "--name-only", head, "--", f"bricks/{brick}/")).strip()
        if gone[brick]:
            removed.add(brick)
            plan.human.append(path)
            continue
        if brick not in policies:
            try:
                policies[brick] = load_policy(repo, base, brick)
            except PolicyError:
                policies[brick] = None
        policy = policies[brick]
        if policy is None:
            new.add(brick)
            plan.human.append(path)
            continue
        label = policy.classify(inside)  # type: ignore[attr-defined]
        if label == CODE:
            code.add(brick)
        elif label == EVALUATION:
            evaluation.add(brick)
        else:
            assert label == HUMAN
            plan.human.append(path)
    plan.code, plan.evaluation, plan.new_bricks = sorted(code), sorted(evaluation), sorted(new)
    plan.removed_bricks = sorted(removed)
    return plan
