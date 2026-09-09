# Changelog

## 0.5.0

### The dashboard stopped shoving things aside

The copy buttons added in 0.4.4 were laid out as ordinary items in the header and in
each repository's card head, so they took their strip of the row whether or not
anyone wanted them -- invisible until hovered, but always occupying the space. That
is why the version, the timezone and every repository's status pill sat pushed left
of where they had always been.

They sit where they did again. The button is out of the flow now, and the text or
pill beside it **slides aside only while the pointer is in that corner**, uncovering
the button in the space it vacates.

### All three trends on one chart

The three trend charts are three readings of the same runs, so **a toggle in the
Trends heading lays them over one another**.

Bytes and seconds cannot share a scale, and the overlay does not pretend otherwise:
**sizes are read off the left axis and durations off the right**, with the dash
pattern saying which of the three readings a line is -- solid for size, dashed for
unused space, dotted for duration. Colour keeps the meaning it has in the split
view, one per repository for the sizes and one per job for the durations. The log
toggle is there too, and applies to both axes at once.

Which view you last chose is remembered, as the log scales already were.

### One run, read across every chart

Hovering a point used to read out the chart it was in and leave the others dark,
though the same run had a point in each of them. Now **hovering one point labels the
same run in the neighbouring charts too**, dimmer, so the chart being asked is still
the one being read. Where a run recorded no such number -- a check job has a
duration but no repository size -- that chart stays blank rather than showing a
neighbouring run instead.

### The run behind the point

A point on a chart is a number with no story: which job put it there, on whose
schedule, what it freed. **The trends card now renders that run's history row
underneath the charts**, built by the same code the history table uses, so the two
cannot come to disagree.

- **Hovering** a point shows its row.
- **Clicking** a point pins it, so the row stays put while the eye goes back to the
  lines. Clicking a row in the history table pins it too, and marks it on the
  charts. `Esc`, the `✕ unpin` button, or clicking the point again lets go.

### Also

- `/api/trends` now includes each point's **run id**. It is the one field that lets a
  point be traded back for the whole run; everything else still comes from
  `/api/runs/{id}`, and only when someone actually asks.

## 0.4.4

### The prune job no longer repacks behind your back

`--max-unused` was only passed to restic when `prune.max_unused` had a value. Left off,
restic does not skip the limit -- it applies **its own default of 5%**, and the prune job
starts repacking, which is the one thing it exists not to do. Nothing on the dashboard
said it was happening; the runs were simply slower and the repository churned.

- The flag is **always** sent now, so what the page says is what restic is told.
- An empty `prune.max_unused` is **refused at startup** rather than quietly meaning 5%.
  Write `unlimited` to leave repacking to the repack job, or a percentage such as `5%` to
  ask for it deliberately. A disabled prune job is not checked.

### Copy any part of the dashboard

Reading a number off the page is one thing; getting it into a notebook or a chat window
was retyping.

- A **copy button** on the header, the trends, the log, the history table and each
  repository card, hidden until that section is hovered or the button is focused. The
  header's copies the whole page.
- The text is **Markdown, not the raw API JSON**: it says the same thing in a fraction of
  the words, and every timestamp is absolute UTC -- a *7 days ago* means nothing once it
  has left the page that computed it. The charts come across as a summary line and a
  short tail per series, since several hundred raw points is the wrong thing to paste.
- Two ordinary installs cannot use the clipboard API at all -- plain HTTP on a LAN
  address is not a secure context, and Home Assistant frames the add-on without
  clipboard permission -- so a **fallback** handles both the missing API and the refusal.

### Weeks you can see in the history table

The three jobs run as a weekly cluster, and the table drew them as three unrelated rows.

- Rows are now **banded by ISO week**, so each cluster reads as one block. Weeks begin on
  Monday, so a Sunday cluster falls wholly inside one.
- Banding is **dropped under the duration and size sorts**, where the weeks interleave and
  the stripes would say nothing about the rows they were striping.

## 0.4.3

### A log scale, per chart

A linear axis flattens anything whose interesting movement is small next to its largest
value. Run duration is the clearest case: prune takes about a minute and repack over an
hour, so on one linear axis the prune line lies along the floor and "is prune getting
slower" cannot be answered.

Each chart now has its own **log** button. Per chart, because the size charts are usually
fine linear and the duration chart usually is not, and the choice is remembered across
reloads.

- Size axes climb in powers of two -- `128 MiB`, `512 MiB`, `1 GiB` -- and duration axes
  in the steps a clock has: `1m`, `5m`, `15m`, `1h`. Over a range too short to hold a
  rung, the round numbers of the linear axis are kept and merely placed logarithmically.
- A log axis has no room for zero, and unused space legitimately reaches it. Those runs
  are **left out rather than flattened onto the bottom tick**, the line breaks where they
  were, and the caption counts them: *16 at 0 B not shown*. A chart with nothing above
  zero says so instead of drawing an empty plot.

## 0.4.2

### A history table you can interrogate

The last 25 runs were a flat list with three job types interleaved, and the numeric
columns were inert text.

- A **Job** dropdown above the table narrows it to prune, check or repack. It refetches
  rather than hiding rows, so filtering gives you 25 runs *of that job* instead of
  however few of them the last 25 mixed runs happened to contain.
- **Started**, **Duration** and **Size** sort on click, in Wikipedia's three states:
  the order as served, then ascending, then descending, then back. One column at a time.
  Runs with nothing to compare -- an unfinished run has no duration, a check reports no
  size -- sit at the bottom whichever way the arrow points.

Sorting reorders the runs that are loaded: the table you see is the table you sort.

### Repository cards that read as cards

The pill beside the repository name was prune's status wearing the repository's hat --
which is why prune was the one job without a tag of its own, and why a green pill could
sit above a failed check.

- The name, the target and the pill now share a **frame**, and the pill is the worst
  status across all three jobs. A failed check turns the card's badge red no matter how
  well prune went.
- **Prune, check and repack are three panels**, each with its own tag -- prune included --
  and a left edge in that job's colour, the same one the history tags and the duration
  chart use. Stacked on a narrow screen they no longer read as more of the rows above
  them.

## 0.4.1

### Charts you can read numbers off

The trend charts drew the shape of things without ever saying how big they were: no
axis, only a `4.8 GiB – 6.3 GiB` caption underneath, and no way to tie a wobble in the
line to the run that caused it.

- Every chart now has a **labelled y axis** with faint gridlines, rounded to whole units
  -- `5 GiB`, `20m` -- and the first and last date under the plot.
- **Pointing at a curve** reads out the nearest run: its exact value, the job or
  repository it belongs to, and when it finished. It snaps to a single point rather than
  lining the series up on a shared moment, because the three jobs finish at different
  times and a shared column would compare runs that never happened together.
- Where several repositories are configured, each gets **its own colour** in the size
  charts. They were all drawn in the accent colour, which made the legend meaningless.

The charts are drawn at a fixed aspect now, so their height follows the card width. That
is what lets the axis labels be text inside the drawing without being stretched with it.

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
