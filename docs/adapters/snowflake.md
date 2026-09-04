# Snowflake

```console
$ pip install 'dbprint[snowflake]'
```

The extra carries the Snowflake Connector for Python and `cryptography`, which key-pair authentication needs. Nothing else — DDL comes from `GET_DDL` rather than an external binary.

Fully-qualified names are `database.schema.table`.

## Credentials

`account`, `user`, `warehouse`, `database` and `role` are required; `schema`, `password`, `private_key_file` and `private_key_file_pwd` are optional, and exactly one of `password` or `private_key_file` is supplied — both, or neither, is an error. `private_key_file_pwd` decrypts an encrypted key. See [Configuration](../CONFIG.md).

## Privileges

```sql
CREATE ROLE dbprint_ro;

GRANT USAGE ON WAREHOUSE profiling_wh TO ROLE dbprint_ro;
GRANT USAGE ON DATABASE arboretum TO ROLE dbprint_ro;
GRANT USAGE ON SCHEMA arboretum.seedbank TO ROLE dbprint_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA arboretum.seedbank TO ROLE dbprint_ro;
GRANT SELECT ON ALL VIEWS IN SCHEMA arboretum.seedbank TO ROLE dbprint_ro;

GRANT ROLE dbprint_ro TO USER dbprint_service;
```

| Privilege | On | Needed for |
|---|---|---|
| `USAGE` | the warehouse | running any query at all |
| `USAGE` | the database | reaching anything inside it |
| `USAGE` | each schema profiled | reaching anything inside it |
| `SELECT` | each table and materialized view profiled | DDL and statistics |
| `SELECT` | each plain view profiled | DDL only — no statement is issued against a view |

Querying a table takes `SELECT` on the table together with `USAGE` on the database and the schema containing it — operating on an object requires at least one privilege on each of its parents. `SELECT` on a view is sufficient to read that view; the underlying objects do not also need it.

The warehouse grant is not optional even though most of the introspection could do without it. `SHOW` commands run without a warehouse, but the `INFORMATION_SCHEMA` views require one running and in use, and the adapter reads both.

`ALL TABLES IN SCHEMA` covers what exists when the grant runs. Use `GRANT SELECT ON FUTURE TABLES IN SCHEMA ... ` to cover tables created afterwards.

### What an under-privileged role does — read this one

Snowflake fails differently from most of the other engines, and the difference is the thing most likely to waste an afternoon.

Both surfaces the adapter enumerates from are filtered to what the role can see. `INFORMATION_SCHEMA` returns only objects the current role has been granted access to, and `SHOW` returns only objects the role holds at least one privilege on. A role missing `SELECT` therefore does not get an error — it gets **an empty result**, and dbprint writes a print with no tables in it.

So a run that reports `0 objects` and exits cleanly is the symptom of a missing grant, not of an empty database. PostgreSQL and MySQL would have reported a permission failure or refused the connection outright; Snowflake reports success over nothing.

Check the role's grants before concluding the selectors are wrong.

### The undocumented grant behind `depends_on`

`introspect_view_dependencies` issues two statements: `information_schema.tables` to seed every view, then `snowflake.account_usage.object_dependencies` filtered to the connected database — an account-wide catalog that lags **up to three hours** behind a newly created view. Reading it needs a grant no other statement on this page requires:

```sql
GRANT DATABASE ROLE SNOWFLAKE.OBJECT_VIEWER TO ROLE dbprint_ro;
```

Issued by `ACCOUNTADMIN`. This is narrower than `IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE` and is the route Snowflake itself recommends. Without it, a role without access to that share does not fail the run: the catalog read raises, the run degrades with one warning, and every view and matview in the connection omits `depends_on` for that run. Do not expect a view created moments ago to resolve — the three-hour lag is Snowflake's own published figure, not a dbprint limitation.

## Sampling

| | |
|---|---|
| Construct | `SAMPLE SYSTEM (p) SEED (s)` |
| Seeded | yes, from the table's own name |
| `looks_like` sub-draw | `SAMPLE ROW (n ROWS)`, **cannot** be seeded |

Snowflake's seed applies only to `SYSTEM`/`BLOCK` sampling, so that is what a fraction uses. Two restrictions come with it, and both are properties of the engine rather than choices dbprint made:

- **A seed is not supported on fixed-size sampling.** The extra distinct-value draw that `inferred.looks_like` takes is `SAMPLE ROW (n ROWS)`, which is fixed-size, so it cannot be seeded at all.
- **A seed is not supported on a view or a subquery.** A fraction binds to a base table.

The consequence for a sampled Snowflake print: coherence between a shape claim and the rest of the profile is **population-level only**, never row for row. A test or a consumer asserting that `inferred.sampled` describes the same rows as `null_count` is asserting something this engine cannot deliver. PostgreSQL and duckdb are the two engines that cohere row for row; Databricks reproduces the sampled fraction but not that row-level agreement.

## When dbprint writes

Only one thing makes dbprint write, and only under one condition: `materialize_sample` is on by default, and it fires **only for a table narrowed to a fraction of its rows**. A fraction has two sources — a rule's `sample`, and a `max_rows_scanned` ceiling resolving against the table's size, which is legal at connection and `defaults` level as well as inside a rule. A project that narrows nothing, which is what `dbprint init` scaffolds, never writes. A `filter` is a predicate rather than a fraction and never materializes.

Where it does fire, the drawn rows are copied once into a session-lifetime temporary table created **in the profiled table's own schema**, and every statistics statement for that table reads the copy. That is what makes the numbers in one file describe one set of rows, and it is why the fraction is not simply re-evaluated: two evaluations of one seeded expression are not documented to read the same rows.

No extra grant is required for it. Snowflake exempts temporary tables from the `CREATE TABLE` privilege, and the schema the copy lands in is one the role already holds `USAGE` on.

Where the write is refused anyway — a managed-access schema, or a policy that blocks it — the run does not fail the connection, but it does refuse that table. `SAMPLE_FALLBACK_COHERENT` is `false` on Snowflake: a fixed-size sample cannot be seeded, so an unmaterialized draw can never be trusted to agree across statements, and the table is refused rather than degraded:

```
table 'arboretum.seedbank.germination_trial': connection 'primary' (snowflake) could not
materialize its sample of 0.25 (<cause>), and this adapter's per-statement fallback cannot be
seeded into agreement across statements. Narrow with a filter instead of a sample fraction.
```

Setting `materialize_sample: false` on a `sample`-scoped table is refused the same way, before any statement runs.

## Reference

- Every configuration key: [Configuration](../CONFIG.md)
- What a `scope` block does to the numbers: [choosing what to profile](../guide/scoping.md) and [SPEC 2.2.8](../format/v1/SPEC.md#228-scope--statistics-over-part-of-a-table)
- How a shape claim is sampled and thresholded: [SPEC 4.1.2](../format/v1/SPEC.md#412-sampling-strategy)
- What DDL normalization strips and preserves: [SPEC 2.1.3](../format/v1/SPEC.md#213-what-must-be-stripped-per-adapter)
