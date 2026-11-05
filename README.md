# reicloud-sync

A Linux-first personal Dropbox sync manager powered by rclone. Part of Reiko Studio.

Keep a shared Dropbox folder on each computer, plus optional computer-specific
mappings for folders that live elsewhere. Reicloud handles configuration,
initialization, periodic sync, local watching, logs and desktop alerts; rclone
handles OAuth and file transfers. Google Drive support is planned.

## Install the release

Install a release wheel with rpy. Reicloud needs Python 3.14 or newer and
`rclone` on your PATH.

```sh
rpy tool install ./reicloud_sync-0.1.1-py3-none-any.whl --python python3.14
~/.local/bin/reicloud doctor
```

Run these commands outside any activated development environment or use the
explicit `~/.local/bin/reicloud` path throughout. rpy exposes the command there
and keeps its dependencies in a dedicated tool environment.

After setup and initialization, install and start the user service:

```sh
~/.local/bin/reicloud service install --watch
~/.local/bin/reicloud service start
~/.local/bin/reicloud service status
~/.local/bin/reicloud doctor
```

Doctor shows both the CLI interpreter and the interpreter configured in the
unit. They should point to the same tool environment. The service starts
in future user sessions. To start it at boot and keep it running after logout,
explicitly enable lingering with `loginctl enable-linger "$USER"`.

To upgrade or roll back, stop the service, run `rpy tool uninstall reicloud-sync`,
then install the desired wheel and rerun `service install --watch` and
`service start` from the installed command. Keep the previous wheel for rollback.
The rpy uninstall removes the package environment. Reicloud's own
`uninstall --wipe` also deletes local setup and history, so reserve it for a reset.

## Install for development

Requires Python 3.14+, rpy and rclone on PATH.

```sh
rpy env create --python python3.14
rpy pip install -e .
```

## Shared workspace

For a guided first setup, run `reicloud setup` in a terminal. A small wizard asks
for the workspace folder, computer label and whether to connect Dropbox, then
shows the resolved setup before saving. Ctrl+C cancels before any setup
writes. The wizard never initializes synchronization or installs a service.
Existing workspace identities are displayed and preserved; legacy pairs require
an explicit migration rather than being silently converted.

Use `setup --interactive` to force the wizard, or `--non-interactive` to suppress
it. Providing setup flags keeps the command noninteractive unless
`--interactive` is also specified. Redirected/non-terminal input uses flags and
defaults without prompts. The equivalent explicit workflow is:

```sh
reicloud setup --workspace ~/Dropbox --computer p16v --login
reicloud init workspace --dry-run
reicloud init workspace
reicloud run --watch
```

Setup saves your computer name as `<label>-<first six characters of machine-id>`,
matching suisave's convention. The default label is the hostname. The name is
saved permanently; renaming the host does not retarget existing mappings.
The default workspace is `~/Dropbox`. Setup and login do not sync files.

The workspace maps to `dropbox:`. Repeat setup and initialization on a second PC
to share its content. For example, `~/Dropbox/experiment-code` on each PC maps to
the same Dropbox `/experiment-code`. Each PC keeps its own credentials and sync
history. Concurrent edits are preserved as conflict copies for manual merging.
Complete the first PC's initial sync before initializing the second.

## External folders

```sh
reicloud add results ~/documents/results
reicloud show results --json
reicloud init results --dry-run
reicloud init results
```

This derives `dropbox:.reicloud-computers/p16v-ABCDEF/results`, where `ABCDEF`
represents the actual lowercase machine-ID prefix shown during setup. You do not
need to create the Dropbox directory first. Real initialization creates missing
local and remote roots, including parents. The preview reports planned creation
without creating sync directories or writing remote markers.

Every workspace always excludes `/.reicloud-computers/**`, including its own
computer's folders. External mappings therefore stay separate from both PCs'
workspace copies. Shared content belongs inside the canonical workspace.

External mappings cannot target the shared root or another computer's namespace.
Separate pairs cannot overlap locally, including through symlinks. This also
rejects a separate pair for a directory already inside the workspace. Use
`list`, `show NAME` and `status` to inspect roles and endpoints. `remove NAME`
unregisters an external mapping without deleting its files or saved history.
The canonical workspace cannot be removed independently of its profile.

## Initialization and manual sync

```sh
reicloud sync workspace --dry-run
reicloud sync workspace
reicloud status
reicloud status results --json
```

Initialization combines unique included files. Preference is optional for empty,
disjoint or identical selections. If same-path files differ, initialization stops
and requires `--prefer local` or `--prefer remote`; review a dry run first. Its
comparison uses rclone's size/hash checks. Explicit preference authorizes choosing
that side for differing files during initialization.

Routine sync propagates additions, edits and deletions in both directions. It
requires existing roots and access markers; it never recreates a missing root.
The reserved `.reicloud-sync-access` file must remain in both roots.

The deletion limit is 50% on either side. For intended larger deletions, review
`sync NAME --dry-run --max-delete 100`, then apply with the same limit. This does
not reset history or disable access checks. Retryable failures permit another
sync; critical failures and interruptions require explicit recovery:

```sh
reicloud recover workspace --dry-run --prefer local
reicloud recover workspace --prefer local
```

Recovery rebuilds history and can resurrect deleted files or overwrite differing
versions. It never runs automatically. Failed history is retained. Old versions'
incorrect recovery lockouts can be retried with `sync NAME --retry --dry-run`,
then `sync NAME --retry`, using existing history rather than resync.

## Exclusions

```sh
reicloud exclude workspace '.venv/**'
reicloud exclude workspace '__pycache__/**'
reicloud exclusions workspace
reicloud exclusions workspace --apply --prefer local --dry-run
reicloud exclusions workspace --apply --prefer local
```

Patterns use rclone glob syntax, not `.gitignore` syntax; quote them in the shell.
They apply in both directions. Existing excluded files remain in place on both
sides. `include NAME PATTERN` removes an exclusion. For initialized pairs, changes
are staged and sync waits for explicit application/rebuilding of selection
history. Preview competing versions and possible reappearance of deleted files.
For new pairs, exclusions take effect during initialization.

Managed workspace exclusions are shown separately and cannot be removed. The
access marker is always included. No user exclusion rules are enabled by default.

## Browse Dropbox

```sh
reicloud browse
reicloud browse experiment-code
reicloud browse --computers
reicloud browse --computer p16v
reicloud browse --json
```

Browsing is read-only. Listings distinguish shared content, computer namespaces
and registered mappings. Computer labels resolve only when unambiguous; use the
full saved name if necessary. Workspace content with user rules is labelled as
subject to those exclusions, rather than claiming each arbitrary glob was
resolved by the browser. Listing a remote directory does not prove it has synced.

## Runner and service

```sh
reicloud interval workspace 10s
reicloud pause results
reicloud resume results
reicloud run --watch
```

Intervals accept seconds, minutes or hours, from `1s` to seven days. They are
measured after completion; existing pairs default to five minutes. `run` uses
scheduled scans by default. `--watch` adds debounced local-change triggers.
Remote watching is configured separately for each Dropbox connection. Excluded-path events may trigger a scan, but
transfer exclusions still apply. Changes during a transfer coalesce into a
follow-up scan. Pause, pending filters, safety failures and retry backoff remain
in force. Pause allows active transfers to finish and persists across restarts.

Up to two independent pairs run concurrently (`--jobs N`). `run --once` makes one
pass and cannot be combined with watching. Transient failures back off from 30
seconds to one hour; critical, safety and unknown failures wait for attention.
Schedules survive restart. Only one runner may own a configuration. Ctrl+C or
SIGTERM requests graceful shutdown.

```sh
reicloud service install --watch --dry-run
reicloud service install --watch
reicloud service start
reicloud service status
reicloud service stop
reicloud service disable
```

Installation enables the systemd user unit for future sessions without starting
or restarting it. Reinstall and stop/start after changing unit options or moving
the Python environment. Stop does not disable future startup. No root privileges
are needed; installation does not change user lingering policy. Logs also appear
in the user journal. `service path` prints the generated unit path.

## Remote watching

On a v3 profile, enable Dropbox notifications for the registered connection:

```sh
reicloud remote-watch on --remote dropbox
reicloud service install --watch
reicloud service start
reicloud status workspace
```

The runner uses rclone's Dropbox change notifications to schedule earlier syncs.
It coalesces events, excludes computer-specific mappings from workspace triggers,
and applies a short cooldown after each successful sync. Periodic scans continue
through reconnects. Pause, deletion limits and retry backoff still apply.
The watcher was verified with rclone 1.75.2. Use `remote-watch off --remote dropbox`
to return to periodic remote scans.

## Logs and notifications

```sh
reicloud logs workspace --lines 100 --follow
reicloud logs workspace --json
reicloud notifications on
reicloud notifications test
```

Activity logs record previews, attempts and transfer diagnostics. They rotate at
1 MiB with three backups, use mode `0600`, and may contain file names. OAuth setup
output is excluded and credential-related diagnostic lines are redacted. JSON
log output is one object per line. Logging failure does not break syncing.

Desktop alerts are opt-in (`notifications off` disables them) and require
`notify-send` and a desktop session. The runner deduplicates attention alerts and
reports a later successful scheduled recovery. Failed delivery does not stop sync.

## Configuration and migration

`config paths`, `config check` and `doctor` inspect the installation. Configuration
uses `$XDG_CONFIG_HOME/reicloud-sync` (default `~/.config/reicloud-sync`); state and
cache use the corresponding XDG locations. Relative XDG bases are ignored.
`--config-dir` or `REICLOUD_CONFIG_DIR` overrides the config directory; use the
same override on subsequent commands. Settings live in `config.toml`, pairs in
`pairs.toml`, and dedicated OAuth credentials in `rclone.conf`. Treat that file as
a secret. Reicloud never modifies the usual rclone config and ignores ambient
`RCLONE_*` overrides. Status logs use sysentropy on stderr; command data uses stdout.

Version 3 registers connections in `config.toml`. Each connection's workspace
maps to its remote root. For example:

```toml
[remotes.dropbox]
workspace = "~/Dropbox"
remote_watch = false
```

External pairs in `pairs.toml` use a named target:

```toml
[pairs.results]
local = "~/documents/results"
targets = ["dropbox"]
```

`reicloud add results ~/documents/results --remote dropbox` writes this mapping
and derives its computer namespace. Reicloud discovers the backend from rclone.
This release runs Dropbox jobs with one target each. The v3 layout reserves
room for other backends and multiple targets.

For an initialized v2 workspace, stop any active runner and preview the upgrade:

```sh
reicloud migrate --dry-run
reicloud migrate
reicloud status workspace
reicloud sync workspace --dry-run
```

The migration backs up the previous settings and pairs. It preserves connection
names, endpoints, credentials and sync history, so the workspace can continue
from its existing baseline. For a fresh profile, `reicloud setup` shows Dropbox
in the provider picker and writes v3.

Version-1 configurations remain usable. Stop their runner and inspect migration:

```sh
reicloud migrate --workspace ~/Dropbox --computer p16v --dry-run
```

Pairs outside the new policy are listed for explicit retirement. Applying with
`--retire-legacy-pairs` unregisters those pairs without moving/deleting their local
or remote files or histories. Their root-level Dropbox data remains available to
the new workspace. Configuration is backed up before publishing the new profile;
incomplete migration blocks readers and can be restored with `migrate --rollback`.
Review local copies and the new initialization preview before production use.

## Uninstall and wipe

```sh
reicloud uninstall --dry-run
reicloud uninstall
reicloud uninstall --wipe --dry-run
reicloud uninstall --wipe --yes
```

`uninstall` disables/stops and removes only this configuration's managed systemd
unit. `--wipe` also removes local settings, dedicated rclone credentials, known
configuration backups and history/log directories provably owned by this profile.
Review the preview first; destructive wiping requires `--yes`.

Synchronized local folders, Dropbox contents, access markers inside those folders,
other profiles, unknown files and the installed CLI/rpy environment are preserved.
Empty application directories are pruned. Foreground runners and active manual
syncs block wiping; symlinked stores and unmanaged service files are rejected.
Ambiguous old state directories are preserved rather than guessed to be owned.
Removing credentials disconnects this local setup; it does not remove remote data.

## Tests

```sh
rpy run python -m unittest discover -s tests -v
```

Transfer tests use real rclone with disposable local account stand-ins, including
two independent computers, shared edits/deletions/conflicts and private mappings.
Desktop delivery and service controls are mocked. Systemd syntax and filesystem
watch events are checked with their real tools. Automated tests do not access
Dropbox. A real two-PC test remains the deployment gate.

## Build release artifacts

From the repository root, with a clean output directory:

```sh
uv build --out-dir dist
(cd dist && sha256sum ./*.whl ./*.tar.gz > SHA256SUMS)
```

Install the resulting wheel into a clean environment, run the CLI outside the
checkout, and verify the generated service unit before distributing it.
