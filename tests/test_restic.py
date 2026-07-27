from __future__ import annotations

import dataclasses

import pytest

from restic_pruner.config import Settings
from restic_pruner.restic import (
    ForgetPruneResult,
    RepoStats,
    Restic,
    ResticError,
    ResticNotFoundError,
)

# Verbatim output from `restic forget --prune`, restic 0.19, captured from a
# real repository. Not run with --json: that flag suppresses the entire prune
# phase, progress and summary alike.
FORGET_PRUNE_OUTPUT = """\
Applying Policy: keep 1 latest snapshots
keep 1 snapshots:
ID        Time                 Host        Tags
------------------------------------------------
5ec65fec  2026-07-27 14:54:51  fmbp
------------------------------------------------
1 snapshots

remove 2 snapshots:
ID        Time                 Host        Tags
------------------------------------------------
68846e93  2026-07-27 14:54:50  fmbp
e28d342f  2026-07-27 14:54:49  fmbp
------------------------------------------------
2 snapshots

[0:00] 100.00%  2 / 2 files deleted
2 snapshots have been removed, running prune
loading all snapshots...
finding data that is still in use for 1 snapshots
[0:00] 100.00%  1 / 1 snapshots
searching used packs...
collecting packs for deletion and repacking
[0:00] 100.00%  6 / 6 packs processed

to repack:             3 blobs / 1.201 MiB
this removes:          1 blobs / 12 B
to delete:            20 blobs / 786.886 KiB
total prune:          21 blobs / 786.898 KiB
remaining:            10 blobs / 393.443 KiB
unused size after prune: 0 B (0.00% of remaining size)

rebuilding index
[0:00] 100.00%  3 / 3 indexes processed
removing 4 old packs
[0:00] 100.00%  4 / 4 files deleted
done
"""

DRY_RUN_OUTPUT = """\
Applying Policy: keep 1 latest snapshots
keep 1 snapshots:
1 snapshots

remove 2 snapshots:
2 snapshots

Would have removed the following snapshots:
{68846e93 e28d342f}

2 snapshots would be removed, running prune dry run
loading all snapshots...
searching used packs...

Would have made the following changes:

to repack:             0 blobs / 0 B
this removes:          0 blobs / 0 B
to delete:            20 blobs / 786.886 KiB
"""

NOTHING_TO_DO_OUTPUT = """\
Applying Policy: keep 1 latest snapshots
keep 1 snapshots:
1 snapshots

"""


def test_forget_prune_output_is_parsed() -> None:
    result = ForgetPruneResult.from_output(FORGET_PRUNE_OUTPUT.splitlines())
    assert result.removed == 2
    assert result.kept == 1
    assert result.pruned is True
    assert result.blobs_removed == 21
    assert result.blobs_repacked == 3
    assert result.packs_removed == 4
    assert result.summary["remaining"] == "10 blobs / 393.443 KiB"


def test_dry_run_output_reports_what_would_happen() -> None:
    result = ForgetPruneResult.from_output(DRY_RUN_OUTPUT.splitlines())
    assert result.removed == 2, "a dry run still says how many it would remove"
    assert result.kept == 1
    assert result.pruned is True
    assert result.summary["to delete"] == "20 blobs / 786.886 KiB"


def test_a_skipped_prune_is_recognised() -> None:
    """restic skips the prune phase when forget removed nothing."""
    result = ForgetPruneResult.from_output(NOTHING_TO_DO_OUTPUT.splitlines())
    assert result.removed == 0
    assert result.kept == 1
    assert result.pruned is False
    assert result.summary == {}


def test_counts_are_summed_across_snapshot_groups() -> None:
    """With the default --group-by, restic prints one pair per group."""
    grouped = [
        "keep 2 snapshots:",
        "remove 1 snapshots:",
        "keep 3 snapshots:",
        "remove 4 snapshots:",
        "5 snapshots have been removed, running prune",
    ]
    result = ForgetPruneResult.from_output(grouped)
    assert result.kept == 5
    assert result.removed == 5


def test_parsing_survives_unexpected_output() -> None:
    result = ForgetPruneResult.from_output(["done", "", "no summary here"])
    assert result.removed == 0
    assert result.pruned is False
    assert result.summary == {}


def test_repo_stats_from_json() -> None:
    stats = RepoStats.from_json(
        {
            "total_size": 802879,
            "total_uncompressed_size": 805324,
            "total_blob_count": 12,
            "snapshots_count": 1,
        }
    )
    assert stats.total_size == 802879
    assert stats.snapshots_count == 1


def test_restic_error_explains_known_exit_codes() -> None:
    error = ResticError(["prune"], 11)
    assert "already locked" in str(error)
    assert error.locked is True
    assert ResticError(["check"], 12).locked is False
    assert "password is incorrect" in str(ResticError(["check"], 12))


async def test_missing_binary_raises_a_clear_error(settings: Settings) -> None:
    configured = dataclasses.replace(settings, restic_binary="restic-does-not-exist")
    with pytest.raises(ResticNotFoundError, match="not found on PATH"):
        await Restic(configured, configured.repositories[0]).run(["version"])


def test_global_flags_include_retry_lock(settings: Settings) -> None:
    configured = dataclasses.replace(settings, retry_lock="30m")
    restic = Restic(configured, configured.repositories[0])
    lines: list[str] = []
    clone = restic.with_log(lines.append)
    # Internal, but --retry-lock is what keeps a concurrent backup from failing.
    assert clone._global_flags(json_stdout=True) == ["--retry-lock", "30m", "--json"]
