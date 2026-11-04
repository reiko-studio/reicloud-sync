# reicloud-sync

A Linux-first personal cloud sync manager powered by rclone. Part of Reiko Studio.

Currently implements configuration setup, Dropbox login, named folder pairs,
manual two-way synchronization, background scheduling and explicit recovery.

## Development install

Requires Python 3.14+, rpy and rclone on your PATH.

```sh
rpy env create --python python3.14
rpy pip install -e .
```

## First Dropbox login

From this repository:

```sh
rpy run reicloud setup
rpy run reicloud login dropbox
rpy run reicloud check dropbox
```

Login opens rclone's browser OAuth flow. Approve access in Dropbox. The command
then checks access by listing the remote root without displaying folder names or
transferring files. Existing remotes are checked rather than overwritten.
`reicloud setup --login` combines setup and login.

## Folder pairs

Register an existing local directory and its Dropbox destination:

```sh
rpy run reicloud add notes ~/documents/notes dropbox:Notes
rpy run reicloud list
rpy run reicloud show notes --json
rpy run reicloud init notes --dry-run --prefer local
```

Adding a pair only saves configuration. It does not create folders or transfer
files. Local directories must already exist. Dropbox paths are normalized;
currently only the configured `dropbox:` remote is supported. Overlapping local
roots (including symlink aliases) and case-insensitive Dropbox roots are rejected.
Local roots must not overlap reicloud's configuration, state or cache directories.

The Dropbox directory must already exist for a preview; create a disposable test
directory in Dropbox first. The preview uses rclone bisync's initialization mode.
Choose `--prefer local` or `--prefer remote` explicitly: if a file differs on both
sides, that side would take precedence. Unique files would be copied both ways.
Preview output may include file names. No file contents are transferred, and its
temporary bisync history is discarded. It does not initialize a pair for real
syncing.

After reviewing the preview, initialize the pair and run subsequent syncs:

```sh
rpy run reicloud init notes --prefer local
rpy run reicloud sync notes --dry-run
rpy run reicloud sync notes
rpy run reicloud status notes
rpy run reicloud status notes --json
```

**Initialization and sync without `--dry-run` transfer real files.** Initialization
combines unique files and uses the explicit preference for differing versions.
Normal sync propagates changes and deletions in both directions, preserving
competing edits as conflict copies. It aborts if deletions exceed 50% on either
side. For larger intended removals, preview an explicit limit and then apply it:

```sh
rpy run reicloud sync notes --dry-run --max-delete 100
rpy run reicloud sync notes --max-delete 100
```

The limit is a percentage, applies only to that run, and must be 0–100. It does
not reset history or bypass access checks. Use a lower percentage for stricter
protection if desired.

Initialization creates a reserved `.reicloud-sync-access` file in both roots.
Keep it in place: every real run checks access markers to help detect missing
mounts or unavailable folders. Missing roots are reported before bisync starts;
reicloud never creates the sync roots automatically.

Persistent bisync history and atomic status records live beneath the state
directory, separated by configuration directory and folder roots. A per-pair
lock prevents concurrent runs and remains held by an active rclone subprocess.
Pair configuration edits wait until active operations finish. A sync preview
uses a temporary copy of history and does not update last-success timestamps.

Retryable bisync failures (exit 1 or 2 during normal sync) retain usable history
and the last-success timestamp. Status reports `retryable_error`; fix the cause
or review a deletion-limit preview, then run `sync` again.

Older reicloud versions incorrectly marked all failures as requiring recovery.
If an earlier normal sync hit that lockout, explicitly retry its existing history:

```sh
rpy run reicloud sync notes --retry --dry-run
rpy run reicloud sync notes --retry
```

`--retry` never resets history, clears rclone locks or enables resync. Missing
history and rclone's critical safety checks still block the run.

Critical failures and interrupted operations require explicit recovery. Inspect
the rclone diagnostics and restore missing roots before considering recovery:

```sh
rpy run reicloud status notes
rpy run reicloud recover notes --dry-run --prefer local
rpy run reicloud recover notes --prefer local
```

Recovery explicitly reinitializes history and can resurrect previously deleted
files or overwrite differing files according to `--prefer`. Reicloud never runs
it automatically. Failed history is retained beside the new history. A failed
operation keeps the previous successful-run timestamp; a stale running record
without an active lock is reported as needing recovery. Initialization refuses
to reset existing state. Failed initialization and recovery also require explicit
recovery; they cannot be retried as normal syncs.

```sh
rpy run reicloud remove notes
```

Removal unregisters the pair and leaves all local and remote files and saved sync
history untouched. Re-adding the same roots in the same configuration reuses that
history; changing roots creates separate state.
Pairs live in a separate `pairs.toml` beside `config.toml`. Writes are locked and
atomic; the file's comments and formatting are regenerated when pairs change.
`config check` validates both files, including pair overlaps, without requiring
local folders to be mounted. Adding a pair and running a preview require the local
folder to be available. Actual initialization, sync and recovery also require
both roots to be accessible.

## Exclusions

```sh
reicloud exclude notes '.venv/**'
reicloud exclude notes '__pycache__/**'
reicloud exclusions notes
reicloud exclusions notes --apply --prefer local --dry-run
reicloud exclusions notes --apply --prefer local
```

Rules use rclone glob syntax (quote patterns to prevent shell expansion), not
`.gitignore` syntax. They exclude matching paths in both directions. No rules are
enabled by default. The reserved access marker is always included.

For initialized pairs, changes are staged: scheduled and manual sync stop until
you explicitly apply them. Application rebuilds selection history and combines
included files using `--prefer`; review the preview for competing versions and
previously deleted files that could reappear. Excluded existing files remain on
both sides; they are never deleted just because they became excluded. Before
initialization, rules take effect on the first `init`.

`reicloud include notes '.venv/**'` removes that exclusion rule. Removing a rule
also requires preview/application for an initialized pair. `status` and
`exclusions --json` show pending changes. Failed history is retained when rules
are applied; the runner never applies rules or resyncs automatically.

## Activity logs

```sh
reicloud logs notes
reicloud logs notes --lines 100 --follow
reicloud logs notes --json
```

Per-pair logs contain operation start/completion/failure events and live rclone
transfer diagnostics. JSON output is one object per line. Logs rotate at 1 MiB
with three backups, under the pair's private state directory. Log files are
created with mode `0600`. Previews are recorded but do not alter trusted history.
OAuth setup output is never recorded; credential-related diagnostic lines are
redacted. Logs may contain file names. A logging failure warns once for that
operation and does not turn a completed transfer into a sync failure.

## Desktop notifications

```sh
reicloud notifications on
reicloud notifications test
reicloud notifications off
```

Desktop alerts are off by default and require `notify-send` plus an available
desktop notification session. The background runner alerts once per incident
that needs attention (including safety/critical errors), then reports recovery
after a later successful scheduled sync. Transient retries do not create repeated
popups. Delivery attempts are deduplicated across runner restarts. Alert bodies
use pair names and status guidance, not file names or provider diagnostics.
Missing desktop tools/session or failed delivery does not stop synchronization.
`notifications test` sends a test alert even while notifications are disabled.

## Local watching

```sh
reicloud run --watch
```

Watching uses watchfiles to notice local changes recursively, then coalesces
bursts into a sync after roughly two quiet seconds (plus watcher/scheduler
latency). Periodic scans still detect changes made in Dropbox. Plain `run` keeps
using intervals only.

Events during an active transfer produce at most one pending follow-up scan;
this also catches local edits made during a download. Local events never bypass
pause, staged exclusions, critical failures, deletion limits or retry backoff.
Watch errors fall back to periodic synchronization and retry attaching a watcher.
Excluded-path events may trigger a scan, but the same exclusion rules still
control transfers. No `.gitignore` or hidden-file defaults are silently added.

For a service, regenerate its unit with watching enabled, then restart it:

```sh
reicloud service install --watch
reicloud service stop
reicloud service start
```

Reinstalling without `--watch` returns the unit to interval-only operation.
Watching needs an available filesystem-notification backend; watchfiles may use
polling on unsupported filesystems. `run --once` cannot be combined with `--watch`.

## Foreground runner

```sh
reicloud interval notes 2m
reicloud run
```

The runner synchronizes initialized pairs immediately, then waits each pair's
interval after completion. Existing pairs default to five minutes; use
`add --interval 30s` or `interval NAME 30s` to configure it. Intervals accept
seconds, minutes or hours (`30`, `30s`, `5m`, `1h`), from one second to seven days.
Pair configuration is reloaded between scheduler ticks. Up to two independent
pairs run concurrently; set `run --jobs N` to change this. Pair locks also protect
manual runs. Only one runner may use a given configuration at a time.

`run --once` processes eligible pairs once and exits. Ctrl+C or SIGTERM stops new
work and asks active transfers to finish gracefully. No pair is automatically
initialized or recovered. Transient failures retry after 30 seconds, doubling to
at most one hour; success restores the normal pair interval. Critical failures,
deletion guards, missing roots, invalid configuration and unknown errors wait for
manual attention. A manual successful sync makes a blocked pair eligible again.
Retry scheduling never adds `--force` or resync. `run --once` makes one attempt per
eligible pair and returns a nonzero exit status if an attempted sync fails.

## Pause and resume

```sh
reicloud pause notes
reicloud resume notes
```

Pause persists across runner restarts and blocks future scheduled and manual
syncs. Active transfers finish normally; pause does not wait for them. Explicit
initialization, recovery and dry-run previews remain available. The runner notices
resume on its next tick and makes the pair eligible immediately. Pause state is
stored separately from transfer history, so a finishing transfer cannot erase it.

## Status

```sh
reicloud status
reicloud status notes --json
```

Status includes pause state, active transfer, runner ownership, interval, last
successful run, current and last error, next run and consecutive failures. With
no pair name it shows all pairs; JSON output is a list (a single named pair is an
object). Timestamps are UTC with an explicit offset.

Schedules and backoff counts are saved atomically and restored on runner restart.
`next_run` is shown only while a runner owns the configuration and the pair is
eligible; `scheduled_for` retains its saved deadline while the runner is stopped.
Pausing hides the next run without erasing history. Historical last-error details
remain visible after a later successful sync. Broken state for one pair is
reported separately when listing all pairs.

## Systemd user service

Preview the unit, then install and start it when ready:

```sh
reicloud service install --dry-run
reicloud service install
reicloud service start
reicloud service status
```

Installation writes a unit in your XDG systemd user directory, reloads the user
manager and enables it for future user sessions. It does not start or restart the
service. `start` begins syncing initialized, unpaused pairs. Each configuration
gets its own unit name. No root privileges are needed.

```sh
reicloud service stop
reicloud service disable
reicloud service enable
reicloud service path
journalctl --user -u "$(basename "$(reicloud service path)")" -f
```

`stop` stops the current runner; `disable` prevents startup in future sessions
without stopping a currently running service. Use both to stop it permanently.
`enable` restores startup for future sessions. The unit uses the Python environment
from installation, an explicit config path and the current state/cache locations.
Reinstall after moving or replacing that environment. `service install --jobs N`
sets the worker concurrency for the unit. Keep application settings in reicloud's
config files; reinstallation regenerates a managed unit and refuses to replace an
unmanaged unit or symlink.

Systemd captures logs in the user journal and restarts an unexpectedly failed
runner. On stop it gives the runner time to shut transfers down gracefully before
forcing termination. Without user lingering, startup and lifetime follow your
system's user-session policy; installation does not change that policy.

## Configuration

```sh
rpy run reicloud config paths
rpy run reicloud config paths --json
rpy run reicloud config check
rpy run reicloud doctor
```

Inspection does not create directories. Setup creates private application
directories and an initial `config.toml`, preserving existing settings.

| Location | Default |
| --- | --- |
| Configuration | `$XDG_CONFIG_HOME/reicloud-sync` or `~/.config/reicloud-sync` |
| Persistent state | `$XDG_STATE_HOME/reicloud-sync` or `~/.local/state/reicloud-sync` |
| Cache | `$XDG_CACHE_HOME/reicloud-sync` or `~/.cache/reicloud-sync` |

Use `reicloud --config-dir /absolute/path setup` or `REICLOUD_CONFIG_DIR` to override
the configuration directory. Pass the same override to subsequent commands.
Relative XDG base directories are ignored, following the XDG specification.

Settings live in `config.toml`. OAuth credentials are managed by rclone in a
separate `rclone.conf` within the application config directory. Treat that file
as a secret. Reicloud does not modify your usual rclone config, print tokens,
or inherit `RCLONE_*` environment overrides. rclone must be available on PATH.

If login fails, rerun `reicloud check dropbox` to inspect access. An existing
remote is never automatically replaced; a reconnect workflow is still planned.

Status and errors use sysentropy logging on stderr. Command data, including
`--json` output, stays on stdout. No log files are created automatically.

## Tests

```sh
rpy run python -m unittest discover -s tests -v
```

Tests use temporary directories and mocked authentication. Transfer tests run
real rclone between disposable local directories, covering copies, edits,
deletions, conflicts, deletion guards, missing access markers and recovery.
Real Dropbox OAuth requires completing the browser login yourself.

## Workspace profiles (version 2)

New setup establishes a canonical workspace (default `~/Dropbox`) and saves a
computer name as `<label>-<first six machine-id characters>`, matching suisave's
convention. `setup --workspace PATH --computer LABEL` customizes the suggestions.
Local sync directories are created during initialization, not configuration.

The workspace targets `dropbox:` and always excludes `/.reicloud-computers/**`.
An external mapping added as `add NAME LOCAL` targets
`dropbox:.reicloud-computers/COMPUTER/NAME`. Shared content belongs inside the
canonical workspace. Separate local pairs cannot nest inside it. Explicit remote
arguments are accepted only when they match the derived external destination.

Existing version-1 setups remain usable and are never silently retargeted:

```sh
reicloud migrate --workspace ~/Dropbox --dry-run
```

If old root-level pairs cannot be retained under this policy, the preview lists
them. `--retire-legacy-pairs` explicitly unregisters them when migration is applied;
it does not move or delete local/remote files or their histories. Configuration is
backed up before migration. A partially published migration blocks readers and
can be restored with `migrate --rollback`. Stop an existing runner before migrating.
