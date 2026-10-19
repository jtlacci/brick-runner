"""Read a brick's evaluation policy and contract from a pinned commit, never from a work tree."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

import yaml

from .gitrepo import Repo

ALWAYS_HUMAN = ("contract.py", "evaluation/policy.yml", "evaluation/data.lock", "evaluation/AGENTS.md")
CODE, EVALUATION, HUMAN = "code", "evaluation", "human"


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    raw: dict[str, Any]

    @property
    def split(self) -> dict[str, Any]:
        return self.raw["split"]

    @property
    def metric(self) -> str:
        return self.raw["selection"]["metric"]

    @property
    def method(self) -> str:
        return self.raw["selection"]["method"]

    @property
    def seeds(self) -> list[int]:
        return list(self.raw["seeds"])

    @property
    def horizon(self) -> float:
        return float(self.raw.get("label_horizon_seconds", 0))

    def classify(self, path: str) -> str:
        if path in ALWAYS_HUMAN:
            return HUMAN
        surfaces = self.raw.get("surfaces", {})
        for loop, label in (("code_loop", CODE), ("eval_loop", EVALUATION)):
            for name in self.raw.get(loop, []):
                if any(fnmatchcase(path, p.replace("**", "*")) for p in surfaces.get(name, [])):
                    return label
        return HUMAN


def load_policy(repo: Repo, commit: str, brick: str) -> Policy:
    text = repo.show(commit, f"bricks/{brick}/evaluation/policy.yml")
    if text is None:
        raise PolicyError(f"bricks/{brick} has no evaluation policy at {commit[:12]}")
    raw = yaml.safe_load(text)
    for key in ("surfaces", "code_loop", "eval_loop", "split", "selection", "seeds"):
        if key not in (raw or {}):
            raise PolicyError(f"policy for {brick} at {commit[:12]} lacks {key!r}; run the framework linter")
    if raw["selection"].get("method") != "best":
        raise PolicyError(f"selection method {raw['selection'].get('method')!r} is not supported")
    return Policy(raw)


def load_lock(repo: Repo, commit: str, brick: str) -> str:
    text = repo.show(commit, f"bricks/{brick}/evaluation/data.lock")
    return (text or "unpinned").strip()


def contract_literal(repo: Repo, commit: str, brick: str, name: str, default: Any = None) -> Any:
    text = repo.show(commit, f"bricks/{brick}/contract.py")
    if text is None:
        return default
    for node in ast.parse(text).body:
        target = None
        if isinstance(node, ast.Assign):
            target = next((t for t in node.targets if isinstance(t, ast.Name) and t.id == name), None)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            target = node.target
        if target is not None and node.value is not None:
            try:
                return ast.literal_eval(node.value)
            except ValueError:
                return default
    return default


def bricks_at(repo: Repo, commit: str) -> list[str]:
    names = set()
    for path in repo.files(commit, "bricks"):
        parts = path.split("/")
        if len(parts) > 2 and not parts[1].startswith(("_", ".")):
            names.add(parts[1])
    return sorted(names)


def dependencies(repo: Repo, commit: str, brick: str) -> list[str]:
    """The brick's sibling dependencies, transitively, from its contract."""
    seen: list[str] = []
    stack = [brick]
    while stack:
        current = stack.pop()
        for name in sorted((contract_literal(repo, commit, current, "SIBLING_DEPENDENCIES", {}) or {})):
            if name not in seen and name != brick:
                seen.append(name)
                stack.append(name)
    return sorted(seen)


def dependents(repo: Repo, commit: str, brick: str) -> list[str]:
    """Bricks that declare this brick as a sibling dependency."""
    return [name for name in bricks_at(repo, commit)
            if brick in (contract_literal(repo, commit, name, "SIBLING_DEPENDENCIES", {}) or {})]
