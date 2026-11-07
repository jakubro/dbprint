# dbprint

**dbprint measures a database and writes each table's DDL, its joins and per-column statistics as plain text to commit beside the code, next to the notes on what the values mean that a person or an agent writes.** A coding agent reads both offline, with no database connection.

Migrations and models record a database's shape, not what its values mean: that `revenue_cents` is gross and every report uses the net column, that `status` is spelled two ways, how often `orders` joins `shipments` one-to-many, that `legacy_uid` has been unused since a migration. An agent without a database connection cannot find these out. An agent with one re-runs the same queries in every session, at a cost in warehouse time, budget and tokens, and can still misread the result: a count does not say that a code is unused or that a column is gross.

What dbprint does, and what an agent adds:

- **It measures the database once.** Each table's DDL, its joins — declared, and inferred where the schema declares none — and per-column statistics: null shares, complete value lists with every spelling, ranges, distributions, value shapes and more. On PostgreSQL, MySQL, Snowflake, BigQuery, Databricks, Redshift, ClickHouse, duckdb or SQLite.
- **An agent writes down what the numbers mean.** `dbprint annotate` hands an agent one table's measurements and the questions they leave open — what a code means, what a sentinel date stands for, which rows a report leaves out — and the agent answers them from the project's code and read-only queries, into files dbprint never overwrites. `dbprint annotate --check` flags a note the data contradicts.
- **Every later session reads it offline.** Through an MCP server, a packaged skill or `dbprint context`, with no database connection and no credentials at the agent. Because it is text in git, a schema or data change shows up in review, and `dbprint check` gates it in CI.

> **With dbprint, agents get more answers right, for less.** In our tests across multiple codebases, coding agents reading an annotated print got 80% fewer answers wrong than without one, at 17% lower cost per correct answer, and 90% fewer on questions whose catch only the print stated.

## Example: what `dbprint context` prints

An excerpt of `dbprint context --purpose query` on a table from the [reference print](format/v1/examples/README.md) — the fragment an agent gets before it writes SQL:

```markdown
# Table: arboretum.seedbank.germination_trial  (900 rows, 8 columns)

## Column values

| Column | Values (count) | Coverage | Nulls |
|---|---|---|---|
| medium | 'moist filter paper' (176), 'sterile grit' (176), 'vermiculite mix' (176), 'sand tray' (176) {'sand tray' (151), 'Sand Tray' (25)}, 'agar medium' (175), 'control' (21) = 'No growth medium: a no-treatment control group, its germinated_count compared against ambient conditions rather than a physical medium.' | 100% - the list is the whole domain |  |

## Data dictionary

> One row per germination attempt run against a batch drawn from an accession.

## Joins
- accession_id -> arboretum.seedbank.accession.accession_id (declared)
- collector_id -> arboretum.seedbank.collector.collector_id (inferred)
```

Three things here no migration states: `medium` holds exactly these values and no others, `sand tray` is stored in two spellings, so a filter on one misses 25 rows, and `control` is not a medium at all. dbprint measured the first two; a person or an agent wrote the third once, and every later session reads it.

The context is rendered from plain YAML files with a published schema, which stay readable, and diffable in review, without dbprint installed.

## Quickstart

```console
$ pip install 'dbprint[postgres]'
$ dbprint init
$ dbprint generate
$ dbprint annotate my_db.public.orders | claude -p
$ git add .dbprint.yaml prints/
```

`init` writes `.dbprint.yaml` for the project and a credentials file under `~/.dbprint/`; fill in the connection and the tables to read before `generate`. Replace `postgres` with the extra for the database's engine. [Your first print](start.md) walks the same steps with every file and output explained, and [annotating a print](guide/annotations.md) covers what the agent writes.

## What dbprint does to the database

- **It reads.** A role that can `SELECT` the profiled tables is enough; each engine's adapter page lists the exact grants. The one write is a temporary copy of a sampled table's drawn rows, gone when the session ends (on BigQuery, when its expiration passes), and only for a sampled table — [`read.tables.materialize`](CONFIG.md) turns it off.
- **It publishes real values.** Value lists, ranges and percentiles are cell values, and they are committed to the repository. Nothing is withheld by default: [redaction](guide/redaction.md) masks, drops or hashes them by column name, by detected sensitivity (`personal_name`, `national_id`, ...) or by shape, keeping every count.
- **It costs one profiling run, then whatever later runs re-read.** A table is re-read when its print is older than its freshness threshold or its settings changed; sampling, row filters and catalog-only reads bound the cost on a warehouse — see [choosing what to profile](guide/scoping.md).

## Documentation

**Producing a print.** [Your first print](start.md) is the first step, and each engine's page — [duckdb](adapters/duckdb.md), [PostgreSQL](adapters/postgres.md), [MySQL](adapters/mysql.md), [ClickHouse](adapters/clickhouse.md), [Redshift](adapters/redshift.md), [Snowflake](adapters/snowflake.md), [Databricks](adapters/databricks.md), [BigQuery](adapters/bigquery.md) or [SQLite](adapters/sqlite.md) — lists the privileges it needs and what it promises about a sampled table. The guides cover [choosing what to profile](guide/scoping.md), [withholding cell values](guide/redaction.md), [annotating a print](guide/annotations.md), [tracking drift](guide/drift.md), [gating CI](guide/ci.md), [browsing a print](guide/browsing.md), [giving a print to an agent](guide/agents.md) and [what to do when something goes wrong](guide/troubleshooting.md).

**Reading or emitting the format.** The [format specification](format/v1/SPEC.md) is normative and self-contained. [Emitting a conforming print](producers.md) is the entry point for a tool that is not dbprint: the schemas at their canonical addresses, the validator as a public API, and what conformance does and does not certify. [Conformance codes](reference/conformance.md) lists every code a validation can raise.
