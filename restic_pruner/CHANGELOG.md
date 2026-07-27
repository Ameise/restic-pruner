# Changelog

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
