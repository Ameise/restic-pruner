from __future__ import annotations

from collections.abc import AsyncIterator

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from restic_pruner.healthchecks import HealthchecksClient, _with_suffix, resolve_ping_url

UUID = "5e9a1c3e-1f6f-4f9b-9d3f-2b4c6d8e0a11"


def test_resolve_accepts_full_url() -> None:
    assert resolve_ping_url("https://hc.example.com/ping/abc") == "https://hc.example.com/ping/abc"


def test_resolve_expands_bare_uuid() -> None:
    assert resolve_ping_url(UUID) == f"https://hc-ping.com/{UUID}"
    assert resolve_ping_url(UUID, "https://hc.internal/ping/") == f"https://hc.internal/ping/{UUID}"


def test_resolve_ignores_empty() -> None:
    assert resolve_ping_url("   ") == ""


def test_suffix_is_appended_before_the_query() -> None:
    url = _with_suffix("https://hc-ping.com/abc?create=1", "start", "rid-1")
    assert url.startswith("https://hc-ping.com/abc/start?")
    assert "create=1" in url
    assert "rid=rid-1" in url


def test_suffix_handles_trailing_slash() -> None:
    assert _with_suffix("https://hc-ping.com/abc/", "fail", None) == "https://hc-ping.com/abc/fail"


def test_success_ping_has_no_suffix() -> None:
    assert _with_suffix("https://hc-ping.com/abc", "", None) == "https://hc-ping.com/abc"


class PingRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_times = 0

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.text()
        self.calls.append((request.path, body))
        if self.fail_times > 0:
            self.fail_times -= 1
            return web.Response(status=503)
        return web.Response(text="OK")


@pytest.fixture
async def recorder() -> AsyncIterator[tuple[PingRecorder, str]]:
    ping = PingRecorder()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", ping.handle)
    server = TestServer(app)
    await server.start_server()
    try:
        yield ping, str(server.make_url("/check"))
    finally:
        await server.close()


async def test_start_and_success_are_reported(
    recorder: tuple[PingRecorder, str],
) -> None:
    ping, url = recorder
    async with aiohttp.ClientSession() as session:
        client = HealthchecksClient(session, retries=1)
        assert await client.start(url, rid="abc") is True
        assert await client.success(url, rid="abc", body="all good") is True
    assert [path for path, _ in ping.calls] == ["/check/start", "/check"]
    assert ping.calls[1][1] == "all good"


async def test_failure_uses_the_exit_code_endpoint(
    recorder: tuple[PingRecorder, str],
) -> None:
    ping, url = recorder
    async with aiohttp.ClientSession() as session:
        client = HealthchecksClient(session, retries=1)
        await client.exit_code(url, 11, body="locked")
    assert ping.calls[0][0] == "/check/11"


async def test_body_is_truncated(recorder: tuple[PingRecorder, str]) -> None:
    ping, url = recorder
    async with aiohttp.ClientSession() as session:
        client = HealthchecksClient(session, retries=1, body_limit=100)
        await client.success(url, body="x" * 5000)
    _, body = ping.calls[0]
    assert len(body) < 200
    assert body.startswith("[... truncated ...]")


async def test_transient_errors_are_retried(recorder: tuple[PingRecorder, str]) -> None:
    ping, url = recorder
    ping.fail_times = 1
    async with aiohttp.ClientSession() as session:
        client = HealthchecksClient(session, retries=3, timeout=2)
        assert await client.success(url) is True
    assert len(ping.calls) == 2


async def test_unreachable_host_never_raises() -> None:
    async with aiohttp.ClientSession() as session:
        client = HealthchecksClient(session, retries=1, timeout=0.2)
        assert await client.success("http://127.0.0.1:9/never") is False


async def test_empty_url_is_a_no_op() -> None:
    async with aiohttp.ClientSession() as session:
        assert await HealthchecksClient(session).success("") is False
