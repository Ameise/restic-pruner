# Restic Pruner

A Home Assistant add-on that runs `restic forget --prune` and `restic check` on a
schedule, across one or many repositories, reports every run to
[healthchecks.io](https://healthchecks.io), and exposes the results as Home Assistant
entities.

It also runs as a plain Docker container if you don't use Home Assistant.

Both jobs are infrequent and long-running, and `prune` holds an exclusive lock on the
repository while it works, so they are normally scheduled separately from the backups
themselves.

- **Two scheduled jobs.** `prune` (`forget --prune`) and `check` (integrity
  verification), each on its own cron schedule.
- **Many repositories, one schedule.** They are maintained one after another, never in
  parallel. A failure on one does not stop the rest.
- **healthchecks.io reporting.** `/start` when a run begins, success or the restic exit
  code when it ends, with the tail of the log as the request body.
- **Home Assistant entities.** Every repository becomes its own device with last run,
  status, duration, snapshots removed, space reclaimed and repository size, plus buttons
  to run it now.
- **A web UI** in the Home Assistant sidebar with a live log and dry-run buttons.
- **An HTTP API** for everything the UI can do.

## Install (Home Assistant)

1. **Settings → Add-ons → Add-on store → ⋮ → Repositories**, add:

   ```
   https://github.com/Ameise/restic-pruner
   ```

2. Install **Restic Pruner**, open **Configuration**, and set at minimum:

   ```yaml
   repository: "b2:my-bucket:backups"
   password: "your-repository-password"
   environment:
     - name: B2_ACCOUNT_ID
       value: "..."
     - name: B2_ACCOUNT_KEY
       value: "..."
   prune:
     healthchecks_url: "https://hc-ping.com/<your-check-uuid>"
   ```

3. **Start it with `prune.dry_run: true` first.** Open the web UI, press **Dry run**,
   and read the log. When it says what you expect, set `dry_run: false`.

Full option reference: [`restic_pruner/DOCS.md`](restic_pruner/DOCS.md).

## Several repositories

Use the `repositories` list instead of the single-repository fields. They share one
schedule and are maintained in the order listed:

```yaml
repositories:
  - name: vps
    repository: "b2:my-bucket:backups"
    password: "..."
    environment: |
      B2_ACCOUNT_ID=...
      B2_ACCOUNT_KEY=...
    prune_healthchecks_url: "https://hc-ping.com/<uuid-for-vps>"
  - name: nas
    repository: "/share/backups/restic"
    password: "..."
    keep_daily: 30          # overrides the shared policy for this one only
    prune_healthchecks_url: "https://hc-ping.com/<uuid-for-nas>"

retention:                  # shared defaults
  keep_hourly: 48
  keep_daily: 14
  keep_weekly: 8
  keep_monthly: 6
```

## Interaction with backups

`prune` holds an exclusive lock on the repository for as long as it runs. Any restic
operation that starts during that window fails with exit code 11 unless it is willing to
wait, so backups running against the same repository need `--retry-lock`:

```bash
restic backup ... --retry-lock 1h
```

This add-on applies a retention policy. If a policy is also applied elsewhere against the
same repository, the two will disagree and each will remove snapshots the other keeps —
so `forget` should run in one place only.

## Standalone Docker

Same image, no Home Assistant. Configuration comes from `RESTIC_PRUNER_*` environment
variables, and the usual restic variables (`RESTIC_REPOSITORY`, `RESTIC_PASSWORD`,
`B2_ACCOUNT_ID`, `AWS_ACCESS_KEY_ID`, …) are passed through to restic as-is.

```yaml
services:
  restic-pruner:
    build: ./restic_pruner
    restart: unless-stopped
    ports:
      - "8099:8099"
    volumes:
      - ./data:/data
    environment:
      TZ: UTC
      RESTIC_PRUNER_REPOSITORY: b2:my-bucket:backups
      RESTIC_PRUNER_PASSWORD: your-repository-password
      RESTIC_PRUNER_KEEP_HOURLY: "48"
      RESTIC_PRUNER_KEEP_DAILY: "14"
      RESTIC_PRUNER_KEEP_WEEKLY: "8"
      RESTIC_PRUNER_KEEP_MONTHLY: "6"
      RESTIC_PRUNER_PRUNE_SCHEDULE: "0 3 * * 0"
      RESTIC_PRUNER_PRUNE_HEALTHCHECKS_URL: https://hc-ping.com/<uuid>
      RESTIC_PRUNER_CHECK_SCHEDULE: "0 5 * * 3"
      RESTIC_PRUNER_CHECK_HEALTHCHECKS_URL: https://hc-ping.com/<uuid>
      B2_ACCOUNT_ID: "..."
      B2_ACCOUNT_KEY: "..."
```

For several repositories outside Home Assistant, pass them as JSON in
`RESTIC_PRUNER_REPOSITORIES`, or mount an options file and start with
`--options /data/options.json`.

Outside Home Assistant the ingress guard is off, so the UI is reachable on port 8099.
Do not expose that port to the internet — it can trigger destructive operations.

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Liveness (also the add-on watchdog probe) |
| `GET` | `/api/status` | Full status document: jobs, schedules, repositories, last runs |
| `GET` | `/api/runs?job=&repository=&limit=` | Run history |
| `GET` | `/api/runs/{id}` | One run |
| `GET` | `/api/runs/{id}/log` | Full log of one run |
| `GET` | `/api/live?offset=` | Incremental log of the run in progress |
| `POST` | `/api/jobs/{prune\|check}/run` | Start a run, body `{"dry_run": false, "repository": "vps"}` |
| `POST` | `/api/unlock` | Remove stale repository locks |

Omit `repository` to act on every configured repository. Inside Home Assistant
everything except `/api/health` is reachable only through the ingress proxy.

## Entities

With an MQTT broker (the Mosquitto add-on is enough — no configuration needed, the
broker is discovered through the Supervisor) you get a **Restic Pruner** hub device
carrying the schedule and the run-everything buttons, plus one device per repository:

Per repository, where `<repo>` is its configured name:

| Entity | |
| --- | --- |
| `sensor.restic_pruner_<repo>_prune_status` | `success` / `failed` / `running` / `skipped` / `never` |
| `sensor.restic_pruner_<repo>_prune_last_run` | timestamp |
| `sensor.restic_pruner_<repo>_prune_last_success` | timestamp |
| `sensor.restic_pruner_<repo>_prune_duration` | seconds |
| `sensor.restic_pruner_<repo>_snapshots_removed` | last run |
| `sensor.restic_pruner_<repo>_bytes_reclaimed` | last run |
| `sensor.restic_pruner_<repo>_check_status` | as above |
| `sensor.restic_pruner_<repo>_check_last_run` | timestamp |
| `sensor.restic_pruner_<repo>_check_last_success` | timestamp |
| `sensor.restic_pruner_<repo>_repository_size` | bytes |
| `sensor.restic_pruner_<repo>_snapshot_count` | |
| `button.restic_pruner_<repo>_run_prune` | that repository only |
| `button.restic_pruner_<repo>_run_check` | that repository only |

On the hub device:

| Entity | |
| --- | --- |
| `sensor.restic_pruner_prune_next_run` | timestamp |
| `sensor.restic_pruner_check_next_run` | timestamp |
| `binary_sensor.restic_pruner_running` | |
| `button.restic_pruner_run_prune` | every repository |
| `button.restic_pruner_run_prune_dry` | every repository |
| `button.restic_pruner_run_check` | every repository |

Without a broker the same states are pushed through the Home Assistant API instead.
Those entities have no device and no buttons, and they disappear after a Core restart
until the next run.

Example automation:

```yaml
automation:
  - alias: Alert when a prune fails
    triggers:
      - trigger: state
        entity_id: sensor.restic_pruner_vps_prune_status
        to: failed
    actions:
      - action: notify.mobile_app
        data:
          message: "Restic prune failed — check the add-on log."
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest          # unit tests, plus end-to-end tests if restic is installed
.venv/bin/ruff check . && .venv/bin/mypy
```

Run it against a scratch repository:

```bash
RESTIC_PRUNER_REPOSITORY=/tmp/repo RESTIC_PRUNER_PASSWORD=test \
RESTIC_PRUNER_DATA_DIR=/tmp/rp-data .venv/bin/python -m restic_pruner
```

Build the add-on image the way the Supervisor does:

```bash
docker build --build-arg BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.13-alpine3.22 --build-arg BUILD_ARCH=aarch64 -t restic-pruner restic_pruner/
```

The artwork is generated, not hand-committed: `python3 scripts/generate_icons.py`.

## Scope

This add-on maintains repositories. It does not take backups — `restic backup` stays
where your data is.

## License

Apache-2.0. See [LICENSE](LICENSE).
