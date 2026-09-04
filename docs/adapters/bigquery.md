# BigQuery

```console
$ pip install 'dbprint[bigquery]'
```

The extra carries `google-cloud-bigquery>=3.27`. Absent driver: `google-cloud-bigquery is not
installed. Install dbprint with the [bigquery] extra: \`pip install dbprint[bigquery]\`.` A
failed connection: `could not open a BigQuery session for project <project>, dataset
<dataset>: <cause>`.

Fully-qualified names are `dataset.table`.

## Credentials

`project` and `dataset` are required. **There is no password key.** Credentials resolve
through the client's own Application Default Credentials unless `credentials_file` names a
service-account key, in which case the file loads through `google.oauth2.service_account`.
Read-only is the connected principal's own IAM role — the adapter enforces nothing itself.
See [Configuration](../CONFIG.md).

## Privileges

Two roles, not one, because every catalog read is a query job:

```
roles/bigquery.dataViewer   -- reading structure and computing statistics
roles/bigquery.jobUser      -- required separately; no data role carries bigquery.jobs.create
roles/bigquery.dataEditor   -- only if sampling: the copy is a real table in the profiled dataset
```

`roles/bigquery.dataViewer` carries `bigquery.tables.getData`; `roles/bigquery.metadataViewer`
does not, so a metadata-only principal reads structure and computes no statistics at all. A
reader who never samples needs strictly less than one who does — the `dataEditor` grant is
needed only for the `CREATE OR REPLACE TABLE` / `DROP TABLE` pair described below.

### Cost

`INFORMATION_SCHEMA` queries carry a 10 MB minimum each, are never cached, and the adapter
issues several per table — so a `generate` run bills a floor well above what the data itself
costs. This is the one adapter page that has to say that plainly: every catalog read here is
a billed query, not a free metadata lookup.

## Sampling

| | |
|---|---|
| Construct | `TABLESAMPLE SYSTEM (p PERCENT)` |
| Seeded | no — BigQuery's `TABLESAMPLE` takes no seed at all |
| `looks_like` sub-draw | cannot be seeded either; orders by the same seeded hash the final list ships under |

Because the fraction cannot be seeded, coherence across a sampled table depends entirely on
the materialized copy below — like ClickHouse and Redshift, BigQuery refuses a `sample`-scoped
table when the copy is unavailable rather than publish a `statistics.yaml` whose fields
describe different rows.

## When dbprint writes — and where the copy lives

Only one thing makes dbprint write, and only under one condition: `materialize_sample` is on
by default, and it fires **only for a table narrowed to a fraction of its rows**. A fraction
has two sources — a rule's `sample`, and a `max_rows_scanned` ceiling resolving against the
table's size, which is legal at connection and `defaults` level as well as inside a rule. A
project that narrows nothing, which is what `dbprint init` scaffolds, never writes. A
`filter` is a predicate rather than a fraction and never materializes.

BigQuery is the one adapter where the copy is **not** a session-scoped temporary table — it
is a real table in the profiled dataset:

```sql
CREATE OR REPLACE TABLE <dataset>.dbprint_sample_<...>
OPTIONS(expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 6 HOUR))
AS SELECT * FROM (SELECT * FROM <table> TABLESAMPLE SYSTEM (p PERCENT))
```

The profiling role needs create rights in that dataset (`roles/bigquery.dataEditor` above),
and `CREATE OR REPLACE` means an orphaned copy from an earlier interrupted run cannot wedge a
later one. The scratch prefix is excluded from enumeration, so a copy is never profiled as a
table of its own. Release is `DROP TABLE IF EXISTS`; when the drop itself fails, the run
warns that the copy outlives this run and is not session-scoped — the 6-hour expiration is
what bounds its storage in that case, not any cleanup dbprint performs.

`materialize_sample: false` never falls back on BigQuery: `SAMPLE_FALLBACK_COHERENT` is
`false` here, so a `sample`-scoped table with the write disabled is refused before any
statement runs, the same as on ClickHouse or Redshift.

## Identifiers

BigQuery is case-sensitive while the format addresses objects by lowercased paths, so the
adapter carries an identity object holding the physical `(dataset, table)` and a
lowercase-to-physical column map. `list_tables` is the only point where both forms are
visible and **must run first** — looking up an unlisted table raises `UnknownTable` rather
than falling back to a lowercased path, which would filter the catalog for a name that does
not exist. Dataset and table names are folded for the artifact path; the physical spelling is
kept for every query. The rule check refuses a case collision the same way as every other
lowercasing adapter.

## What it cannot deliver

- **`indexes`** — always empty. Search and vector indexes are neither secondary indexes nor a
  SQL join target.
- **`depends_on`** — always absent on every view. There is no view-dependency catalog, only
  `VIEWS.view_definition`, so the field is omitted rather than guessed from DDL text.
- **`unique_keys` beyond the primary key** — BigQuery has no `UNIQUE` constraint type.
- **Column `default`** — always `null`. `column_default` is absent from
  `INFORMATION_SCHEMA.COLUMNS`.
- **Foreign key `on_delete` / `on_update`** — always `NO ACTION`. `enforced` is documented
  "Only `NO`".

Three catalog reads degrade rather than fail when the connection cannot see a column:
`TABLES.ddl`, `COLUMNS.collation_name`, and `COLUMNS.clustering_ordinal_position`. Losing the
third means clustering is not detected, and a clustered table may publish `partition` instead
— worth knowing if physical layout looks wrong on a table you know is clustered.
`COLUMN_FIELD_PATHS` degrades the same way, leaving every column comment absent.

## Reference

- Every configuration key: [Configuration](../CONFIG.md)
- What a `scope` block does to the numbers: [choosing what to profile](../guide/scoping.md)
  and [SPEC 2.2.8](../format/v1/SPEC.md#228-scope--statistics-over-part-of-a-table)
- What DDL normalization strips and preserves:
  [SPEC 2.1.3](../format/v1/SPEC.md#213-what-must-be-stripped-per-adapter)
