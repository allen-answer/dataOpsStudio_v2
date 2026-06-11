# DataOpsStudio 2.0 Portable Quickstart

This guide starts a fresh local DataOpsStudio runtime: bootstrap secrets, managed
PostgreSQL metadata DB, migrations, admin user, API, and worker.

## Prerequisites

- Python 3.12.x
- `uv`
- `curl`
- PostgreSQL server binaries: `initdb`, `pg_ctl`, `postgres`
- A reachable MySQL database for the final SQL datasource check

After cloning or unpacking DataOpsStudio for the first time, install the locked
Python dependencies before running launcher commands:

```bash
uv sync --all-groups
```

Run the launcher as a normal OS user, not as `root`. PostgreSQL `initdb` refuses
to initialize a cluster as root, and container images must use a real user that
exists in `/etc/passwd` rather than only a numeric UID.

## PostgreSQL Binaries

Portable mode expects PostgreSQL server binaries to be present locally. There are
two supported ways to provide them:

1. Install PostgreSQL on the machine and keep `initdb`, `pg_ctl`, and `postgres`
   on `PATH`.
2. Bundle PostgreSQL with the portable package and pass its `bin` directory using
   `--pg-bin-dir`.

Examples:

```bash
# macOS Homebrew
brew install postgresql@16
export DATAOPS_PG_BIN_DIR="$(brew --prefix postgresql@16)/bin"

# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y postgresql-16 postgresql-client-16
export DATAOPS_PG_BIN_DIR="/usr/lib/postgresql/16/bin"
```

If binaries are already on `PATH`, omit `--pg-bin-dir` below. Otherwise include
it before the subcommand in every launcher call.

```bash
export DATAOPS_HOME="$PWD/.dataops-runtime"
export LAUNCHER="uv run python -m app.launcher --root $DATAOPS_HOME --pg-bin-dir $DATAOPS_PG_BIN_DIR"

$LAUNCHER doctor
```

## 1. Generate Bootstrap Secrets

```bash
$LAUNCHER bootstrap init
```

The launcher creates:

- `config/.secret_master.key`
- `config/secrets/pg_app_password`
- `config/secrets/pg_superuser_password`
- `config/secrets/jwt_secret`

On Linux/macOS these files are `0600`. Do not change them to `0400`; the
BootstrapSecrets reader also enforces `0600`. On Windows, chmod is skipped and
Windows ACL hardening is deferred to a later release.

These files are Bootstrap Secrets. They are read before connecting to PG and
must never be copied into `secret_refs`.

## 2. Start Metadata PostgreSQL

```bash
$LAUNCHER pg-up
```

`pg-up` is idempotent. If `$DATAOPS_HOME/data/pgdata` already contains a
PostgreSQL cluster, the launcher starts that cluster instead of running `initdb`
again.

## 3. Run Migrations (Optional, `up` Also Runs This)

```bash
$LAUNCHER alembic-up
```

The launcher reads `config/secrets/pg_app_password` and injects it as process
environment for Alembic. `POSTGRES_DEV_PASSWORD` is not used by the launcher or
dev Makefile targets.

## 4. Create Admin User

```bash
$LAUNCHER admin create \
  --username admin \
  --password 'change-this-password' \
  --project-name Default
```

The command stores only a bcrypt hash in `users.password_hash`.

## 5. Start API and Worker

Run this in a foreground terminal:

```bash
$LAUNCHER up
```

Logs are written to:

- `$DATAOPS_HOME/logs/api.log`
- `$DATAOPS_HOME/logs/worker.log`
- `$DATAOPS_HOME/logs/pg.log`

Graceful stop waits for the worker to finish its current job. Large SQL queries
can make stop take several minutes. Use this in another terminal:

```bash
$LAUNCHER stop
```

Emergency stop:

```bash
$LAUNCHER stop --force
```

Use `--force` only when you accept that an in-flight worker process may be killed;
the JobBackend reaper will recover stale running jobs after the heartbeat
timeout.

### Running under systemd (production)

`launcher up` in a foreground terminal (or `tmux`/`nohup`) is fine for dogfood,
but a long-lived server should run under a process supervisor so an SSH
disconnect does not take the instance down. Template systemd units live in
[`systemd/`](./systemd/) — `dataops.service` (all-in-one: managed PG + API +
worker) and `dataops-worker.service` (split worker layout). They are templates:
substitute every placeholder path/user locally and never commit real values.
`systemctl stop` sends SIGTERM, which is the graceful path (same as `stop` above,
not `stop --force`). See [`systemd/README.md`](./systemd/README.md).

### Log rotation (journald)

Foreground `launcher up` writes plain files under `$DATAOPS_HOME/logs/`; those
files are **not** rotated, so a long-running instance will eventually fill the
disk. The supported production answer is to run under systemd and let **journald**
own the logs (the unit templates set `StandardOutput=journal`). journald rotates
automatically by size/age — no `logrotate` config needed.

Read and follow the logs:

```bash
journalctl -u dataops -f                 # all-in-one unit
journalctl -u dataops -u dataops-worker  # split layout, both units
journalctl -u dataops --since "1 hour ago"
```

Recommended quota (cap how much disk journald keeps). Edit
`/etc/systemd/journald.conf` or drop a file in `/etc/systemd/journald.conf.d/`:

```ini
[Journal]
# Persist across reboots so post-mortem logs survive a restart.
Storage=persistent
# Hard cap on total journal size; tune to the host's disk.
SystemMaxUse=2G
# Drop the oldest data when a single file would exceed this.
SystemMaxFileSize=200M
# Keep at most this much history regardless of size.
MaxRetentionSec=30day
```

Apply with `sudo systemctl restart systemd-journald`. The application logs are
already JSON (structlog) with the R5 redaction processor enforced, so secrets
never reach journald in the first place; the quota just bounds disk usage.

## 5b. Serve the Frontend from the API process (single host, no reverse proxy)

For 2.0 the frontend and backend run on the **same host, same process, with no
reverse proxy**: the API process serves the built Vue SPA directly. This avoids
running a separate static server and the local-only `vite dev` + SSH-tunnel
setup. Skip this whole section if you only want the API (default behavior is
unchanged — without `DATAOPS_FRONTEND_DIST` set, the API serves no static files).

1. Build the SPA (produces `frontend/dist/` with `index.html` + hashed
   `assets/`):

   ```bash
   cd frontend
   npm ci
   npm run build
   cd ..
   ```

2. Point the API at the built dist directory and start the runtime:

   ```bash
   export DATAOPS_FRONTEND_DIST="$PWD/frontend/dist"
   $LAUNCHER up
   ```

3. Open `http://<host>:8020/` in a browser — the full stack (SPA + API) is now
   served from port 8020. No port 5173, no tunnel.

Mount semantics:

- `/api/*` and `/healthz` keep their existing routing and take priority; serving
  the frontend does not weaken API auth (a protected `/api/*` call without a
  token still returns `401`, and an unknown `/api/*` path still returns API
  `404` JSON — it is never replaced by `index.html`).
- `/assets/*` is served from `dist/assets` with a long, immutable cache header
  (`public, max-age=31536000, immutable`) — safe because Vite content-hashes
  asset filenames.
- `index.html` is served with `Cache-Control: no-cache`, so a new release is
  picked up immediately.
- Any other GET path that is not a real file (e.g. `/projects/123`,
  `/admin/users`) returns `index.html`, so client-side routes work on direct
  link and refresh.

`DATAOPS_FRONTEND_DIST` is ignored (API-only fallback, no error) if the path does
not exist or has no `index.html` — useful when the dist has not been built yet.

## 6. Login

```bash
TOKEN="$(
  curl -sS -X POST http://127.0.0.1:8020/api/auth/login \
    -H 'content-type: application/json' \
    -d '{"username":"admin","password":"change-this-password"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
)"

PROJECT_ID="$(
  curl -sS http://127.0.0.1:8020/api/projects \
    -H "authorization: Bearer $TOKEN" \
  | python -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])'
)"
export PROJECT_ID
```

## 7. Create a MySQL Datasource

Set these for a reachable MySQL database:

```bash
export MYSQL_HOST=127.0.0.1
export MYSQL_PORT=3306
export MYSQL_USER=dataops
export MYSQL_PASSWORD='mysql-password'
export MYSQL_DATABASE=dataops_demo
```

Create the datasource:

```bash
DATASOURCE_BODY="$(
  python - <<'PY'
import json
import os

print(json.dumps({
    "project_id": os.environ["PROJECT_ID"],
    "name": "quickstart-mysql",
    "db_type": "mysql",
    "host": os.environ["MYSQL_HOST"],
    "port": int(os.environ["MYSQL_PORT"]),
    "username": os.environ["MYSQL_USER"],
    "password": os.environ["MYSQL_PASSWORD"],
    "database": os.environ["MYSQL_DATABASE"],
    "environment": "dev",
    "extra": {},
}))
PY
)"

DATASOURCE_ID="$(
  curl -sS -X POST http://127.0.0.1:8020/api/datasources \
    -H "authorization: Bearer $TOKEN" \
    -H 'content-type: application/json' \
    -d "$DATASOURCE_BODY" \
  | python -c 'import json,sys; print(json.load(sys.stdin)["id"])'
)"
```

Test it through the worker:

```bash
curl -sS -X POST "http://127.0.0.1:8020/api/datasources/$DATASOURCE_ID/test" \
  -H "authorization: Bearer $TOKEN"
```

## 8. Run SELECT 1+1

```bash
EXECUTION="$(
  curl -sS -X POST http://127.0.0.1:8020/api/sql/execute \
    -H "authorization: Bearer $TOKEN" \
    -H 'content-type: application/json' \
    -d "{\"datasource_id\":\"$DATASOURCE_ID\",\"sql\":\"SELECT 1+1 AS r\",\"params\":{}}"
)"

JOB_ID="$(printf '%s' "$EXECUTION" | python -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')"

for _ in 1 2 3 4 5 6 7 8 9 10; do
  STATUS="$(
    curl -sS "http://127.0.0.1:8020/api/jobs/$JOB_ID" \
      -H "authorization: Bearer $TOKEN" \
    | python -c 'import json,sys; print(json.load(sys.stdin)["status"])'
  )"
  test "$STATUS" = "success" && break
  sleep 1
done

curl -sS "http://127.0.0.1:8020/api/jobs/$JOB_ID/result?offset=0&limit=10" \
  -H "authorization: Bearer $TOKEN"
```

Expected result includes column `r` and the first row value `2`:

```json
{
  "columns": [{"name": "r"}],
  "rows": [{"values": [2]}]
}
```

## DM 数据源注记 (worker DM 客户端加密库)

If you create a DM (达梦) datasource instead of MySQL, the **worker** process needs
two extra environment variables so the `dmpython` driver can `dlopen` the DM
client crypto modules. Without them, connecting/executing against DM fails with
`-70089` (encryption module load failure) — the PyPI `dmpython` wheel bundles a
crypto library missing transitive dependencies, so the driver must instead use
the complete library set under a real DM client install.

Set these for the process that runs `launcher up` (or the split worker), pointing
`DM_HOME` at your actual DM install path:

```bash
export DM_HOME=/opt/dmdbms                                                   # actual DM install path
export LD_LIBRARY_PATH="$DM_HOME/bin:$DM_HOME/bin/external_crypto_libs"
$LAUNCHER up
```

Root cause and the full one-time fix (clearing the wheel's orphaned crypto libs)
are documented in [`acceptance-dm-certified.md`](../acceptance-dm-certified.md)
("环境注记").

## Troubleshooting

### `doctor` Reports `PG TCP port free: ... is already accepting TCP connections`

The metadata PostgreSQL port is already in use. Pick another local port and keep
the same value for every launcher command:

```bash
export DATAOPS_PG_PORT=15433
$LAUNCHER doctor
```

### `doctor` Reports `MISSING uv`

Install `uv`, then start a fresh shell or add its install directory to `PATH`.
The launcher checks `PATH`, `~/.local/bin`, and `~/.cargo/bin`.

### `pg-up` Fails During `pg_ctl start`

The launcher prints the last lines of `$DATAOPS_HOME/logs/pg.log` automatically.
Use those lines first; they usually show port conflicts, permission issues, or a
bad PostgreSQL data directory.
