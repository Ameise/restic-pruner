from __future__ import annotations

from typing import Any

from restic_pruner.publish.entities import (
    HUB_ENTITIES,
    REPOSITORY_ENTITIES,
    commands_for,
    entities_for,
    entity_values,
    hub_device,
    repository_device,
    state_entities,
)
from restic_pruner.publish.mqtt import discovery_payload, discovery_topic


def _status(*repositories: dict[str, Any], running: bool = False) -> dict[str, Any]:
    return {
        "running": running,
        "jobs": {
            "prune": {"next_run": "2026-07-26T03:00:00+02:00"},
            "check": {"next_run": None},
        },
        "repositories": list(repositories),
    }


def _repository(slug: str, name: str, *, populated: bool = False) -> dict[str, Any]:
    if not populated:
        return {
            "slug": slug,
            "name": name,
            "size_bytes": 0,
            "snapshot_count": 0,
            "jobs": {
                "prune": {"last_status": "never", "last_run": None, "last_success": None},
                "check": {"last_status": "never", "last_run": None, "last_success": None},
            },
        }
    return {
        "slug": slug,
        "name": name,
        "size_bytes": 1234,
        "snapshot_count": 7,
        "jobs": {
            "prune": {
                "last_status": "success",
                "last_success": "2026-07-19T03:04:00+00:00",
                "last_run": {
                    "finished_at": "2026-07-19T03:04:00+00:00",
                    "duration_seconds": 42.4242,
                    "metrics": {"snapshots_removed": 5, "bytes_reclaimed": 999},
                },
            },
            "check": {
                "last_status": "failed",
                "last_success": None,
                "last_run": {"finished_at": "2026-07-15T05:00:00+00:00"},
            },
        },
    }


EMPTY = _status(_repository("main", "main"))
TWO = _status(
    _repository("vps", "vps", populated=True),
    _repository("nas", "nas"),
    running=True,
)


def test_every_state_entity_has_a_value() -> None:
    values = entity_values(TWO)
    for entity in state_entities(TWO):
        assert entity.key in values, f"{entity.key} is discovered but never published"


def test_no_stray_values_without_an_entity() -> None:
    keys = {entity.key for entity in state_entities(TWO)}
    assert set(entity_values(TWO)) == keys


def test_entity_count_scales_with_repositories() -> None:
    one = entities_for(EMPTY)
    two = entities_for(TWO)
    assert len(one) == len(HUB_ENTITIES) + len(REPOSITORY_ENTITIES)
    assert len(two) == len(HUB_ENTITIES) + 2 * len(REPOSITORY_ENTITIES)


def test_values_for_a_fresh_install_are_none() -> None:
    values = entity_values(EMPTY)
    assert values["main_prune_status"] == "never"
    assert values["main_prune_last_run"] is None
    assert values["main_bytes_reclaimed"] is None
    assert values["running"] == "OFF"


def test_values_are_flattened_per_repository() -> None:
    values = entity_values(TWO)
    assert values["vps_prune_status"] == "success"
    assert values["vps_prune_last_run"] == "2026-07-19T03:04:00+00:00"
    assert values["vps_prune_duration"] == 42.4
    assert values["vps_snapshots_removed"] == 5
    assert values["vps_bytes_reclaimed"] == 999
    assert values["vps_repository_size"] == 1234
    assert values["vps_snapshot_count"] == 7
    assert values["vps_check_status"] == "failed"
    assert values["nas_prune_status"] == "never"
    assert values["running"] == "ON"
    assert values["prune_next_run"] == "2026-07-26T03:00:00+02:00"


def test_discovery_topics_are_unique() -> None:
    entities = entities_for(TWO)
    topics = {discovery_topic(entity, "homeassistant") for entity in entities}
    assert len(topics) == len(entities)


def test_sensor_discovery_payload() -> None:
    entity = next(e for e in entities_for(TWO) if e.key == "vps_bytes_reclaimed")
    payload = discovery_payload(entity, repository_device("vps", "vps"))
    assert payload["unique_id"] == "restic_pruner_vps_bytes_reclaimed"
    assert payload["state_topic"] == "restic_pruner/state"
    assert payload["value_template"] == "{{ value_json.vps_bytes_reclaimed }}"
    assert payload["device_class"] == "data_size"
    assert payload["unit_of_measurement"] == "B"
    assert payload["availability_topic"] == "restic_pruner/availability"


def test_repository_devices_hang_off_the_hub() -> None:
    device = repository_device("vps", "VPS")
    assert device["identifiers"] == ["restic_pruner_vps"]
    assert device["name"] == "Restic Pruner (VPS)"
    assert device["via_device"] == hub_device()["identifiers"][0]


def test_enum_sensor_declares_its_options() -> None:
    entity = next(e for e in entities_for(EMPTY) if e.key == "main_prune_status")
    payload = discovery_payload(entity)
    assert "never" in payload["options"]
    assert payload["device_class"] == "enum"


def test_binary_sensor_payloads() -> None:
    entity = next(e for e in entities_for(EMPTY) if e.key == "running")
    payload = discovery_payload(entity)
    assert payload["payload_on"] == "ON"
    assert payload["payload_off"] == "OFF"
    assert entity.repository == "", "the running sensor belongs to the hub"


def test_button_discovery_payload_has_no_state_topic() -> None:
    entity = next(e for e in entities_for(EMPTY) if e.key == "run_prune_dry")
    payload = discovery_payload(entity)
    assert payload["command_topic"] == "restic_pruner/command"
    assert payload["payload_press"] == "prune_dry"
    assert "state_topic" not in payload


def test_commands_cover_the_hub_and_every_repository() -> None:
    commands = commands_for(TWO)
    # Hub buttons run every repository.
    assert commands["prune"] == ("prune", None)
    assert commands["prune_dry"] == ("prune_dry", None)
    assert commands["check"] == ("check", None)
    # Per-repository buttons target one.
    assert commands["prune:vps"] == ("prune", "vps")
    assert commands["check:nas"] == ("check", "nas")
    assert "prune:unknown" not in commands


def test_repack_entities_and_command() -> None:
    status = {
        "running": False,
        "jobs": {"repack": {"next_run": "2026-09-01T04:17:00+00:00"}},
        "repositories": [
            {
                "slug": "vps",
                "name": "vps",
                "size_bytes": 1000,
                "snapshot_count": 3,
                "jobs": {
                    "repack": {
                        "last_status": "success",
                        "last_success": "2026-08-01T04:20:00+00:00",
                        "last_run": {
                            "finished_at": "2026-08-01T04:20:00+00:00",
                            "metrics": {"unused_bytes": 40, "bytes_reclaimed": 900},
                        },
                    },
                    "prune": {
                        "last_run": {
                            "finished_at": "2026-07-26T03:10:00+00:00",
                            "metrics": {"unused_bytes": 4000},
                        }
                    },
                },
            }
        ],
    }
    values = entity_values(status)
    assert values["repack_next_run"] == "2026-09-01T04:17:00+00:00"
    assert values["vps_repack_status"] == "success"
    assert values["vps_unused_bytes"] == 40, "the newer of the two runs wins"
    assert ("repack", "vps") in commands_for(status).values()


def test_unused_space_is_unknown_until_something_measures_it() -> None:
    status = {
        "running": False,
        "jobs": {},
        "repositories": [{"slug": "vps", "name": "vps", "jobs": {}}],
    }
    assert entity_values(status)["vps_unused_bytes"] is None
