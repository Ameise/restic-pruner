"""Shared fixtures.

The end-to-end tests drive the real restic binary against repositories created
in temporary directories. They are skipped when restic is not installed, which
keeps ``pytest`` usable on a bare checkout while CI still runs them.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from restic_pruner.config import CheckConfig, PruneConfig, RepositoryConfig, Retention, Settings

RESTIC_AVAILABLE = shutil.which("restic") is not None
REPO_PASSWORD = "test-password"

requires_restic = pytest.mark.skipif(
    not RESTIC_AVAILABLE, reason="the restic binary is not installed"
)

#: Keep exactly one snapshot, so a three-snapshot repository loses two.
KEEP_ONE = Retention(keep_last=1, keep_hourly=0, keep_daily=0, keep_weekly=0, keep_monthly=0)


def make_settings(tmp_path: Path, *names: str) -> Settings:
    """Settings for one or more repositories rooted under *tmp_path*."""
    return Settings(
        repositories=tuple(
            RepositoryConfig(
                name=name,
                repository=str(tmp_path / f"repo-{name}"),
                password=REPO_PASSWORD,
                retention=KEEP_ONE,
            )
            for name in (names or ("main",))
        ),
        prune=PruneConfig(enabled=True, schedule="0 3 * * 0"),
        check=CheckConfig(enabled=True, schedule="0 5 * * 3", read_data_subset=""),
        data_dir=tmp_path / "data",
        retry_lock="1m",
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """A valid single-repository configuration."""
    return make_settings(tmp_path)


@pytest.fixture
def local_settings(settings: Settings) -> Settings:
    """Settings with the web server open, as when running outside Home Assistant."""
    return dataclasses.replace(settings, web=dataclasses.replace(settings.web, ingress_only=False))


def init_repository(settings: Settings, name: str, tmp_path: Path, snapshots: int = 3) -> None:
    """Create a repository with *snapshots* generations of changing data."""
    repository = settings.repository(name)
    assert repository is not None
    path = Path(repository.repository)
    path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / f"source-{name}"
    source.mkdir(exist_ok=True)
    env = repository.restic_env(settings.data_dir)
    _run(["restic", "init"], env)
    for _ in range(snapshots):
        # Overwrite rather than add, so each snapshot supersedes the previous
        # one's data and forgetting leaves unreferenced packs to reclaim.
        (source / "data.bin").write_bytes(os.urandom(1024 * 1024))
        _run(["restic", "backup", str(source)], env)


@pytest.fixture
def restic_repo(settings: Settings, tmp_path: Path) -> Iterator[Settings]:
    """One initialised repository with three snapshots."""
    if not RESTIC_AVAILABLE:
        pytest.skip("the restic binary is not installed")
    init_repository(settings, "main", tmp_path)
    yield settings


@pytest.fixture
def restic_repos(tmp_path: Path) -> Iterator[Settings]:
    """Two initialised repositories, for the batch tests."""
    if not RESTIC_AVAILABLE:
        pytest.skip("the restic binary is not installed")
    settings = make_settings(tmp_path, "vps", "nas")
    for name in ("vps", "nas"):
        init_repository(settings, name, tmp_path)
    yield settings


def snapshot_count(settings: Settings, name: str) -> int:
    """How many snapshots the named repository currently holds."""
    import json

    repository = settings.repository(name)
    assert repository is not None
    result = subprocess.run(
        ["restic", "snapshots", "--json"],
        env=repository.restic_env(settings.data_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    return len(json.loads(result.stdout))


def _run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(
            f"{' '.join(command)} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}"
        )
    return result
