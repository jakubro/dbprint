# SQLite

```console
$ pip install dbprint
```

SQLite needs no extra: dbprint reads it through Python's own `sqlite3`, so the SQLite library is the one the interpreter was built with, and it must be 3.38.0 or later. SQLite runs in-process: no server and no external binary. A failed open: `could not open sqlite database <database>: <cause>`.

A table's `ddl.sql` holds its `CREATE TABLE` as the file stores it, then each of its `CREATE INDEX` statements; a view's holds its `CREATE VIEW`. Triggers are left out.

Fully-qualified names are `main.table`, a name SQLite's own SQL accepts. The file's name appears nowhere in the print: the print's directory is named after the connection.

## Credentials

`database` is the only key: the file's path. A relative path resolves against the project root, and a file that does not exist is refused with exit `4`, never created. Every key and its meaning are in [Configuration](../CONFIG.md).

The file is always opened read-only, so there is no `read_only` key. There is no host, no port, no user and nothing to grant — the file's own filesystem permissions are the only access control in play.

## Sampling

| | |
|---|---|
| Construct | `DBPRINT_DRAW(seed, row) < p`, a hash of the table's seed and the row's values |
| Seeded | yes, from the table's own name |
| `looks_like` sub-draw | `ORDER BY DBPRINT_DRAW(seed, row) LIMIT n`, seeded |

SQLite has no sampling clause, so dbprint registers a draw function of its own. A sampled print is coherent **row for row**, the same guarantee PostgreSQL and duckdb give: every statement for a sampled table reads the same rows, including the extra distinct-value draw `inferred.looks_like` takes on top of them, and an unchanged file draws the same rows on the next run.

## Distinct and row counts

Every `cardinality` is an exact distinct count, so `cardinality_method` is always `exact`; a column declared `COLLATE NOCASE` or `COLLATE RTRIM` counts its values the way that collation compares them. Every `row_count`, and every size a `min_rows` rule or a `max_rows` ceiling is tested against, is an exact `COUNT(*)`.

## When dbprint writes

Nothing is ever written to the file. A sampled table's draw is copied into SQLite's own `temp` schema, which lives outside the file and is gone when the session closes. An `ATTACH` in a SQL assertion is refused, so `dbprint check --online` cannot create a file either.

A file in WAL mode gets SQLite's own `-shm` and `-wal` files beside it, as every reader of such a file does, and they remain after the run; a WAL file in a directory dbprint cannot write to cannot be opened.

## Identifiers

Every SQLite identifier resolves case-insensitively, so a capital in the schema survives only as `physical_name`, carried for a reader to see. Path segments are lowercased before the SPEC 1.5 rule check runs, so a capital letter can never trigger `contains-unsafe-character` — a space or other unsafe character still can.

## What it cannot deliver

- **`physical_layout`** — always absent. SQLite declares no clustering or partitioning key.
- **Comments** — SQLite stores no table or column comment.
- **Foreign key `constraint_name`** — always absent. SQLite names no foreign key constraint.
- **`mean` and `sum` past 64 bits** — SQLite has no exact type wider than a 64-bit integer, so such a column names both in `unmeasured`.
- **Exact decimals** — a `DECIMAL` column holds INTEGER and REAL values, and a REAL keeps 15 significant digits.

A view's `depends_on` names the tables and views it reads, as SQLite resolves the view; a view SQLite can no longer prepare, such as one over a dropped table, carries none.

## Column types

A SQLite declared type is free text, and every value carries its own storage class. A column classifies by its declared type while its values fit that type, and by the values it holds otherwise ([SPEC 3.3](../format/v1/SPEC.md#33-edge-cases)). `sql_type` is the declared type as written, `''` when there is none.

| Declared | Profiled as | What to know |
|---|---|---|
| `INTEGER` holding text | `text` | The value list holds the numbers and the text alike; no `range`, `mean` or `sum` |
| `DATE`, `DATETIME`, `TIMESTAMP` | `temporal` | Only while every value is text beginning `YYYY-MM-DD`. Bounds, percentiles and listed values are the stored text as written, the spelling a predicate against the column matches |
| `BOOLEAN` | `boolean` | Only while every value is `0` or `1` |
| `JSON` | `json` | Only while every value is valid JSON; `types` keys are what `json_type` returns |
| No declared type, `BLOB` | by the values held | All numbers read as a number, all blobs as `binary`, anything else as `text` |

A virtual table, such as an FTS5 index, is printed like a table, and the shadow tables behind it are not. One whose module this SQLite library lacks fails into `failed_tables`, and its shadow tables are then printed as ordinary tables.

## Statement timeout

SQLite has no server to cancel on, so `run.statement_timeout` is a client-side clock: a running statement checks its deadline and the run's interrupt as it goes, and ends shortly after either.

## Parallelism

`run.parallelism` above `1` opens one read-only connection per session on the same file, and SQLite reads them at once.

## Reference

- Every configuration key: [Configuration](../CONFIG.md)
- What a `scope` block does to the numbers: [choosing what to profile](../guide/scoping.md) and [SPEC 2.2.8](../format/v1/SPEC.md#228-scope--statistics-over-part-of-a-table)
- What `ddl.sql` holds: [SPEC 2.1.3](../format/v1/SPEC.md#213-what-must-be-stripped-per-adapter) and [SPEC 2.1.5](../format/v1/SPEC.md#215-multi-statement-ddl)
