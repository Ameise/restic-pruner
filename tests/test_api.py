from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from restic_pruner.api import create_app
from restic_pruner.config import Settings
from restic_pruner.healthchecks import HealthchecksClient
from restic_pruner.jobs import JobRunner
from restic_pruner.scheduler import Scheduler
from restic_pruner.state import RunRecord, RunStatus, StateStore, Trigger, new_run_id

#: The bundled app under test, as aiohttp types it.
Client = TestClient[web.Request, web.Application]


@dataclasses.dataclass
class Harness:
    client: Client
    runner: JobRunner
    session: aiohttp.ClientSession
    state: StateStore

    async def close(self) -> None:
        await self.client.close()
        await self.session.close()


async def _harness(settings: Settings) -> Harness:
    state = StateStore(settings.data_dir)
    state.load()
    session = aiohttp.ClientSession()
    runner = JobRunner(settings, state, HealthchecksClient(session))
    scheduler = Scheduler(settings, runner)
    scheduler.prime()
    client = TestClient(TestServer(create_app(settings, state, runner, scheduler)))
    await client.start_server()
    return Harness(client, runner, session, state)


@pytest.fixture
async def harness(local_settings: Settings) -> AsyncIterator[Harness]:
    harness = await _harness(local_settings)
    try:
        yield harness
    finally:
        await harness.close()


@pytest.fixture
def client(harness: Harness) -> Client:
    return harness.client


@pytest.fixture
def state(harness: Harness) -> StateStore:
    """The store the API is serving, so a test can seed history into it."""
    return harness.state


async def test_health(client: Client) -> None:
    response = await client.get("/api/health")
    assert response.status == 200
    assert (await response.json())["status"] == "ok"


async def test_status_document_shape(client: Client) -> None:
    payload = await (await client.get("/api/status")).json()
    assert payload["running"] is False
    assert set(payload["jobs"]) == {"prune", "check", "repack"}
    assert payload["jobs"]["prune"]["schedule"] == "0 3 * * 0"
    assert payload["jobs"]["prune"]["next_run"] is not None
    assert "version" in payload

    assert len(payload["repositories"]) == 1
    repository = payload["repositories"][0]
    assert repository["slug"] == "main"
    assert repository["retention"]["keep_last"] == 1
    assert repository["jobs"]["prune"]["last_status"] == "never"


async def test_runs_is_empty_initially(client: Client) -> None:
    payload = await (await client.get("/api/runs")).json()
    assert payload["runs"] == []


async def test_unknown_job_filter_is_rejected(client: Client) -> None:
    assert (await client.get("/api/runs?job=vacuum")).status == 400


async def test_unknown_run_is_404(client: Client) -> None:
    assert (await client.get("/api/runs/nope")).status == 404
    assert (await client.get("/api/runs/nope/log")).status == 404


async def test_live_log_is_empty_when_idle(client: Client) -> None:
    payload = await (await client.get("/api/live")).json()
    assert payload == {"offset": 0, "lines": [], "running": False, "run_id": None, "job": None}


async def test_index_is_served(client: Client) -> None:
    response = await client.get("/")
    assert response.status == 200
    assert "Restic Pruner" in await response.text()


async def test_unknown_job_cannot_be_started(client: Client) -> None:
    assert (await client.post("/api/jobs/vacuum/run")).status == 400


async def test_disabled_job_cannot_be_started(local_settings: Settings) -> None:
    configured = dataclasses.replace(
        local_settings, check=dataclasses.replace(local_settings.check, enabled=False)
    )
    harness = await _harness(configured)
    try:
        response = await harness.client.post("/api/jobs/check/run")
        assert response.status == 409
    finally:
        await harness.close()


async def test_dry_run_must_be_boolean(client: Client) -> None:
    response = await client.post("/api/jobs/prune/run", json={"dry_run": "yes please"})
    assert response.status == 400


async def test_unknown_repository_is_404(client: Client) -> None:
    assert (await client.get("/api/runs?repository=ghost")).status == 404
    assert (await client.post("/api/jobs/prune/run", json={"repository": "ghost"})).status == 404
    assert (await client.post("/api/unlock", json={"repository": "ghost"})).status == 404


async def test_runs_can_be_filtered_by_repository(client: Client) -> None:
    response = await client.get("/api/runs?repository=main")
    assert response.status == 200
    assert (await response.json())["runs"] == []


async def test_ingress_guard_blocks_direct_access(settings: Settings) -> None:
    """With ingress_only set, only Home Assistant's proxy address may connect."""
    harness = await _harness(settings)
    try:
        assert (await harness.client.get("/api/status")).status == 403
        assert (await harness.client.get("/")).status == 403
        # ...except the health probe, which the Supervisor watchdog calls from
        # its own address and which would otherwise report the add-on as dead.
        assert (await harness.client.get("/api/health")).status == 200
    finally:
        await harness.close()


async def test_trends_returns_plottable_points(client: Client) -> None:
    payload = await (await client.get("/api/trends")).json()
    assert "points" in payload
    for point in payload["points"]:
        assert set(point) == {
            "finished_at",
            "job",
            "repository",
            "duration_seconds",
            "repo_size_after",
            "unused_bytes",
        }, "the chart payload stays narrow; /api/runs is where full metrics live"


async def test_trends_omits_runs_that_cannot_be_plotted(client: Client, state: StateStore) -> None:
    """A failed run's sizes describe a repository mid-operation."""
    started = datetime.now(UTC)
    state.add(
        RunRecord(
            id=new_run_id(),
            job="prune",
            repository="main",
            status=RunStatus.SUCCESS,
            trigger=Trigger.SCHEDULE,
            started_at=started,
            finished_at=started + timedelta(seconds=5),
            metrics={"repo_size_after": 100, "unused_bytes": 10},
        )
    )
    state.add(
        RunRecord(
            id=new_run_id(),
            job="prune",
            repository="main",
            status=RunStatus.FAILED,
            trigger=Trigger.SCHEDULE,
            started_at=started,
            finished_at=started + timedelta(seconds=1),
            metrics={"repo_size_after": 999},
        )
    )
    state.add(
        RunRecord(
            id=new_run_id(),
            job="check",
            repository="main",
            status=RunStatus.RUNNING,
            trigger=Trigger.MANUAL,
            started_at=started,
        )
    )

    points = (await (await client.get("/api/trends")).json())["points"]
    sizes = [point["repo_size_after"] for point in points]
    assert 100 in sizes
    assert 999 not in sizes, "a failed run is not a data point"
    assert all(point["finished_at"] for point in points)


async def test_trends_are_oldest_first(client: Client, state: StateStore) -> None:
    started = datetime.now(UTC)
    for index in range(3):
        state.add(
            RunRecord(
                id=new_run_id(),
                job="prune",
                repository="main",
                status=RunStatus.SUCCESS,
                trigger=Trigger.SCHEDULE,
                started_at=started + timedelta(minutes=index),
                finished_at=started + timedelta(minutes=index, seconds=5),
                metrics={"repo_size_after": index},
            )
        )
    points = (await (await client.get("/api/trends")).json())["points"]
    stamps = [point["finished_at"] for point in points]
    assert stamps == sorted(stamps), "a chart reads left to right"
