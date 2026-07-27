"""HTTP API and ingress web UI.

============================  =====================================
``GET  /api/health``          liveness, used by the add-on watchdog
``GET  /api/status``          full status document
``GET  /api/runs``            run history
``GET  /api/runs/{id}``       one run
``GET  /api/runs/{id}/log``   full log of one run
``GET  /api/live``            incremental log of the running job
``POST /api/jobs/{job}/run``  start prune or check
``POST /api/unlock``          remove stale repository locks
============================  =====================================

``/api/jobs/{job}/run`` and ``/api/unlock`` accept an optional ``repository``
in the body to target one repository instead of every configured one; ``runs``
accepts it as a query parameter.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Final

from aiohttp import web
from aiohttp.typedefs import Handler

from . import __version__
from .config import JOB_NAMES, JobName, Settings
from .jobs import JobBusyError, JobDisabledError, JobRunner, UnknownRepositoryError
from .scheduler import Scheduler
from .state import StateStore, Trigger
from .views import run_view, status_view

_LOGGER: Final = logging.getLogger(__name__)

#: Home Assistant's ingress proxy always originates from this address.
INGRESS_SOURCE: Final = "172.30.32.2"

#: The Supervisor watchdog probes this from its own address, so it has to stay
#: reachable. It exposes nothing but liveness and the version string.
GUARD_EXEMPT: Final = frozenset({"/api/health"})

WEB_ROOT: Final = Path(__file__).parent / "web"

_SETTINGS: Final = web.AppKey[Settings]("settings")
_STATE: Final = web.AppKey[StateStore]("state")
_RUNNER: Final = web.AppKey[JobRunner]("runner")
_SCHEDULER: Final = web.AppKey[Scheduler]("scheduler")
_TASKS: Final = web.AppKey["set[asyncio.Task[None]]"]("tasks")


@web.middleware
async def ingress_guard(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Reject anything that did not come through the Home Assistant ingress."""
    settings = request.app[_SETTINGS]
    if request.path in GUARD_EXEMPT:
        return await handler(request)
    if settings.web.ingress_only and request.remote != INGRESS_SOURCE:
        _LOGGER.warning("Rejected request from %s", request.remote)
        raise web.HTTPForbidden(reason="Only reachable through Home Assistant ingress")
    return await handler(request)


def create_app(
    settings: Settings,
    state: StateStore,
    runner: JobRunner,
    scheduler: Scheduler,
) -> web.Application:
    app = web.Application(middlewares=[ingress_guard])
    app[_SETTINGS] = settings
    app[_STATE] = state
    app[_RUNNER] = runner
    app[_SCHEDULER] = scheduler
    app[_TASKS] = set()

    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/runs", handle_runs)
    app.router.add_get("/api/runs/{run_id}", handle_run)
    app.router.add_get("/api/runs/{run_id}/log", handle_run_log)
    app.router.add_get("/api/live", handle_live)
    app.router.add_post("/api/jobs/{job}/run", handle_run_job)
    app.router.add_post("/api/unlock", handle_unlock)
    app.router.add_get("/", handle_index)
    if WEB_ROOT.is_dir():
        app.router.add_static("/static", WEB_ROOT)
    app.on_cleanup.append(_cancel_tasks)
    return app


async def _cancel_tasks(app: web.Application) -> None:
    tasks: set[asyncio.Task[None]] = app[_TASKS]
    for task in tuple(tasks):
        task.cancel()


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "version": __version__})


async def handle_status(request: web.Request) -> web.Response:
    return web.json_response(
        status_view(
            request.app[_SETTINGS],
            request.app[_STATE],
            request.app[_RUNNER],
            request.app[_SCHEDULER],
        )
    )


async def handle_runs(request: web.Request) -> web.Response:
    raw_job = request.query.get("job")
    job = _parse_job(raw_job) if raw_job is not None else None
    repository = request.query.get("repository")
    if repository is not None:
        _require_repository(request, repository)
    limit = _int_param(request, "limit", default=25, minimum=1, maximum=200)
    runs = request.app[_STATE].runs(job, limit, repository=repository)
    return web.json_response({"runs": [run_view(run) for run in runs]})


async def handle_run(request: web.Request) -> web.Response:
    run = request.app[_STATE].get(request.match_info["run_id"])
    if run is None:
        raise web.HTTPNotFound(reason="no such run")
    return web.json_response(run_view(run))


async def handle_run_log(request: web.Request) -> web.Response:
    log = request.app[_RUNNER].log_for(request.match_info["run_id"])
    if log is None:
        raise web.HTTPNotFound(reason="no log for this run")
    return web.Response(text=log, content_type="text/plain")


async def handle_live(request: web.Request) -> web.Response:
    offset = _int_param(request, "offset", default=0, minimum=0, maximum=10**9)
    next_offset, lines = request.app[_RUNNER].live_log(offset)
    current = request.app[_RUNNER].current
    return web.json_response(
        {
            "offset": next_offset,
            "lines": lines,
            "running": current is not None,
            "run_id": current.id if current else None,
            "job": current.job if current else None,
        }
    )


async def handle_run_job(request: web.Request) -> web.Response:
    job_name = _parse_job(request.match_info["job"])
    settings = request.app[_SETTINGS]
    runner = request.app[_RUNNER]
    body = await _json_body(request)
    dry_run = body.get("dry_run")
    if dry_run is not None and not isinstance(dry_run, bool):
        raise web.HTTPBadRequest(reason="dry_run must be a boolean")

    repository = body.get("repository")
    if repository is not None:
        repository = str(repository)
        _require_repository(request, repository)

    if not settings.job(job_name).enabled:
        raise web.HTTPConflict(reason=f"the {job_name} job is disabled in the configuration")
    if runner.busy:
        raise web.HTTPConflict(reason="a restic job is already running")

    task = asyncio.create_task(
        _guarded(runner, job_name, dry_run, repository), name=f"manual-{job_name}"
    )
    request.app[_TASKS].add(task)
    task.add_done_callback(request.app[_TASKS].discard)
    return web.json_response(
        {"started": True, "job": job_name, "repository": repository}, status=202
    )


async def _guarded(
    runner: JobRunner, job: JobName, dry_run: bool | None, repository: str | None
) -> None:
    try:
        await runner.trigger(job, trigger=Trigger.MANUAL, dry_run=dry_run, repository=repository)
    except (JobBusyError, JobDisabledError, UnknownRepositoryError) as exc:
        _LOGGER.warning("Manual %s run rejected: %s", job, exc)


async def handle_unlock(request: web.Request) -> web.Response:
    body = await _json_body(request)
    remove_all = bool(body.get("remove_all", False))
    repository = body.get("repository")
    if repository is not None:
        repository = str(repository)
        _require_repository(request, repository)
    try:
        await request.app[_RUNNER].unlock(repository, remove_all=remove_all)
    except JobBusyError as exc:
        raise web.HTTPConflict(reason=str(exc)) from exc
    except Exception as exc:  # surface the restic error to the UI
        raise web.HTTPBadGateway(reason=str(exc)[:200]) from exc
    return web.json_response({"unlocked": True, "remove_all": remove_all, "repository": repository})


async def handle_index(request: web.Request) -> web.StreamResponse:
    index = WEB_ROOT / "index.html"
    if not index.is_file():
        raise web.HTTPNotFound(reason="web UI not installed")
    return web.FileResponse(index)


async def _json_body(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        payload = await request.json()
    except ValueError as exc:
        raise web.HTTPBadRequest(reason="body must be JSON") from exc
    return payload if isinstance(payload, dict) else {}


def _parse_job(value: str) -> JobName:
    if value not in JOB_NAMES:
        raise web.HTTPBadRequest(reason=f"unknown job {value!r}")
    return "prune" if value == "prune" else "check"


def _require_repository(request: web.Request, slug: str) -> None:
    if request.app[_SETTINGS].repository(slug) is None:
        raise web.HTTPNotFound(reason=f"no repository named {slug!r}")


def _int_param(request: web.Request, name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = request.query.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise web.HTTPBadRequest(reason=f"{name} must be an integer") from exc
    return max(minimum, min(maximum, value))
