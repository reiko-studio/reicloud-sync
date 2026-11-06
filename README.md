# reicloud-sync

Two-way cloud sync for Linux. A fancy wrapper around rclone with an automation layer.

Part of **Reiko Studio**.

## Features

- **Shared workspace.** Choose a local folder for your cloud account. Files
  you put there sync to the same cloud paths on each computer.
- **Folders outside the workspace.** Sync a project or documents folder to
  its own spot under your computer's name in the cloud. Reicloud creates the
  remote folder during initialization.
- **Background sync.** Run on a schedule, react to local changes, or enable
  remote change notifications where supported. A systemd user service keeps
  sync running in the background.
- **Control what syncs.** Browse remote files, set folder exclusions, and preview
  initialization or a manual sync. A deletion limit can stop a run before
  it propagates a large batch of deletions.
- **See what happened.** Check the last successful sync, follow per-folder logs,
  and opt in to desktop alerts when a sync needs attention.

## Provider support

As of v0.1.1, Dropbox is the only supported provider. Google Drive support
is planned.

## Install

**Requirements**

* Python 3.14 or newer
* `rclone` (tested on v1.75.2) on your PATH
* `systemd` for the background service

**Installation**

Download the wheel from [GitHub Releases](https://github.com/reiko-studio/reicloud-sync/releases)
if one is available, or build it from the repository root with Python and pip.

```sh
python3.14 -m venv .venv.local.build
.venv.local.build/bin/python -m pip wheel --no-deps --wheel-dir dist .
```

Use either [rpy](https://github.com/reiko-studio/rpy) or uv from the directory
containing the wheel. For a local build, that directory is `dist/`. Replace the
example filename with the wheel you downloaded or built.

**With rpy**

```sh
rpy tool install ./reicloud_sync-0.1.2-py3-none-any.whl --python python3.14
```

**With uv**

```sh
uv tool install ./reicloud_sync-0.1.2-py3-none-any.whl --python 3.14
```

Both commands install `reicloud` into an isolated tool environment. Check the
installation from a shell outside any activated development environment.

```sh
reicloud doctor
```

If the shell can't find `reicloud`, add the tool executable directory to your
PATH. rpy uses `~/.local/bin`. For uv, run `uv tool update-shell` and open a new
shell.

## Set up your first workspace

A workspace is a local directory that maps to the root of your cloud provider.

```sh
reicloud setup
```

The setup wizard lets you pick a provider, choose the local folder, name the
computer, and connect your account. It previews the setup before saving it.

> [!NOTE]
> Reicloud appends a short machine ID to your computer label. Folders outside
> the workspace sync under that name in the cloud, which keeps each computer's
> mappings separate and lets the shared workspace filter them out.

For Dropbox, the workspace defaults to `~/Dropbox` and the sync job is called
`workspace-dropbox`. Preview and initialize it when you're ready to sync.

```sh
reicloud init workspace-dropbox --dry-run
reicloud init workspace-dropbox
reicloud status workspace-dropbox
```

Initialization runs the first sync in your terminal and brings together files
from both sides. Run it once per workspace on each computer. Use the next
section to start background sync. If a file exists on both sides with different
contents, review the preview with `--prefer local`
or `--prefer remote` before running initialization with that choice.

To share the workspace with a second PC, finish the first sync, then repeat
setup and initialization on the second PC with the same cloud account.
For example, `~/Dropbox/code` on both PCs syncs with
`dropbox:code`.

## Keep sync running

```sh
reicloud remote-watch on --remote dropbox
reicloud service install --watch
reicloud service start
reicloud service status
```

`remote-watch on` is optional and lets Dropbox remote changes trigger a sync job.
`service install --watch` enables the user service with local file watching.
`service start` starts it now. The runner also scans each eligible pair on its
interval, which defaults to five minutes. For a foreground session, use
`reicloud run --watch` with the service stopped.

See the [service guide](docs/usage.md#runner-and-service) for starting at boot
and keeping sync running after logout.

## Sync a folder outside the workspace

```sh
reicloud add results ~/documents/results --remote dropbox
reicloud init results --dry-run
reicloud init results
reicloud show results
```

Reicloud derives a destination such as
`dropbox:.reicloud-computers/p16v-d2e6ba/results`. The workspace skips
`.reicloud-computers/` on every PC, so these folders stay out of the shared
workspace. Put folders you want under the same shared path on all devices
inside their workspaces.

## Everyday commands

| What you want to do | Command |
| --- | --- |
| Browse shared cloud files | `reicloud browse` |
| List computer folders in the cloud | `reicloud browse --computers` |
| Check all sync jobs | `reicloud status` |
| Preview the next workspace sync | `reicloud sync workspace-dropbox --dry-run` |
| Set the workspace interval | `reicloud interval workspace-dropbox 30s` |
| Pause or resume a mapping | `reicloud pause results` / `reicloud resume results` |
| Follow activity for the workspace | `reicloud logs workspace-dropbox --follow` |
| Enable desktop alerts | `reicloud notifications on` |

## Documentation

- [Exclusions](docs/usage.md#exclusions)
- [Sync safety and recovery](docs/usage.md#initialization-and-manual-sync)
- [Configuration and migration](docs/usage.md#configuration-and-migration)
- [Upgrading and uninstalling](docs/usage.md#upgrade-or-roll-back)
- [Development, tests, and release builds](docs/development.md)

## License

See [LICENSE](LICENSE).
