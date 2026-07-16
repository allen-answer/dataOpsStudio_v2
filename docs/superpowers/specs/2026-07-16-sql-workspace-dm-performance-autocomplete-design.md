# SQL Workspace DM Query Responsiveness and Autocomplete Design

Date: 2026-07-16

## Goal

Improve the SQL workspace without changing query semantics:

- DM queries must no longer make the browser unresponsive while results are still loading.
- SQL syntax highlighting must remain available in Monaco.
- Typing a schema name followed by `.` must offer table-name completions from the selected datasource.

The worker must continue executing the full read-only query and spooling the complete result. The UI must not rewrite user SQL, inject a `LIMIT`, or silently truncate the result.

## Current Evidence and Root Cause

The DM adapter already streams query rows with `fetchmany(1000)`. The worker writes each 1,000-row batch to ResultStore and exposes progressive results. There is no evidence that the normal SQL query path materializes the whole DM result in memory.

The browser load is caused by the current polling/rendering combination:

- `SqlWorkspaceView.vue` polls every 500 ms.
- Every poll requests both job state and up to 1,000 result rows.
- `ResultTable.vue` renders those rows as a normal DOM table.
- A 1,000-row result with many columns produces thousands of cells, and the table is replaced repeatedly while the query is running.

This creates repeated response decoding, Vue reconciliation, cell formatting, layout, and paint work on the browser main thread. The issue is most visible for DM queries that return large or wide result sets, even though the backend is streaming correctly.

Monaco already loads the SQL language contribution and the editor uses `language="sql"`, so SQL tokenization/highlighting exists. However, `onEditorMount` currently registers only the execute keyboard shortcut; it does not register a completion provider.

## Selected Approach

Use a surgical frontend change and reuse existing metadata APIs.

### Query responsiveness

- Change the SQL result page size from 1,000 to 100 rows.
- Change the active query polling interval from 500 ms to 1,000 ms.
- Keep progressive preview while a query is running.
- Keep fetching only the currently selected page.
- Stop polling on terminal job states as today.
- Preserve the complete ResultStore spool, result totals, pagination, and export behavior.

This bounds each result response and the visible table to at most 100 rows without changing backend contracts or query semantics.

### SQL syntax highlighting

Keep the existing Monaco SQL contribution and `language="sql"` configuration. Add an E2E assertion that rendered SQL contains tokenized spans so future editor changes cannot silently remove highlighting.

### Schema-qualified table autocomplete

Register one Monaco completion provider for SQL when the workspace component mounts.

The provider will:

1. Run when `.` is typed or completion is explicitly requested.
2. Inspect only the text before the cursor on the current line.
3. Recognize an immediately preceding schema identifier, including ordinary DM-style identifiers and double-quoted identifiers.
4. Capture the currently selected datasource ID before starting an async metadata request.
5. Load tables through the existing `listMetadataTables(datasourceId, schema, false)` API.
6. Return table-name completion items whose replacement range starts at the current table-name fragment.
7. Discard results if the selected datasource changed while the request was in flight.
8. Return no suggestions when there is no datasource, no schema prefix, an unsupported datasource, or a metadata request failure.

Metadata failures must not block typing or overwrite the existing metadata-browser error state.

## Cache and Lifecycle

- Cache table lists by `datasourceId + schema` for the lifetime of the workspace component.
- Reuse an in-flight request for the same cache key so rapid completion triggers do not duplicate probes.
- Clear the relevant cache when metadata is explicitly refreshed.
- Keep datasource IDs in cache keys so switching datasources cannot mix suggestions.
- Dispose the Monaco completion provider when the component unmounts to prevent duplicate providers after route changes.

## Error Handling and Safety

- Autocomplete metadata errors degrade to an empty suggestion list.
- Existing metadata-browser refresh and error UI remain unchanged.
- Query errors, cancellation, timeout, pagination, and export behavior remain unchanged.
- No SQL text, result rows, datasource credentials, API keys, tokens, or personal data are logged by the new code or tests.
- Browser verification reports only timings, counts, status codes, and sanitized Console errors.

## Tests

Add focused Playwright regressions before production changes:

1. A running SQL query records result-request URLs and asserts `limit=100`.
2. A large progressive result response never renders more than 100 body rows.
3. The query reaches its terminal state and polling stops.
4. Typing `app.` in Monaco calls the existing tables metadata endpoint and shows the mocked table completion.
5. Switching datasource during an in-flight completion request does not show stale tables.
6. SQL keywords and identifiers are rendered with Monaco token classes, proving SQL highlighting remains enabled.
7. Console error capture remains empty.

Run the related SQL workspace E2E first, then the complete frontend E2E suite, frontend typecheck/build, DM adapter tests, SQL workspace backend tests, diff review, and secret scan.

## Acceptance Evidence

After deployment to the D drive runtime:

- `/healthz` succeeds and API/worker/launcher processes are healthy.
- A real DM query shows result requests bounded to 100 rows and the page remains interactive while the worker continues spooling.
- Pagination can reach later rows without rerunning or truncating the query.
- Typing a real schema followed by `.` shows tables from that schema.
- SQL keywords are visibly highlighted.
- The SQL workspace has no red Console errors or unexpected Network 4xx/5xx responses.

If no authenticated browser session or DM datasource is available, that missing real-environment verification must be reported explicitly and must not be replaced with a mock-based completion claim.

## Out of Scope

- Backend result-progress API changes.
- Result-table virtualization.
- Automatic SQL `LIMIT` rewriting.
- Query cancellation redesign.
- Metadata schema or database migration changes.
- Dependency upgrades or unrelated refactoring.
