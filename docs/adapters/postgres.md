# PostgreSQL

```console
$ pip install 'dbprint[postgres]'
```

The extra carries psycopg 3. dbprint reads PostgreSQL 11 or later. PostgreSQL additionally needs the **`pg_dump` client binary on `PATH`**: DDL comes from `pg_dump --schema-only`, not from a query. The binary is probed when the connection opens, before any query runs, so a container that installs the Python package alone does not profile statistics and then fail on DDL — it fails at `connecting` with `pg_dump binary not found on PATH`, exits `4`, and writes nothing. `pg_dump` must also be at least the server's major version; an older client refuses a newer server outright, which fails every table's DDL.

Fully-qualified names are `database.schema.table`, whether or not `database` is set.

## Privileges

```sql
CREATE ROLE dbprint_ro LOGIN PASSWORD '...';

GRANT CONNECT ON DATABASE my_db TO dbprint_ro;
GRANT USAGE ON SCHEMA seedbank TO dbprint_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA seedbank TO dbprint_ro;

-- Only when a table is narrowed to a fraction; see "When dbprint writes".
GRANT TEMPORARY ON DATABASE my_db TO dbprint_ro;
```

| Privilege | On | Needed for |
|---|---|---|
| `CONNECT` | the database | connecting |
| `USAGE` | each schema profiled | reaching anything inside it |
| `SELECT` | each table and materialized view profiled | DDL and statistics |
| `SELECT` | each plain view profiled | DDL only under the default `read.views.mode: catalog`, which issues no statement against a view; a view read in `full` or `filter` mode is queried like a table |
| `TEMPORARY` | the database | the sampled-table copy only |

`CONNECT` and `TEMPORARY` are granted to `PUBLIC` on a stock cluster, so a role that was only just created already holds both. They are listed because a hardened cluster that revoked them from `PUBLIC` needs them granted back explicitly.

`GRANT SELECT ON ALL TABLES` covers the tables that exist when it runs. For tables created later, add `ALTER DEFAULT PRIVILEGES IN SCHEMA seedbank GRANT SELECT ON TABLES TO dbprint_ro`.

### What an under-privileged role does

`pg_catalog` and `information_schema` are readable without any grant, so a role holding nothing still connects and enumerates every object in scope. The run then fails per object rather than as a whole, and the two failure paths look different because they are different:

- **DDL** fails per table, grouped on stderr at the end of the run with the exit status and `pg_dump`'s own stderr carried through. Without `USAGE` on the schema:

  ```
  1 table failed: PostgresConnectionError: pg_dump failed for 'arboretum.seedbank.accession': exit 1;
  stderr: pg_dump: error: query failed: ERROR:  permission denied for schema seedbank
  pg_dump: detail: Query was: LOCK TABLE seedbank.accession IN ACCESS SHARE MODE
    operation: extract_ddl
    first: arboretum.seedbank.accession
  ```

  With `USAGE` but no `SELECT` on the table, the same message ends `permission denied for table accession`. Both are raised by the `ACCESS SHARE` lock `pg_dump` takes before reading, which is why the last clause is the quickest way to tell which grant is still missing.
- **Statistics** fail as `InsufficientPrivilege: permission denied for schema seedbank`.

Granting `USAGE` on the schema moves both messages from naming the schema to naming the object, which is the signal that the remaining gap is `SELECT`. A run that ends `4 ok / 1 failed` exits `5`, and the tables that succeeded are still written.

A plain view is the exception at every rung, including the bottom one: a role holding no grant at all still gets a complete print of it. No query is issued against a view read in the default `read.views.mode: catalog`, so its `statistics.yaml` comes from the catalog, and its DDL succeeds where every table's fails. A view read in `full` or `filter` mode is queried like a table and needs `SELECT` on it.

## Sampling

| | |
|---|---|
| Construct | `TABLESAMPLE BERNOULLI(p) REPEATABLE(seed)` |
| Seeded | yes, from the table's own name |
| `looks_like` sub-draw | `TABLESAMPLE` at a computed rate, seeded |

PostgreSQL is one of two engines on which a sampled print is coherent **row for row**: every statement for a sampled table reads the same rows, including the extra distinct-value draw that `inferred.looks_like` takes on top of them. duckdb gives the identical guarantee. Databricks reproduces the sampled fraction reliably but not that row-level agreement with `looks_like`; every other engine can only promise coherence at the population level.

## When dbprint writes

Only one thing makes dbprint write, and only under one condition: `profile.read.tables.materialize` is on by default, and it fires **only for a table read in `sample` mode** — by a `fraction`, or by a `max_rows` ceiling that resolves below the table's size, at whatever level the ceiling is set. A project that narrows nothing, which is what `dbprint init` scaffolds, never writes. A `filter` is a predicate rather than a fraction and never materializes.

Where it does fire, the drawn rows are copied once into a session-lifetime temporary table in the session's own temporary space, and every statistics statement for that table reads the copy. That is what makes the numbers in one file describe one set of rows.

Where `TEMPORARY` is absent the run does not fail. It warns on stderr and falls back to re-evaluating the sample per statement:

```
table 'arboretum.seedbank.germination_trial': could not materialize its sample of 0.25
(InsufficientPrivilege: permission denied to create temporary tables in database "my_db");
every statement reads the seeded sample directly, so a write between two of them reaches one
and not the other
```

On a table nobody writes to during the run, the fallback still reads the same rows in every statement: `BERNOULLI` decides each row from the seed, so the file agrees with itself. What it gives up is protection from writes — a row inserted or deleted between two statements reaches one and not the other, where the copy would have frozen the draw, so a column's listed value counts and the non-null figure they are a share of can come from different rows. Setting `read.tables.materialize: false` chooses that trade deliberately, which is the right call where the tool must stay strictly read-only — and the wrong one on a table taking writes, where it was chosen to avoid a grant.

## Partitioned tables

A partitioned table is printed as its parent. Its partitions are not listed, and a foreign key into or out of it is published once, against the parent, never against the copies PostgreSQL keeps on each partition. A partition detached from its parent is an ordinary table again and publishes its own foreign keys.

## Foreign tables

A foreign table is listed with `external: true` (SPEC 2.2.20). It is always described from the catalog alone: `USAGE` on its schema is enough, its DDL shows the `CREATE FOREIGN TABLE ... SERVER ... OPTIONS (...)` statement, and no statement reaches the remote server. The foreign server and its user mappings are never dumped.

A rule writing any other `read.tables.mode` for it is refused before any table is read, with exit 1, because the read would reach another live database. A foreign partition is not listed, like any partition; reading its partitioned parent reads it too.

## Inheritance

A table with inheritance children is profiled with them, as a query against it returns them. Its approximate distinct counts come from the `pg_stats` row PostgreSQL keeps for the table and its children together, and its row estimate is the sum of `reltuples` over the table and every descendant. A child that was never analyzed and holds no pages counts nothing; any other never-analyzed child leaves the table without an estimate, so it is counted exactly and size rules do not apply to it. A table whose children carry no such `pg_stats` row has that column counted exactly too, never estimated from its own rows alone.

Autovacuum does not analyze a parent whose children alone change, so its inherited figures can lag them; run `ANALYZE` on the parent to refresh them. Since PostgreSQL 18 that also analyzes every child. A partitioned table's figures come from `ANALYZE` of the partitioned table itself, which autovacuum never runs: until then it has no estimate and is counted exactly.

## Types without a comparison operator

A `json` column is counted by its text, so two documents differing only in whitespace or key order count as two, and a `money` column is measured as `numeric`, publishing plain numbers rather than the session's currency spelling. A type with no equality operator at all (`point`, `xml`, `xid`, ...) is published `unsupported` with its null count, and a type nothing declares that fails a statement degrades that one column to `unmeasured`.

## Column types

| Type | Profiled as | What to know |
|---|---|---|
| array | `composite` | Postgres does not enforce dimensions, so a multi-dimensional array has one element part `[*]` over every element, never `[*][*]` |
| composite type, or a domain over one | `composite` | `ROW(NULL, NULL)` is a value, not a NULL: it counts as held, with null fields |
| `hstore` | `composite` | Recognized in whichever schema the extension lives |
| `json`, `jsonb` | `json` | A `json` value is read as `jsonb`, so a key written twice counts once, as its last value; `types` keys are what `jsonb_typeof` returns |
| `jsonpath` | `text` | Values are the paths' own spelling |
| PostGIS `geometry`, `geography` | `spatial` | The native `point`, `box` and `polygon` are not OGC geometries and stay `unsupported` |
| pgvector `vector`, `halfvec`, `sparsevec` | `vector` | `halfvec` and `sparsevec` need pgvector 0.7 or later; a `sparsevec` reports its declared dimension |

## Listed values

A network (`inet`, `cidr`), range, multirange or `interval` column lists its values as PostgreSQL's own text (`10.0.0.1/32`, `[1,5)`, `1 year 2 mons`), the text `length` is measured over — so every listed value, cast back to the column's type, selects its own rows.

## Namespaces

`database` is optional. Omitted, the connection first opens `postgres` and reads every database that accepts connections and is not a template. A PostgreSQL session is bound to one database, so the connection opens one session per database holding a selected table, with the same credentials, and runs every statement — `pg_dump` included — against the table's own database; a table whose database compares under a different default collation than the connection's first says so on each of its string columns. The grants below are needed in each database profiled. A database whose session will not open, or whose listing fails, is skipped with a warning. A database no `include` pattern can reach by its name is never opened at all. View dependencies are read only in databases holding a selected object; a database whose dependency read fails is named in a warning and its views omit `depends_on` for that run, while every other database's views keep theirs.

## The row-count estimate

A table's size, and a sampled table's `row_count`, come from `pg_class.reltuples`, which `VACUUM`, `ANALYZE` and a few DDL commands update: rows deleted since the last of them reached the table are still counted, so a sampled table publishes `row_count_method: approximate`.

## Statement timeout

`run.statement_timeout` is sent as the session's own `statement_timeout` setting in the connection's start-up packet, so it bounds every statement the session runs, lock waits included, and a statement that exceeds it is cancelled by the server. `pg_dump`, which extracts each table's DDL, zeroes that setting on its own session; there the limit becomes `--lock-wait-timeout`, bounding the wait for the table lock, while the dump as a whole keeps its fixed 60-second ceiling.

Every session also starts with `standard_conforming_strings = on`, and a session that does not read it back as `on` — a connection pooler that dropped the start-up option — fails the connection. A map or document key read back from the data is spelled into SQL as a string literal, and that literal is safe only under the one grammar the setting fixes.

## Parallelism

`run.parallelism: N` opens up to N sessions per database the run reads, each on first use, and profiles up to N tables at once. `pg_dump` opens a connection of its own for each table's DDL, so the connection count on the server can reach twice N; size `max_connections` and any per-role `CONNECTION LIMIT` for that.

## Reference

- Every configuration key: [Configuration](../CONFIG.md)
- What a `scope` block does to the numbers: [choosing what to profile](../guide/scoping.md) and [SPEC 2.2.8](../format/v1/SPEC.md#228-scope--statistics-over-part-of-a-table)
- What DDL normalization strips and preserves: [SPEC 2.1.3](../format/v1/SPEC.md#213-what-must-be-stripped-per-adapter)
