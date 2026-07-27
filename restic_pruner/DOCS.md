# Restic Pruner

Runs `restic forget --prune` and `restic check` on a schedule, across one or many
repositories, reports every run to healthchecks.io, and exposes the results to Home
Assistant.

## Before you start

Two things follow from how restic works, and both concern whatever else uses the same
repository:

1. **Concurrent operations need `--retry-lock`.** `prune` holds an exclusive lock for the
   duration of the run. Anything else touching the repository during that window fails
   with exit code 11 unless it waits:

   ```bash
   restic backup ... --retry-lock 1h
   ```

2. **`forget` should run in one place only.** This add-on applies a retention policy. If
   another schedule applies a different one to the same repository, each will remove
   snapshots the other intends to keep.

## Configuration

There are two shapes. Fill in `repository` and `password` for a single repository, or
fill in the `repositories` list for several. Do not do both.

### `repository`

The repository, exactly as you would pass it to `restic -r`:

| Backend | Example |
| --- | --- |
| Backblaze B2 | `b2:my-bucket:backups` |
| S3 | `s3:s3.eu-central-1.amazonaws.com/my-bucket` |
| SFTP | `sftp:user@host:/path/to/repo` |
| REST server | `rest:https://user:pass@host:8000/` |
| Local / network share | `/share/backups/restic` |

### `password` / `password_file`

Set one of them. For `password_file`, put the file in `/addon_configs/<slug>/` on the
host — the add-on sees that directory as `/config`, so a file called `repo.key` is
referenced here as `/config/repo.key`.

### `environment`

Backend credentials, passed to restic as environment variables. Values are stored as
add-on secrets and masked in the UI.

```yaml
environment:
  - name: B2_ACCOUNT_ID
    value: "0012ab..."
  - name: B2_ACCOUNT_KEY
    value: "K001..."
```

For S3 use `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.

SFTP repositories authenticate with an SSH key rather than an environment variable, so the
key and any `ssh` configuration have to be reachable inside the container.

### `repositories`

Maintain several repositories from one add-on. They are pruned and checked one after
another — never in parallel — in the order listed, on the shared schedule below. Each
gets its own run history, its own entities and its own healthchecks.io check.

```yaml
repositories:
  - name: vps
    repository: "b2:my-bucket:backups"
    password: "..."
    environment: |
      B2_ACCOUNT_ID=0012ab...
      B2_ACCOUNT_KEY=K001...
    prune_healthchecks_url: "https://hc-ping.com/<uuid>"
    check_healthchecks_url: "https://hc-ping.com/<uuid>"
  - name: nas
    repository: "/share/backups/restic"
    password_file: "/config/nas.key"
    keep_daily: 30
```

- **`name`** labels the repository in the UI, the log and the entity ids
  (`sensor.restic_pruner_vps_prune_status`). Pick it once and keep it: renaming a
  repository renames its entities.
- **`environment`** is one `KEY=value` per line rather than the masked name/value list
  used at the top level, because the Supervisor's option schema cannot nest a list of
  mappings inside a list. Top-level `environment` entries still apply to every
  repository; entries here override them for this one.
- Any **`keep_*`** value set on a repository overrides the shared `retention` policy for
  that repository alone. Omit them to inherit.
- **`prune_healthchecks_url`** / **`check_healthchecks_url`** override the job-level
  URLs. One check per repository is the recommended setup: the alert then names the
  repository that failed.

A failure on one repository does not stop the others. The run is recorded as failed for
that repository only, and the batch continues.

### `retention`

Which snapshots survive `forget`. The defaults keep two days of hourly snapshots, two
weeks of daily, two months of weekly and six months of monthly:

```yaml
retention:
  keep_hourly: 48
  keep_daily: 14
  keep_weekly: 8
  keep_monthly: 6
```

Also available: `keep_last`, `keep_yearly`, `keep_within` (e.g. `7d`), and `group_by`
(default `host,paths`, restic's own default).

**At least one `keep_*` value must be non-zero.** An empty policy would tell restic to
forget everything, so the add-on refuses to start instead.

With several repositories these are the shared defaults; a repository can override any
of them.

### `prune`

```yaml
prune:
  enabled: true
  schedule: "0 3 * * 0"          # 03:00 every Sunday, in your Home Assistant timezone
  healthchecks_url: "https://hc-ping.com/<uuid>"
  dry_run: false
  max_unused: unlimited
  max_repack_size: ""
```

`max_unused: unlimited` is the default. It tells prune to delete only pack
files that are *entirely* unused, and never to repack partially-used ones. Repacking
means downloading and re-uploading pack data, which is the slowest and most expensive
part of a prune on a remote backend. The trade-off is that some dead data remains inside
partially-used packs. Set a percentage (`5%`) to reclaim it, and add `max_repack_size` (e.g. `2G`) to
bound how much data a single run moves.

### `check`

```yaml
check:
  enabled: true
  schedule: "0 5 * * 3"          # 05:00 every Wednesday
  healthchecks_url: "https://hc-ping.com/<uuid>"
  read_data_subset: "5%"
  with_cache: true
```

`restic check` on its own verifies structure: indexes, trees, and that every referenced
pack exists. `read_data_subset` additionally re-reads and re-hashes that share of actual
pack data, which is what catches silent corruption at the storage layer. The subset is
chosen at random each run, so repeated runs re-read some packs and leave others
untouched; a higher percentage verifies more per run at proportionally more cost.

Unlike `prune`, `check` takes a non-exclusive lock, so backups can run alongside it.

### Other options

| Option | Default | |
| --- | --- | --- |
| `name` | `main` | Label for the single repository, used in its entity ids |
| `retry_lock` | `15m` | How long a job waits for a lock held by another process |
| `healthchecks_base_url` | `https://hc-ping.com` | Only used when you enter bare UUIDs |
| `history_limit` | `50` | Runs and run logs kept |
| `mqtt` | auto | Leave empty; the broker is discovered via the Supervisor |
| `log_level` | `info` | `trace`, `debug`, `info`, `warning`, `error` |

## Scheduling

Schedules are standard five-field cron expressions evaluated in the timezone Home
Assistant gives the add-on, including across daylight saving changes.

Runs missed while the add-on was stopped are **not** caught up. A prune that was due
during a reboot waits for the next slot.

Nothing ever runs concurrently: not the two jobs, and not two repositories. A schedule
fires once and works through every repository in turn. If something is still running
when the next job comes due, that job is recorded as `skipped` rather than queued.

## What a prune runs

One command per repository:

```
restic forget --prune --keep-hourly 48 ... --max-unused unlimited
```

`forget --prune` performs both in one invocation. It takes the repository lock once, and
it skips the prune phase entirely when the retention policy removed no snapshots —
so a weekly prune that has nothing to do costs almost nothing. Immediately before and
after, `restic stats --mode raw-data` measures the repository, and the difference is the
"space reclaimed" you see in the UI and the entities.

## healthchecks.io

Every repository pings independently for every job, so with two repositories you want
**four checks**. Set them per repository (`prune_healthchecks_url`,
`check_healthchecks_url`); the job-level `healthchecks_url` is only a fallback for
repositories that do not have their own.

Each ping pair is:

- `POST <url>/start` when the run begins
- `POST <url>` on success, or `POST <url>/<restic exit code>` on failure

Both carry a `rid` so healthchecks pairs them into one execution, and the failure ping
carries the tail of the restic log as the body, so the notification distinguishes a lock
from a wrong password or genuine corruption.

Suggested check settings: period **1 week**, grace **6 hours** for prune. A weekly prune
that takes hours is normal.

You can enter a bare check UUID instead of the full URL; set `healthchecks_base_url` if
you self-host.

## The web UI

The add-on adds a sidebar panel showing the schedule, one card per repository with its
size, snapshot count and last prune and check, a live log while a job runs, and the last
25 runs. Click any run in the history to read its full log.

**Prune all now** / **Check all now** work through every repository; the buttons on a
repository card act on that one alone. **Dry run** passes `--dry-run`: it reports
exactly what would be removed and changes nothing. Use it the first time.

**Remove stale locks** runs `restic unlock`. Nothing scheduled ever does this
automatically — a lock is usually another restic process doing its job, and removing it
while a backup is running risks repository damage. Only use it after a crash left a lock
behind.

## Entities

With an MQTT broker available (the Mosquitto add-on suffices; no configuration needed)
the add-on creates a **Restic Pruner** hub device holding the next-run sensors, the
`running` binary sensor and the run-everything buttons, plus one child device per
repository — `Restic Pruner (vps)` — with that repository's status, timestamps,
snapshots removed, space reclaimed, size, snapshot count and its own two buttons.

Entity ids carry the repository name: `sensor.restic_pruner_vps_prune_status`. Renaming
a repository in the configuration therefore renames its entities, so choose names before
you build automations on them.

Without a broker, the same states are pushed through the Home Assistant API. Those
entities have no device and no buttons, and they vanish on a Core restart until the next
run repopulates them.

## Troubleshooting

**"the repository is already locked" (exit code 11)** — another restic process held the
lock for longer than `retry_lock`. Raise `retry_lock`, or move the schedule.

**"the repository password is incorrect" (exit code 12)** — check `password`, or whether
`password_file` points at a file with a trailing newline your repository does not expect.

**"the repository does not exist" (exit code 10)** — restic could not open the repository.
Check the `repository` value and the backend credentials in `environment`.

**Prune takes hours** — expected on a large repository, especially the first run. Keep
`max_unused: unlimited`, and consider `max_repack_size: 2G` to bound each run.

**No entities appeared** — the add-on log says which path it took on startup:
"Publishing entities over MQTT" or "No MQTT broker found".
