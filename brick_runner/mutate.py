"""Controlled defects: seeded AST mutations of a brick's implementation.

A mutation of strength k applies k independent changes chosen from every
mutation point in the brick's code folders (``src/`` and ``input/adapters/``):
comparison flips, arithmetic swaps, boolean flips, numeric shifts and string
changes (field names, request keys). Docstrings are never touched. The same
seed always gives the same mutant.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import random

CODE_FOLDERS = ("src", "input/adapters")
COMPARE_FLIPS = {ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE, ast.LtE: ast.Gt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq}
BINOP_SWAPS = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult}


@dataclass(frozen=True)
class Point:
    file: str
    index: int  # the n-th mutable node in that file, in ast.walk order


def _docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def _nodes(tree: ast.AST) -> list[ast.AST]:
    skip = _docstrings(tree)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and type(node.ops[0]) in COMPARE_FLIPS:
            out.append(node)
        elif isinstance(node, ast.BinOp) and type(node.op) in BINOP_SWAPS:
            out.append(node)
        elif isinstance(node, ast.Constant) and id(node) not in skip and isinstance(node.value, (bool, int, float, str)):
            out.append(node)
    return out


def _files(brick: Path) -> list[Path]:
    return sorted(p for folder in CODE_FOLDERS if (brick / folder).is_dir() for p in (brick / folder).rglob("*.py"))


def points(brick: Path) -> list[Point]:
    found = []
    for path in _files(brick):
        count = len(_nodes(ast.parse(path.read_text(encoding="utf-8"))))
        found += [Point(path.relative_to(brick).as_posix(), i) for i in range(count)]
    return found


def _apply(node: ast.AST, rng: random.Random) -> None:
    if isinstance(node, ast.Compare):
        node.ops[0] = COMPARE_FLIPS[type(node.ops[0])]()
    elif isinstance(node, ast.BinOp):
        node.op = BINOP_SWAPS[type(node.op)]()
    elif isinstance(node.value, bool):
        node.value = not node.value
    elif isinstance(node.value, int):
        node.value = node.value + rng.choice((-1, 1)) * max(1, abs(node.value) // 2)
    elif isinstance(node.value, float):
        node.value = node.value * rng.choice((0.5, 1.5)) + rng.choice((-1.0, 1.0)) * 0.1
    else:
        node.value = node.value + "_mutated"


def mutate(brick: Path, strength: int, seed: int) -> list[Point]:
    """Rewrite the brick's code folders in place with ``strength`` mutations."""
    rng = random.Random(seed)
    candidates = points(brick)
    if not candidates:
        return []
    chosen = rng.sample(candidates, min(strength, len(candidates)))
    for file in sorted({p.file for p in chosen}):
        path = brick / file
        tree = ast.parse(path.read_text(encoding="utf-8"))
        nodes = _nodes(tree)
        for index in sorted(p.index for p in chosen if p.file == file):
            _apply(nodes[index], rng)
        path.write_text(ast.unparse(tree) + "\n", encoding="utf-8")
    return chosen
