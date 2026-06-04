# DataOpsStudio 2.0 In-Place Upgrade

This guide upgrades an **existing, already-running portable instance** to a target
version **without losing data**. It reuses the existing PostgreSQL cluster,
bootstrap secrets, admin user, and all datasources.

For a first-time / fresh bring-up instead, use
[`quickstart.md`](./quickstart.md) — do **not** use this guide on a machine that
has never been bootstrapped.

## When to use which guide

| Situation | Guide |
|---|---|
| Machine never deployed before; clean host | `quickstart.md` (full bootstrap) |
| Instance already running an older version; keep its data | **this guide** |
| Existing instance, but you want to wipe and start clean | `quickstart.md` after manually removing `$DATAOPS_HOME` and `config/secrets/` (destructive — old encrypted data is unrecoverable) |

## Two hard rules

These two commands are safe on a fresh machine but **destructive on an existing
instance**. Never run them during an in-place upgrade:

- ❌ **`bootstrap init`** — regenerates `config/.secret_master.key`. Every value
  encrypted with the old master key (datasource passwords, secret refs) becomes
  permanently unreadable.
- ❌ **`admin create`** — this is create-*or-update*. Running it resets the
  existing admin user's password.

## Prerequisites

- You can already reach the server (SSH or a local shell on the host).
- The repository is already checked out on the host and was previously brought up
  with `quickstart.md`.
- You know the exact `DATAOPS_HOME` and `DATAOPS_PG_BIN_DIR` values the existing
  instance uses. Pointing `--root` at a different directory makes the launcher
  behave as if it were a brand-new install and it will not see the old database.

> Secrets discipline: never paste the server IP, SSH key, account, or any
> credential into commit messages, scripts, or shared logs. Scripts use the
> placeholder repo path `/path/to/dataOpsStudio_v2`; substitute your real path
> locally only.

## 1. Pre-flight (read-only — changes nothing)

```bash
cd /path/to/dataOpsStudio_v2
git fetch origin
git status                  # must be clean; stop and investigate if tracked files are modified
git rev-parse HEAD          # record the currently-running version

# Re-export the SAME values the existing instance already uses:
export DATAOPS_HOME="$PWD/.dataops-runtime"            # <- your existing value
export DATAOPS_PG_BIN_DIR="/usr/lib/postgresql/16/bin"  # <- your existing value
export LAUNCHER="uv run python -m app.launcher --root $DATAOPS_HOME --pg-bin-dir $DATAOPS_PG_BIN_DIR"

$LAUNCHER status            # confirm what API / worker / PG state you are starting from
```

`.dataops-runtime/` and `config/secrets/` are git-ignored, so the `git checkout`
in step 3 will not touch them — **provided `git status` is clean**. That is why
the pre-flight checks `git status` before anything mutates.

## 2. Graceful stop (first step that changes running state)

```bash
$LAUNCHER stop
```

`stop` is graceful: it waits for the worker to finish its current job. A large
in-flight SQL query can make this take several minutes.

Emergency stop only if you accept that an in-flight worker process may be killed:

```bash
$LAUNCHER stop --force
```

After `--force`, the JobBackend reaper recovers stale `running` jobs once the
worker heartbeat timeout elapses (2.0.0 default is long — see ADR-0018 — so
recovery is not immediate).

## 3. Align to the target version

```bash
git checkout main
git pull --ff-only origin main
git rev-parse HEAD          # must equal the target commit/tag you intend to run
uv sync --all-groups        # re-sync locked dependencies
```

To pin an exact release instead of `main`, `git checkout <tag-or-commit>` and
confirm `git rev-parse HEAD` matches it.

## 4. Migrate and start

```bash
$LAUNCHER alembic-up        # apply forward migrations explicitly so you see the output
```

`up` also runs migrations, but running `alembic-up` first makes the schema change
visible before the API starts. Migrations are forward-only; if `alembic-up`
fails, do **not** run `up` — capture the error (with any password / connection
string redacted) and resolve the migration first.

Start the services. Run `up` under a process supervisor so an SSH disconnect does
not take the instance down (a first-class systemd unit is still a backlog item):

```bash
tmux new -s dataops -d "$LAUNCHER up"
# or:
nohup $LAUNCHER up > "$DATAOPS_HOME/logs/up.console.log" 2>&1 &
```

Logs remain at `$DATAOPS_HOME/logs/{api,worker,pg}.log`.

## 5. Verify same version and health

```bash
$LAUNCHER status            # API, worker, and PG all up
git rev-parse HEAD          # confirm the running checkout is the target version

curl -sS -X POST http://127.0.0.1:8020/api/auth/login \
  -H 'content-type: application/json' \
  -d '{"username":"admin","password":"<admin-password>"}'
```

A real `access_token` in the response confirms the metadata DB, secrets, and API
all came back up against the existing data.

## Rollback

If the new version misbehaves and the migration is reversible:

```bash
$LAUNCHER stop
git checkout <previous-commit>   # the value recorded in step 1
uv sync --all-groups
$LAUNCHER up
```

If a migration must be undone, downgrade it explicitly before starting the older
code (`alembic downgrade <previous-revision>`); only do this for migrations you
have confirmed are safely reversible.
