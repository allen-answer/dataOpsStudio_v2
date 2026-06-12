# ADR 0020: Persist Lineage Edges With SQL Hash Cache

Status: Accepted
Date: 2026-06-12

## Context

The v0.3.2 design inherited a 1.x behavior: lineage parse results were not persisted as primary metadata and could be recomputed whenever needed. That behavior was acceptable for 1.x because lineage was mostly a run/export artifact and the graph was small enough for occasional full recomputation.

The v0.3.3 lineage design changes the product shape. The first-class query is now a focused N-hop subgraph, and 2.4.0 also includes basic downstream impact analysis. Both features need an adjacency store that can be queried repeatedly and incrementally. Recomputing every SQL script on every graph request would make the subgraph design slow and brittle.

sqlglot parsing is usually fast per statement, but at 1-10 ms per statement, thousand-statement script sets become seconds to tens of seconds under synchronous recomputation, and ten-thousand-statement sets can exceed interactive time budgets. The product needs cacheable parsed edges.

## Decision

Starting in 2.4.0, persist deterministic lineage edges in PostgreSQL tables.

The persisted shape should include table-level edges and column-level edges, for example:

- `lineage_runs`: parse run metadata, including project, datasource, dialect, source reference, `sql_hash`, status, and parse summary
- `lineage_edges`: table-level source to target edges
- `lineage_column_edges`: column-level source to target edges, including transformation classification

Use `sql_hash` as the cache key. A statement or script is reparsed only when its hash or relevant parser context changes. `sql_hash` is a non-secret fingerprint for SQL identity and is not a secret or cryptographic credential.

Focused subgraph queries should read the persisted edge tables through PostgreSQL recursive CTEs, with a maximum depth of 5 and visited-node de-duplication to avoid cycles. This persistence decision is a prerequisite for ADR 0019's subgraph-first graph query model.

The `LineageReport` 20-field envelope remains the API and export shape. It is no longer the primary storage model for queryable lineage. The envelope can be assembled from persisted edges, parse summaries, and ResultStore artifacts.

## Consequences

- Subgraph queries and basic impact analysis can run without reparsing every source script.
- PostgreSQL becomes the lineage graph query substrate for 2.4.0; no graph database is required.
- Parser invalidation must account for `sql_hash`, dialect, datasource/schema context, and parser version where needed.
- `migrate_from_v1.py` still does not migrate 1.x lineage parse results, because 1.x did not persist them as source-of-truth metadata. 2.0-generated lineage edges are persisted from 2.4.0 onward.
- Storage and retention policy must treat AI-inferred edges differently from deterministic edges.

## Non-Goals

- This ADR does not change the `LineageReport` envelope fields.
- This ADR does not make AI inferred edges equivalent to deterministic edges. AI inferred edges must carry state such as `inferred`, `confirmed`, or `rejected`.
- This ADR does not require a graph database.
- This ADR does not require migrating historical 1.x lineage artifacts into edge tables.
