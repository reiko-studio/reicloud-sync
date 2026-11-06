# Development

## Install for development

Requires Python 3.14+, rpy and rclone on PATH.

```sh
rpy env create --python python3.14
rpy pip install -e .
```

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

From the repository root, build the wheel and source archive with Python's
`build` package. Use a clean output directory.

```sh
python3.14 -m venv .venv.local.build
.venv.local.build/bin/python -m pip install build
.venv.local.build/bin/python -m build --outdir dist
(cd dist && sha256sum ./*.whl ./*.tar.gz > SHA256SUMS)
```

Install the resulting wheel into a clean environment, run the CLI outside the
checkout, and verify the generated service unit before distributing it.
