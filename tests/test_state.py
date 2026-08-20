from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from restic_pruner.state import (
    RunRecord,
    RunStatus,
    StateStore,
    Trigger,
    new_run_id,
)


def _run(
    job: str = "prune",
    status: RunStatus = RunStatus.SUCCESS,
    age: int = 0,
    repository: str = "main",
) -> RunRecord:
    started = datetime.now(UTC) - timedelta(minutes=age)
    return RunRecord(
        id=new_run_id(),
        job=job,  # type: ignore[arg-type]
        repository=repository,
        status=status,
        trigger=Trigger.SCHEDULE,
        started_at=started,
        finished_at=started + timedelta(seconds=42),
        metrics={"bytes_reclaimed": 1024},
    )


def test_roundtrip(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.load()
    run = _run()
    store.add(run)

    reloaded = StateStore(tmp_path)
    reloaded.load()
    assert [r.id for r in reloaded.runs()] == [run.id]
    assert reloaded.runs()[0].metrics == {"bytes_reclaimed": 1024}
    assert reloaded.runs()[0].duration_seconds == 42


def test_history_is_trimmed_and_logs_follow(tmp_path: Path) -> None:
    store = StateStore(tmp_path, history_limit=3, log_limit=3)
    store.load()
    ids = []
    for _ in range(5):
        run = _run()
        ids.append(run.id)
        store.add(run)
        store.write_log(run.id, ["line"])

    assert len(store.runs()) == 3
    assert store.read_log(ids[-1]) is not None
    assert store.read_log(ids[0]) is None, "logs for evicted runs should be deleted"
    assert len(list((tmp_path / "logs").glob("*.log"))) == 3


def test_records_outlive_their_logs(tmp_path: Path) -> None:
    """The point of the two limits: keep the overview, drop the bulk."""
    store = StateStore(tmp_path, history_limit=0, log_limit=2)
    store.load()
    ids = []
    for _ in range(6):
        run = _run()
        ids.append(run.id)
        store.add(run)
        store.write_log(run.id, ["a log line"])

    assert len(store.runs()) == 6, "history_limit=0 keeps every record"
    assert len(list((tmp_path / "logs").glob("*.log"))) == 2
    assert store.read_log(ids[-1]) is not None, "the newest run keeps its log"
    assert store.read_log(ids[0]) is None, "the oldest lost only its log"
    assert store.get(ids[0]) is not None, "but its record is still there"


def test_an_unlimited_history_survives_a_reload(tmp_path: Path) -> None:
    store = StateStore(tmp_path, history_limit=0, log_limit=1)
    store.load()
    for _ in range(20):
        store.add(_run())

    reloaded = StateStore(tmp_path, history_limit=0, log_limit=1)
    reloaded.load()
    assert len(reloaded.runs()) == 20


def test_interrupted_runs_are_marked_failed_on_load(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.load()
    run = _run(status=RunStatus.RUNNING)
    run.finished_at = None
    store.add(run)

    reloaded = StateStore(tmp_path)
    reloaded.load()
    recovered = reloaded.runs()[0]
    assert recovered.status is RunStatus.FAILED
    assert recovered.error is not None
    assert "interrupted" in recovered.error


def test_corrupt_state_file_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text("{not json")
    store = StateStore(tmp_path)
    store.load()
    assert store.runs() == []


def test_last_run_skips_in_flight_runs(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.load()
    finished = _run(age=10)
    store.add(finished)
    running = _run(status=RunStatus.RUNNING)
    store.add(running)
    latest = store.last_run("prune", "main")
    in_flight = store.last_run("prune", "main", only_finished=False)
    assert latest is not None and latest.id == finished.id
    assert in_flight is not None and in_flight.id == running.id


def test_runs_can_be_filtered_by_repository(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.load()
    store.add(_run(repository="vps"))
    store.add(_run(repository="nas"))
    store.add(_run(job="check", repository="vps"))
    assert len(store.runs(repository="vps")) == 2
    assert len(store.runs("check", repository="vps")) == 1
    assert len(store.runs("check", repository="nas")) == 0
    assert store.last_run("prune", "nas") is not None
    assert store.last_run("check", "nas") is None


def test_repository_snapshots_round_trip(tmp_path: Path) -> None:
    from restic_pruner.state import RepositorySnapshot

    store = StateStore(tmp_path)
    store.load()
    assert store.repository("vps").size_bytes == 0, "unknown repositories read as empty"
    store.set_repository("vps", RepositorySnapshot(size_bytes=4096, snapshot_count=7))
    store.save()

    reloaded = StateStore(tmp_path)
    reloaded.load()
    assert reloaded.repository("vps").size_bytes == 4096
    assert reloaded.repository("vps").snapshot_count == 7


def test_runs_can_be_filtered_by_job(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.load()
    store.add(_run(job="prune"))
    store.add(_run(job="check"))
    assert len(store.runs("check")) == 1
    assert len(store.runs()) == 2
    assert len(store.runs(limit=1)) == 1


def test_update_replaces_in_place(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.load()
    run = _run(status=RunStatus.RUNNING)
    store.add(run)
    run.status = RunStatus.SUCCESS
    store.update(run)
    assert len(store.runs()) == 1
    assert store.runs()[0].status is RunStatus.SUCCESS
