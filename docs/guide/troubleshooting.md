# When something goes wrong

Indexed by what you saw, not by what caused it.

## The run stopped before anything was profiled (exit `4`)

| Symptom | Usually |
|---|---|
| Nothing profiled, no print written | The connection. Credentials resolve from `DBPRINT_<CONN>_<KEY>`, then `~/.dbprint/connections.yaml`, then a project-root `.env` — first hit per key, so an old environment variable silently outranks a corrected file. |
| `<driver> is not installed. Install dbprint with the [<extra>] extra: ...` | The driver import is lazy, so a base install never pays for a driver it does not use — install the extra your engine's adapter page names. |
| `could not open duckdb database ...` / `could not connect to ClickHouse at ...` / `could not connect to Redshift at ...` / `could not open Databricks session for ...` / `could not open a BigQuery session for project ...` | The cause in parentheses is the driver's own error. On PostgreSQL this is also what a missing `pg_dump` looks like: the binary is probed before the first query. |
| `invalid port '<value>': ...` | A non-integer `port` — PostgreSQL, MySQL, ClickHouse and Redshift validate it this early; the rest take no `port` key at all. |
| `pg_dump: error: server version mismatch` | The client is older than the server. `pg_dump` refuses to dump from a newer server, so every table's DDL fails while statistics succeed. Install a `pg_dump` at least the server's major version. |

## The run stopped with exit `1` and one `error:` line — the whole run, not one table

| Symptom | Usually |
|---|---|
| `ERROR: Table identifier rejected: <fqn>` / `Reason: contains-unsafe-character` | One object's name fails SPEC 1.5's path-segment rule — a space or other unsafe character on any engine, or a capital letter specifically on ClickHouse, which cannot fold identifiers. The message's own `Resolution` block gives the `exclude:` snippet to drop that object from scope. |
| `... Reason: case-collides-with-<other>` | Two objects differ only by case and would overwrite each other's artifact path once folded to lowercase — every engine except ClickHouse detects this; ClickHouse does not fold case at all, so it has no collision case. Exclude one of the two objects. |
| `<schema>.<table>: unrecognised table_type '<value>' - not one of the eight Databricks documents (...)` | A table type newer than the adapter knows. Surfaces loudly rather than silently dropping the object. |

## The run stopped with exit `7`, every table failed

The account reached the database but can do nothing with it. Check the grants on your engine's adapter page — [duckdb](../adapters/duckdb.md), [PostgreSQL](../adapters/postgres.md), [MySQL](../adapters/mysql.md), [ClickHouse](../adapters/clickhouse.md), [Redshift](../adapters/redshift.md), [Snowflake](../adapters/snowflake.md), [Databricks](../adapters/databricks.md), [BigQuery](../adapters/bigquery.md).

## One table failed, others written (exit `5`)

| Symptom | Usually |
|---|---|
| A per-table privilege failure, grouped on stderr with the operation that raised it | Most often one schema's grant. The tables that succeeded are already on disk and valid. |
| `... sets materialize_sample: false, and this adapter's per-statement sampling construct cannot be seeded into agreement across statements. Set materialize_sample: true for this connection, or narrow with a filter instead of a sample fraction.` | Hits ClickHouse, Redshift, BigQuery, MySQL and Snowflake — the five engines whose per-statement sampling cannot be trusted to agree across statements without the materialized copy. The remedy is in the message. |
| `... could not materialize its sample of <p> (<cause>), and this adapter's per-statement fallback cannot be seeded into agreement across statements. Narrow with a filter instead of a sample fraction.` | Same five engines, this time the write itself was refused. On ClickHouse the `<cause>` is usually the table declaring no `SAMPLE BY` key — a catalog fact caught before any `CREATE` is issued. On BigQuery the cause is a refused `CREATE OR REPLACE TABLE` in the profiled dataset. |
| `no DDL available for '<fqn>'; not found in catalog` | Fails that one table's DDL extraction; statistics for it may still succeed. |

## The output is not what you expected

| Symptom | Cause |
|---|---|
| A table shows `- rows` | It is a view. No query is ever issued against one; its `statistics.yaml` comes from the catalog and says `catalog_only`. |
| `generate` reports skips and writes nothing new | Every table is still inside its `max_age_days` window, seven days by default. `--force` re-profiles regardless. This is also why a newly created annotation file stays invisible — see [annotating a print](annotations.md). |
| Ratios look wrong for the table size | The table was narrowed. Every ratio in a file carrying a `scope` block is denominated in `rows_scanned`, not `row_count` — see [choosing what to profile](scoping.md). |
| A column has no sketch | Sketches are skipped entirely for a narrowed table and for a view. |
| `check` exits non-zero with nothing obviously wrong on screen | Warnings are reported as a count, not by code. Take `--format json` and filter on severity — see [gating CI](ci.md). |
| A config key seems to do nothing | An unrecognised or mis-nested key is dropped silently. Compare against the nesting in [Configuration](../CONFIG.md); the common error is a `rules` entry indented under the wrong parent. |
| A print is silently short of tables on Databricks | Unity Catalog's `information_schema` is privilege-filtered, not privilege-gated — an under-privileged principal gets fewer rows, not an error. Check the table count against what you expect; see the [Databricks page](../adapters/databricks.md). |
| Snowflake reports `0 objects` and exits cleanly | Both catalog surfaces the adapter reads are filtered to what the role can see, so a missing `SELECT` produces an empty result rather than an error. Check the role's grants before concluding the selectors are wrong; see the [Snowflake page](../adapters/snowflake.md). |

## The run succeeded but the artifact is thinner than expected — all warnings, nothing fails

| Symptom | What is lost |
|---|---|
| `view-dependency catalog read failed for connection '<name>': <cause>; every view/matview omits depends_on this run` | `depends_on` on every view for that connection this run. On Snowflake the cause is usually the missing `snowflake.account_usage` grant — see the [Snowflake page](../adapters/snowflake.md). |
| No message at all | duckdb, ClickHouse, Databricks and BigQuery never carry `depends_on` on a view, by design — nothing warns, because there is nothing to compare against. |
| `no row-count estimate for '<fqn>'; rules carrying \`min_rows\` or a \`max_rows_scanned\` ceiling do not apply to it` | Every size-gated rule silently does not apply to that table. On Redshift the usual cause is a missing grant on `svv_table_info` — see the [Redshift page](../adapters/redshift.md). On Databricks it is a refused `DESCRIBE TABLE EXTENDED ... AS JSON`. |
| `table '<fqn>': could not drop the materialized sample '<name>' (<cause>); it outlives this run and is not session-scoped` | BigQuery only. The copy is a real table in the profiled dataset; its 6-hour expiration is what bounds it when the drop itself fails. Every other engine's variant of this warning ends "the session drops it". |

## Finding out more

`--debug` on any command replaces the one-line error with a traceback. Without it, an unexpected failure prints only its message.

Every run also writes a log to `~/.dbprint/logs/<project-slug>/`, named for the time it started and the command it ran, keeping the last three per project. It records the dbprint version, the full command line, the config file, the connections, and every statement issued with its parameters and row count — which is usually enough to see which query failed and against what.

That log contains the SQL your project ran and the host, database and account it ran against. Read it before attaching it to anything.
