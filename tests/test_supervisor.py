"""Supervisor API client and the REST state publisher.

Both only ever run inside Home Assistant, so they are exercised here against a
stub of the Supervisor's HTTP API.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from restic_pruner import supervisor as supervisor_module
from restic_pruner.publish.hass import HassStatePublisher
from restic_pruner.supervisor import SupervisorClient

STATUS: dict[str, Any] = {
    "running": False,
    "jobs": {"prune": {"next_run": None}, "check": {"next_run": None}},
    "repositories": [
        {
            "slug": "vps",
            "name": "vps",
            "size_bytes": 4096,
            "snapshot_count": 3,
            "jobs": {
                "prune": {
                    "last_status": "success",
                    "last_success": "2026-07-19T03:04:00+00:00",
                    "last_run": {
                        "finished_at": "2026-07-19T03:04:00+00:00",
                        "duration_seconds": 12.0,
                        "metrics": {"snapshots_removed": 2, "bytes_reclaimed": 512},
                    },
                },
                "check": {"last_status": "never", "last_success": None, "last_run": None},
            },
        }
    ],
}


class SupervisorStub:
    """Just enough of the Supervisor API for these two consumers."""

    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}
        self.tokens: list[str | None] = []
        self.mqtt_status = 200
        self.mqtt_payload: dict[str, Any] = {
            "result": "ok",
            "data": {
                "host": "core-mosquitto",
                "port": 1883,
                "username": "addons",
                "password": "secret",
                "ssl": False,
            },
        }

    async def mqtt(self, request: web.Request) -> web.Response:
        self.tokens.append(request.headers.get("Authorization"))
        if self.mqtt_status != 200:
            return web.Response(status=self.mqtt_status)
        return web.json_response(self.mqtt_payload)

    async def set_state(self, request: web.Request) -> web.Response:
        entity_id = request.match_info["entity_id"]
        self.states[entity_id] = json.loads(await request.text())
        return web.json_response({"entity_id": entity_id}, status=201)


@pytest.fixture
async def supervisor(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SupervisorStub]:
    stub = SupervisorStub()
    app = web.Application()
    app.router.add_get("/services/mqtt", stub.mqtt)
    app.router.add_post("/core/api/states/{entity_id}", stub.set_state)
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    monkeypatch.setattr(supervisor_module, "SUPERVISOR_URL", base)
    monkeypatch.setattr(supervisor_module, "CORE_API_URL", f"{base}/core/api")
    try:
        yield stub
    finally:
        await server.close()


async def test_mqtt_service_is_discovered(supervisor: SupervisorStub) -> None:
    async with aiohttp.ClientSession() as session:
        service = await SupervisorClient(session, "token123").mqtt_service()
    assert service is not None
    assert (service.host, service.port) == ("core-mosquitto", 1883)
    assert (service.username, service.password) == ("addons", "secret")
    assert supervisor.tokens == ["Bearer token123"]


async def test_no_broker_configured_returns_none(supervisor: SupervisorStub) -> None:
    """The Supervisor answers 400 when no MQTT service is provided."""
    supervisor.mqtt_status = 400
    async with aiohttp.ClientSession() as session:
        assert await SupervisorClient(session, "t").mqtt_service() is None


async def test_malformed_service_payload_returns_none(supervisor: SupervisorStub) -> None:
    supervisor.mqtt_payload = {"result": "ok", "data": {}}
    async with aiohttp.ClientSession() as session:
        assert await SupervisorClient(session, "t").mqtt_service() is None


async def test_without_a_token_nothing_is_attempted(supervisor: SupervisorStub) -> None:
    async with aiohttp.ClientSession() as session:
        client = SupervisorClient(session, "")
        assert client.available is False
        assert await client.mqtt_service() is None
        assert await client.set_state("sensor.x", "1", {}) is False
    assert supervisor.tokens == []


async def test_states_are_pushed_for_every_entity(supervisor: SupervisorStub) -> None:
    async with aiohttp.ClientSession() as session:
        publisher = HassStatePublisher(SupervisorClient(session, "t"), lambda: STATUS)
        await publisher.publish()

    pushed = supervisor.states
    # Hub entities plus one repository's worth, buttons excluded.
    assert "binary_sensor.restic_pruner_running" in pushed
    assert "sensor.restic_pruner_vps_prune_status" in pushed
    assert not any(entity_id.startswith("button.") for entity_id in pushed)

    status = pushed["sensor.restic_pruner_vps_prune_status"]
    assert status["state"] == "success"
    assert status["attributes"]["device_class"] == "enum"
    assert "never" in status["attributes"]["options"]

    reclaimed = pushed["sensor.restic_pruner_vps_bytes_reclaimed"]
    assert reclaimed["state"] == "512"
    assert reclaimed["attributes"]["unit_of_measurement"] == "B"
    assert reclaimed["attributes"]["device_class"] == "data_size"

    # A job that has never run must read as unknown, not as an empty string.
    assert pushed["sensor.restic_pruner_vps_check_last_run"]["state"] == "unknown"
    assert pushed["binary_sensor.restic_pruner_running"]["state"] == "OFF"


async def test_publishing_survives_a_supervisor_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable Supervisor must not raise into the job that triggered it."""
    # Port 1 refuses immediately, so this stays fast and deterministic.
    monkeypatch.setattr(supervisor_module, "CORE_API_URL", "http://127.0.0.1:1/core/api")
    async with aiohttp.ClientSession() as session:
        publisher = HassStatePublisher(SupervisorClient(session, "t"), lambda: STATUS)
        await publisher.publish()
