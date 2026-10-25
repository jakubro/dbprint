# When something goes wrong

Indexed by what you saw, not by what caused it.

## The run stopped before anything was profiled (exit `4`)

| Symptom | Usually |
|---|---|
| Nothing profiled, no print written | The connection. Credentials resolve from `DBPRINT_<CONNECTION>_<KEY>`, then `~/.dbprint/connections.yaml`, then a project-root `.env` — first hit per key, so an old environment variable silently outranks a corrected file. |
| `<driver> is not installed. Install dbprint with the [<extra>] extra: ...` | The driver import is lazy, so a base install never pays for a driver it does not use — install the extra your engine's adapter page names. |
| `could not open duckdb database ...` / `could not connect to ClickHouse at ...` / `could not connect to Redshift at ...` / `could not open Databricks session for ...` / `could not open a BigQuery session for project ...` | The cause in parentheses is the driver's own error. On PostgreSQL this is also what a missing `pg_dump` looks like: the binary is probed before the first query. |
| `invalid port '<value>': ...` | A non-integer `port` — PostgreSQL, MySQL, ClickHouse and Redshift validate it this early; the rest take no `port` key at all. |
| `pg_dump: error: server version mismatch` | The client is older than the server. `pg_dump` refuses to dump from a newer server, so every table's DDL fails while statistics succeed. Install a `pg_dump` at least the server's major version. |

## The run stopped with exit `1` and one `error:` line — the whole run, not one table

| Symptom | Usually |
|---|---|
| `ERROR: Table identifier rejected: <table>` / `Reason: contains-unsafe-character` | One object's name fails SPEC 1.5's path-segment rule — a space or other unsafe character on any engine, or a capital letter specifically on ClickHouse, which cannot fold identifiers. The message's own `Resolution` block gives the `exclude:` snippet to drop that object from scope. |
| `... Reason: case-collides-with-<other>` | Two objects differ only by case and would overwrite each other's artifact path once folded to lowercase — every engine except ClickHouse detects this; ClickHouse does not fold case at all, so it has no collision case. Exclude one of the two objects. |
| `<schema>.<table>: unrecognised table_type '<value>' - not one of the eight Databricks documents (...)` | A table type newer than the adapter knows. Surfaces loudly rather than silently dropping the object. |
| `<rule>.profile.read.tables.mode: <mode> is not valid for external table '<table>' on <adapter> - reading it reaches another system` | A rule asks to read an external table whose rows live in another live database (PostgreSQL, MySQL, ClickHouse, Databricks). Nothing is read; describe the table from the catalog, or exclude it from that rule's `match`. |
| `dbt manifest <path>: ...` | The connection's `dbt_manifest` is missing, unreadable, older than dbt 1.3, or came from `dbt parse` without compiled SQL. The message names which; run `dbt compile` against the target the connection prints — see [Configuration](../CONFIG.md). |
| `sqlglot is not installed. Install dbprint with the [dbt] extra: ...` | A connection names a `dbt_manifest` and the `[dbt]` extra is not installed. |

## The run stopped with exit `7`, every table failed

The account reached the database but can do nothing with it. Check the grants on your engine's adapter page — [duckdb](../adapters/duckdb.md), [PostgreSQL](../adapters/postgres.md), [MySQL](../adapters/mysql.md), [ClickHouse](../adapters/clickhouse.md), [Redshift](../adapters/redshift.md), [Snowflake](../adapters/snowflake.md), [Databricks](../adapters/databricks.md), [BigQuery](../adapters/bigquery.md).

## One table failed, others written (exit `5`)

| Symptom | Usually |
|---|---|
| A per-table privilege failure, grouped on stderr with the operation that raised it | Most often one schema's grant. The tables that succeeded are already on disk and valid. |
| `... reads it with read.tables.materialize: false, and this adapter's per-statement sampling construct cannot be seeded into agreement across statements. Set read.tables.materialize: true for this table's rule, or narrow with mode: filter instead of a sample.` | Hits ClickHouse, Redshift, BigQuery, MySQL and Snowflake — the five engines whose per-statement sampling cannot be trusted to agree across statements without the materialized copy. The remedy is in the message. |
| `... could not materialize its sample of <p> (<cause>), and this adapter's per-statement fallback cannot be seeded into agreement across statements. Narrow with mode: filter instead of a sample.` | Same five engines, this time the write itself was refused. On ClickHouse the `<cause>` is usually the table declaring no `SAMPLE BY` key — a catalog fact caught before any `CREATE` is issued. On BigQuery the cause is a refused `CREATE OR REPLACE TABLE` in the profiled dataset. |
| `no DDL available for '<table>'; not found in catalog` | Fails that one table's DDL extraction; statistics for it may still succeed. |
| `OSError: [Errno 36] File name too long`, or another `OSError`, followed by the table's `Directory:` and an `exclude:` snippet | The filesystem refused that table's own directory — most often a name longer than it allows. Every other table is written; paste the snippet into `tables.exclude` until the directory can be created. |
| `<directory> is a symlink to a directory outside the connection root` | The table's print directory is a symbolic link leading out of the print, so nothing is written through it. Replace the link with a directory, or point it inside the print. |

## The output is not what you expected

| Symptom | Cause |
|---|---|
| A table shows `- rows` | No query read its rows: it is a plain view under the default `read.views.mode: catalog`, or a table a rule reads in `catalog` mode. Its `statistics.yaml` comes from the catalog and says `catalog_only` — see [choosing what to profile](scoping.md). |
| `generate` reports skips and writes nothing new | Every table is still inside its `max_age_days` window, seven days by default. `--force` re-profiles regardless. |
| Ratios look wrong for the table size | The table was narrowed. Every ratio in a file carrying a `scope` block is denominated in `rows_scanned`, not `row_count` — see [choosing what to profile](scoping.md). |
| A column has no sketch | A redaction rule covers the column — a sketch is a set of digests of the values the rule withheld — or the whole table is narrowed, described from the catalog, or has `sketches` switched off (its `statistics.yaml` names it in `disabled`). |
| A join edge the last print carried is gone | It was a `detection: measured` edge, and measured edges are built from sketches. Redacting, narrowing or switching `sketches` off on either endpoint removes the sketch, and the edge with it. Containment alone also never proposes an edge out of a column declared unique on its own table, a column that already carries a declared or inferred edge, a timestamp or boolean, a measured-unique column not named `*_id`, or any column not named `*_id` into an integer key whose values run gapless from its minimum to its maximum ([SPEC 2.3.11](../format/v1/SPEC.md)) — so a print regenerated by a newer dbprint can drop such edges. |
| `check` exits non-zero with nothing obviously wrong on screen | Warnings are reported as a count, not by code. Take `--format json` and filter on severity — see [gating CI](ci.md). |
| `.dbprint.yaml: ... unknown key; did you mean ...` | The file is checked whole when it loads, and every key it does not know is refused with its full path and the nearest valid key. Move the key to the path the message names; [Configuration](../CONFIG.md) has the full shape. |
| A print is silently short of tables on Databricks | Unity Catalog's `information_schema` is privilege-filtered, not privilege-gated — an under-privileged principal gets fewer rows, not an error. Check the table count against what you expect; see the [Databricks page](../adapters/databricks.md). |
| `the target lists none of the N committed tables in scope; refusing to record them as removed` (exit 4) | The connection reaches a target that shares no table with the committed print. Three causes: a wrong path or database (a relative path resolves against the project root), a role without grants, or every table genuinely dropped. Only the last is recorded, with `generate --confirm-all-removed`. |
| `generate` exits 1 naming `manifest.yaml` (could not parse / ignoring / has entries generate cannot follow / no manifest ... but ... table directories) | The committed manifest is unreadable, often a merge's conflict markers, or was deleted while its prints remain, or an entry leads outside the connection root — `dbprint check` reports it as `manifest.path-outside-print`; correct the `path` or replace the symlink with the file. Repair or restore it from git; to start the print over, delete the connection's print directory. |
| Snowflake reports `0 objects` and exits cleanly on a first run | Both catalog surfaces the adapter reads are filtered to what the role can see, so a missing `SELECT` produces an empty result rather than an error. Against an existing print the same listing exits `4` instead. Check the role's grants before concluding the selectors are wrong; see the [Snowflake page](../adapters/snowflake.md). |

## The run succeeded but the artifact is thinner than expected — all warnings, nothing fails

| Symptom | What is lost |
|---|---|
| `view-dependency catalog read failed for connection '<name>': <cause>; every view/matview omits depends_on this run` | `depends_on` on every view for that connection this run. On Snowflake the cause is usually the missing `snowflake.account_usage` grant — see the [Snowflake page](../adapters/snowflake.md). |
| No message at all | duckdb, ClickHouse, Databricks and BigQuery never carry `depends_on` on a view, by design — nothing warns, because there is nothing to compare against. |
| `no row-count estimate for '<table>'; rules carrying \`match.min_rows\` or a \`read.tables.max_rows\` ceiling do not apply to it` | Every size-gated rule silently does not apply to that table. On Redshift the usual cause is a missing grant on `svv_table_info` — see the [Redshift page](../adapters/redshift.md). On Databricks it is a refused `DESCRIBE TABLE EXTENDED ... AS JSON`. |
| `table '<table>' requires a partition filter (require_partition_filter), so it is described from the catalog alone; to profile it, add: rules: [...]` | BigQuery only. The table refuses any query without a predicate on its partitioning column, so it is printed `catalog_only` on every run; the rule the message spells out reads it in `filter` mode — see the [BigQuery page](../adapters/bigquery.md). |
| `table '<table>': could not drop the materialized sample '<name>' (<cause>); it outlives this run and is not session-scoped` | BigQuery only. The copy is a real table in the profiled dataset; its 6-hour expiration is what bounds it when the drop itself fails. Every other engine's variant of this warning ends "the session drops it". |

## Finding out more

`--debug` on any command replaces the one-line error with a traceback. Without it, an unexpected failure prints only its message.

Every run also writes a log to `~/.dbprint/logs/<project-slug>/`, named for the time it started and the command it ran, keeping the last three per project. It records the dbprint version, the full command line, the config file, the connections, and every statement issued with its parameters and row count — which is usually enough to see which query failed and against what.

That log contains the SQL your project ran and the host, database and account it ran against. Read it before attaching it to anything.
