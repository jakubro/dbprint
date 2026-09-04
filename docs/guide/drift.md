# Tracking drift

A print is a measurement with a timestamp. The database keeps moving; the file does not. Drift is the gap between them, and dbprint gives you three separate ways to see it — which one you want depends on whether you are asking *what changed*, *how old is this*, or *is it still true*.

## What changed: `diff.yaml`

Every `dbprint generate` writes `prints/<conn>/diff.yaml` alongside the artifacts, describing what moved since the previous run. It is a machine artifact — a consumer reads it, a reviewer skims it in the pull request that regenerated the print.

Its `changes` array carries one entry per event, each with a `kind`. Nineteen kinds exist, and the module puts them on two sides of one line: `statistic_changed` and `table_row_count_changed` are the only two the engine treats as **data** movement — a value that changed because the rows underneath it did. Every other kind means the committed print no longer describes the database it names at all:

| Grain | Kinds |
|---|---|
| Table | `table_added`, `table_removed`, `grain_changed`, `physical_layout_changed`, `depends_on_changed`, `comment_changed` |
| Column | `column_added`, `column_removed`, `column_type_changed`, `column_nullable_changed`, `column_default_changed`, `comment_changed` |
| Relationship | `relationship_added`, `relationship_removed`, `relationship_modified` |
| Index | `index_added`, `index_removed`, `index_modified` |
| Data | `statistic_changed`, `table_row_count_changed` |

`grain_changed`, `physical_layout_changed` and `depends_on_changed` sit with the schema-moving kinds even though they can look like data at a glance: each states what a constraint or a view's own substrate declares, which rows churning cannot move on their own. `depends_on_changed` fires only when both sides carry a `depends_on` list at all — a side that never asked (a plain table, or a `catalog_only` read) reports nothing, never a removal; the format distinguishes `[]` (the catalog answered, reads nothing) from an omitted key (the producer could not ask), and the diff preserves that distinction rather than collapsing it.

[SPEC 2.6.6](../format/v1/SPEC.md#266-per-kind-field-schemas) gives every kind its own field schema.

For a one-off comparison against the live database, `dbprint diff` does the same computation and writes nothing to disk. It is not read-only against the database: it re-profiles every table in scope, so any table narrowed to a fraction still gets the temporary-table copy that `materialize_sample` describes. The same is true of `check --online`. A comparison that touches nothing needs either no sampling in scope or `materialize_sample: false`.

### The thresholds are presentation, not filtering

```yaml
    diff:
      stat_change_threshold:
        cardinality_ratio: 0.02
        percentile_pct: 0.05
        values_coverage: 0.05
        default: 0.01
```

These govern what `dbprint diff`'s **human** output bothers to show. Machine output — `--format json` or `--format yaml` — is always unfiltered, and so is the committed `diff.yaml`. Raising a threshold quiets a report; it never removes an event from the artifact.

### What a scoped side suppresses from comparison

A `scope` block on either side is table-grain, never per-column, and it suppresses the fields that scale one-for-one with population size: `cardinality`, `null_count`, `values`, `sum`, `zero_count`, `negative_count`, `empty_count`, `quantized_count` and `normalized_cardinality`. A table whose sample size simply moved between two runs would otherwise report that population change as data drift. Ratios stay compared — they are normalized to their own scan — and so do `mean` and every `length.*` field, neither of which scales with population the way a count does.

A scoped table that emits no change at all is counted `unevaluated`, not `unchanged` — as is a `catalog_only` table on either side, or a table this run did not re-read. "Not compared" and "compared and equal" are separate numbers in the artifact; `table_row_count_changed` is exempt from all of this, since `row_count` is a count over the whole table rather than the scanned set, and stays meaningful under a sample-scale scope.

Four more sets narrow the comparison for reasons that have nothing to do with scope: `freshness`, `sql_type`, `nullable`, `rows_scanned` and every `inferred.sampled`/`inferred.matched`/`inferred.looks_like_candidate*`/`sketch` field are never compared at all — they move on every run regardless of the data, or (for `sketch`) a `diff` run never re-executes the pass that produces one. `cardinality`/`cardinality_ratio` drop out of the comparison only when either side counted approximately, since two approximations of one unchanged column differ by construction. `normalized_cardinality` drops out only when the path is present on one side and absent on the other — a side that never computed it, not a value that appeared or vanished. And `unmeasured` itself is read off both sides to decide what *not* to compare, never compared as a statistic in its own right — a field either side names there has no reading behind it to diff.

### `detection: measured` edges never appear in a diff

Filtered off both sides before any matching happens: a `diff` run never executes the sketch pass a measured edge depends on, so if it were compared, every measured edge would report as removed on every single run. Read this as a property of `dbprint diff` and `check --online` specifically, not of the format — a `generate` run's own manifest still carries the edge.

## How old: freshness

Every table carries a freshness threshold in days. `max_age_days` sets it, and a `rules` entry can override it per table — which is how a dimension refreshes daily while a matview that takes an hour to profile refreshes monthly:

```yaml
# .dbprint.yaml
connections:
  primary:
    max_age_days: 1
    rules:
      - include: ["seedbank.germination_by_taxon_mv"]
        max_age_days: 30
```

The threshold does two jobs. `dbprint generate` skips a table whose print is still inside it, so a re-run is cheap rather than a full re-profile; `--force` re-profiles every matched table regardless, which is what to reach for when a run reports skips you did not want. `dbprint check` fails on a table that has fallen outside it.

Two values behave specially. `0` means the print is stale the moment it is written — `generate` re-extracts it every run and `check`'s freshness gate cannot pass, whatever order the commands run in. A negative value asks for the same thing and is refused when the config loads, because it holds every table stale with no way for the artifact to say so.

`check` judges each print against the threshold **its own manifest entry records** — the one the run that wrote it skipped it against — rather than against whatever the config says today. An explicit `--max-age` on the command line overrides every table's recorded threshold for that run.

## Is it still true: `check --online`

`dbprint check` is offline by default: it reads the committed print and verifies it is well-formed and fresh. `--online` adds a connection and re-extracts, so the question becomes whether the committed print still describes the database.

That is the check that catches a column added without a regenerate. It is also the one that needs credentials and costs a profiling run, which is why it is opt-in.

## What a drift exit code should mean in your pipeline

`check` distinguishes staleness from drift, and the distinction is worth respecting:

| Code | Means | A reasonable response |
|---|---|---|
| `2` | staleness — the print is older than its own threshold | regenerate; often a scheduled job rather than a failed build |
| `3` | drift — the committed print no longer matches the database | a human looks: either the schema changed and the print needs regenerating, or the change was not intended |

Treating both as a hard build failure makes the freshness threshold a deadline for every branch, which is rarely what anyone wants. Treating `3` as a warning defeats the point of running `--online` at all. The usual split is `2` on a schedule and `3` in the pull request.

[Gating CI](ci.md) has the full exit-code table and a workflow that acts on them.

## Keeping the diff readable

A print regenerated on every run produces a diff on every run, and a diff nobody reads is a diff nobody notices. Three things keep the signal:

- **Seeded sampling.** A sampled table draws one row set per run, and on PostgreSQL and duckdb the same one next run, so its statistics do not move on a table nobody wrote to. On the other six engines that stability is likely rather than guaranteed — see your engine's adapter page before gating on it.
- **A stable redaction salt.** Rotating it changes every hashed value at once.
- **A `max_rows_scanned` ceiling snaps to a grid** rather than tracking the catalog estimate exactly, so an estimate that drifted by a few percent resolves to the same fraction and reports nothing.
