# ADR 0019: Lineage Subgraph First

Status: Accepted
Date: 2026-06-12

## Context

DataOpsStudio 1.x lineage can produce rich `LineageReport` output, but daily use exposed a product and performance problem: large lineage graphs are slow to compute, hard to read, and often require users to manually find the one table they care about inside a full graph.

The v0.3.2 design already decided to reuse the 1.x `LineageReport` 20-field envelope and not persist lineage parse results as primary metadata tables. This ADR does not change that decision. It only defines the graph query and UI loading strategy for the 2.4.0 lineage capability.

The user-facing task is usually local: "what is upstream/downstream of this table or column?" A whole-graph view is useful for exploration, but it should not be the default execution path.

## Decision

Make "focused N-hop neighborhood subgraph" the first-class lineage graph query.

The 2.4.0 lineage graph engine must support querying from a focus table or column with:

- direction: upstream, downstream, or both
- max depth, defaulting to no more than 3 hops
- optional column-level expansion
- optional inclusion of AI inferred edges

The engine must compute and load the requested subgraph on demand. Implementations must not compute or render the whole graph and then filter it down to the focus neighborhood.

The whole-graph view is a secondary view. It may exist, but it must be lazy-loaded, visibly marked as potentially large, and use clustering or layering so it remains inspectable.

Acceptance target: focused subgraph queries with max depth <= 3 should have P95 latency under 2 seconds on the supported 2.4.0 scale target.

## Consequences

- Lineage UI defaults to a readable local graph instead of an overwhelming global graph.
- Backend APIs and indexes should be shaped around adjacency traversal, depth limits, truncation markers, and node/edge counts.
- Basic impact analysis can ship in 2.4.0 as pure graph traversal over the same subgraph primitives.
- Whole-graph rendering becomes a deliberate user action, not an accidental default.
- The existing `LineageReport` envelope remains intact; graph query strategy does not require changing or persisting lineage parse results as primary metadata tables.

## Non-Goals

- This ADR does not introduce a graph database requirement.
- This ADR does not move AI weighted impact analysis into 2.4.0. That remains a 2.7.0 enhancement on top of basic impact analysis.
- This ADR does not remove column-level lineage from 2.4.0. Column-level lineage remains a first-release requirement.
