# Compare Normalization Spec

Status: Accepted for 2.2.0-W1 Compare kernel.

This spec defines the deterministic row identity used by recursive hashdiff. It is a
non-secret fingerprint use of MD5/hashlib/DB-side MD5, not credential storage or crypto
protection.

## Row Payload

For a compare segment, each side builds the same normalized row payload:

1. Emit a NULL bitmap bit for every compared column, in compare column order.
2. Replace normalized SQL NULL values with the configured NULL sentinel.
3. Emit each normalized value as `character_length`, then the value, using the same
   column order.
4. Join bitmap bits, lengths and values with the configured field separator.

String columns apply trim, case-folding and empty-as-null rules before sentinel
replacement. Numeric columns use fixed decimal scale. Date/time columns use configured
timestamp precision. MySQL expressions specify charset/collation per expression; DM uses
`TO_CHAR` for text normalization and must degrade to client row hashing unless the
requested normalization charset is UTF-8 compatible.

The length prefix is part of the canonical payload so values that contain the separator or
the sentinel cannot collide with different column boundaries.

## Aggregate Hash

The database-side aggregate hash is:

1. Normalize one row payload.
2. Compute MD5 over the payload.
3. Take the first 8 bytes of the MD5 digest as an unsigned integer.
4. Sum the row integers for the segment with exact decimal/integer arithmetic.

MySQL expresses step 3 with `CONV(..., 16, 10)` cast to `DECIMAL(20,0)`. DM must avoid
floating conversion for the 64-bit value: split the 16 hex characters into high and low
32-bit chunks, expand each chunk with integer weights, then compute
`high * 4294967296 + low` as `DECIMAL(20,0)`. Segment aggregation must promote row hashes
to a wider exact decimal, such as `DECIMAL(38,0)`, before `SUM`.

The aggregate result is a prefilter only. Equal aggregate hashes let the engine skip a
segment; mismatches recurse or switch to row-level comparison. Final truth remains the
row-level bucket comparison.

Do not use engine-private or weak cross-database hashes as compare truth:
`ORA_HASH`, `CRC32` + `BIT_XOR`, `CHECKSUM_AGG`, or equivalent shortcuts are forbidden for
cross-database Compare.

## Recursive Hashdiff

For PK ranges, the engine splits the root range by `bisection_factor` between 8 and 32.
Each segment compares row count plus aggregate hash. Equal segments are skipped. Mismatched
segments recurse until `bisection_threshold` or `max_bisection_depth`, then row-level fetch
is used.

Default `bisection_threshold` is about 16K rows. Sparse differences should approach one
top-level segment scan plus recursive probes instead of a full-table row scan.

## Degradation

Adapters must explicitly return client row hashing when DB-side normalization or hashing is
not expressible. The current kernel degrades for unknown, JSON, and binary column types,
unsafe MySQL charset/collation names, and non-UTF-8 DM normalization charsets. Future
adapters may add more reasons, but must not silently use engine defaults.

## DM PoC Gate

DM uses `DBMS_CRYPTO.HASH(..., DBMS_CRYPTO.HASH_MD5)` for DB-side MD5. Before promoting
large DM compare runs, run a medium-table performance PoC around 100K rows. If this is too
slow for the target deployment, DM must run the client-side streaming row hash fallback for
that datasource profile.
