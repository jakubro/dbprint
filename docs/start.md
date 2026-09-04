# Your first print

This walks a database you can already connect to through to a print committed alongside your code. It ends at files in your repository, not at a command that printed something.

## Before you start

dbprint needs Python 3.13 or newer, and one extra per database engine. The extra carries the driver; the base package carries no database dependency at all.

```console
$ pip install 'dbprint[mysql]'
```

This page uses MySQL because it needs nothing beyond the extra — no external binary, no service account file, no warehouse. Every engine dbprint ships an adapter for has its own page with the install line, the exact grants and what it cannot promise about a sampled table: [duckdb](adapters/duckdb.md), [PostgreSQL](adapters/postgres.md), [MySQL](adapters/mysql.md), [ClickHouse](adapters/clickhouse.md), [Redshift](adapters/redshift.md), [Snowflake](adapters/snowflake.md), [Databricks](adapters/databricks.md), [BigQuery](adapters/bigquery.md). Swap the extra and the two connection blocks below for your own engine and the rest of this page is unchanged.

Two extras are independent of the engine: `dbprint[docs]` for the browsable site, and `dbprint[mcp]` for the MCP server. Combine them as `pip install 'dbprint[mysql,docs]'`.

You also need an account on the database that can read the tables you want profiled. Your engine's adapter page states the exact grants; a role that can already run `SELECT` against those tables is enough to follow this page.

## 1. Scaffold the project

From the root of the repository that will hold the print:

```console
$ dbprint init
wrote	project_config	<project>/.dbprint.yaml
created	prints_dir	<project>/prints
wrote	connections_file	<home>/.dbprint/connections.yaml
```

One tab-separated line per file: what happened, which file it is, and where it landed — the third column is an absolute path in the real output. A second run reports `kept` for anything already there.

Two files, with deliberately different lifetimes. `.dbprint.yaml` describes the project and is committed. `~/.dbprint/connections.yaml` holds credentials, lives in your home directory rather than the repository, and is shared by every project on the machine — `init` writes it only when it is absent, so a second project never overwrites the first one's entries.

## 2. Name the connection

`.dbprint.yaml` arrives with one connection called `primary`. Point `adapter` and `include` at your engine — this page uses MySQL:

```yaml
connections:
  primary:
    adapter: mysql
    auto: true
    include:
      - "seedbank.*"
```

`include` is the one line worth reading twice. It decides which tables are profiled, as `fnmatch` globs over the lowercased fully-qualified name; the shape of that name is the engine's own — `database.table` on MySQL, `schema.table` on PostgreSQL and Redshift, `database.schema.table` on Snowflake. Your adapter page states the shape for your engine. Omitting `include` entirely profiles everything the connection can see, which on a warehouse is rarely what you want.

`auto: true` means a bare `dbprint generate` runs this connection without naming it.

Then fill in the credentials stub for the same connection name:

```yaml
primary:
  host: localhost
  port: 3306
  database: seedbank
  user: dbprint_ro
  password: change_me
```

Every key here also reads from `DBPRINT_PRIMARY_<KEY>` in the environment, which is how CI supplies a password without a file. [Configuration](CONFIG.md) has the full resolution order and the per-adapter key list — several engines need a different credential shape entirely (Snowflake takes `account`, `warehouse` and `role`; BigQuery takes `project` and `dataset` with no password at all).

## 3. Profile

```console
$ dbprint generate
╭────────────────────────────────────────────────╮
│                  Cataloguing                   │
╰────────────────────────────────────────────────╯
primary
  seedbank                                                  4 objects       0.1s
╭────────────────────────────────────────────────╮
│                   Profiling                    │
╰────────────────────────────────────────────────╯
primary
  seedbank
    accession                                              2,500 rows       0.2s
    collector                                                120 rows       0.1s
    taxon                                                    300 rows       0.1s
    taxon_names                                                - rows       0.1s
╭────────────────────────────────────────────────╮
│                   Sketching                    │
╰────────────────────────────────────────────────╯
primary
  seedbank
    accession                                                                0.0s
    collector                                                                0.0s
    taxon                                                                    0.1s
primary  -  4 ok  0 failed  0 skipped  -  0.8s
Completed  [########################]  4/4  100%  0:00:00
```

A progress bar tracks each pass while it runs and settles when it finishes; a boxed heading marks each pass in the scrollback above it — Cataloguing, then Profiling, then Sketching — and the tree fills in as tables complete, one line per table with its row count and how long it took. Tables are profiled in name order, not the order they appear in the schema.

`- rows` appears once, on `taxon_names`, a view: no query is ever issued against one, so its `statistics.yaml` is written from the catalog alone and says so. The sketching lines carry no rows column at all — that pass reads columns rather than rows, so each of its lines is a name and a duration, nothing else.

Sketching is a second pass because it answers a different question: it summarises join keys so a consumer can estimate how far two columns overlap without querying either. It is skipped for views, and for any table you later narrow to a sample.

Piping the command somewhere gives a plain tab-separated record per event instead, which is the form a CI log should capture — `--no-tui` forces it at a terminal, and `-q` silences progress altogether. [Gating CI](guide/ci.md) shows that form.

One table's failure does not stop the others: every table is attempted and the failures are reported together at the end, listed under the summary and then in full on stderr. The exit code tells you which kind of outcome you got — `0` all well, `5` some tables failed and the rest were still written, `7` every table failed. A `5` is usually a missing grant on one table; a `7` is usually the connection, or an adapter-specific prerequisite your engine's page names (a missing `pg_dump` on PostgreSQL, an absent `SAMPLE BY` key on ClickHouse). [Troubleshooting](guide/troubleshooting.md) indexes those by symptom.

The run is read-only unless a table is narrowed to a fraction of its rows, and nothing here narrows anything — see [choosing what to profile](guide/scoping.md) for when that changes and what it needs.

## 4. Read what it wrote

```
prints/
└── primary/
    ├── manifest.yaml          index of every table, its artifacts and its freshness threshold
    ├── diff.yaml              what changed against the previous run
    ├── reading.md             how to read this print, written for whoever opens it next
    └── seedbank/
        ├── accession/
        │   ├── ddl.sql        normalized CREATE TABLE
        │   ├── statistics.yaml    row count, and per column: type, nulls, cardinality, distribution
        │   └── relationships.yaml outgoing and incoming foreign keys
        ├── collector/
        ├── taxon/
        └── taxon_names/       a view: statistics.yaml from the catalog alone, no rows scanned
```

Open `prints/primary/reading.md` first. It is written into every print alongside the data, and it names each kind of column dbprint recognises, what each measurement means, and the traps worth knowing before you rely on a number.

Then `statistics.yaml`, which is where the data is. Each column carries what it is, how much of it is null, how many distinct values it holds, and — depending on what kind of column it is — its value list, its range, or its percentiles. For the normative definition of every field, and of what a *missing* field means, see [SPEC 2.2](format/v1/SPEC.md#22-statisticsyaml) and [SPEC 7](format/v1/SPEC.md#7-reading-an-absence).

To read the whole print as a browsable site instead, see [browsing a print](guide/browsing.md).

## 5. Check it offline

```console
$ dbprint list
$ dbprint check
```

Neither connects to the database. `list` summarises what is committed; `check` verifies the print is well-formed and still fresh, and is what a CI job runs. See [gating CI](guide/ci.md).

## 6. Commit it

```console
$ git add .dbprint.yaml prints/
$ git commit -m "add dbprint prints"
```

That is the artifact. From here:

- Add prose the numbers cannot carry — see [annotating a print](guide/annotations.md).
- Withhold cell values from columns that should not leave the database — see [withholding cell values](guide/redaction.md).
- Point an agent at it — see [giving a print to an agent](guide/agents.md).
