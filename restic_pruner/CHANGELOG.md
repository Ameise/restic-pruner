# Changelog

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
