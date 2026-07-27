"""End-to-end tests against real restic repositories.

These exercise the real restic binary against repositories created in temporary
directories, with a stub healthchecks.io endpoint recording the pings.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from conftest import REPO_PASSWORD, requires_restic, snapshot_count
from restic_pruner.config import Settings
from restic_pruner.healthchecks import HealthchecksClient
from restic_pruner.jobs import JobBusyError, JobRunner, UnknownRepositoryError
from restic_pruner.state import RunStatus, StateStore, Trigger

pytestmark = requires_restic


class PingLog:
    def __init__(self) -> None:
        self.paths: list[str] = []
        self.bodies: list[str] = []
        self.rids: list[str | None] = []

    async def handle(self, request: web.Request) -> web.Response:
        self.paths.append(request.path)
        self.bodies.append(await request.text())
        self.rids.append(request.query.get("rid"))
        return web.Response(text="OK")


@pytest.fixture
async def pings() -> AsyncIterator[tuple[PingLog, str]]:
    log = PingLog()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", log.handle)
    server = TestServer(app)
    await server.start_server()
    try:
        yield log, str(server.make_url("/hc"))
    finally:
        await server.close()


async def _runner(settings: Settings, session: aiohttp.ClientSession) -> JobRunner:
    state = StateStore(settings.data_dir)
    state.load()
    return JobRunner(settings, state, HealthchecksClient(session, retries=1))


def _with_prune_url(settings: Settings, url: str) -> Settings:
    return dataclasses.replace(
        settings, prune=dataclasses.replace(settings.prune, healthchecks_url=url)
    )


async def test_prune_removes_snapshots_and_reclaims_space(
    restic_repo: Settings, pings: tuple[PingLog, str]
) -> None:
    ping_log, ping_url = pings
    settings = _with_prune_url(restic_repo, ping_url)
    assert snapshot_count(settings, "main") == 3

    async with aiohttp.ClientSession() as session:
        runner = await _runner(settings, session)
        runs = await runner.run("prune", trigger=Trigger.SCHEDULE)

    assert len(runs) == 1
    run = runs[0]
    assert run.status is RunStatus.SUCCESS, run.error
    # keep_last=1 against three snapshots of the same host and path.
    assert run.metrics["snapshots_removed"] == 2
    assert run.metrics["snapshots_kept"] == 1
    assert run.metrics["pruned"] is True
    assert run.metrics["bytes_reclaimed"] > 512 * 1024
    assert run.metrics["repo_size_after"] < run.metrics["repo_size_before"]
    assert snapshot_count(settings, "main") == 1

    # begin and conclusion, in that order, correlated by rid.
    assert ping_log.paths == ["/hc/start", "/hc"]
    assert ping_log.rids[0] == ping_log.rids[1]
    assert "forget:" in ping_log.bodies[1]
    assert "reclaimed" in ping_log.bodies[1]


async def test_a_second_prune_skips_the_prune_phase(restic_repo: Settings) -> None:
    """``forget --prune`` does no prune work when nothing was forgotten."""
    async with aiohttp.ClientSession() as session:
        runner = await _runner(restic_repo, session)
        first = (await runner.run("prune"))[0]
        second = (await runner.run("prune"))[0]

    assert first.metrics["pruned"] is True
    assert second.status is RunStatus.SUCCESS, second.error
    assert second.metrics["snapshots_removed"] == 0
    assert second.metrics["pruned"] is False
    assert second.metrics["bytes_reclaimed"] == 0


async def test_the_repository_is_still_valid_after_pruning(restic_repo: Settings) -> None:
    async with aiohttp.ClientSession() as session:
        runner = await _runner(restic_repo, session)
        assert (await runner.run("prune"))[0].status is RunStatus.SUCCESS
        check = (await runner.run("check"))[0]
    assert check.status is RunStatus.SUCCESS, check.error
    assert check.metrics["snapshot_count"] == 1


async def test_dry_run_changes_nothing(restic_repo: Settings, pings: tuple[PingLog, str]) -> None:
    ping_log, ping_url = pings
    settings = _with_prune_url(restic_repo, ping_url)
    async with aiohttp.ClientSession() as session:
        runner = await _runner(settings, session)
        run = (await runner.run("prune", dry_run=True))[0]

    assert run.status is RunStatus.SUCCESS, run.error
    assert run.dry_run is True
    assert run.metrics["snapshots_removed"] == 2, "a dry run still reports what it would do"
    assert run.metrics["bytes_reclaimed"] == 0
    assert snapshot_count(settings, "main") == 3, "but the repository is untouched"
    assert ping_log.paths == ["/hc/start", "/hc"]


async def test_a_failing_run_reports_the_exit_code(
    restic_repo: Settings, pings: tuple[PingLog, str]
) -> None:
    ping_log, ping_url = pings
    settings = _with_prune_url(restic_repo, ping_url)
    broken = dataclasses.replace(
        settings,
        repositories=(dataclasses.replace(settings.repositories[0], password="wrong-password"),),
    )
    async with aiohttp.ClientSession() as session:
        runner = await _runner(broken, session)
        run = (await runner.run("prune"))[0]

    assert run.status is RunStatus.FAILED
    assert run.error is not None
    assert "password is incorrect" in run.error
    # restic's documented exit code 12 becomes the healthchecks exit code.
    assert ping_log.paths == ["/hc/start", "/hc/12"]
    assert snapshot_count(settings, "main") == 3


async def test_run_history_and_logs_are_persisted(restic_repo: Settings) -> None:
    async with aiohttp.ClientSession() as session:
        runner = await _runner(restic_repo, session)
        run = (await runner.run("prune"))[0]

    reloaded = StateStore(restic_repo.data_dir)
    reloaded.load()
    stored = reloaded.get(run.id)
    assert stored is not None
    assert stored.status is RunStatus.SUCCESS
    assert stored.repository == "main"
    assert reloaded.repository("main").snapshot_count == 1
    log = reloaded.read_log(run.id)
    assert log is not None
    assert "$ restic forget --prune" in log


async def test_a_second_job_is_refused_while_one_runs(restic_repo: Settings) -> None:
    async with aiohttp.ClientSession() as session:
        runner = await _runner(restic_repo, session)
        async with runner._lock:  # simulate a run in flight
            with pytest.raises(JobBusyError):
                await runner.trigger("check")
            skipped = await runner.run("prune", trigger=Trigger.SCHEDULE)
    assert [run.status for run in skipped] == [RunStatus.SKIPPED]
    assert skipped[0].error is not None
    assert "still running" in skipped[0].error


async def test_unlock_leaves_the_repository_usable(restic_repo: Settings) -> None:
    lock_dir = Path(restic_repo.repositories[0].repository) / "locks"
    assert lock_dir.is_dir()
    async with aiohttp.ClientSession() as session:
        runner = await _runner(restic_repo, session)
        await runner.unlock()
    assert snapshot_count(restic_repo, "main") == 3


# -- several repositories ------------------------------------------------


async def test_every_repository_is_pruned_in_turn(restic_repos: Settings) -> None:
    async with aiohttp.ClientSession() as session:
        runner = await _runner(restic_repos, session)
        runs = await runner.run("prune")

    assert [run.repository for run in runs] == ["vps", "nas"], "order follows the config"
    assert all(run.status is RunStatus.SUCCESS for run in runs), [r.error for r in runs]
    for name in ("vps", "nas"):
        assert snapshot_count(restic_repos, name) == 1


async def test_one_broken_repository_does_not_stop_the_others(
    restic_repos: Settings, pings: tuple[PingLog, str]
) -> None:
    ping_log, ping_url = pings
    vps, nas = restic_repos.repositories
    settings = _with_prune_url(
        dataclasses.replace(
            restic_repos,
            repositories=(dataclasses.replace(vps, password="wrong-password"), nas),
        ),
        ping_url,
    )

    async with aiohttp.ClientSession() as session:
        runner = await _runner(settings, session)
        runs = await runner.run("prune")

    statuses = {run.repository: run.status for run in runs}
    assert statuses == {"vps": RunStatus.FAILED, "nas": RunStatus.SUCCESS}
    assert snapshot_count(restic_repos, "nas") == 1, "the healthy repository was still pruned"
    assert (
        snapshot_count(
            dataclasses.replace(
                settings, repositories=(dataclasses.replace(vps, password=REPO_PASSWORD),)
            ),
            "vps",
        )
        == 3
    )
    # One failure ping and one success ping, one per repository.
    assert ping_log.paths == ["/hc/start", "/hc/12", "/hc/start", "/hc"]


async def test_a_single_repository_can_be_targeted(restic_repos: Settings) -> None:
    async with aiohttp.ClientSession() as session:
        runner = await _runner(restic_repos, session)
        runs = await runner.trigger("prune", repository="nas")

    assert [run.repository for run in runs] == ["nas"]
    assert snapshot_count(restic_repos, "nas") == 1
    assert snapshot_count(restic_repos, "vps") == 3, "the other repository was left alone"


async def test_targeting_an_unknown_repository_is_an_error(restic_repos: Settings) -> None:
    async with aiohttp.ClientSession() as session:
        runner = await _runner(restic_repos, session)
        with pytest.raises(UnknownRepositoryError):
            await runner.trigger("prune", repository="ghost")


async def test_each_repository_can_have_its_own_healthchecks_url(
    restic_repos: Settings, pings: tuple[PingLog, str]
) -> None:
    ping_log, base = pings
    vps, nas = restic_repos.repositories
    settings = dataclasses.replace(
        restic_repos,
        repositories=(
            dataclasses.replace(vps, prune_healthchecks_url=f"{base}-vps"),
            dataclasses.replace(nas, prune_healthchecks_url=f"{base}-nas"),
        ),
    )
    async with aiohttp.ClientSession() as session:
        runner = await _runner(settings, session)
        await runner.run("prune")

    assert ping_log.paths == ["/hc-vps/start", "/hc-vps", "/hc-nas/start", "/hc-nas"]
