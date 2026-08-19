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

from .config import JobName, RepositoryConfig, Settings, parse_subset_slice
from .healthchecks import HealthchecksClient
from .restic import (
    MAX_FINDINGS,
    CheckError,
    CheckResult,
    ForgetPruneResult,
    RepoStats,
    Restic,
    ResticError,
)
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
                config = self._settings.job(job)
                dry_run = getattr(config, "dry_run", False)
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

        restic = (
            self.restic_for(repository).with_log(self._emit).with_hostname(f"restic-pruner-{job}")
        )
        # Filled as the job goes, so a failure still reports whatever it learned
        # before it failed -- for check, that is the whole point.
        metrics: dict[str, Any] = {}
        run.metrics = metrics
        try:
            if job == "prune":
                await self._run_prune(restic, repository, metrics, dry_run=dry_run)
            elif job == "repack":
                await self._run_repack(restic, repository, metrics, dry_run=dry_run)
            else:
                await self._run_check(restic, repository, metrics)
        except asyncio.CancelledError:
            self._finish(run, RunStatus.FAILED, "cancelled: the add-on is shutting down")
            await self._report(ping_url, run, ok=False)
            await self._notify()
            raise
        except ResticError as exc:
            _LOGGER.error("%s run on %s failed: %s", job, repository.name, exc)
            detail = _failure_detail(exc)
            self._emit(detail)
            self._finish(run, RunStatus.FAILED, detail)
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

        self._emit(f"=== {job} run finished in {time.monotonic() - started:.1f}s ===")
        self._finish(run, RunStatus.SUCCESS, None)
        await self._report(ping_url, run, ok=True)
        await self._notify()
        return run

    async def _run_prune(
        self,
        restic: Restic,
        repository: RepositoryConfig,
        metrics: dict[str, Any],
        *,
        dry_run: bool,
    ) -> None:
        """``forget --prune``, with the reclaimed figures taken as cheaply as possible.

        ``stats --mode raw-data`` is exact but expensive: each call re-opens the
        repository and re-reads every index, over the network, inside the
        exclusive lock a concurrent backup is waiting on. prune already prints
        the same numbers, so by default they are read out of its output and the
        lock is released that much sooner.
        """
        exact = self._settings.prune.exact_reclaimed
        before = await restic.stats() if exact else None
        result = await restic.forget_and_prune(dry_run=dry_run)
        after = await restic.stats() if exact else None

        metrics.update(snapshots_removed=result.removed, snapshots_kept=result.kept)
        self._record_reclaimed(repository, metrics, result, before, after)

    async def _run_repack(
        self,
        restic: Restic,
        repository: RepositoryConfig,
        metrics: dict[str, Any],
        *,
        dry_run: bool,
    ) -> None:
        """``prune`` on its own, to reclaim the dead space prune had to leave.

        No snapshot is touched, so this cannot conflict with the retention
        policy. It reports the same figures as prune minus the snapshot counts,
        because there is no forget phase to count.
        """
        result = await restic.prune(dry_run=dry_run)
        self._record_reclaimed(repository, metrics, result, None, None)

    def _record_reclaimed(
        self,
        repository: RepositoryConfig,
        metrics: dict[str, Any],
        result: ForgetPruneResult,
        before: RepoStats | None,
        after: RepoStats | None,
    ) -> None:
        """Fill in what a prune-shaped run reclaimed, and remember the new size.

        ``stats --mode raw-data`` is exact but expensive: each call re-opens the
        repository and re-reads every index, over the network, inside the
        exclusive lock a concurrent backup is waiting on. restic already prints
        the same numbers, so by default they are read out of its output and the
        lock is released that much sooner. *before* and *after* are supplied only
        when the exact measurement was asked for.
        """
        exact = before is not None and after is not None
        metrics.update(
            pruned=result.pruned,
            blobs_removed=result.blobs_removed,
            blobs_repacked=result.blobs_repacked,
            packs_removed=result.packs_removed,
            prune_summary=result.summary,
            exact_sizes=exact,
        )
        if result.unused_bytes is not None:
            # Dead data still in packs that hold live blobs. Only the repack job
            # gets this back; under the prune job it is what accumulates. Zero is
            # a real answer here, so the check is for "reported", not "non-zero".
            metrics["unused_bytes"] = result.unused_bytes
            metrics["unused_percent"] = result.unused_percent or 0.0

        if before is not None and after is not None:
            reclaimed = max(0, before.total_size - after.total_size)
            size_before, size_after = before.total_size, after.total_size
            metrics["snapshot_count"] = after.snapshots_count
            self._remember(repository, after)
        else:
            # From restic's own "total prune:" and "remaining:" lines. A run that
            # had nothing to do prints neither, so the last known size stands
            # rather than the dashboard dropping to zero.
            previous = self._state.repository(repository.slug)
            reclaimed = result.bytes_removed
            size_after = result.bytes_remaining or previous.size_bytes
            size_before = size_after + reclaimed
            snapshots = result.kept or previous.snapshot_count
            metrics["snapshot_count"] = snapshots
            self._remember_partial(repository, size_after, snapshots)

        metrics.update(
            repo_size_before=size_before,
            repo_size_after=size_after,
            bytes_reclaimed=reclaimed,
        )
        about = "" if exact else " (from restic's own output)"
        self._emit(
            f"reclaimed {_human_bytes(reclaimed)} "
            f"({_human_bytes(size_before)} -> {_human_bytes(size_after)}){about}"
        )
        if result.unused_bytes is not None:
            self._emit(
                f"unused: {_human_bytes(result.unused_bytes)} "
                f"({result.unused_percent or 0.0:.0f}% of the repository)"
            )

    async def _run_check(
        self, restic: Restic, repository: RepositoryConfig, metrics: dict[str, Any]
    ) -> None:
        subset = self._check_subset(repository)
        metrics["read_data_subset"] = subset or "structure only"
        try:
            result = await restic.check(subset)
        except CheckError as exc:
            metrics.update(_check_metrics(exc.result))
            raise
        metrics.update(_check_metrics(result))
        self._advance_check_subset(repository, subset)

        # Unlike prune, check keeps its trailing stats call: it is not the phase
        # that fights the consumer for the lock, and the snapshot count and size
        # it returns are what the dashboard shows between prune runs.
        stats = await restic.stats()
        self._remember(repository, stats)
        metrics["snapshot_count"] = stats.snapshots_count
        metrics["repo_size_after"] = stats.total_size

    def _check_subset(self, repository: RepositoryConfig) -> str:
        """The read scope for this run, rotated if it is an ``n/t`` slice."""
        configured = self._settings.check.read_data_subset
        parsed = parse_subset_slice(configured)
        if parsed is None or not self._settings.check.rotate_subset:
            return configured
        _, parts = parsed
        index = self._state.repository(repository.slug).next_check_slice
        return f"{(index - 1) % parts + 1}/{parts}"

    def _advance_check_subset(self, repository: RepositoryConfig, subset: str) -> None:
        """Move to the next slice, but only once this one has actually been read."""
        parsed = parse_subset_slice(subset)
        if parsed is None or not self._settings.check.rotate_subset:
            return
        index, parts = parsed
        self._state.set_check_slice(repository.slug, index % parts + 1)

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

    def _remember_partial(
        self, repository: RepositoryConfig, size_bytes: int, snapshot_count: int
    ) -> None:
        """Update what prune's own output can tell us, keeping the rest.

        Without a ``stats`` call there is no blob count or uncompressed size to
        report, and stale figures are more useful to a dashboard than zeroes.
        """
        previous = self._state.repository(repository.slug)
        self._state.set_repository(
            repository.slug,
            RepositorySnapshot(
                checked_at=datetime.now(UTC),
                size_bytes=size_bytes,
                uncompressed_bytes=previous.uncompressed_bytes,
                blob_count=previous.blob_count,
                snapshot_count=snapshot_count,
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

    def _body(self, run: RunRecord) -> str:
        mode = self._settings.healthchecks_body
        if mode == "none":
            return ""
        if mode == "log":
            return "\n".join(self._log_lines)
        return summary_body(run)

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
        body = self._body(run)
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


def _failure_detail(exc: ResticError) -> str:
    """The failure text for the history, with a check's findings hoisted up front."""
    text = str(exc)
    if not isinstance(exc, CheckError) or not exc.result.findings:
        return text
    head, _, rest = text.partition("\n")
    findings = [str(finding) for finding in exc.result.findings[:MAX_FINDINGS]]
    block = "\n".join([exc.result.headline(), *findings])
    return "\n".join(part for part in (head, block, rest) if part)


def _check_metrics(result: CheckResult) -> dict[str, Any]:
    """The reportable part of a check, ids and counts only."""
    metrics: dict[str, Any] = {
        "findings": [str(finding) for finding in result.findings[:MAX_FINDINGS]],
        "finding_count": len(result.findings),
        "findings_headline": result.headline(),
    }
    if result.packs_total:
        metrics["packs_read"] = result.packs_read
        metrics["packs_total"] = result.packs_total
    return metrics


def summary_body(run: RunRecord) -> str:
    """A compact report of one run, for the healthchecks.io ping body.

    Built exclusively from this program's own values: the repository label the
    user chose, counts, sizes, and the error line this program formats. restic's
    own output is never included, because its forget listing names every host,
    tag and absolute path in the repository.
    """
    headline = f"{run.job} on {run.repository}: {run.status}"
    if run.dry_run:
        headline += " (dry run)"
    if run.duration_seconds is not None:
        headline += f" in {run.duration_seconds:.1f}s"
    lines = [headline]

    metrics = run.metrics
    if run.job in ("prune", "repack"):
        if "snapshots_removed" in metrics:
            lines.append(
                f"snapshots: removed {metrics['snapshots_removed']}, "
                f"kept {metrics['snapshots_kept']}"
            )
        if metrics.get("pruned"):
            lines.append(
                f"{run.job}: {metrics['blobs_removed']} blobs removed, "
                f"{metrics['blobs_repacked']} repacked, "
                f"{metrics['packs_removed']} packs deleted"
            )
        elif "pruned" in metrics:
            lines.append("prune: skipped, nothing was forgotten")
        if "bytes_reclaimed" in metrics:
            lines.append(
                f"reclaimed {_human_bytes(metrics['bytes_reclaimed'])} "
                f"({_human_bytes(metrics['repo_size_before'])} -> "
                f"{_human_bytes(metrics['repo_size_after'])})"
            )
        if "unused_bytes" in metrics:
            lines.append(
                f"unused: {_human_bytes(metrics['unused_bytes'])} "
                f"({metrics['unused_percent']:.0f}% of the repository)"
            )
    else:
        if metrics.get("read_data_subset"):
            verified = f"verified: {metrics['read_data_subset']}"
            if metrics.get("packs_total"):
                verified += f" ({metrics['packs_read']} of {metrics['packs_total']} packs read)"
            lines.append(verified)
        if "snapshot_count" in metrics:
            lines.append(f"snapshots: {metrics['snapshot_count']}")
        # Object ids, never restic's error lines: "pack 6dcad00d1e missing"
        # starts the actual work, where "check failed" only starts an ssh session.
        if metrics.get("findings_headline"):
            lines.append(metrics["findings_headline"])
        lines += list(metrics.get("findings", []))
        hidden = int(metrics.get("finding_count", 0)) - len(metrics.get("findings", []))
        if hidden > 0:
            lines.append(f"... and {hidden} more")

    if run.error:
        # Only the first line: everything this program formats itself, never the
        # restic output that may follow it.
        lines += ["", run.error.strip().splitlines()[0]]
    return "\n".join(lines)


def _ping_rid(run_id: str) -> str:
    """healthchecks expects the rid to be a UUID."""
    return f"{run_id[0:8]}-{run_id[8:12]}-{run_id[12:16]}-{run_id[16:20]}-{run_id[20:32]}"


def _human_bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"
