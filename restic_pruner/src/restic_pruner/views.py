"""The JSON shape returned by the API and consumed by the web UI and MQTT
publisher."""

from __future__ import annotations

from typing import Any

from . import __version__
from .config import JOB_NAMES, JobName, RepositoryConfig, Settings
from .jobs import JobRunner
from .scheduler import Scheduler
from .state import RunRecord, RunStatus, StateStore


def run_view(run: RunRecord | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "job": run.job,
        "repository": run.repository,
        "status": str(run.status),
        "trigger": str(run.trigger),
        "dry_run": run.dry_run,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_seconds": run.duration_seconds,
        "error": run.error,
        "metrics": run.metrics,
    }


def job_view(job: JobName, settings: Settings, scheduler: Scheduler | None) -> dict[str, Any]:
    """Schedule-level facts, which are shared by every repository."""
    config = settings.job(job)
    next_run = scheduler.next_run(job) if scheduler else None
    return {
        "name": job,
        "enabled": config.enabled,
        "schedule": config.schedule,
        "next_run": next_run.isoformat() if next_run else None,
    }


def repository_job_view(
    job: JobName,
    settings: Settings,
    repository: RepositoryConfig,
    state: StateStore,
) -> dict[str, Any]:
    last = state.last_run(job, repository.slug)
    return {
        "last_run": run_view(last),
        "last_status": str(last.status) if last else "never",
        "last_success": _last_success_iso(state, job, repository.slug),
        "healthchecks_configured": bool(settings.healthchecks_url(repository, job)),
    }


def repository_view(
    settings: Settings,
    repository: RepositoryConfig,
    state: StateStore,
) -> dict[str, Any]:
    snapshot = state.repository(repository.slug)
    return {
        "name": repository.name,
        "slug": repository.slug,
        "target": redact_repository(repository.repository),
        "checked_at": snapshot.checked_at.isoformat() if snapshot.checked_at else None,
        "size_bytes": snapshot.size_bytes,
        "uncompressed_bytes": snapshot.uncompressed_bytes,
        "blob_count": snapshot.blob_count,
        "snapshot_count": snapshot.snapshot_count,
        "retention": {
            "keep_last": repository.retention.keep_last,
            "keep_hourly": repository.retention.keep_hourly,
            "keep_daily": repository.retention.keep_daily,
            "keep_weekly": repository.retention.keep_weekly,
            "keep_monthly": repository.retention.keep_monthly,
            "keep_yearly": repository.retention.keep_yearly,
            "keep_within": repository.retention.keep_within,
        },
        "jobs": {job: repository_job_view(job, settings, repository, state) for job in JOB_NAMES},
    }


def status_view(
    settings: Settings,
    state: StateStore,
    runner: JobRunner,
    scheduler: Scheduler | None = None,
) -> dict[str, Any]:
    current = runner.current
    return {
        "version": __version__,
        "running": current is not None,
        "current_run": run_view(current),
        "timezone": settings.timezone,
        "dry_run_default": settings.prune.dry_run,
        "jobs": {job: job_view(job, settings, scheduler) for job in JOB_NAMES},
        "repositories": [
            repository_view(settings, repository, state) for repository in settings.repositories
        ],
    }


def _last_success_iso(state: StateStore, job: JobName, repository: str) -> str | None:
    for run in state.runs(job, repository=repository):
        if run.status is RunStatus.SUCCESS and run.finished_at and not run.dry_run:
            return run.finished_at.isoformat()
    return None


def redact_repository(repository: str) -> str:
    """Never echo credentials that some backends allow inside the repo string."""
    if "@" in repository and "://" in repository:
        scheme, _, rest = repository.partition("://")
        _, _, host = rest.rpartition("@")
        return f"{scheme}://***@{host}"
    return repository
