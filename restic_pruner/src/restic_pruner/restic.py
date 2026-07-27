"""Thin async wrapper around the restic command line.

One instance targets one repository. Output is streamed to a log sink as restic
produces it, which is what feeds the web UI tail and the healthchecks.io body
during a long prune.

``forget --prune`` runs without ``--json``. Measured against restic 0.19,
``--json`` makes it emit the forget document and then suppress the prune phase
completely: no progress, no summary, nothing on either stream. The plain output
carries progress, the prune summary, and ``keep N snapshots:`` /
``remove N snapshots:`` counts that stay correct under ``--dry-run``, so that is
parsed instead. Reclaimed bytes come from ``stats --mode raw-data`` measured
before and after.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from .config import RepositoryConfig, Settings

LogSink = Callable[[str], None]

#: Documented in ``restic <command> --help`` under EXIT STATUS.
EXIT_CODE_MEANINGS: Final[dict[int, str]] = {
    1: "restic reported an error",
    3: "some source data could not be read",
    10: "the repository does not exist",
    11: "the repository is already locked by another restic process",
    12: "the repository password is incorrect",
    130: "restic was interrupted",
}

_PRUNE_SUMMARY_RE: Final = re.compile(
    r"^(?P<label>to repack|this removes|to delete|total prune|remaining):\s+"
    r"(?P<blobs>\d+) blobs / (?P<size>.+?)\s*$"
)
_PRUNE_PACKS_RE: Final = re.compile(r"^removing (?P<packs>\d+) old packs\s*$")
#: One pair per snapshot group, so the counts are summed rather than taken once.
_KEEP_RE: Final = re.compile(r"^keep (?P<count>\d+) snapshots?:")
_REMOVE_RE: Final = re.compile(r"^remove (?P<count>\d+) snapshots?:")
#: "2 snapshots have been removed, running prune" / "... would be removed, ..."
_RUNNING_PRUNE_RE: Final = re.compile(r"running prune")


class ResticError(Exception):
    """A restic invocation failed."""

    def __init__(self, command: Sequence[str], exit_code: int, tail: str = "") -> None:
        meaning = EXIT_CODE_MEANINGS.get(exit_code, "unknown error")
        name = command[0] if command else "?"
        message = f"restic {name} failed with exit code {exit_code}: {meaning}"
        if tail:
            message = f"{message}\n{tail}"
        super().__init__(message)
        self.command = tuple(command)
        self.exit_code = exit_code
        self.tail = tail

    @property
    def locked(self) -> bool:
        return self.exit_code == 11


class ResticNotFoundError(ResticError):
    """The restic binary is not installed or not on PATH."""

    def __init__(self, binary: str) -> None:
        super().__init__([binary], 127, f"{binary} not found on PATH")


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    duration: float


@dataclass(frozen=True, slots=True)
class RepoStats:
    total_size: int = 0
    total_uncompressed_size: int = 0
    total_blob_count: int = 0
    snapshots_count: int = 0

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> RepoStats:
        return cls(
            total_size=int(payload.get("total_size", 0)),
            total_uncompressed_size=int(payload.get("total_uncompressed_size", 0)),
            total_blob_count=int(payload.get("total_blob_count", 0)),
            snapshots_count=int(payload.get("snapshots_count", 0)),
        )


@dataclass(frozen=True, slots=True)
class ForgetPruneResult:
    """What one ``forget --prune`` invocation reported.

    Sizes are restic's own human-readable strings, kept for the log and the UI.
    Byte-accurate figures come from :class:`RepoStats` deltas instead.

    ``pruned`` is false when nothing was forgotten and restic therefore skipped
    the prune, which is normal and not a failure.
    """

    removed: int = 0
    kept: int = 0
    pruned: bool = False
    blobs_removed: int = 0
    blobs_repacked: int = 0
    packs_removed: int = 0
    summary: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_output(cls, lines: Iterable[str]) -> ForgetPruneResult:
        summary: dict[str, str] = {}
        blobs: dict[str, int] = {}
        packs_removed = 0
        removed = kept = 0
        pruned = False
        for line in lines:
            stripped = line.strip()
            if match := _PRUNE_SUMMARY_RE.match(stripped):
                label = match["label"]
                summary[label] = f"{match['blobs']} blobs / {match['size']}"
                blobs[label] = int(match["blobs"])
            elif match := _PRUNE_PACKS_RE.match(stripped):
                packs_removed = int(match["packs"])
            elif match := _KEEP_RE.match(stripped):
                # Summed: restic prints one pair of these per snapshot group.
                kept += int(match["count"])
            elif match := _REMOVE_RE.match(stripped):
                removed += int(match["count"])
            elif _RUNNING_PRUNE_RE.search(stripped):
                pruned = True
        return cls(
            removed=removed,
            kept=kept,
            pruned=pruned,
            blobs_removed=blobs.get("total prune", 0),
            blobs_repacked=blobs.get("to repack", 0),
            packs_removed=packs_removed,
            summary=summary,
        )


class Restic:
    """Runs restic subcommands against one repository."""

    def __init__(
        self,
        settings: Settings,
        repository: RepositoryConfig,
        log: LogSink | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._log: LogSink = log or (lambda _line: None)
        self._env = repository.restic_env(settings.data_dir)

    @property
    def repository(self) -> RepositoryConfig:
        return self._repository

    def with_log(self, log: LogSink) -> Restic:
        """Return a view of this wrapper that streams output to *log*."""
        clone = Restic.__new__(Restic)
        clone._settings = self._settings
        clone._repository = self._repository
        clone._log = log
        clone._env = self._env
        return clone

    # -- process plumbing ------------------------------------------------

    async def run(
        self,
        args: Sequence[str],
        *,
        json_stdout: bool = False,
        echo_stdout: bool = True,
    ) -> CommandResult:
        """Run ``restic <args>`` and stream its output to the log sink.

        When *json_stdout* is set, ``--json`` is added; pair it with
        ``echo_stdout=False`` so a machine-readable blob does not end up in the
        human log. Progress, which restic writes to stderr, always reaches the
        log.
        """
        binary = self._settings.restic_binary
        if shutil.which(binary) is None:
            raise ResticNotFoundError(binary)

        command = [binary, *self._global_flags(json_stdout), *args]
        self._log(f"$ restic {' '.join(args)}")
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
        )
        stdout_chunks: list[str] = []
        try:
            await asyncio.gather(
                self._pump(process.stdout, stdout_chunks, echo=echo_stdout),
                self._pump(process.stderr, None, echo=True),
            )
            exit_code = await process.wait()
        except asyncio.CancelledError:
            await self._terminate(process)
            raise

        duration = time.monotonic() - started
        stdout = "".join(stdout_chunks)
        if exit_code != 0:
            raise ResticError(list(args), exit_code, tail=_tail(stdout, 2000))
        return CommandResult(tuple(command), exit_code, stdout, duration)

    def _global_flags(self, json_stdout: bool) -> list[str]:
        flags: list[str] = []
        if self._settings.retry_lock:
            flags += ["--retry-lock", self._settings.retry_lock]
        if json_stdout:
            flags.append("--json")
        return flags

    async def _pump(
        self,
        stream: asyncio.StreamReader | None,
        sink: list[str] | None,
        *,
        echo: bool,
    ) -> None:
        if stream is None:
            return
        while True:
            try:
                raw = await stream.readline()
            except (asyncio.LimitOverrunError, ValueError):
                # Line longer than the stream buffer; read a chunk and continue.
                raw = await stream.read(64 * 1024)
            if not raw:
                return
            text = raw.decode("utf-8", errors="replace")
            if sink is not None:
                sink.append(text)
            if echo:
                stripped = text.rstrip("\n")
                if stripped:
                    self._log(stripped)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=30)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    # -- commands --------------------------------------------------------

    async def version(self) -> str:
        result = await self.run(["version"], echo_stdout=False)
        return result.stdout.strip()

    async def stats(self, mode: str = "raw-data") -> RepoStats:
        result = await self.run(["stats", "--mode", mode], json_stdout=True, echo_stdout=False)
        payload = _first_json_object(result.stdout)
        return RepoStats.from_json(payload) if payload else RepoStats()

    async def forget_and_prune(self, *, dry_run: bool = False) -> ForgetPruneResult:
        """Run ``restic forget --prune`` and report what it did.

        Output is streamed to the log as restic produces it, which is the only
        way a multi-hour prune shows progress, and parsed afterwards for the
        counts.
        """
        config = self._settings.prune
        args = ["forget", "--prune", *self._repository.retention.as_flags()]
        if config.max_unused:
            args += ["--max-unused", config.max_unused]
        if config.max_repack_size:
            args += ["--max-repack-size", config.max_repack_size]
        if dry_run:
            args.append("--dry-run")

        captured: list[str] = []
        sink = self._log

        def tee(line: str) -> None:
            captured.append(line)
            sink(line)

        await self.with_log(tee).run(args)
        result = ForgetPruneResult.from_output(captured)

        verb = "would remove" if dry_run else "removed"
        self._log(f"forget: {verb} {result.removed} snapshot(s), kept {result.kept}")
        if not result.pruned:
            self._log("prune: no snapshots were removed, so restic skipped it")
        return result

    async def check(self) -> None:
        config = self._settings.check
        args = ["check"]
        if config.read_data_subset:
            args += ["--read-data-subset", config.read_data_subset]
        if config.with_cache:
            args.append("--with-cache")
        await self.run(args)

    async def unlock(self, *, remove_all: bool = False) -> None:
        args = ["unlock"]
        if remove_all:
            args.append("--remove-all")
        await self.run(args)


def _tail(text: str, limit: int) -> str:
    stripped = text.strip()
    return stripped if len(stripped) <= limit else "..." + stripped[-limit:]


def _first_json_object(text: str) -> dict[str, Any] | None:
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None
