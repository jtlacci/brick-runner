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

    @property
    def mixed(self) -> bool:
        """The framework rule: one change is code-loop or evaluation-loop, never both."""
        return bool(self.code and self.evaluation)

    def as_dict(self) -> dict:
        return {"code": self.code, "evaluation": self.evaluation, "human": self.human,
                "new_bricks": self.new_bricks, "mixed": self.mixed}


def changed_files(repo: Repo, base: str, head: str) -> list[str]:
    out = str(repo.git("diff", "--name-only", "--no-renames", f"{base}...{head}"))
    return sorted({line for line in out.splitlines() if line})


def make_plan(repo: Repo, base: str, head: str) -> Plan:
    plan = Plan()
    code, evaluation, new = set(), set(), set()
    policies: dict[str, object] = {}
    for path in changed_files(repo, base, head):
        parts = path.split("/")
        if len(parts) < 3 or parts[0] != "bricks" or parts[1].startswith(("_", ".")):
            plan.human.append(path)
            continue
        brick, inside = parts[1], "/".join(parts[2:])
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
    return plan
