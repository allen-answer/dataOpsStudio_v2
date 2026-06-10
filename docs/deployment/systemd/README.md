# systemd units for DataOpsStudio 2.0

These are **templates**. Every path / user is a placeholder you substitute on the
host. Per contract §5, no real server IP, account, key, or password ever goes
into these files or into git — only placeholders like `/path/to/dataops-runtime`
and `<DEPLOY_USER>`.

They replace the `tmux` / `nohup` supervision shown in
[`../upgrade-in-place.md`](../upgrade-in-place.md) §4 with a first-class process
supervisor, so an SSH disconnect no longer takes the instance down.

## Pick one layout

| Layout | Units to enable | When |
|---|---|---|
| **All-in-one** (default, portable / single host) | `dataops.service` only | `launcher up` supervises managed PG + API + worker in one process tree. This is the dogfood and small-deployment default. |
| **Split API/worker** | `dataops-worker.service` (+ your own API/PG units) | API and worker on separate hosts, or a scaled worker fleet. The launcher does not split these for you in 2.0.0, so you run `python -m app.worker` as its own unit. |

> ⚠️ Do **not** enable both `dataops.service` and `dataops-worker.service` on the
> same host: `up` already starts a worker, so you would run two workers.

## Install (all-in-one)

```bash
sudo cp dataops.service /etc/systemd/system/dataops.service
sudoedit /etc/systemd/system/dataops.service   # replace every placeholder
sudo systemctl daemon-reload
sudo systemctl enable --now dataops.service
sudo systemctl status dataops.service
```

## Stop semantics (graceful by default)

`systemctl stop dataops` sends **SIGTERM**, which is the **graceful** path — the
same as `launcher stop` (not `stop --force`). The worker stops accepting new work
and finishes its in-flight job before exiting. This is why both units set a large
`TimeoutStopSec` (900s): a long OLAP query must be allowed to drain rather than be
SIGKILLed. After a forced kill, the JobBackend reaper only recovers the stale
`running` job once `heartbeat_timeout_seconds` elapses (default 600s, see
ADR-0018) — so prefer graceful stop.

Full signal/stop discussion: [`../quickstart.md`](../quickstart.md) §5 and
[`../upgrade-in-place.md`](../upgrade-in-place.md) §2.

## Logs

Both units send stdout/stderr to **journald** (`StandardOutput=journal`). Do not
additionally `tee` to a file — journald is the single sink and it rotates on its
own. See the "Log rotation (journald)" section in
[`../quickstart.md`](../quickstart.md) for quota recommendations and how to read
the logs (`journalctl -u dataops -f`).

## AI provider keys (if you enable AI)

AI is off by default (design §2.7.6). If you enable a provider, the API key must
come from the environment / a systemd credential at runtime — never inlined in
the unit and never committed (R8). See the commented `LoadCredential` example in
`dataops.service` and [`../ai-gateway.md`](../ai-gateway.md).
