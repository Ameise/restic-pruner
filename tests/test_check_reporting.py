"""What a check reports, and how its read scope rotates.

Two properties matter here. A failed check must name the objects that are
broken, because "check failed" only starts an ssh session where "pack
6dcad00d1e missing" starts the actual work. And it must name *only* those
objects: restic's error lines quote the file names inside the damaged trees.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from restic_pruner.config import (
    CheckConfig,
    ConfigError,
    Settings,
    parse_subset_slice,
    validate_read_data_subset,
)
from restic_pruner.healthchecks import HealthchecksClient
from restic_pruner.jobs import JobRunner
from restic_pruner.restic import CheckResult, ForgetPruneResult, namespaced, parse_size
from restic_pruner.state import StateStore

#: A failing run, including the line shape that quotes a backed-up file name.
BROKEN_CHECK = """\
using temporary cache in /tmp/restic-check-cache-1234
load indexes
check all packs
pack 6dcad00d1e4f0a2b1c: does not exist
error for tree 4f77aa1c0299bb: file "/srv/example/invoices/2026.pdf" blob 0 size could not be found
pack 91b0f2c4aa11ff is missing from the repository
Fatal: repository contains errors
""".splitlines()

CLEAN_CHECK = """\
using temporary cache in /tmp/restic-check-cache-1234
load indexes
check all packs
check snapshots, trees and blobs
read 4 / 13 data packs
[0:41] 100.00%  312 / 312 packs
no errors were found
""".splitlines()


def test_findings_name_the_objects() -> None:
    result = CheckResult.from_output(BROKEN_CHECK, "4/13")
    assert [str(finding) for finding in result.findings] == [
        "pack 6dcad00d1e missing",
        "tree 4f77aa1c02 damaged",
        "pack 91b0f2c4aa missing",
    ]
    assert result.headline() == "2 packs missing, 1 tree damaged, 1 further error(s)"


def test_findings_never_carry_file_names() -> None:
    """The regression this file exists for."""
    result = CheckResult.from_output(BROKEN_CHECK, "4/13")
    rendered = result.headline() + " ".join(str(finding) for finding in result.findings)
    for secret in ("/srv/example", "invoices", "2026.pdf", "restic-check-cache"):
        assert secret not in rendered, f"{secret!r} escaped the parser"


def test_a_clean_check_reports_what_it_read() -> None:
    result = CheckResult.from_output(CLEAN_CHECK, "4/13")
    assert result.findings == ()
    assert result.total_problems == 0
    assert (result.packs_read, result.packs_total) == (312, 312)


def test_findings_are_capped() -> None:
    lines = [f"pack {index:010x}beef: does not exist" for index in range(600)]
    result = CheckResult.from_output(lines)
    assert len(result.findings) == 500
    assert result.truncated
    assert "and more" in result.headline()


# -- read scope --------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "1/4", "4/13", "5%", "0.5%", "500M"])
def test_valid_subsets(value: str) -> None:
    validate_read_data_subset(value)


@pytest.mark.parametrize("value", ["0/4", "5/4", "4/0", "120%", "0%", "every second one"])
def test_invalid_subsets_are_refused_at_startup(value: str) -> None:
    with pytest.raises(ConfigError, match="read_data_subset"):
        validate_read_data_subset(value)


def _runner(settings: Settings, tmp_path: Path) -> JobRunner:
    state = StateStore(tmp_path / "state")
    state.load()
    # No pings are sent here; the runner only needs a client to hold.
    return JobRunner(settings, state, HealthchecksClient(None))  # type: ignore[arg-type]


def test_the_slice_advances_and_wraps(settings: Settings, tmp_path: Path) -> None:
    settings = dataclasses.replace(
        settings, check=CheckConfig(read_data_subset="1/3", rotate_subset=True)
    )
    runner = _runner(settings, tmp_path)
    repository = settings.repositories[0]

    seen = []
    for _ in range(4):
        subset = runner._check_subset(repository)
        seen.append(subset)
        runner._advance_check_subset(repository, subset)
    assert seen == ["1/3", "2/3", "3/3", "1/3"], "every part is read, then it starts over"


def test_a_failed_run_re_reads_the_same_slice(settings: Settings, tmp_path: Path) -> None:
    settings = dataclasses.replace(
        settings, check=CheckConfig(read_data_subset="1/3", rotate_subset=True)
    )
    runner = _runner(settings, tmp_path)
    repository = settings.repositories[0]
    assert runner._check_subset(repository) == "1/3"
    # No advance: the run failed before it finished reading.
    assert runner._check_subset(repository) == "1/3"


def test_rotation_off_pins_the_slice(settings: Settings, tmp_path: Path) -> None:
    settings = dataclasses.replace(
        settings, check=CheckConfig(read_data_subset="2/3", rotate_subset=False)
    )
    runner = _runner(settings, tmp_path)
    repository = settings.repositories[0]
    subset = runner._check_subset(repository)
    runner._advance_check_subset(repository, subset)
    assert subset == "2/3"
    assert runner._check_subset(repository) == "2/3"


def test_a_percentage_is_passed_through_untouched(settings: Settings, tmp_path: Path) -> None:
    settings = dataclasses.replace(settings, check=CheckConfig(read_data_subset="5%"))
    runner = _runner(settings, tmp_path)
    assert runner._check_subset(settings.repositories[0]) == "5%"
    assert parse_subset_slice("5%") is None


# -- prune sizes without a stats call ----------------------------------------


def test_prune_sizes_come_from_prune_itself() -> None:
    """§7b: the trailing stats call is what these numbers replace."""
    result = ForgetPruneResult.from_output(
        [
            "keep 122 snapshots:",
            "remove 706 snapshots:",
            "2 snapshots have been removed, running prune",
            "to repack:          1327 blobs / 33.418 MiB",
            "removing 1427 old packs",
            "total prune:        5897 blobs / 68.519 MiB",
            "remaining:          1660 blobs / 135.222 MiB",
        ]
    )
    assert result.removed == 706
    assert result.kept == 122
    assert result.bytes_removed == parse_size("68.519 MiB")
    assert result.bytes_remaining == parse_size("135.222 MiB")


@pytest.mark.parametrize(
    ("text", "expected"),
    [("512 B", 512), ("68.519 MiB", 71847378), ("1.5 GiB", 1610612736), ("nonsense", 0)],
)
def test_size_parsing(text: str, expected: int) -> None:
    assert parse_size(text) == expected


# -- lock identity -----------------------------------------------------------


def test_the_namespace_wrapper_passes_the_hostname_as_an_argument() -> None:
    """A hostname must never be able to turn into shell syntax."""
    wrapped = namespaced(["restic", "check", "--read-data-subset", "4/13"], "restic-pruner-check")
    assert wrapped[:4] == ["unshare", "--user", "--map-root-user", "--uts"]
    assert "restic-pruner-check" in wrapped, "passed as its own argv entry"
    assert wrapped[-4:] == ["restic", "check", "--read-data-subset", "4/13"]
    script = wrapped[wrapped.index("-c") + 1]
    assert "restic-pruner-check" not in script, "never interpolated into the script"
