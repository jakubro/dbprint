# dbprint Configuration

> **Purpose**: every key dbprint reads from `.dbprint.yaml` and `~/.dbprint/connections.yaml`. For the on-disk output these settings produce, see [format/v1/SPEC.md](format/v1/SPEC.md) (normative). For the command surface, see [CLI.md](CLI.md). For the `assertions:` grammar, see [ASSERTIONS.md](ASSERTIONS.md).

Two files, with different lifetimes:

| File | Committed? | Holds |
|---|---|---|
| `.dbprint.yaml` | **Yes** — it describes the project | Connections, rules, tuning |
| `~/.dbprint/connections.yaml` | **No** — credentials | Host, user, password, keys |

`dbprint init` writes a starting pair. A worked `.dbprint.yaml` ships at [`format/v1/examples/production/.dbprint.yaml`](format/v1/examples/production/.dbprint.yaml) alongside the print it produces.

**Every key is checked when the file loads.** The one exception is the inside of a connection's `assertions` block, which `dbprint check` reads and reports when it evaluates the block (see [`assertions`](#assertions)). A key not named below is refused at any depth, with its full path and the nearest valid key — `connections.production.profile.statistcs: unknown key; did you mean 'statistics'?` — and so is a value of the wrong type or outside its range. Every problem in the file is reported in one run. The same rules ship as a JSON Schema, [`project.schema.json`](https://jakubro.github.io/dbprint/config/project.schema.json), which `dbprint init` names on the file's first line so an editor validates as you type; the loader validates against that same schema. One root key is never read: `x-shared` holds anything, typically YAML anchors (`&name`) that the rest of the file spreads with aliases (`*name`).

---

## `.dbprint.yaml`

```yaml
# yaml-language-server: $schema=https://jakubro.github.io/dbprint/config/project.schema.json
defaults:                         # OPTIONAL; the same shape as a connection, less adapter, auto, assertions, dbt_manifest
  output: prints
  tables: { ... }
  run: { ... }
  profile: { ... }
  rules: [ ... ]
  diff: { ... }

connections:                      # REQUIRED; at least one
  <name>:                         # free-form identifier; also the print subdirectory
    adapter: postgres | snowflake | mysql | duckdb | clickhouse | redshift | databricks | bigquery   # REQUIRED
    auto: false
    output: prints
    tables:                       # which tables are profiled
      include: ["<PATTERN>"]
      exclude: ["<PATTERN>"]
    run:                          # how the run talks to the database
      parallelism: 1
      statement_timeout: <DURATION>   # absent by default
    profile:                      # what is measured, and how
      freshness:
        max_age_days: 7
      statistics: { ... }
      read:
        tables: {mode: full}      # full | sample | filter | catalog
        views: {mode: catalog}    # full | filter | catalog
      looks_like: {enabled: true, sample_size: 1000}
      null_patterns: {enabled: true, top_n: 20}
      parts: {enabled: true, max: 20, max_depth: 3}
      timeline: {enabled: true}
      sketches: {enabled: true, columns: required}   # required | all
      relationships: {infer: true}
      redact: [ ... ]
    rules: [ ... ]                # per-table overrides of `profile`
    diff: { ... }
    assertions: { ... }
    dbt_manifest: <PATH>          # absent by default

x-shared: { ... }                 # OPTIONAL; never read
```

**One merge rule.** Values cascade `defaults` → connection → each matching rule, in order: a mapping merges key by key, a later value wins, and a list replaces the earlier list whole. `rules` themselves concatenate, `defaults` rules first. So a connection's `redact` list or `tables.include` list replaces the one in `defaults` rather than adding to it; to share entries across connections, anchor each entry once under `x-shared` and list it with an alias where it applies. [`dbprint config explain`](CLI.md) prints what a table resolves to — by the same resolution `generate` runs, including which tables get a row-count estimate; a table name is matched case-insensitively.

### Connection keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `adapter` | enum | — (required) | `postgres` \| `snowflake` \| `mysql` \| `duckdb` \| `clickhouse` \| `redshift` \| `databricks` \| `bigquery` |
| `auto` | bool | `false` | Run this connection on a bare `dbprint <command>` with no `CONNECTION` argument. Any number of connections may set it |
| `output` | path | `prints` | Root directory, relative to `.dbprint.yaml`. Prints land in `<output>/<name>/` — the connection name is appended, so do not include it. While a run is in flight `generate` keeps a hidden, git-ignored `.dbprint-run/` beside the prints; it must share the prints' filesystem, and an advisory lock guards it (on a network filesystem that lock may be emulated or absent) |
| `tables.include` | list of glob | `["*"]` | Tables to profile. **Omitting this profiles everything the connection can see** |
| `tables.exclude` | list of glob | `[]` | Removed from the include set |
| `run.statement_timeout` | duration | absent | The longest any one statement on this connection may run, as `Nd`, `Nh`, `Nm` or `Ns` (one unit). Absent means no limit and no session setting. See below |
| `run.parallelism` | int ≥ 1 | `1` | How many tables the connection profiles at once, each on a session of its own. `1` is one session and tables in listing order. duckdb refuses anything above `1`. See below |
| `profile.freshness.max_age_days` | int ≥ 0 | `7` | A print younger than this passes `check`'s freshness gate, and is left alone by `generate` — which additionally requires that the running version of dbprint wrote it, with every file dbprint wrote for it present and readable, under the settings the configuration still resolves to (see below). A rule can override it per table, and can condition that override on the table's size. `0` re-profiles on every run — see below |
| `profile.statistics` | map | see below | What gets measured per column, and how much of it |
| `profile.read.tables` | map | `{mode: full}` | How tables and materialized views are read: every row, a sample, a predicate, or the catalog alone. See [`profile.read`](#profileread) |
| `profile.read.views` | map | `{mode: catalog}` | How plain views are read: described from the catalog, or queried whole or through a predicate. See [`profile.read`](#profileread) |
| `profile.looks_like` | map | `{enabled: true, sample_size: 1000}` | Detect each text-like column's value shape from a sample of `sample_size` distinct non-null values ([SPEC 4.1](format/v1/SPEC.md)) |
| `profile.null_patterns` | map | `{enabled: true, top_n: 20}` | Publish which columns are null on the same rows, up to `top_n` combinations; `null_patterns.coverage` states the share of rows the listed ones account for |
| `profile.parts` | map | `{enabled: true, max: 20, max_depth: 3}` | Describe array, record, map and JSON columns part by part: at most `max` parts per column, `max_depth` steps per path (`items[*].sku` is two). Both are at least `1` ([SPEC 2.2.18](format/v1/SPEC.md)) |
| `profile.timeline` | map | `{enabled: true}` | Bucket one temporal column's activity over time — see below |
| `profile.sketches` | map | `{enabled: true, columns: required}` | Sketch the required columns, or `all` every sketchable-type column — see below |
| `profile.relationships` | map | `{infer: true}` | Derive the foreign keys the catalog does not declare, from column naming — see below |
| `profile.redact` | list | `[]` | What to do with the cell values of the columns each entry covers |
| `rules` | list | `[]` | Per-table overrides of `profile`, plus the keys that narrow what is read |
| `diff` | map | see below | Presentation thresholds for `dbprint diff`'s human output |
| `assertions` | map | `{}` | Data-quality checks `dbprint check` evaluates |
| `dbt_manifest` | path | absent | A dbt `manifest.json`, relative to `.dbprint.yaml`; `generate` records each printed model column's source columns from it — see below |

`profile.statistics`, `rules`, `profile.redact`, `diff` and `assertions` each have their own section below.

**Every optional measurement is a switch, at every level.** `looks_like`, `null_patterns`, `parts`, `timeline`, `sketches` and `relationships.infer` can be set in `defaults`, on a connection or in a rule, so one table can drop a measurement the rest keep: `rules: [{match: {tables: [...]}, profile: {null_patterns: {enabled: false}}}]`. A table with a measurement switched off says so — its `statistics.yaml` names it in `disabled` ([SPEC 2.2.21](format/v1/SPEC.md)), and `context`, the MCP server and `dbprint docs` read the missing block as not measured, never as none. A parameter of a switched-off measurement is ignored.

**Switching `looks_like` off costs more than the verdict.** No sample is drawn, so `inferred.sensitivity` rests on the column name alone and loses the categories a value shape supplies (`contact` from an email-shaped column, and so on); no `epoch_unit` comes from a sample; and a free-text column loses the `prose` verdict that exempts it from the grouped value scan, so its most frequent entries are published. A `redact` rule keyed on `looks_like` could never match such a table, so a table that switches it off while one is in force fails, naming both.

**`relationships.infer` derives the foreign keys a schema never declared.** Plenty of warehouses declare none — Snowflake does not enforce them, and analytics schemas in PostgreSQL routinely skip them — so a print of one carries an empty relationship graph and a reader cannot tell that `accession.collector_id` points at `collector.collector_id`. With the key on, a column named `<stem>_id` whose stem resolves to an in-scope **table** declaring a single-column key of a compatible type becomes an edge marked `detection: inferred`; the rule and its refusals are specified in [SPEC 2.3.8](format/v1/SPEC.md). Turning it off for a table removes the edges that table would have inferred; the table stays a target of other tables' inferred edges. Its own `*_id` columns then classify by type and cardinality rather than as `foreign_key_candidate`, and the run that flips the switch reports none of the fields that moves as drift. Turned off for every table, it also skips the catalog pre-pass that reads the columns and declared keys inference resolves against.

**`statement_timeout` bounds every statement the connection issues.** It is applied once per session through the vendor's own setting, so a limit that fires cancels the statement in the database rather than abandoning it. A statement that exceeds it fails like any other: a table-wide statistic fails its table, a column's own statistics or an optional block are named `unmeasured`, and the run log and summary say `timed out after <limit>`. A zero or compound value is refused at load; a value above the vendor's own ceiling (Snowflake 7d, Databricks 2d) is refused at connect. What each adapter can and cannot bound is stated on its adapter page.

**`parallelism` profiles several whole tables at once.** The connection opens that many sessions, and each table runs start to finish on one of them; a sampled table's copy is read and dropped on the session that made it, so the unit of parallel work is a table, never a statement. The artifacts match a run at `1`: tables are written in whatever order they finish, but the manifest, diff and run summary list them in listing order, and a table that fails does not stop the others. Connections still run one after another. Every session carries the connection's `statement_timeout`. A session that will not open leaves the run on the ones that did, with a warning naming how many that was; if the first one fails, the run fails with a connection error. dbprint does not cap the count to what the warehouse serves: sessions beyond what it serves concurrently queue there, and on PostgreSQL and Redshift the cap applies per database, one session each opened on first use. duckdb refuses a value above `1` — an unnamed `:memory:` database is private to each session. The adapter pages state what each vendor makes of concurrent sessions.

**`read.tables.materialize` is the one setting that makes dbprint write to your database.** A sampled table is read by many statements, and a sampling construct re-evaluated per statement can draw a fresh set of rows each time — so a column's listed value counts and the non-null figure they are a share of come from different reads, and the two disagree on a table nobody wrote to. With the key on, the producer copies the draw into a temporary table once and every statement for that table reads that instead. What the write costs you: a temporary-table privilege where the copy is made, spelled differently per engine — `TEMPORARY` on the database for PostgreSQL, `CREATE TEMPORARY TABLES` on the database for MySQL, and nothing at all on Snowflake, which exempts temporary tables from the schema's `CREATE TABLE` privilege. The copy lands in the session's own temporary space on PostgreSQL and MySQL and in the profiled table's own schema on Snowflake. The object holds the sampled fraction only rather than the whole table, and on every adapter but BigQuery its lifetime is the session, so it is gone when the run ends whether the run succeeded or not — BigQuery has no session-scoped table at all, so its own copy is a real dataset object with an expiration set instead; see the per-adapter note below. Where the privilege is absent, what happens next depends on the adapter. PostgreSQL's own `TABLESAMPLE BERNOULLI` decides row membership per row from its seed, so it falls back to sampling per statement with a warning on stderr: every statement reads the same rows while the table is unchanged, and a write landing between two statements reaches one and not the other. duckdb and Databricks fall back the same way. MySQL, Snowflake, ClickHouse, Redshift and BigQuery have no construct that reproduces a row set statement to statement, so turning the key off (or a refused write) fails that one table instead, naming `read.tables.materialize` in the message, rather than publish a file whose fields describe different reads — see the per-adapter notes below for the full split. A table that is not sampled never materializes — a full scan has nothing to copy, and a `filter` is a predicate, so re-evaluating it selects the same rows every time.

**`sketches.columns: all` widens the second setting that changes what leaves the database.** [SPEC 2.2.14](format/v1/SPEC.md) always sketches a column named by an edge, plus every declared-unique column, every column at or below the sketch's own retained size, and every column carrying a measured candidate key — a fixed-size summary of a column's distinct values, from which a consumer computes set overlap against another column offline, no query against either source. With `all`, every column whose type the sketch format covers is sketched, whether or not it fits one of those four categories, at the cost of one extra query per newly-sketched column. A KMV sketch is an unsalted hash of real cell values, so `all` widens that surface to every column redaction did not withhold, on top of whatever the required set already carries. `sketches: {enabled: false}` takes even the required set away, and with it every join the run would measure from a sketch with an endpoint in that table.

The key has no effect on a table that carries a `scope` block, or on an object written `catalog_only`: neither is sketched at all, whatever this is set to. A sketch answers set-overlap questions between two columns, which needs a reproducible read of the whole column — a narrowed read cannot give one, and a catalog-only object is not read at all. Since a large warehouse is also where sampling gets turned on, expect the two settings to meet: a table read in `sample` or `filter` mode publishes no sketches regardless.

**`timeline` picks one temporal column per table and buckets its non-null values by day, week, or month.** [SPEC 2.2.16](format/v1/SPEC.md) names the anchor deterministically — a temporal, non-redacted, calendar-typed column named in the table's own clustering or partitioning key first, otherwise the eligible column with the lowest null rate — at the cost of one extra grouped query per table that has one. Switching it off skips that query, and no column carries `populated`. Like a sketch, a table carrying a `scope` block or an empty table publishes no `timeline` regardless of this setting.

**`dbt_manifest` records column lineage from a dbt project's compiled SQL.** With the `[dbt]` extra installed (`pip install dbprint[dbt]`), `generate` reads the manifest before connecting, parses each model's compiled `SELECT`, and writes every printed model column's direct source columns as `derived_from` in its `statistics.yaml` ([SPEC 2.2.4](format/v1/SPEC.md)); a source's notes then reach the column in `dbprint context --purpose query`. The manifest must come from `dbt compile`, `dbt run` or `dbt build` (dbt 1.3 or later) against the same target the connection prints — `dbt parse` alone writes no compiled SQL. A manifest that is missing, unreadable, older than dbt 1.3 or uncompiled fails the connection with exit 1. A model whose compiled SQL cannot be parsed gets no lineage and one warning naming it, and the run goes on. A `derived_from` written in `statistics.annotations.yaml` replaces the parsed list for its column, and `derived_from: []` there suppresses it. Lineage is read from local files and never re-profiles a table: a fresh table's `statistics.yaml` is updated in place when its traced sources change.

**`max_age_days: 0` means the print is stale the moment it is written.** `generate` re-extracts it every run, and `check`'s freshness gate cannot pass at `0` whatever order the commands run in. Use it where `check` does not gate the pipeline, or pass `check --max-age` explicitly — an explicit flag overrides every table's recorded threshold. A negative value asks for the same thing and is refused at load, because it holds every table stale with no way for the artifact to say so.

**A release bump makes every print stale, whatever `max_age_days` says.** `generate` carries a committed table forward only when the manifest records the version now running, so the first run after an upgrade re-profiles every table. A run that leaves tables unread — narrowed with `--include` or `--exclude`, or with a table that failed — keeps the older release's stamp, so the next full run still re-reads every table.

**A changed setting makes the tables it governs stale, whatever `max_age_days` says.** `generate` carries a table forward only while its print was written under the settings the configuration resolves to now: the whole resolved profile the manifest records for it — the read mode and its `filter`, `fraction` or `max_rows` ceiling (compared as the ceiling, so a table that grows under an unchanged ceiling stays fresh), the `statistics` parameters, and every measurement switch with its parameters — plus each column's `redact` primitive. A changed setting re-reads exactly the tables whose resolved profile it changes, scoped and catalog-only tables included; a parameter of a measurement that stays switched off changes nothing. A print whose manifest records no `profile` is re-read whole once. A table this run does not re-read — outside the CLI selectors, failed, or never reached under `--fail-fast` — but whose print the current `redact` rules contradict fails the run (exit 5), naming the columns; its files stay as they are until a run re-reads it. Rotating `redaction_salt` is the exception: the salt is never recorded, so the print cannot tell it changed, and a rotation needs `generate --force`.

`tables` decides **which** tables are profiled; `profile` and `rules` decide **how** each one is profiled. The two axes never mix: a rule cannot bring a table into scope. A rule selects within the second axis — its `match.tables`, `match.exclude` and `match.min_rows` narrow which of the profiled tables that rule governs, and none of them can widen the connection's scope.

Patterns are `fnmatch` globs over the lowercased fully-qualified name, so matching is case-insensitive. `*` spans dot separators. The FQN shape is the adapter's: `database.schema.table` (Snowflake, duckdb, PostgreSQL, Redshift), `catalog.schema.table` (Databricks), `database.table` (MySQL, ClickHouse), `dataset.table` (BigQuery).

### `profile.read`

How each object's rows are read, set separately for tables (materialized views included) and for plain views. Each block names one `mode`, and each parameter belongs to exactly one mode:

| Mode | Parameters | Tables | Views | What is read |
|---|---|---|---|---|
| `full` | — | default | yes | Every row |
| `sample` | `fraction` or `max_rows`, plus `materialize` | yes | refused | A fraction of the rows, fixed or derived from a ceiling |
| `filter` | `filter` | yes | yes | The rows one SQL predicate selects |
| `catalog` | — | yes | default | Nothing: columns, DDL and dependencies from the catalog, with `catalog_only` in `statistics.yaml` |

```yaml
profile:
  read:
    tables: {mode: sample, max_rows: 1000000000}   # a project-wide ceiling
    views: {mode: catalog}
rules:
  - match: {tables: ["arboretum.seedbank.storage_reading*"], min_rows: 500000000}
    profile: {read: {tables: {mode: sample, fraction: 0.01}}}
  - match: {tables: ["arboretum.seedbank.germination_reading"]}
    profile: {read: {tables: {mode: filter, filter: "created_at >= current_date - interval '30 days'"}}}
  - match: {tables: ["arboretum.seedbank.*_archive"]}
    profile: {read: {tables: {mode: catalog}}}
  - match: {tables: ["arboretum.seedbank.*_v"]}
    profile: {read: {views: {mode: full}}}
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `mode` | enum | `full` (tables), `catalog` (views) | How the object is read; see the table above |
| `fraction` | float in (0, 1] | absent | `sample` only. The share of rows to read, per [SPEC 2.2.8](format/v1/SPEC.md) |
| `max_rows` | int ≥ 1 | absent | `sample` only, tables only. A row-count ceiling, resolved into a fraction against the table's size. See below |
| `materialize` | bool | `true` | `sample` only. Draw the sampled rows once into a temporary table, so every statistic describes the same rows. Takes a temporary-table privilege on PostgreSQL and MySQL, none on Snowflake. On MySQL, Snowflake, ClickHouse, Redshift and BigQuery the copy is not optional: `false`, or a copy that cannot be taken, fails that table rather than reading it |
| `filter` | string | absent | `filter` only. One SQL predicate, applied verbatim |

- **A block merges like any other mapping, with one exception: a new `mode` drops the parameters it does not take.** A rule switching an inherited `sample` to `filter` loses the inherited `fraction`, `max_rows` and `materialize` rather than carrying them as dead keys, so a project-wide ceiling never collides with a filtered table. Writing `fraction` clears an inherited `max_rows`, and the reverse, since a sample is sized one way or the other.
- **A parameter under a mode that does not take it is refused at load**, naming the parameter, the mode and where that mode came from: `rules[0].profile.read.tables.fraction: not valid under mode: filter`. A block writing a parameter without `mode` is checked against the mode it inherits. A block writing both `fraction` and `max_rows`, or a `sample` with neither, is refused too. So is `views.mode: sample` or `views.max_rows` — a sampling clause binds to a table, and a view has no row-count estimate to resolve a ceiling against — and a rule carrying both `match.min_rows` and a `views` block, since `min_rows` never selects a view. A rule that writes `mode` carries that mode's own parameters — `fraction` or `max_rows` for `sample`, `filter` for `filter` — since an earlier matching rule may have switched the table to another mode and dropped the inherited ones.
- **`catalog` sends a table down the path a view takes by default.** No statement reads its rows: its `statistics.yaml` carries `catalog_only`, its columns' types and classifications, its `physical_layout` and its relationships, and no `row_count`. `context`, the MCP server and `dbprint docs` render it as they render a catalog-only view, and `diff` reports it as unevaluated. Switching a table into or out of `catalog` re-reads it on the next `generate`.
- **A view read in `full` or `filter` mode is profiled like a table**: row count, per-column statistics, grain, dependencies, value lists. It publishes no `physical_layout` and no declared key of its own, and a view whose catalog lists no columns stays catalog-only, since there is nothing to read. A view is never narrowed by a ceiling and never selected by `min_rows`, so it is read whole unless a `filter` narrows it, and its `row_count` is counted exactly either way. A query that fails fails the view — its committed print stays as it was, rather than falling back to the catalog-only file.
- **An external table is read only when a rule asks for it by name or pattern.** An object whose rows live in another system (`external`, SPEC 2.2.20), and a BigQuery snapshot, stay catalog-only under the inherited `full`; a matching rule that writes `read.tables.mode` reads them in that mode. A BigQuery table that requires a partition filter is read only under `read.tables.mode: filter`, and described from the catalog under any other mode. On PostgreSQL, MySQL, ClickHouse and Databricks that read reaches another live database, so a rule writing any mode but `catalog` for an external table is refused before any table is read, exiting 1 and naming the rule, the table and the adapter.

> **The predicate is interpolated, not bound.** It is your SQL, passed verbatim into every statistics query for that table; dbprint never parses, rewrites or validates it, because [SPEC 2.2.8](format/v1/SPEC.md) requires it recorded as written. Treat `.dbprint.yaml` as carrying the same trust as the credentials file — it already names the connection whose credentials a run uses.

#### `max_rows`

A row-count ceiling states the cost an operator can afford directly, in rows, rather than as a fraction — the engine derives the fraction from the table's size, read for this key as it is for `min_rows`.

- **A ceiling is a different policy from a fraction, not another way to spell one.** `fraction` reads a fixed share regardless of table size; a ceiling caps the rows the draw *returns*, so a table under it is read whole and every table over it yields the same number of rows regardless of how far over. What a ceiling bounds is the downstream work — the rows aggregated, `rows_scanned`, and on Snowflake the warehouse time, since `SAMPLE SYSTEM` prunes at block level. It does not bound what leaves the disk on PostgreSQL or MySQL: `TABLESAMPLE BERNOULLI` tests rows individually and a `RAND() < p` predicate is unindexable, so both scan the whole table however small the fraction. Replacing `min_rows` rules that set a `fraction` with one `max_rows` value changes what is read at the low end: a table under the ceiling that a size rule would have sampled is read whole.
- **A project-wide budget is the point of the feature**, so a ceiling belongs at connection or `defaults` level as naturally as in a rule; a rule's own `fraction`, `filter` or `catalog` mode replaces it for the tables it names.
- **The ceiling is recorded on the table's manifest entry** as `max_rows_scanned`. Freshness compares ceilings, never the fraction one resolved to, which moves with the estimate; a table the ceiling governed but did not sample records it too.
- **A resolved fraction of exactly `1.0` is not a sample.** When the ceiling is at or above a table's size, the table is read whole: no `scope` block, no `row_count_method: approximate`, and `row_count` is counted rather than estimated. `sample: 1.0` never reaches the artifact through this path.
- **The resolved fraction snaps down to a geometric grid, 10% per step.** A pure function of the ceiling and the estimate alone — no run-to-run state — so a catalog estimate that drifted by a few percent (`ANALYZE` noise, InnoDB's approximate `table_rows`) resolves to the same fraction it did last run, and `diff` reports nothing. A table that genuinely changed size by 10% or more crosses at least one grid step, and `diff` shows the statistics move. Snapping down, never up, keeps the ceiling a true ceiling: `rows_scanned` never exceeds `max_rows` because of the grid.
- **A ceiling gates nothing offline.** `check` and `list` never connect, so a ceiling never resolves there — unlike `min_rows`, it does not affect what an offline command reads, because it governs only what `generate` scans.

### `profile.statistics`

Tuning for [SPEC 2.2](format/v1/SPEC.md). Every key is optional.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enumeration_threshold` | int | `50` | Cardinality ≤ this makes a column `categorical`; above it the column classifies by type and may carry a range instead. It is also how far a `values` list enumerates: a column at or below it publishes every distinct value, whatever its classification |
| `top_n_values` | int | `20` | How many entries the `values` list samples on a column whose cardinality exceeds `enumeration_threshold`. Raise it above that threshold and it becomes the enumeration bound instead |
| `percentiles` | list of int | `[1, 25, 50, 75, 99]` | **Integer percents in 1..99.** Fractions such as `0.25` are rejected at load |

Lowering `enumeration_threshold` is the cheapest way to cut cost on a wide table: fewer columns are enumerated in full, and the ones that still are carry fewer values. `values_coverage` states how much of the column the listed entries cover.

### `rules`

An ordered list. Each entry is a `match` saying which tables it governs and a partial `profile` merged over theirs, so one connection can sample a billion-row fact table without sampling anything else, and refresh dimensions daily while refreshing that fact table weekly.

```yaml
rules:
  - match:
      tables: ["arboretum.seedbank.storage_reading*"]    # OPTIONAL; defaults to ["*"]
      exclude: ["arboretum.seedbank.storage_reading_v2"] # OPTIONAL; defaults to []
      min_rows: 500000000                                # OPTIONAL; only tables at least this large
    profile:                                             # REQUIRED; sets at least one key
      read: {tables: {mode: sample, fraction: 0.01}}
      statistics: {top_n_values: 5}
      freshness: {max_age_days: 30}
  - match: {tables: ["arboretum.seedbank.germination_reading"]}
    profile:
      read: {tables: {mode: filter, filter: "created_at >= current_date - interval '30 days'"}}
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `match.tables` | list of glob | `["*"]` | Tables this rule governs. Same glob grammar as `tables.include` |
| `match.exclude` | list of glob | `[]` | Removed from this rule's own match |
| `match.min_rows` | int ≥ 1 | absent | Rule applies only to tables of at least this many rows |
| `profile.read` | map | — | `tables` and `views` blocks, as in [`profile.read`](#profileread) |
| `profile.statistics` | map | — | Any subset of the `profile.statistics` keys |
| `profile.freshness.max_age_days` | int ≥ 0 | — | Freshness threshold for these tables. Same meaning of `0` as at connection level |
| `profile.looks_like`, `null_patterns`, `parts`, `timeline`, `sketches`, `relationships` | map | — | Any subset of each block's keys, for these tables |

- **Every matching rule applies, in declaration order, and later ones win.** Rules from `defaults` are walked before the connection's own, and each merges its `profile` over the result by the one merge rule: `statistics` merges key by key, so two rules can set different keys and both hold, and every scalar is last-wins. A `read` block whose `mode` changes drops the parameters the new mode does not take — see [`profile.read`](#profileread).
- A rule whose `profile` sets nothing is refused at load: it would do nothing. So is a rule whose `match.tables` is an empty list, which matches no table; omitting `match` (or `tables`) is the way to match everything. An empty `exclude` is fine — it removes nothing.
- **`min_rows` belongs to a rule's `match`.** Written anywhere else it is refused like any unknown key.
- Two matching filters never combine: the later predicate replaces the earlier one, so no query runs a condition neither rule authored.
- `inferred.looks_like` honors both `filter` and `sample` modes: it must not describe rows outside the artifact, and its own draw composes with the sample fraction rather than replacing it — so honouring `sample` costs no extra rows. Composition is row for row on PostgreSQL and duckdb only; on every other engine it is population-level.
- **A sampled table reads one row set within a run, and on PostgreSQL and duckdb the same one next run.** One table's profile issues many statements against the same narrowed source, so an unseeded fraction would describe different rows per field on a table nobody wrote to. What prevents that is the materialized copy, not the seed. MySQL, Snowflake and Databricks also seed the fraction from the table's own name, but only PostgreSQL's and duckdb's `TABLESAMPLE ... REPEATABLE` document a stable draw across runs (duckdb's measured directly, reproducing regardless of thread count): MySQL's seeded predicate holds only while scan order does, and Snowflake does not document two evaluations of one seeded expression reading the same rows — which is precisely why the copy exists, and why the engine refuses a `sample` scope with no materialized copy on both rather than publish a file whose fields disagree with each other (see the per-adapter notes below). ClickHouse, Redshift and BigQuery take no seed on the fraction at all, so each refuses a `sample` scope with no materialized copy outright, for the same reason. The exception is the extra draw `inferred.looks_like` takes on top of that row set: PostgreSQL and duckdb seed it into row-for-row agreement with the rest of the profile, and on every other engine the shape claim agrees with it at the population level.
- **ClickHouse never reads a sampled table without the copy.** A declared `SAMPLE BY` key does not mean `SAMPLE` narrows anything — measured directly: a monotonic key reads the whole table at every requested fraction, and a table with no key at all raises rather than falling back to a full scan. When the copy is unavailable ClickHouse fails that one table, naming `read.tables.materialize` in the message, because there is no unmaterialized reading of it that is honest. Here, as on MySQL, Snowflake, Redshift and BigQuery, `materialize: false` is not a performance trade — it decides whether a `sample`-scoped table is profiled at all.
- **Redshift never reads a sampled table without the copy either.** There is no seeded sampling clause at all: `WHERE RANDOM() < p` is the only narrowing, and its result is not deterministic across a distributed cluster's compute slices, even within one run. A `sample` scope with no materialized copy fails that table rather than mixing rows drawn by independent evaluations of the same predicate — the same refusal ClickHouse makes, for a different reason.
- **Databricks degrades instead of refusing.** `TABLESAMPLE ... REPEATABLE` is a genuinely reproducible draw on this engine, so a table the copy cannot be taken on still reads a stable fraction directly, with a warning, rather than failing outright.
- **BigQuery never reads a sampled table without the copy either.** `TABLESAMPLE SYSTEM` has no seed clause in the grammar at all, and each execution processes an independently computed sample, so results are not guaranteed to match from one execution to the next. A `sample` scope with no materialized copy fails that table rather than publish a `statistics.yaml` whose fields describe different rows — the same refusal ClickHouse and Redshift make, for a third reason. The copy itself also differs from every other adapter's: BigQuery has no session-scoped temporary table, so the draw is copied into an ordinary table in the profiled dataset, created with its own `expiration_timestamp` (a few hours out) rather than the run's own cleanup being the only thing standing between it and an orphaned copy — a killed process or a revoked delete privilege leaves it to expire on its own instead of billing as storage indefinitely. `CREATE OR REPLACE` also means an orphan from an earlier run never wedges a later one with `Already Exists`. The scratch table is never listed, so it is never profiled as a table of its own.
- **`min_rows` selects by size, and both conditions must hold.** A rule carrying it governs a table only when the name matchers admit it *and* it is at least that large, so `min_rows: 500000000` with `read: {tables: {mode: sample, fraction: 0.01}}` samples the tables that are too big to scan without naming them one by one. It gates whatever the rule sets — `read`, `statistics` and `max_age_days` alike — because it sits on the matcher rather than on one key. It is a matcher, not a setting: a rule carrying `min_rows` and nothing else selects a set of tables and does nothing to them, and is rejected at load like any other rule that overrides nothing.
- **A size condition needs a database, so the offline commands cannot apply one.** `check` and `list` never connect, so there is no row count for `min_rows` to be tested against and a rule carrying it is left unapplied — the same answer the engine gives a table whose catalog holds no estimate. Where that matters is `max_age_days`: a size-gated threshold governs what `generate` does, and a print that records no threshold of its own is judged offline against the rules that match by name alone. Both commands say so on stderr, naming the tables, rather than leaving the number unexplained.
- **The size is usually a catalog estimate, so the bar is fuzzy near the boundary.** Postgres reports a planner statistic that lags writes and is unset until the table is `ANALYZE`d; MySQL's InnoDB `table_rows` is approximate by design; duckdb's and ClickHouse's are exact counts. A table sitting close to the threshold may fall either side of it between runs. When the catalog has no number at all the rule does **not** apply — sampling degrades the artifact, so an unknown size takes the un-narrowed path, and the run says so on stderr rather than deciding silently.
- **A config with neither `min_rows` nor `max_rows` anywhere costs nothing.** No estimate is fetched, and the run issues no statement on a table's account beyond the ones it profiles it with. Either key, at any level, turns the pre-flight on for a table — a ceiling needs the estimate to derive its fraction. A plain view never gets one: it has no row-count estimate, so none is fetched and no size rule can govern it, whatever its mode. Nor does an object described from the catalog, unless a `min_rows` rule that would change its mode names it.
- A narrowed run takes the table's `row_count` from the catalog rather than counting it, so `row_count_method` reports `approximate` (duckdb and ClickHouse count it and report `exact`), and the emitted `statistics.yaml` carries the `scope` block naming `rows_scanned` with every ratio computed against it. A table crossing the bar therefore changes the shape of its artifact with no schema change, and `diff` shows that — there is no hysteresis.
- `dbprint check` judges each print against the threshold its own manifest entry records — the one the run that wrote it skipped it against. An entry recording none falls back to the rules, and offline that is the rules matching by name (see the size-condition note above). An explicit `--max-age` overrides every table's threshold directly and reads no rule to find one.

### `profile.redact`

An ordered list. Each entry names the columns it covers and what to do with their **cell values**; everything measured about those columns is left alone.

```yaml
connections:
  production:
    profile:
      redact:
        - columns: ["*.collector.email", "*.curator.*_name"] # selector globs over <table>.<column>
          with: mask                                         # OPTIONAL; mask | drop | hash
        - sensitivity: [personal_name, postal_address]       # matches inferred.sensitivity
          with: drop
        - looks_like: [email]                                # matches inferred.looks_like
          with: hash
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `columns` | list of glob | `[]` | Globs over the qualified `<table>.<column>` |
| `sensitivity` | list of enum | `[]` | Covers columns whose `inferred.sensitivity` is listed. One of `personal_name`, `postal_address`, `geolocation`, `date_of_birth`, `national_id`, `financial_account`, `credential`, `health`, `demographic`, `employment`, `contact`, `online_identifier` |
| `looks_like` | list of enum | `[]` | Covers columns whose `inferred.looks_like` is listed. One of the [SPEC 4.1](format/v1/SPEC.md) patterns |
| `with` | enum | `mask` | `mask` \| `drop` \| `hash` |

- **A rule covers a column matching ANY of its three criteria.** Rules apply in declaration order and the last matching one decides the primitive. A rule naming none of the three is rejected at load — it would cover everything, which is never what writing one means.
- **`sensitivity` and `looks_like` are closed vocabularies, checked at load, case and all.** A value outside the set is rejected by name rather than stored, because a rule targeting a category that does not exist covers nothing and produces a print that reads as redacted. `columns` is an open glob and is not checked: a pattern matching no table today may match one tomorrow.
- **`redact` is a list, so a connection's list replaces the one in `defaults` whole.** A connection that sets its own `redact` drops every `defaults` entry it does not repeat — no primitive means "not redacted" — so a project-wide rule a connection must keep is anchored once under `x-shared` and aliased into each list.
- **The count profile does not change; everything else computed from the values does.** `null_count`, `null_rate`, `cardinality`, `cardinality_ratio`, the value counts, `values_coverage`, `distribution` and `frequencies` are identical to an unredacted run. `range` bounds and `percentiles` are cell values and receive the same primitive; under `drop` they are omitted entirely. `mean`, `sum`, `length`, `zero_count`, `negative_count`, `non_finite_count`, `empty_count`, `quantized_count`, `normalized_cardinality` and `unrepresentable` are withheld under every primitive, since beside the value counts each solves for the literals. `range.span_days` and `freshness.max_age_days` are floored to the nearest 90 days under **every** primitive, `drop` included, since an exact age narrows the values it was computed from.
- **The column declares it** with a `redacted` marker naming the primitive, so a consumer can tell a measurement from a substitution. A `check` predicate over `accepted_values`, `range`, `percentiles` or `freshness.max_age_days` on a redacted column is refused rather than evaluated against placeholders or against a coarsened figure. A column a rule covers carries the marker even where it publishes no cell value — a `text` column detected as prose (SPEC 2.2.3's enumeration exemption), or one whose values were not measured — so a withheld column never reads as an ordinary one; only `json` and `unsupported` columns, whose classification carries no cell value, never carry it. The absence of a marker means no rule covers the column, so its emitted values are the real ones.
- **A rule reaches a column's parts through the column.** A part inside an array, record, map or document ([SPEC 2.2.18](format/v1/SPEC.md)) is covered by every rule covering its column, and by a `sensitivity` or `looks_like` rule listing the part's own detection or that of any part it sits inside, so an email key inside a document is caught by the `looks_like: [email]` rule above, and every field of a `mailing_address` object inside a document by a `sensitivity: [postal_address]` rule. A covered part carries its own `redacted` marker; the column above it carries none. A `columns` glob never names a part.
- **A detected category with no rule covering it is reported, not silenced.** `dbprint check` carries `privacy.unredacted-sensitive` (a warning; SPEC §4.4.2) for a column that names its own `inferred.sensitivity`, or a part inside a column or part that does, and still publishes a cell value nothing withheld. Writing the rule above is what clears it — the check reads the committed print, not this file.
- **`hash` requires a salt and is rejected without one.** An unsalted digest of an email is reversible by dictionary attack in minutes. The salt lives with the credentials — `redaction_salt` in `~/.dbprint/connections.yaml`, or `DBPRINT_<CONNECTION>_REDACTION_SALT` — never in `.dbprint.yaml`, which is committed. Keep it stable per project or every redacted column churns on every diff. A value that is empty or only whitespace is not a salt: `DBPRINT_<CONNECTION>_REDACTION_SALT=` is what a shell produces when a secret did not resolve, and it is refused exactly as an absent one is. A digest is taken of the value as the artifact spells it, so the same number digests identically in a `DECIMAL` and an integer column.
- **Detection is unaffected.** `looks_like` and `sensitivity` run over sampled values that are never written, so a hashed email column still reports `looks_like: email`. The shape claim describes the column, not the emitted literals.

### `diff`

Presentation thresholds for `dbprint diff`'s human output. Machine output (`--format json` / `yaml`) is always unfiltered.

```yaml
diff:
  stat_change_threshold:
    cardinality_ratio: 0.02
    percentile_pct: 0.05
    values_coverage: 0.05
    default: 0.01               # every statistic without its own entry
```

Each threshold is a fraction in `[0, 1]`. The four keys above are the whole accepted set.

`--threshold` overrides every per-stat value for one run and is parsed by the CLI, so it is not subject to this check.

### `assertions`

Data-quality checks evaluated by `dbprint check`. The loader checks only that the block is a mapping; the assertion layer interprets the rest and reports a malformed entry at evaluation. Its grammar, severities and exit codes are specified in [ASSERTIONS.md](ASSERTIONS.md).

---

## `--project` locators

Every command except `init` accepts `--project`, pointing it at a project without a `cd`; `init` scaffolds in the current directory only. Local by default: a directory whose direct child is `.dbprint.yaml`, or that file itself — never an upward walk, never a downward scan.

`--project` also accepts a git address, so a project committed to a repository can be read without cloning it by hand first:

| Form | Resolves to |
|---|---|
| `https://github.com/<owner>/<repo>` | `.dbprint.yaml` at the repository root, default branch |
| `git@github.com:<owner>/<repo>.git` | Same, over SSH |
| `https://github.com/<owner>/<repo>/blob/<ref>/<path>/` | `<path>/.dbprint.yaml` at `<ref>` |
| `https://github.com/<owner>/<repo>/blob/<ref>/<path>/.dbprint.yaml` | The same file, named directly |
| `<git-url>#<ref>:<subpath>` | Explicit form — any git URL and ref not beginning with `-`, any subpath inside the repository |

GitLab (`/-/blob/<ref>/<path>`) and Bitbucket (`/src/<ref>/<path>`) web URLs parse the same way. A bare remote always means the repository root at its default branch — a `.dbprint.yaml` nested under one is never discovered from the bare form.

`<ref>` is anything git can check out: a branch, a tag, a full commit SHA or a short one — so a permalink copied from a forge, which pins a commit, resolves like any other address. A branch and a tag are re-read at most once every 15 minutes; a commit cannot move, so an address pinned to one is fetched when it is first cached and never again.

---

## `~/.dbprint/connections.yaml`

Keyed by connection name, matching `.dbprint.yaml`. Never commit it. The entry for the connection a command runs is checked like `.dbprint.yaml`: a key its adapter does not take is refused with the nearest valid name, as is a nested value, together with any missing required key, in one error that names keys and never values — exit `1`. Other connections' entries are left alone, since the file is shared by every project on the host.

```yaml
production:
  host: db.internal
  port: 5432
  database: arboretum
  user: dbprint_ro
  password: ...
```

### Required keys per adapter

| Adapter | Required | Optional |
|---|---|---|
| PostgreSQL | `host`, `port`, `user`, `password` | `database`, `redaction_salt` |
| MySQL | `host`, `port`, `user`, `password` | `database`, `redaction_salt` |
| Snowflake | `account`, `user`, `warehouse`, `role` | `database`, `password`, `private_key_file`, `private_key_file_pwd`, `schema` (requires `database`), `redaction_salt` |
| duckdb | `database` (a file path relative to the project root, or `:memory:`) | `read_only`, `redaction_salt` |
| ClickHouse | `host` | `database`, `port` (default `8123`), `user` (default `default`), `password`, `redaction_salt` |
| Redshift | `host`, `user`, `password` | `database`, `port` (default `5439`), `redaction_salt` |
| Databricks | `server_hostname`, `http_path`, `access_token` | `catalog`, `redaction_salt` |
| BigQuery | `project` | `dataset`, `credentials_file`, `redaction_salt` |

Snowflake takes **exactly one** of `password` or `private_key_file`; supplying both, or neither, is an error. `private_key_file_pwd` decrypts an encrypted key.

BigQuery has no `password` key at all: credentials resolve through Application Default Credentials, the `google-cloud-bigquery` client's own mechanism, unless `credentials_file` names a service account key explicitly.

`database` (duckdb), `credentials_file` (BigQuery) and `private_key_file` (Snowflake) are file paths. A relative one resolves against the project root, whichever source supplies it; a leading `~` expands to the home directory; and a path that does not exist is refused with exit `4` rather than created or handed to the driver. A value naming no local file — duckdb's `:memory:`, or a URI such as `md:` — passes through unchanged.

The namespace key — `database`, `catalog` on Databricks, `dataset` on BigQuery — is optional everywhere but duckdb. Set, the connection reads that one namespace; omitted, it reads every namespace the credentials can see, less the vendor's own system namespaces, and `tables.include`/`tables.exclude` are the only scoping (`tables: {include: ["arboretum.*", "garden.*"]}`). PostgreSQL, Redshift and Databricks name a table `database.schema.table` (`catalog.schema.table`) whether or not the key is set, so adding a second namespace renames nothing; a namespace that cannot be listed is skipped with a warning. Each adapter page states what it enumerates and the grant that needs.

Every unresolved required key is collected and reported in one error rather than one at a time.

### Resolution order

Per key, first hit wins:

1. `DBPRINT_<CONNECTION>_<KEY>` environment variable — connection name and key upper-cased
2. `~/.dbprint/connections.yaml`
3. `DBPRINT_<CONNECTION>_<KEY>` in the project's `.env`

So `DBPRINT_PRODUCTION_PASSWORD` overrides the file entry for `production`, and a `.env` entry serves as the fallback a checkout can carry without a user-level file.

**A variable carrying no value is skipped, not used.** `DBPRINT_PRODUCTION_HOST=` — set to empty, or to whitespace — is what a shell produces when a secret did not resolve, so resolution continues to the next source rather than handing an empty credential to the adapter. A `.env` entry with nothing after the `=` is skipped the same way, as is a bare key with no `=` at all. An empty value in `~/.dbprint/connections.yaml` (`host: ""`) is deliberate and is used as written.

**`password` is the exception, because its empty value is a credential.** A cluster configured for `trust` authentication is reached with no password at all, and `DBPRINT_<CONNECTION>_PASSWORD=` is how a runner says so. An empty password is therefore taken as given, from any source.
