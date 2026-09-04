# Databricks

```console
$ pip install 'dbprint[databricks]'
```

The extra carries `databricks-sql-connector>=4.2`. Absent driver: `databricks-sql-connector
is not installed. Install dbprint with the [databricks] extra: \`pip install
dbprint[databricks]\`.` A failed connection: `could not open Databricks session for
<server_hostname>: <cause>`.

Fully-qualified names are `schema.table` — the catalog is the connection's own, not part of
the path.

## Credentials

`server_hostname`, `http_path`, `access_token` and `catalog` are all required; there are no
optional keys. All four go straight to the connector. See [Configuration](../CONFIG.md).

## Privileges

```sql
GRANT USE CATALOG, USE SCHEMA, SELECT ON CATALOG my_catalog TO `dbprint_ro`;
```

No `CREATE TABLE`, no `MODIFY`, no ownership. Two prerequisites no `GRANT` covers: the
Databricks SQL access entitlement on the principal, and `CAN USE` on the warehouse the
connection targets. Temporary-table creation needs no privilege at all, but is unsupported on
Dedicated (single-user) access-mode clusters.

### The behavior worth knowing before trusting a table count

Unity Catalog's `information_schema` is privilege-**filtered**, not privilege-**gated**: an
under-privileged principal does not get an error, it gets **fewer rows**. A print that is
silently short of tables looks identical to a database that genuinely has fewer tables.
Check the table count this run reports against what you expect before trusting an empty or
small print.

## Unity Catalog vs. the fallback

`connect()` probes once with `SELECT 1 FROM information_schema.tables WHERE 1 = 0`; **any**
failure — not one specific driver error — is read as "no Unity Catalog", and the connection
is not retried. That flag then routes four operations for the rest of the run:

| On the fallback path (no Unity Catalog) | Reason |
|---|---|
| `relationships` returns `[]` | the legacy path has no constraint surface at all |
| `unique_keys` returns `[]` | same reason |
| Every column reports `nullable: true`, `default: null`, no collation | `DESCRIBE TABLE` carries name/type/comment only |
| Ordinals are positional, not catalog-stated | same limitation |
| A materialized view is not distinguishable from a table | enumeration is `SHOW SCHEMAS` then per-schema `SHOW TABLES` cross-referenced against `SHOW VIEWS`, which does not carry a matview marker |

On Unity Catalog, `table_type` maps eight documented values; a ninth raises
`UnmappedTableType` rather than silently dropping the object. Defaults and per-column
collations come from a best-effort `DESCRIBE TABLE EXTENDED ... AS JSON`: a refused statement
leaves every column on that table without an override, and a collation is published only
where it differs from the table's own default. A foreign key whose target lives in another
catalog keeps its catalog segment, giving a three-segment `target_table`.

## Sampling

| | |
|---|---|
| Construct | `TABLESAMPLE (p PERCENT)` |
| Seeded | yes, and measured reproducible — a table the copy cannot be taken on still reads a stable fraction directly, with a warning |
| `looks_like` sub-draw | `ORDER BY rand(seed) LIMIT`, seeded — `TABLESAMPLE (n ROWS)` is documented as a `LIMIT`, not a random draw, so it is not used for this |

Databricks reproduces the sampled fraction reliably — the measured coherence above the
runtime floor that lets it degrade rather than refuse when the copy is unavailable — but that
guarantee does not extend to row-level agreement between the fraction and the separate
`looks_like` draw; see [PostgreSQL](postgres.md#sampling) for where the two engines that do
promise full row-for-row coherence stand relative to it.

## When dbprint writes

Only one thing makes dbprint write, and only under one condition: `materialize_sample` is on
by default, and it fires **only for a table narrowed to a fraction of its rows**. A fraction
has two sources — a rule's `sample`, and a `max_rows_scanned` ceiling resolving against the
table's size, which is legal at connection and `defaults` level as well as inside a rule. A
project that narrows nothing, which is what `dbprint init` scaffolds, never writes. A
`filter` is a predicate rather than a fraction and never materializes.

Where it does fire, the drawn rows are copied once into a session-lifetime temporary table -
`CREATE TEMPORARY TABLE <name> AS SELECT * FROM (SELECT * FROM <table> TABLESAMPLE (p
PERCENT))` — and every statistics statement for that table reads the copy. Where `CREATE
TEMPORARY TABLE` is unavailable (a Dedicated cluster is the usual cause), dbprint warns and
degrades to reading the un-materialized `TABLESAMPLE` directly, since `SAMPLE_FALLBACK_COHERENT`
is `true` on this engine — the measured reproducibility above is what makes that degrade
safe rather than silently wrong.

## Identifiers

Both path segments are lowercased for the FQN; columns keep their catalog spelling in
`physical_name`. The rule check refuses a case collision the same way as every other
lowercasing adapter. `information_schema` is always skipped as a system schema.

## What it cannot deliver, on either path

- **`indexes`** — always empty. No index concept exists on Databricks.
- **`depends_on`** — always absent on every view. `view_table_usage` does not exist,
  `view_definition` needs ownership the profiling role does not have, and lineage stays
  silent for a view nothing has queried yet.
- **A schema's own declared default collation** — `default_collation` reports the session
  value or `UTF8_BINARY`; no catalog surface publishes what a schema itself declares, and the
  value governs DML rather than comparison semantics.

## Row count and cost

`DESCRIBE TABLE EXTENDED ... AS JSON` reads `statistics.num_rows`; any failure returns `None`
rather than falling back to `COUNT(*)` — a silent full scan being judged worse than an absent
estimate. Every statement here runs on the connection's SQL warehouse, so the cost is
warehouse compute time rather than bytes billed. DDL comes from `SHOW CREATE TABLE`, and is
not a byte-exact round trip: Databricks filters table properties out of its own output.

## Reference

- Every configuration key: [Configuration](../CONFIG.md)
- What a `scope` block does to the numbers: [choosing what to profile](../guide/scoping.md)
  and [SPEC 2.2.8](../format/v1/SPEC.md#228-scope--statistics-over-part-of-a-table)
- What DDL normalization strips and preserves:
  [SPEC 2.1.3](../format/v1/SPEC.md#213-what-must-be-stripped-per-adapter)
