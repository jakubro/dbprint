# dbprint Configuration

> **Purpose**: every key dbprint reads from `.dbprint.yaml` and
> `~/.dbprint/connections.yaml`. For the on-disk output these settings produce, see
> [format/v1/SPEC.md](format/v1/SPEC.md) (normative). For the command surface, see
> [CLI.md](CLI.md). For the `assertions:` grammar, see [ASSERTIONS.md](ASSERTIONS.md).

Two files, with different lifetimes:

| File | Committed? | Holds |
|---|---|---|
| `.dbprint.yaml` | **Yes** — it describes the project | Connections, rules, tuning |
| `~/.dbprint/connections.yaml` | **No** — credentials | Host, user, password, keys |

`dbprint init` writes a starting pair. A worked `.dbprint.yaml` ships at
[`format/v1/examples/production/.dbprint.yaml`](format/v1/examples/production/.dbprint.yaml)
alongside the print it produces.

**Unknown keys are ignored, not rejected.** dbprint reads exactly the keys below; anything
else is dropped silently, so a misspelled or mis-nested key leaves the default in place
without a warning. When something appears to have no effect, check the spelling and the
nesting depth first.

---

## `.dbprint.yaml`

```yaml
defaults:                       # OPTIONAL; each key cascades into every connection. A connection-level value overrides it, except for `rules` and `redact`, which append — see below
  output: prints
  include: ["<PATTERN>"]
  exclude: ["<PATTERN>"]
  max_age_days: 7
  infer_relationships: true
  sketch_all_columns: false
  statistics: { ... }
  rules: [ ... ]
  redact: [ ... ]
  diff: { ... }

connections:                    # REQUIRED; at least one
  <name>:                       # free-form identifier; also the print subdirectory
    adapter: postgres | snowflake | mysql   # REQUIRED
    auto: false
    output: prints
    include: ["<PATTERN>"]
    exclude: ["<PATTERN>"]
    max_age_days: 7
    max_rows_scanned: <INT>       # absent by default
    infer_relationships: true
    materialize_sample: true
    sketch_all_columns: false
    statistics: { ... }
    rules: [ ... ]
    redact: [ ... ]
    diff: { ... }
    assertions: { ... }
```

`defaults` accepts every connection key except `adapter`, `auto` and `assertions`, which are
per-connection only. `rules` and `redact` are the two keys that do not override: a connection's
entries are appended to the ones from `defaults`, which is what makes a connection entry win.

### Connection keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `adapter` | enum | — (required) | `postgres` \| `snowflake` \| `mysql` |
| `auto` | bool | `false` | Run this connection on a bare `dbprint <command>` with no `CONN` argument. Any number of connections may set it |
| `output` | path | `prints` | Root directory, relative to `.dbprint.yaml`. Prints land in `<output>/<name>/` — the connection name is appended, so do not include it |
| `include` | list of glob | `["*"]` | Tables to profile. **Omitting this profiles everything the connection can see** |
| `exclude` | list of glob | `[]` | Removed from the include set |
| `max_age_days` | int ≥ 0 | `7` | A print younger than this is left alone by `generate`, and passes `check`'s freshness gate. A `rules` entry can override it per table, and can condition that override on the table's size. `0` re-profiles on every run — see below. A negative value is refused at load |
| `max_rows_scanned` | int ≥ 1 | absent | A row-count ceiling covering every table this connection profiles; `defaults` and `rules` may also carry it. See below |
| `infer_relationships` | bool | `true` | Derive the foreign keys the catalog does not declare, from column naming — see below |
| `materialize_sample` | bool | `true` | Draw a sampled table's rows once into a temporary table, so every statistic for that table describes the same rows. Takes a temporary-table privilege on PostgreSQL and MySQL, none on Snowflake — see below |
| `sketch_all_columns` | bool | `false` | Sketch every sketchable-type column, not only the smaller required set — see below |
| `statistics` | map | see below | What gets measured per column, and how much of it |
| `rules` | list | `[]` | Per-table overrides of the keys above, plus the two that narrow what is read |
| `redact` | list | `[]` | What to do with the cell values of the columns each entry covers |
| `diff` | map | see below | Presentation thresholds for `dbprint diff`'s human output |
| `assertions` | map | `{}` | Data-quality checks `dbprint check` evaluates |

The five block-valued keys each have their own section below.

**`infer_relationships` derives the foreign keys a schema never declared.** Plenty of
warehouses declare none — Snowflake does not enforce them, and analytics schemas in
PostgreSQL routinely skip them — so a print of one carries an empty relationship graph and
a reader cannot tell that `invoice.user_id` points at `user.id`. With the key on, a column
named `<stem>_id` whose stem resolves to an in-scope **table** declaring a single-column
key of a compatible type becomes an edge marked `detection: inferred`; the rule and its
refusals are specified in [SPEC 2.3.8](format/v1/SPEC.md). Turning it off removes every one
of those edges — the graph then carries only what the catalog declares — and skips the
catalog pre-pass that reads the columns and declared keys inference resolves against.

**`materialize_sample` is the one setting that makes dbprint write to your database.** A sampled
table is read by many statements, and a sampling construct re-evaluated per statement draws a
fresh set of rows each time — so a column's listed value counts and the non-null figure they are a
share of come from different reads, and the two disagree on a table nobody wrote to. With the key
on, the producer copies the draw into a temporary table once and every statement for that table
reads that instead. What the write costs you: a temporary-table privilege where the copy is made,
spelled differently per engine — `TEMPORARY` on the database for PostgreSQL, `CREATE TEMPORARY
TABLES` on the database for MySQL, and nothing at all on Snowflake, which exempts temporary tables
from the schema's `CREATE TABLE` privilege. The copy lands in the session's own temporary space on
PostgreSQL and MySQL and in the profiled table's own schema on Snowflake. The object holds the
sampled fraction only rather than the whole table,
and its lifetime is the session, so it is gone when the run ends whether the run succeeded or not.
Where the privilege is absent the run does **not** fail: it falls back to sampling per statement,
warns on stderr, and the incoherence above is back. Turning the key off chooses that fallback
deliberately, which is the setting an organisation whose policy forbids the tool writing anything
wants. A table that is not sampled never materializes — a full scan has nothing to copy, and a
`filter` is a predicate, so re-evaluating it selects the same rows every time.

**`sketch_all_columns` widens the second setting that changes what leaves the database.**
[SPEC 2.2.14](format/v1/SPEC.md) always sketches a column named by an edge, plus every
declared-unique column, every column at or below the sketch's own retained size, and every
column carrying a measured candidate key — a fixed-size summary of a column's distinct
values, from which a consumer computes set overlap against another column offline, no query
against either source. With the key on, every column whose type the sketch format covers is
sketched, whether or not it fits one of those four categories, at the cost of one extra query
per newly-sketched column. A KMV sketch is an unsalted hash of real cell values, so turning
this on widens that surface to every column redaction did not withhold, on top of whatever
the required set already carries.

The key has no effect on a table that carries a `scope` block, or on a plain view: neither is
sketched at all, whatever this is set to. A sketch answers set-overlap questions between two
columns, which needs a reproducible read of the whole column — a narrowed read cannot give
one, and a view is never queried. Since a large warehouse is also where sampling gets turned
on, expect the two settings to meet: a table narrowed by `sample`, `filter` or
`max_rows_scanned` publishes no sketches regardless.

**`max_age_days: 0` means the print is stale the moment it is written.** `generate`
re-extracts it every run, and `check`'s freshness gate cannot pass at `0` whatever order the
commands run in. Use it where `check` does not gate the pipeline, or pass `check --max-age`
explicitly — an explicit flag overrides every table's recorded threshold. A negative value
asks for the same thing and is refused at load, because it holds every table stale with no
way for the artifact to say so.

`include` and `exclude` decide **which** tables are profiled; `rules` decides **how** each one
is profiled. The two axes never mix: a rule cannot bring a table into scope. A rule selects
within the second axis — `include` / `exclude` / `min_rows` narrow which of the profiled
tables that rule governs, and none of them can widen the connection's scope.

Patterns are `fnmatch` globs over the lowercased fully-qualified name, so matching is
case-insensitive. `*` spans dot separators. The FQN shape is the adapter's:
`database.schema.table` (Snowflake), `schema.table` (PostgreSQL), `database.table` (MySQL).

### `statistics`

Tuning for [SPEC 2.2](format/v1/SPEC.md). Every key is optional.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enumeration_threshold` | int | `50` | Cardinality ≤ this makes a column `categorical`; above it the column classifies by type and may carry a range instead |
| `top_n_values` | int | `20` | Cap on the `values` list. A column with at most this many distinct values is enumerated in full, whatever its classification |
| `top_n_null_patterns` | int | `20` | Cap on the `null_patterns` list — how many distinct combinations of null columns a table publishes. `null_patterns.coverage` states what share of the rows the listed combinations account for |
| `looks_like_sample_size` | int | `1000` | Distinct non-null values sampled for `inferred.looks_like` detection |
| `percentiles` | list of int | `[1, 25, 50, 75, 99]` | **Integer percents in 1..99.** Fractions such as `0.25` are rejected at load |

Lowering `enumeration_threshold` is the cheapest way to cut cost on a wide table: the
`values` list is bounded by `top_n_values`, and `values_coverage` states how much of the column it covers.

### `rules`

An ordered list. Each entry carries a matcher and the settings it overrides for the tables it
matches, so one connection can sample a billion-row fact table without sampling anything else,
and refresh dimensions daily while refreshing that fact table weekly.

```yaml
rules:
  - include: ["analytics.events*"]        # OPTIONAL; defaults to ["*"]
    exclude: ["analytics.events_v2"]      # OPTIONAL; defaults to []
    min_rows: 500000000                   # OPTIONAL; only tables at least this large
    sample: 0.01                          # OPTIONAL; fraction in (0, 1]. Excludes `filter`
    statistics: {top_n_values: 5}         # OPTIONAL; merged key by key
    max_age_days: 30                      # OPTIONAL
  - include: ["analytics.orders"]
    filter: "created_at >= current_date - interval '30 days'"   # OPTIONAL. Excludes `sample`
  - include: ["analytics.big_*"]
    max_rows_scanned: 1000000000          # OPTIONAL; also valid on a connection or in `defaults`
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `include` | list of glob | `["*"]` | Tables this rule governs. Same glob grammar as the connection's `include` |
| `exclude` | list of glob | `[]` | Removed from this rule's own match |
| `min_rows` | int | absent | Rule applies only to tables of at least this many rows. Positive integers only |
| `sample` | float | absent | Fraction of the table to read, per [SPEC 2.2.8](format/v1/SPEC.md). Never alongside `filter` |
| `filter` | string | absent | One SQL predicate, applied verbatim. Never alongside `sample` |
| `max_rows_scanned` | int ≥ 1 | absent | A row-count ceiling, resolved into a `sample` fraction against the table's catalog estimate. See below |
| `statistics` | map | `{}` | Any subset of the `statistics` keys, merged onto the connection's |
| `max_age_days` | int ≥ 0 | absent | Freshness threshold for these tables. Same bound and same meaning of `0` as the connection key |

- **Every matching rule applies, in declaration order, and later ones win.** Rules from
  `defaults` are walked before the connection's own. Each of `sample`, `filter` and
  `max_age_days` is last-wins against another rule setting the same key; `statistics` merges key
  by key, so two rules can set different keys and both hold. `sample` and `filter` are the one
  pair that does not resolve this way — see below.
- A rule that matches a table but sets nothing is rejected at load rather than ignored — that
  shape is almost always a mis-nested key, which the ignore-unknown-keys policy would swallow.
- The mirror shape is rejected for the same reason: a rule whose `include` is an empty list
  matches no table and would never fire. Omitting `include` is the way to match everything;
  `include: []` matches nothing, so it is refused rather than silently ignored. An empty
  `exclude` is fine — it removes nothing.
- **`sample`, `filter` and `min_rows` are read only inside a rule.** Placing any of them directly
  on a connection or in `defaults` is rejected by name rather than dropped, because it is the
  predictable way to mis-migrate a config and the ignore-unknown-keys policy would otherwise
  swallow it. That deny list is exactly `scope`, `sample`, `filter` and `min_rows`; every other
  unrecognized key is still ignored silently, and `statistics` and `max_age_days` are read at
  connection level as normal.
- Two matching filters never combine: the later predicate replaces the earlier one, so no query
  runs a condition neither rule authored.
- **A table is narrowed by a predicate or by a fraction, never both.** A rule carrying `sample` and
  `filter` together is rejected at load; rules that each carry one and match the same table are
  rejected when that table resolves, naming both of them. Neither key silently clears the other,
  because a config that reads as narrowing two ways must not quietly do one. To sample a slice,
  widen the predicate until it describes the rows you want.
- `inferred.looks_like` honors both `filter` and `sample`: it must not describe rows outside
  the artifact, and its own draw composes with the sample fraction rather than replacing it -
  so honouring `sample` costs no extra rows. Composition is population-level only on MySQL and
  Snowflake, which take no seed on this sub-draw; only Postgres coheres row for row.
- **A sampled table reads one row set within a run, and on PostgreSQL the same one next run.**
  One table's profile issues many statements against the same narrowed source, so an unseeded
  fraction would describe different rows per field on a table nobody wrote to. What prevents
  that is the materialized copy, not the seed. All three adapters also seed the fraction from
  the table's own name, but only PostgreSQL's `TABLESAMPLE ... REPEATABLE` documents a stable
  draw across runs: MySQL's seeded predicate holds only while scan order does, and Snowflake
  does not document two evaluations of one seeded expression reading the same rows — which is
  precisely why the copy exists. A drift-gating consumer should not read run-to-run stability
  as a guarantee off PostgreSQL. The exception is the extra draw
  `inferred.looks_like` takes on top of that row set, which takes no seed on MySQL or
  Snowflake — so on those two the shape claim agrees with the rest of the profile at the
  population level rather than row for row.
- **`min_rows` selects by size, and both conditions must hold.** A rule carrying it governs a
  table only when the name matchers admit it *and* it is at least that large, so
  `min_rows: 500000000` with `sample: 0.01` samples the tables that are too big to scan
  without naming them one by one. It gates whatever the rule sets — `sample`, `filter`,
  `statistics` and `max_age_days` alike — because it sits on the matcher rather than on one
  key. It is a matcher, not a setting: a rule carrying `min_rows` and nothing else selects a
  set of tables and does nothing to them, and is rejected at load like any other rule that
  overrides nothing.
- **A size condition needs a database, so the offline commands cannot apply one.** `check` and
  `list` never connect, so there is no row count for `min_rows` to be tested against and a rule
  carrying it is left unapplied — the same answer the engine gives a table whose catalog holds
  no estimate. Where that matters is `max_age_days`: a size-gated threshold governs what
  `generate` does, and a print that records no threshold of its own is judged offline against
  the rules that match by name alone. Both commands say so on stderr, naming the tables, rather
  than leaving the number unexplained.
- **The size is a catalog estimate, so the bar is fuzzy near the boundary.** Postgres reports
  a planner statistic that lags writes and is unset until the table is `ANALYZE`d; MySQL's
  InnoDB `table_rows` is approximate by design. A table sitting close to the threshold may
  fall either side of it between runs. When the catalog has no number at all the rule does
  **not** apply — sampling degrades the artifact, so an unknown size takes the un-narrowed
  path, and the run says so on stderr rather than deciding silently.
- **A config with neither `min_rows` nor `max_rows_scanned` anywhere costs nothing.** No estimate
  is fetched and the run issues exactly the statements it issued before the keys existed. Either
  key, at any level, turns the pre-flight on — a ceiling needs the estimate to derive its fraction.
- A narrowed run takes the table's `row_count` from the catalog rather than counting it, so
  `row_count_method` reports `approximate`, and the emitted `statistics.yaml` carries the
  `scope` block naming `rows_scanned` with every ratio computed against it. A table crossing
  the bar therefore changes the shape of its artifact with no schema change, and `diff` shows
  that — there is no hysteresis.
- `dbprint check` judges each print against the threshold its own manifest entry records — the
  one the run that wrote it skipped it against. An entry recording none falls back to the rules,
  and offline that is the rules matching by name (see the size-condition note above). An
  explicit `--max-age` overrides every table's threshold directly and reads no rule to find
  one — but the rules are still read to catch the structural error below, since that is a
  property of the configuration independent of freshness.
- **A cascade that resolves one table to both a `filter` and a `sample` is refused.** Offline
  the refusal is contained to that one table: `check` reports it as a check that did not run
  and still judges every other table in the connection; `list` reports the cause and skips
  that connection, since its output is aggregate counts and a table with no threshold has no
  bucket. Neither command aborts, and neither loses a connection it had already summarised.
  Under `check`'s default (no `--max-age`), the refusal costs the connection its exit code —
  `1`. Under an explicit `--max-age` the refusal is still reported, on stderr and in the
  machine envelope, but does not move the exit: the override already governs every table's
  freshness, so a scope error the override does not depend on cannot fail a run it decided.

> **The predicate is interpolated, not bound.** It is your SQL, passed verbatim into every
> statistics query for that table; dbprint never parses, rewrites or validates it, because
> [SPEC 2.2.8](format/v1/SPEC.md) requires it recorded as written. Treat `.dbprint.yaml` as
> carrying the same trust as the credentials file — it already names the connection whose
> credentials a run uses.

#### `max_rows_scanned`

A row-count ceiling states the cost an operator can afford directly, in rows, rather than as a
fraction — the engine derives the fraction from a catalog estimate, fetched for this key as it is
for `min_rows`.

- **A ceiling is a different policy from a fraction, not another way to spell one.** `sample`
  reads a fixed share regardless of table size; a ceiling caps the rows the draw *returns*, so a
  table under it is read whole and every table over it yields the same number of rows regardless
  of how far over. What a ceiling bounds is the downstream work — the rows aggregated, `rows_scanned`,
  and on Snowflake the warehouse time, since `SAMPLE SYSTEM` prunes at block level. It does not
  bound what leaves the disk on PostgreSQL or MySQL: `TABLESAMPLE BERNOULLI` tests rows
  individually and a `RAND() < p` predicate is unindexable, so both scan the whole table however
  small the fraction. Migrating a `min_rows`/`sample` ladder built to
  approximate a cost curve to one `max_rows_scanned` value changes what gets read at the low
  end — a table just over the ceiling is now read whole rather than sampled — and that is the
  intended difference, not a bug.
- **Unlike `sample`, `filter` and `min_rows`, a ceiling is legal at connection and `defaults`
  level as well as inside a rule.** It cascades exactly like `max_age_days`: the connection's
  own value wins over `defaults`, and a rule's value — at whatever level it is declared —
  overrides both for the tables it names. It is deliberately absent from the deny list that
  rejects the other three outside a rule, because a project-wide budget is the point of the
  feature.
- **A resolved fraction of exactly `1.0` is not a sample.** When the ceiling is at or above a
  table's catalog estimate, the table is read whole: no `scope` block, no `row_count_method:
  approximate`, and `row_count` is counted rather than estimated. `sample: 1.0` never reaches
  the artifact through this path.
- **The resolved fraction snaps down to a geometric grid, 10% per step.** A pure function of
  the ceiling and the estimate alone — no run-to-run state — so a catalog estimate that drifted
  by a few percent (`ANALYZE` noise, InnoDB's approximate `table_rows`) resolves to the same
  fraction it did last run, and `diff` reports nothing. A table that genuinely changed size by
  10% or more crosses at least one grid step, and `diff` shows the statistics move. Snapping
  down, never up, keeps the ceiling a true ceiling: `rows_scanned` never exceeds
  `max_rows_scanned` because of the grid.
- **A ceiling and an explicit `sample` cascade on the same timeline.** Whichever was set later —
  by declaration order, connection value first, then `defaults` rules, then the connection's
  own — wins outright; the earlier one is discarded rather than blended, and a ceiling a later
  `sample` overrides is never converted to a fraction. One rule setting both prefers its own
  `sample`.
- **A ceiling meeting a `filter` yields to it, with a warning, rather than being refused.**
  `sample` and `filter` are mutually exclusive and a cascade resolving to both is a load error
  (above) — a ceiling is not a third narrowing directive competing for that slot, since a
  connection-wide ceiling would otherwise collide with every filtered table on every run. A
  predicate already bounds cost, so the ceiling stands down and `generate` says so on stderr,
  naming the table.
- **A ceiling gates nothing offline.** `check` and `list` never connect, so a ceiling never
  resolves there — unlike `min_rows`, it does not affect what an offline command reads, because
  it governs only what `generate` scans.

### `redact`

An ordered list. Each entry names the columns it covers and what to do with their **cell
values**; everything measured about those columns is left alone.

```yaml
connections:
  production:
    redact:
      - columns: ["*.users.email", "*.customers.*_name"]   # selector globs over <fqn>.<column>
        with: mask                                         # OPTIONAL; mask | drop | hash
      - sensitivity: [personal_name, postal_address]       # matches inferred.sensitivity
        with: drop
      - looks_like: [email]                                # matches inferred.looks_like
        with: hash
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `columns` | list of glob | `[]` | Globs over the qualified `<fqn>.<column>` |
| `sensitivity` | list of enum | `[]` | Covers columns whose `inferred.sensitivity` is listed. One of `personal_name`, `postal_address`, `geolocation`, `date_of_birth`, `national_id`, `financial_account`, `credential`, `health`, `demographic`, `employment`, `contact`, `online_identifier` |
| `looks_like` | list of enum | `[]` | Covers columns whose `inferred.looks_like` is listed. One of the [SPEC 4.1](format/v1/SPEC.md) patterns |
| `with` | enum | `mask` | `mask` \| `drop` \| `hash` |

- **A rule covers a column matching ANY of its three criteria.** Rules apply in declaration
  order and the last matching one decides the primitive, the same resolution `rules` uses. A
  rule naming none of the three is rejected at load — it would cover everything, which is never
  what writing one means.
- **`sensitivity` and `looks_like` are closed vocabularies, checked at load.** A value outside
  the set is rejected by name rather than stored, because a rule targeting a category that does
  not exist covers nothing and produces a print that reads as redacted. `columns` is an open
  glob and is not checked: a pattern matching no table today may match one tomorrow.
- **`redact` cascades from `defaults` and concatenates**, the same way `rules` does: the
  `defaults` entries are walked first, then the connection's own, and the last matching entry
  decides the primitive. So a connection can change what a project-wide rule applies to a
  column — `mask` to `hash` — but cannot lift the coverage, because no primitive means "not
  redacted". A connection that must stay unredacted is one whose rule does not belong in
  `defaults`.
- **Counts do not change; cell values and two derived day counts do.** `null_count`, `null_rate`,
  `cardinality`, `cardinality_ratio`, the value counts, `values_coverage` and `distribution` are
  identical to an unredacted run. `range` bounds and `percentiles` are cell values and receive the
  same primitive; under `drop` they are omitted entirely, along with `unrepresentable`. The two
  exceptions are derived rather than measured: `range.span_days` and `freshness.max_age_days` are
  floored to the nearest 90 days under **every** primitive, `drop` included, since an exact age
  narrows the values it was computed from.
- **The column declares it** with a `redacted` marker naming the primitive, so a consumer can
  tell a measurement from a substitution. A `check` predicate over `accepted_values`, `range`,
  `percentiles` or `freshness.max_age_days` on a redacted column is refused rather than evaluated
  against placeholders or against a coarsened figure.
  A column a rule covers but that publishes no cell value at all carries no marker, because
  nothing was withheld from it — whether that is because its classification never carries one
  (`json`, `unsupported`) or because a `text` column detected as prose published
  no value list for an unrelated reason (SPEC 2.2.3's enumeration exemption). The absence of a
  marker means the emitted values are the real ones, which stays true either way.
- **A detected category with no rule covering it is reported, not silenced.** `dbprint check`
  carries `privacy.unredacted-sensitive` (a warning; SPEC §4.4.2) for a column that names its
  own `inferred.sensitivity` and still publishes a cell value nothing withheld. Writing the
  rule above is what clears it — the check reads the committed print, not this file.
- **`hash` requires a salt and is rejected without one.** An unsalted digest of an email is
  reversible by dictionary attack in minutes. The salt lives with the credentials —
  `redaction_salt` in `~/.dbprint/connections.yaml`, or
  `DBPRINT_<CONN>_REDACTION_SALT` — never in `.dbprint.yaml`, which is committed. Keep it
  stable per project or every redacted column churns on every diff.
- **Detection is unaffected.** `looks_like` and `sensitivity` run over sampled values that are
  never written, so a hashed email column still reports `looks_like: email`. The shape claim
  describes the column, not the emitted literals.

### `diff`

Presentation thresholds for `dbprint diff`'s human output. Machine output (`--format json`
/ `yaml`) is always unfiltered.

```yaml
diff:
  stat_change_threshold:
    cardinality_ratio: 0.02
    percentile_pct: 0.05
    values_coverage: 0.05
    default: 0.01               # every statistic without its own entry
```

Each threshold is a fraction in `[0, 1]` and is checked when the config loads: a value that is
not a number, or one outside that range, is refused with the file, the connection and the key.
The four keys above are the whole accepted set, and a key outside it is refused rather than
ignored — an unread key would leave `default` governing the statistic its author meant to
configure, which is indistinguishable from a working config.

`--threshold` overrides every per-stat value for one run and is parsed by the CLI, so it is not
subject to this check.

### `assertions`

Data-quality checks evaluated by `dbprint check`. The block is stored unparsed by the config
loader and interpreted by the assertion layer; its grammar, severities and exit codes are
specified in [ASSERTIONS.md](ASSERTIONS.md).

---

## `--project` locators

Every command except `init` accepts `--project`, pointing it at a project without a `cd`; `init`
scaffolds in the current directory only. Local by default: a
directory whose direct child is `.dbprint.yaml`, or that file itself - never an upward walk,
never a downward scan.

`--project` also accepts a git address, so a project committed to a repository can be read
without cloning it by hand first:

| Form | Resolves to |
|---|---|
| `https://github.com/<owner>/<repo>` | `.dbprint.yaml` at the repository root, default branch |
| `git@github.com:<owner>/<repo>.git` | Same, over SSH |
| `https://github.com/<owner>/<repo>/blob/<ref>/<path>/` | `<path>/.dbprint.yaml` at `<ref>` |
| `https://github.com/<owner>/<repo>/blob/<ref>/<path>/.dbprint.yaml` | The same file, named directly |
| `<git-url>#<ref>:<subpath>` | Explicit form - any git URL, any ref, any subpath |

GitLab (`/-/blob/<ref>/<path>`) and Bitbucket (`/src/<ref>/<path>`) web URLs parse the same way.
A bare remote always means the repository root at its default branch - a `.dbprint.yaml` nested
under one is never discovered from the bare form.

---

## `~/.dbprint/connections.yaml`

Keyed by connection name, matching `.dbprint.yaml`. Never commit it.

```yaml
production:
  host: db.internal
  port: 5432
  database: analytics
  user: dbprint_ro
  password: ...
```

### Required keys per adapter

| Adapter | Required | Optional |
|---|---|---|
| PostgreSQL | `host`, `port`, `database`, `user`, `password` | `redaction_salt` |
| MySQL | `host`, `port`, `database`, `user`, `password` | `redaction_salt` |
| Snowflake | `account`, `user`, `warehouse`, `database`, `role` | `password`, `private_key_file`, `private_key_file_pwd`, `schema`, `redaction_salt` |

Snowflake takes **exactly one** of `password` or `private_key_file`; supplying both, or
neither, is an error. `private_key_file_pwd` decrypts an encrypted key.

Every unresolved required key is collected and reported in one error rather than one at a
time.

### Resolution order

Per key, first hit wins:

1. `DBPRINT_<CONN>_<KEY>` environment variable — connection name and key upper-cased
2. `~/.dbprint/connections.yaml`
3. `DBPRINT_<CONN>_<KEY>` in the project's `.env`

So `DBPRINT_PRODUCTION_PASSWORD` overrides the file entry for `production`, and a `.env`
entry serves as the fallback a checkout can carry without a user-level file.
