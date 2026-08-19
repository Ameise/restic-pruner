"""Configuration loading and validation.

Two sources are supported and they are mutually exclusive per run:

* ``/data/options.json`` -- written by the Home Assistant Supervisor from the
  add-on options schema in ``restic_pruner/config.yaml``.
* environment variables prefixed ``RESTIC_PRUNER_`` -- used when running the
  same image as a plain Docker container.

One instance maintains one *or more* repositories. They are always normalised
into :attr:`Settings.repositories`, so the rest of the program never has to care
whether the user wrote the single-repository shorthand or the list form.

The resulting :class:`Settings` object is frozen; everything downstream reads
from it and never mutates it.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

JobName = Literal["prune", "check"]

JOB_NAMES: Final[tuple[JobName, ...]] = ("prune", "check")

#: Where the Supervisor drops the rendered add-on options.
SUPERVISOR_OPTIONS_PATH: Final = Path("/data/options.json")

#: Persistent add-on data directory (also used standalone via a volume).
DEFAULT_DATA_DIR: Final = Path("/data")

ENV_PREFIX: Final = "RESTIC_PRUNER_"

LOG_LEVELS: Final = ("trace", "debug", "info", "warning", "error")

#: What to put in the healthchecks.io ping body.
#:
#: ``summary`` sends only text this program generates -- counts, sizes and its
#: own error line -- and never restic's output. That matters because restic's
#: forget listing includes hostnames, tags and the absolute paths of everything
#: being backed up, which has no business being sent to a third party.
BODY_MODES: Final = ("summary", "log", "none")

#: Job defaults. The minute is deliberately not zero: a producer backing up on
#: the hour and every quarter past has a lock-wait budget, and a job that starts
#: at ``:05`` and finishes inside five minutes never collides with ``:15``.
DEFAULT_PRUNE_SCHEDULE: Final = "5 3 * * 0"
DEFAULT_CHECK_SCHEDULE: Final = "5 5 * * 3"

#: The n-th of four equal parts: four weekly runs verify all of the pack data.
DEFAULT_READ_DATA_SUBSET: Final = "1/4"

#: Used when the single-repository shorthand does not name the repository.
DEFAULT_REPOSITORY_NAME: Final = "main"

_SLUG_RE: Final = re.compile(r"[^a-z0-9_]+")

_KEEP_FIELDS: Final = ("last", "hourly", "daily", "weekly", "monthly", "yearly")


class ConfigError(Exception):
    """Raised when the supplied configuration cannot be used safely."""


@dataclass(frozen=True, slots=True)
class Retention:
    """A restic ``forget`` policy.

    At least one ``keep_*`` value must be set. restic refuses an empty policy
    itself; this fails earlier, at startup, with a clearer message.
    """

    keep_last: int = 0
    keep_hourly: int = 48
    keep_daily: int = 14
    keep_weekly: int = 8
    keep_monthly: int = 6
    keep_yearly: int = 0
    keep_within: str = ""
    keep_tags: tuple[str, ...] = ()
    group_by: str = "host,paths"

    def as_flags(self) -> list[str]:
        """Render the policy as restic ``forget`` command line flags."""
        flags: list[str] = []
        for name in _KEEP_FIELDS:
            value: int = getattr(self, f"keep_{name}")
            if value > 0:
                flags += [f"--keep-{name}", str(value)]
        if self.keep_within:
            flags += ["--keep-within", self.keep_within]
        for tag in self.keep_tags:
            flags += ["--keep-tag", tag]
        if self.group_by:
            flags += ["--group-by", self.group_by]
        return flags

    def validate(self, where: str = "retention") -> None:
        for name in _KEEP_FIELDS:
            if getattr(self, f"keep_{name}") < 0:
                raise ConfigError(f"{where}.keep_{name} must not be negative")
        has_policy = any(getattr(self, f"keep_{name}") > 0 for name in _KEEP_FIELDS) or bool(
            self.keep_within
        )
        if not has_policy:
            raise ConfigError(
                f"{where} is empty: at least one of keep_last, keep_hourly, keep_daily, "
                "keep_weekly, keep_monthly, keep_yearly or keep_within must be set. "
                "Refusing to run forget, which would remove every snapshot."
            )


@dataclass(frozen=True, slots=True)
class RepositoryConfig:
    """One restic repository and everything specific to it."""

    name: str
    repository: str
    password: str = ""
    password_file: str = ""
    environment: Mapping[str, str] = field(default_factory=dict)
    retention: Retention = field(default_factory=Retention)
    prune_healthchecks_url: str = ""
    check_healthchecks_url: str = ""

    @property
    def slug(self) -> str:
        """A name safe for entity ids and MQTT topics."""
        return _SLUG_RE.sub("_", self.name.strip().lower()).strip("_") or "repository"

    def healthchecks_url(self, job: JobName) -> str:
        return self.prune_healthchecks_url if job == "prune" else self.check_healthchecks_url

    def restic_env(self, data_dir: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """Build the environment restic is invoked with for this repository.

        The process environment is inherited so that backend credentials set the
        conventional way (``B2_ACCOUNT_ID``, ``AWS_ACCESS_KEY_ID``, ...) keep
        working, and this repository's own entries win over it.
        """
        env = dict(os.environ if base is None else base)
        env.update(self.environment)
        env["RESTIC_REPOSITORY"] = self.repository
        if self.password_file:
            env["RESTIC_PASSWORD_FILE"] = self.password_file
            env.pop("RESTIC_PASSWORD", None)
        else:
            env["RESTIC_PASSWORD"] = self.password
            env.pop("RESTIC_PASSWORD_FILE", None)
        env.setdefault("RESTIC_CACHE_DIR", str(data_dir / "cache"))
        env.setdefault("HOME", str(data_dir))
        return env

    def validate(self) -> None:
        where = f"repository {self.name!r}"
        if not self.name.strip():
            raise ConfigError("every repository needs a name")
        if not self.repository:
            raise ConfigError(
                f"{where}: repository is required (e.g. 'b2:my-bucket:backups' or '/share/backups')"
            )
        if not self.password and not self.password_file:
            raise ConfigError(f"{where}: either password or password_file must be set")
        if self.password_file and not Path(self.password_file).is_file():
            raise ConfigError(f"{where}: password_file does not exist: {self.password_file}")
        self.retention.validate(f"{where} retention")


@dataclass(frozen=True, slots=True)
class PruneConfig:
    """The ``forget --prune`` job."""

    enabled: bool = True
    #: A few minutes past the hour on purpose: see :data:`DEFAULT_PRUNE_SCHEDULE`.
    schedule: str = DEFAULT_PRUNE_SCHEDULE
    #: Fallback for repositories that do not carry their own ping URL.
    healthchecks_url: str = ""
    dry_run: bool = False
    #: ``unlimited`` skips repacking entirely, which avoids re-uploading pack data.
    max_unused: str = "unlimited"
    max_repack_size: str = ""
    #: Measure the reclaimed bytes with ``stats`` before and after instead of
    #: reading them out of prune's own output. Exact, and expensive: the trailing
    #: call re-opens the repository and re-reads every index, inside the exclusive
    #: lock a concurrent backup is waiting on.
    exact_reclaimed: bool = False


@dataclass(frozen=True, slots=True)
class CheckConfig:
    """The ``check`` job.

    The schedule starts a few minutes past the hour deliberately. A producer
    backing up on the hour and every quarter past has a lock-wait budget; a job
    that starts at ``:05`` and finishes inside five minutes never collides with
    the ``:15`` run at all.
    """

    enabled: bool = True
    schedule: str = DEFAULT_CHECK_SCHEDULE
    healthchecks_url: str = ""
    #: Empty means structure-only. ``n/t`` reads the n-th of t equal parts,
    #: ``n%`` a random sample of that size.
    read_data_subset: str = DEFAULT_READ_DATA_SUBSET
    #: Advance ``n`` in an ``n/t`` subset each run, wrapping at ``t``, so every
    #: part is eventually verified. A fixed sample re-reads the same data forever.
    rotate_subset: bool = True
    with_cache: bool = True


#: ``n/t`` -- the n-th of t equal parts of the pack data.
_SUBSET_SLICE_RE: Final = re.compile(r"^(\d+)\s*/\s*(\d+)$")
#: ``12%`` / ``0.5%`` -- a random sample of that share.
_SUBSET_PERCENT_RE: Final = re.compile(r"^\d+(?:\.\d+)?%$")
#: ``500M`` / ``2G`` -- a random sample of that size.
_SUBSET_SIZE_RE: Final = re.compile(r"^\d+(?:\.\d+)?[KMGT]$", re.IGNORECASE)


def parse_subset_slice(value: str) -> tuple[int, int] | None:
    """Return ``(n, t)`` for an ``n/t`` subset, or ``None`` for any other form."""
    match = _SUBSET_SLICE_RE.match(value.strip())
    if match is None:
        return None
    return int(match[1]), int(match[2])


def validate_read_data_subset(value: str) -> None:
    """Reject a subset restic would only complain about once it holds the lock."""
    value = value.strip()
    if not value:
        return  # structure-only, which is a legitimate choice
    if (parsed := parse_subset_slice(value)) is not None:
        index, parts = parsed
        if parts < 1:
            raise ConfigError(f"check.read_data_subset: t must be at least 1 in {value!r}")
        if not 1 <= index <= parts:
            raise ConfigError(
                f"check.read_data_subset: n must be between 1 and {parts} in {value!r}"
            )
        return
    if _SUBSET_PERCENT_RE.match(value):
        if not 0 < float(value.rstrip("%")) <= 100:
            raise ConfigError(f"check.read_data_subset: {value!r} is not between 0% and 100%")
        return
    if _SUBSET_SIZE_RE.match(value):
        return
    raise ConfigError(
        f"check.read_data_subset: {value!r} is not a subset restic understands. "
        "Use 'n/t' (the n-th of t equal parts), 'n%', a size like '500M', "
        "or leave it empty to check the structure only."
    )


@dataclass(frozen=True, slots=True)
class MqttConfig:
    """MQTT publishing.

    Left empty, the broker is discovered through the Supervisor services API, so
    a Home Assistant user configures nothing at all.
    """

    enabled: bool = True
    host: str = ""
    port: int = 1883
    username: str = ""
    password: str = ""
    discovery_prefix: str = "homeassistant"

    @property
    def explicit(self) -> bool:
        return bool(self.host)


@dataclass(frozen=True, slots=True)
class WebConfig:
    # Container-local bind; the ingress guard is what restricts callers.
    host: str = "0.0.0.0"
    port: int = 8099
    #: When true, only the Home Assistant ingress proxy may talk to the API.
    ingress_only: bool = True


@dataclass(frozen=True, slots=True)
class Settings:
    repositories: tuple[RepositoryConfig, ...]
    prune: PruneConfig = field(default_factory=PruneConfig)
    check: CheckConfig = field(default_factory=CheckConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    web: WebConfig = field(default_factory=WebConfig)
    #: How long restic waits for a lock held by a concurrent backup.
    retry_lock: str = "15m"
    log_level: str = "info"
    timezone: str = "UTC"
    history_limit: int = 50
    #: Used to expand a bare check UUID; point this at a self-hosted instance.
    healthchecks_base_url: str = "https://hc-ping.com"
    #: One of :data:`BODY_MODES`.
    healthchecks_body: str = "summary"
    #: Only applies to the ``log`` body mode.
    healthchecks_body_limit: int = 10_000
    #: Run each job in its own UTS namespace so restic's repository lock names the
    #: job rather than the container. Falls back silently where the kernel refuses.
    lock_hostname: bool = True
    data_dir: Path = DEFAULT_DATA_DIR
    restic_binary: str = "restic"
    #: Publish states through the Supervisor's Home Assistant API when MQTT is
    #: unavailable. Ignored outside Home Assistant.
    hass_push: bool = True
    supervisor_token: str = ""

    @property
    def under_supervisor(self) -> bool:
        return bool(self.supervisor_token)

    def job(self, name: JobName) -> PruneConfig | CheckConfig:
        return self.prune if name == "prune" else self.check

    def repository(self, slug: str) -> RepositoryConfig | None:
        return next((repo for repo in self.repositories if repo.slug == slug), None)

    def healthchecks_url(self, repository: RepositoryConfig, job: JobName) -> str:
        """Per-repository URL, falling back to the job-level one."""
        return repository.healthchecks_url(job) or self.job(job).healthchecks_url

    def tzinfo(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    def validate(self) -> None:
        if not self.repositories:
            raise ConfigError("no repository configured; set 'repository' or 'repositories'")
        slugs = [repo.slug for repo in self.repositories]
        duplicates = {slug for slug in slugs if slugs.count(slug) > 1}
        if duplicates:
            raise ConfigError(
                f"repository names must be unique, these collide: {', '.join(sorted(duplicates))}"
            )
        for repo in self.repositories:
            repo.validate()
        if self.log_level not in LOG_LEVELS:
            raise ConfigError(f"log_level must be one of {', '.join(LOG_LEVELS)}")
        if self.healthchecks_body not in BODY_MODES:
            raise ConfigError(f"healthchecks_body must be one of {', '.join(BODY_MODES)}")
        for name in JOB_NAMES:
            job = self.job(name)
            if job.enabled and not croniter.is_valid(job.schedule):
                raise ConfigError(
                    f"{name}.schedule is not a valid cron expression: {job.schedule!r}"
                )
        if not any(self.job(name).enabled for name in JOB_NAMES):
            raise ConfigError("no job is enabled; enable at least one of prune or check")
        if self.history_limit < 1:
            raise ConfigError("history_limit must be at least 1")
        validate_read_data_subset(self.check.read_data_subset)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"expected an integer, got {value!r}") from exc


def _as_str(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def _section(options: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = options.get(key) or {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"option {key!r} must be a mapping")
    return value


def _environment(raw: Any) -> dict[str, str]:
    """Parse an ``environment`` option.

    Accepts the masked add-on shape (a list of ``{name, value}`` mappings), a
    plain mapping, a list of ``KEY=value`` strings, or one multi-line string.
    The Supervisor's option schema cannot express a list of mappings nested
    inside another list, so per-repository entries use the string forms.
    """
    if not raw:
        return {}
    if isinstance(raw, Mapping):
        return {str(k): str(v) for k, v in raw.items()}
    items: Sequence[Any] = raw.splitlines() if isinstance(raw, str) else raw
    result: dict[str, str] = {}
    for item in items:
        if isinstance(item, Mapping):
            name = str(item.get("name", "")).strip()
            if not name:
                raise ConfigError("environment entries need a non-empty 'name'")
            result[name] = str(item.get("value", ""))
            continue
        text = str(item).strip()
        if not text or text.startswith("#"):
            continue
        if "=" not in text:
            raise ConfigError(f"environment entry {text!r} is not in KEY=value form")
        name, _, value = text.partition("=")
        result[name.strip()] = value
    return result


def _retention(raw: Mapping[str, Any], base: Retention) -> Retention:
    """Overlay ``keep_*`` values onto *base*, which supplies the defaults."""
    keep_tags = raw.get("keep_tags")
    return Retention(
        **{
            f"keep_{name}": _as_int(raw.get(f"keep_{name}"), getattr(base, f"keep_{name}"))
            for name in _KEEP_FIELDS
        },
        keep_within=_as_str(raw.get("keep_within"), base.keep_within),
        keep_tags=tuple(str(tag) for tag in keep_tags) if keep_tags else base.keep_tags,
        group_by=_as_str(raw.get("group_by"), base.group_by),
    )


def _repositories(options: Mapping[str, Any]) -> tuple[RepositoryConfig, ...]:
    """Normalise both configuration shapes into a list of repositories."""
    defaults = _retention(_section(options, "retention"), Retention())
    shared_env = _environment(options.get("environment"))
    raw_list = options.get("repositories")

    if not raw_list:
        # Single-repository shorthand: everything lives at the top level.
        if not options.get("repository"):
            return ()
        prune = _section(options, "prune")
        check = _section(options, "check")
        return (
            RepositoryConfig(
                name=_as_str(options.get("name"), DEFAULT_REPOSITORY_NAME),
                repository=_as_str(options.get("repository")),
                password=_as_str(options.get("password")),
                password_file=_as_str(options.get("password_file")),
                environment=shared_env,
                retention=defaults,
                prune_healthchecks_url=_as_str(prune.get("healthchecks_url")),
                check_healthchecks_url=_as_str(check.get("healthchecks_url")),
            ),
        )

    if isinstance(raw_list, str) or not isinstance(raw_list, Sequence):
        raise ConfigError("'repositories' must be a list")

    repositories: list[RepositoryConfig] = []
    for index, raw in enumerate(raw_list):
        if not isinstance(raw, Mapping):
            raise ConfigError(f"repositories[{index}] must be a mapping")
        repositories.append(
            RepositoryConfig(
                name=_as_str(raw.get("name"), f"repository{index + 1}"),
                repository=_as_str(raw.get("repository")),
                password=_as_str(raw.get("password")),
                password_file=_as_str(raw.get("password_file")),
                # Shared credentials first, this repository's own entries win.
                environment={**shared_env, **_environment(raw.get("environment"))},
                retention=_retention(raw, defaults),
                prune_healthchecks_url=_as_str(raw.get("prune_healthchecks_url")),
                check_healthchecks_url=_as_str(raw.get("check_healthchecks_url")),
            )
        )
    return tuple(repositories)


def settings_from_options(
    options: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Build settings from Supervisor add-on options."""
    environ = os.environ if environ is None else environ
    prune_raw = _section(options, "prune")
    check_raw = _section(options, "check")
    mqtt_raw = _section(options, "mqtt")
    web_raw = _section(options, "web")

    supervisor_token = _as_str(environ.get("SUPERVISOR_TOKEN"))
    return Settings(
        repositories=_repositories(options),
        prune=PruneConfig(
            enabled=_as_bool(prune_raw.get("enabled"), True),
            schedule=_as_str(prune_raw.get("schedule"), DEFAULT_PRUNE_SCHEDULE),
            healthchecks_url=_as_str(prune_raw.get("healthchecks_url")),
            dry_run=_as_bool(prune_raw.get("dry_run"), False),
            max_unused=_as_str(prune_raw.get("max_unused"), "unlimited"),
            max_repack_size=_as_str(prune_raw.get("max_repack_size")),
            exact_reclaimed=_as_bool(prune_raw.get("exact_reclaimed"), False),
        ),
        check=CheckConfig(
            enabled=_as_bool(check_raw.get("enabled"), True),
            schedule=_as_str(check_raw.get("schedule"), DEFAULT_CHECK_SCHEDULE),
            healthchecks_url=_as_str(check_raw.get("healthchecks_url")),
            read_data_subset=_as_str(
                check_raw.get("read_data_subset"), DEFAULT_READ_DATA_SUBSET
            ).strip(),
            rotate_subset=_as_bool(check_raw.get("rotate_subset"), True),
            with_cache=_as_bool(check_raw.get("with_cache"), True),
        ),
        mqtt=MqttConfig(
            enabled=_as_bool(mqtt_raw.get("enabled"), True),
            host=_as_str(mqtt_raw.get("host")),
            port=_as_int(mqtt_raw.get("port"), 1883),
            username=_as_str(mqtt_raw.get("username")),
            password=_as_str(mqtt_raw.get("password")),
            discovery_prefix=_as_str(mqtt_raw.get("discovery_prefix"), "homeassistant"),
        ),
        web=WebConfig(
            host=_as_str(web_raw.get("host"), "0.0.0.0"),
            port=_as_int(web_raw.get("port"), 8099),
            # Outside Home Assistant there is no ingress proxy to require.
            ingress_only=_as_bool(web_raw.get("ingress_only"), bool(supervisor_token)),
        ),
        retry_lock=_as_str(options.get("retry_lock"), "15m"),
        healthchecks_base_url=_as_str(options.get("healthchecks_base_url"), "https://hc-ping.com"),
        healthchecks_body=_as_str(options.get("healthchecks_body"), "summary").lower(),
        log_level=_as_str(options.get("log_level"), "info").lower(),
        timezone=_as_str(environ.get("TZ"), "UTC"),
        history_limit=_as_int(options.get("history_limit"), 50),
        lock_hostname=_as_bool(options.get("lock_hostname"), True),
        hass_push=_as_bool(options.get("hass_push"), True),
        supervisor_token=supervisor_token,
    )


def _env_options(environ: Mapping[str, str]) -> dict[str, Any]:
    """Translate ``RESTIC_PRUNER_*`` variables into the options mapping shape."""

    def get(name: str, *fallbacks: str) -> str | None:
        for key in (ENV_PREFIX + name, *fallbacks):
            if environ.get(key):
                return environ[key]
        return None

    def section(prefix: str, keys: tuple[str, ...]) -> dict[str, Any]:
        raw = {key: get(f"{prefix}_{key.upper()}") for key in keys}
        return {key: value for key, value in raw.items() if value is not None}

    options: dict[str, Any] = {
        "repository": get("REPOSITORY", "RESTIC_REPOSITORY"),
        "name": get("REPOSITORY_NAME"),
        "password": get("PASSWORD", "RESTIC_PASSWORD"),
        "password_file": get("PASSWORD_FILE", "RESTIC_PASSWORD_FILE"),
        "environment": get("ENVIRONMENT"),
        "retry_lock": get("RETRY_LOCK"),
        "healthchecks_base_url": get("HEALTHCHECKS_BASE_URL"),
        "healthchecks_body": get("HEALTHCHECKS_BODY"),
        "lock_hostname": get("LOCK_HOSTNAME"),
        "log_level": get("LOG_LEVEL"),
        "history_limit": get("HISTORY_LIMIT"),
        "retention": section("KEEP", (*_KEEP_FIELDS, "within")),
        "prune": section(
            "PRUNE",
            (
                "enabled",
                "schedule",
                "healthchecks_url",
                "dry_run",
                "max_unused",
                "max_repack_size",
                "exact_reclaimed",
            ),
        ),
        "check": section(
            "CHECK",
            (
                "enabled",
                "schedule",
                "healthchecks_url",
                "read_data_subset",
                "rotate_subset",
                "with_cache",
            ),
        ),
        "mqtt": section("MQTT", ("enabled", "host", "port", "username", "password")),
        "web": section("WEB", ("host", "port", "ingress_only")),
    }
    options["retention"] = {f"keep_{key}": value for key, value in options["retention"].items()}
    if group_by := get("GROUP_BY"):
        options["retention"]["group_by"] = group_by

    # Several repositories are impractical as flat variables; take JSON instead.
    if raw := get("REPOSITORIES"):
        try:
            options["repositories"] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{ENV_PREFIX}REPOSITORIES is not valid JSON: {exc}") from exc

    return {key: value for key, value in options.items() if value not in (None, {})}


def load_settings(
    options_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load settings from the Supervisor options file, falling back to env vars."""
    environ = os.environ if environ is None else environ
    path = SUPERVISOR_OPTIONS_PATH if options_path is None else options_path
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"could not read add-on options from {path}: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise ConfigError(f"{path} does not contain a JSON object")
        options: Mapping[str, Any] = raw
    else:
        options = _env_options(environ)

    settings = settings_from_options(options, environ)
    if data_dir := environ.get(ENV_PREFIX + "DATA_DIR"):
        settings = dataclasses.replace(settings, data_dir=Path(data_dir))
    settings.validate()
    return settings
