"""Cron scheduling for the prune and check jobs.

Fire times are evaluated in the container's local timezone (Home Assistant sets
``TZ`` for add-ons), including across daylight saving changes.

Runs missed while the add-on was stopped are not caught up; the job waits for
its next scheduled slot.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Final

from croniter import croniter

from .config import JOB_NAMES, JobName, Settings
from .jobs import JobRunner
from .state import Trigger

_LOGGER: Final = logging.getLogger(__name__)

#: Poll interval. Cron has minute resolution, so this is plenty precise and
#: keeps the "next run" countdown in the UI honest.
TICK_SECONDS: Final = 15.0


class Scheduler:
    def __init__(self, settings: Settings, runner: JobRunner) -> None:
        self._settings = settings
        self._runner = runner
        self._tz = settings.tzinfo()
        self._next: dict[JobName, datetime] = {}

    def prime(self, now: datetime | None = None) -> None:
        """Compute the first fire time for every enabled job."""
        reference = now or datetime.now(self._tz)
        self._next = {
            job: self._compute_next(job, reference)
            for job in JOB_NAMES
            if self._settings.job(job).enabled
        }
        for job, when in self._next.items():
            _LOGGER.info("Next %s run: %s", job, when.isoformat())

    def next_run(self, job: JobName) -> datetime | None:
        return self._next.get(job)

    def next_runs(self) -> dict[str, str | None]:
        return {
            job: (self._next[job].isoformat() if job in self._next else None) for job in JOB_NAMES
        }

    def _compute_next(self, job: JobName, reference: datetime) -> datetime:
        schedule = self._settings.job(job).schedule
        iterator = croniter(schedule, reference)
        result = iterator.get_next(datetime)
        assert isinstance(result, datetime)
        return result

    async def run_forever(self) -> None:
        if not self._next:
            self.prime()
        while True:
            await asyncio.sleep(TICK_SECONDS)
            await self.tick()

    async def tick(self, now: datetime | None = None) -> list[JobName]:
        """Run whatever is due. Returns the jobs that were triggered."""
        reference = now or datetime.now(self._tz)
        due = [job for job, when in self._next.items() if when <= reference]
        for job in due:
            self._next[job] = self._compute_next(job, reference)
            _LOGGER.info(
                "Starting scheduled %s run; next one at %s",
                job,
                self._next[job].isoformat(),
            )
            await self._runner.run(job, trigger=Trigger.SCHEDULE)
        return due
