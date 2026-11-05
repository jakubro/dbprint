# dbprint

[![PyPI](https://img.shields.io/pypi/v/dbprint.svg)](https://pypi.org/project/dbprint/) [![Docs](https://img.shields.io/badge/docs-jakubro.github.io-blue.svg)](https://jakubro.github.io/dbprint/) [![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/jakubro/dbprint/blob/main/LICENSE) [![Status](https://img.shields.io/badge/status-actively_developed-green.svg)](#) [![dbprint MCP server – quality and maintenance score on Glama](https://glama.ai/mcp/servers/jakubro/dbprint/badges/score.svg)](https://glama.ai/mcp/servers/jakubro/dbprint) <!-- [![M8ven Score](https://m8ven.ai/badge/mcp/jakubro-dbprint-1jc05n?v=0571c23784758aea08f9efbba4a8995f)](https://m8ven.ai/mcp/jakubro-dbprint-1jc05n?s=readme) -->

[Example](#example) · [Quickstart](#quickstart) · [Using dbprint](#using-dbprint) · [Compared with other tools](#compared-with-other-tools) · [The format](#the-format)

**dbprint measures a database and writes each table's DDL, its joins and per-column statistics as plain text to commit beside the code, next to the notes on what the values mean that a person or an agent writes.** A coding agent reads both offline, with no database connection.

Migrations and models record a database's shape, not what its values mean: that `revenue_cents` is gross and every report uses the net column, that `status` is spelled two ways, how often `orders` joins `shipments` one-to-many, that `legacy_uid` has been unused since a migration. An agent without a database connection cannot find these out. An agent with one re-runs the same queries in every session, at a cost in warehouse time, budget and tokens, and can still misread the result: a count does not say that a code is unused or that a column is gross.

What dbprint does, and what an agent adds:

- **It measures the database once.** Each table's DDL, its joins — declared, and inferred where the schema declares none — and per-column statistics: null shares, complete value lists with every spelling, ranges, distributions, value shapes and more. On PostgreSQL, MySQL, Snowflake, BigQuery, Databricks, Redshift, ClickHouse, duckdb or SQLite.
- **An agent writes down what the numbers mean.** `dbprint annotate` hands an agent one table's measurements and the questions they leave open — what a code means, what a sentinel date stands for, which rows a report leaves out — and the agent answers them from the project's code and read-only queries, into files dbprint never overwrites. `dbprint annotate --check` flags a note the data contradicts.
- **Every later session reads it offline.** Through an MCP server, a packaged skill or `dbprint context`, with no database connection and no credentials at the agent. Because it is text in git, a schema or data change shows up in review, and `dbprint check` gates it in CI.

> **With dbprint, agents get more answers right, for less.** In our tests across multiple codebases, coding agents reading an annotated print got 80% fewer answers wrong than without one, at 17% lower cost per correct answer, and 90% fewer on questions whose catch only the print stated.

## Example

An excerpt of `dbprint context --purpose query` on a table from [the reference print](https://github.com/jakubro/dbprint/tree/main/docs/format/v1/examples) in this repo — the fragment an agent gets before it writes SQL:

```markdown
# Table: arboretum.seedbank.germination_trial  (900 rows, 8 columns)

## Column values

| Column | Values (count) | Coverage | Nulls |
|---|---|---|---|
| medium | 'moist filter paper' (176), 'sterile grit' (176), 'vermiculite mix' (176), 'sand tray' (176) {'sand tray' (151), 'Sand Tray' (25)}, 'agar medium' (175), 'control' (21) = 'No growth medium: a no-treatment control group, its germinated_count compared against ambient conditions rather than a physical medium.' | 100% - the list is the whole domain |  |

## Data dictionary

One row per germination attempt run against a batch drawn from an accession.

## Joins
- accession_id -> arboretum.seedbank.accession.accession_id (declared)
- collector_id -> arboretum.seedbank.collector.collector_id (inferred)
```

Three things here no migration states: `medium` holds exactly these values and no others, `sand tray` is stored in two spellings, so a filter on one misses 25 rows, and `control` is not a medium at all. dbprint measured the first two; a person or an agent wrote the third once, and every later session reads it.

The same command runs with no database and no configuration, given only [uv](https://docs.astral.sh/uv/):

```sh
uvx dbprint context arboretum.seedbank.germination_trial --purpose query \
  --project https://github.com/jakubro/dbprint/blob/main/docs/format/v1/examples/production/.dbprint.yaml
```

## Quickstart

Requires **Python 3.13+**. Install with the driver extra for the database's engine:

```sh
uv tool install 'dbprint[postgres]'     # or [mysql], [snowflake], [duckdb], [clickhouse], [redshift], [databricks], [bigquery]; SQLite needs none
```

Add `[mcp]`, `[docs]` or `[dbt]` in the same install — `uv tool install 'dbprint[postgres,mcp]'`, or `'dbprint[all]'` for every extra — since a later install replaces the earlier one.

The Postgres adapter shells out to **`pg_dump`** for DDL, so the PostgreSQL client binaries must be on `PATH`.

Scaffold a project in the repository root:

```sh
dbprint init
```

That writes `.dbprint.yaml` (commit it), creates `prints/`, and writes a credentials template to `~/.dbprint/connections.yaml` — outside the repo, so secrets never sit next to the config.

Put the connection details in the credentials file:

```yaml
# ~/.dbprint/connections.yaml
primary:
  host: localhost
  port: 5432
  database: my_db
  user: dbprint_ro
  password: change_me
```

and tell `.dbprint.yaml` which adapter to use and which tables to read:

```yaml
# .dbprint.yaml
connections:
  primary:
    adapter: postgres
    auto: true
    tables:
      include:
        - "*.public.*"
```

Credentials resolve from `DBPRINT_<CONNECTION>_<KEY>` environment variables first, then `~/.dbprint/connections.yaml`, then a project-root `.env`. Every key of both files is in [`docs/CONFIG.md`](https://github.com/jakubro/dbprint/blob/main/docs/CONFIG.md).

Profile it:

```sh
dbprint generate
```

Commit the result beside the code. Then inspect it with no database in reach:

```sh
dbprint list
```

Then have an agent annotate a table — the brief carries the [annotation skill](https://github.com/jakubro/dbprint/blob/main/docs/examples/skill/dbprint-annotate.md)'s steps, so any agent CLI can follow it:

```sh
dbprint annotate my_db.public.orders | claude -p
```

and [connect an agent](#connecting-an-agent) to the result.

## Using dbprint

The full documentation is on [the dbprint site](https://jakubro.github.io/dbprint/).

### What dbprint does to the database

- **It reads.** A role that can `SELECT` the profiled tables is enough; each engine's adapter page, linked from [your first print](https://jakubro.github.io/dbprint/start/), lists the exact grants. The one write is a temporary copy of a sampled table's drawn rows, gone when the session ends (on BigQuery, when its expiration passes), and only for a sampled table — [`read.tables.materialize`](https://github.com/jakubro/dbprint/blob/main/docs/CONFIG.md) turns it off.
- **It publishes real values.** Value lists, ranges and percentiles are cell values, and they are committed to the repository. Nothing is withheld by default: [redaction](https://jakubro.github.io/dbprint/guide/redaction/) masks, drops or hashes them by column name, by detected sensitivity (`personal_name`, `national_id`, ...) or by shape, keeping every count. A password or key written into the schema itself — a view body, a default, a comment — is masked in `ddl.sql` on every run, whatever the rules say.
- **It costs one profiling run, then whatever later runs re-read.** A table is re-read when its print is older than its freshness threshold or its settings changed; sampling, row filters and catalog-only reads bound the cost on a warehouse — see [choosing what to profile](https://jakubro.github.io/dbprint/guide/scoping/).

### What a print looks like

Under the context an agent reads are plain YAML files. An excerpt of one table's `statistics.yaml`, some fields elided, from the same reference print:

```yaml
row_count: 2500
columns:
  catalogue_url:
    sql_type: character varying(200)
    nullable: false
    null_rate: 0.0
    classification: text
    cardinality: 2500
    cardinality_ratio: 1.0
    inferred:
      looks_like: url
      sampled: 1000
      matched: 1000
      candidate_key: true
    values:
    - value: https://specimens.example.org/accession/1
      count: 1
    - value: https://specimens.example.org/accession/10
      count: 1
    # ... 28 more
    values_coverage: 0.012      # top 30 of 2500 - a value list, not a claim of completeness
    distribution: long_tail

  provenance_country:
    sql_type: character(2)
    classification: categorical
    cardinality: 10
    inferred:
      looks_like: country_code
      sampled: 10
      matched: 10
    values:
    - value: AU
      count: 250
    - value: CA
      count: 250
    - value: DE
      count: 250
    - value: FR
      count: 250
    - value: GB
      count: 250
    # ... 5 more
    values_coverage: 1.0        # the list is the whole domain, not a sample
    distribution: uniform

  received_at:
    sql_type: timestamp(0) with time zone
    classification: temporal
    range:
      min: '2018-01-08T00:00:00Z'
      max: '2024-11-28T00:00:00Z'
      span_days: 2516
    percentiles:
      p01: '2018-02-09T00:00:00Z'
      p25: '2019-10-02T00:00:00Z'
      p50: '2021-06-19T00:00:00Z'
      p75: '2023-03-05T00:00:00Z'
      p99: '2024-10-25T00:00:00Z'
```

`values_coverage: 1.0` marks those ten values as the *entire* domain, not the top ten, so an eleventh value is ruled out without querying the database. (When a table is profiled from a sample rather than in full, coverage is measured over the rows that were actually read — see [Scoped profiling](#further-features) below.)

`inferred` is what dbprint concluded rather than read: `looks_like: country_code` is a verdict, and `sampled`/`matched` are the evidence for it, so a shape confirmed by ten values never looks like one confirmed by ten thousand.

Alongside it, `relationships.yaml` carries the join graph:

```yaml
refers_to:
- column:
  - collector_id
  target_table: arboretum.seedbank.collector
  target_column:
  - collector_id
  on_delete: RESTRICT
  detection: declared      # or `inferred`, when dbprint guessed the edge from naming
  observed:
    fanout_avg: 6.25
    target_coverage: 1.0
```

Those two files are measured. Notes are written beside them, by a person or an agent, in the same directory and the same commit — `statistics.annotations.yaml` for a column:

```yaml
columns:
  collector_id:
    note: >-
      FK to collector.collector_id. Field collectors are affiliated with a
      partner institution; collector.institution names it.
```

and `description.md` for table-level narrative that no statistic can express.

dbprint never writes to either file, and both are read alongside the statistics — but on anything the statistics can answer, the statistics win; the narrative is authoritative only on what a number can't.

### Print directory layout

```
prints/<connection>/
  manifest.yaml                    # inventory, freshness, per-table thresholds
  reading.md                       # generated guide - how to read the rest of the print
  diff.yaml                        # machine-readable drift from the last run
  manifest.annotations.yaml        # optional, connection-level notes you write
  <namespace>/<table>/             # one directory per FQN segment, e.g. arboretum/seedbank/accession/
    ddl.sql                        # native DDL, normalized for stable diffs
    statistics.yaml                # column distributions
    relationships.yaml             # declared + inferred foreign keys
    description.md                 # optional, you write it
    statistics.annotations.yaml    # optional, per-column notes you write
    relationships.annotations.yaml # optional, per-edge notes you write
```

`reading.md` is written fresh on every run: it teaches a consumer which fields to trust for what, so a print explains itself to an agent that has never seen the format.

dbprint never overwrites the four files a person or an agent writes — `description.md` and the three `.annotations.yaml` — and all four are folded into what agents read.

### Commands

| Command | What it does |
|---|---|
| `dbprint init` | Scaffold `.dbprint.yaml` + `prints/` + a credentials template |
| `dbprint config explain` | Print the profile one table resolves to, each value commented with the default, connection or rule that set it |
| `dbprint generate` | Profile the database; write the print and `diff.yaml` |
| `dbprint list` | Offline summary of the committed print |
| `dbprint diff` | Compare the live database against the committed print |
| `dbprint check` | CI gate: structure, freshness and statistic assertions; `--online` adds drift and SQL assertions |
| `dbprint context` | Emit a prompt-ready Markdown view of one or more tables |
| `dbprint annotate` | Print an authoring brief for one table's annotations, or `--check` them |
| `dbprint docs` | Browse the print as an HTML site — `serve` it live or `build` it static (the `[docs]` extra) |
| `dbprint serve` | Read-only MCP server over the committed print (the `[mcp]` extra) |

Full reference: [`docs/CLI.md`](https://github.com/jakubro/dbprint/blob/main/docs/CLI.md).

### Connecting an agent

An MCP client launches the server itself, so the client picks the working directory. Name the project explicitly:

```json
{
  "mcpServers": {
    "dbprint": {
      "command": "uvx",
      "args": ["--from", "dbprint[mcp]", "dbprint", "serve", "--project", "/srv/analytics"]
    }
  }
}
```

The server reads the committed print and never connects to a database, so the client needs no credentials.

### Reading a remote project

`--project` also accepts a git address, so a print committed to another repository reads without a manual clone:

```bash
dbprint list --project https://github.com/jakubro/dbprint/blob/main/docs/format/v1/examples/production/.dbprint.yaml
dbprint context arboretum.seedbank.accession --project https://github.com/jakubro/dbprint#main:docs/format/v1/examples/production
```

dbprint clones it and asks for no credentials of its own — the existing SSH agent or git credential helper covers a private repository. A remote project is read-only: every command that connects to a database — `generate`, `diff`, and the `--online` forms of `check` and `config explain` — refuses a remote locator; every other command works against it exactly as it does locally.

### Configuration

`.dbprint.yaml` holds project config (committed); `~/.dbprint/connections.yaml` holds credentials (never committed). Every key of both, with types, defaults and resolution order, is in [`docs/CONFIG.md`](https://github.com/jakubro/dbprint/blob/main/docs/CONFIG.md). Internals are documented in [`docs/ARCHITECTURE.md`](https://github.com/jakubro/dbprint/blob/main/docs/ARCHITECTURE.md).

### Further features

- **Semantic inference.** Every column gets a classification (`categorical`, `numeric`, `temporal`, `foreign_key_candidate`, `json`, ...). Text, categorical and foreign-key columns are matched against a closed set of shape patterns (`uuid`, `email`, `phone`, `ip`, `country_code`, ...), published with the sample size and match count behind the verdict. Columns that hold data that must not leave the database get a `sensitivity` axis (`personal_name`, `postal_address`, `national_id`, `credential`, ...). Columns effectively unique (cardinality ratio >= 0.9999) are marked `candidate_key`, with a `candidate_key_exception` alongside whenever that threshold admits measured or estimated duplicates rather than true uniqueness. Foreign keys the schema never declares are inferred from naming and stamped `detection: inferred`. Each of these is published as a guess, and a wrong one can be withdrawn in the annotations so agents stop seeing it.
- **Redaction.** Withhold cell values with `mask`, `drop`, or `hash` (salted), targeting columns by glob *or* by inferred sensitivity *or* by shape — so a rule written now also covers a `personal_name` column added later. Counts, cardinality and distributions survive intact; only literals are withheld. The salt lives with the credentials, never in the committed config.
- **Assertions.** Write data-quality checks in YAML against any statistic, or as raw SQL. Statistic assertions are evaluated against the committed print by plain `dbprint check`, with no database in reach; SQL assertions need `dbprint check --online`. See [`docs/ASSERTIONS.md`](https://github.com/jakubro/dbprint/blob/main/docs/ASSERTIONS.md).
- **CI exit codes.** `0` ok, `1` malformed print or unusable config, `2` stale, `3` drift, `4` connection failure, `5` partial run, `6` assertion failure, `7` total failure. `diff`, `check` and `context` all speak `--format json|yaml`.
- **Scoped profiling.** Per-table rules can sample (seeded from the table name, where the engine supports seeding), apply a row filter, or change what's measured — gated on table size, so one rule covers every fact table not yet named. A table read this way carries a `scope` block and a per-column `rows_scanned`, and every count in the file is denominated in the rows actually read, never silently in the whole table.
- **MCP server.** `dbprint serve` exposes the tools `get_table_context`, `list_tables`, `search_columns`, `resolve_value`, `get_manifest`, `get_diff`, `get_reference` — plus every artifact as a resource. It reads the committed print only and never opens a database connection. See [`docs/MCP.md`](https://github.com/jakubro/dbprint/blob/main/docs/MCP.md).
- **Markdown skills.** For clients that take markdown rules or custom instructions, a drop-in skill file tells an agent what a committed print answers, which tool answers it, and what each number covers — no process to run. A second skill has an agent write the annotation files from the code that writes each table and read-only queries, checked with `dbprint annotate --check`. Both sit at [`docs/examples/skill/`](https://github.com/jakubro/dbprint/tree/main/docs/examples/skill) and read without installing anything.
- **dbt lineage.** Point a connection's `dbt_manifest` at a compiled dbt `manifest.json` and `generate` records each model column's source columns as `derived_from`, so a note written once on a source column reaches every model built from it (the `[dbt]` extra). See [`docs/CONFIG.md`](https://github.com/jakubro/dbprint/blob/main/docs/CONFIG.md).
- **Token-budgeted context.** `dbprint context arboretum.seedbank.accession --budget 4000` fits the Markdown view into a token budget, dropping the lowest-priority sections first.

## Compared with other tools

### Closest neighbours

| Project | What it does | How dbprint differs |
|---|---|---|
| [dryrun](https://github.com/boringSQL/dryrun) | Offline Postgres schema snapshot + MCP server + linting | Nearest thing to this. Its snapshot is a compressed binary bundle marked `binary` in `.gitattributes`; dbprint's artifact is line-diffable YAML with a published spec, across every engine it reads |
| [dbt-profiler](https://github.com/data-mie/dbt-profiler) | Column profiles committed as dbt YAML / Markdown | dbt projects only. No DDL, no relationship graph, no semantic inference or redaction |
| Live-connection MCP servers (dbhub, postgres-mcp, Supabase, Neon) | Query and introspect a database at agent runtime | They need credentials at the agent, every session. dbprint profiles once, commits the result, and serves it with no database in reach |
| [tbls](https://github.com/k1LoW/tbls), [SchemaSpy](https://github.com/schemaspy/schemaspy) | Schema documentation and ER diagrams for humans | Structure only, no data distributions. `dbprint docs serve` renders the same browsable pages and FK diagrams over a print's measured columns |

### What dbprint is not

- **Not a migration tool.** It doesn't author, apply or version DDL changes. Use Alembic, Flyway, Liquibase, sqldef, Atlas.
- **Not a schema-management or ORM layer.** A print is generated *from* the database; it is never the source of truth the database is built from.
- **Not a live query interface.** Nothing in a print executes SQL for an agent.
- **Not a data-quality platform.** The assertion DSL is a CI gate over a print, not monitoring, alerting or lineage. Use Soda, Great Expectations, Elementary.
- **Not a data catalog.** No hosted service, no shared search index, no business glossary. `dbprint docs` serves a local read-only view of a committed print, bound to loopback, with nothing to administer.

## The format

**A print is a portable, git-committed snapshot of a database's structure and column-level data distributions, consumable offline by humans, AI coding agents, and CI.** The on-disk format — not the CLI — is the durable artifact here: specified, versioned independently of the tool, and designed for other producers to emit.

The format is specified in [`docs/format/v1/SPEC.md`](https://github.com/jakubro/dbprint/blob/main/docs/format/v1/SPEC.md).

The JSON Schemas ship inside the installed package at [`src/dbprint/spec/v1/`](https://github.com/jakubro/dbprint/tree/main/src/dbprint/spec/v1) — one for each generated artifact and each annotation file — and the conformance validator is a public API with no database, config or CLI required:

```python
from dbprint.conformance import validate_print

issues = validate_print("prints/production")  # conforms iff no error-severity issues
```

The reference print at [`docs/format/v1/examples/`](https://github.com/jakubro/dbprint/tree/main/docs/format/v1/examples) is real dbprint output, not a hand-written illustration.

## License

Copyright 2026 Jakub Roman. Distributed under the [Apache License 2.0](https://github.com/jakubro/dbprint/blob/main/LICENSE).

<!-- mcp-name: io.github.jakubro/dbprint -->
