# dbprint

A print is a portable, git-committed snapshot of a database's structure and column-level data distributions, read offline by humans, AI coding agents and CI.

An agent already reads your migrations and your ORM models, so it gets the shape right. What it spends tool calls rediscovering every session is the data: how many distinct values `biome` has, whether `parent_taxon_id` is ever null, which of two columns a join actually uses. And what no database can tell it at all is the part a colleague would have said in thirty seconds — which column is authoritative, which one has been dead since the migration.

dbprint measures the first half and gives you somewhere to write the second. Both land in your repository as plain text that needs no connection and no credentials to read.

One column, as dbprint writes it, from `prints/<connection>/<schema>/<table>/statistics.yaml`:

```yaml
row_count: 300
columns:
  parent_taxon_id:
    sql_type: integer
    nullable: true
    null_count: 4
    null_rate: 0.013333
    classification: foreign_key_candidate
    cardinality: 12
    cardinality_ratio: 0.04
    cardinality_method: exact
    values_coverage: 1.0
    distribution: uniform
    # values: the full list of 12, omitted here
```

`classification` is dbprint's read of what kind of column this is, and it decides which measurements the column carries; `foreign_key_candidate` means the column looks like a key pointing at another table.

The files are plain text with a published schema, so they stay readable — and diffable in review — whether or not dbprint is still installed.

## Two ways in

**You have a database and want a print of it.** Start at [your first print](start.md), then pick the page for your engine — [duckdb](adapters/duckdb.md), [PostgreSQL](adapters/postgres.md), [MySQL](adapters/mysql.md), [ClickHouse](adapters/clickhouse.md), [Redshift](adapters/redshift.md), [Snowflake](adapters/snowflake.md), [Databricks](adapters/databricks.md) or [BigQuery](adapters/bigquery.md) — for the privileges it needs and what it can promise about a sampled table. The guides cover [choosing what to profile](guide/scoping.md), [withholding cell values](guide/redaction.md), [annotating a print](guide/annotations.md), [tracking drift](guide/drift.md), [gating CI](guide/ci.md), [browsing a print](guide/browsing.md), [giving a print to an agent](guide/agents.md) and [what to do when something goes wrong](guide/troubleshooting.md).

**You want to read or emit the format.** The [format specification](format/v1/SPEC.md) is normative and self-contained. [Emitting a conforming print](producers.md) is the entry point for a tool that is not dbprint: the schemas at their canonical addresses, the validator as a public API, and what conformance does and does not certify. [Conformance codes](reference/conformance.md) lists every code a validation can raise.
