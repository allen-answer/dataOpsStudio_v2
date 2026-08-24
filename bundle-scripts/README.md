# Bundle runtime scripts

`bundle-scripts/` is the single maintained runtime contract for both desktop packages:

- Windows 10: `_common.ps1`, `install.ps1`, `start.ps1`, `stop.ps1` and the `.cmd` shims.
- Linux Mint: `_common.sh`, `install.sh`, `start.sh` and `stop.sh`.

The package builders copy the applicable files into the package root. Do not maintain a second
template copy under a builder directory.

Both implementations use API port `8020`, managed PostgreSQL port `15432`,
`DATAOPS_ADMIN_USER=admin`, and an overridable `DATAOPS_HOME`. They force `UV_OFFLINE=1`,
`UV_NO_SYNC=1`, `UV_FROZEN=1` and `UV_PYTHON_DOWNLOADS=0` at runtime. Installation and startup are
idempotent: an existing virtual environment and initialized home are reused.

On first start the scripts create the offline virtual environment, bootstrap secrets, managed
PostgreSQL, migrations and the admin user. `DATAOPS_ADMIN_PASSWORD`, when supplied by the Tauri
first-run form or an operator, is passed only to `app.launcher admin create`; it is never written to
a script or package file. Later starts call the launcher `up` path directly.

The Mint installer rejects a PostgreSQL Unix socket path longer than 107 UTF-8 bytes before
building the environment. Windows PostgreSQL uses loopback TCP only and does not configure a Unix
socket directory, so spaces and deep paths in `DATAOPS_HOME` do not enter PostgreSQL's `-k` option.
Both stop scripts call the launcher's graceful stop path; the Tauri shell adds a bounded wait and
platform-specific process tree/group kill only if graceful shutdown does not finish.

After the venv exists, both script families invoke the launcher through the stable top-level
`updater` module. With no active payload this resolves to the immutable full-package `app/` and
`frontend/dist/`. With a verified payload it resolves both paths and `DATAOPS_BUILD_*` from the same
atomic active pointer. The scripts never parse or evaluate update manifest values themselves.

Payload installation, health completion and rollback commands are documented in
[`docs/packaging.md`](../docs/packaging.md). Stage 1 requires an owner-provisioned trust store and is
not yet exposed as a Tauri button or an online check.

Build and package instructions live in [`docs/packaging.md`](../docs/packaging.md).
