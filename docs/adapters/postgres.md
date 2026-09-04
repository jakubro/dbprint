# PostgreSQL

```console
$ pip install 'dbprint[postgres]'
```

The extra carries psycopg 3. PostgreSQL additionally needs the **`pg_dump` client binary on `PATH`**: DDL comes from `pg_dump --schema-only`, not from a query. The binary is probed when the connection opens, before any query runs, so a container that installs the Python package alone does not profile statistics and then fail on DDL — it fails at `connecting` with `pg_dump binary not found on PATH`, exits `4`, and writes nothing. `pg_dump` must also be at least the server's major version; an older client refuses a newer server outright, which fails every table's DDL.

Fully-qualified names are `schema.table`.

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
| `SELECT` | each plain view profiled | DDL only — no statement is issued against a view |
| `TEMPORARY` | the database | the sampled-table copy only |

`CONNECT` and `TEMPORARY` are granted to `PUBLIC` on a stock cluster, so a role that was only just created already holds both. They are listed because a hardened cluster that revoked them from `PUBLIC` needs them granted back explicitly.

`GRANT SELECT ON ALL TABLES` covers the tables that exist when it runs. For tables created later, add `ALTER DEFAULT PRIVILEGES IN SCHEMA seedbank GRANT SELECT ON TABLES TO dbprint_ro`.

### What an under-privileged role does

`pg_catalog` and `information_schema` are readable without any grant, so a role holding nothing still connects and enumerates every object in scope. The run then fails per object rather than as a whole, and the two failure paths look different because they are different:

- **DDL** fails per table, grouped on stderr at the end of the run with the exit status and `pg_dump`'s own stderr carried through. Without `USAGE` on the schema:

  ```
  1 table failed: PostgresConnectionError: pg_dump failed for 'seedbank.accession': exit 1;
  stderr: pg_dump: error: query failed: ERROR:  permission denied for schema seedbank
  pg_dump: detail: Query was: LOCK TABLE seedbank.accession IN ACCESS SHARE MODE
    operation: extract_ddl
    first: seedbank.accession
  ```

  With `USAGE` but no `SELECT` on the table, the same message ends `permission denied for table accession`. Both are raised by the `ACCESS SHARE` lock `pg_dump` takes before reading, which is why the last clause is the quickest way to tell which grant is still missing.
- **Statistics** fail as `InsufficientPrivilege: permission denied for schema seedbank`.

Granting `USAGE` on the schema moves both messages from naming the schema to naming the object, which is the signal that the remaining gap is `SELECT`. A run that ends `4 ok / 1 failed` exits `5`, and the tables that succeeded are still written.

A plain view is the exception at every rung, including the bottom one: a role holding no grant at all still gets a complete print of it. No query is issued against a view, so its `statistics.yaml` comes from the catalog, and its DDL succeeds where every table's fails.

## Sampling

| | |
|---|---|
| Construct | `TABLESAMPLE BERNOULLI(p) REPEATABLE(seed)` |
| Seeded | yes, from the table's own name |
| `looks_like` sub-draw | `TABLESAMPLE` at a computed rate, seeded |

PostgreSQL is one of two engines on which a sampled print is coherent **row for row**: every statement for a sampled table reads the same rows, including the extra distinct-value draw that `inferred.looks_like` takes on top of them. duckdb gives the identical guarantee. Databricks reproduces the sampled fraction reliably but not that row-level agreement with `looks_like`; the remaining five engines can only promise coherence at the population level.

## When dbprint writes

Only one thing makes dbprint write, and only under one condition: `materialize_sample` is on by default, and it fires **only for a table narrowed to a fraction of its rows**. A fraction has two sources — a rule's `sample`, and a `max_rows_scanned` ceiling resolving against the table's size, which is legal at connection and `defaults` level as well as inside a rule. A project that narrows nothing, which is what `dbprint init` scaffolds, never writes. A `filter` is a predicate rather than a fraction and never materializes.

Where it does fire, the drawn rows are copied once into a session-lifetime temporary table in the session's own temporary space, and every statistics statement for that table reads the copy. That is what makes the numbers in one file describe one set of rows.

Where `TEMPORARY` is absent the run does not fail. It warns on stderr and falls back to re-evaluating the sample per statement:

```
table 'seedbank.germination_trial': could not materialize its sample of 0.25
(InsufficientPrivilege: permission denied to create temporary tables in database "my_db");
each statistic for it is measured over its own draw of the rows
```

The fallback is not an equivalent path. Each statement then draws its own rows, so a column's listed value counts and the non-null figure they are a share of come from different reads, and the file can disagree with itself on a table nobody wrote to. Setting `materialize_sample: false` chooses that trade deliberately, which is the right call where the tool must stay strictly read-only — and the wrong one where it was chosen to avoid a grant.

## Reference

- Every configuration key: [Configuration](../CONFIG.md)
- What a `scope` block does to the numbers: [choosing what to profile](../guide/scoping.md) and [SPEC 2.2.8](../format/v1/SPEC.md#228-scope--statistics-over-part-of-a-table)
- What DDL normalization strips and preserves: [SPEC 2.1.3](../format/v1/SPEC.md#213-what-must-be-stripped-per-adapter)
