"""Fallback publisher for installations without an MQTT broker.

States are pushed into Home Assistant Core through the Supervisor proxy.
Entities created this way have no device and no config entry, and they vanish on
a Core restart until the next run updates them. Buttons are not possible at all,
so without MQTT the web UI is the only way to trigger a run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Final

from ..supervisor import SupervisorClient
from .entities import entity_values, state_entities

_LOGGER: Final = logging.getLogger(__name__)

StatusProvider = Callable[[], dict[str, Any]]


class HassStatePublisher:
    def __init__(self, supervisor: SupervisorClient, status_provider: StatusProvider) -> None:
        self._supervisor = supervisor
        self._status = status_provider
        self._warned = False

    @property
    def available(self) -> bool:
        return self._supervisor.available

    async def publish(self) -> None:
        if not self.available:
            return
        status = self._status()
        values = entity_values(status)
        entities = state_entities(status)
        failures = 0
        for entity in entities:
            value = values.get(entity.key)
            attributes: dict[str, Any] = {"friendly_name": f"Restic Pruner {entity.name}"}
            if entity.device_class:
                attributes["device_class"] = entity.device_class
            if entity.state_class:
                attributes["state_class"] = entity.state_class
            if entity.unit:
                attributes["unit_of_measurement"] = entity.unit
            if entity.icon:
                attributes["icon"] = entity.icon
            if entity.options:
                attributes["options"] = list(entity.options)
            ok = await self._supervisor.set_state(entity.entity_id, _as_state(value), attributes)
            failures += 0 if ok else 1
        if failures and not self._warned:
            _LOGGER.warning(
                "%d of %d states could not be pushed to Home Assistant",
                failures,
                len(entities),
            )
            self._warned = True
        elif not failures:
            self._warned = False


def _as_state(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)
