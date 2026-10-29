# Withholding cell values

A print carries real cell values: the value list of a categorical column, the bounds of a range, the percentiles of a numeric. That is what makes it useful to a reader, and it is also why an email column or a street address needs handling before the file is committed.

Redaction withholds the **literals** and keeps the **measurements**. A redacted column still publishes its null count, its cardinality, its ratios, its value counts, its coverage and its distribution — all identical to an unredacted run. Only the values themselves are replaced or removed.

## Three ways to target a column

An entry covers a column matching **any** of its three criteria. Entries apply in declaration order and the last matching one decides the primitive.

```yaml
# .dbprint.yaml
connections:
  primary:
    profile:
      redact:
        - columns: ["arboretum.seedbank.collector.email", "arboretum.seedbank.*.*_address"]
          with: mask
        - sensitivity: [personal_name, national_id]
          with: drop
        - looks_like: [email]
          with: hash
```

| Criterion | Matches on | Use it for |
|---|---|---|
| `columns` | globs over `<table>.<column>` | the column detection misses, and the one your own naming convention marks |
| `sensitivity` | the column's own detected `inferred.sensitivity` | a category, so a column added next quarter is covered by a rule written today |
| `looks_like` | the column's own detected `inferred.looks_like` | a shape, whatever the column is called |

The second and third are the ones that age well. A glob covers the columns you know about; a category covers the ones the schema has not grown yet. `sensitivity` and `looks_like` are closed vocabularies checked when the config loads, so a typo is refused by name rather than quietly covering nothing — see [SPEC 4.4.1](../format/v1/SPEC.md#441-categories) for the categories and [SPEC 4.1](../format/v1/SPEC.md#41-looks_like-patterns) for the shapes. `columns` is an open glob and is not checked, because a pattern matching no table today may match one tomorrow.

`looks_like` is a 95% verdict, so a `looks_like` rule misses a column that is mostly but not wholly one shape; a `sensitivity` rule reaches it once half its sampled values carry the category's shape ([SPEC 4.4.4](../format/v1/SPEC.md#444-thresholds)).

Both follow the detection `statistics.yaml` publishes. An annotation rejecting a column's `sensitivity` or `looks_like` withdraws it from what agents read, not from redaction: a rule naming that category or shape still covers the column, and `privacy.unredacted-sensitive` still reports it while its values publish ([annotating a print](annotations.md)).

## Three primitives

| `with` | Effect on the literal |
|---|---|
| `mask` | replaced with a placeholder |
| `drop` | omitted entirely, along with `range` and `percentiles` |
| `hash` | replaced with a salted digest, so equal values stay equal across the print |

The column declares which one it received, with a `redacted` marker naming the primitive, so a reader can tell a measurement from a substitution ([SPEC 2.2.9](../format/v1/SPEC.md#229-redacted--values-withheld)). A column a rule covers carries the marker even where it publishes no cell value — a prose column, or one whose values were not measured; only `json` and `unsupported` columns, which carry no cell value by their kind, never do.

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

## `defaults` and a connection's own list

`profile.redact` is a list, so a connection that sets its own replaces the one in `defaults` whole — any `defaults` entry it does not repeat no longer covers its columns, because no primitive means "not redacted". To keep a project-wide entry in every connection, anchor it once under the root `x-shared` key and list it with an alias wherever it applies:

```yaml
x-shared:
  mask_contact: &mask_contact {sensitivity: [contact], with: mask}

connections:
  primary:
    profile:
      redact:
        - *mask_contact
        - columns: ["arboretum.seedbank.collector.institution"]
          with: hash
```

## Three things redaction does not change

**Detection still runs.** `looks_like` and `sensitivity` are measured over sampled values that are never written to the print, so a hashed email column still reports `looks_like: email`. The shape claim describes the column, not the literals that were emitted. A column whose best-scoring pattern falls short of the 95% verdict bar but still clears 50% publishes that near-miss as `inferred.looks_like_candidate` alongside its own share — a weaker signal than `looks_like`, not a value, so redaction leaves it untouched the same way.

**A detected category with no rule covering it is reported, not silenced.** `dbprint check` raises `privacy.unredacted-sensitive` — a warning — for a column that names its own `inferred.sensitivity`, or a part inside a column or part that does, and still publishes a cell value nothing withheld. The check reads the committed print, so writing the rule and regenerating is what clears it. [Gating CI](ci.md) covers how warnings surface, which is not the same as how errors do.

**A rule on a document or map column withholds its key names.** The column itself carries no marker; the rule marks its parts, and while the key-set part `[keys]` is redacted no part whose path spells a key is published, so the key names stay in the database with the values — no statement spelling one is sent, so none reaches the run log or the warehouse's query history. A rule naming a category reaches every part inside a part detected with it: under `sensitivity: [postal_address]`, a `mailing_address` object inside a document withholds its members and a `home_address` record inside a struct masks each of its fields.

**A rule reaches every table it covers on the next `generate`.** A table whose print no longer matches the rules is re-read however recently it was profiled. A run that does not re-read such a table — one outside its `--include`/`--exclude`, one that failed, one `--fail-fast` never reached — fails that table instead, naming the columns, and leaves its files as they are; `dbprint check` reports each such column as `privacy.redaction-not-applied`, an error, until a run re-reads it.

## What a redacted column still publishes

A covered column keeps its count profile and nothing else computed from its values. The count profile is what a renaming of the values could not change: `null_count`, `null_rate`, `cardinality`, the value counts, `values_coverage`, `distribution` and `frequencies`, each the true measurement. `mean`, `sum`, `length`, `zero_count`, `negative_count`, `non_finite_count`, `empty_count`, `quantized_count`, `normalized_cardinality` and `unrepresentable` are withheld under every primitive, `mask`, `drop` and `hash` alike, at every row count.

No threshold would make them safe, because the value counts are published beside them. Take a column holding `0` in 50 rows and one other number in 10: the value list shows two masked entries counted 50 and 10, `zero_count: 50` names the first as `0`, and `sum / (60 - 50)` is the second. A count-like column whose values grow rarer as they grow larger does the same at any size: its `sum` is the least its counts allow, which only one assignment of values reaches. `empty_count` beside a masked entry of the same count names that literal as `''`, and a `length` whose bounds differ says which entry is the short one.

The producer applies the rule while writing the column, and the conformance validator rejects a marked column carrying any withheld field (`stats.forbidden-field-for-classification`) or publishing a literal its primitive withholds — a raw value under `mask` or `hash`, a `value` key under `drop`, a `spelling_of` under any marker (`stats.redacted-literal-published`).

## What a consumer can no longer ask

A covered column carries no `sketch`, because a set of digests of the withheld values is that column enumerated in another form — and a `detection: measured` join edge is built from a sketch, so edges resting on a covered column disappear with it.

A redacted column has no literal to compare against, so predicates over `accepted_values`, `range` or `percentiles` are refused rather than evaluated against placeholders. Temporal columns coarsen their derived day counts as well. If a data-quality assertion depends on a column's actual values, redacting that column removes the assertion's ground truth — see [Assertions](../ASSERTIONS.md).

## The DDL is masked, not redacted

A rule covers cell values only; `ddl.sql` is never redacted, since a `DEFAULT` or `CHECK` constant is schema, not a row's value. What every adapter does instead is mask a password or key written into a view body, a default, a constraint or a comment — a URL's `user:password@`, a `password=` or `api_key: ` value, a key in a recognised secret format — with `[HIDDEN]`, keeping the key and the text around it ([SPEC 2.1.3](../format/v1/SPEC.md#213-what-must-be-stripped-per-adapter)). The same masks reach a column's `sql_type` and a clustering or merging key's `expression` in `statistics.yaml`.
