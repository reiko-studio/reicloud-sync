# reicloud-sync

A Linux-first personal cloud sync manager powered by rclone. Part of Reiko Studio.

Currently implements configuration setup and Dropbox login. Folder synchronization
and background services are planned; no synchronization commands are implemented.

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

Tests use temporary directories and mocked authentication; real Dropbox OAuth
requires completing the browser login yourself.
