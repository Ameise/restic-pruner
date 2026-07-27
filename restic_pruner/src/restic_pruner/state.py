"""Persistent run history.

State lives in ``<data_dir>/state.json`` and is written atomically, so a power
cut in the middle of a write cannot leave the add-on unable to start. Full run
logs are kept as separate files under ``<data_dir>/logs`` and rotate together
with the history.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .config import JOB_NAMES, JobName

STATE_VERSION = 1


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class Trigger(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"
    STARTUP = "startup"


@dataclass(slots=True)
class RunRecord:
    id: str
    job: JobName
    #: Slug of the repository this run maintained.
    repository: str
    status: RunStatus
    trigger: Trigger
    started_at: datetime
    dry_run: bool = False
    finished_at: datetime | None = None
    error: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = str(self.status)
        payload["trigger"] = str(self.trigger)
        payload["started_at"] = self.started_at.isoformat()
        payload["finished_at"] = self.finished_at.isoformat() if self.finished_at else None
        payload["duration_seconds"] = self.duration_seconds
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RunRecord:
        job = payload.get("job")
        return cls(
            id=str(payload.get("id") or uuid.uuid4()),
            job=job if job in JOB_NAMES else "prune",
            repository=str(payload.get("repository") or "main"),
            status=RunStatus(payload.get("status", RunStatus.FAILED)),
            trigger=Trigger(payload.get("trigger", Trigger.SCHEDULE)),
            started_at=_parse_dt(payload.get("started_at")) or datetime.now(UTC),
            dry_run=bool(payload.get("dry_run", False)),
            finished_at=_parse_dt(payload.get("finished_at")),
            error=payload.get("error"),
            metrics=dict(payload.get("metrics") or {}),
        )


@dataclass(slots=True)
class RepositorySnapshot:
    """Last known facts about the repository itself."""

    checked_at: datetime | None = None
    size_bytes: int = 0
    uncompressed_bytes: int = 0
    blob_count: int = 0
    snapshot_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["checked_at"] = self.checked_at.isoformat() if self.checked_at else None
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RepositorySnapshot:
        return cls(
            checked_at=_parse_dt(payload.get("checked_at")),
            size_bytes=int(payload.get("size_bytes", 0)),
            uncompressed_bytes=int(payload.get("uncompressed_bytes", 0)),
            blob_count=int(payload.get("blob_count", 0)),
            snapshot_count=int(payload.get("snapshot_count", 0)),
        )


class StateStore:
    """Run history and repository facts, persisted as JSON."""

    def __init__(self, data_dir: Path, history_limit: int = 50) -> None:
        self._path = data_dir / "state.json"
        self._log_dir = data_dir / "logs"
        self._history_limit = max(1, history_limit)
        self._runs: list[RunRecord] = []
        self._repositories: dict[str, RepositorySnapshot] = {}

    @property
    def log_dir(self) -> Path:
        return self._log_dir

    def load(self) -> None:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        if not self._path.is_file():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt state file must never stop maintenance from running.
            return
        if not isinstance(payload, dict):
            return
        self._runs = [
            RunRecord.from_dict(item) for item in payload.get("runs", []) if isinstance(item, dict)
        ]
        repositories = payload.get("repositories")
        if isinstance(repositories, dict):
            self._repositories = {
                str(slug): RepositorySnapshot.from_dict(value)
                for slug, value in repositories.items()
                if isinstance(value, dict)
            }
        # A run left in RUNNING state means we were killed mid-flight.
        for run in self._runs:
            if run.status is RunStatus.RUNNING:
                run.status = RunStatus.FAILED
                run.error = "interrupted: the add-on stopped while this run was in progress"
                run.finished_at = run.finished_at or run.started_at

    def save(self) -> None:
        payload = {
            "version": STATE_VERSION,
            "runs": [run.to_dict() for run in self._runs],
            "repositories": {
                slug: snapshot.to_dict() for slug, snapshot in self._repositories.items()
            },
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # -- runs ------------------------------------------------------------

    def add(self, run: RunRecord) -> None:
        self._runs.insert(0, run)
        self._trim()
        self.save()

    def update(self, run: RunRecord) -> None:
        for index, existing in enumerate(self._runs):
            if existing.id == run.id:
                self._runs[index] = run
                break
        else:
            self._runs.insert(0, run)
        self._trim()
        self.save()

    def runs(
        self,
        job: JobName | None = None,
        limit: int | None = None,
        repository: str | None = None,
    ) -> list[RunRecord]:
        selected = [
            run
            for run in self._runs
            if (job is None or run.job == job)
            and (repository is None or run.repository == repository)
        ]
        return selected if limit is None else selected[:limit]

    def get(self, run_id: str) -> RunRecord | None:
        return next((run for run in self._runs if run.id == run_id), None)

    def last_run(
        self, job: JobName, repository: str, *, only_finished: bool = True
    ) -> RunRecord | None:
        for run in self._runs:
            if run.job != job or run.repository != repository:
                continue
            if only_finished and run.status is RunStatus.RUNNING:
                continue
            return run
        return None

    # -- repositories ----------------------------------------------------

    def repository(self, slug: str) -> RepositorySnapshot:
        return self._repositories.get(slug, RepositorySnapshot())

    def set_repository(self, slug: str, snapshot: RepositorySnapshot) -> None:
        self._repositories[slug] = snapshot

    # -- logs ------------------------------------------------------------

    def log_path(self, run_id: str) -> Path:
        return self._log_dir / f"{run_id}.log"

    def write_log(self, run_id: str, lines: Iterable[str]) -> None:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path(run_id).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def read_log(self, run_id: str) -> str | None:
        path = self.log_path(run_id)
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _trim(self) -> None:
        del self._runs[self._history_limit :]
        keep = {run.id for run in self._runs}
        if not self._log_dir.is_dir():
            return
        for path in self._log_dir.glob("*.log"):
            if path.stem not in keep:
                path.unlink(missing_ok=True)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def new_run_id() -> str:
    return uuid.uuid4().hex
