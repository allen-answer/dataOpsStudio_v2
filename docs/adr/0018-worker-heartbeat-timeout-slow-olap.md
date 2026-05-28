# ADR 0018: Worker Heartbeat Timeout Favors Slow OLAP Queries

Status: Accepted
Date: 2026-05-29

## Context

The 2.0.0 worker executes SQL jobs by claiming a PG queue row, running the adapter, streaming rows into spool, and heartbeating at safe points. It does not run a separate heartbeat thread yet.

Some legitimate OLAP queries can spend more than 90 seconds before returning the first row, especially large joins and aggregations. During that first-row wait the worker may not reach a heartbeat safe point. A 90 second stale-worker reaper threshold can therefore falsely fail or requeue a valid query.

## Decision

Set the default `worker_heartbeat_timeout` to 600 seconds for 2.0.0.

The product tradeoff is explicit: false-killing a legitimate slow query is worse than waiting several more minutes to recover a truly dead worker. Operators may still lower the value in environments where short query latency is guaranteed.

2.0.0 keeps the worker implementation simple and does not add an independent heartbeat thread. That thread remains a later improvement once worker lifecycle and cancellation behavior settle.

## Consequences

- A dead worker may remain `running` for up to about 10 minutes before reaper recovery.
- Slow first-row OLAP queries are much less likely to be misclassified as stale.
- Reaper correctness still depends on SQL ownership checks: terminal updates must include `worker_id` and `status='running'`.

## Backlog

- T4前必修: throttle `is_cancel_requested` during SQL streaming to every 5000 rows or cache it for a short interval, avoiding one PG query per result row.
- Split adapter-factory unsupported DB type errors from `UnsupportedJobKindError` into a dedicated `UnsupportedDbTypeError`.
