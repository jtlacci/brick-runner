"""A small YAML subset, the same as the framework's bricks/_yaml.py (vendored, kept in step).

Standard library only. It reads what this framework writes and nothing more:

- comments (``#`` outside quotes) and blank lines
- ``key: value`` and ``key:`` followed by an indented block
- block lists of scalars (``- item``)
- flow lists ``[a, b]`` and flow maps ``{k: v}`` of scalars
- scalars: integers, floats, ``true``/``false``, ``null``, quoted or bare strings

Anything else is an error, never a guess.
"""

from __future__ import annotations

import re
from typing import Any


class YamlError(ValueError):
    """The text is outside the supported subset."""


_INT = re.compile(r"^[-+]?\d+$")
_FLOAT = re.compile(r"^[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$")


def loads(text: str) -> dict[str, Any]:
    lines = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise YamlError(f"line {number}: tabs are not allowed for indentation")
        lines.append((number, len(line) - len(line.lstrip(" ")), line.strip()))
    value, index = _block(lines, 0, 0)
    if index != len(lines):
        raise YamlError(f"line {lines[index][0]}: unexpected indentation")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise YamlError("the document must be a mapping")
    return value


def _strip_comment(line: str) -> str:
    quote = None
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


def _block(lines: list[tuple[int, int, str]], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines) or lines[index][1] < indent:
        return None, index
    indent = lines[index][1]
    if lines[index][2].startswith("- ") or lines[index][2] == "-":
        items = []
        while index < len(lines) and lines[index][1] == indent and lines[index][2].startswith("-"):
            number, _, text = lines[index]
            item = text[1:].strip()
            if not item:
                raise YamlError(f"line {number}: nested block lists are not supported")
            items.append(_scalar_or_flow(item, number))
            index += 1
        return items, index
    mapping: dict[str, Any] = {}
    while index < len(lines) and lines[index][1] == indent:
        number, _, text = lines[index]
        if ":" not in text:
            raise YamlError(f"line {number}: expected 'key: value'")
        key, _, rest = text.partition(":")
        key = _unquote(key.strip(), number)
        if key in mapping:
            raise YamlError(f"line {number}: duplicate key {key!r}")
        rest = rest.strip()
        index += 1
        if rest:
            mapping[key] = _scalar_or_flow(rest, number)
        else:
            child, index = _block(lines, index, indent + 1)
            mapping[key] = child
    return mapping, index


def _scalar_or_flow(text: str, number: int) -> Any:
    if text.startswith("["):
        if not text.endswith("]"):
            raise YamlError(f"line {number}: unclosed flow list")
        return [_scalar(part, number) for part in _split(text[1:-1], number)]
    if text.startswith("{"):
        if not text.endswith("}"):
            raise YamlError(f"line {number}: unclosed flow map")
        result: dict[str, Any] = {}
        for part in _split(text[1:-1], number):
            key, sep, value = part.partition(":")
            if not sep:
                raise YamlError(f"line {number}: expected 'key: value' in flow map")
            result[_unquote(key.strip(), number)] = _scalar(value.strip(), number)
        return result
    return _scalar(text, number)


def _split(text: str, number: int) -> list[str]:
    parts, current, quote = [], "", None
    for char in text:
        if quote:
            current += char
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
            current += char
        elif char in "[]{}":
            raise YamlError(f"line {number}: nested flow collections are not supported")
        elif char == ",":
            parts.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        parts.append(current.strip())
    return parts


def _scalar(text: str, number: int) -> Any:
    if not text:
        return None
    if text[0] in "\"'":
        return _unquote(text, number)
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "~"):
        return None
    if _INT.match(text):
        return int(text)
    if _FLOAT.match(text):
        return float(text)
    return text


def _unquote(text: str, number: int) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text[:1] in "\"'":
        raise YamlError(f"line {number}: unclosed quote")
    return text
