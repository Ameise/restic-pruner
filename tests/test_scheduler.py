from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

from restic_pruner.config import JobName, RepositoryConfig, Settings
from restic_pruner.healthchecks import HealthchecksClient
from restic_pruner.jobs import JobRunner
from restic_pruner.scheduler import Scheduler
from restic_pruner.state import RunRecord, RunStatus, StateStore, Trigger, new_run_id


class RecordingRunner(JobRunner):
    """A runner that records what the scheduler asked for instead of running it."""

    def __init__(self, settings: Settings, state: StateStore, hc: HealthchecksClient) -> None:
        super().__init__(settings, state, hc)
        self.calls: list[tuple[JobName, Trigger]] = []

    async def run(
        self,
        job: JobName,
        *,
        trigger: Trigger = Trigger.SCHEDULE,
        dry_run: bool | None = None,
        repositories: Sequence[RepositoryConfig] | None = None,
    ) -> list[RunRecord]:
        self.calls.append((job, trigger))
        now = datetime.now(UTC)
        return [
            RunRecord(
                id=new_run_id(),
                job=job,
                repository=repository.slug,
                status=RunStatus.SUCCESS,
                trigger=trigger,
                started_at=now,
                finished_at=now,
            )
            for repository in (repositories or self._settings.repositories)
        ]


async def _scheduler(settings: Settings, tmp_path: Path) -> tuple[Scheduler, RecordingRunner]:
    state = StateStore(tmp_path)
    state.load()
    session = aiohttp.ClientSession()
    runner = RecordingRunner(settings, state, HealthchecksClient(session))
    await session.close()
    return Scheduler(settings, runner), runner


async def test_next_run_is_in_the_future(settings: Settings, tmp_path: Path) -> None:
    scheduler, _ = await _scheduler(settings, tmp_path)
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    scheduler.prime(now)
    next_prune = scheduler.next_run("prune")
    assert next_prune is not None
    assert next_prune > now
    # "0 3 * * 0" -- the next Sunday at 03:00.
    assert next_prune.weekday() == 6
    assert (next_prune.hour, next_prune.minute) == (3, 0)


async def test_disabled_jobs_are_never_scheduled(settings: Settings, tmp_path: Path) -> None:
    configured = dataclasses.replace(
        settings, check=dataclasses.replace(settings.check, enabled=False)
    )
    scheduler, _ = await _scheduler(configured, tmp_path)
    scheduler.prime(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    assert scheduler.next_run("check") is None
    assert scheduler.next_run("prune") is not None


async def test_due_jobs_fire_once_and_reschedule(settings: Settings, tmp_path: Path) -> None:
    scheduler, runner = await _scheduler(settings, tmp_path)
    scheduler.prime(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))

    # Sunday 03:00, the moment the prune job is due.
    due_at = datetime(2026, 7, 26, 3, 0, tzinfo=UTC)
    assert await scheduler.tick(due_at) == ["prune"]
    assert runner.calls == [("prune", Trigger.SCHEDULE)]

    # A second tick at the same instant must not run it again.
    assert await scheduler.tick(due_at) == []
    assert len(runner.calls) == 1
    next_prune = scheduler.next_run("prune")
    assert next_prune is not None and next_prune > due_at


async def test_nothing_fires_before_its_time(settings: Settings, tmp_path: Path) -> None:
    scheduler, runner = await _scheduler(settings, tmp_path)
    scheduler.prime(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    assert await scheduler.tick(datetime(2026, 7, 25, 23, 59, tzinfo=UTC)) == []
    assert runner.calls == []


async def test_schedules_follow_the_configured_timezone(settings: Settings, tmp_path: Path) -> None:
    berlin = dataclasses.replace(settings, timezone="Europe/Berlin")
    scheduler, _ = await _scheduler(berlin, tmp_path)
    scheduler.prime(datetime(2026, 7, 24, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")))
    next_prune = scheduler.next_run("prune")
    assert next_prune is not None
    # 03:00 local, and in July that is CEST, i.e. 01:00 UTC.
    assert next_prune.hour == 3
    assert next_prune.utcoffset() == timedelta(hours=2)
