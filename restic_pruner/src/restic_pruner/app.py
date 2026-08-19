"""Wiring: build every component, start them, shut them down cleanly."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime
from typing import Any, Final

import aiohttp
from aiohttp import web

from . import __version__
from .api import create_app
from .config import JOB_NAMES, JobName, Settings
from .healthchecks import HealthchecksClient
from .jobs import JobBusyError, JobDisabledError, JobRunner, UnknownRepositoryError
from .publish import HassStatePublisher, MqttPublisher
from .restic import ResticError, uts_namespace_available
from .scheduler import Scheduler
from .state import RepositorySnapshot, StateStore, Trigger
from .supervisor import SupervisorClient
from .views import redact_repository, status_view

_LOGGER: Final = logging.getLogger(__name__)

LOG_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

#: Home Assistant's own log levels, mapped onto Python's.
LOG_LEVEL_MAP: Final[dict[str, int]] = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

#: How often states are re-pushed when running without MQTT.
HASS_PUSH_INTERVAL: Final = 300.0


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=LOG_LEVEL_MAP.get(level, logging.INFO),
        format=LOG_FORMAT,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


class Application:
    """The running add-on."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._state = StateStore(settings.data_dir, settings.history_limit)
        self._session: aiohttp.ClientSession | None = None
        self._mqtt: MqttPublisher | None = None
        self._hass: HassStatePublisher | None = None
        self._runner: JobRunner | None = None
        self._scheduler: Scheduler | None = None
        self._stop = asyncio.Event()

    def status(self) -> dict[str, Any]:
        assert self._runner is not None
        return status_view(self._settings, self._state, self._runner, self._scheduler)

    async def run(self) -> int:
        settings = self._settings
        self._state.load()
        _LOGGER.info("Restic Pruner %s starting", __version__)
        _LOGGER.info(
            "Maintaining %d repositor%s in timezone %s",
            len(settings.repositories),
            "y" if len(settings.repositories) == 1 else "ies",
            settings.timezone,
        )
        for repository in settings.repositories:
            _LOGGER.info("  %s -> %s", repository.name, redact_repository(repository.repository))

        async with aiohttp.ClientSession() as session:
            self._session = session
            healthchecks = HealthchecksClient(
                session,
                base_url=settings.healthchecks_base_url,
                body_limit=settings.healthchecks_body_limit,
            )
            runner = JobRunner(settings, self._state, healthchecks, on_change=self._on_change)
            self._runner = runner
            scheduler = Scheduler(settings, runner)
            scheduler.prime()
            self._scheduler = scheduler

            await self._setup_publishers(session)
            site = await self._start_web(runner, scheduler)
            self._install_signal_handlers()

            tasks: list[asyncio.Task[Any]] = [
                asyncio.create_task(scheduler.run_forever(), name="scheduler"),
                asyncio.create_task(self._probe_repositories(runner), name="probe"),
            ]
            if self._mqtt is not None:
                tasks.append(asyncio.create_task(self._mqtt.run(), name="mqtt"))
            if self._mqtt is None and self._hass is not None:
                tasks.append(asyncio.create_task(self._hass_loop(), name="hass-push"))

            _LOGGER.info("Listening on %s:%s", settings.web.host, settings.web.port)
            await self._stop.wait()
            _LOGGER.info("Shutting down")

            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await site.cleanup()
        return 0

    # -- setup -----------------------------------------------------------

    async def _setup_publishers(self, session: aiohttp.ClientSession) -> None:
        settings = self._settings
        supervisor = SupervisorClient(session, settings.supervisor_token)
        service = None
        if settings.mqtt.enabled and not settings.mqtt.explicit:
            service = await supervisor.mqtt_service()
        if settings.mqtt.enabled and (settings.mqtt.explicit or service is not None):
            self._mqtt = MqttPublisher(settings.mqtt, service, self.status, self._handle_command)
            _LOGGER.info("Publishing entities over MQTT")
            return
        if settings.hass_push and supervisor.available:
            self._hass = HassStatePublisher(supervisor, self.status)
            _LOGGER.info(
                "No MQTT broker found; pushing states through the Home Assistant API. "
                "Install the Mosquitto add-on for proper device entities and buttons."
            )
            return
        _LOGGER.info("Not publishing to Home Assistant (no MQTT broker, no Supervisor)")

    async def _start_web(self, runner: JobRunner, scheduler: Scheduler) -> web.AppRunner:
        app = create_app(self._settings, self._state, runner, scheduler)
        app_runner = web.AppRunner(app, access_log=None)
        await app_runner.setup()
        site = web.TCPSite(app_runner, self._settings.web.host, self._settings.web.port)
        await site.start()
        return app_runner

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop.set)

    # -- callbacks -------------------------------------------------------

    async def _on_change(self) -> None:
        if self._mqtt is not None:
            self._mqtt.notify()
        elif self._hass is not None:
            await self._hass.publish()

    async def _handle_command(self, command: str, repository: str | None) -> None:
        assert self._runner is not None
        # Button payloads are "<job>" or "<job>_dry", optionally ":<repository>".
        name, _, suffix = command.partition("_")
        job: JobName = next((j for j in JOB_NAMES if j == name), "prune")
        dry_run = suffix == "dry"
        try:
            await self._runner.trigger(
                job, trigger=Trigger.MANUAL, dry_run=dry_run, repository=repository
            )
        except (JobBusyError, JobDisabledError, UnknownRepositoryError) as exc:
            _LOGGER.warning("Command %r rejected: %s", command, exc)

    async def _hass_loop(self) -> None:
        assert self._hass is not None
        while True:
            await self._hass.publish()
            await asyncio.sleep(HASS_PUSH_INTERVAL)

    async def _probe_repositories(self, runner: JobRunner) -> None:
        """Confirm each repository is reachable and seed its sensors.

        A repository that is unreachable at startup is logged and skipped; its
        scheduled jobs still run, because the backend may simply be asleep.
        """
        if self._settings.lock_hostname:
            # Probed here rather than mid-run, so the answer is in the log from
            # the start and not discovered while a job is holding the lock.
            if await uts_namespace_available():
                _LOGGER.info("Jobs will name themselves in the repository lock")
            else:
                _LOGGER.info(
                    "Jobs cannot name themselves in the repository lock on this host; "
                    "it will show the container hostname instead"
                )

        logged_version = False
        for repository in self._settings.repositories:
            restic = runner.restic_for(repository)
            try:
                if not logged_version:
                    version = await restic.version()
                    _LOGGER.info("Using %s", version.splitlines()[0] if version else "restic")
                    logged_version = True
                stats = await restic.stats()
            except ResticError as exc:
                _LOGGER.error(
                    "Repository %s is not reachable yet: %s. Scheduled jobs will still run.",
                    repository.name,
                    exc,
                )
                continue
            self._state.set_repository(
                repository.slug,
                RepositorySnapshot(
                    checked_at=datetime.now(UTC),
                    size_bytes=stats.total_size,
                    uncompressed_bytes=stats.total_uncompressed_size,
                    blob_count=stats.total_blob_count,
                    snapshot_count=stats.snapshots_count,
                ),
            )
            _LOGGER.info(
                "Repository %s holds %d snapshot(s), %d bytes of raw data",
                repository.name,
                stats.snapshots_count,
                stats.total_size,
            )
        self._state.save()
        await self._on_change()


async def async_main(settings: Settings) -> int:
    configure_logging(settings.log_level)
    for job in JOB_NAMES:
        config = settings.job(job)
        if config.enabled:
            _LOGGER.info("Job %s scheduled at %r", job, config.schedule)
        else:
            _LOGGER.info("Job %s is disabled", job)
    return await Application(settings).run()
