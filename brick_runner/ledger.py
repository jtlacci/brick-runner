"""The trial ledger, kept in MLflow.

One MLflow experiment per brick holds every trial (one MLflow run each),
every evaluation-loop report and every promotion. Tags carry the identities;
metrics carry the term means; the full result envelope is an artifact.

MLflow lets runs be deleted, so the runner must be the only writer of the
tracking store. Agents get no access to it.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

import mlflow
from mlflow.tracking import MlflowClient

ENVELOPE = "envelope.json"


@dataclass
class Entry:
    run_id: str
    tags: dict[str, str]
    metrics: dict[str, float]
    start_time: int

    def tag(self, name: str, default: str = "") -> str:
        return self.tags.get(name, default)


class Ledger:
    def __init__(self, home: Path) -> None:
        home = Path(home)
        home.mkdir(parents=True, exist_ok=True)
        self.uri = f"sqlite:///{home / 'mlflow.db'}"
        self.artifacts = (home / "mlartifacts").resolve()
        os.environ.setdefault("MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR", "false")
        os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
        self.client = MlflowClient(tracking_uri=self.uri)

    def _experiment(self, brick: str) -> str:
        name = f"brick:{brick}"
        found = self.client.get_experiment_by_name(name)
        if found:
            return found.experiment_id
        return self.client.create_experiment(name, artifact_location=(self.artifacts / brick).as_uri())

    def write(self, brick: str, kind: str, tags: dict[str, Any], metrics: dict[str, float],
              envelope: dict[str, Any]) -> str:
        run = self.client.create_run(self._experiment(brick), tags={"kind": kind, **{k: str(v) for k, v in tags.items()}})
        for key, value in metrics.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.client.log_metric(run.info.run_id, _metric_name(key), float(value))
        self.client.log_dict(run.info.run_id, envelope, ENVELOPE)
        self.client.set_terminated(run.info.run_id)
        return run.info.run_id

    def find(self, brick: str, **tags: str) -> list[Entry]:
        """Entries whose tags equal every given value, newest first."""
        clauses = " and ".join(f"tags.{k} = '{_quote(v)}'" for k, v in tags.items())
        runs = self.client.search_runs([self._experiment(brick)], filter_string=clauses,
                                       order_by=["attributes.start_time DESC"], max_results=5000)
        return [Entry(r.info.run_id, dict(r.data.tags), dict(r.data.metrics), r.info.start_time) for r in runs]

    def envelope(self, run_id: str) -> dict[str, Any]:
        path = self.client.download_artifacts(run_id, ENVELOPE)
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def set_tag(self, run_id: str, name: str, value: str) -> None:
        self.client.set_tag(run_id, name, value)


def _quote(value: str) -> str:
    return str(value).replace("'", "\\'")


def _metric_name(term: str) -> str:
    return "".join(c if c.isalnum() or c in "_-. /" else "_" for c in term)
