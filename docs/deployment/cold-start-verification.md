# Cold-start Verification Report

This report records the independent T6 cold-start verification. It intentionally
omits cloud server IPs, SSH users, key paths, tokens, passwords, and any other
deployment credential.

## Scope

- Target: cloud server, clean runtime root
- Runtime: non-root deployment user
- Metadata DB: launcher-managed PostgreSQL
- Business DB: temporary MySQL service
- Flow: `doctor` → `bootstrap init` → `pg-up` → `alembic-up` → `admin create`
  → `up` → login → create datasource → test datasource → `SELECT 1+1 AS r`

## Result

Cold start completed successfully. The final result contained column `r` and row
value `2`.

```text
STEP1_DOCTOR
OK      Python 3.12.x
OK      uv
OK      PostgreSQL initdb
OK      PostgreSQL pg_ctl
OK      PostgreSQL postgres
OK      config dir writable
OK      data dir writable
OK      log dir writable

STEP2_BOOTSTRAP
bootstrap secrets ready: created=4, kept=0
Bootstrap secret files are chmod 0600.

STEP3_PG_UP
server started
Created PG role: dataops
Created PG database: dataops
PG ready

STEP4_ALEMBIC
Running upgrade  -> 0001_initial
Running upgrade 0001_initial -> 0002_datasources_database_name
alembic upgrade head complete

STEP5_ADMIN
admin user created

STEP6_UP
launcher: running
api: running
worker: running
PG status: ready

STEP7_LOGIN
{"access_token": "<redacted>", "token_type": "bearer"}

STEP7_CREATE_DATASOURCE
{"db_type":"mysql","database":"dataops_demo",...}

STEP7_TEST_DATASOURCE
{"ok":true}

STEP8_EXECUTE_SQL
{"job_id":"<redacted>","result_set_id":"<redacted>"}
poll_01={"status":"success",...}
result={"columns":[{"name":"r",...}],"rows":[{"values":[2]}],"loaded_rows":1,"truncated":false}
assertion=columns[0].name == r and rows[0].values[0] == 2
```

## Findings Folded Into T6

| ID | Finding | Fix |
|---|---|---|
| F1 | `uv sync --all-groups` was missing from prerequisites. | Quickstart now tells users to run it after clone/unpack. |
| F2 | `doctor` could report `MISSING uv` in non-login shells even when launcher was started by `uv`. | `doctor` now checks `PATH`, `~/.local/bin`, and `~/.cargo/bin`, and prints a troubleshooting hint. |
| F3 | `pg_ctl start` failures only showed a generic command error. | Launcher now prints the last lines of `logs/pg.log` on start failure. |
| F4 | `doctor` did not preflight PG port conflicts. | `doctor` now checks whether `DATAOPS_PG_PORT` is already accepting TCP connections. |
| F5 | `DATAOPS_PG_PORT` escape hatch was not documented. | Quickstart troubleshooting documents changing the port. |
| F6 | `alembic-up` looked mandatory even though `up` also migrates. | Quickstart marks the migration step as optional. |

The verification also confirmed worker logging is no longer silent: `worker.log`
contains `worker starting`, `worker job claimed`, `worker job start`, and
`worker job complete` JSON events.
