# When something goes wrong

Indexed by what you saw, not by what caused it.

## The run stopped early

| Symptom | Usually |
|---|---|
| Exits `4`, nothing profiled, no print written | The connection. Credentials resolve from `DBPRINT_<CONN>_<KEY>`, then `~/.dbprint/connections.yaml`, then a project-root `.env` — first hit per key, so an old environment variable silently outranks a corrected file. On PostgreSQL this is also what a missing `pg_dump` looks like: the binary is probed before the first query. |
| Exits `7`, every table failed | The account reached the database but can do nothing with it. Check the grants on your engine's adapter page — [PostgreSQL](../adapters/postgres.md), [MySQL](../adapters/mysql.md), [Snowflake](../adapters/snowflake.md). |
| Exits `5`, some tables written and some not | A per-table privilege, most often on one schema. The failures are grouped on stderr with the operation that raised each one; the tables that succeeded are already on disk and valid. |
| `pg_dump: error: server version mismatch` | The client is older than the server. `pg_dump` refuses to dump from a newer server, so every table's DDL fails while statistics succeed. Install a `pg_dump` at least the server's major version. |

## The output is not what you expected

| Symptom | Cause |
|---|---|
| A table shows `- rows` | It is a view. No query is ever issued against one; its `statistics.yaml` comes from the catalog and says `catalog_only`. |
| `generate` reports skips and writes nothing new | Every table is still inside its `max_age_days` window, seven days by default. `--force` re-profiles regardless. This is also why a newly created annotation file stays invisible — see [annotating a print](annotations.md). |
| Ratios look wrong for the table size | The table was narrowed. Every ratio in a file carrying a `scope` block is denominated in `rows_scanned`, not `row_count` — see [choosing what to profile](scoping.md). |
| A column has no sketch | Sketches are skipped entirely for a narrowed table and for a view. |
| `check` exits non-zero with nothing obviously wrong on screen | Warnings are reported as a count, not by code. Take `--format json` and filter on severity — see [gating CI](ci.md). |
| A config key seems to do nothing | An unrecognised or mis-nested key is dropped silently. Compare against the nesting in [Configuration](../CONFIG.md); the common error is a `rules` entry indented under the wrong parent. |

## Finding out more

`--debug` on any command replaces the one-line error with a traceback. Without it, an unexpected failure prints only its message.

Every run also writes a log to `~/.dbprint/logs/<project-slug>/`, named for the time it started and the command it ran, keeping the last three per project. It records the dbprint version, the full command line, the config file, the connections, and every statement issued with its parameters and row count — which is usually enough to see which query failed and against what.

That log contains the SQL your project ran and the host, database and account it ran against. Read it before attaching it to anything.
