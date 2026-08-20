# Changelog

## 0.4.0

### Trends

The add-on measures repository size, unused space and how long each job holds the
exclusive lock, and until now showed each of them only as a single latest number.
Unused space in particular exists to answer "is this climbing or converging",
which one number cannot do.

A **Trends** card between the repository cards and the log now charts all three
over the whole retained history. Run duration is drawn one line per job, in the
same colours the history table tags them with. The charts are hand-drawn SVG: the
panel remains a single self-contained document with no build step and no
dependencies.

### History that is worth keeping

Run records and their logs were the same knob, so keeping a year of overview rows
meant keeping a year of full logs. They are very different sizes -- a record is a
few hundred bytes, a log is up to 2000 lines -- so they are now separate:

- `history_limit` defaults to **0**, meaning keep every run record. Ten years of
  daily runs is a few megabytes.
- `log_limit` defaults to **25** and bounds the logs alone. An older run keeps its
  row and its counts, and clicking it says the log is no longer kept rather than
  showing an empty pane.

`history_limit` previously defaulted to 50 and was capped at 500; the cap is gone.

### Also

- Jobs are colour-tagged in the history table, which three job types had made hard
  to scan.
- The history table gained a **Size** column, so it reads as a record of the
  repository's trajectory rather than just of events.
- The read scope is spelled out: `5%` shows as *5% random sample*, `1/4` as *part 1
  of 4*, empty as *structure only*. A percentage samples afresh every run and may
  never reach some packs; `n/t` covers everything in t runs. Home Assistant keeps
  your existing options across updates, so anyone configured before 0.3.0 is still
  on the old fixed `5%` and had no way to tell from the UI.
- The history table scrolls inside its card on a phone instead of pushing the whole
  page sideways.
- New `GET /api/trends?repository=&limit=`, the history reduced to chart points.

## 0.3.3

0.3.2 stopped the panel *becoming* stale, but could not rescue a copy a browser
had already cached -- and reloading the Home Assistant page does not fix it,
because a normal reload of a page does not revalidate a document inside a frame.
The panel could therefore keep rendering a previous release indefinitely, through
reloads and restarts, with the request never reaching the add-on at all.

The page now knows which version it was built from and compares that against what
`/api/status` reports:

- On a mismatch it reloads itself once at a URL carrying the running version.
  A cache has never seen that URL, so it must fetch, and the page repairs itself.
- If it still disagrees afterwards, a banner says so and names both versions,
  rather than leaving a stale page looking perfectly healthy.

`scripts/check_versions.py` now covers the version baked into the page too, so
the four places that carry it cannot drift apart.

**Stuck on an older panel right now?** Right-click it and choose **Reload frame**.
Once 0.3.3 has loaded once, it looks after itself.

## 0.3.2

The web UI could keep showing the previous version's page after an add-on
update. It was served without a `Cache-Control` header, and a browser given only
`Last-Modified` may apply heuristic freshness and serve its cached copy without
revalidating -- so the panel showed the old UI while the new code ran underneath
it, with no sign anything was wrong.

- The page is now served `Cache-Control: no-cache`, so it revalidates every time.
  Static assets are versioned instead, so they stay cacheable.
- The header shows the running version, so what you are looking at is answerable
  from the page rather than from the add-on log.
- **The reported version was wrong.** `__version__` had said `0.1.0` since the
  first release, so the add-on log's startup line, `/api/health`, `/api/status`
  and the Home Assistant device `sw_version` all reported 0.1.0 through 0.2.0 and
  0.3.x. Anyone checking "which version is running" got a misleading answer.
  `scripts/check_versions.py` now fails CI whenever the three places that carry
  the version disagree.

If you are on 0.3.0 or 0.3.1 and the panel looks like an older release, a hard
reload (Ctrl/Cmd+Shift+R) fixes it once; this release stops it recurring.

## 0.3.1

The repack job shipped with only one of prune's two buttons, so the only way to
try a repack without committing to it was to set `repack.dry_run` on the
configuration page — a persistent setting standing in for a one-off action.

- **Repack (dry run)** buttons in the web UI and as a Home Assistant entity,
  alongside the existing prune ones.
- **Repack all now** in the web UI, which was missing entirely: repack could only
  be started per repository, not across all of them.
- The Schedule card shows repack's schedule and next run, which it did not.
- The per-repository dry-run button is now labelled **Prune (dry)** rather than
  **Dry run**, since there are two of them.

## 0.3.0

### A `repack` job, to reclaim the space prune cannot

restic can only delete a pack file as a whole, so `forget --prune` deletes the packs
whose blobs are all dead and has to leave the rest alone. What stays behind is dead
data inside packs that still hold live blobs -- on a repository backed up every 15
minutes that can reach 30% of its size, and nothing short of rewriting those packs
gets it back.

The new job runs `restic prune` on its own with a `max_unused` target. It never
touches snapshots, so it cannot conflict with the retention policy. It is a superset
of the prune job -- repacking is not a separate restic operation, it is prune with a
tighter target -- so give it a rarer schedule rather than the same one.

**Off by default**, because it holds the repository lock for longer than any other
job. Defaults to `17 4 1 * *` (04:17 on the 1st) and `max_unused: 5%` when enabled;
`unlimited` is refused, since it would repack nothing. `max_repack_size` bounds one
run so a large repository converges over several instead of one very long lock.

It has its own `repack_healthchecks_url`, per repository or job-wide, its own run
history, its own entities and its own button in the web UI. The existing
`prune_healthchecks_url` is untouched, and `prune` itself is unchanged.

### Unused space is now a number you can watch

restic prints `unused size after prune` on every prune, and until now it went only to
the run log. It is now parsed and reported as a metric, a line in the healthchecks.io
ping body, and an **Unused space** sensor per repository -- taken from whichever of
prune or repack ran most recently. It is the number that tells you whether the repack
job is worth enabling at all; on a small repository the answer is usually no.

## 0.2.0

### The repository lock now says which job is holding it

restic writes the operating system's hostname into its lock file, which in a container is
the container -- so `prune` and `check` were indistinguishable from the other side of a
shared repository, and a lock seen at an odd hour told you nothing about its cause. Each
job now runs under its own hostname and the lock reads `on restic-pruner-prune` or
`on restic-pruner-check`. This adds no container privileges: the job enters an
unprivileged user namespace first. Where a kernel forbids that, the add-on logs it once
at startup and carries on. Turn it off with `lock_hostname: false`.

### `check` reports what it found

- On success: the slice that was read, how many packs, and the snapshot count.
- On failure: the missing or damaged objects, by id, in the run history and in the
  healthchecks.io body -- `pack 6dcad00d1e missing` rather than "check failed". Only
  object ids are forwarded; restic's error lines quote the file names inside damaged
  trees and are never sent.
- Every run logs the exact restic command line, so the scope that was verified is visible
  in the job history rather than inferred.

### `check` rotates its read scope

`read_data_subset` now defaults to `1/4` -- the n-th of four equal parts -- and advances
each run, so four runs verify all of the pack data instead of re-reading the same
arbitrary sample forever. A failed run does not advance the counter. Set
`rotate_subset: false` to pin one slice; percentages and sizes still work as before.

### Prune holds the exclusive lock for less time

`restic stats --mode raw-data` is no longer run before and after each prune. Each call
re-opened the repository and re-read every index over the network, inside the lock a
concurrent backup was waiting on; the same figures are now taken from prune's own output.
Set `prune.exact_reclaimed: true` for the old, byte-exact behaviour.

### Also

- Default schedules moved to five minutes past the hour (`5 3 * * 0` and `5 5 * * 3`), so
  a run finishes before a producer's next quarter-hourly backup asks for the lock.
  Existing installations keep their configured schedules.
- The ping body is a short generated summary by default; restic's own output is not sent,
  since its snapshot listing contains host names, tags and the absolute paths of
  everything backed up. `healthchecks_body` can select the full log or no body at all.
- Documented that **both** jobs take an exclusive lock. The previous documentation said
  `check` did not, which was wrong.
- Replaced the deprecated `watchdog` option with a container `HEALTHCHECK`, and dropped
  four manifest keys that only repeated Supervisor defaults. This unblocks CI.

## 0.1.0

First release.

- Scheduled `restic forget --prune` job with a configurable retention policy.
- Scheduled `restic check` job with optional `--read-data-subset` data verification.
- One or many repositories per instance, maintained one after another on a shared
  schedule, each with its own retention overrides, run history, entities and
  healthchecks.io checks. A failure on one does not stop the others.
- healthchecks.io reporting: `/start` on begin, success or the restic exit code on end,
  with the log tail as the body and a `rid` correlating the pair.
- Home Assistant entities over MQTT discovery, with automatic broker detection through
  the Supervisor: a hub device plus one device per repository. Falls back to pushing
  states through the Home Assistant API when no broker exists.
- Ingress web UI: schedule, a card per repository, live log, run history, and manual or
  dry runs for one repository or all of them.
- HTTP API for status, history, logs, triggering runs and removing stale locks.
- Runs unchanged as a plain Docker container, configured through `RESTIC_PRUNER_*`
  environment variables.
- Ships restic 0.19.1, pinned and SHA256-verified at image build.
