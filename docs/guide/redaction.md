# Withholding cell values

A print carries real cell values: the value list of a categorical column, the bounds of a range, the percentiles of a numeric. That is what makes it useful to a reader, and it is also why an email column or a street address needs handling before the file is committed.

Redaction withholds the **literals** and keeps the **measurements**. A redacted column still publishes its null count, its cardinality, its ratios, its value counts, its coverage and its distribution — all identical to an unredacted run. Only the values themselves are replaced or removed.

## Three ways to target a column

An entry covers a column matching **any** of its three criteria. Entries apply in declaration order and the last matching one decides the primitive.

```yaml
# .dbprint.yaml
connections:
  primary:
    redact:
      - columns: ["seedbank.collector.email", "seedbank.*.*_address"]
        with: mask
      - sensitivity: [personal_name, national_id]
        with: drop
      - looks_like: [email]
        with: hash
```

| Criterion | Matches on | Use it for |
|---|---|---|
| `columns` | globs over `<fqn>.<column>` | the column detection misses, and the one your own naming convention marks |
| `sensitivity` | the column's own detected `inferred.sensitivity` | a category, so a column added next quarter is covered by a rule written today |
| `looks_like` | the column's own detected `inferred.looks_like` | a shape, whatever the column is called |

The second and third are the ones that age well. A glob covers the columns you know about; a category covers the ones the schema has not grown yet. `sensitivity` and `looks_like` are closed vocabularies checked when the config loads, so a typo is refused by name rather than quietly covering nothing — see [SPEC 4.4.1](../format/v1/SPEC.md#441-categories) for the categories and [SPEC 4.1](../format/v1/SPEC.md#41-looks_like-patterns) for the shapes. `columns` is an open glob and is not checked, because a pattern matching no table today may match one tomorrow.

## Three primitives

| `with` | Effect on the literal |
|---|---|
| `mask` | replaced with a placeholder |
| `drop` | omitted entirely, along with `range` and `percentiles` |
| `hash` | replaced with a salted digest, so equal values stay equal across the print |

The column declares which one it received, with a `redacted` marker naming the primitive, so a reader can tell a measurement from a substitution ([SPEC 2.2.9](../format/v1/SPEC.md#229-redacted--values-withheld)). A column a rule covers but that publishes no cell value at all carries no marker — nothing was withheld from it.

## Where the salt lives, and why not in the config

`hash` is refused without a salt. An unsalted digest of an email address is reversible by dictionary attack in minutes, so the digest is only worth anything with a secret behind it — and `.dbprint.yaml` is committed.

The salt therefore lives with the credentials, never in the project config:

```yaml
# ~/.dbprint/connections.yaml
primary:
  host: db.internal
  redaction_salt: ...
```

or as `DBPRINT_PRIMARY_REDACTION_SALT` in the environment, which is how CI supplies it.

Keep it stable for the life of the project. Rotating it changes every hashed value in every column at once, and `dbprint diff` reports all of them as movement.

## Cascade, and the direction it only goes in

`redact` concatenates rather than overrides: the `defaults` entries are walked first, then the connection's own, and the last matching entry decides the primitive.

So a connection can change what a project-wide rule applies to a column — `mask` to `hash` — but it cannot lift the coverage, because no primitive means "not redacted". A connection that has to stay unredacted is one whose rule does not belong in `defaults` in the first place.

## Three things redaction does not change

**Detection still runs.** `looks_like` and `sensitivity` are measured over sampled values that are never written to the print, so a hashed email column still reports `looks_like: email`. The shape claim describes the column, not the literals that were emitted. A column whose best-scoring pattern falls short of the 95% verdict bar but still clears 50% publishes that near-miss as `inferred.looks_like_candidate` alongside its own share - a weaker signal than `looks_like`, not a value, so redaction leaves it untouched the same way.

**A detected category with no rule covering it is reported, not silenced.** `dbprint check` raises `privacy.unredacted-sensitive` — a warning — for a column that names its own `inferred.sensitivity` and still publishes a cell value nothing withheld. The check reads the committed print, so writing the rule and regenerating is what clears it. [Gating CI](ci.md) covers how warnings surface, which is not the same as how errors do.

**A degenerate-value count discloses no literal, so redaction never suppresses one.** `zero_count`, `negative_count`, `empty_count` and `quantized_count` are exact counts, never the values themselves, and stay in every redacted column at every row count — unlike `mean`, `sum` and `length` below, which are withheld under one specific condition.

## The one condition that withholds an aggregate

`mean`, `sum` and `length` are the exception to "redaction withholds literals and keeps
measurements": all three are withheld under every primitive, `mask`, `drop` and `hash` alike,
but only where the scanned set holds at most one non-null value - `rows_scanned - null_count
<= 1`. Above that threshold none of the three is touched: an aggregate over two or more real
values does not narrow down to any one of them the way a single-row aggregate would.

This is the same rule stated two ways, and both stay in step: the producer applies it while
writing the column, and the conformance validator re-derives it from `rows_scanned` and
`null_count` to check the column was not published in violation of it. Every other
measurement - `null_count`, `cardinality`, the value counts, `distribution` - is unaffected at
any row count, redacted or not.

An all-null column never reaches this condition for `length`: it always classifies
`categorical` (SPEC 3.3), and `categorical`'s own carve-out already omits `length` on an
all-null scanned set independently of redaction.

## What a consumer can no longer ask

A redacted column has no literal to compare against, so predicates over `accepted_values`, `range` or `percentiles` are refused rather than evaluated against placeholders. Temporal columns coarsen their derived day counts as well. If a data-quality assertion depends on a column's actual values, redacting that column removes the assertion's ground truth — see [Assertions](../ASSERTIONS.md).
