"""Entity definitions shared by both publishing backends.

Every repository becomes its own Home Assistant device, linked by ``via_device``
to a parent "Restic Pruner" device that carries the schedule and the
run-everything buttons.

:func:`entity_values` flattens the status document into one value per entity.
MQTT publishes that mapping as a single retained JSON payload and every entity
reads one key out of it; the REST fallback reads the same mapping.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Final

from .. import __version__

NODE_ID: Final = "restic_pruner"

#: Values a status sensor can take. ``never`` means the job has not run yet.
STATUS_OPTIONS: Final = ("success", "failed", "running", "skipped", "never")

_MANUFACTURER: Final = "restic-pruner"

ORIGIN_INFO: Final[dict[str, Any]] = {
    "name": "Restic Pruner",
    "sw_version": __version__,
    "support_url": "https://github.com/Ameise/restic-pruner",
}


@dataclass(frozen=True, slots=True)
class EntityDescription:
    key: str
    name: str
    component: str = "sensor"
    device_class: str | None = None
    state_class: str | None = None
    unit: str | None = None
    icon: str | None = None
    entity_category: str | None = None
    options: tuple[str, ...] = ()
    #: Buttons only: the payload published to the command topic.
    command: str | None = None
    #: Slug of the repository this entity belongs to; empty for hub entities.
    repository: str = ""

    @property
    def unique_id(self) -> str:
        return f"{NODE_ID}_{self.key}"

    @property
    def entity_id(self) -> str:
        return f"{self.component}.{NODE_ID}_{self.key}"

    def for_repository(self, slug: str, label: str) -> EntityDescription:
        """Rebind this description to one repository."""
        return dataclasses.replace(
            self,
            key=f"{slug}_{self.key}",
            command=f"{self.command}:{slug}" if self.command else None,
            repository=slug,
            name=self.name if self.name else label,
        )


#: Entities that belong to the hub: the schedule and the run-everything buttons.
HUB_ENTITIES: Final[tuple[EntityDescription, ...]] = (
    EntityDescription(
        key="running",
        name="Running",
        component="binary_sensor",
        device_class="running",
        icon="mdi:cog-sync-outline",
    ),
    EntityDescription(
        key="prune_next_run",
        name="Prune next run",
        device_class="timestamp",
        icon="mdi:clock-start",
    ),
    EntityDescription(
        key="check_next_run",
        name="Check next run",
        device_class="timestamp",
        icon="mdi:clock-start",
    ),
    EntityDescription(
        key="repack_next_run",
        name="Repack next run",
        device_class="timestamp",
        icon="mdi:clock-start",
    ),
    EntityDescription(
        key="run_prune",
        name="Run prune",
        component="button",
        command="prune",
        icon="mdi:broom",
    ),
    EntityDescription(
        key="run_prune_dry",
        name="Run prune (dry run)",
        component="button",
        command="prune_dry",
        icon="mdi:broom",
        entity_category="diagnostic",
    ),
    EntityDescription(
        key="run_check",
        name="Run check",
        component="button",
        command="check",
        icon="mdi:shield-search",
    ),
    EntityDescription(
        key="run_repack",
        name="Run repack",
        component="button",
        command="repack",
        icon="mdi:package-down",
    ),
)

#: Entities created once per repository. Keys are prefixed with its slug.
REPOSITORY_ENTITIES: Final[tuple[EntityDescription, ...]] = (
    EntityDescription(
        key="prune_status",
        name="Prune status",
        device_class="enum",
        options=STATUS_OPTIONS,
        icon="mdi:broom",
    ),
    EntityDescription(
        key="prune_last_run",
        name="Prune last run",
        device_class="timestamp",
        icon="mdi:clock-outline",
    ),
    EntityDescription(
        key="prune_last_success",
        name="Prune last success",
        device_class="timestamp",
        icon="mdi:clock-check-outline",
    ),
    EntityDescription(
        key="prune_duration",
        name="Prune duration",
        device_class="duration",
        state_class="measurement",
        unit="s",
        icon="mdi:timer-outline",
    ),
    EntityDescription(
        key="snapshots_removed",
        name="Snapshots removed",
        state_class="measurement",
        unit="snapshots",
        icon="mdi:delete-clock-outline",
    ),
    EntityDescription(
        key="bytes_reclaimed",
        name="Space reclaimed",
        device_class="data_size",
        state_class="measurement",
        unit="B",
        icon="mdi:database-minus-outline",
    ),
    EntityDescription(
        key="check_status",
        name="Check status",
        device_class="enum",
        options=STATUS_OPTIONS,
        icon="mdi:shield-search",
    ),
    EntityDescription(
        key="check_last_run",
        name="Check last run",
        device_class="timestamp",
        icon="mdi:clock-outline",
    ),
    EntityDescription(
        key="check_last_success",
        name="Check last success",
        device_class="timestamp",
        icon="mdi:shield-check-outline",
    ),
    EntityDescription(
        key="repack_status",
        name="Repack status",
        device_class="enum",
        options=STATUS_OPTIONS,
        icon="mdi:package-down",
    ),
    EntityDescription(
        key="repack_last_run",
        name="Repack last run",
        device_class="timestamp",
        icon="mdi:clock-outline",
    ),
    EntityDescription(
        key="repack_last_success",
        name="Repack last success",
        device_class="timestamp",
        icon="mdi:package-variant-closed-check",
    ),
    EntityDescription(
        key="unused_bytes",
        name="Unused space",
        device_class="data_size",
        state_class="measurement",
        unit="B",
        icon="mdi:database-alert-outline",
    ),
    EntityDescription(
        key="repository_size",
        name="Repository size",
        device_class="data_size",
        state_class="measurement",
        unit="B",
        icon="mdi:database",
    ),
    EntityDescription(
        key="snapshot_count",
        name="Snapshots",
        state_class="measurement",
        unit="snapshots",
        icon="mdi:camera-burst",
    ),
    EntityDescription(
        key="run_prune",
        name="Run prune",
        component="button",
        command="prune",
        icon="mdi:broom",
    ),
    EntityDescription(
        key="run_check",
        name="Run check",
        component="button",
        command="check",
        icon="mdi:shield-search",
    ),
    EntityDescription(
        key="run_repack",
        name="Run repack",
        component="button",
        command="repack",
        icon="mdi:package-down",
    ),
)


def hub_device() -> dict[str, Any]:
    return {
        "identifiers": [NODE_ID],
        "name": "Restic Pruner",
        "manufacturer": _MANUFACTURER,
        "model": "restic maintenance",
        "sw_version": __version__,
    }


def repository_device(slug: str, name: str) -> dict[str, Any]:
    return {
        "identifiers": [f"{NODE_ID}_{slug}"],
        "name": f"Restic Pruner ({name})",
        "manufacturer": _MANUFACTURER,
        "model": "restic repository",
        "sw_version": __version__,
        "via_device": NODE_ID,
    }


def entities_for(status: dict[str, Any]) -> list[EntityDescription]:
    """Every entity this installation should publish, hub first."""
    result = list(HUB_ENTITIES)
    for repository in status.get("repositories", []):
        slug = repository["slug"]
        label = repository["name"]
        result += [entity.for_repository(slug, label) for entity in REPOSITORY_ENTITIES]
    return result


def state_entities(status: dict[str, Any]) -> list[EntityDescription]:
    """Entities that carry state; buttons are write-only."""
    return [entity for entity in entities_for(status) if entity.component != "button"]


def commands_for(status: dict[str, Any]) -> dict[str, tuple[str, str | None]]:
    """Map an MQTT command payload onto ``(job, repository slug or None)``."""
    result: dict[str, tuple[str, str | None]] = {}
    for entity in entities_for(status):
        if not entity.command:
            continue
        job, _, slug = entity.command.partition(":")
        result[entity.command] = (job, slug or None)
    return result


def entity_values(status: dict[str, Any]) -> dict[str, Any]:
    """Flatten a :func:`restic_pruner.views.status_view` document.

    ``None`` is published verbatim; Home Assistant's MQTT platform renders it as
    the string ``None`` and treats that as "unknown", which is exactly right for
    a job that has never run.
    """
    jobs = status.get("jobs") or {}
    values: dict[str, Any] = {
        "running": "ON" if status.get("running") else "OFF",
        "prune_next_run": (jobs.get("prune") or {}).get("next_run"),
        "check_next_run": (jobs.get("check") or {}).get("next_run"),
        "repack_next_run": (jobs.get("repack") or {}).get("next_run"),
    }
    for repository in status.get("repositories", []):
        slug = repository["slug"]
        repo_jobs = repository.get("jobs") or {}
        prune = repo_jobs.get("prune") or {}
        check = repo_jobs.get("check") or {}
        repack = repo_jobs.get("repack") or {}
        prune_last = prune.get("last_run") or {}
        check_last = check.get("last_run") or {}
        repack_last = repack.get("last_run") or {}
        metrics = prune_last.get("metrics") or {}
        values.update(
            {
                f"{slug}_prune_status": prune.get("last_status", "never"),
                f"{slug}_prune_last_run": prune_last.get("finished_at"),
                f"{slug}_prune_last_success": prune.get("last_success"),
                f"{slug}_prune_duration": _round(prune_last.get("duration_seconds")),
                f"{slug}_snapshots_removed": metrics.get("snapshots_removed"),
                f"{slug}_bytes_reclaimed": metrics.get("bytes_reclaimed"),
                f"{slug}_check_status": check.get("last_status", "never"),
                f"{slug}_check_last_run": check_last.get("finished_at"),
                f"{slug}_check_last_success": check.get("last_success"),
                f"{slug}_repack_status": repack.get("last_status", "never"),
                f"{slug}_repack_last_run": repack_last.get("finished_at"),
                f"{slug}_repack_last_success": repack.get("last_success"),
                f"{slug}_unused_bytes": _latest_unused(prune_last, repack_last),
                f"{slug}_repository_size": repository.get("size_bytes"),
                f"{slug}_snapshot_count": repository.get("snapshot_count"),
            }
        )
    return values


def _latest_unused(*runs: dict[str, Any]) -> int | None:
    """Dead space as of the most recent run that measured it.

    Both prune and repack report it, and either can be the newer of the two, so
    the figure is taken from whichever finished last rather than from one job.
    """
    measured: list[tuple[str, int]] = [
        (str(run.get("finished_at") or ""), int(unused))
        for run in runs
        if (unused := (run.get("metrics") or {}).get("unused_bytes")) is not None
    ]
    if not measured:
        return None
    return max(measured, key=lambda item: item[0])[1]


def _round(value: Any) -> float | None:
    return None if value is None else round(float(value), 1)
