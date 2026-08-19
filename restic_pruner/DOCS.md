# Restic Pruner

Runs `restic forget --prune` and `restic check` on a schedule, across one or many
repositories, reports every run to healthchecks.io, and exposes the results to Home
Assistant.

## Before you start

Two things follow from how restic works, and both concern whatever else uses the same
repository:

1. **Concurrent operations need `--retry-lock`.** Both jobs hold an exclusive lock for
   the duration of the run. Anything else touching the repository during that window
   fails with exit code 11 unless it waits:

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

Backend credentials, passed to restic as environment variables. The `value` fields use the
`password` schema type, so the Configuration UI masks them while you type. They are stored
in the add-on's own options like any other setting — masking is not encryption.

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
  schedule: "5 3 * * 0"          # 03:05 every Sunday, in your Home Assistant timezone
  healthchecks_url: "https://hc-ping.com/<uuid>"
  dry_run: false
  max_unused: unlimited
  max_repack_size: ""
  exact_reclaimed: false
```

The default minute is `5`, not `0`, on purpose — see [Scheduling](#scheduling).

`max_unused: unlimited` is the default. It tells prune to delete only pack
files that are *entirely* unused, and never to repack partially-used ones. Repacking
means downloading and re-uploading pack data, which is the slowest and most expensive
part of a prune on a remote backend. The trade-off is that some dead data remains inside
partially-used packs. Set a percentage (`5%`) to reclaim it, and add `max_repack_size` (e.g. `2G`) to
bound how much data a single run moves.

`exact_reclaimed` measures the freed bytes with a `restic stats --mode raw-data` call
before and immediately after the prune. It is exact, and it is not free: each call
re-opens the repository and re-reads every index over the network, *inside the exclusive
lock*. On a small repository the trailing call alone can cost more than every deletion
phase put together. Left off, the figures come from prune's own `total prune:` and
`remaining:` lines instead, which are close enough for a dashboard, and the lock is
released that much sooner. The UI marks derived figures as approximate.

### `check`

```yaml
check:
  enabled: true
  schedule: "5 5 * * 3"          # 05:05 every Wednesday
  healthchecks_url: "https://hc-ping.com/<uuid>"
  read_data_subset: "1/4"
  rotate_subset: true
  with_cache: true
```

**Give `check` its own healthchecks URL.** It is the only thing that would notice
repository corruption, and a monitoring job nobody monitors is the weakest link in the
design. One check covering both jobs cannot express "prune is fine, check has been dead
for a month".

`restic check` on its own verifies *structure*: it downloads every snapshot, index and
tree object and confirms that every pack the index references exists. It does not
download pack contents. `read_data_subset` additionally re-reads and re-hashes some of
the actual pack data, which is what catches silent corruption at the storage layer.

Three forms are accepted:

| Value | Meaning |
| --- | --- |
| `""` | structure only, no pack data read |
| `"1/4"` | the n-th of four equal parts — **the default** |
| `"5%"`, `"500M"` | a random sample of that share or size |

With `rotate_subset: true` (the default) an `n/t` value advances every run and wraps, so
four weekly runs verify *all* of the pack data and the next four verify it again. A fixed
`5%` re-reads the same arbitrary 5% forever and leaves the other 95% unverified
indefinitely. A run that fails does not advance the counter, so the same part is retried.
The counter lives in the add-on's persistent data and survives restarts and updates.

Pick `t` against measured runtime, not against egress fear. The binding constraint is
the lock, not the bill: `check` takes an **exclusive** lock for its whole run, so anything
else backing up to the repository is blocked until it finishes. Size a slice so a run
stays well inside that producer's `--retry-lock` budget. `restic stats --mode raw-data`
gives the repository size to reason from; on a few-gigabyte repository `t=4` is a few
hundred megabytes per run.

Each run logs the exact command line it ran, so the scope that was actually verified is
visible in the job history rather than inferred.

### `repack`

```yaml
repack:
  enabled: false
  schedule: "17 4 1 * *"     # 04:17 on the 1st of the month
  healthchecks_url: "https://hc-ping.com/<uuid>"
  max_unused: "5%"
  max_repack_size: ""        # e.g. 2G, to bound one run
  dry_run: false
```

**What it is for.** restic bundles blobs into pack files of a few megabytes, and a
pack is the smallest thing it can delete. When `forget` drops snapshots their blobs
become garbage — but they sit in packs that also hold blobs still in use. `prune`
deletes the packs that are entirely dead and has to leave the rest alone. What stays
behind is *unused space*, and prune reports it:

```
remaining:          1810 blobs / 144.292 MiB
unused size after prune: 43.340 MiB (30.04% of remaining size)
```

The only way to reclaim it is to download those packs and write them out again
without the dead blobs. That is repacking, and it is why this is a separate job.

**It never touches snapshots.** This job runs `restic prune` on its own, with no
`forget`, so it cannot remove a snapshot and cannot disagree with your retention
policy. In that sense it is the safest of the three jobs.

**It is a superset of `prune`.** Repacking is not a distinct restic operation — it
is `prune` with a tighter `--max-unused`. A repack run therefore redoes everything
the prune job does (index load, pack scan, dead-pack deletion) and then also
rewrites partial packs. Give it a rarer schedule than prune rather than the same
one; there is nothing to gain from running both close together.

**`max_unused` is the point of the job.** It is a target, not a command to repack
everything: at `5%`, restic repacks only as much as it takes to get unused space
under 5% of the repository, then stops. `0` means repack everything, and is the most
expensive. `unlimited` is refused here — it tolerates any amount of dead space, so
the job would repack nothing and merely repeat what prune already did.

**Sizing it.** The cost is download traffic and lock time, not storage. Backblaze
B2, for instance, charges $0.01/GB egress but gives you 3× your stored data free
every month, so a repository of a few gigabytes can be repacked in full for nothing.
Lock time is the real constraint: this job holds the repository exclusively for
longer than prune does. On a large repository set `max_repack_size` (e.g. `2G`) so
each run is bounded and the repository converges over several months instead of one
very long lock.

**Does unused space grow forever?** Partly. Packs younger than your retention
horizon tend to die wholesale — their blobs were written together and expire
together — so their waste is reclaimed for free. Packs pinned by a single long-lived
blob never are. Expect a quick rise to a plateau and then a slow creep. Watch the
`Unused space` sensor for a few months before deciding whether you need this job at
all; on a small repository the honest answer is usually that you do not.

### Other options

| Option | Default | |
| --- | --- | --- |
| `name` | `main` | Label for the single repository, used in its entity ids |
| `retry_lock` | `15m` | How long a job waits for a lock held by another process |
| `healthchecks_base_url` | `https://hc-ping.com` | Only used when you enter bare UUIDs |
| `healthchecks_body` | `summary` | `summary`, `log` or `none` — see below |
| `lock_hostname` | `true` | Name the job in restic's repository lock — see below |
| `repack.dry_run` | `false` | Report what a repack would rewrite without doing it |
| `history_limit` | `50` | Runs and run logs kept |
| `mqtt` | auto | Leave empty; the broker is discovered via the Supervisor |
| `log_level` | `info` | `trace`, `debug`, `info`, `warning`, `error` |

## Scheduling

Both jobs are configured, never built in. `prune.schedule` and `check.schedule` are
standard five-field cron expressions evaluated in the timezone Home Assistant gives the
add-on, including across daylight saving changes, and any job can be switched off
entirely with `enabled: false`. Defaults: prune `5 3 * * 0` (Sundays 03:05), check
`5 5 * * 3` (Wednesdays 05:05), repack `17 4 1 * *` (04:17 on the 1st, off unless
you enable it).

**Why five past.** Every job takes an exclusive lock for its whole run. Anything else
backing up to the same repository is blocked meanwhile, and gives up once its own
`--retry-lock` window expires. A producer that backs up on the hour and every quarter
past has a fifteen-minute rhythm: a job starting at `:00` collides with that backup
immediately, while one starting at `:05` and finishing in under ten minutes never meets
it at all. Match the offset to whatever else uses your repository.

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
so a weekly prune that has nothing to do costs almost nothing. The "space reclaimed"
figure in the UI and the entities comes from prune's own `total prune:` and `remaining:`
output; set `exact_reclaimed: true` to measure it with `restic stats` instead.

### How long the lock is held

Most of a prune's wall time is not deletion. On a small repository over a remote backend
the deleting phases can account for a quarter of the run, with the rest going on opening
the repository and loading indexes and snapshots. Two things shorten the window:

- **Prune more often.** Cost scales with what has accumulated since the last run. A
  producer taking 96 snapshots a day leaves ~700 for a weekly prune to forget and ~96 for
  a daily one, and the daily runs hold the lock for less time *in total per week*. If a
  weekly prune's long tail is costing you a backup run, move `prune.schedule` to daily.
- **Leave `exact_reclaimed` off**, which is the default. It removes a full repository
  open and index load from inside the lock.

### The restic cache

restic caches index and snapshot metadata under `$RESTIC_CACHE_DIR`, which this add-on
points at `/data/cache` — the add-on's persistent volume. It therefore survives restarts,
add-on updates and Home Assistant reboots, and is excluded from Home Assistant backups.

Expect less from it than a backup client does. Prune *rebuilds* every index file, so the
following run's indexes are cache misses no matter what; snapshots created since the last
run are new objects too. What stays warm is the metadata of the snapshots that survived
retention.

## Sharing the repository with a backup client

Every job takes an **exclusive** lock for its whole run, so a client backing up to the
same repository is blocked until the job finishes and needs `--retry-lock` set to cover
it. `repack` holds it longest. Two things make that easy to reason about from the other side.

**The lock names the job.** restic writes whatever the operating system reports as the
hostname into its lock file, which in a container is the container — so by default
`prune` and `check` are indistinguishable from outside, and a lock seen on a Wednesday
tells you nothing about which job is holding it. With `lock_hostname: true` (the default)
each job runs in a throwaway namespace with its own hostname, and the lock reads:

```
repository is already locked exclusively by PID 142 on restic-pruner-check by root
```

This needs no added privileges — the add-on stays unprivileged — because the job first
enters an unprivileged *user* namespace and only then renames itself. A kernel that
forbids unprivileged user namespaces refuses this; the add-on says so once in its log at
startup and carries on with the container hostname.

**Both jobs ping healthchecks.io.** Give `prune` and `check` separate checks and the
timeline answers "which job held the lock at 03:00 on Wednesday" in seconds.

## healthchecks.io

Every repository pings independently for every job, so with two repositories and all
three jobs enabled you want **six checks**. Set them per repository
(`prune_healthchecks_url`, `check_healthchecks_url`, `repack_healthchecks_url`); the
job-level `healthchecks_url` is only a fallback for repositories that do not have
their own.

Each ping pair is:

- `POST <url>/start` when the run begins
- `POST <url>` on success, or `POST <url>/<restic exit code>` on failure

Both carry a `rid` so healthchecks pairs them into one execution.

### What goes in the ping body

`healthchecks_body` controls this, and defaults to `summary`:

```
prune on vps: success in 10.1s
snapshots: removed 1, kept 61
prune: 6 blobs removed, 380 repacked, 1 packs deleted
reclaimed 157.9 KiB (13.4 MiB -> 13.3 MiB)
```

A few hundred bytes, assembled from this add-on's own counters. On failure it ends with the
one error line the add-on formats, such as
`restic prune failed with exit code 11: the repository is already locked`, which is enough
to tell a lock from a wrong password.

restic's output is deliberately absent. `restic forget` prints a table of every snapshot it
kept and removed, including host names, tags and the absolute path of every directory in
the repository — easily 10 KB per run, sent to a third party, describing the layout of the
machine being backed up.

Set `healthchecks_body: log` to send the run log instead (capped, and it will contain those
paths), or `none` to send no body at all. The add-on stores the full log locally in every
case; the web UI is where to read it.

For a check run, the body names what was verified and — when something is wrong — the
objects that are broken:

```
check on vps: failed in 44.1s
verified: 4/13
2 packs missing, 1 tree damaged
pack 6dcad00d1e missing
pack 91b0f2c4aa missing
tree 4f77aa1c02 damaged

restic check failed with exit code 1
```

Only object ids are forwarded, never restic's error lines: those quote the file names
inside the damaged trees. `pack 6dcad00d1e missing` starts the actual work, where "check
failed" only starts an ssh session.

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
snapshots removed, space reclaimed, unused space, size, snapshot count and its own
buttons.

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
