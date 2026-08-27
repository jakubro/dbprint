# Tracking drift

A print is a measurement with a timestamp. The database keeps moving; the file does not. Drift is the gap between them, and dbprint gives you three separate ways to see it — which one you want depends on whether you are asking *what changed*, *how old is this*, or *is it still true*.

## What changed: `diff.yaml`

Every `dbprint generate` writes `prints/<conn>/diff.yaml` alongside the artifacts, describing what moved since the previous run. It is a machine artifact — a consumer reads it, a reviewer skims it in the pull request that regenerated the print.

Its `changes` array carries one entry per event, each with a `kind`. The kinds cover schema movement (`table_added`, `column_type_changed`, `column_nullable_changed`, `index_modified`, `comment_changed`), relationship movement (`relationship_added`, `relationship_modified`), and data movement (`statistic_changed`, `table_row_count_changed`, `grain_changed`, `physical_layout_changed`). [SPEC 2.6.6](../format/v1/SPEC.md#266-per-kind-field-schemas) gives every kind its own field schema.

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

### Comparing across a scope change

A number measured over a sample and a number measured over a whole table are not comparable, and a table that crossed a size gate between runs changes which of the two it publishes. The `scope` block is what tells a reader that happened — see [choosing what to profile](scoping.md). Read a `statistic_changed` on such a table as a change of population before reading it as a change of data.

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

- **Seeded sampling.** A sampled table draws one row set per run, and on PostgreSQL the same one next run, so its statistics do not move on a table nobody wrote to. On MySQL and Snowflake that stability is likely rather than guaranteed — see your engine's adapter page before gating on it.
- **A stable redaction salt.** Rotating it changes every hashed value at once.
- **A `max_rows_scanned` ceiling snaps to a grid** rather than tracking the catalog estimate exactly, so an estimate that drifted by a few percent resolves to the same fraction and reports nothing.
