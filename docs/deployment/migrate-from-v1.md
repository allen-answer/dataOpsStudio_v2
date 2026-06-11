# DataOpsStudio 1.x → 2.0 Data Migration

This runbook covers migrating data from a **1.x instance** into a fresh **2.0
PostgreSQL** metadata database, using `tools/migrate_from_v1.py`.

DataOpsStudio 2.0 makes **no backward-compatibility promise** (design §5.4.3):
the API paths, data schema (SQLite → PG), config format, secret storage, and
workflow node model all changed. There is no in-place 1.x→2.0 upgrade; you must
follow this official migration path. (For upgrading a **2.0** instance to a newer
2.0.x, use [`upgrade-in-place.md`](./upgrade-in-place.md) instead.)

> Secrets discipline: never paste the server IP, SSH key, account, PG
> credentials, or any 1.x datasource password into commit messages, scripts, or
> shared logs. The migration log is scrubbed (R5), but you must not echo
> credentials yourself.

## What the tool migrates

Only tables that exist in the 2.0.0 skeleton schema (`app/db/models.py`) are
migrated. 1.x data whose 2.0 target table is a 2.1+ feature (compare tasks,
workflows, scenarios, sql templates, AI configs, asset aspects, refresh/revoked
tokens, slow-sql plans, run index, …) is **not** migrated — the tool prints a
"skipped + reason" line for each and does **not** create any 2.1+ tables.

| 1.x source | 2.0 PG target | Notes |
|---|---|---|
| `config/users.json` | `users` | `password_hash` migrated as-is; `mfa_secret_encrypted` (Fernet) decrypted with the 1.x key and re-encrypted into the 2.0 SecretStore (`mfa_secret_ref`). Recovery codes have no 2.0 column → skipped with a warning. |
| `config/projects.json` | `projects` + `project_members` | `members` (User.id array) is split into the `project_members` join table; the owner gets the `owner` role. |
| `config/datasources.json` | `datasources` | Plaintext `password` → 2.0 SecretStore → `password_secret_ref`; `extra` → `capability_profile.connection`; `db_type` lower-cased. A 1.x datasource with an **empty `project_id`** (globally visible) cannot be inserted (2.0 `project_id` is a NOT NULL FK) — by default it is skipped; pass `--global-datasource-project` to migrate it into a dedicated project instead (see [Global datasources](#global-datasources)). |
| SQLite `jobs` | `jobs` | `running` / `cancelling` are marked `failed` on import. Rows whose `owner_user_id` / `project_id` cannot be resolved from the payload are skipped (those columns are NOT NULL FKs in 2.0). |
| SQLite `audit_logs` | `audit_logs` | `method`+`path` → `action`; HTTP status → `result` (`401/403` → `denied`, `>=400` → `error`). |

`audit_logs` and `jobs` are read **only** from the 1.x SQLite database
(`data/dataops.db`); the tool does **not** re-read `logs/audit.jsonl` or
`config/jobs.json` (1.x already migrated those into SQLite at startup, so
re-reading would double-count).

The 1.x `config/.dataops_secret.key` is a **migration input** (used to decrypt
old Fernet fields), not a migration target.

## Global datasources

In 1.x a datasource could be **globally visible** — its `project_id` was empty,
so any project could see it. 2.0 requires every datasource to belong to a
project (`datasources.project_id` is a NOT NULL foreign key). A 1.x global
datasource therefore has no project to land in.

By default the tool **skips** every global datasource and records a "skipped +
reason" row in the datasources report. (Real 1.x instances are commonly
*all* global — e.g. 4/4 datasources — so without the flag below none of them
migrate.)

To migrate them, pass `--global-datasource-project`: the tool creates (or, if a
project of that name was already migrated, reuses) a dedicated project to hold
all 1.x global datasources, and attaches them there.

| Flag | Default | Meaning |
|---|---|---|
| `--global-datasource-project <name>` | *(unset)* | Create/reuse a project named `<name>` and attach all 1.x global datasources to it. Unset = the legacy behaviour (skip + report). |
| `--global-datasource-owner <username>` | `admin` | The owner of that project, resolved by **username** against the **already-migrated** users. If it does not resolve, the tool exits with an error that lists the available usernames (it does **not** silently fall back). |

Behaviour details:

- The project is created **only if at least one global datasource actually
  exists** — no flag, no global datasources, no synthetic project.
- The new project gets a fresh UUID id and the description
  `migrated: 1.x global datasources`. It is counted as `+1` migrated in the
  `projects` report and logged on its own line (synthetic project created +
  number of datasources attached).
- The owner is also added to `project_members` with the `owner` role, exactly
  like a normally-migrated project.
- A 1.x datasource that points at a **named** project which itself was not
  migrated is still skipped — this flag only covers the empty-`project_id`
  (global) case.

## Fault tolerance

Per the contract, "individual field failures are allowed":

- A single field that fails (e.g. an MFA secret that won't decrypt) is logged as
  a **warning** and migration continues — the error is recorded, not swallowed.
- A row that cannot satisfy a 2.0 NOT NULL / FK constraint is **skipped with a
  reason** — no half-written row is inserted.
- If the migration aborts, already-written data is retained but the run is marked
  **incomplete** and the tool exits non-zero, so a half-finished migration is
  never mistaken for success.

## Migration procedure (design §5.4.2)

1. **Stop the 1.x instance.** No writes during migration.
2. **Back up all 1.x data** — `config/` + `results/` + `data/` + `logs/` +
   `config/.dataops_secret.key`. Migration is not allowed without a backup.
3. **Bring up an empty 2.0 instance** (any form; PostgreSQL already provisioned
   and `alembic upgrade head` applied). See [`quickstart.md`](./quickstart.md).
4. **Generate a new 2.0 master key** — `launcher bootstrap init` creates
   `config/.secret_master.key`. This is the key the migration re-encrypts secrets
   with.
5. **Run the migration:**

   ```bash
   python -m tools.migrate_from_v1 \
       --source <v1_instance_dir> \
       --target <2.0_pg_dsn> \
       --v1-secret-key <v1_dir>/config/.dataops_secret.key \
       --master-key-file <2.0_config>/.secret_master.key
   ```

   - `--source` — the 1.x instance root (containing `config/` and `data/`).
   - `--target` — the 2.0 PostgreSQL DSN (SQLAlchemy URL, e.g.
     `postgresql+psycopg://user@host:5432/dataops`).
   - `--v1-secret-key` — the 1.x Fernet key, used **only** to decrypt old
     encrypted fields.
   - `--master-key-file` — the 2.0 master key from step 4, used to re-encrypt
     secrets into the 2.0 SecretStore.
   - `--global-datasource-project` *(optional)* — migrate 1.x global datasources
     (empty `project_id`) into a dedicated project of this name instead of
     skipping them. See [Global datasources](#global-datasources).
   - `--global-datasource-owner` *(optional, default `admin`)* — the username
     that owns the project above; must match an already-migrated user.

   If your 1.x instance has global datasources (check the datasources report or
   `config/datasources.json` for empty `project_id`), add the flags to land them
   in a dedicated project:

   ```bash
   python -m tools.migrate_from_v1 \
       --source <v1_instance_dir> \
       --target <2.0_pg_dsn> \
       --v1-secret-key <v1_dir>/config/.dataops_secret.key \
       --master-key-file <2.0_config>/.secret_master.key \
       --global-datasource-project "Legacy Global Datasources" \
       --global-datasource-owner admin
   ```

   The tool prints a per-table report (migrated / skipped counts, field
   warnings) and a list of skipped sources with reasons. A non-zero exit code
   means the run was incomplete — do **not** put the instance into service.

6. **Verify:**
   - Imported row counts match 1.x.
   - A sampled datasource `test_connection` succeeds (password decrypted
     correctly).
   - Historical jobs/audit entries are visible.
   - (When the relevant 2.1+ features ship: sampled AI provider ping, historical
     runs viewable, all workflows load with no forbidden nodes.)
7. **Cut over** to the 2.0 instance.
8. **Keep the 1.x instance for 30 days** as a rollback safety net.
9. **After 30 days, physically destroy** `config/datasources.json` and the rest
   of the 1.x secret material.
