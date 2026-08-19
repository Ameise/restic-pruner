"""What ends up in the healthchecks.io ping body.

restic's forget listing names every host, tag and absolute path in the
repository. Sending that to a third party on every run discloses the shape of
the infrastructure being backed up and is a needless load on the receiving
service, so the default body is built only from values this program generates.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from restic_pruner.config import ConfigError, Settings
from restic_pruner.jobs import summary_body
from restic_pruner.state import RunRecord, RunStatus, Trigger

# A realistic excerpt of what restic prints, and what must never be forwarded.
RESTIC_LISTING = """\
ID        Time                 Host          Tags       Paths
aa214d24  2026-07-27 03:45:33  example-host  scheduled  /srv/example/data
                                                        /srv/example/spool
61 snapshots
"""

SECRETS = ("/srv/example", "example-host", "scheduled", "spool")


def _run(**overrides: object) -> RunRecord:
    started = datetime.now(UTC)
    defaults: dict[str, object] = {
        "id": "abc",
        "job": "prune",
        "repository": "vps",
        "status": RunStatus.SUCCESS,
        "trigger": Trigger.SCHEDULE,
        "started_at": started,
        "finished_at": started + timedelta(seconds=10.1),
        "metrics": {
            "snapshots_removed": 1,
            "snapshots_kept": 61,
            "pruned": True,
            "blobs_removed": 6,
            "blobs_repacked": 380,
            "packs_removed": 1,
            "bytes_reclaimed": 161690,
            "repo_size_before": 14050000,
            "repo_size_after": 13888310,
        },
    }
    return RunRecord(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_summary_is_small_and_reports_the_outcome() -> None:
    body = summary_body(_run())
    assert len(body) < 400, f"body should stay tiny, was {len(body)} bytes"
    assert "prune on vps: success in 10.1s" in body
    assert "snapshots: removed 1, kept 61" in body
    assert "380 repacked" in body
    assert "reclaimed 157.9 KiB" in body


def test_summary_never_contains_restic_output() -> None:
    """The regression this exists for: paths and hostnames leaking to a third party."""
    error = f"restic forget failed with exit code 1: restic reported an error\n{RESTIC_LISTING}"
    run = _run(error=error)
    body = summary_body(run)
    for secret in SECRETS:
        assert secret not in body, f"{secret!r} leaked into the ping body"
    # The generated first line survives, so the alert still says what went wrong.
    assert "restic forget failed with exit code 1" in body
    assert "61 snapshots" not in body


def test_summary_of_a_skipped_prune() -> None:
    body = summary_body(
        _run(metrics={"snapshots_removed": 0, "snapshots_kept": 61, "pruned": False})
    )
    assert "prune: skipped, nothing was forgotten" in body


def test_summary_of_a_check_run() -> None:
    body = summary_body(
        _run(
            job="check",
            metrics={
                "read_data_subset": "4/13",
                "snapshot_count": 61,
                "packs_read": 312,
                "packs_total": 312,
            },
        )
    )
    assert "check on vps: success" in body
    assert "verified: 4/13 (312 of 312 packs read)" in body
    assert "snapshots: 61" in body


def test_a_failed_check_names_the_damaged_objects() -> None:
    """What separates "check failed" from a body someone can act on."""
    body = summary_body(
        _run(
            job="check",
            status=RunStatus.FAILED,
            metrics={
                "read_data_subset": "4/13",
                "findings": ["pack 6dcad00d1e missing", "tree 4f77aa1c02 damaged"],
                "finding_count": 2,
                "findings_headline": "1 pack missing, 1 tree damaged",
            },
            error=(
                f"restic check failed with exit code 1: restic reported an error\n{RESTIC_LISTING}"
            ),
        )
    )
    assert "1 pack missing, 1 tree damaged" in body
    assert "pack 6dcad00d1e missing" in body
    assert "tree 4f77aa1c02 damaged" in body
    assert "restic check failed with exit code 1" in body
    for secret in SECRETS:
        assert secret not in body, f"{secret!r} leaked into the ping body"


def test_a_long_finding_list_is_capped() -> None:
    body = summary_body(
        _run(
            job="check",
            status=RunStatus.FAILED,
            metrics={
                "findings": [f"pack {index:010x} missing" for index in range(20)],
                "finding_count": 137,
                "findings_headline": "137 packs missing",
            },
        )
    )
    assert "... and 117 more" in body
    assert len(body) < 1000


def test_dry_runs_are_labelled() -> None:
    assert "(dry run)" in summary_body(_run(dry_run=True))


def test_a_failure_without_metrics_still_produces_a_body() -> None:
    body = summary_body(
        _run(
            status=RunStatus.FAILED,
            metrics={},
            error="restic prune failed with exit code 11: the repository is already locked",
        )
    )
    assert "failed" in body
    assert "already locked" in body


def test_body_mode_is_validated(settings: Settings) -> None:
    dataclasses.replace(settings, healthchecks_body="log").validate()
    dataclasses.replace(settings, healthchecks_body="none").validate()
    with pytest.raises(ConfigError, match="healthchecks_body must be one of"):
        dataclasses.replace(settings, healthchecks_body="everything").validate()


def test_summary_is_the_default(settings: Settings) -> None:
    assert settings.healthchecks_body == "summary"
