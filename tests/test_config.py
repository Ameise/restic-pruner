from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from restic_pruner.config import (
    ConfigError,
    RepositoryConfig,
    Retention,
    Settings,
    load_settings,
    settings_from_options,
)


def test_retention_flags_skip_zero_values() -> None:
    retention = Retention(keep_last=0, keep_hourly=48, keep_daily=14, keep_weekly=0)
    flags = retention.as_flags()
    assert "--keep-hourly" in flags
    assert "--keep-last" not in flags
    assert "--keep-weekly" not in flags
    assert flags[flags.index("--keep-daily") + 1] == "14"


def test_retention_includes_group_by_and_within() -> None:
    retention = Retention(keep_within="7d", group_by="host")
    flags = retention.as_flags()
    assert flags[-2:] == ["--group-by", "host"]
    assert "--keep-within" in flags


def test_empty_retention_is_rejected() -> None:
    """An empty policy would tell restic to forget every snapshot."""
    retention = Retention(
        keep_last=0, keep_hourly=0, keep_daily=0, keep_weekly=0, keep_monthly=0, keep_yearly=0
    )
    with pytest.raises(ConfigError, match="Refusing to run forget"):
        retention.validate()


def test_negative_retention_is_rejected() -> None:
    with pytest.raises(ConfigError, match="must not be negative"):
        Retention(keep_daily=-1).validate()


def test_settings_require_a_repository(settings: Settings) -> None:
    with pytest.raises(ConfigError, match="no repository configured"):
        dataclasses.replace(settings, repositories=()).validate()


def test_repository_requires_a_password(settings: Settings) -> None:
    broken = dataclasses.replace(settings.repositories[0], password="", password_file="")
    with pytest.raises(ConfigError, match="password"):
        dataclasses.replace(settings, repositories=(broken,)).validate()


def test_repository_names_must_be_unique(settings: Settings) -> None:
    repository = settings.repositories[0]
    twice = dataclasses.replace(settings, repositories=(repository, repository))
    with pytest.raises(ConfigError, match="must be unique"):
        twice.validate()


def test_repository_slug_is_entity_safe() -> None:
    repository = RepositoryConfig(name="VPS / Hetzner!", repository="/repo", password="x")
    assert repository.slug == "vps_hetzner"


def test_invalid_cron_is_rejected(settings: Settings) -> None:
    broken = dataclasses.replace(
        settings, prune=dataclasses.replace(settings.prune, schedule="every tuesday")
    )
    with pytest.raises(ConfigError, match="not a valid cron expression"):
        broken.validate()


def test_disabled_jobs_skip_cron_validation(settings: Settings) -> None:
    dataclasses.replace(
        settings,
        prune=dataclasses.replace(settings.prune, enabled=False, schedule="nonsense"),
    ).validate()


def test_all_jobs_disabled_is_rejected(settings: Settings) -> None:
    broken = dataclasses.replace(
        settings,
        prune=dataclasses.replace(settings.prune, enabled=False),
        check=dataclasses.replace(settings.check, enabled=False),
    )
    with pytest.raises(ConfigError, match="no job is enabled"):
        broken.validate()


def test_restic_env_prefers_password_file(settings: Settings, tmp_path: Path) -> None:
    password_file = tmp_path / "pw"
    password_file.write_text("secret")
    repository = dataclasses.replace(settings.repositories[0], password_file=str(password_file))
    env = repository.restic_env(tmp_path, {"EXISTING": "1"})
    assert env["RESTIC_PASSWORD_FILE"] == str(password_file)
    assert "RESTIC_PASSWORD" not in env
    assert env["EXISTING"] == "1"
    assert env["RESTIC_REPOSITORY"] == repository.repository


def test_restic_env_explicit_environment_wins(settings: Settings, tmp_path: Path) -> None:
    repository = dataclasses.replace(
        settings.repositories[0], environment={"B2_ACCOUNT_ID": "from-config"}
    )
    env = repository.restic_env(tmp_path, {"B2_ACCOUNT_ID": "from-process"})
    assert env["B2_ACCOUNT_ID"] == "from-config"


def test_single_repository_shorthand() -> None:
    options = {
        "repository": "b2:bucket:path",
        "password": "hunter2",
        "environment": [
            {"name": "B2_ACCOUNT_ID", "value": "abc"},
            {"name": "B2_ACCOUNT_KEY", "value": "def"},
        ],
        "retention": {"keep_hourly": 24, "keep_daily": 7},
        "prune": {"schedule": "30 2 * * 1", "max_unused": "5%", "dry_run": True},
        "check": {"enabled": False},
        "mqtt": {"host": "core-mosquitto", "port": 1884},
    }
    settings = settings_from_options(options, {"TZ": "Europe/Berlin"})
    assert len(settings.repositories) == 1
    repository = settings.repositories[0]
    assert repository.name == "main"
    assert repository.repository == "b2:bucket:path"
    assert repository.environment == {"B2_ACCOUNT_ID": "abc", "B2_ACCOUNT_KEY": "def"}
    assert repository.retention.keep_hourly == 24
    assert settings.prune.schedule == "30 2 * * 1"
    assert settings.prune.dry_run is True
    assert settings.check.enabled is False
    assert settings.mqtt.explicit is True
    assert settings.timezone == "Europe/Berlin"


def test_repository_list_form() -> None:
    options = {
        "environment": [{"name": "SHARED", "value": "yes"}],
        "retention": {"keep_daily": 30},
        "repositories": [
            {
                "name": "vps",
                "repository": "b2:bucket:vps",
                "password": "one",
                "environment": ["B2_ACCOUNT_ID=vps-id"],
                "prune_healthchecks_url": "https://hc-ping.com/vps",
            },
            {
                "name": "nas",
                "repository": "/share/backups",
                "password": "two",
                # Overrides the shared default for this repository only.
                "keep_daily": 7,
            },
        ],
    }
    settings = settings_from_options(options, {})
    assert [repo.slug for repo in settings.repositories] == ["vps", "nas"]
    vps, nas = settings.repositories
    assert vps.environment == {"SHARED": "yes", "B2_ACCOUNT_ID": "vps-id"}
    assert vps.retention.keep_daily == 30
    assert nas.retention.keep_daily == 7, "per-repository overrides beat the shared default"
    assert nas.environment == {"SHARED": "yes"}
    assert settings.healthchecks_url(vps, "prune") == "https://hc-ping.com/vps"


def test_job_level_healthchecks_url_is_the_fallback() -> None:
    options = {
        "prune": {"healthchecks_url": "https://hc-ping.com/batch"},
        "repositories": [
            {"name": "a", "repository": "/a", "password": "x"},
            {
                "name": "b",
                "repository": "/b",
                "password": "x",
                "prune_healthchecks_url": "https://hc-ping.com/b",
            },
        ],
    }
    settings = settings_from_options(options, {})
    a, b = settings.repositories
    assert settings.healthchecks_url(a, "prune") == "https://hc-ping.com/batch"
    assert settings.healthchecks_url(b, "prune") == "https://hc-ping.com/b"


def test_repositories_must_be_a_list() -> None:
    with pytest.raises(ConfigError, match="must be a list"):
        settings_from_options({"repositories": "nope"}, {})


def test_environment_accepts_key_value_strings() -> None:
    settings = settings_from_options(
        {"repository": "/repo", "password": "x", "environment": ["A=1", "B=2"]}, {}
    )
    assert settings.repositories[0].environment == {"A": "1", "B": "2"}


def test_environment_accepts_a_multiline_string() -> None:
    settings = settings_from_options(
        {"repository": "/repo", "password": "x", "environment": "A=1\n# comment\n\nB=2"}, {}
    )
    assert settings.repositories[0].environment == {"A": "1", "B": "2"}


def test_environment_rejects_malformed_entries() -> None:
    with pytest.raises(ConfigError, match="KEY=value"):
        settings_from_options({"repository": "/repo", "environment": ["nope"]}, {})


def test_ingress_guard_defaults_to_supervisor_presence() -> None:
    inside = settings_from_options({"repository": "/repo"}, {"SUPERVISOR_TOKEN": "t"})
    outside = settings_from_options({"repository": "/repo"}, {})
    assert inside.web.ingress_only is True
    assert outside.web.ingress_only is False


def test_load_settings_reads_options_file(tmp_path: Path) -> None:
    options = tmp_path / "options.json"
    options.write_text(
        json.dumps({"repository": str(tmp_path), "password": "x", "check": {"enabled": False}})
    )
    settings = load_settings(options, {})
    assert settings.repositories[0].repository == str(tmp_path)
    assert settings.check.enabled is False


def test_load_settings_falls_back_to_env(tmp_path: Path) -> None:
    settings = load_settings(
        tmp_path / "missing.json",
        {
            "RESTIC_PRUNER_REPOSITORY": "/srv/repo",
            "RESTIC_PRUNER_PASSWORD": "x",
            "RESTIC_PRUNER_KEEP_DAILY": "9",
            "RESTIC_PRUNER_PRUNE_SCHEDULE": "0 4 * * *",
            "RESTIC_PRUNER_CHECK_ENABLED": "false",
        },
    )
    assert settings.repositories[0].repository == "/srv/repo"
    assert settings.repositories[0].retention.keep_daily == 9
    assert settings.prune.schedule == "0 4 * * *"
    assert settings.check.enabled is False


def test_load_settings_accepts_plain_restic_env(tmp_path: Path) -> None:
    """Standalone users already have RESTIC_REPOSITORY exported."""
    settings = load_settings(
        tmp_path / "missing.json",
        {"RESTIC_REPOSITORY": "sftp:host:/backups", "RESTIC_PASSWORD": "x"},
    )
    assert settings.repositories[0].repository == "sftp:host:/backups"
    assert settings.repositories[0].password == "x"


def test_several_repositories_from_env_json(tmp_path: Path) -> None:
    settings = load_settings(
        tmp_path / "missing.json",
        {
            "RESTIC_PRUNER_REPOSITORIES": json.dumps(
                [
                    {"name": "vps", "repository": "/a", "password": "x"},
                    {"name": "nas", "repository": "/b", "password": "y"},
                ]
            )
        },
    )
    assert [repo.slug for repo in settings.repositories] == ["vps", "nas"]


def test_malformed_repositories_json_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_settings(tmp_path / "missing.json", {"RESTIC_PRUNER_REPOSITORIES": "{oops"})


def test_timezone_falls_back_to_utc(settings: Settings) -> None:
    assert str(dataclasses.replace(settings, timezone="Mars/Olympus").tzinfo()) == "UTC"


def test_new_options_round_trip_through_the_supervisor_shape() -> None:
    settings = settings_from_options(
        {
            "repository": "/srv/repo",
            "password": "x",
            "healthchecks_body": "LOG",
            "lock_hostname": False,
            "prune": {"exact_reclaimed": True},
            "check": {"read_data_subset": " 4/13 ", "rotate_subset": False},
        },
        {},
    )
    settings.validate()
    assert settings.healthchecks_body == "log"
    assert settings.lock_hostname is False
    assert settings.prune.exact_reclaimed is True
    assert settings.check.read_data_subset == "4/13"
    assert settings.check.rotate_subset is False


def test_new_options_round_trip_through_env(tmp_path: Path) -> None:
    settings = load_settings(
        tmp_path / "missing.json",
        {
            "RESTIC_PRUNER_REPOSITORY": "/srv/repo",
            "RESTIC_PRUNER_PASSWORD": "x",
            "RESTIC_PRUNER_HEALTHCHECKS_BODY": "none",
            "RESTIC_PRUNER_LOCK_HOSTNAME": "false",
            "RESTIC_PRUNER_PRUNE_EXACT_RECLAIMED": "true",
            "RESTIC_PRUNER_CHECK_READ_DATA_SUBSET": "2/7",
            "RESTIC_PRUNER_CHECK_ROTATE_SUBSET": "false",
        },
    )
    assert settings.healthchecks_body == "none"
    assert settings.lock_hostname is False
    assert settings.prune.exact_reclaimed is True
    assert settings.check.read_data_subset == "2/7"
    assert settings.check.rotate_subset is False


def test_defaults_start_five_past_the_hour(settings: Settings) -> None:
    """A run that starts at :05 and takes minutes never meets a :15 backup."""
    defaults = settings_from_options({"repository": "/srv/repo", "password": "x"}, {})
    assert defaults.prune.schedule.split()[0] == "5"
    assert defaults.check.schedule.split()[0] == "5"
    assert defaults.check.read_data_subset == "1/4", "rotating, not a fixed sample"
    assert defaults.check.rotate_subset is True
    assert defaults.prune.exact_reclaimed is False, "no second stats call by default"


def test_repack_options_round_trip() -> None:
    settings = settings_from_options(
        {
            "repository": "/srv/repo",
            "password": "x",
            "repack": {
                "enabled": True,
                "schedule": "17 4 1 * *",
                "healthchecks_url": "https://hc-ping.com/abc",
                "max_unused": "2%",
                "max_repack_size": "2G",
                "dry_run": True,
            },
        },
        {},
    )
    settings.validate()
    assert settings.repack.enabled is True
    assert settings.repack.max_unused == "2%"
    assert settings.repack.max_repack_size == "2G"
    assert settings.repack.dry_run is True
    assert settings.job("repack") is settings.repack


def test_repack_options_round_trip_through_env(tmp_path: Path) -> None:
    settings = load_settings(
        tmp_path / "missing.json",
        {
            "RESTIC_PRUNER_REPOSITORY": "/srv/repo",
            "RESTIC_PRUNER_PASSWORD": "x",
            "RESTIC_PRUNER_REPACK_ENABLED": "true",
            "RESTIC_PRUNER_REPACK_MAX_UNUSED": "1G",
            "RESTIC_PRUNER_REPACK_SCHEDULE": "17 4 1 * *",
        },
    )
    assert settings.repack.enabled is True
    assert settings.repack.max_unused == "1G"


def test_repack_is_off_by_default() -> None:
    """It takes an exclusive lock to rewrite packs; that should be asked for."""
    settings = settings_from_options({"repository": "/srv/repo", "password": "x"}, {})
    assert settings.repack.enabled is False
    assert settings.repack.schedule == "17 4 1 * *"


def test_per_repository_repack_url() -> None:
    settings = settings_from_options(
        {
            "repositories": [
                {
                    "name": "vps",
                    "repository": "/srv/repo",
                    "password": "x",
                    "repack_healthchecks_url": "https://hc-ping.com/repack",
                }
            ],
            "repack": {"healthchecks_url": "https://hc-ping.com/fallback"},
        },
        {},
    )
    vps = settings.repositories[0]
    assert settings.healthchecks_url(vps, "repack") == "https://hc-ping.com/repack"
    assert settings.healthchecks_url(vps, "check") == ""
