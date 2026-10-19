"""The framework's policy check (tools/policy.py), vendored and kept in step.

The linter checks a policy's shape; the change check uses it to classify each
changed file as code-loop, evaluation-loop or human-only.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
import sys
from typing import Any


from . import yaml_subset as _yaml

POLICY_PATH = "evaluation/policy.yml"
# Paths no loop may change, whatever the surfaces say.
ALWAYS_HUMAN = ("contract.py", POLICY_PATH, "evaluation/data.lock", "evaluation/AGENTS.md")
SPLIT_KINDS = {"hash", "time"}
SELECTION_METHODS = {"best"}
CODE, EVALUATION, HUMAN = "code", "evaluation", "human"


def load(text: str) -> dict[str, Any]:
    return _yaml.loads(text)


def matches(pattern: str, path: str) -> bool:
    """``*`` and ``**`` both match across folders."""
    return fnmatchcase(path, pattern.replace("**", "*"))


def check(policy: Any) -> list[str]:
    """Every problem with a policy's shape, as messages."""
    if not isinstance(policy, dict):
        return ["policy must be a mapping"]
    errors: list[str] = []
    surfaces = policy.get("surfaces")
    if not isinstance(surfaces, dict) or not surfaces or not all(
        isinstance(name, str) and isinstance(patterns, list) and patterns
        and all(isinstance(p, str) and p for p in patterns)
        for name, patterns in surfaces.items()
    ):
        errors.append("surfaces must map names to non-empty lists of path patterns")
        surfaces = {}
    loops = {}
    for loop in ("code_loop", "eval_loop"):
        names = policy.get(loop)
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            errors.append(f"{loop} must list surface names")
            names = []
        for name in names:
            if name not in surfaces:
                errors.append(f"{loop} names unknown surface {name!r}")
        loops[loop] = set(names)
    if loops["code_loop"] & loops["eval_loop"]:
        errors.append("code_loop and eval_loop may not share a surface")
    for name in loops["code_loop"]:
        for pattern in surfaces.get(name, []):
            if matches(pattern, "evaluation/evaluate.py") or pattern.startswith("evaluation"):
                errors.append(f"code_loop surface {name!r} reaches evaluation/ through {pattern!r}")
    for name, patterns in surfaces.items():
        for pattern in patterns:
            for path in ALWAYS_HUMAN:
                if matches(pattern, path):
                    errors.append(f"surface {name!r} reaches human-only {path} through {pattern!r}")

    split = policy.get("split")
    if not isinstance(split, dict) or split.get("kind") not in SPLIT_KINDS:
        errors.append(f"split.kind must be one of {', '.join(sorted(SPLIT_KINDS))}")
    else:
        validation = split.get("validation")
        if not _share(validation):
            errors.append("split.validation must be a number between 0 and 1")
        if split["kind"] == "hash":
            holdout = split.get("holdout")
            if not _share(holdout):
                errors.append("split.holdout must be a number between 0 and 1")
            elif _share(validation) and validation + holdout >= 1:
                errors.append("split.validation and split.holdout must leave a development share")
        minimum = split.get("min_holdout")
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
            errors.append("split.min_holdout must be a positive integer")

    selection = policy.get("selection")
    if not isinstance(selection, dict) or selection.get("method") not in SELECTION_METHODS:
        errors.append(f"selection.method must be one of {', '.join(sorted(SELECTION_METHODS))}")
    elif not isinstance(selection.get("metric"), str) or not selection["metric"]:
        errors.append("selection.metric must name a score term")

    seeds = policy.get("seeds")
    if not isinstance(seeds, list) or not seeds or not all(
        isinstance(s, int) and not isinstance(s, bool) for s in seeds
    ):
        errors.append("seeds must be a non-empty list of integers")

    cap = policy.get("max_out_of_evidence", 0.05)
    if not isinstance(cap, (int, float)) or isinstance(cap, bool) or not 0 <= cap < 1:
        errors.append("max_out_of_evidence must be a share from 0 up to (not including) 1")

    horizon = policy.get("label_horizon_seconds", 0)
    if not isinstance(horizon, (int, float)) or isinstance(horizon, bool) or horizon < 0:
        errors.append("label_horizon_seconds must be a number of seconds, 0 or more")
    return errors


def _share(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < 1


def classify(policy: dict[str, Any], path: str) -> str:
    """The loop a path inside a brick belongs to: code, evaluation or human."""
    if path in ALWAYS_HUMAN:
        return HUMAN
    surfaces = policy.get("surfaces", {})
    for loop, label in (("code_loop", CODE), ("eval_loop", EVALUATION)):
        for name in policy.get(loop, []):
            if any(matches(pattern, path) for pattern in surfaces.get(name, [])):
                return label
    return HUMAN
