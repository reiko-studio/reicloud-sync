# reicloud-sync

A Linux-first personal cloud sync manager powered by rclone. Part of Reiko Studio.

Currently implements configuration setup, Dropbox login, named folder pairs,
manual two-way synchronization and explicit recovery. Background services are planned.

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
