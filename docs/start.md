# Your first print

This walks a database you can already connect to through to a print committed alongside your code. It ends at files in your repository, not at a command that printed something.

## Before you start

dbprint needs Python 3.13 or newer, and one extra per database engine. The extra carries the driver; the base package carries no database dependency at all.

| Engine | Install | Also needs |
|---|---|---|
| PostgreSQL | `pip install 'dbprint[postgres]'` | the `pg_dump` client binary on `PATH` — DDL comes from it, not from a query |
| MySQL | `pip install 'dbprint[mysql]'` | nothing beyond the extra |
| Snowflake | `pip install 'dbprint[snowflake]'` | nothing beyond the extra |

Two extras are independent of the engine: `dbprint[docs]` for the browsable site, and `dbprint[mcp]` for the MCP server. Combine them as `pip install 'dbprint[postgres,docs]'`.

You also need an account on the database that can read the tables you want profiled. The adapter page for your engine — [PostgreSQL](adapters/postgres.md), [MySQL](adapters/mysql.md), [Snowflake](adapters/snowflake.md) — states the exact grants; a role that can already run `SELECT` against those tables is enough to follow this page.

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

`.dbprint.yaml` arrives with one connection called `primary`:

```yaml
connections:
  primary:
    adapter: postgres
    auto: true
    include:
      - "public.*"
```

`include` is the one line worth reading twice. It decides which tables are profiled, as `fnmatch` globs over the lowercased fully-qualified name; the shape of that name is the engine's own — `schema.table` on PostgreSQL, `database.table` on MySQL, `database.schema.table` on Snowflake. Omitting `include` entirely profiles everything the connection can see, which on a warehouse is rarely what you want.

`auto: true` means a bare `dbprint generate` runs this connection without naming it.

The scaffold is PostgreSQL-shaped. On MySQL or Snowflake, change `adapter` to match, and change `include` with it — `public` is a PostgreSQL schema and matches nothing elsewhere. Use `my_db.*` on MySQL and `db.schema.*` on Snowflake.

Then fill in the credentials stub for the same connection name:

```yaml
primary:
  host: localhost
  port: 5432
  database: my_db
  user: dbprint_ro
  password: change_me
```

Every key here also reads from `DBPRINT_PRIMARY_<KEY>` in the environment, which is how CI supplies a password without a file. [Configuration](CONFIG.md) has the full resolution order and the per-adapter key list — Snowflake takes `account`, `warehouse` and `role` rather than `host` and `port`.

## 3. Profile

```console
$ dbprint generate
primary
  public                                                    4 objects     0m 00s
  public
    accession                                              2,500 rows       0.2s
    collector                                                120 rows       0.1s
    taxon                                                    300 rows       0.1s
    taxon_names                                                - rows       0.1s

-- Sketching -------------------------------------------------------------------
primary
  public
    accession                                                  - rows       0.0s
    collector                                                  - rows       0.0s
    taxon                                                      - rows       0.0s
primary  -  4 ok  0 failed  0 skipped  -  849ms
Completed  [########################]  4/4  100%  0:00:00
```

A progress bar tracks each pass while it runs and settles when it finishes; the tree above it fills in as tables complete, one line per table with its row count and how long it took. Tables are profiled in name order, not the order they appear in the schema.

`- rows` appears for two unrelated reasons here. `taxon_names` is a view, and no query is ever issued against one — its `statistics.yaml` is written from the catalog alone and says so. The sketching lines report it because that pass reads columns rather than rows.

Sketching is a second pass because it answers a different question: it summarises join keys so a consumer can estimate how far two columns overlap without querying either. It is skipped for views, and for any table you later narrow to a sample.

Piping the command somewhere gives a plain tab-separated record per event instead, which is the form a CI log should capture — `--no-tui` forces it at a terminal, and `-q` silences progress altogether. [Gating CI](guide/ci.md) shows that form.

One table's failure does not stop the others: every table is attempted and the failures are reported together at the end, listed under the summary and then in full on stderr. The exit code tells you which kind of outcome you got — `0` all well, `5` some tables failed and the rest were still written, `7` every table failed. A `5` is usually a missing grant on one table; a `7` is usually the connection or, on PostgreSQL, a missing `pg_dump`. [Troubleshooting](guide/troubleshooting.md) indexes those by symptom.

The run is read-only unless a table is narrowed to a fraction of its rows, and nothing here narrows anything — see [choosing what to profile](guide/scoping.md) for when that changes and what it needs.

## 4. Read what it wrote

```
prints/
└── primary/
    ├── manifest.yaml          index of every table, its artifacts and its freshness threshold
    ├── diff.yaml              what changed against the previous run
    ├── reading.md             how to read this print, written for whoever opens it next
    └── public/
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
