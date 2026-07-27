"""Home Assistant Supervisor helpers.

Only used when running as an add-on: ``SUPERVISOR_TOKEN`` is present in the
environment and the Supervisor is reachable at ``http://supervisor``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

import aiohttp

_LOGGER: Final = logging.getLogger(__name__)

SUPERVISOR_URL: Final = "http://supervisor"
CORE_API_URL: Final = f"{SUPERVISOR_URL}/core/api"
REQUEST_TIMEOUT: Final = aiohttp.ClientTimeout(total=15)


@dataclass(frozen=True, slots=True)
class MqttService:
    host: str
    port: int
    username: str = ""
    password: str = ""
    ssl: bool = False


class SupervisorClient:
    def __init__(self, session: aiohttp.ClientSession, token: str) -> None:
        self._session = session
        self._token = token

    @property
    def available(self) -> bool:
        return bool(self._token)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def mqtt_service(self) -> MqttService | None:
        """Fetch broker credentials published by the Mosquitto add-on.

        Returns ``None`` when no broker is configured, which is the signal to
        fall back to pushing states through the Core API.
        """
        if not self.available:
            return None
        try:
            async with self._session.get(
                f"{SUPERVISOR_URL}/services/mqtt",
                headers=self._headers,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status != 200:
                    _LOGGER.info(
                        "No MQTT service available from the Supervisor (HTTP %s)",
                        response.status,
                    )
                    return None
                payload = await response.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.info("Could not query the Supervisor for MQTT: %s", exc)
            return None

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not data.get("host"):
            return None
        return MqttService(
            host=str(data["host"]),
            port=int(data.get("port", 1883)),
            username=str(data.get("username") or ""),
            password=str(data.get("password") or ""),
            ssl=bool(data.get("ssl", False)),
        )

    async def set_state(self, entity_id: str, state: str, attributes: dict[str, Any]) -> bool:
        """Push a state to Home Assistant Core.

        These entities are not backed by a config entry, so they disappear on a
        Core restart until the next update. That is the documented trade-off of
        running without MQTT.
        """
        if not self.available:
            return False
        try:
            async with self._session.post(
                f"{CORE_API_URL}/states/{entity_id}",
                headers=self._headers,
                json={"state": state, "attributes": attributes},
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status >= 400:
                    _LOGGER.warning("Could not set %s: HTTP %s", entity_id, response.status)
                    return False
                return True
        except (aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.warning("Could not set %s: %s", entity_id, exc)
            return False
