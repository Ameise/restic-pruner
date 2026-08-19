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
import logging
import re
import shlex
import shutil
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .config import RepositoryConfig, Settings

_LOGGER: Final = logging.getLogger(__name__)

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


#: What the lock says when a job runs in its own UTS namespace. restic has no
#: flag for the identity in its lock file -- it writes whatever ``gethostname()``
#: returns -- so the only lever is giving the process its own hostname.
_UNSHARE_ARGS: Final = ("unshare", "--user", "--map-root-user", "--uts")

#: ``$0`` is cosmetic; ``$1`` is the hostname and the rest is the command.
_SET_HOSTNAME_SH: Final = 'hostname "$1"; shift; exec "$@"'

_uts_namespace: bool | None = None


async def uts_namespace_available() -> bool:
    """Whether a job can be given its own hostname without extra privileges.

    ``unshare --user`` opens a throwaway *user* namespace first, inside which the
    process holds ``CAP_SYS_ADMIN`` and may therefore create a UTS namespace and
    rename it. Nothing has to be granted to the container, so the add-on stays
    unprivileged. Kernels that forbid unprivileged user namespaces refuse it, and
    then jobs simply run as they always did.

    Probed once and remembered; the answer cannot change while we are running.
    """
    global _uts_namespace
    if _uts_namespace is None:
        _uts_namespace = await _probe_uts_namespace()
    return _uts_namespace


async def _probe_uts_namespace() -> bool:
    if shutil.which("unshare") is None:
        _LOGGER.info("unshare is not installed; restic locks will show the container hostname")
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            *_UNSHARE_ARGS,
            "hostname",
            "restic-pruner-probe",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        ok = await process.wait() == 0
    except OSError as exc:
        _LOGGER.info("could not probe for a UTS namespace: %s", exc)
        return False
    if not ok:
        _LOGGER.info(
            "the kernel refused an unprivileged user namespace; restic locks will show "
            "the container hostname instead of the job name"
        )
    return ok


def namespaced(command: Sequence[str], hostname: str) -> list[str]:
    """Wrap *command* so it runs under its own *hostname*.

    Everything travels as separate argv entries, so a hostname can never be read
    as shell syntax.
    """
    return [*_UNSHARE_ARGS, "sh", "-c", _SET_HOSTNAME_SH, "sh", hostname, *command]


def reset_uts_namespace_probe(value: bool | None = None) -> None:
    """Set or forget the cached probe result. For tests."""
    global _uts_namespace
    _uts_namespace = value


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


class CheckError(ResticError):
    """``restic check`` failed, with whatever it managed to report attached."""

    def __init__(self, cause: ResticError, result: CheckResult) -> None:
        super().__init__(cause.command, cause.exit_code, cause.tail)
        self.result = result


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
    #: ``total prune:`` and ``remaining:`` in bytes. restic prints these itself,
    #: which is what makes a second ``stats`` call over the network avoidable.
    bytes_removed: int = 0
    bytes_remaining: int = 0

    @classmethod
    def from_output(cls, lines: Iterable[str]) -> ForgetPruneResult:
        summary: dict[str, str] = {}
        blobs: dict[str, int] = {}
        sizes: dict[str, int] = {}
        packs_removed = 0
        removed = kept = 0
        pruned = False
        for line in lines:
            stripped = line.strip()
            if match := _PRUNE_SUMMARY_RE.match(stripped):
                label = match["label"]
                summary[label] = f"{match['blobs']} blobs / {match['size']}"
                blobs[label] = int(match["blobs"])
                sizes[label] = parse_size(match["size"])
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
            bytes_removed=sizes.get("total prune", 0),
            bytes_remaining=sizes.get("remaining", 0),
        )


#: An object restic named in an error. Ids are the only part of a check error
#: that is safe to repeat outside the add-on: everything else on those lines can
#: carry hostnames, tags and the absolute paths of the backed-up files.
_CHECK_OBJECT_RE: Final = re.compile(
    r"\b(pack|tree|blob|snapshot|index)\s+(?:file\s+)?([0-9a-f]{8,64})\b", re.IGNORECASE
)

#: Ordered: the first phrase that appears in a line decides how it is labelled.
_CHECK_PROBLEMS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("missing", ("does not exist", "not found", "is missing", "missing")),
    ("orphaned", ("not referenced", "orphaned", "duplicate")),
    (
        "damaged",
        (
            "does not match",
            "mismatch",
            "truncated",
            "corrupt",
            "damaged",
            "broken",
            "invalid",
            "could not be loaded",
            "could not be found",
        ),
    ),
)

#: restic's own abbreviation length for object ids.
_ID_LENGTH: Final = 10

#: How many individual objects a report names before it starts counting instead.
MAX_FINDINGS: Final = 20

#: Hard ceiling on what is kept in memory: a thoroughly broken repository can
#: produce an error per object, and none of it changes the conclusion.
_FINDING_CEILING: Final = 500

_CHECK_PACKS_RE: Final = re.compile(r"(\d+) / (\d+) packs")


@dataclass(frozen=True, slots=True)
class CheckFinding:
    """One damaged or missing object, reduced to what is safe to report."""

    kind: str
    id: str
    problem: str

    def __str__(self) -> str:
        return f"{self.kind} {self.id} {self.problem}"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """What one ``check`` invocation found.

    Built only from object ids and this program's own labels. restic's error
    lines are never carried over verbatim -- they quote file names.
    """

    findings: tuple[CheckFinding, ...] = ()
    #: Errors restic reported that named no object.
    other_errors: int = 0
    packs_read: int = 0
    packs_total: int = 0
    subset: str = ""
    #: More objects were named than :data:`_FINDING_CEILING` allows keeping.
    truncated: bool = False

    @property
    def total_problems(self) -> int:
        return len(self.findings) + self.other_errors

    def counts(self) -> dict[str, int]:
        """How many objects of each kind hit each problem, e.g. ``pack missing``."""
        counts: dict[str, int] = {}
        for finding in self.findings:
            key = f"{finding.kind} {finding.problem}"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def headline(self) -> str:
        """``2 packs missing, 1 tree damaged``, or an empty string when clean."""
        parts = [
            f"{count} {kind.split(' ')[0]}{'s' if count != 1 else ''} {kind.split(' ', 1)[1]}"
            for kind, count in sorted(self.counts().items())
        ]
        if self.other_errors:
            parts.append(f"{self.other_errors} further error(s)")
        if self.truncated:
            parts.append("and more")
        return ", ".join(parts)

    @classmethod
    def from_output(cls, lines: Iterable[str], subset: str = "") -> CheckResult:
        findings: list[CheckFinding] = []
        seen: set[tuple[str, str]] = set()
        other = 0
        truncated = False
        packs_read = packs_total = 0
        for line in lines:
            for read, total in _CHECK_PACKS_RE.findall(line):
                packs_read, packs_total = int(read), int(total)
            problem = _problem_in(line)
            if problem is None:
                continue
            objects = _CHECK_OBJECT_RE.findall(line)
            if not objects:
                other += 1
                continue
            for kind, object_id in objects:
                key = (kind.lower(), object_id[:_ID_LENGTH])
                if key in seen:
                    continue
                seen.add(key)
                if len(findings) >= _FINDING_CEILING:
                    truncated = True
                    continue
                findings.append(CheckFinding(key[0], key[1], problem))
        return cls(
            findings=tuple(findings),
            other_errors=other,
            packs_read=packs_read,
            packs_total=packs_total,
            subset=subset,
            truncated=truncated,
        )


#: restic says this when everything is fine, and it contains the word "error".
_CHECK_ALL_CLEAR: Final = ("no errors were found", "no errors found")


def _problem_in(line: str) -> str | None:
    """Classify an error line, or return ``None`` if it is not one."""
    lowered = line.lower()
    if any(phrase in lowered for phrase in _CHECK_ALL_CLEAR):
        return None
    if not any(
        marker in lowered for marker in ("error", "fatal", "missing", "does not", "not found")
    ):
        return None
    for label, phrases in _CHECK_PROBLEMS:
        if any(phrase in lowered for phrase in phrases):
            return label
    return "damaged"


class Restic:
    """Runs restic subcommands against one repository."""

    def __init__(
        self,
        settings: Settings,
        repository: RepositoryConfig,
        log: LogSink | None = None,
        hostname: str = "",
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._log: LogSink = log or (lambda _line: None)
        self._env = repository.restic_env(settings.data_dir)
        self._hostname = hostname

    @property
    def repository(self) -> RepositoryConfig:
        return self._repository

    def with_log(self, log: LogSink) -> Restic:
        """Return a view of this wrapper that streams output to *log*."""
        return self._clone(log=log)

    def with_hostname(self, hostname: str) -> Restic:
        """Return a view whose invocations claim *hostname* in the repository lock.

        This is what makes ``prune`` and ``check`` distinguishable from the other
        side of a shared repository, where both otherwise appear as the container.
        """
        return self._clone(hostname=hostname)

    def _clone(self, *, log: LogSink | None = None, hostname: str | None = None) -> Restic:
        clone = Restic.__new__(Restic)
        clone._settings = self._settings
        clone._repository = self._repository
        clone._log = self._log if log is None else log
        clone._env = self._env
        clone._hostname = self._hostname if hostname is None else hostname
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
        # The whole command line, not just the subcommand: what a check actually
        # read should be answerable from the job history, not from the source.
        # Credentials travel in the environment and never appear here.
        self._log(f"$ {shlex.join([Path(binary).name, *command[1:]])}")

        hostname = self._hostname if self._settings.lock_hostname else ""
        if hostname and await uts_namespace_available():
            self._log(f"# running as host {hostname}, so the repository lock names this job")
            command = namespaced(command, hostname)

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

    async def check(self, subset: str | None = None) -> CheckResult:
        """Run ``restic check`` and report what it found.

        *subset* overrides the configured read scope, which is how the rotating
        ``n/t`` slice is applied. Failures raise :class:`CheckError`, which
        carries the same report, because a failed check is exactly the one whose
        findings someone needs.
        """
        config = self._settings.check
        scope = config.read_data_subset if subset is None else subset
        args = ["check"]
        if scope:
            args += ["--read-data-subset", scope]
        if config.with_cache:
            args.append("--with-cache")

        captured: list[str] = []
        sink = self._log

        def tee(line: str) -> None:
            captured.append(line)
            sink(line)

        try:
            await self._clone(log=tee).run(args)
        except ResticError as exc:
            raise CheckError(exc, CheckResult.from_output(captured, scope)) from exc
        result = CheckResult.from_output(captured, scope)
        if result.total_problems:
            # restic exited 0, so this is advisory -- unreferenced packs and the
            # like. Worth saying out loud, not worth failing the run over.
            self._log(f"check: {result.headline()}")
        return result

    async def unlock(self, *, remove_all: bool = False) -> None:
        args = ["unlock"]
        if remove_all:
            args.append("--remove-all")
        await self.run(args)


#: restic renders sizes as ``68.519 MiB``; the plain ``B`` case has no prefix.
_SIZE_RE: Final = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMGTP]?)i?B$", re.IGNORECASE)

_SIZE_MULTIPLIERS: Final = {
    "": 1,
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
    "P": 1024**5,
}


def parse_size(value: str) -> int:
    """Turn one of restic's human-readable sizes into bytes; 0 if unparseable."""
    match = _SIZE_RE.match(value.strip())
    if match is None:
        return 0
    return int(float(match[1]) * _SIZE_MULTIPLIERS[match[2].upper()])


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
