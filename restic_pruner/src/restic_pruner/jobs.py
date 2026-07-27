"""Job orchestration.

A job run is a batch: every configured repository is maintained in turn, never in
parallel. Each produces its own run record, log and pair of healthchecks.io
pings, and a failure on one does not stop the others.

A single lock guards the whole batch; anything arriving mid-batch is rejected
rather than queued.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from .config import JobName, RepositoryConfig, Settings
from .healthchecks import HealthchecksClient
from .restic import RepoStats, Restic, ResticError
from .state import RepositorySnapshot, RunRecord, RunStatus, StateStore, Trigger, new_run_id

_LOGGER: Final = logging.getLogger(__name__)

#: How many log lines are kept in memory for the live tail.
LIVE_LOG_LINES: Final = 2000

ChangeCallback = Callable[[], Awaitable[None]]


class JobBusyError(RuntimeError):
    """Another job is already running."""


class JobDisabledError(RuntimeError):
    """The requested job is turned off in the configuration."""


class UnknownRepositoryError(LookupError):
    """No repository is configured under that name."""


class JobRunner:
    def __init__(
        self,
        settings: Settings,
        state: StateStore,
        healthchecks: HealthchecksClient,
        on_change: ChangeCallback | None = None,
    ) -> None:
        self._settings = settings
        self._state = state
        self._healthchecks = healthchecks
        self._on_change = on_change
        self._lock = asyncio.Lock()
        self._current: RunRecord | None = None
        self._log_lines: list[str] = []

    # -- introspection ---------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @property
    def current(self) -> RunRecord | None:
        return self._current

    def restic_for(self, repository: RepositoryConfig) -> Restic:
        """Build the restic wrapper for *repository*; overridden in tests."""
        return Restic(self._settings, repository)

    def live_log(self, offset: int = 0) -> tuple[int, list[str]]:
        """Return ``(next_offset, lines)`` for the run currently in progress."""
        offset = max(0, min(offset, len(self._log_lines)))
        return len(self._log_lines), self._log_lines[offset:]

    def log_for(self, run_id: str) -> str | None:
        if self._current is not None and self._current.id == run_id:
            return "\n".join(self._log_lines)
        return self._state.read_log(run_id)

    # -- running ---------------------------------------------------------

    def _select(self, repository: str | None) -> Sequence[RepositoryConfig]:
        if repository is None:
            return self._settings.repositories
        selected = self._settings.repository(repository)
        if selected is None:
            raise UnknownRepositoryError(f"no repository named {repository!r}")
        return (selected,)

    async def trigger(
        self,
        job: JobName,
        *,
        trigger: Trigger = Trigger.MANUAL,
        dry_run: bool | None = None,
        repository: str | None = None,
    ) -> list[RunRecord]:
        """Run *job* now, raising if it is off or something else is running."""
        if not self._settings.job(job).enabled:
            raise JobDisabledError(f"the {job} job is disabled in the configuration")
        targets = self._select(repository)
        if self.busy:
            raise JobBusyError("a restic job is already running")
        return await self.run(job, trigger=trigger, dry_run=dry_run, repositories=targets)

    async def run(
        self,
        job: JobName,
        *,
        trigger: Trigger = Trigger.SCHEDULE,
        dry_run: bool | None = None,
        repositories: Sequence[RepositoryConfig] | None = None,
    ) -> list[RunRecord]:
        """Maintain every selected repository, one after another."""
        targets = self._settings.repositories if repositories is None else repositories
        if self.busy:
            return self._record_skipped(job, trigger, targets)

        async with self._lock:
            if dry_run is None:
                dry_run = self._settings.prune.dry_run if job == "prune" else False
            records = [
                await self._execute(job, repository, trigger, dry_run) for repository in targets
            ]
            if len(targets) > 1:
                failed = [r.repository for r in records if r.status is not RunStatus.SUCCESS]
                _LOGGER.info(
                    "%s finished for %d repositories%s",
                    job,
                    len(records),
                    f"; failed: {', '.join(failed)}" if failed else "",
                )
            return records

    def _record_skipped(
        self, job: JobName, trigger: Trigger, targets: Sequence[RepositoryConfig]
    ) -> list[RunRecord]:
        _LOGGER.warning("Skipping scheduled %s run: another job is still running", job)
        now = datetime.now(UTC)
        records = []
        for repository in targets:
            record = RunRecord(
                id=new_run_id(),
                job=job,
                repository=repository.slug,
                status=RunStatus.SKIPPED,
                trigger=trigger,
                started_at=now,
                finished_at=now,
                error="skipped: another restic job was still running",
            )
            self._state.add(record)
            records.append(record)
        return records

    async def _execute(
        self,
        job: JobName,
        repository: RepositoryConfig,
        trigger: Trigger,
        dry_run: bool,
    ) -> RunRecord:
        run = RunRecord(
            id=new_run_id(),
            job=job,
            repository=repository.slug,
            status=RunStatus.RUNNING,
            trigger=trigger,
            started_at=datetime.now(UTC),
            dry_run=dry_run,
        )
        self._current = run
        self._log_lines = []
        self._state.add(run)
        await self._notify()

        ping_url = self._settings.healthchecks_url(repository, job)
        started = time.monotonic()
        self._emit(
            f"=== {job} run {run.id} on {repository.name} started at "
            f"{run.started_at.isoformat()}{' (dry run)' if dry_run else ''} ==="
        )
        if ping_url:
            await self._healthchecks.start(ping_url, rid=_ping_rid(run.id))

        restic = self.restic_for(repository).with_log(self._emit)
        try:
            metrics = (
                await self._run_prune(restic, repository, dry_run=dry_run)
                if job == "prune"
                else await self._run_check(restic, repository)
            )
        except asyncio.CancelledError:
            self._finish(run, RunStatus.FAILED, "cancelled: the add-on is shutting down")
            await self._report(ping_url, run, ok=False)
            await self._notify()
            raise
        except ResticError as exc:
            _LOGGER.error("%s run on %s failed: %s", job, repository.name, exc)
            self._emit(str(exc))
            self._finish(run, RunStatus.FAILED, str(exc))
            await self._report(ping_url, run, ok=False, exit_code=exc.exit_code)
            await self._notify()
            return run
        except Exception as exc:  # a job must never take the whole add-on down
            _LOGGER.exception("%s run on %s failed unexpectedly", job, repository.name)
            self._emit(f"unexpected error: {exc!r}")
            self._finish(run, RunStatus.FAILED, f"unexpected error: {exc}")
            await self._report(ping_url, run, ok=False)
            await self._notify()
            return run

        run.metrics = metrics
        self._emit(f"=== {job} run finished in {time.monotonic() - started:.1f}s ===")
        self._finish(run, RunStatus.SUCCESS, None)
        await self._report(ping_url, run, ok=True)
        await self._notify()
        return run

    async def _run_prune(
        self, restic: Restic, repository: RepositoryConfig, *, dry_run: bool
    ) -> dict[str, Any]:
        before = await restic.stats()
        result = await restic.forget_and_prune(dry_run=dry_run)
        after = await restic.stats()
        self._remember(repository, after)

        reclaimed = max(0, before.total_size - after.total_size)
        self._emit(
            f"reclaimed {_human_bytes(reclaimed)} "
            f"({_human_bytes(before.total_size)} -> {_human_bytes(after.total_size)})"
        )
        return {
            "snapshots_removed": result.removed,
            "snapshots_kept": result.kept,
            "snapshot_count": after.snapshots_count,
            "repo_size_before": before.total_size,
            "repo_size_after": after.total_size,
            "bytes_reclaimed": reclaimed,
            "pruned": result.pruned,
            "blobs_removed": result.blobs_removed,
            "blobs_repacked": result.blobs_repacked,
            "packs_removed": result.packs_removed,
            "prune_summary": result.summary,
        }

    async def _run_check(self, restic: Restic, repository: RepositoryConfig) -> dict[str, Any]:
        await restic.check()
        stats = await restic.stats()
        self._remember(repository, stats)
        return {
            "read_data_subset": self._settings.check.read_data_subset or "structure only",
            "snapshot_count": stats.snapshots_count,
            "repo_size_after": stats.total_size,
        }

    async def unlock(self, repository: str | None = None, *, remove_all: bool = False) -> None:
        """Remove stale repository locks. Manual action only, never scheduled."""
        targets = self._select(repository)
        if self.busy:
            raise JobBusyError("cannot unlock while a restic job is running")
        async with self._lock:
            for target in targets:
                await self.restic_for(target).with_log(self._emit).unlock(remove_all=remove_all)

    # -- helpers ---------------------------------------------------------

    def _remember(self, repository: RepositoryConfig, stats: RepoStats) -> None:
        self._state.set_repository(
            repository.slug,
            RepositorySnapshot(
                checked_at=datetime.now(UTC),
                size_bytes=stats.total_size,
                uncompressed_bytes=stats.total_uncompressed_size,
                blob_count=stats.total_blob_count,
                snapshot_count=stats.snapshots_count,
            ),
        )

    def _emit(self, line: str) -> None:
        _LOGGER.debug("restic: %s", line)
        self._log_lines.append(line)
        if len(self._log_lines) > LIVE_LOG_LINES:
            del self._log_lines[: len(self._log_lines) - LIVE_LOG_LINES]

    def _finish(self, run: RunRecord, status: RunStatus, error: str | None) -> None:
        run.status = status
        run.error = error
        run.finished_at = datetime.now(UTC)
        self._state.write_log(run.id, self._log_lines)
        self._state.update(run)
        self._current = None

    async def _report(
        self,
        ping_url: str,
        run: RunRecord,
        *,
        ok: bool,
        exit_code: int | None = None,
    ) -> None:
        if not ping_url:
            return
        body = "\n".join(self._log_lines)
        rid = _ping_rid(run.id)
        if ok:
            await self._healthchecks.success(ping_url, rid=rid, body=body)
        elif exit_code is not None:
            await self._healthchecks.exit_code(ping_url, exit_code, rid=rid, body=body)
        else:
            await self._healthchecks.fail(ping_url, rid=rid, body=body)

    async def _notify(self) -> None:
        if self._on_change is None:
            return
        try:
            await self._on_change()
        except Exception:  # publishing must never break a run
            _LOGGER.exception("state change callback failed")


def _ping_rid(run_id: str) -> str:
    """healthchecks expects the rid to be a UUID."""
    return f"{run_id[0:8]}-{run_id[8:12]}-{run_id[12:16]}-{run_id[16:20]}-{run_id[20:32]}"


def _human_bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"
