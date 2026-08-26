# dbprint assertion DSL — v1

User-facing specification for dbprint's assertion DSL. Assertions are declared in `.dbprint.yaml` and evaluated by `dbprint check`; they let producers and CI pipelines declare what MUST be true about a database's schema and data, with vocabulary that mirrors `statistics.yaml`.

Any tool that consumes this DSL MUST comply with the requirements below. Conformance MUST be mechanically verifiable.

---

## 0. Scope and terminology

### 0.1 What this spec covers

- The shape of the `assertions:` block in `.dbprint.yaml`.
- The statistic assertion (stat predicate) vocabulary and evaluation rules.
- The SQL assertion (SQL query) shape and result semantics.
- The output `Issue` shape and code catalog emitted by assertion evaluation.
- Evaluation behavior in offline vs online `check` modes.

### 0.2 What this spec does NOT cover

- The implementation of the `dbprint check` command.
- The format of `statistics.yaml` (covered by [`format/v1/SPEC.md`](format/v1/SPEC.md) §2.2).
- Assertion authoring helpers and LLM-assisted assertion drafting.
- Cross-table declarative predicates; SQL assertions cover cross-table cases.

### 0.3 Terminology

- **Assertion**: a declaration that something MUST be true about a database or its prints. Each entry under `tables.<fqn>` or `queries` is a single assertion.
- **Predicate**: an assertion clause that compares a single statistic against an expected value, range, set, or pattern.
- **Statistic assertion**: declarative predicates over the [SPEC §2.2 `statistics.yaml`](format/v1/SPEC.md#22-statisticsyaml) vocabulary.
- **SQL assertion**: an arbitrary SQL query with explicit `expect` semantics, executed against the live database.
- **Evaluation**: producing an `Issue` list by running every configured assertion against current evidence.
- **Severity**: per-assertion classification of failures — `error` (default) or `warning`.
- **Issue**: a single assertion outcome emitted with a stable code, severity, path, detail string, and spec reference.

### 0.4 Requirement levels

This document uses **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** per RFC 2119.

---

## 1. Configuration

### 1.1 Location

Assertions are declared in `.dbprint.yaml` under each connection:

```yaml
connections:
  <connection_name>:
    adapter: postgres | snowflake | mysql
    # ... existing connection fields ...

    assertions:
      tables:
        <fqn>: { ... }
      queries:
        - { ... }
```

The `assertions:` block is OPTIONAL. Absence is equivalent to an empty block: no assertions are evaluated.

### 1.2 Block shape

```yaml
assertions:
  tables:                                  # OPTIONAL - statistic assertion predicates per table FQN
    <fqn>:
      row_count: <predicate>               # OPTIONAL - per-table predicate
      columns:                             # OPTIONAL - per-column predicates
        <column_name>:
          <stat>: <predicate>
          ...
  queries:                                 # OPTIONAL - SQL assertions
    - name: <identifier>
      severity: error | warning            # OPTIONAL - default error
      sql: |
        <SQL query>
      expect: 0 | empty
```

Both `tables` and `queries` are OPTIONAL. An `assertions:` block with neither key is equivalent to no block at all.

A shape violating this section - `assertions:` itself not a mapping, `tables:` not a mapping, `queries:` not a list, a table or column body not a mapping, a query entry not a mapping, or a query missing `name`/`sql`/a valid `expect` - is a block-shape fault. Evaluators MUST emit `assertion.malformed-block` (error) for it. Only a fault in `assertions:` itself, the one shape nothing else can be extracted from, MAY abort evaluation of the whole connection; every other block-shape fault MUST NOT prevent evaluation of the assertions unaffected by it - the malformed table, column or query is skipped, and every sibling assertion is still evaluated, per §5.4's run-everything-then-report principle.

### 1.3 FQN matching

Each key under `tables:` is a fully-qualified table name matching the FQN convention from the adapter's namespace path (per [SPEC §1.3](format/v1/SPEC.md#13-namespace-path)). Evaluators MUST match assertion FQNs against produced table FQNs exactly: lowercase, dotted (e.g., `garden.seedbank.accession`).

### 1.4 Unknown FQNs and columns

If an assertion references an FQN not present in the connection's manifest, evaluators MUST emit `assertion.unknown-table` as a warning and skip every predicate under that FQN.

If a predicate references a column not present in the table's `statistics.yaml`, evaluators MUST emit `assertion.unknown-column` as a warning and skip that column's predicates.

Warnings do NOT drive non-zero exit codes; the assertion is treated as inapplicable, not failed.

### 1.5 Equivalent empty forms

The following are equivalent: no assertions to evaluate.

```yaml
# (no assertions: key)

assertions: {}

assertions:
  tables: {}
  queries: []
```

---

## 2. Statistic assertions - stat predicates

Statistic assertion predicates are declarative comparisons against the statistics vocabulary specified in [SPEC §2.2](format/v1/SPEC.md#22-statisticsyaml). They are evaluated against committed `statistics.yaml` in offline mode, and against live re-extracted statistics in online mode.

### 2.1 Predicate forms

Every predicate is `<stat>: <expected>` where `<expected>` follows one of the forms below.

| Form | YAML shape | Meaning |
|---|---|---|
| Scalar | `null_rate: 0.0` | Exact equality |
| Range | `null_rate: {max: 0.01}` | Bounds - `min`, `max`, or both |
| Enum | `classification: categorical` | Field MUST equal the value |
| Set | `accepted_values: [a, b, c]` | The column's `values` list MUST be a subset of the given set. Applies only when that list is exhaustive (`values_coverage` of `1.0`) |
| Pattern | `looks_like: email` | The `inferred.looks_like` field MUST equal the value |

Range form details:

- `{min: X}` - actual value MUST be >= X
- `{max: Y}` - actual value MUST be <= Y
- `{min: X, max: Y}` - actual value MUST be in `[X, Y]`

Evaluators MUST emit `assertion.malformed-predicate` (error) when a predicate uses an unknown form or an incompatible value type (e.g., string scalar against a numeric stat).

### 2.2 Per-table predicates

```yaml
tables:
  <fqn>:
    row_count: <predicate>
```

The only per-table predicate is `row_count`, applied to the top-level `row_count` field from `statistics.yaml`.

### 2.3 Per-column predicates

```yaml
tables:
  <fqn>:
    columns:
      <column_name>:
        <stat>: <predicate>
        ...
```

Multiple predicates on the same column compose with AND semantics: every predicate MUST pass for the column to pass.

### 2.4 Assertable stats

The following stats from SPEC §2.2 are assertable:

| Stat | Predicate forms | Type |
|---|---|---|
| `sql_type` | scalar, enum | native-type string |
| `nullable` | scalar | boolean |
| `null_count` | scalar, range | integer >= 0 |
| `null_rate` | scalar, range | float in [0, 1] |
| `cardinality` | scalar, range | integer >= 0 |
| `cardinality_ratio` | scalar, range | float in [0, 1] |
| `classification` | enum | one of the SPEC §3.1 values |
| `distribution` | enum | one of `uniform`, `imbalanced`, `dominant_value`, `long_tail` |
| `accepted_values` | set | the column's `values` MUST be a subset of the asserted set; requires an exhaustive list |
| `looks_like` | pattern | matches `inferred.looks_like` |
| `candidate_key` | scalar | boolean; matches `inferred.candidate_key` |
| `range.min` | scalar, range | type-matched (numeric or ISO 8601) |
| `range.max` | scalar, range | type-matched (numeric or ISO 8601) |
| `percentiles.<key>` | scalar, range | dotted access; e.g., `percentiles.p99` |
| `freshness.classification` | enum | one of `live`, `stale`, `dormant` |
| `freshness.max_age_days` | scalar, range | integer >= 0 |

Stats marked R or O in the SPEC §2.2.3 field matrix for the column's classification MAY be asserted. Stats marked "must not emit" for the column's classification (e.g., asserting `percentiles.p99` on a `boolean` column) MUST emit `assertion.inapplicable-stat` as a warning and skip the predicate.

### 2.5 Evaluation source

| Mode | Source for statistic assertion evaluation |
|---|---|
| Offline (`dbprint check`) | The committed `prints/<conn>/<namespace>/<table>/statistics.yaml` |
| Online (`dbprint check --online`) | Live re-extraction via the adapter; the just-computed `ColumnStats` payloads |

The predicate form is identical in both modes. Only the source of evidence differs.

### 2.6 Edge cases

| Case | Resolution |
|---|---|
| Predicate references a stat the column doesn't expose for its classification | `assertion.inapplicable-stat` warning; skip the predicate |
| Predicate references an unknown stat name | `assertion.unknown-stat` error |
| Empty table (`row_count == 0`) asserted with `cardinality_ratio: {min: 0.999}` | Evaluator computes `cardinality_ratio == 0`; emits `assertion.cardinality-ratio-mismatch` per the predicate |
| A nonzero `null_count`/`cardinality` asserted with `null_rate: 0` / `cardinality_ratio: 0` | FAIL - SPEC 2.2.6's floor means a nonzero numerator never publishes exactly `0.0`; use `{max: 0.000001}` for an effectively-zero tolerance |
| A nonzero non-null count asserted with `null_rate: 1` | FAIL - `null_rate: 1.0` is a defined sentinel (SPEC 2.2.7) and a nonzero non-null count never publishes it; use `{min: 0.999999}` for an effectively-all-null tolerance. `cardinality_ratio` carries no such ceiling |
| `accepted_values` asserts a superset of what the table contains | PASS - the set predicate requires the table's values to be a subset, not equal |
| `accepted_values` on a column whose `values` list is truncated | `assertion.inapplicable-stat` warning - a capped list is the frequent slice of a domain, not the domain, so a subset check over it would both miss real violations and invent others |
| `accepted_values` and the table has values outside the asserted set | FAIL - `assertion.accepted-values-violated`; the offending values listed in the Issue detail |
| Numeric range bound is a string, or a string range bound is numeric | `assertion.malformed-predicate` error |
| All-null column asserted with `looks_like: <pattern>` | `assertion.inapplicable-stat` warning - `looks_like` requires sampled non-null values |
| Predicate over cell values on a redacted column | `assertion.redacted-stat` warning; skip the predicate. The subjects are `accepted_values`, `range` with its bounds, `percentiles` with its keys, and a temporal column's `freshness.max_age_days` - a redacted column emits a placeholder or a digest for the first three under `mask` and `hash` and omits them under `drop` (SPEC §2.2.9), and floors `max_age_days` to the nearest 90 days under every primitive including `drop`, so evaluating any of them would compare the assertion against a stand-in rather than the real measurement. Every other measurement on that column stays assertable, including `freshness.classification`, which is derived from the true, uncoarsened age |

---

## 3. SQL assertions

SQL assertions are SQL strings executed against the live database. They cover assertions that cannot be expressed declaratively as statistic assertions - cross-table predicates, filtered counts, complex business rules.

SQL assertions run ONLY in online mode (`dbprint check --online`).

### 3.1 Query block shape

```yaml
queries:
  - name: <identifier>                     # REQUIRED - stable string for Issue paths
    severity: error | warning              # OPTIONAL - default error
    sql: |
      <SQL query>
    expect: 0 | empty                      # REQUIRED
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | YES | Identifier-style (letters, digits, underscores); MUST be unique within the connection's `queries` list |
| `severity` | enum | NO | `error` (default) or `warning` |
| `sql` | string | YES | One or more SQL statements; the LAST statement's result is the assertion subject |
| `expect` | enum | YES | `0` or `empty` (see §3.2, §3.3) |

### 3.2 `expect: 0`

The query MUST return at least one row; the assertion subject is the value in row 0, column 0. The assertion PASSES when that value compares equal to the integer 0; otherwise FAILS with `assertion.sql-non-zero`.

Type coercion rules:

- An integer or float `0` (or `0.0`) PASSES.
- A `NULL` FAILS (treated as non-zero).
- A non-numeric value (string, boolean, etc.) emits `assertion.sql-type-mismatch`, at the query's own severity.

If the query returns zero rows, evaluators MUST emit `assertion.sql-empty-result`, at the query's own severity - `expect: 0` requires a scalar result.

### 3.3 `expect: empty`

The query PASSES when it returns zero rows. The assertion FAILS with `assertion.sql-non-empty` when one or more rows are returned. The row contents are emitted in the Issue detail (truncated to a producer-defined limit if the row count is large).

### 3.4 Adapter dialect notes

SQL assertions are written in the adapter's native SQL dialect. Producers MUST NOT rewrite or normalize the SQL before execution. Common dialect awareness:

| Adapter | Notes |
|---|---|
| PostgreSQL | Standard SQL; identifiers case-folded to lowercase unless quoted |
| Snowflake | Standard SQL; identifiers UPPERCASE unless quoted; warehouse selection inherited from connection config |
| MySQL | Standard SQL; identifiers lowercase on Linux, case-insensitive on Windows/macOS depending on `lower_case_table_names` |

Queries MUST be read-only. Evaluators MUST run them in a read-only session where the adapter supports one (e.g., Snowflake `USE ROLE` with read-only role; PostgreSQL `SET TRANSACTION READ ONLY`). Write operations (INSERT, UPDATE, DELETE, DDL) are out of scope for assertions.

### 3.5 Edge cases

| Case | Resolution |
|---|---|
| Query raises a DB error at execution time | `assertion.sql-execution-error`, at the query's own severity (default `error`); Issue detail carries the DB error message |
| `expect: 0` query returns a column type the producer cannot coerce to integer | `assertion.sql-type-mismatch`, at the query's own severity (default `error`) |
| `expect: 0` query returns NULL in row 0, column 0 | FAIL with `assertion.sql-non-zero`; Issue detail records `actual: null` |
| `expect: empty` query returns rows | FAIL with `assertion.sql-non-empty`; up to producer-defined N rows listed in Issue detail |
| Multi-statement SQL where intermediate statements have side-effects | Disallowed - read-only session SHOULD reject; producer behavior in non-read-only sessions is implementation-defined and out of scope |
| `name` collides with another query in the same connection | `assertion.duplicate-query-name` error at configuration parse time; the first query with that name is kept and runs, every later duplicate is skipped and faulted - every other query and every table predicate in the connection still evaluates, per §5.4 |

---

## 4. Severity model

### 4.1 Default

Every assertion defaults to severity `error`. Failed `error` assertions drive a non-zero exit code from `dbprint check --online` (see §6).

### 4.2 Per-assertion override

SQL assertions accept an explicit `severity:` field. `severity:` is a property of the assertion as a whole, not of one outcome kind: setting `severity: warning` downgrades every Issue that assertion can emit to warning, driving no non-zero exit code - the PASS/FAIL verdict (`assertion.sql-non-zero`, `assertion.sql-non-empty`) and every diagnostic the same query can raise instead of a verdict (`assertion.sql-execution-error`, `assertion.sql-empty-result`, `assertion.sql-type-mismatch`) alike. A query that cannot execute or cannot be coerced into a verdict has not proven the condition it was written to check, which is exactly what `severity: warning` already says the author is willing to tolerate.

Statistic assertion predicates do NOT carry per-predicate severity. Every statistic assertion failure is `error` severity. (There is no per-predicate severity; the `accepted_values` warning case in §2.4 is a structural skip, not a downgrade.)

### 4.3 Effect on exit code

See §6 for the full exit-code mapping. Summary:

- Any `error`-severity assertion failure -> exit 6
- Only `warning`-severity assertion failures (a SQL assertion with `severity: warning`) -> exit unchanged from the structural pass (typically 0)

---

## 5. Output

### 5.1 Issue shape

Assertion evaluators emit `Issue` records using the same dataclass shape as the conformance suite:

```python
@dataclass(frozen=True, order=True)
class Issue:
    path: str
    code: str
    severity: Literal["error", "warning"]
    detail: str
    spec_ref: str
```

| Field | Content for assertions |
|---|---|
| `path` | Dotted path identifying the assertion - e.g., `assertions.<conn>.tables.<fqn>.columns.<col>.<stat>` (statistic assertion) or `assertions.<conn>.queries.<name>` (SQL assertion) |
| `code` | One of the `assertion.*` or `drift.*` values in §5.2 |
| `severity` | `error` or `warning` per §4 |
| `detail` | Human-readable explanation; MUST include expected and actual values where applicable |
| `spec_ref` | The section that defines the rule, named with the document it lives in: `ASSERTIONS.md §<N>` for everything this document specifies, and `SPEC §<N>` of [`format/v1/SPEC.md`](format/v1/SPEC.md) where it does not. `assertion.redacted-stat` carries `SPEC §2.2.9`, the redaction contract, which lives in the format spec. A bare section number would leave a consumer holding the Issue unable to tell which document to open |

### 5.2 Code catalog

| Code | Severity | Trigger |
|---|---|---|
| `assertion.unknown-table` | warning | FQN not in the manifest |
| `assertion.unknown-column` | warning | Column not in the table's statistics |
| `assertion.unknown-stat` | error | Stat name not in the §2.4 vocabulary |
| `assertion.inapplicable-stat` | warning | Stat is "MUST NOT emit" for the column's classification |
| `assertion.redacted-stat` | warning | Predicate over cell values on a column whose values were redacted |
| `assertion.malformed-predicate` | error | Predicate form invalid or incompatible value type |
| `assertion.malformed-block` | error | The `assertions:` block, or one table/column/query entry within it, does not match §1.2's shape |
| `assertion.row-count-mismatch` | error | `row_count` predicate failed |
| `assertion.null-rate-mismatch` | error | `null_rate` predicate failed |
| `assertion.null-count-mismatch` | error | `null_count` predicate failed |
| `assertion.cardinality-mismatch` | error | `cardinality` predicate failed |
| `assertion.cardinality-ratio-mismatch` | error | `cardinality_ratio` predicate failed |
| `assertion.classification-mismatch` | error | `classification` predicate failed |
| `assertion.distribution-mismatch` | error | `distribution` predicate failed |
| `assertion.accepted-values-violated` | error | `accepted_values` set predicate failed |
| `assertion.looks-like-mismatch` | error | `looks_like` predicate failed |
| `assertion.candidate-key-mismatch` | error | `candidate_key` predicate failed |
| `assertion.sql-type-mismatch` | error | `sql_type` predicate failed |
| `assertion.nullable-mismatch` | error | `nullable` predicate failed |
| `assertion.range-out-of-bounds` | error | `range.min` / `range.max` predicate failed |
| `assertion.percentile-mismatch` | error | `percentiles.<key>` predicate failed |
| `assertion.freshness-mismatch` | error | `freshness.classification` predicate failed |
| `assertion.freshness-age-mismatch` | error | `freshness.max_age_days` predicate failed |
| `assertion.duplicate-query-name` | error | Two queries share the same `name` |
| `assertion.sql-non-zero` | error | `expect: 0` query returned non-zero |
| `assertion.sql-non-empty` | error | `expect: empty` query returned rows |
| `assertion.sql-empty-result` | error | `expect: 0` query returned zero rows |
| `assertion.sql-execution-error` | error | DB raised an error executing the query |
| `drift.schema-changed` | error | `dbprint check --online` re-extraction found a change of shape - any diff event kind except `statistic_changed` and `table_row_count_changed` |
| `drift.statistic-changed` | error | `dbprint check --online` re-extraction found a `statistic_changed` or `table_row_count_changed` event - the committed print's data moved |

Severity column shows the DEFAULT. SQL assertions may downgrade per §4.2. The two `drift.*` codes are emitted by `check --online`'s drift phase (§6.2), not by an assertion evaluator, and are not `assertion.*` values - `spec_ref` points here regardless.

### 5.3 Ordering

Issues are ordered by `(path, code)` lexicographic, matching the conformance ordering rule from SPEC §6.6. Deterministic across runs.

### 5.4 Run-all-then-report

Evaluators MUST evaluate every assertion before returning. A failure on one assertion MUST NOT short-circuit evaluation of subsequent assertions. Matches the conformance suite run-all-then-report principle from SPEC §6.5.

---

## 6. Evaluation modes

### 6.1 Offline mode

`dbprint check` (no flag) evaluates:

1. Structural checks - manifest presence, artifact presence, orphans, conformance, freshness.
2. Statistic assertion predicates against the committed `statistics.yaml`.

SQL assertions are NOT executed in offline mode (no live DB connection) - but the `queries:` block is still parsed and validated per §1.2, since that is a property of the configuration, not of the database. A SQL assertion whose shape is malformed (a missing `name`/`sql`, an invalid `expect`, a duplicate `name`) emits `assertion.malformed-block` or `assertion.duplicate-query-name` offline, the same as it would online; only the query's *execution* - and therefore `assertion.sql-non-zero`, `assertion.sql-non-empty`, and the diagnostic codes in §3.5 - is offline-skipped.

### 6.2 Online mode

`dbprint check --online` evaluates the offline set, then:

1. Drift detection against the live database - both a change of shape and a moved statistic; see §5.2's two `drift.*` codes.
2. Statistic assertion predicates against live re-extracted statistics.
3. SQL assertions against the live database.

A structural failure found offline - a conformance error or a stale print - means there is nothing worth comparing, and suppresses this phase; an *assertion* failure found offline does not, since the print itself is still well-formed and fresh. The two are independent questions, and §6.3's MAX rule is what lets both exit codes surface at once when both are true.

### 6.3 Exit code mapping

| Code | Trigger | Mode |
|---|---|---|
| 0 | All structural checks pass; all evaluated assertions pass (warnings allowed) | offline + online |
| 1 | Structural failure (manifest malformed, artifact missing, orphan, conformance error) | offline + online |
| 2 | Staleness - print older than max-age threshold | offline + online |
| 3 | Drift detected - `drift.schema-changed`, `drift.statistic-changed`, or both | online only |
| 4 | Connection error (DB unreachable, auth failed) | online only |
| 5 | Partial extraction - the connection was reached but some tables could not be re-extracted; the ones that did are still compared and reported normally | online only |
| 6 | At least one `error`-severity assertion failed - a statistic assertion predicate, or a block-shape/duplicate-name fault in the `queries:` config itself | offline + online |

When multiple failure conditions co-occur, top-level exit is the MAX of the per-condition codes. SQL assertion execution errors emit `assertion.sql-execution-error` Issues, not exit code 4 - the DB connection succeeded; the QUERY failed.

---

## 7. Forward compatibility

Consumers MUST tolerate:

- **Unknown predicate forms**: a YAML structure under `<stat>:` not matching any of the forms in §2.1. Evaluators tolerant of unknown forms SHOULD skip with `assertion.malformed-predicate`; strict evaluators MAY accept a form whose semantics this document does not define.
- **Unknown SQL `expect` values**: values beyond `0` and `empty`. Evaluators MUST emit `assertion.malformed-block` and skip; additional values (`equals N`, `greater_than N`) are not defined.
- **Unknown `severity` values**: treat as `warning` by default.
- **Unknown Issue codes** emitted by downstream tools: pass through unchanged.

The statistic assertion vocabulary and the SQL assertion `expect:` value set MAY grow in MINOR releases (additive only). Existing codes' semantics MUST NOT change within a MAJOR version.

---

## Cross-references

- [`format/v1/SPEC.md`](format/v1/SPEC.md) - format specification for `statistics.yaml`, `relationships.yaml`, `manifest.yaml`, `diff.yaml`
- [`ARCHITECTURE.md`](ARCHITECTURE.md) - internal architecture reference
