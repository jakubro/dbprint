# duckdb

```console
$ pip install 'dbprint[duckdb]'
```

The extra carries `duckdb>=1.0`. duckdb runs in-process: no server, no separate driver process, and no `pg_dump`-style external binary — DDL comes straight from the catalog. Absent driver: `duckdb is not installed. Install dbprint with the [duckdb] extra: \`pip install dbprint[duckdb]\`.` A failed open: `could not open duckdb database <database>: <cause>`.

Fully-qualified names are `database.schema.table` — three segments, matching duckdb's own attached-database-then-schema nesting.

## Credentials

`database` is the only required key: a file path, or `:memory:` for an ephemeral database that vanishes when the process exits. A relative path resolves against the project root, and a file that does not exist is refused, never created. `read_only` is optional and defaults to `false`; every key and its meaning are in [Configuration](../CONFIG.md).

There is no host, no port, no user, and nothing to grant — the file's own filesystem permissions are the only access control in play.

### What a read-only connection can and cannot do

`read_only: true` opens the file without a write lock, and it does **not** block the temporary table `materialize_scope` creates for a narrowed table: duckdb's read-only guard applies per attached database, and the session's own `temp` catalog is exempt from it. A `read_only` connection over a file therefore still samples correctly.

`:memory:` combined with `read_only: true` is a different case — the connection never opens at all, since there is nothing on disk to open read-only.

## Sampling

| | |
|---|---|
| Construct | `TABLESAMPLE bernoulli(p PERCENT) REPEATABLE (seed)` |
| Seeded | yes, from the table's own name |
| `looks_like` sub-draw | `TABLESAMPLE reservoir(n ROWS) REPEATABLE (seed)`, seeded |

duckdb is coherent **row for row** on a sampled table, the same guarantee PostgreSQL gives: every statement for a sampled table reads the same rows, including the extra distinct-value draw `inferred.looks_like` takes on top of them.

## When dbprint writes

Only one thing makes dbprint write, and only under one condition: `materialize_sample` is on by default, and it fires **only for a table narrowed to a fraction of its rows**. A fraction has two sources — a rule's `sample`, and a `max_rows_scanned` ceiling resolving against the table's size, which is legal at connection and `defaults` level as well as inside a rule. A project that narrows nothing, which is what `dbprint init` scaffolds, never writes. A `filter` is a predicate rather than a fraction and never materializes.

Where it does fire, the drawn rows are copied once into a session-lifetime temporary table in duckdb's own `temp` catalog — unqualified, since duckdb allows a temporary table only there — and every statistics statement for that table reads the copy.

`materialize_sample: false` never falls back on duckdb: `SAMPLE_FALLBACK_COHERENT` is `true` here, the same as PostgreSQL, so an unmaterialized `sample` scope already stays coherent across statements. The only way the write itself can fail is the read-only interaction above.

## Identifiers

Every duckdb identifier resolves case-insensitively, so a capital in the catalog survives only as `physical_name`, carried for a reader to see. Path segments are lowercased before the SPEC 1.5 rule check runs, so a capital letter can never trigger `contains-unsafe-character` — a space or other unsafe character still can. A period inside a name is part of it: every statement addresses the table by its captured parts, never by splitting its FQN.

## What it cannot deliver

- **`physical_layout`** — always absent. duckdb declares no clustering or partitioning key for an ordinary table.
- **`depends_on`** — always absent on every view. `duckdb_dependencies()` misses a plain view's read of a table, so the field is omitted rather than guessed from DDL text.
- **Column `collation`** — always `null`. duckdb exposes no per-column collation surface.
- **Foreign key `on_delete` / `on_update`** — always `NO ACTION`. duckdb's catalog parses no referential-action clause at all.

## Column types

| Type | Profiled as | What to know |
|---|---|---|
| `T[]`, `T[n]` | `composite` | An array of floats with one length per value, such as an embedding in `FLOAT[n]`, also carries `norm` and `zero_count`; duckdb has no `vector` type |
| `STRUCT(...)`, `UNION(...)` | `composite` | A nested field keeps its full path (`.spot.x`) |
| `MAP(K, V)` | `composite` | `labels['site'] IS NULL` holds for an absent key, a null value, an empty map and a null map alike; the parts tell them apart |
| `JSON` | `json` | `types` keys are what `json_type` returns |
| `GEOMETRY` | `spatial` | Needs the `spatial` extension installed: dbprint loads it but never installs it, and without it `geometry` and `extent` are reported unmeasured with a warning. `srid` is a CRS string such as `OGC:CRS84`; duckdb 1.5.5 reads a file database's `GEOMETRY('OGC:CRS84')` column back with no CRS |

## Statement timeout

duckdb has no server to cancel on, so `statement_timeout` is a client-side clock: a timer interrupts the connection when a statement outlives the limit. The interrupt is observed only where duckdb checks for it, so a statement can overrun briefly.

## Parallelism

`parallelism` is refused above `1` at load. An unnamed `:memory:` database is private to each session, and a temporary table to the connection that made it, so a second session would read neither; duckdb already parallelises one statement across cores.

## Reference

- Every configuration key: [Configuration](../CONFIG.md)
- What a `scope` block does to the numbers: [choosing what to profile](../guide/scoping.md) and [SPEC 2.2.8](../format/v1/SPEC.md#228-scope--statistics-over-part-of-a-table)
- What DDL normalization strips and preserves: [SPEC 2.1.3](../format/v1/SPEC.md#213-what-must-be-stripped-per-adapter)
