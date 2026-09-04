# ClickHouse

```console
$ pip install 'dbprint[clickhouse]'
```

The extra carries `clickhouse-connect>=1.7`. Absent driver: `clickhouse-connect is not
installed. Install dbprint with the [clickhouse] extra: \`pip install dbprint[clickhouse]\`.`
A failed connection: `could not connect to ClickHouse at <host>:<port>/<database> as <user>:
<cause>`. There is no session-level read-only setting the adapter applies — what a connection
can do is entirely the connected user's own grants.

Fully-qualified names are `database.table` — two segments; ClickHouse has no schema tier.

## Privileges

`system.tables` and `system.columns` are named exceptions to
`select_from_system_db_requires_grant` and need no grant at all.
`system.data_skipping_indices` is not an exception:

```sql
GRANT SELECT ON system.data_skipping_indices TO dbprint_ro;
GRANT SELECT ON my_db.* TO dbprint_ro;

-- Only when a table is narrowed to a fraction; see "When dbprint writes".
GRANT CREATE ARBITRARY TEMPORARY TABLE ON *.* TO dbprint_ro;
```

| Privilege | On | Needed for |
|---|---|---|
| `SELECT` | `system.data_skipping_indices` | index metadata; `system.tables` and `system.columns` need no grant |
| `SELECT` | each table profiled | DDL and statistics — note this grant is enforced at **column** level, so a column-list grant makes `SELECT *` return nothing rather than error |
| `CREATE ARBITRARY TEMPORARY TABLE` | GLOBAL level | the sampled-table copy only, and the narrower `CREATE TEMPORARY TABLE` is not enough — the copy declares `ENGINE = MergeTree`, and that distinction is settled by the server source, not by any documentation page |

Independently of every grant above, the `readonly` server setting gates the write: `readonly
= 1` refuses the sampled copy outright; `readonly = 2` permits it. Two of ClickHouse's own
documentation pages disagree on whether the `system` database is always readable — the
server-settings page is the more specific and more recently maintained, and is the one that
matches what `system.tables`/`system.columns` actually do here.

### What an under-privileged user does

Missing `SELECT` on `system.data_skipping_indices` fails index enumeration for that table
alone; missing `SELECT` on the table itself fails DDL and statistics together, since both
read the same grant. A `readonly = 1` connection reaches every catalog read and fails only
where `materialize_scope` tries to write — see "When dbprint writes" below.

## Sampling

| | |
|---|---|
| Construct | `SAMPLE p` against a declared `SAMPLE BY` key |
| Seeded | no — determinism depends on the table declaring a `SAMPLE BY` key at creation, which is out of dbprint's control |
| `looks_like` sub-draw | fixed-size `SAMPLE n`, degrading to a direct scan when the table declares no sampling key |

`list_tables` reads `system.tables.sampling_key` in the same statement that enumerates, and
the adapter caches an FQN-to-samplable map from it. A sampled Snowflake- or Postgres-style
row-for-row guarantee does not apply here: coherence with the rest of a sampled ClickHouse
profile is population-level only.

## When dbprint writes

Only one thing makes dbprint write, and only under one condition: `materialize_sample` is on
by default, and it fires **only for a table narrowed to a fraction of its rows**. A fraction
has two sources — a rule's `sample`, and a `max_rows_scanned` ceiling resolving against the
table's size, which is legal at connection and `defaults` level as well as inside a rule. A
project that narrows nothing, which is what `dbprint init` scaffolds, never writes. A
`filter` is a predicate rather than a fraction and never materializes.

Where it does fire, dbprint checks the samplable map from `list_tables` **before** touching
the server. A table with no declared `SAMPLE BY` key (or that is not a MergeTree-family
table) is refused outright:

```
table 'my_db.germination_trial': connection 'primary' (clickhouse) could not materialize its
sample of 0.25 (table 'my_db.germination_trial' declares no SAMPLE BY key (or is not a
MergeTree-family table), so ClickHouse's SAMPLE clause is not available on it at any
fraction.), and this adapter's per-statement fallback cannot be seeded into agreement across
statements. Narrow with a filter instead of a sample fraction.
```

Where the table is samplable, the copy is created as
`CREATE TEMPORARY TABLE <name> ENGINE = MergeTree ORDER BY tuple() AS SELECT * FROM (SELECT *
FROM <table> SAMPLE p)`, session-scoped, dropped as soon as the table finishes.
`materialize_sample: false` never falls back on ClickHouse either — `SAMPLE_FALLBACK_COHERENT`
is `false` here, so an unmaterialized `sample` scope on a `sample`-narrowed table is refused
the same way, before any statement runs:

```
table 'my_db.germination_trial': connection 'primary' (clickhouse) sets
materialize_sample: false, and this adapter's per-statement sampling construct cannot be
seeded into agreement across statements. Set materialize_sample: true for this connection,
or narrow with a filter instead of a sample fraction.
```

On ClickHouse, `materialize_sample` therefore decides whether a `sample`-scoped table is
profiled at all, not merely how faithfully.

## Identifiers

Both path segments go into the FQN **verbatim** — `system.*` compares case-sensitively, so
folding would address a table that does not exist. The path-segment rule is
`^[a-z0-9_][a-z0-9_.-]*$`, so **any capital letter in a database or table name is refused
outright** with `contains-unsafe-character`, and there is nothing to fold: the run fails for
that object rather than addressing a lowercased path. ClickHouse is also the only adapter
whose rule check carries **no** case-collision branch, because two names differing only by
case stay two distinct FQNs here rather than colliding on one path. Column names fold to
their lowercase map key and keep their catalog spelling in `physical_name`.

## What it cannot deliver

- **`relationships`** — always empty. `REFERENTIAL_CONSTRAINTS` is documented permanently
  empty, and a `FOREIGN KEY` clause in `CREATE TABLE` is accepted and silently discarded.
- **`unique_keys`** — always empty. ClickHouse's `PRIMARY KEY` admits duplicate values, so it
  is not a declared-unique group under SPEC 2.6.7.
- **`depends_on`** — always absent on every view. The dependency tables answer only the
  reverse edge, and only for materialized views.
- **Index `columns`** — always empty for a data-skipping index. One covers an expression
  rather than a column list, so `columns` is empty rather than guessed.
- **Column `collation`** — always the fixed constant `binary`. ClickHouse has no server-side
  collation model.

## Reference

- Every configuration key: [Configuration](../CONFIG.md)
- What a `scope` block does to the numbers: [choosing what to profile](../guide/scoping.md)
  and [SPEC 2.2.8](../format/v1/SPEC.md#228-scope--statistics-over-part-of-a-table)
- What DDL normalization strips and preserves:
  [SPEC 2.1.3](../format/v1/SPEC.md#213-what-must-be-stripped-per-adapter)
