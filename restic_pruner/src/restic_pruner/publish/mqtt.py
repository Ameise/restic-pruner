"""MQTT discovery publisher.

All state travels in one retained JSON message on ``restic_pruner/state``; each
discovered entity extracts a single key from it with a value template. That
keeps a full refresh to one publish, and because the message is retained the
entities come back immediately after a Home Assistant restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final

import aiomqtt

from ..config import MqttConfig
from ..supervisor import MqttService
from .entities import (
    NODE_ID,
    ORIGIN_INFO,
    EntityDescription,
    commands_for,
    entities_for,
    entity_values,
    hub_device,
    repository_device,
)

_LOGGER: Final = logging.getLogger(__name__)

BASE_TOPIC: Final = NODE_ID
AVAILABILITY_TOPIC: Final = f"{BASE_TOPIC}/availability"
STATE_TOPIC: Final = f"{BASE_TOPIC}/state"
COMMAND_TOPIC: Final = f"{BASE_TOPIC}/command"

RECONNECT_MIN: Final = 2.0
RECONNECT_MAX: Final = 60.0

StatusProvider = Callable[[], dict[str, Any]]
CommandHandler = Callable[[str, str | None], Awaitable[None]]


def discovery_payload(
    entity: EntityDescription, device: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build the MQTT discovery config for one entity."""
    payload: dict[str, Any] = {
        "name": entity.name,
        "unique_id": entity.unique_id,
        "object_id": entity.unique_id,
        "device": device or hub_device(),
        "origin": ORIGIN_INFO,
        "availability_topic": AVAILABILITY_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
    }
    if entity.icon:
        payload["icon"] = entity.icon
    if entity.entity_category:
        payload["entity_category"] = entity.entity_category

    if entity.component == "button":
        payload["command_topic"] = COMMAND_TOPIC
        payload["payload_press"] = entity.command
        return payload

    payload["state_topic"] = STATE_TOPIC
    payload["value_template"] = f"{{{{ value_json.{entity.key} }}}}"
    if entity.device_class:
        payload["device_class"] = entity.device_class
    if entity.state_class:
        payload["state_class"] = entity.state_class
    if entity.unit:
        payload["unit_of_measurement"] = entity.unit
    if entity.options:
        payload["options"] = list(entity.options)
    if entity.component == "binary_sensor":
        payload["payload_on"] = "ON"
        payload["payload_off"] = "OFF"
    return payload


def discovery_topic(entity: EntityDescription, discovery_prefix: str) -> str:
    return f"{discovery_prefix}/{entity.component}/{NODE_ID}/{entity.key}/config"


class MqttPublisher:
    """Keeps a broker connection alive and mirrors add-on state onto it."""

    def __init__(
        self,
        config: MqttConfig,
        service: MqttService | None,
        status_provider: StatusProvider,
        command_handler: CommandHandler,
    ) -> None:
        self._config = config
        self._service = service
        self._status = status_provider
        self._handle_command = command_handler
        self._dirty = asyncio.Event()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def host(self) -> str:
        return self._config.host or (self._service.host if self._service else "")

    def notify(self) -> None:
        """Mark the published state as stale; the connection task republishes."""
        self._dirty.set()

    async def run(self) -> None:
        """Connect, publish, and reconnect forever."""
        delay = RECONNECT_MIN
        while True:
            started = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except aiomqtt.MqttError as exc:
                _LOGGER.warning("MQTT connection lost (%s); reconnecting in %.0fs", exc, delay)
            except Exception:  # never let publishing kill the app
                _LOGGER.exception("Unexpected MQTT failure; reconnecting in %.0fs", delay)
            finally:
                self._connected = False
            # Reset the backoff after a connection that stayed up a while, so a
            # single dropped connection does not inflate the next delay.
            if time.monotonic() - started > 60:
                delay = RECONNECT_MIN
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    async def _session(self) -> None:
        host, port, username, password = self._credentials()
        _LOGGER.info("Connecting to MQTT broker at %s:%s", host, port)
        will = aiomqtt.Will(AVAILABILITY_TOPIC, b"offline", qos=1, retain=True)
        async with aiomqtt.Client(
            hostname=host,
            port=port,
            username=username or None,
            password=password or None,
            will=will,
            identifier=f"{NODE_ID}-{id(self):x}",
        ) as client:
            self._connected = True
            _LOGGER.info("MQTT connected")
            await self._publish_discovery(client)
            await client.publish(AVAILABILITY_TOPIC, b"online", qos=1, retain=True)
            await self._publish_state(client)
            await client.subscribe(COMMAND_TOPIC, qos=1)
            try:
                async with asyncio.TaskGroup() as group:
                    group.create_task(self._state_loop(client))
                    group.create_task(self._command_loop(client))
            finally:
                self._connected = False
                with contextlib.suppress(aiomqtt.MqttError):
                    await client.publish(AVAILABILITY_TOPIC, b"offline", qos=1, retain=True)

    def _credentials(self) -> tuple[str, int, str, str]:
        if self._config.explicit:
            return (
                self._config.host,
                self._config.port,
                self._config.username,
                self._config.password,
            )
        if self._service is None:
            raise aiomqtt.MqttError("no MQTT broker configured")
        return (
            self._service.host,
            self._service.port,
            self._service.username,
            self._service.password,
        )

    async def _publish_discovery(self, client: aiomqtt.Client) -> None:
        prefix = self._config.discovery_prefix
        status = self._status()
        labels = {repo["slug"]: repo["name"] for repo in status.get("repositories", [])}
        entities = entities_for(status)
        for entity in entities:
            slug = entity.repository
            device = repository_device(slug, labels.get(slug, slug)) if slug else hub_device()
            await client.publish(
                discovery_topic(entity, prefix),
                json.dumps(discovery_payload(entity, device)).encode(),
                qos=1,
                retain=True,
            )
        _LOGGER.info(
            "Published discovery for %d entities across %d repositories",
            len(entities),
            len(labels),
        )

    async def _publish_state(self, client: aiomqtt.Client) -> None:
        values = entity_values(self._status())
        await client.publish(STATE_TOPIC, json.dumps(values).encode(), qos=1, retain=True)

    async def _state_loop(self, client: aiomqtt.Client) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            await self._publish_state(client)

    async def _command_loop(self, client: aiomqtt.Client) -> None:
        async for message in client.messages:
            payload = _decode(message.payload)
            command = commands_for(self._status()).get(payload)
            if command is None:
                _LOGGER.warning("Ignoring unknown MQTT command %r", payload)
                continue
            job, repository = command
            _LOGGER.info("MQTT command received: %s", payload)
            try:
                await self._handle_command(job, repository)
            except Exception:  # a bad command must not drop the link
                _LOGGER.exception("MQTT command %r failed", payload)


def _decode(payload: Any) -> str:
    if isinstance(payload, bytes | bytearray):
        return payload.decode("utf-8", errors="replace").strip()
    return str(payload).strip()
