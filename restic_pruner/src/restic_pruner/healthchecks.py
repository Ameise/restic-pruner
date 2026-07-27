"""healthchecks.io reporting.

Each run pings ``/start`` when it begins and either the bare URL (success) or
``/<exit code>`` when it ends, carrying the tail of the restic log as the
request body. A ``rid`` query parameter ties the two pings together as one
execution.

Reporting is best effort: a healthchecks outage must not turn a successful prune
into a failed one, so every error here is logged and swallowed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

_LOGGER: Final = logging.getLogger(__name__)

DEFAULT_BASE_URL: Final = "https://hc-ping.com"

_UUID_RE: Final = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def resolve_ping_url(value: str, base_url: str = DEFAULT_BASE_URL) -> str:
    """Accept a full ping URL or a bare check UUID.

    A self-hosted healthchecks instance works either way: give the full URL, or
    give the UUID and point ``healthchecks_base_url`` at your instance.
    """
    value = value.strip()
    if not value:
        return ""
    if _UUID_RE.match(value):
        return f"{base_url.rstrip('/')}/{value}"
    return value


def _with_suffix(url: str, suffix: str, rid: str | None) -> str:
    """Append a path segment to a ping URL, preserving any query string."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if suffix:
        path = f"{path}/{suffix.lstrip('/')}"
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if rid:
        query["rid"] = rid
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), parts.fragment))


class HealthchecksClient:
    """Fire-and-forget pings with bounded retries."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        retries: int = 3,
        body_limit: int = 10_000,
    ) -> None:
        self._session = session
        self._base_url = base_url
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._retries = max(1, retries)
        self._body_limit = body_limit

    async def start(self, url: str, *, rid: str | None = None) -> bool:
        return await self._ping(url, "start", rid=rid)

    async def success(self, url: str, *, rid: str | None = None, body: str = "") -> bool:
        return await self._ping(url, "", rid=rid, body=body)

    async def fail(self, url: str, *, rid: str | None = None, body: str = "") -> bool:
        return await self._ping(url, "fail", rid=rid, body=body)

    async def exit_code(
        self, url: str, code: int, *, rid: str | None = None, body: str = ""
    ) -> bool:
        return await self._ping(url, str(code), rid=rid, body=body)

    async def _ping(
        self,
        url: str,
        suffix: str,
        *,
        rid: str | None = None,
        body: str = "",
    ) -> bool:
        resolved = resolve_ping_url(url, self._base_url)
        if not resolved:
            return False
        target = _with_suffix(resolved, suffix, rid)
        payload = _tail(body, self._body_limit).encode("utf-8") if body else b""
        delay = 1.0
        for attempt in range(1, self._retries + 1):
            try:
                async with self._session.post(
                    target, data=payload, timeout=self._timeout
                ) as response:
                    if response.status < 400:
                        return True
                    _LOGGER.warning(
                        "healthchecks ping %s returned HTTP %s (attempt %s/%s)",
                        suffix or "success",
                        response.status,
                        attempt,
                        self._retries,
                    )
            except (aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.warning(
                    "healthchecks ping %s failed: %s (attempt %s/%s)",
                    suffix or "success",
                    exc,
                    attempt,
                    self._retries,
                )
            if attempt < self._retries:
                await asyncio.sleep(delay)
                delay *= 2
        _LOGGER.error("giving up on healthchecks ping %r", suffix or "success")
        return False


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "[... truncated ...]\n" + text[-limit:]
