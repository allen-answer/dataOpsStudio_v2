# DataOpsStudio 2.0 — Docker Compose (on-prem stack)

This is the **on-prem Docker** deployment form (design §1.2). It runs the same
flow as the portable launcher — metadata PostgreSQL + API + worker — but as a
Compose stack, satisfying the contract §5 acceptance line:

> docker compose(api+worker+pg)跑通同样流程。

It is intentionally separate from `docker/dev-pg.yml` (the dev single-PG
container) and never modifies it.

## What the stack contains

| Service | Kind | Role |
|---|---|---|
| `secrets-init` | one-shot | Generates bootstrap secrets into the shared `config` volume and a pg-readable password copy into `pgpw`. Idempotent (keeps existing secrets). |
| `pg` | long-lived | `postgres:16-alpine` metadata DB, internal network only (no host port). |
| `migrate` | one-shot | `alembic upgrade head` before API/worker start. |
| `api` | long-lived | `python -m app.main`, published on `127.0.0.1:18020`. |
| `worker` | long-lived | `python -m app.worker`, graceful SIGTERM drain. |
| `admin-create` | manual tool | One-off `launcher admin create` (compose `tools` profile). |

Startup order is enforced with `depends_on` conditions:

```
secrets-init (completes) → pg (healthy) → migrate (completes) → api + worker
```

### Ports

Only the API is published, on loopback: `127.0.0.1:18020 → :8020`. This avoids
ports already occupied on the shared daily-server
(`5432 / 8000 / 8010 / 8020 / 15432 / 16432`). PostgreSQL is reachable only on
the internal compose network — it is **not** published to the host.

### Secrets (R8 — no plaintext in any config file)

No password is ever written into the Dockerfile, the compose YAML, or git. The
`secrets-init` service runs `python -m app.launcher bootstrap init` at runtime,
writing four bootstrap files into the `config` volume:

- `config/.secret_master.key`
- `config/secrets/pg_app_password`
- `config/secrets/pg_superuser_password`
- `config/secrets/jwt_secret`

These are written `0600` owned by the app uid (`10001`). `LocalFileBootstrapSecrets`
enforces `0600` on Unix, so the API/worker can read them.

PostgreSQL runs as its own image user. To stay image-uid-agnostic (alpine `70`,
debian `999`) without running pg as a custom user — which breaks its socket and
`initdb` ownership assumptions — `secrets-init` writes a **separate pg-readable
copy** of the app password into the `pgpw` volume (mode `0644`, internal volume
only), pointed at by `POSTGRES_PASSWORD_FILE`. This mirrors `dev-pg.yml`, where
the docker `secrets:` file is likewise readable to the postgres user inside the
already-isolated container.

The bootstrap files live only inside the `dataops-v2-stack-config` /
`dataops-v2-stack-pgpw` Docker volumes. They are runtime state, never committed.

### Migration placement (design trade-off)

Migrations run in a dedicated short-lived `migrate` service, **not** in the API
entrypoint. Reasons:

- API and worker both start; a single ordered `migrate` step avoids two
  processes racing the same `alembic upgrade head` DDL.
- API/worker stay single-responsibility — no migration side effect on every
  restart or scale-up.

`migrate` runs `alembic upgrade head` directly from the locked venv (the runtime
image has no `uv`, so the launcher's `uv run alembic` path is not used inside the
container). The PG app password is read from the `0600` bootstrap file and
exported as `PGPASSWORD` for libpq — the same pattern the dev Makefile uses.

---

## Run it from zero

> All commands below use placeholder credentials. Substitute real values locally;
> never commit them.

### 1. Build and start

Set a unique image tag and the source commit for every release build, then start
the stack. The values are public build metadata, not credentials.

```bash
export DATAOPS_BUILD_VERSION=2.0.1
export DATAOPS_BUILD_COMMIT="$(git rev-parse HEAD)"
SHORT_COMMIT="$(git rev-parse --short=12 HEAD)"
export DATAOPS_IMAGE_VERSION="2.0.1-${SHORT_COMMIT}"
docker compose -f docker/compose.dataops.yml up -d --build
```

This builds the image, generates secrets, starts PG, runs migrations, and starts
API + worker. Watch progress:

```bash
docker compose -f docker/compose.dataops.yml ps
docker compose -f docker/compose.dataops.yml logs -f migrate   # should print "alembic upgrade head complete"
```

Health check:

```bash
curl -sS http://127.0.0.1:18020/healthz
# {"status":"ok"}

curl -sS http://127.0.0.1:18020/api/version
# {"version":"2.0.1","commit":"<git-sha>","image_version":"<image-tag>"}

API_IMAGE_ID="$(docker inspect dataops-v2-stack-api --format '{{.Image}}')"
WORKER_IMAGE_ID="$(docker inspect dataops-v2-stack-worker --format '{{.Image}}')"
test "$API_IMAGE_ID" = "$WORKER_IMAGE_ID"
```

The last assertion prevents a partial rollout in which the API serves new UI or
contracts while the worker still executes old SQL behavior. The same build
identity appears in the application header; hover it to see the image tag and
full commit.

### 2. Create the admin user

`admin-create` is in the `tools` profile, so it does not start with `up`. Run it
once, passing admin args after the service name:

```bash
docker compose -f docker/compose.dataops.yml run --rm admin-create \
  --username admin \
  --password 'CHANGE-ME-admin-password' \
  --project-name Default
```

It stores only a bcrypt hash in `users.password_hash`.

### 3. Login and grab the project id

```bash
TOKEN="$(
  curl -sS -X POST http://127.0.0.1:18020/api/auth/login \
    -H 'content-type: application/json' \
    -d '{"username":"admin","password":"CHANGE-ME-admin-password"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
)"

PROJECT_ID="$(
  curl -sS http://127.0.0.1:18020/api/projects \
    -H "authorization: Bearer $TOKEN" \
  | python -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])'
)"
export PROJECT_ID
```

### 4. Add a MySQL datasource

Point these at a MySQL the stack can reach. Note: `127.0.0.1` inside a container
is the container itself — use the host's LAN address or a DB reachable on the
compose network, not `127.0.0.1`.

```bash
export MYSQL_HOST=db.internal.example      # NOT 127.0.0.1 from inside a container
export MYSQL_PORT=3306
export MYSQL_USER=dataops
export MYSQL_PASSWORD='CHANGE-ME-mysql-password'
export MYSQL_DATABASE=dataops_demo

DATASOURCE_BODY="$(
  python - <<'PY'
import json, os
print(json.dumps({
    "project_id": os.environ["PROJECT_ID"],
    "name": "compose-mysql",
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
  curl -sS -X POST http://127.0.0.1:18020/api/datasources \
    -H "authorization: Bearer $TOKEN" \
    -H 'content-type: application/json' \
    -d "$DATASOURCE_BODY" \
  | python -c 'import json,sys; print(json.load(sys.stdin)["id"])'
)"

# Test through the worker:
curl -sS -X POST "http://127.0.0.1:18020/api/datasources/$DATASOURCE_ID/test" \
  -H "authorization: Bearer $TOKEN"
```

The password is encrypted by SecretStore (master key) before it touches PG —
the request body is the only place plaintext appears.

### 5. Run SELECT 1+1 through the worker

```bash
EXECUTION="$(
  curl -sS -X POST http://127.0.0.1:18020/api/sql/execute \
    -H "authorization: Bearer $TOKEN" \
    -H 'content-type: application/json' \
    -d "{\"datasource_id\":\"$DATASOURCE_ID\",\"sql\":\"SELECT 1+1 AS r\",\"params\":{}}"
)"
JOB_ID="$(printf '%s' "$EXECUTION" | python -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')"

for _ in 1 2 3 4 5 6 7 8 9 10; do
  STATUS="$(
    curl -sS "http://127.0.0.1:18020/api/jobs/$JOB_ID" \
      -H "authorization: Bearer $TOKEN" \
    | python -c 'import json,sys; print(json.load(sys.stdin)["status"])'
  )"
  test "$STATUS" = "success" && break
  sleep 1
done

curl -sS "http://127.0.0.1:18020/api/jobs/$JOB_ID/result?offset=0&limit=10" \
  -H "authorization: Bearer $TOKEN"
```

Expected: column `r`, first row value `2` — proving enqueue → worker claim →
adapter execute → spool → paged read all work in the compose form.

---

## Lifecycle

```bash
# Stop (graceful: worker drains its current job; up to stop_grace_period)
docker compose -f docker/compose.dataops.yml stop

# Start again (data persists in named volumes)
docker compose -f docker/compose.dataops.yml up -d

# Tear down containers, KEEP data
docker compose -f docker/compose.dataops.yml down

# Tear down AND delete all data volumes (DESTRUCTIVE — wipes PG + secrets + spool)
docker compose -f docker/compose.dataops.yml down -v
```

Named volumes:

- `dataops-v2-stack-config` — bootstrap secrets (regenerated only if deleted)
- `dataops-v2-stack-pgpw` — pg-readable copy of the app password
- `dataops-v2-stack-data` — ResultStore spool
- `dataops-v2-stack-pgdata` — PostgreSQL cluster

## DM 数据源注记 (worker DM 客户端加密库)

The walkthrough above uses MySQL. If you point a datasource at DM (达梦) instead,
the **worker** service needs two extra environment variables so the `dmpython`
driver can `dlopen` the DM client crypto modules. Without them, DM connect/execute
fails with `-70089` (encryption module load failure): the PyPI `dmpython` wheel
bundles a crypto library missing transitive dependencies, so the driver must use
the complete library set under a real DM client install. Root cause and the full
one-time fix are in [`acceptance-dm-certified.md`](../acceptance-dm-certified.md)
("环境注记").

This requires a DM client install reachable inside the `worker` container (mount
it as a volume or bake it into a derived image — the stock runtime image does not
ship DM client libraries). Then add the two env vars to the `worker` service, e.g.
via a compose override file (`docker/compose.dataops.dm.yml`) so you do not edit
the base compose YAML:

```yaml
# docker/compose.dataops.dm.yml — placeholder override, adjust paths to your mount
services:
  worker:
    environment:
      DM_HOME: /opt/dmdbms                                                   # DM client install inside the container
      LD_LIBRARY_PATH: /opt/dmdbms/bin:/opt/dmdbms/bin/external_crypto_libs
```

Apply it alongside the base file:

```bash
docker compose -f docker/compose.dataops.yml -f docker/compose.dataops.dm.yml up -d
```

## Notes / limits

- This stack does not bundle the frontend SPA. The API serves the JSON API only;
  the frontend is built and served separately (out of scope for §5 backend
  acceptance).
- TLS termination is out of scope here — front the API with a reverse proxy for
  production (see `docs/deployment/tls.md`).
- License: with no `config/license.lic` mounted, the API runs in trial mode
  (same as portable). To use a license, place it at `config/license.lic` inside
  the `config` volume before starting.
