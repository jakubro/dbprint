# Choosing what to profile

A print of a warehouse costs a query per column per table. This page is about spending that budget deliberately: which tables get profiled at all, how much of each one is read, and how the artifact records what it actually looked at.

Two axes, and they never mix. `include` and `exclude` decide **which** tables are profiled. `rules` decide **how** each profiled table is read. A rule cannot bring a table into scope; it can only narrow what happens to one already there.

## Which tables

`include` and `exclude` are `fnmatch` globs over the lowercased fully-qualified name, so matching is case-insensitive and `*` spans the dot separators.

```yaml
# .dbprint.yaml
connections:
  primary:
    adapter: postgres
    include: ["seedbank.*", "fixture.*"]
    exclude: ["seedbank.audit_*"]
```

Omitting `include` profiles everything the connection can see. On a warehouse that is the difference between a print you commit and one you abandon.

The command line can narrow this further for one run — `--include` intersects with the config and `--exclude` unions with it — but neither can widen beyond what the config already admits. `--dry-run` resolves the whole plan and writes nothing, which is how to see what a scope change would do before it runs.

## How much of each table

A `rules` entry pairs a matcher with the settings it overrides. Every matching rule applies, in declaration order, and later ones win.

```yaml
# .dbprint.yaml
connections:
  primary:
    rules:
      - include: ["seedbank.germination_trial"]
        sample: 0.01
      - include: ["seedbank.accession"]
        filter: "collected_on >= current_date - interval '30 days'"
      - min_rows: 500000000
        max_rows_scanned: 1000000000
```

Three ways to narrow, and a table takes at most one of the first two:

- **`sample`** reads a fraction of the table, drawn once and seeded from the table's own name. Within a run every statistic for the table reads that one draw. Across runs only PostgreSQL and duckdb guarantee the same rows come back; see your engine's adapter page.
- **`filter`** is one SQL predicate, interpolated verbatim into every statistics query for that table. dbprint does not parse or validate it. Treat `.dbprint.yaml` as carrying the same trust as the credentials file.
- **`max_rows_scanned`** is a ceiling in rows rather than a fraction; the engine resolves it against the catalog's row estimate. Unlike the other two it is also legal at connection and `defaults` level, because a project-wide budget is the point of it.

A cascade that resolves one table to both a `sample` and a `filter` is refused rather than silently resolved — a config that reads as narrowing two ways does not quietly do one.

## The gate that covers tables nobody has named

`min_rows` sits on the matcher rather than on a setting, so it gates everything the rule sets:

```yaml
      - min_rows: 500000000
        sample: 0.01
```

That samples every table over half a billion rows without naming any of them — including the one somebody adds next quarter. It is the difference between a scoping config that ages well and one that needs an edit every time the schema grows.

Two consequences worth knowing before relying on it. The size comes from the catalog's own estimate, which lags writes on PostgreSQL and is approximate by design on InnoDB, so a table sitting near the threshold can fall either side of it between runs. And where the catalog has no estimate at all the rule does **not** apply: an unknown size takes the un-narrowed path, and the run says so on stderr rather than deciding silently.

Because `check` and `list` never connect, a size condition cannot be re-evaluated offline. They do not need to: the run that wrote the print resolved the threshold *with* the row count in hand and recorded the result on each table's manifest entry, so a `min_rows` rule that applied online still governs the offline verdict. Only an entry that records no threshold at all — one written before the key existed, or by another producer — falls back to the rules that match by name alone, and both commands warn when that happens.

## What a narrowed read does to the artifact

This is the part worth internalising, because it changes what every number in the file means.

A narrowed table's `statistics.yaml` carries a `scope` block naming `rows_scanned`, and **every ratio in that file is computed against `rows_scanned`, not against `row_count`**. A `null_rate` of `0.04` on a one-percent sample describes the four percent of the sampled rows that were null. It is not a claim about the table.

The same applies to `values_coverage`: a value of `1.0` under a scope means the list is exhaustive over the rows that were scanned, and says nothing about the ones that were not.

`row_count` itself is the count the scan produced whenever the table was read whole, and `row_count_method` reads `exact`. Only a narrowed table takes the catalog's estimate instead, which is what `approximate` marks — and a narrowed table the catalog holds no estimate for is counted with a `COUNT` and reads `exact` too. So `approximate` always means the read was narrowed, while `exact` says nothing either way; `scope` remains the field to read for that.

[SPEC 2.2.8](../format/v1/SPEC.md#228-scope--statistics-over-part-of-a-table) defines the block and the arithmetic; [SPEC 7.1](../format/v1/SPEC.md#71-the-two-absences-that-are-not-absences) covers reading the result. A table crossing a size threshold therefore changes the shape of its own artifact with no schema change at all, and `dbprint diff` shows exactly that.

## What a scope removes outright, rather than rescales

Five whole-table measurements do not just change denominator under a `scope` block - they
stop running:

- **`timeline`** is absent entirely - a bucketed count over a sample is not a timeline.
- **`populated`** goes absent alongside it, on every column, for the same reason: it is the
  timeline anchor's own `min`/`max` over the rows where the *subject* column is non-null, so
  it needs the same whole-table read `timeline` does. Where it is present, it states what the
  data shows - never when the column was added.
- **`grain.search`** is absent - the measured probe never runs. Declared keys are unaffected
  and still publish.
- **`dependencies`** (the functional-dependency block) is forced to `[]` - empty, not absent,
  because the probe read "none found" rather than skipping the question.
- **`sketch`** is absent on every column, and with it every `detection: measured` relationship
  edge into or out of the table - a measured edge needs the sketch pass, which a scoped read
  never takes.

`normalized_cardinality` is the deliberate exception: it is computed over the same scanned set
`cardinality` already is, so it stays eligible under `scope` like every other per-column field.

None of this is what `unmeasured` describes. `scope` is a decision the config made before the
run started; `unmeasured` names a field the run tried for and lost - a catalog read that
failed, a probe that errored. A field a `scope` block removes was never attempted in the first
place, so it never appears in `unmeasured` either.

## Coherence across a sampled table

One table's profile issues many statements. If the fraction were re-evaluated per statement, each would read different rows and the file would contradict itself on a table nobody wrote to. dbprint avoids that in two layers: the fraction is seeded from the table's name, and — where the connection permits the write — the drawn rows are copied once into a session-lifetime temporary table that every subsequent statement reads.

That copy is the only circumstance in which dbprint writes to your database, and what it needs differs per engine. The adapter page for your engine — [PostgreSQL](../adapters/postgres.md), [MySQL](../adapters/mysql.md) or [Snowflake](../adapters/snowflake.md) — states the privilege, where the object lives, and what changes when the write is refused.

## Cheaper without narrowing

Sampling is not the only lever. Two `statistics` keys cut cost on wide tables without reducing what was scanned:

| Key | Default | Effect |
|---|---|---|
| `enumeration_threshold` | `50` | Cardinality at or below this makes a column `categorical`; above it, the column classifies by type and may carry a range instead of a value list |
| `top_n_values` | `20` | Cap on the `values` list; `values_coverage` states how much of the column the listed entries account for |

Lowering `enumeration_threshold` is the cheapest single change on a wide table.

Every key named on this page, with its type, default and cascade rules, is in [Configuration](../CONFIG.md).
