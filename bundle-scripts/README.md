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

Both installers reject a PostgreSQL Unix socket path longer than 107 UTF-8 bytes before building
the environment. Shorten `DATAOPS_HOME` if this preflight fails. Both stop scripts call the
launcher's graceful stop path; the Tauri shell adds a bounded wait and platform-specific process
tree/group kill only if graceful shutdown does not finish.

Build and package instructions live in [`docs/packaging.md`](../docs/packaging.md).
