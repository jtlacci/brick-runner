"""The evaluation data store: payloads by SHA-256, record batches, datasets.

Layout under the store folder:

    payloads/<hex[:2]>/<hex>.json   one JSON value, named by sha256 of its canonical form
    records/<batch>.jsonl           records, with payload fields replaced by hashes
    datasets/<hex>.json             a sorted list of record IDs; its hash is the dataset version

The folder is a DVC project: ``sync()`` runs ``dvc add`` and ``dvc push`` so the
content is pinned in Git by DVC's pointer files and copied to the DVC remote.
DVC addresses files with MD5; the file names here are SHA-256, and the runner
uses only the SHA-256 names.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Iterable, Iterator

from .hashing import canonical, digest

PAYLOAD_FIELDS = ("input", "output", "request", "response")
DVC_TRACKED = ("payloads", "records", "datasets")


class StoreError(RuntimeError):
    pass


class Store:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        for folder in DVC_TRACKED:
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self._index: dict[str, dict[str, Any]] | None = None

    # ------------------------------------------------------------ payloads

    def put(self, value: Any) -> str:
        key = digest(value)
        path = self._payload_path(key)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(canonical(value))
            os.replace(temporary, path)
        return key

    def get(self, key: str) -> Any:
        path = self._payload_path(key)
        if not path.exists():
            raise StoreError(f"payload {key} is not in the store")
        data = path.read_bytes()
        if digest(json.loads(data)) != key:
            raise StoreError(f"payload {key} does not match its hash")
        return json.loads(data)

    def _payload_path(self, key: str) -> Path:
        if not key.startswith("sha256:") or len(key) != 71:
            raise StoreError(f"not a sha256 key: {key!r}")
        hexpart = key[7:]
        return self.root / "payloads" / hexpart[:2] / f"{hexpart}.json"

    # ------------------------------------------------------------ records

    def add_records(self, records: Iterable[dict[str, Any]], *, source: str = "live") -> list[str]:
        """Store records. Payload fields move to the payload store. Returns record IDs.

        A record's ID is the hash of its content, so adding the same record
        twice stores it once.
        """
        lines, ids = [], []
        index = self._load_index()
        for record in records:
            stored = {k: v for k, v in record.items() if k not in PAYLOAD_FIELDS}
            stored["source"] = source  # the caller decides, never the record
            if "time" in stored:
                stored["time"] = normalise_time(stored["time"])
            for field in PAYLOAD_FIELDS:
                if field in record:
                    stored[f"{field}_ref"] = self.put(record[field])
            stored.pop("id", None)
            stored["id"] = digest(stored)
            ids.append(stored["id"])
            if stored["id"] not in index:
                index[stored["id"]] = stored
                lines.append(json.dumps(stored, sort_keys=True))
        if lines:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            path = self.root / "records" / f"{stamp}-{os.getpid()}.jsonl"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ids

    def _load_index(self) -> dict[str, dict[str, Any]]:
        if self._index is None:
            self._index = {}
            for path in sorted((self.root / "records").glob("*.jsonl")):
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        record = json.loads(line)
                        self._index[record["id"]] = record
        return self._index

    def record(self, record_id: str, *, payloads: bool = True) -> dict[str, Any]:
        stored = self._load_index().get(record_id)
        if stored is None:
            raise StoreError(f"record {record_id} is not in the store")
        return self.expand(stored) if payloads else dict(stored)

    def expand(self, stored: dict[str, Any]) -> dict[str, Any]:
        record = {k: v for k, v in stored.items() if not k.endswith("_ref")}
        for field in PAYLOAD_FIELDS:
            if f"{field}_ref" in stored:
                record[field] = self.get(stored[f"{field}_ref"])
        return record

    def query(self, **match: Any) -> Iterator[dict[str, Any]]:
        """Records whose fields equal every given value, without payloads, in time order."""
        rows = [r for r in self._load_index().values() if all(r.get(k) == v for k, v in match.items())]
        yield from sorted(rows, key=lambda r: (r.get("time", ""), r["id"]))

    def exchanges(self, trace_id: str, within: set[str] | None = None) -> list[dict[str, Any]]:
        """A trace's exchanges; with ``within``, only those pinned in that ID set."""
        return [self.expand(r) for r in self.query(kind="exchange", trace_id=trace_id)
                if within is None or r["id"] in within]

    def between(self, start: str, end: str) -> list[dict[str, Any]]:
        """Records with start < time <= end, with payloads."""
        return [self.expand(r) for r in self._load_index().values() if start < r.get("time", "") <= end]

    # ------------------------------------------------------------ datasets

    def create_dataset(self, record_ids: Iterable[str]) -> str:
        """Pin runs, and every exchange recorded in their traces, as one dataset."""
        ids = set(record_ids)
        index = self._load_index()
        missing = [i for i in ids if i not in index]
        if missing:
            raise StoreError(f"{len(missing)} record IDs are not in the store")
        traces = {index[i].get("trace_id") for i in ids if index[i].get("kind") == "run"}
        sources = {index[i].get("source") for i in ids}
        ids |= {r["id"] for r in index.values()
                if r.get("kind") == "exchange" and r.get("trace_id") in traces and r.get("source") in sources}
        ids = sorted(ids)
        key = digest(ids)
        path = self.root / "datasets" / f"{key[7:]}.json"
        if not path.exists():
            path.write_bytes(canonical(ids))
        return key

    def dataset(self, key: str) -> list[str]:
        path = self.root / "datasets" / f"{key[7:]}.json"
        if not path.exists():
            raise StoreError(f"dataset {key} is not in the store")
        ids = json.loads(path.read_bytes())
        if digest(ids) != key:
            raise StoreError(f"dataset {key} does not match its hash")
        return ids

    # ------------------------------------------------------------ DVC

    def init_dvc(self) -> None:
        if not (self.root / ".git").exists():
            _run(["git", "init", "-q"], self.root)
        if not (self.root / ".dvc").exists():
            _run(["dvc", "init", "-q"], self.root)

    def sync(self, *, push: bool = True) -> None:
        """Pin the store in Git through DVC, and push it to the DVC remote if one is set."""
        if not shutil.which("dvc"):
            raise StoreError("dvc is not installed")
        self.init_dvc()
        _run(["dvc", "add", "-q", *DVC_TRACKED], self.root)
        _run(["git", "add", *(f"{name}.dvc" for name in DVC_TRACKED), ".gitignore"], self.root)
        status = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=self.root)
        if status.returncode:
            _run(["git", "-c", "user.name=brick-runner", "-c", "user.email=runner@localhost",
                  "commit", "-qm", "sync evaluation data"], self.root)
        remotes = subprocess.run(["dvc", "remote", "list"], cwd=self.root, capture_output=True, text=True)
        if push and remotes.stdout.strip():
            _run(["dvc", "push", "-q"], self.root)


def normalise_time(value: Any) -> str:
    """One time format, so times compare as strings: UTC ISO 8601 with microseconds."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise StoreError(f"not an ISO 8601 time: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _run(command: list[str], cwd: Path) -> None:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise StoreError(f"{' '.join(command)} failed: {result.stderr.strip()}")


def ingest_spool(store: Store, spool: Path) -> int:
    """Move captured records from a runtime spool folder into the store."""
    done = spool / "ingested"
    done.mkdir(exist_ok=True)
    count = 0
    for path in sorted(spool.glob("*.jsonl")):
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        count += len(store.add_records(records, source="live"))
        os.replace(path, done / path.name)
    return count
