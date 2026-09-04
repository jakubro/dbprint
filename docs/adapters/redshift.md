# Redshift

```console
$ pip install 'dbprint[redshift]'
```

The extra carries `redshift-connector>=2.1`. Absent driver: `redshift-connector is not
installed. Install dbprint with the [redshift] extra: \`pip install dbprint[redshift]\`.` A
failed connection: `could not connect to Redshift at <host>:<port>/<database> as <user>:
<cause>`. The connection opens with `autocommit = True`; the adapter applies no session-level
read-only setting.

Fully-qualified names are `schema.table`.

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

### Read this one: `svv_table_info` is superuser-only by default

`SVV_TABLE_INFO` is visible only to superusers, and `GRANT SELECT ON svv_table_info TO <user>`
is what permits an ordinary user to query it. `GRANT ROLE sys:monitor` is a second route to
the same access. `SYSLOG ACCESS UNRESTRICTED` is **not** a substitute — it does not open
superuser-visible system views.

Without that grant, `estimate_row_count` catches the failure and returns `-1` for every
table, silently: the run warns `no row-count estimate for '<fqn>'; rules carrying
\`min_rows\` or a \`max_rows_scanned\` ceiling do not apply to it`, but nothing else in the
run tells a reader that the whole size-gating system just went dark. An empty table is
missing from `SVV_TABLE_INFO` entirely, rather than reported as zero, so its absence there is
not itself a sign of a missing grant.

## Sampling

| | |
|---|---|
| Construct | `RANDOM() < p` in a wrapping subquery |
| Seeded | no — `RANDOM()` carries no seed at all on Redshift |
| `looks_like` sub-draw | oversamples with `RANDOM()`; the distinct set is hash-ordered either way |

Because the fraction cannot be seeded, coherence across a sampled table depends entirely on
the materialized copy below — there is no population-level fallback guarantee the way
duckdb or PostgreSQL have.

## When dbprint writes

Only one thing makes dbprint write, and only under one condition: `materialize_sample` is on
by default, and it fires **only for a table narrowed to a fraction of its rows**. A fraction
has two sources — a rule's `sample`, and a `max_rows_scanned` ceiling resolving against the
table's size, which is legal at connection and `defaults` level as well as inside a rule. A
project that narrows nothing, which is what `dbprint init` scaffolds, never writes. A
`filter` is a predicate rather than a fraction and never materializes.

Where it does fire, the drawn rows are copied once into a session-lifetime temporary table -
`CREATE TEMPORARY TABLE <name> AS SELECT * FROM (SELECT * FROM <table> WHERE RANDOM() < p) AS
dbprint_scoped`, evaluating `RANDOM()` exactly once — and every statistics statement for that
table reads the copy.

`materialize_sample: false` never falls back on Redshift: `SAMPLE_FALLBACK_COHERENT` is
`false` here, so a `sample`-scoped table with the write disabled, or a refused write, is
refused outright rather than degrading:

```
table 'seedbank.germination_trial': connection 'primary' (redshift) sets
materialize_sample: false, and this adapter's per-statement sampling construct cannot be
seeded into agreement across statements. Set materialize_sample: true for this connection,
or narrow with a filter instead of a sample fraction.
```

## Identifiers

Both path segments are lowercased for the FQN, and the rule check refuses two objects whose
physical spellings differ only by case with `case-collides-with-<other>`. A mixed-case
**column** keeps its own spelling in `physical_name`; addressing one for the `looks_like`
draw re-reads the catalog's spelling under `enable_case_sensitive_identifier`, raising `no
column named <name> (case-insensitive) on <fqn>` when there is no match.

## What it cannot deliver

- **`indexes`** — always empty. No index concept exists on Redshift.
- **`depends_on` on a late-binding view** — always absent. A late-binding view has no
  `pg_rewrite` entry, and the code drops the unresolved row rather than let it collapse into
  "resolved, reads nothing" — so such a view is missing from the dependency map entirely.
- **Row-count estimate without the `svv_table_info` grant** — see "Read this one" above.
- **Foreign key `on_delete` / `on_update`** — always `NO ACTION`. The FK grammar Redshift
  exposes carries no referential-action slot.

`is_nullable` on a column has a documented blank third state; only an explicit `NO` / `FALSE`
/ `F` makes the NOT NULL claim.

## Reference

- Every configuration key: [Configuration](../CONFIG.md)
- What a `scope` block does to the numbers: [choosing what to profile](../guide/scoping.md)
  and [SPEC 2.2.8](../format/v1/SPEC.md#228-scope--statistics-over-part-of-a-table)
- What DDL normalization strips and preserves:
  [SPEC 2.1.3](../format/v1/SPEC.md#213-what-must-be-stripped-per-adapter)
