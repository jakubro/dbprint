# Redshift

```console
$ pip install 'dbprint[redshift]'
```

The extra carries `redshift-connector>=2.2.0`. Absent driver: `redshift-connector is not installed. Install dbprint with the [redshift] extra: \`pip install dbprint[redshift]\`.` A failed connection: `could not connect to Redshift at <host>:<port>/<database> as <user>: <cause>`. The connection opens with `autocommit = True`; the adapter applies no session-level read-only setting.

Fully-qualified names are `database.schema.table`, whether or not `database` is set.

## Privileges

```sql
CREATE USER dbprint_ro PASSWORD '...';

GRANT USAGE ON SCHEMA seedbank TO dbprint_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA seedbank TO dbprint_ro;

-- The one grant that matters: without it, size-gated rules silently do not apply.
GRANT SELECT ON svv_table_info TO dbprint_ro;
```

| Privilege | On | Needed for |
|---|---|---|
| `USAGE` | each schema profiled | reaching anything inside it |
| `SELECT` | each table and materialized view profiled | DDL and statistics |
| `SELECT` | `svv_table_info` | the row-count estimate every size-gated rule reads |
| `TEMP` (via `PUBLIC`) | the database | the sampled-table copy — every user already holds this through `PUBLIC` membership; it is not `CREATE ON SCHEMA` |
| — | `svv_redshift_tables`, `svv_redshift_columns`, `stv_mv_info` | need nothing beyond connecting — they self-filter to the caller's own objects |
| `USAGE` | each external schema | listing its external tables through `svv_external_tables` and `svv_external_columns`, and `SHOW EXTERNAL TABLE` |
| `SELECT` | each external table a rule reads by writing its `read.tables.mode` | statistics read through Spectrum or the federated source |

### Read this one: `svv_table_info` is superuser-only by default

`SVV_TABLE_INFO` is visible only to superusers, and `GRANT SELECT ON svv_table_info TO <user>` is what permits an ordinary user to query it. `GRANT ROLE sys:monitor` is a second route to the same access. `SYSLOG ACCESS UNRESTRICTED` is **not** a substitute — it does not open superuser-visible system views.

Without that grant, the row-count read fails for every table and is taken as no estimate at all, silently: the run warns `no row-count estimate for '<table>'; rules carrying \`match.min_rows\` or a \`read.tables.max_rows\` ceiling do not apply to it`, but nothing else in the run tells a reader that the whole size-gating system just went dark. An empty table is missing from `SVV_TABLE_INFO` entirely, rather than reported as zero, so its absence there is not itself a sign of a missing grant.

## Sampling

| | |
|---|---|
| Construct | `RANDOM() < p` in a wrapping subquery |
| Seeded | no — `RANDOM()` carries no seed at all on Redshift |
| `looks_like` sub-draw | oversamples with `RANDOM()`; the distinct set is hash-ordered either way |

Because the fraction cannot be seeded, coherence across a sampled table depends entirely on the materialized copy below — there is no population-level fallback guarantee the way duckdb or PostgreSQL have.

## When dbprint writes

Only one thing makes dbprint write, and only under one condition: `profile.read.tables.materialize` is on by default, and it fires **only for a table read in `sample` mode** — by a `fraction`, or by a `max_rows` ceiling that resolves below the table's size, at whatever level the ceiling is set. A project that narrows nothing, which is what `dbprint init` scaffolds, never writes. A `filter` is a predicate rather than a fraction and never materializes.

Where it does fire, the drawn rows are copied once into a session-lifetime temporary table — `CREATE TEMPORARY TABLE <name> AS SELECT * FROM (SELECT * FROM <table> WHERE RANDOM() < p) src`, evaluating `RANDOM()` exactly once — and every statistics statement for that table reads the copy.

`read.tables.materialize: false` never falls back on Redshift: an unseeded `RANDOM()` draw never reads the same rows twice, so a `sample`-scoped table with the write disabled, or a refused write, is refused outright rather than degrading:

```
table 'arboretum.seedbank.germination_trial': connection 'primary' (redshift) reads it
with read.tables.materialize: false, and this adapter's per-statement sampling
construct cannot be seeded into agreement across statements. Set
read.tables.materialize: true for this table's rule,
or narrow with mode: filter instead of a sample.
```

## External tables

A Spectrum or federated external table is listed with `external: true` (SPEC 2.2.20); an external view or Iceberg materialized view is not listed. Unless a matching rule writes its `read.tables.mode`, it is described from the catalog alone, its partition columns published as `physical_layout` — the filter that lets Spectrum skip files. No row estimate exists for it.

Read that way, it is read through Spectrum, which bills the bytes every statement scans. Redshift has no `TABLESAMPLE`, so a `sample` still scans every file once, and `min_rows` and `max_rows` do not apply to it.

## Identifiers

Both path segments are lowercased for the FQN, and the rule check refuses two objects whose physical spellings differ only by case with `case-collides-with-<other>`. A mixed-case **column** keeps its own spelling in `physical_name`, and every statement addresses it by that spelling. Two columns differing only by case — creatable under `enable_case_sensitive_identifier` — refuse their table at column grain (SPEC 1.5.2).

## What it cannot deliver

- **`indexes`** — always empty. No index concept exists on Redshift.
- **`depends_on` on a late-binding view** — always absent. A late-binding view has no `pg_rewrite` entry, and dbprint drops the unresolved row rather than let it collapse into "resolved, reads nothing" — so such a view is missing from the dependency map entirely.
- **Row-count estimate without the `svv_table_info` grant** — see "Read this one" above.
- **Foreign key `on_delete` / `on_update`** — always `NO ACTION`. The FK grammar Redshift exposes carries no referential-action slot.

`is_nullable` on a column has a documented blank third state; only an explicit `NO` / `FALSE` / `F` makes the NOT NULL claim.

## Listed values

An `INTERVAL` column lists its values as Redshift's own `::varchar` text, the text `length` is measured over.

## Column types

| Type | Profiled as | What to know |
|---|---|---|
| `SUPER` | `json` | Attribute names read case-insensitively unless the cluster sets `enable_case_sensitive_super_attribute`, so `userId` and `userid` may read as one part; `types` keys are what `JSON_TYPEOF` returns |
| `GEOMETRY`, `GEOGRAPHY` | `spatial` | — |

## Namespaces

`database` is optional. Omitted, the connection first opens `dev` — which exists on every cluster and namespace and can be neither dropped nor renamed — and reads every local database `svv_redshift_databases` lists, less the system ones. A datashare's consumer database (`database_type = 'shared'`) is not enumerated.

A Redshift session reads one database's catalog alone — cross-database queries do not reach `pg_catalog` or `information_schema`, and `SHOW TABLE` has no database slot — so the connection opens one session per database holding a selected table, with the same credentials, and runs every statement for a table on its own database's session. It holds one connection per database read for the whole run. The grants below are needed in each database profiled, `TEMP` included wherever a table is sampled. A database whose session will not open is skipped with a warning. View dependencies are read only in databases holding a selected table; one whose dependency read fails is named in a warning and its views omit `depends_on` for that run. A configured `database` is matched to the spelling `svv_redshift_databases` stores, so `Dev` reads `dev`.

## The row-count estimate

A table's size, and a sampled table's `row_count`, come from `svv_table_info.estimated_visible_rows`, which excludes rows marked for deletion and not yet vacuumed; a sampled table publishes `row_count_method: approximate`.

## Statement timeout

`run.statement_timeout` is set as the session's `statement_timeout` right after it opens, covering planning, WLM queueing and execution; a lower WLM `max_execution_time` still wins. A statement that exceeds it is cancelled by the server.

## Parallelism

`run.parallelism: N` opens up to N sessions per database the run reads, each on first use, and profiles up to N tables at once. Concurrent statements share the cluster's workload-management queues, so sessions beyond the queue's slots wait there rather than run.

## Reference

- Every configuration key: [Configuration](../CONFIG.md)
- What a `scope` block does to the numbers: [choosing what to profile](../guide/scoping.md) and [SPEC 2.2.8](../format/v1/SPEC.md#228-scope--statistics-over-part-of-a-table)
- What DDL normalization strips and preserves: [SPEC 2.1.3](../format/v1/SPEC.md#213-what-must-be-stripped-per-adapter)
