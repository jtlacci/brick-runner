"""Assign records to development, validation and holdout, by the policy's rule."""

from __future__ import annotations

import hashlib
from typing import Any

DEVELOPMENT, VALIDATION, HOLDOUT = "development", "validation", "holdout"
SPLITS = (DEVELOPMENT, VALIDATION, HOLDOUT)


def _position(record_id: str) -> float:
    return int(hashlib.sha256(record_id.encode()).hexdigest()[:12], 16) / float(16 ** 12)


def by_hash(records: list[dict[str, Any]], validation: float, holdout: float) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {name: [] for name in SPLITS}
    for record in records:
        position = _position(record["id"])
        name = HOLDOUT if position < holdout else VALIDATION if position < holdout + validation else DEVELOPMENT
        out[name].append(record)
    return out


def assign(records: list[dict[str, Any]], split: dict[str, Any], freeze: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Split records. ``freeze`` is the candidate's freeze time, needed for the time rule.

    Synthetic records always use the hash rule, because their times are not real.
    """
    synthetic = [r for r in records if r.get("source") == "synthetic"]
    real = [r for r in records if r.get("source") != "synthetic"]
    holdout_share = split.get("holdout", 0.2) if split["kind"] == "hash" else 0.2
    out = by_hash(synthetic, split["validation"], holdout_share)
    if split["kind"] == "hash":
        extra = by_hash(real, split["validation"], split["holdout"])
    else:
        before = sorted((r for r in real if freeze is None or r["time"] <= freeze), key=lambda r: (r["time"], r["id"]))
        cut = len(before) - int(round(len(before) * split["validation"]))
        extra = {
            DEVELOPMENT: before[:cut],
            VALIDATION: before[cut:],
            HOLDOUT: [r for r in real if freeze is not None and r["time"] > freeze],
        }
    for name in SPLITS:
        out[name].extend(extra[name])
    return out
