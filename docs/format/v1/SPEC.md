# dbprint format specification — v1

dbprint is a portable on-disk format for "what this database actually looks like" — DDL, statistics, relationships, and human descriptions, designed for offline consumption by humans and AI agents alike.

This document specifies the v1 format. Any tool that produces or consumes the format MUST comply with the requirements below. Conformance MUST be mechanically verifiable. The reference JSON Schemas SHALL be located at `spec/v1/`. The reference pytest-based conformance suite SHALL be located at `tests/conformance/`.

---

## 0. Scope and terminology

### 0.1 What this spec covers

- On-disk directory layout for a "print" of one or more databases.
- The schema and field semantics of each artifact (DDL, statistics, relationships, description, manifest, diff).
- Column classifications and inferred semantics.
- What an absent field, block or file means, collected for a reader (§7).
- Versioning and compatibility rules.
- Conformance criteria.

### 0.2 What this spec does NOT cover

- Producer implementation details (how a producer connects to a database, computes statistics, infers relationships).
- Consumer behavior (how a consumer renders, queries, or assembles fragments from the format).
- The dbprint CLI surface — that's documented separately.

### 0.3 Terminology

- **Producer**: any tool that writes dbprint-format artifacts. The dbprint CLI is one producer; future producers may include dbt, Atlas, IDE integrations.
- **Consumer**: any tool that reads dbprint-format artifacts. AI coding agents, drift dashboards, CI gates, the `dbprint context` command itself.
- **Print**: a directory tree containing artifacts for one or more tables in a database connection.
- **Connection**: a named scope inside a project, typically mapping 1:1 to a database/warehouse credential set.

### 0.4 Requirement levels

This document uses **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** per RFC 2119.

---

## 1. Directory layout

### 1.1 Project root

A project root contains:

- A `.dbprint.yaml` config file (project-level).
- A `prints/` directory containing one subdirectory per connection.

### 1.2 Connection root

`prints/<connection_name>/` contains:

- `manifest.yaml` (REQUIRED) — index over all per-table artifacts.
- `diff.yaml` (REQUIRED once `manifest.yaml` records any table) — latest structured diff.
- `reading.md` (REQUIRED) — generated consumer guide (§1.2.1).
- `manifest.annotations.yaml` (OPTIONAL; user-authored; §2.7.3) — connection-grain human notes.
- One or more namespace path directories below.

#### 1.2.1 The consumer guide

`reading.md` teaches a consumer how to read the rest of the print — not the format grammar (this document already does that), but which fields to trust for what, and the questions a reader can answer without querying the database. It is a generated file: a producer MUST overwrite it in full on every `generate`, byte-identical run to run unless the producer's own generator version has changed. A consumer MUST NOT hand-edit it — the next `generate` discards any edit without warning, the same terms `manifest.yaml` and `diff.yaml` are held to.

### 1.3 Namespace path

Per-adapter hierarchy:

| Adapter | Path shape | Example |
|---|---|---|
| Snowflake | `<database>/<schema>/` | `arboretum/seedbank/` |
| PostgreSQL | `<schema>/` | `public/` |
| MySQL | `<database>/` | `analytics/` |

Path segments MUST be lowercase. Producers MUST lowercase identifier segments when constructing paths. SQL identifier content inside `.sql` files MAY remain in native case (e.g., Snowflake's `GET_DDL` typically emits UPPERCASE).

### 1.4 Per-table directory

Each table/view/matview is a directory at the leaf of its namespace path. The directory contains the table's artifacts:

```
<namespace_path>/<object_name>/
├── ddl.sql                        (REQUIRED for all object types)
├── statistics.yaml                (REQUIRED for all object types; catalog-only for plain views, §2.2.15)
├── relationships.yaml             (REQUIRED for tables and matviews; MAY be absent for plain views)
├── description.md                 (OPTIONAL; user-authored)
├── statistics.annotations.yaml    (OPTIONAL; user-authored; §2.7.1)
└── relationships.annotations.yaml (OPTIONAL; user-authored; §2.7.2)
```

§7.3 collects what each of these files means when it is absent.

### 1.5 Identifier handling

The format maintains a 1:1 bijection between SQL identifiers and filesystem path segments, modulo case folding. Producers MUST reject identifiers that would break this bijection rather than escape, hash, or otherwise transform them.

#### 1.5.1 Character allowlist

After lowercase normalization (§1.3), a path segment MUST match the regex:

```
^[a-z0-9_][a-z0-9_.-]*$
```

That is:
- First character: ASCII lowercase letter, digit, or underscore (`[a-z0-9_]`)
- Subsequent characters: those plus hyphen (`-`) and period (`.`)
- Allowed character set (ASCII only): `a-z`, `0-9`, `_`, `-`, `.`

Producers MUST reject identifiers that lowercase to a path segment not matching this regex, including:

- All ASCII non-alphanumerics except `_`, `-`, `.`
- All Unicode (BMP and beyond)
- All control characters (0x00–0x1F)
- All whitespace (space, tab, newline, etc.)
- Segments beginning with `.` (Unix hidden-file convention)

Leading hyphen (`-users`) and leading digit (`9users`) are allowed.

dbprint identifiers are ASCII-only. Production databases overwhelmingly use ASCII identifiers; Unicode is exotic and adds cross-platform / git / search complexity for little AI-context utility gain.

#### 1.5.2 Case-collision detection

At scan time, after applying the allowlist check, producers MUST compute the lowercased path for every matched identifier and reject the run when two distinct SQL identifiers produce the same lowercased path.

Example: a Snowflake schema with both quoted `"Users"` (stored as-is) and unquoted `USERS` (stored UPPERCASE) → both lowercase to `users`. Rejected before any file is written.

Rare in practice but the format's bijection invariant depends on it.

#### 1.5.3 No length enforcement

dbprint does not enforce a maximum identifier length. Filesystem natural limits apply (typically 255 bytes per segment on Linux ext4 / macOS APFS / Windows NTFS; Snowflake's 255-char identifier limit matches). Producers SHOULD NOT pre-emptively reject on length grounds.

Long total paths may exceed Windows' default 260-char path limit. Documented as a known cross-platform consideration but not actively rejected.

#### 1.5.4 Why reject rather than escape or hash

**Escape** (e.g., URL-encode `weird name` → `weird%20name`):

- Pollutes git diffs with `%XX` sequences
- Loses greppability — `grep -r 'weird name' prints/` doesn't find it
- Breaks the "path == identifier" mental model
- Path becomes ambiguous: encoded `users%20v2` vs a literal SQL identifier named `users%20v2`

**Hash** (e.g., `weird name` → `weird_a3f9b/`):

- Loses identity — `ls` shows hashes, not names
- Hash collision risk (low but non-zero for short hashes)
- Renames trigger cascading filesystem changes even when the identifier didn't change

**Reject + `exclude:` selectors as the escape hatch**:

- Preserves the 1:1 bijection between SQL identifier and filesystem path (modulo case folding)
- Forces the user to make an explicit choice ("this table exists; leave it unprofiled")
- Preserves human readability and greppability
- Real-world rare — production warehouses almost never have weird identifiers
- Error message is actionable: rename in DB, OR add an `exclude:` pattern

#### 1.5.5 Error message contract

Producers MUST surface rejection with:

```
ERROR: Table identifier rejected: <FQN>
  Reason: <reason-code>
  Detail: <character / collision-target / specific cause>
  Resolution: Either rename the identifier in the database, OR exclude it via .dbprint.yaml selectors:
    exclude:
      - "<pattern that matches this identifier>"
```

Reason codes:

- `contains-unsafe-character` — identifier contains a disallowed character
- `leading-period` — identifier lowercases to a segment starting with `.`
- `case-collides-with-<other-FQN>` — two SQL identifiers produce the same lowercased path

Producers MUST exit with code 1 (generic error / configuration issue) on identifier rejection. Not code 4 (connection error) — this is a config/scoping problem, not a DB-availability issue.

---

## 2. Artifacts

### 2.1 `ddl.sql`

Native-dialect SQL DDL for the object — the canonical SQL representation of the object's schema-shape, post-normalization for diff stability.

This file is NOT versioned with a `format_version` header — it's SQL, and the manifest entry carries the format reference.

#### 2.1.1 Per-adapter source SQL

| Adapter | Object type | Source |
|---|---|---|
| Snowflake | table | `SELECT GET_DDL('TABLE', '<fqn>')` |
| Snowflake | view / matview | `SELECT GET_DDL('VIEW', '<fqn>')` |
| PostgreSQL | table | `pg_dump --schema-only --table=<fqn> <db>` (shell-out) |
| PostgreSQL | view | `pg_dump --schema-only --table=<fqn> <db>` |
| MySQL | table | `SHOW CREATE TABLE <fqn>` |
| MySQL | view | `SHOW CREATE VIEW <fqn>` |

Postgres uses shell-out to `pg_dump`. Reconstruction from `pg_catalog` is not implemented.

#### 2.1.2 Normalization philosophy

`ddl.sql` holds the text the source command in §2.1.1 returned, modified only by the strip list in §2.1.3. Everything else about the file — keyword casing, indentation, line breaks, statement style, quoting — is the database's own, which is why it differs from one adapter to the next.

dbprint applies **light normalization**: strip well-defined adapter noise to enable diff stability, preserve everything semantically meaningful, keep adapter idioms intact. Producers MUST NOT reflow / canonicalize / reorder / reformat the SQL — the DDL stays in the adapter's native style. The strip list (§2.1.3) is exhaustive; producers MUST NOT strip more, MUST NOT strip less.

#### 2.1.3 What MUST be stripped (per adapter)

**PostgreSQL** (`pg_dump` output):

- Leading header block (`-- PostgreSQL database dump`, `-- Dumped from database version X.Y`, `-- Dumped by pg_dump version A.B`, `-- Started on TIMESTAMP`)
- Trailing footer (`-- PostgreSQL database dump complete`)
- All session-control `SET` statements (`SET statement_timeout`, `SET lock_timeout`, `SET idle_in_transaction_session_timeout`, `SET client_encoding`, `SET standard_conforming_strings`, `SET check_function_bodies`, `SET xmloption`, `SET client_min_messages`, `SET row_security`, `SET search_path`)
- `SELECT pg_catalog.set_config(...);` calls
- Per-object banner comments (`-- Name: X; Type: Y; Schema: Z; Owner: -`) pg_dump emits ahead of every statement - the banner never carries information the statement immediately below it doesn't already state, and stripping it does not touch the statement itself, including `COMMENT ON` (see 2.1.4)
- `GRANT` / `REVOKE` statements (access-control, not schema)
- `CREATE TRIGGER`, `CREATE RULE` (behavior, not schema-shape — out of scope for DDL)
- `\restrict` / `\unrestrict` psql meta-commands — client directives, not schema, and their token is regenerated on every invocation, so keeping them would break the §2.1.6 diff-stability guarantee
- Multiple consecutive blank lines collapsed to a single blank between logical statements

**MySQL** (`SHOW CREATE TABLE` output):

- `AUTO_INCREMENT=<N>` clause from the table-options trailer — the counter is volatile (changes on every INSERT) and produces noise without semantic content
- `AUTO_INCREMENT` keyword on COLUMN definitions is PRESERVED (it defines the auto-increment column)

**Snowflake** (`GET_DDL` output):

- Trailing whitespace per line (minimal — Snowflake output is already clean)

**All adapters**:

- Trailing whitespace per line
- Final newline at end of file ensured (POSIX convention) — added if the adapter output lacks it

#### 2.1.4 What MUST be preserved

- Full `CREATE TABLE` / `CREATE VIEW` / `CREATE MATERIALIZED VIEW` statement(s)
- All constraints (PK, FK, UNIQUE, CHECK, DEFAULT, NOT NULL)
- All `COMMENT ON` statements (table and column comments — semantic info, distinct from the user-authored `description.md` artifact)
- Native casing of identifiers (Snowflake UPPERCASE typical, Postgres lowercase typical, MySQL backtick-quoted)
- Native quoting style (Snowflake unquoted/quoted, Postgres unquoted/quoted, MySQL backtick)
- Engine / charset / collate clauses (MySQL — semantic table options)
- Constraint names (adapter-native)
- `CREATE OR REPLACE` (Snowflake idiom)
- All custom SQL types referenced
- Index definitions inline with CREATE TABLE (Postgres / MySQL inline index syntax)
- `CREATE INDEX` statements appearing after CREATE TABLE in `pg_dump` output

#### 2.1.5 Multi-statement DDL

`pg_dump` typically emits multiple statements per table: a `CREATE TABLE`, several `ALTER TABLE ADD CONSTRAINT`, optionally `COMMENT ON`, optionally `CREATE INDEX`. The `ddl.sql` file SHALL contain all of them in `pg_dump`'s emission order, separated by semicolons and blank lines.

Producers MUST NOT split multi-statement DDL into multiple files. One `ddl.sql` per table — multi-statement when the adapter emits multiple statements.

#### 2.1.6 Diff stability guarantee

After normalization, running `dbprint generate` twice on the same unchanged schema MUST produce byte-identical `ddl.sql` files. The strip rules in §2.1.3 are exactly what's required to make this guarantee hold against the noisy elements each adapter introduces.

This is a normative correctness criterion for any conforming producer.

Adapter-version-stable normalization (DDL stays identical across upgrades of the underlying DB engine that change `pg_dump` / `GET_DDL` output) is out of scope; that conformance enhancement is not implemented.

#### 2.1.7 Edge cases

| Case | Resolution |
|---|---|
| Table has zero constraints | DDL is shorter; emit as-is post-normalization. Cross-artifact signal is empty `refers_to`/`referenced_by` in `relationships.yaml`. |
| Table with no comments | DDL has no `COMMENT ON`. The user-authored `description.md` is a separate artifact. |
| View with complex query body | Preserve the full view definition. No normalization of the SELECT clause. |
| `CREATE INDEX` in `pg_dump` output | Preserved. This is the source of truth for secondary indexes (per §2.6.7). |
| Adapter version produces different DDL after upgrade | Normalization does not stabilize across adapter version changes. Consumers compare DDL files as opaque strings; cross-version stability is not implemented. |
| `GRANT` / `REVOKE` / `CREATE TRIGGER` / `CREATE RULE` in `pg_dump` | Stripped — not schema-shape. dbprint captures schema, not behavior or access control. |

### 2.2 `statistics.yaml`

Column-level statistics designed for SQL-writing utility. Every field is one of:

1. **Directly usable by an LLM in writing a query** (e.g., distinct values for `WHERE`).
2. **A signal flag that changes the consumer's approach** (e.g., `inferred.candidate_key: true`).

The reference JSON Schema SHALL be at `spec/v1/statistics.schema.json`. The normative content below is authoritative; the JSON Schema is its mechanical companion.

#### 2.2.1 Top-level fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `format_version` | int | ALWAYS | `1` for v1 artifacts |
| `table` | string | ALWAYS | Fully-qualified name as its adapter reports it: `schema.table` (PostgreSQL), `db.schema.table` (Snowflake), `db.table` (MySQL). The number of parts is the adapter's, not the format's, and selectors and directory paths follow it |
| `type` | enum | ALWAYS | `table` \| `matview` \| `view`. A `view` file always carries `catalog_only` (§2.2.15) - no query is issued against a plain view. |
| `profiled_at` | string | ALWAYS | ISO 8601 with explicit UTC offset (`2026-05-17T22:48:01Z`). Producers MUST normalize to UTC; no local-time-with-offset strings. |
| `catalog_only` | bool | OPTIONAL | Present and `true` only when no query was issued for this object at all; see §2.2.15 |
| `row_count` | int ≥ 0 | CONDITIONAL | Total rows in the table, including nulls. Not affected by `scope`. REQUIRED unless `catalog_only` is present, in which case MUST NOT be emitted; see §2.2.15 |
| `row_count_method` | enum | CONDITIONAL | `exact` (`COUNT(*)`) \| `approximate` (system-table estimate). REQUIRED unless `catalog_only` is present, in which case MUST NOT be emitted; see §2.2.15 |
| `scope` | map | OPTIONAL | Present only when the statistics describe part of the table; see §2.2.8 |
| `null_patterns` | map | CONDITIONAL | Which columns are null on the same rows. REQUIRED when any column reports a non-zero `null_count`, MUST NOT be emitted otherwise. See §2.2.10 |
| `physical_layout` | map | OPTIONAL | The table's declared clustering/partitioning key. Absent means not clustered/partitioned, never "not checked" - every producer MUST report on this, except that a `catalog_only` file's absence needs no further explanation: a producer MAY still emit the block there from catalog metadata alone. See §2.2.11, §2.2.15 |
| `grain` | map | OPTIONAL | What identifies a row: declared keys always, plus a bounded measured probe. A conforming producer MUST emit it, `keys` possibly empty, never silently omitted; absence on an artifact predating this field is not itself a defect. See §2.2.12 |
| `dependencies` | list | OPTIONAL | Which columns determine which, measured over the scanned rows. A conforming producer MUST emit it, possibly empty, never silently omitted, unless `catalog_only` is present, in which case it MUST NOT be emitted; absence on an artifact predating this field is not itself a defect either. See §2.2.13, §2.2.15 |
| `columns` | map | ALWAYS | Keyed by column name; values are per-column stats objects per §§2.2.2–2.2.4 |

**The map key is always lowercase.** Producers MUST lowercase every column name for this key, on every adapter, regardless of the case the catalog reports it in — the same normalization §1.3 requires for path segments, applied here at column-name grain so detection (§4.4.3), `statistics.annotations.yaml` keys (§2.7.1) and this map itself agree on one spelling for a schema ported between engines. Where a column's catalog-reported spelling differs from its lowercased key, the producer MAY carry it forward as `physical_name` (§2.2.4) so a consumer can still address the column directly.

#### 2.2.2 Universal per-column fields

Required on every column except `unsupported` (see §2.2.3):

| Field | Type | Notes |
|---|---|---|
| `sql_type` | string | Native dialect type as the database reports it (`VARCHAR(64)`, `TIMESTAMP_NTZ(9)`, `JSONB`) |
| `nullable` | bool | From DDL, not inferred from data |
| `null_count` | int ≥ 0 | Count of NULL rows in this column |
| `null_rate` | float [0, 1] | `null_count / rows_scanned`; `0` when `rows_scanned == 0`; floored/ceilinged at the two boundaries per §2.2.6 |
| `cardinality` | int ≥ 0 | Distinct non-null values, under the collation the source compares them with — see below |
| `cardinality_ratio` | float [0, 1] | `cardinality / rows_scanned`; `0` when `rows_scanned == 0`; floored at the lower boundary per §2.2.6 |
| `cardinality_method` | enum | `exact` (`COUNT(DISTINCT)`) \| `approximate` (a measurement not obtained by counting - see below) |
| `classification` | enum | One of the 8 values per §3 |

**`approximate` names more than one measurement.** It covers a live sketch computed over the scanned rows (`APPROX_COUNT_DISTINCT`, an HLL estimate this run's own read produced) and a stored planner statistic of unbounded staleness (a catalog's own last-`ANALYZE` estimate, taken at an unrelated time over the whole table rather than the scanned set). A consumer cannot tell which kind a given `approximate` is from this field alone - both name "not counted", not "counted this way". Producers MAY publish either under the same token; a producer publishing the catalog-statistic kind SHOULD re-probe a column exactly once its estimated ratio nears the §4.2 candidate-key threshold, so an estimate's own imprecision cannot cost a column its `candidate_key` verdict.

**Distinctness is collation-relative.** For a string-valued column, `cardinality`, the `values` list (§2.2.4) and `distribution` (§2.2.5) are computed under whatever collation the source compares that column's values with — the format states this rather than pinning one collation across engines, so the read a producer already issues is what the artifact describes. Two prints of one logical schema taken through different engines, or through the same engine under different column-level collations, can disagree on `cardinality` for a text column for this reason alone; the disagreement is explicable from the manifest's `default_collation` (§2.5) and a column's own `collation` (§2.2.4) when it overrides that default, never from the number by itself.
#### 2.2.3 Required / optional / forbidden field matrix per classification

Cell legend: **R** = REQUIRED, **O** = OPTIONAL (emit if applicable), **—** = MUST NOT emit, **R†** and **R‡** = REQUIRED unless the marked footnote's condition holds, under which the field MUST NOT be emitted, **R (scoped)** = REQUIRED when the file's top-level `scope` block is present (§2.2.8), MUST NOT emit otherwise — uniform across every classification, unlike the two per-column conditions above.

| Field | `unsupported` | `boolean` | `json` | `foreign_key_candidate` | `categorical` | `temporal` | `numeric` | `text` |
|---|---|---|---|---|---|---|---|---|
| `sql_type` | R | R | R | R | R | R | R | R |
| `nullable` | R | R | R | R | R | R | R | R |
| `null_count` | R | R | R | R | R | R | R | R |
| `null_rate` | R | R | R | R | R | R | R | R |
| `cardinality` | — | R | R | R | R | R | R | R |
| `cardinality_ratio` | — | R | R | R | R | R | R | R |
| `cardinality_method` | — | R | R | R | R | R | R | R |
| `classification` | R | R | R | R | R | R | R | R |
| `physical_name` (§2.2.4) | O | O | O | O | O | O | O | O |
| `collation` (§2.2.4) | O | O | O | O | O | O | O | O |
| `physical_layout_key` (§2.2.11) | O | O | O | O | O | O | O | O |
| `rows_scanned` (§2.2.8) | R (scoped) | R (scoped) | R (scoped) | R (scoped) | R (scoped) | R (scoped) | R (scoped) | R (scoped) |
| `inferred.looks_like` | — | — | — | O | O | — | — | O |
| `inferred.sampled` (§4.1.3) | — | — | — | O | O | — | — | O |
| `inferred.matched` (§4.1.3) | — | — | — | O | O | — | — | O |
| `inferred.sensitivity` | — | O | O | O | O | O | O | O |
| `inferred.epoch_unit` (§4.5) | — | — | — | O | O | — | O | O |
| `redacted` | — | O | — | O | O | O | O | O |
| `inferred.candidate_key` (§4.2) | — | O | O | O | O | O | O | O |
| `inferred.candidate_key_exception` (§4.2) | — | O | O | O | O | O | O | O |
| `inferred.fk_candidate` (reserved; see §4.3) | — | — | — | O | — | — | — | — |
| `values` | — | R | — | R | R | — | — | R‡ |
| `values_coverage` | — | R | — | R | R | — | — | R‡ |
| `values_coverage_method` (§2.2.4) | — | O | — | O | O | — | — | O |
| `distribution` | — | — | — | R | R | R | R | R‡ |
| `frequencies` (§2.2.4) | — | — | — | — | — | R | R | — |
| `range` (min, max) | — | — | — | — | — | R† | R† | — |
| `range.span_days` | — | — | — | — | — | R† | — | — |
| `percentiles` | — | — | — | — | — | R† | R† | — |
| `freshness` | — | — | — | — | — | R | — | — |
| `unrepresentable` | — | — | — | — | — | O | — | — |
| `sketch` (§2.2.14) | — | O | — | O | O | O | O | O |

Producers MUST emit exactly the fields marked R. They MAY emit O fields when applicable. They MUST NOT emit — fields. The `inferred` sub-object MUST be omitted entirely when it has no sub-fields. `inferred.candidate_key` is set whenever `cardinality_ratio` clears the SPEC 4.2 threshold, independent of classification - it is not required by any row, since a column's ratio may fall short of it regardless of type. `sketch` is O everywhere a join-key column's classification can land, per §2.2.14's own emission rule (edge participation, canonical type, redaction and scope), never per this matrix alone - a `categorical` or `numeric` column carries one only when §2.2.14's own conditions hold, the same way `redacted` is O here but gated by whether a rule actually matched.

Every field this matrix marks anything but **R** can therefore be absent from a conforming column, usually for more than one reason. §7.2 lists what each absence can mean, and is derived from the rows above.

† **Bounds under a dropped column.** `range`, its `span_days`, and `percentiles` are cell values, so a column declaring `redacted: drop` emits none of them: that primitive emits no literal at all, and a bound is nothing but a literal (§2.2.9). For every other column of these classifications the three cells are REQUIRED; for a dropped one they MUST NOT be emitted. `freshness` stays REQUIRED under every primitive, `drop` included: a derived value is a cell value whenever the derivation can be run backwards, and `max_age_days` can be - `range.max = profiled_at - max_age_days`, from any artifact carrying a `redacted` marker. §2.2.9 states the coarsening this requires rather than an omission.

‡ **A prose column publishes no value list.** A `text` column whose `inferred.looks_like` is `prose` (§4.1.1) MUST NOT emit `values`, `values_coverage` or `distribution`; every other `text` column MUST emit all three. The top entries of a free-text column describe nothing a consumer can act on, while the grouped scan that finds them is among the most expensive statements a producer issues — so this is a saving the format grants rather than an omission it tolerates, and a producer SHOULD skip the query rather than discard its result. `distribution` is covered because for this classification it is derived from the value list, and computing it another way would re-issue the scan the exemption exists to avoid.

This is the only cell conditional on a field of `inferred`, and it reaches `text` alone. `categorical` runs the same detection (§4.1.5) and can also report `prose`, but its cardinality is bounded by construction: the scan is cheap there, and the enumeration is what the classification is for. A column that is prose in one run and not the next therefore changes its emitted field set, which `diff` reports like any other change.

#### 2.2.4 Sub-object schemas

**`physical_name`** (string, OPTIONAL):

The column's identifier as its catalog reports it, in the case a producer would have to quote to address the column directly. Producers MUST omit this field when it is identical to the map key (§2.2.1) — the common case on every adapter that already reports lowercase identifiers, and on any column whose name happened to be written in lowercase to begin with. A reader who does not find it MAY assume the map key addresses the column unchanged.

```yaml
columns:
  firstname:
    physical_name: firstName        # catalog spelling; the map key is always lowercase
    ...
```

**`collation`** (string, OPTIONAL): the catalog's own name for the collation this column compares under, when the catalog reports one explicitly set. Producers MUST omit this field when the column carries no explicit collation, or when its collation is identical to the connection's `default_collation` (§2.5) — the common case, since most columns inherit the connection default. A reader who does not find it MAY assume the column compares under `default_collation`.

```yaml
columns:
  country_code:
    collation: utf8mb4_bin          # overrides the connection default recorded in manifest.yaml
    ...
```

**`inferred`** (omit when all sub-fields absent):

```yaml
inferred:
  looks_like: <pattern>           # per §4.1; omit when no match
  sampled: <int>                  # per §4.1.3; the draw looks_like was scored against
  matched: <int>                  # per §4.1.3; how much of the draw agreed with looks_like
  candidate_key: true             # only ever true; omit when not set
  candidate_key_exception: measured_duplicates | estimated  # per §4.2; omit at ratio 1.0
  sensitivity: <category>         # per §4.4; omit when nothing detected
  epoch_unit: seconds | milliseconds  # per §4.5; omit when not detected
  fk_candidate: { ... }           # reserved, not defined (see §4.3)
```

`sampled` and `matched` are emitted together, and only beside `looks_like`: both MUST be omitted when `looks_like` is absent, even on a classification where a sample was drawn and cleared no pattern. Producers MUST NOT emit either field alongside `epoch_unit` or `sensitivity` alone - both axes may read the same draw, but the pair describes `looks_like`'s own verdict exclusively, per §4.1.3.

**`values`** (boolean, categorical, foreign_key_candidate, text):

```yaml
values:
  - { value: <scalar>, count: <int> }
```

One ordered list describes every column that carries value data. Entries are ordered by `count` DESC, with ties broken by lexicographic order on the string form of `value` — deterministic across runs. Values MUST be strings, numbers, or booleans. NULL MUST NOT appear (NULL is tracked separately via `null_count`).

**How much of the column the list describes is decided by cardinality, not by classification.** When the column's distinct count is at most `top_n_values` (config; default 20) the list is exhaustive and carries every distinct non-null value. Above it, the list carries the `top_n_values` most frequent entries. A producer MUST NOT decide this from the classification: a low-cardinality column is enumerated in full whether it is `boolean`, `categorical`, `foreign_key_candidate` or `text`.

**An exhaustive list's entry count equals `cardinality`.** "Carries every distinct non-null value" means the number of entries IS the distinct count - a producer publishing `values_coverage: 1.0` with a list shorter or longer than `cardinality` has broken this obligation regardless of what its row-level counts sum to.

A list rather than a map, because YAML and JSON mappings are unordered by definition — an ordering rule on a mapping asks consumers to honor something their parsers may discard.

**`values_coverage`** (always present alongside `values`):

- Float in [0, 1]
- = sum of `values` counts / (rows_scanned - null_count)
- Exactly `1.0` when the list is exhaustive — carries every distinct non-null value the column has — regardless of what the raw division would give. A producer MUST NOT let rounding report `1.0` for a list it truncated, and MUST emit `1.0` for a column with no non-null rows. Under `scope` (§2.2.8), `1.0` states that the list is exhaustive **over the scanned set**, not over the whole table; the same column block's `rows_scanned` says what that set was, so a consumer reading `values_coverage` alongside it never mistakes scanned-set completeness for a table-wide guarantee

**`values_coverage_method`** (enum, OPTIONAL; `measured` | `bounded`): whether the emitted `values_coverage` is the raw quotient (`measured`) or the clamp the rule above describes (`bounded`), because the value list and the population it is measured against were not read at the same instant - the same distinction §2.2.10's `coverage_method` draws, applied to this field.

Emitted only for an exhaustive list a producer's own truncation did not cut short; a truncated list is short by design, which is a different, already-explained condition this field does not cover. `measured` states the listed counts agreed with the non-null rows they were scanned against; `bounded` states a producer detected the two disagreeing - the listed counts exceeding the non-null rows, the phase A / phase B disagreement a live table taking writes between the two reads can produce. The detector is one-sided, catching only an overrun: a row deleted between the two reads shrinks the listed total below the scanned count instead, which reads as `measured` the same as a genuine same-read agreement would.

**Day counts.** `range.span_days` and `freshness.max_age_days` are the only statistics a
producer derives by arithmetic rather than by reading. Both are the count of **whole elapsed
days** between two instants:

```
day_count(earlier, later) = floor( (later - earlier) in seconds / 86400 )
```

Producers MUST compute elapsed time, NOT the number of calendar-date boundaries crossed.
The two differ in both directions and are not interchangeable: a span from 01:00 to 21:00
elapses 20 hours and crosses no date boundary, while a span from 23:50 to 00:10 elapses 20
minutes and crosses one. Counting boundaries would report `1` and `0` respectively; counting
elapsed whole days reports `0` for both, which is what this rule requires.

Flooring rather than rounding is what makes a sub-day span report `0` (§2.2.7) without a
special case: any interval shorter than 24 hours has zero whole days in it.

**`range`**:

```yaml
range:
  min: <val>                      # rendered in the column's own domain; see below
  max: <val>
  span_days: <int>                # TEMPORAL ONLY; day_count(min, max)
```

**`percentiles`** (numeric, temporal):

```yaml
percentiles:
  p01: <val>                      # key format: `p` + zero-padded 2-digit integer percent
  p25: <val>
  p50: <val>
  p75: <val>
  p99: <val>
```

Percentile values in `.dbprint.yaml` configuration MUST be representable as integer percentages (multiples of 0.01); fractional percentiles like 0.999 are REJECTED by config validation. Emitted values are rendered in the column's own domain, exactly as `range` is.

**Percentiles MUST ascend with their keys** (non-decreasing, not strictly ascending — a single-valued column legitimately publishes the same value at every key), and, when `range` is present, **every percentile MUST lie within `[range.min, range.max]`**. Both bounds and every percentile come from one statement per column, so a producer cannot correctly emit a percentile outside its own range or out of order with its neighbors.


**Domain rendering.** `range.min`, `range.max` and every `percentiles` entry are emitted **in the column's own domain** - the form a predicate against that column would use. For most temporal types that is an ISO 8601 string (`'2026-05-17T22:48:01Z'`, `'2026-05-17'`). For types that carry no date it is not:

| Column type | Emitted | A consumer then writes |
|---|---|---|
| `TIMESTAMP` / `DATE` | `'2026-05-17'` | `WHERE seen_at >= '2026-05-17'` |
| `TIME` | `'08:00:00'` | `WHERE run_at >= '08:00:00'` |
| `YEAR` | `1960` | `WHERE made BETWEEN 1960 AND 2019` |

The rule exists so a value read out of an artifact goes back into a query unchanged. The ISO form of a `YEAR` — `'1960-01-01'` — does not: MySQL evaluates a YEAR-to-date comparison to NULL, and the predicate returns no rows without reporting an error.

**`frequencies`** (numeric, temporal):

```yaml
frequencies:
  top: <int>       # the most frequent listed count
  bottom: <int>    # the least frequent listed count
  listed: <int>    # how many counts were listed
  total: <int>     # the listed counts added up
```

A fixed-size summary of the same top-N frequency fetch `distribution` (§2.2.5) is computed from, published because these two classifications carry no `values` list for a validator to recompute the verdict against. All four are counts over the scanned set (§2.2.8), never a share, so a consumer recomputes any ratio itself against the `non_null` and `cardinality` the column already publishes rather than trusting a rounded one. The set publishes no literal value, so it does not reinstate `values` on a classification the matrix forbids it on.

**`unrepresentable`** (temporal only, optional):

A temporal column MAY carry `unrepresentable`: a list naming the emitted fields - `min`, `max`, and any emitted `percentiles` key - whose value lies outside the years `0001` through `9999` inclusive, proleptic Gregorian. The boundary is stated as this calendar range, not as any one library's or driver's limits, because two conforming producers reading the same column MUST mark the same fields regardless of the runtime they were written in.

The domain-rendering rule above already makes the text form correct for such a value - a database's own rendering of an instant beyond the standard calendar is exactly what belongs in the file. `unrepresentable` is what lets a consumer feeding that value to a typed parser degrade deliberately instead of crashing.

```yaml
created_at:
  classification: temporal
  sql_type: TIMESTAMP_NTZ
  range:
    min: "1970-01-01T00:00:00"
    max: "52030-01-01T00:00:00"
    span_days: 15376234
  percentiles:
    p50: "2026-01-01T09:30:00"
    p95: "11000-07-05T00:00:00"
  unrepresentable: [max, p95]
```

Entries are ordered as the fields are emitted (`min`, `max`, then percentile keys in ascending order) and carry no duplicates. The key is omitted entirely when no emitted value qualifies.

The marker names a field the format carries, not a claim about the value's correctness: a column whose maximum is year 52030 probably holds a mis-scaled epoch, and saying so is not this field's job - carrying the value and flagging that it will not parse is. A `redacted: drop` column emits no bounds at all (see the † footnote above), so no field can be listed and `unrepresentable` MUST be absent alongside them.

`unrepresentable` deliberately introduces no diff change kind of its own: it is derived from the same bound its own `statistic_changed` event already reports, so a value moving in or out of the representable range is already visible as a change to `range.min`, `range.max`, or the percentile in question.

**Date-less temporal types.** A type carrying a time of day but no date - `TIME`, and `TIME WITH TIME ZONE` - has no instant to measure from. Producers MUST emit `range.span_days: 0` (every such value falls inside one day) and `freshness.max_age_days: 0`. Producers MUST NOT derive either quantity by arithmetic against the current timestamp; the operation is undefined for these types and the dialects fail differently, some silently.

**`freshness`** (temporal only):

```yaml
freshness:
  max_age_days: <int>             # max(0, day_count(max(column), profiled_at))
  classification: live | stale | dormant
```

`max_age_days` is clamped at `0`. A column whose newest value has not happened yet — a
queue of future-dated work, an expiry timestamp, a sentinel birth date — would otherwise produce a
negative age, which this field does not carry. Producers MUST emit `0` for such a column,
and it therefore classifies `live`. This is the answer for a column where only some values
are in the future and for one where all of them are: the rule is applied to the maximum, so
the two cases are the same case.

The clamp is deliberately lossy. `freshness` answers "how stale is this data", and data that
has not arrived yet is not stale, so `0` is the honest reading of that question — but it does
not distinguish a column written a moment ago from one scheduled a century out. Consumers
needing that distinction MUST read `range.max`, which always carries the true maximum.

**The day count is computed in UTC.** `profiled_at` is always UTC (§2.2.1). A zone-aware
`range.max` is an absolute instant, so `day_count(range.max, profiled_at)` is unambiguous.
A `timestamp without time zone` or MySQL `DATETIME` value carries no zone; producers MUST
treat its rendered reading as UTC when computing `max_age_days`, the same assumption the
value's own domain rendering makes. This can differ from the value's true civil age by the
server's UTC offset - at most one day, and only when the newest value sits within that
offset of midnight, where it can shift the `live`/`stale`/`dormant` bucket below.

Thresholds (hardcoded; not configurable):

- `live`: max_age_days < 7
- `stale`: 7 ≤ max_age_days < 90
- `dormant`: max_age_days ≥ 90

**A column carrying a `redacted` marker emits `max_age_days` floored to the nearest 90** rather than the exact count — see §2.2.9, which states the reason and the identical rule for `range.span_days`. `classification` is still computed from the true, uncoarsened age, never re-derived from the floored integer.

#### 2.2.5 `distribution` rules

Enum: `uniform` | `imbalanced` | `dominant_value` | `long_tail`.

Producers MUST evaluate the rules in this priority order; first match wins:

1. `dominant_value` — top value's count / (rows_scanned - null_count) ≥ 0.95
2. `long_tail` — cardinality > `top_n_values` AND sum of the listed counts / (rows_scanned - null_count) < 0.30
3. `imbalanced` — max-frequency / min-frequency > 2× (over non-null values)
4. `uniform` — fallthrough (max/min ratio ≤ 2×)

Where the `values` list is exhaustive (`values_coverage` of `1.0`), `long_tail` is undefined and producers MUST skip step 2: a complete enumeration has no tail beyond itself. Steps 3 and 4 read the true minimum frequency in that case, and the least frequent listed entry otherwise.

A validator was not present at the scan and cannot see `rows_scanned` directly, so it checks steps 1 and 2 only where `values` is exhaustive, against the sum of the listed counts - which then equals `rows_scanned - null_count` exactly, since an exhaustive list already accounts for every non-null value the scan produced. A truncated list is not checked against this rule at all.

`numeric` and `temporal` carry no `values` list, so the check above cannot reach them; `frequencies` (§2.2.4) exists to close that gap. Its four integers reproduce the same priority order over the scanned set (`non_null`) and `cardinality` the column already publishes: `top` decides step 1, `total` decides step 2's ratio, and the `top`-to-`bottom` spread decides steps 3 and 4. A validator reads the fetch's own exhaustiveness from `listed == cardinality`, when `cardinality_method` is `exact` - an approximate cardinality is not guaranteed to equal the fetch's own count of distinct groups, so a validator MUST NOT check `distribution` against `frequencies` on such a column.

#### 2.2.6 Numerical precision

Numeric stats (range bounds, percentiles, ratios) MUST be rounded to **6 decimal places** before emission. This stabilizes git diffs across runs where adapter floating-point precision varies slightly, while leaving a value's magnitude intact — a maximum of `12345678.9` is emitted as it stands, not rewritten to a rounder number of the same size.

`null_rate` and `cardinality_ratio` (floats in [0,1]) follow the same rule.

**Both ratios are recomputable and checked, with a floor and a ceiling.** `null_rate` MUST equal `null_count / rows_scanned` rounded per this rule, and `cardinality_ratio` MUST equal `cardinality / rows_scanned` rounded the same way - the same identities §2.2.2 already states, now with a conformance check behind them. Two exceptions to plain six-decimal rounding: a nonzero numerator (`null_count` or `cardinality`) MUST NOT round down to `0.0` - it emits `0.000001`, the smallest representable value at this precision, instead. `null_rate` additionally MUST NOT round up to `1.0` unless `null_count == rows_scanned` - a nonzero non-null count emits `0.999999` instead, because `null_rate: 1.0` is a defined sentinel (§2.2.7, §3.3) that imprecision must not manufacture; `cardinality_ratio` carries no such ceiling. `rows_scanned == 0` is the one case this floor does not touch: both ratios stay exactly `0` there, per §2.2.2, since the ratio is undefined rather than merely small. A rounding boundary the producer's own rule lands on - including the floor and the ceiling - is not a violation; a value that disagrees with the recomputed identity is.

A consumer recomputing either ratio by hand from `null_count` or `cardinality` and `rows_scanned` will not reproduce the floored or ceilinged value at the extremes - the published number and the raw division agree everywhere else, but diverge deliberately at the two boundaries this rule exists to avoid publishing.

Counts are exact integers — no rounding. `null_count`, `cardinality` and every `values` entry count are measured over the scanned set (§2.2.8); `row_count` is a separate, table-scale count and MUST NOT be read as measured over the same population.

**Rendering.** Every float in an artifact MUST be written in positional decimal notation. Exponent form (`6.6e-05`) is not permitted anywhere, including inside a `values` list, and the conversion MUST be lossless — the emitted text MUST parse back to the identical float. This is a producer obligation stated in prose rather than a schema constraint, because a validator sees parsed numbers and cannot observe the notation they arrived in.

Rendering notation is not a value change, so it does not conflict with §2.2.7's prohibition on altering listed values: an exact positional expansion of `1e-09` denotes the same number.

#### 2.2.7 Edge cases

| Case | Resolution |
|---|---|
| **Empty table** (`row_count = 0`) | File emitted with `row_count: 0`. Per-column: `null_count: 0`, `null_rate: 0`, `cardinality: 0`, `cardinality_ratio: 0`. `classification` derived from `sql_type` via §3 priority; at `cardinality = 0` this resolves to `categorical` (or `foreign_key_candidate` / `boolean` by FK/type). The value fields are emitted EMPTY rather than omitted: `values: []` + `values_coverage: 1.0` - a column with nothing to list covers all of it - plus `distribution` for the classifications that require it. `unsupported` columns stay minimal (`cardinality: null`, no value fields). A `row_count: 0` carrying a non-zero `scope.rows_scanned` is NOT this case: §2.2.8 lets an estimate that undershot stand as the estimate it is, so that pair reads as inverted rather than as an empty table, and a consumer MUST read the two fields together before concluding a table holds nothing. |
| **All-null column** (`cardinality = 0`, `null_rate = 1`) | Same empty-collection behavior as the empty table for that column. `classification` per §3.3. |
| **Single-value column** (`cardinality = 1`) | Classification = `categorical`. `values: { sole_value: count }`. `distribution: dominant_value`. |
| **Sub-day temporal span** | `range.span_days: 0` — a consequence of the day-count rule (§2.2.4), not an exception to it: an interval shorter than 24 hours contains zero whole days however it sits against the calendar. |
| **Long string values in `values`** | Emitted as-is. No truncation. Producers MAY log warnings when total YAML size exceeds an adapter-defined threshold. Producers MUST NOT alter values **except under a declared redaction** (§2.2.9), which is the one sanctioned substitution and is always announced by the `redacted` marker. |
| **Approximate vs exact methods** | Adapter chooses based on table size / cost. `row_count_method` and `cardinality_method` document the choice. |
| **`values` ties at the cutoff boundary** | Lexicographic order on the string form of `value` decides — deterministic across runs. |
| **Empty scanned set** (`scope.rows_scanned = 0`, `row_count > 0`) | A filter that matched no rows. Per-column shape follows the empty-table row above, but `row_count` keeps the table's own count — the two cases are distinct and MUST NOT be collapsed. |

The empty-collection shapes these rows produce read like omissions and are not; §7.4 states what each one can mean.

#### 2.2.8 `scope` — statistics over part of a table

By default every statistic in this file describes every row of the table. A producer that reads only part of it — because a row predicate was configured, or because the table was sampled to bound cost — MUST say so with a top-level `scope` block. Its absence is the assertion that nothing was skipped.

A statistic measured under this block is present, not partial; §7.1 states why that is the absence readers mistake most often.

```yaml
scope:
  rows_scanned: <int ≥ 0>       # ALWAYS when scope is present; rows the statistics were computed over
  sample: <float (0, 1]>        # OPTIONAL; the fraction the producer asked the database to read - see below
  filter: <string>              # OPTIONAL; the row predicate applied, verbatim
```

**`rows_scanned` is the denominator for every scanned-set-relative field, not only the two ratios.** That set is every §2.2.3 matrix cell outside `sql_type`, `nullable` and `classification` that is not a dash on every classification: `null_count`, `null_rate`, `cardinality`, `cardinality_ratio`, every `values` entry `count`, `values_coverage`'s denominator, `range.min`, `range.max`, `percentiles`, `freshness.max_age_days`, and every `frequencies` integer. `row_count` is the one required field this rule does not touch — it is a count over the whole table, not the scanned set (see below). When `scope` is absent, `rows_scanned` is defined to equal `row_count`, so one rule covers both cases.

**Every column of a scoped file echoes `rows_scanned`.** A reader of one column block recomputes `null_rate`, `cardinality_ratio` and `values_coverage` without leaving it, and needs only the file head's `row_count` to rescale a count to table grain. The field is REQUIRED on every column — `unsupported` included, since its `null_rate` is scanned-set-relative too — whenever the top-level `scope` block is present, and MUST NOT be emitted on any column otherwise (§2.2.3). A file whose `rows_scanned` equals `row_count` still emits the marker on every column; omitting it there would make its absence mean two different things again.

**`row_count` describes the table, not the slice.** It counts the rows in the table whether or not the read narrowed. The two fields are read together: `rows_scanned` of `row_count`.

**`rows_scanned` / `row_count` is the rescaling ratio to table grain, not `sample`.** `sample` is the fraction the producer *asked* the database to read: passed through as configured, or, under a `max_rows_scanned` ceiling, snapped to a power of the producer's own ceiling grid against a catalog estimate — not a measurement of what the scan touched. `rows_scanned` and `row_count` are both required fields already, `rows_scanned` is already the stated denominator above, and naming their ratio as authoritative costs nothing further. Wherever `row_count_method` is `approximate`, this rescaling ratio is itself an estimate: `rows_scanned` is exact, but `row_count` is not.

**`filter` is provenance, not a query language.** Producers record the predicate verbatim and MUST NOT parse, rewrite, normalize, or validate it. Consumers MUST treat it as opaque text — it exists so a reader can judge whether these numbers answer their question, not so a tool can reconstruct the query.

**A table is narrowed one way or the other.** `sample` and `filter` are mutually exclusive, and a block carrying both is invalid. Composing them expresses nothing a wider predicate does not already express, while costing an ordering rule every producer has to honor and forcing the dialects whose sampling construct binds to a base table to wrap that table in a subquery — which is where a sample stops being reproducible. Producers MUST NOT record how the sample was drawn; the sampling constructs available differ per dialect and the difference is not actionable for a consumer.

Rules:

- `sample` and `filter` MUST NOT both be present. Each describes the whole narrowing the producer applied.
- A producer MUST omit the block entirely when it read every row. A block whose `rows_scanned` equals `row_count` and which carries neither `sample` nor `filter` asserts nothing and SHOULD NOT be emitted.
- `rows_scanned` MUST NOT exceed `row_count`, except where `row_count_method` is `approximate` and the estimate undershot — in which case the producer SHOULD emit the exact scanned count and leave `row_count` as the estimate it is.
- `rows_scanned: 0` with `row_count > 0` is well-formed: a predicate that matched nothing.

#### 2.2.9 `redacted` — values withheld

A print enumerates a column's values, and for a low-cardinality column that enumeration is exhaustive — so a small set of personal values is written out in full and committed. The counts are what a consumer needs; the literals are what must not leave the database.

A producer MAY replace or omit cell values, and MUST declare it with a column-level `redacted` marker naming the primitive applied:

| Primitive | Behaviour |
|---|---|
| `mask` | Every literal replaced by one fixed placeholder |
| `drop` | No literal emitted; each entry keeps its `count` |
| `hash` | A salted digest emitted in the literal's place |

**Scope is cell values only.** `null_count`, `null_rate`, `cardinality`, `cardinality_ratio`, `cardinality_method`, the value counts, `values_coverage` and `distribution` MUST be unaffected. They describe the data without disclosing a row. This is cell-level privacy, not aggregation-level privacy, and a consumer may rely on every measurement in a redacted print being the true one.

**Bounds and percentiles are cell values too.** Under `mask` and `hash`, `range.min`, `range.max` and every `percentiles` entry carry the substituted form; under `drop` the `range` and `percentiles` fields are omitted, the one primitive where redacting a bound and omitting it coincide. A consumer MUST NOT order, compare, or perform arithmetic on a bound in a column carrying the marker — a masked maximum still looks like a maximum, and two hashed bounds sort by digest rather than by value, so `min` may sort above `max`.

**A derived value is a cell value whenever the derivation can be run backwards.** `freshness.max_age_days` and `range.span_days` (§2.2.4) are computed by arithmetic rather than read from a cell, but arithmetic that inverts is not exempt on that account: `range.max = profiled_at - max_age_days`, and `profiled_at` is always present (§2.2.1), so an uncoarsened `max_age_days` recovers the maximum a `mask`/`hash`/`drop` marker declares withheld — to the day, from any artifact. `range.min = range.max - span_days` recovers the minimum the same way wherever `range` is still present (`mask`/`hash`; `drop` omits it already). A `temporal` column carrying any `redacted` marker MUST therefore emit both fields floored to the nearest 90 days: `coarsened = 90 * floor(value / 90)`. This holds under every primitive, `drop` included — `freshness` stays REQUIRED there (§2.2.3's matrix footnote), so the coarsening is the only thing standing between a fully dropped column and a birth date recovered to the day. `freshness.classification` is unaffected and still reads the true age: the bucket is coarse enough on its own (§2.2.4) that coarsening it too would only relabel a `dormant` column, not protect one.

**The marker is mandatory whenever a literal was altered or withheld.** An artifact that substitutes values silently is worse than one that omits them, because a consumer cannot tell measurement from fabrication — the same principle the `scope` block (§2.2.8) rests on. A `values` entry carrying no `value` in a column that declares no marker is an error.

**Detection is unaffected.** `looks_like` and `sensitivity` are computed over sampled values that are never persisted, so a hashed email column still reports `looks_like: email`. That is correct: the shape claim describes the column, not the emitted literals.

**A salt is a precondition of `hash`, not an option.** An unsalted digest of an email is reversible by dictionary attack, so a producer offering `hash` MUST require a configured salt and MUST NOT default one. The salt belongs with credentials, never in a committed project config, and MUST be stable per project or every redacted column churns on every diff.

#### 2.2.10 `null_patterns` — which columns are null together

`null_rate` describes one column at a time. It cannot say that two columns are null on the same rows, and a consumer that assumes they are not will read a filter on one as narrowing by a dimension the other already narrowed — and will read a `LEFT JOIN` whose null-producing side is null on exactly those rows as a wider query than it is.

A file whose data carries nulls records which columns carry them together in a top-level `null_patterns` block:

```yaml
null_patterns:
  coverage: <float [0, 1]>            # ALWAYS; the listed counts over rows_scanned
  coverage_method: measured | bounded # OPTIONAL; whether the census agreed with rows_scanned
  patterns:                           # ALWAYS; may be empty
    - columns: [<column name>, ...]   # ALWAYS; the columns null in these rows, empty for none
      count: <int ≥ 0>                # ALWAYS; rows carrying exactly this combination
```

**A pattern names columns, never positions.** Every entry in `columns` MUST name a column present in this file's `columns` map. A bitstring or an index list would carry its meaning in a column order stated somewhere else, and adding a column to the table would silently change what every artifact written before it meant.

**An entry is an exact combination, not a superset.** A row counted under `columns: [a]` has `a` null and every other column populated, so the entries partition the rows they cover and no row is counted twice.

**The all-populated pattern is listed.** Rows carrying no null at all appear as an entry whose `columns` is empty. Omitting it would leave `coverage` describing a subset of the scanned rows without saying which.

**Each combination appears at most once.** Two entries naming the same columns would count the same rows twice, and the reconciliation rule below would fail with neither count wrong on its own.

**Entries are ordered by `count` descending, ties broken by ascending lexicographic order of the `columns` array** — the same shape §2.2.4 sets for `values`, and for the same reason: two runs over unchanged data MUST emit the same bytes. Which entries survive a producer's cap is a separate question from the order they are published in: at a `count` tie straddling the cap a producer MAY keep either, provided it keeps the same one on every run over unchanged data. §2.2.7 draws the same distinction for `values`.

**`coverage` is the share of the scanned rows the listed entries account for**: the sum of their `count` values over `rows_scanned`, rounded per §2.2.6. A producer that caps how many entries it lists MUST publish the coverage that cap leaves. `1.0` asserts the census is complete, and then the listed counts MUST sum to `rows_scanned` exactly.

**The counts reconcile against each column's own `null_count`.** For any column, the sum of `count` over the entries naming it MUST NOT exceed that column's `null_count`, and MUST equal it exactly where `coverage` is `1.0`. This is the one arithmetic identity in the format that crosses from a table-level object to a per-column one, and it is what catches two figures that came from different reads of the same table.

**`coverage_method`** (enum, OPTIONAL; `measured` | `bounded`): whether an untruncated census agreed with `rows_scanned` (`measured`) or a producer detected the two disagreeing (`bounded`), because the census and the row count it is measured against were not read at the same instant - the same distinction §2.2.4 draws for `values_coverage_method`, applied to this block. Emitted only for a census a producer's own cap did not cut short; a truncated census is short by design, which is a different, already-explained condition `coverage_method` does not cover. A sampled table whose statements each redraw an unmaterialized sample can show the identical symptom from a different cause - a working fix already exists for that one (a materialized draw removes it entirely, reading `measured`) - and `coverage_method` states the symptom it observed, not which of the two causes produced it.

**Absence is a claim about the data, not about the producer.** The block MUST be omitted when no column in the file carries a null, and MUST be present when any does. Both cases are checkable against the `null_count` of every column in the same file, so an absent block never leaves a reader deciding between "no nulls" and "not measured".

**A pattern is a measurement, not a constraint.** That `b` was null on every scanned row where `a` was null is an observation over the rows that were read, not a rule the database enforces, and a consumer MUST NOT treat it as one — the same terms §2.3.8 sets for an inferred edge. Under a `scope` block it says less still: a combination occurring on few rows may not have been drawn at all, so the absence of a pattern is not evidence that the combination does not occur.

**Redaction does not interact.** A pattern discloses which columns were null together and on how many rows, never a cell value, so §2.2.9's markers neither apply to it nor alter it.

#### 2.2.11 `physical_layout` — the declared clustering or partitioning key

A row count and per-column cardinality say nothing about how a table is physically laid out, and on a warehouse that clusters or partitions, layout is the difference between a pruned scan and a full one. That fact already exists in `ddl.sql`, recoverable only by parsing a dialect's own clustering or partitioning syntax; `physical_layout` states it as structured data instead.

```yaml
physical_layout:
  mechanism: cluster | partition      # ALWAYS; named honestly per adapter
  keys:                               # ALWAYS; ordered, may not be empty
    - expression: <string>            # ALWAYS; the declared expression, verbatim
      column: <string>                # OPTIONAL; the base column, when the expression resolves to one
```

**`mechanism` names the mechanism, not a judgment.** `cluster` for Snowflake's clustering key, `partition` for Postgres declarative partitioning and MySQL `PARTITION BY` - both classes of table say the same thing to a consumer sizing a query (these columns prune, the rest do not), so one field carries both rather than forcing a consumer to know which dialects use which word.

**`keys` is ordered, because the order is not decoration.** A multi-column key prunes far more on its first component than its last (`cluster by (vault_id, reading_id)` prunes almost entirely on `vault_id`), and the array's own order is that ranking - a producer MUST NOT reorder it.

**`column` is what a predicate matches against; `expression` is what was declared.** `cluster by (logged_at::date)` yields the expression `logged_at::date` and the column `logged_at` - a consumer needs the column to match a `WHERE` clause against, and the expression to know what was actually declared rather than assume. `column` MUST be omitted when the expression resolves to no single column (a function call over more than one column, or one dbprint cannot parse); the expression alone is still recorded. When present, `column` MUST name a column present in this file's `columns` map, on the same precedent §2.2.10 sets for `null_patterns`.

**The two surfaces agree, or the artifact is inconsistent.** Every column named as a `column` in `physical_layout.keys` MUST carry `physical_layout_key: true`, and every column carrying that marker MUST be named there - the table-level list and the per-column flag are two views of one fact, and a producer emitting one without the other has written a self-contradicting file.

**Declared, never measured.** `physical_layout` states what the schema declares, never how well the table is actually clustered at any moment - Snowflake's automatic clustering can leave a declared key poorly maintained, and clustering depth is a measurement that costs a query and goes stale immediately. A producer MUST NOT publish a cost or pruning claim anywhere in this block; naming the key is a schema fact, asserting what it saves is not.

§7.3 sets this absence beside the others a reader has to interpret.

**Absence means not clustered, never not checked.** Every producer MUST answer whether a table declares a physical layout key. An adapter that cannot express the concept for a given engine surface, or a table that genuinely declares none, both emit no `physical_layout` block - the two are indistinguishable from the artifact alone, which is the same absence-has-one-meaning discipline §2.2.10 applies to `null_patterns`.

**`physical_layout_key`** (bool, OPTIONAL, per column): `true` on every column named as a `column` in the table's own `physical_layout.keys`, omitted otherwise. A declared catalog fact, not a detection, so it sits beside `physical_name` rather than under `inferred`. Carries no order - a consumer wanting the pruning priority reads `physical_layout.keys` at the table level; this marker exists so the fact is visible where a reader is already looking, in the per-column statistics.

#### 2.2.12 `grain` — what identifies a row

`inferred.candidate_key` (§4.2) is single-column by construction. A table whose rows are identified by more than one column - `(vault_id, logged_at)`, `(taxon_id, trial_year)` - has no field to say so, and a consumer is left re-deriving the grain from a `GROUP BY` or, worse, assuming a column that merely looks like a key is one.

```yaml
grain:
  keys:                                # ALWAYS; may be empty
    - columns: [<column name>, ...]    # ALWAYS; one or more, in declaration order for a `declared` entry
      detection: declared | measured   # ALWAYS
  search:                              # OPTIONAL; present only when the measured probe ran
    exhausted: <bool>                  # ALWAYS once `search` is present
```

**A conforming producer always emits the block.** Declared-key introspection is catalog metadata and costs nothing, so a producer MUST include `grain` on every table it emits, with `keys` an empty array rather than the block omitted when nothing declared and nothing measured applies - the block itself is the answer "nothing identifies a row here", not its absence.

**Every declared unique key is emitted, at every arity, in declaration order.** A table declaring a single-column PRIMARY KEY still gets a `grain` entry for it, even though `inferred.candidate_key` says the same thing on that column: the two are independent axes - one declared, one measured - and a consumer reading only `grain` gets the full answer without cross-checking `inferred` on every column.

**A `measured` entry is a probe result, never a constraint.** It states that the named columns were jointly unique over the rows read at `profiled_at`, on the same footing §2.2.10 gives a null pattern and §2.3.8 gives an inferred foreign key: an observation, not a guarantee the database enforces. A producer and a consumer MUST NOT call it a key.

**The measured search is bounded, column-pairs only, and arithmetic-pruned before any statement is issued.** A producer MAY search for an undeclared two-column grain among columns carrying no null (`COUNT(DISTINCT a, b)` diverges on nulls across dialects, so the candidate space excludes them rather than special-case each one), restricted to pairs where `cardinality(a) * cardinality(b) >= row_count` - necessary, not sufficient, and free to compute from fields already on disk. A producer MUST cap how many candidate pairs it actually tests; a three-or-more-column grain is out of reach.

**`search.exhausted` distinguishes "nothing found" from "the search gave up".** `true` when the search tested every arithmetic-pruned candidate; `false` when a per-table cap cut the search short before it could. Both are measurements, and neither is the same bytes as `search` being absent entirely, which means the measured search never ran at all - because the file carries `scope`, `row_count` or `rows_scanned` is `0`, or some column already carries `inferred.candidate_key` and a pair search would answer a question a single column already settled. A consumer reading an empty `keys` list MUST check `search` before concluding no grain exists: absent means nobody looked, `exhausted: false` means the look was incomplete, and only `exhausted: true` means the search itself found nothing.

**Never emitted under `scope`, and never on an empty table.** Uniqueness measured over a sample is not uniqueness (§2.2.8) and every combination is trivially unique on a table with no rows - a `measured` entry MUST NOT appear in either case, though `declared` entries are unaffected: they are a schema fact, not a measurement, and hold regardless of how much of the table was read.

§7.3 covers an absent block and §7.4 an empty `keys` list.

**No per-column marker.** Unlike `physical_layout_key`, membership in a `grain` key is not restated on the column - `inferred.candidate_key` already carries the single-column case, and a multi-column one has no single column to mark without implying an ordering or a weight the combination does not have.

#### 2.2.13 `dependencies` — which columns determine which

A `GROUP BY status, status_label` and a `GROUP BY status` return the same rows whenever every `status` value pairs with exactly one `status_label`, and a print with no way to say so leaves a consumer re-deriving the redundancy from the data or, worse, assuming a wider grouping than the table actually has.

```yaml
dependencies:                        # ALWAYS; may be empty
  - determinant: <column name>       # ALWAYS
    dependent: <column name>         # ALWAYS
    strength: <float (0, 1]>         # ALWAYS
```

**A conforming producer always emits the block**, `[]` for nothing found - the same "answered, not skipped" convention §2.2.12 sets for `grain`.

**`strength` is `cardinality(determinant) / cardinality(determinant, dependent)`.** `1.0` when every determinant value pairs with exactly one dependent value (the exact case); each additional distinct pairing under one determinant value lowers it. Both operands are cardinalities already published elsewhere in this file, computed under the same collation (§2.2.2). This is a group-level measure - how many distinct pairings exist - not a row-weighted share of consistent rows; a table where one determinant value carries a thousand exceptions and one where it carries a single exception can report the same strength if the count of distinct wrong pairings matches.

**Only a pair clearing the 95% confidence bar §4.1.3 already sets is worth publishing.** A producer MUST NOT emit an entry below that threshold - two independent columns with any cardinality asymmetry between them will still show some fraction less than the threshold, and publishing every candidate a producer measured would flood the block with noise a consumer cannot act on.

**A determination is a measurement, not a constraint.** It states that the named columns paired this way over the rows read at `profiled_at`, on the same footing §2.2.10 gives a null pattern and §2.3.8 gives an inferred foreign key. A producer and a consumer MUST NOT call it a rule the database enforces.

**Never emitted under `scope`, and never on an empty table.** A dependency measured over a sample is not a dependency (§2.2.8), and every combination is trivially functional on a table with no rows - `dependencies` MUST be empty in both cases. A consumer checking whether the search ran at all reads `scope` and `row_count`, the same signals §2.2.12 points a `grain` reader to; there is no second indicator here.

**Direction is not interchangeable.** `determinant` names the column whose value fixes the other; a pair may hold in one direction, both (a mutual dependency, published as two entries), or neither. Producers MUST NOT publish a pair in the direction cardinality rules out: `cardinality(determinant) >= cardinality(dependent)` is necessary for the direction to be possible at all, since a function's image has no more distinct values than its domain.

**No new privacy surface.** A dependency discloses which columns move together and how strongly, never a literal value; §4.4's markers neither apply to it nor alter it, the same terms §2.2.10 sets for a null pattern.

#### 2.2.14 `sketch` — whether two key sets overlap, without a second scan

`relationships.yaml` states that an edge exists; it cannot state whether the referencing column's values actually fall inside the referenced column's, because that requires reading both tables together, and the two may not even share a print. `sketch` answers a narrower, offline question instead: a fixed-size summary of one column's distinct value set, from which a consumer computes an approximate overlap against another column's summary — same print, a different print, or a different database entirely — with no query against either source.

```yaml
sketch:
  method: kmv_md5_lo64      # ALWAYS; identifies the hash, k and encoding as one bundle
  values: <base64 string>   # ALWAYS; packed sketch payload, see below
```

**What it is.** A k-minimum-values (KMV) sketch: the k smallest values of `hash(v)` over the column's distinct non-null values, `hash` and k fixed by `method`. `kmv_md5_lo64` is `k = 1024`, MD5, low 64 bits of the digest read as an unsigned big-endian integer, no salt and no seed — the hash is unkeyed and deterministic by construction, so two producers hashing the same value always agree. A KMV sketch, unlike HyperLogLog, supports set intersection and union directly from two sketches' own minimums, which is the property this field exists for; a producer MUST NOT substitute a HyperLogLog or any cardinality-only sketch under this field.

**`values` encoding.** The retained hashes, ascending, each packed as 8 bytes big-endian unsigned, concatenated, then base64-encoded (standard alphabet, per RFC 4648 §4). Decoded length is a multiple of 8; dividing by 8 gives the retained count, which is `k` exactly when the column has at least `k` distinct values and the exact distinct count otherwise — a decoded `values` shorter than `k * 8` bytes is not truncated, it is the whole distinct set, and the sketch it produces is exact rather than estimated.

**Canonical encoding.** Two producers must hash the same logical value to the same bytes, or their sketches are incomparable regardless of which database either read from. Before hashing, a value is rendered to a canonical UTF-8 byte string per its SQL type — the byte form MUST NOT vary by adapter, by the column's native type spelling, or by locale:

| Kind | SQL types | Canonical form |
|---|---|---|
| integer | `smallint`, `integer`, `bigint`, `int`, `tinyint`, `mediumint` (any dialect spelling) | Decimal string, no leading zeros, ASCII `-` for negative, no `+` for positive (`42`, `-7`, `0`) |
| exact decimal | `decimal`, `numeric`, `number` | SPEC §2.2.6's positional-decimal rendering, scale-preserving (trailing zeros within the column's own scale are kept: a `numeric(10,2)` value of `4` renders `4.00`) |
| text | `varchar`, `text`, `char`, `character varying`, `character`, `string`, `uuid` | Raw UTF-8 bytes, unmodified. A native `uuid` column is cast to text first, which every supported adapter renders in lowercase dashed form (`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`) by default — the same form §4.1.1's `uuid` pattern matches — so no separate UUID rule exists. Inherits §2.2.2's collation dependence: two engines that disagree on a text column's distinct set (a case- or accent-sensitivity difference) produce different sketches for it whatever this encoding does; the format does not resolve that, only states it |
| boolean | `boolean` | ASCII `true` or `false` |
| temporal | `date`, `time`, `timestamp`, `timestamp with time zone`, `timestamp without time zone`, `time with time zone`, `time without time zone`, `timestamp_ntz`, `timestamp_ltz`, `timestamp_tz`, `datetime`, `year` | ISO 8601: `YYYY-MM-DD` for a date-only value; `YYYY-MM-DDTHH:MM:SS[.ffffff]` for a value with no timezone concept (nothing to normalize, so nothing to mark); `YYYY-MM-DDTHH:MM:SS[.ffffff]Z` for a timezone-aware value, normalized to UTC first. This binds every adapter the same way regardless of that adapter's own `range.min`/`range.max` rendering - §2.2.4 permits one adapter to omit `Z` there even on a timezone-aware type (a display choice), but the sketch cannot: two adapters hashing the same instant to different bytes breaks cross-adapter agreement |

A floating-point type (`real`, `double precision`, `double`, `float`, `money`) has no canonical encoding and MUST NOT carry a sketch — equality-based set membership on a floating-point value is not stable across engines, and a join key of this type is not a case this field is built for. A type outside every row above — `json`/`jsonb`/`variant` and unsupported types among them — also has no canonical encoding and MUST NOT carry a sketch.

**Test vectors** (MD5, low 64 bits, unsigned big-endian — every adapter's hash expression MUST reproduce these):

| Kind | Canonical bytes | Low-64-bits value |
|---|---|---|
| integer | `42` | `15584161582054922406` |
| integer | `-7` | `5585573698858288085` |
| exact decimal | `4.00` | `10071140874863616590` |
| text | `hello` | `13362634815750784402` |
| boolean | `true` | `317521853213362953` |
| temporal | `2026-05-17T22:48:01Z` | `11467405332662396900` |

**Which columns carry one.** A producer MUST emit `sketch` on every column, in a table read unscoped, whose SQL type falls in the table above, is not redacted (see below), and that meets at least one of:

- it is, on either side, a `column`/`target_column` (declared or inferred `refers_to`) or `referencer_column`/`column` (`referenced_by`) entry in that table's own `relationships.yaml`;
- it carries a single-column PRIMARY KEY or a sole single-column UNIQUE constraint;
- its `cardinality` is at most `k` (the method's own retained size — `1024` under `kmv_md5_lo64`), so the sketch it would carry is exact rather than estimated;
- it carries `inferred.candidate_key: true` (§4.2).

Composite keys sketch per member column — there is no joint sketch for a multi-column key, on the same footing §2.3.4 gives every other per-edge measurement; a composite key's own membership in the second bullet above is therefore never satisfied, only a single-column key's. A producer MAY sketch other columns beyond this set; this specification does not require it, and does not require a producer to expose a way to opt into it.

**Redacted columns carry no sketch.** A KMV sketch is a set of hashes of real cell values; an unsalted one is a dictionary-attackable enumeration of a column someone deliberately withheld, and a salted one is incomparable across projects — the property this whole field exists for. Neither is worth shipping, so `sketch` is absent from a `redacted` column under every primitive, not only `drop`.

**A sketch of a scoped column is not shipped either.** `scope` (§2.2.8) narrows a table's read to less than the whole; a sketch built from a re-drawn narrowing is not reproducibly the same rows a second `generate` would draw, and §2.2.7's determinism expectation does not hold for it the way it does for a full scan. A producer MUST NOT emit `sketch` for a column whose file carries a top-level `scope` block: the absence and its cause are stated, rather than a value that cannot be reproduced.

**Determinism.** Two runs over unchanged data MUST emit byte-identical `sketch.values` — the hash is unkeyed, so nothing about the run itself can move it, and the k smallest values of a fixed set are themselves fixed.

**The error bound is a function of what is actually comparable, not a flat percentage.** Two sketches agree only below the tighter of their two retained thresholds — the count of hashes genuinely answerable is that intersection's size, not `k`, and it varies per edge. A truncated sketch's estimate carries roughly `1/sqrt(answerable count)`; a published overlap is not a data-quality signal until it is read against that edge's own margin, and a small answerable count widens the margin sharply. Only an edge where **both** sketches are exhaustive needs no such margin — neither side truncated, so the shared hashes are the whole comparable set, not a sample of it. An exhaustive sketch measured against a truncated one still measures over a sample: the truncated side's own retained threshold, not the exhaustive side's completeness, decides how much of it was comparable, and the same margin applies to that measurement as to any other truncated one. `observed.answerable_count` (§2.3.10) publishes that count on every sketch-measured edge, so a consumer computes the margin from the print alone rather than reading it as a stated bound.

**Membership is answerable per sketch, not per field.** A sketch whose decoded `values` length is below `k` is exhaustive, so hashing a candidate value's canonical bytes and testing it against the retained set answers membership exactly — absence is proof, presence is certain up to a 64-bit collision. A decoded length of `k` or more is truncated, the same test the encoding rule above already draws that line with, and membership for a value the sketch does not itself hold is not answerable at all: only a value whose hash falls below the retained threshold is checkable from the sketch. A consumer wanting to test a value against a truncated sketch needs a Bloom filter, which this field is not. The candidate value is canonicalized per this section's encoding and matched on the same collation terms §2.2.2 gives the column — a case- or accent-folded miss reads as a real absence, not evidence either way. Membership is as of `profiled_at`, the same staleness every other statistic in this file carries; it is not a live check against the column.

**Not a privacy leak on its own terms, but not nothing either.** §4.4's sensitivity detection and its redaction requirement above are the boundary; nothing else in this section changes what §4.4 already requires.

#### 2.2.15 `catalog_only` — no query was issued

Every field above this line assumes a producer queried the object it describes. `row_count` assumes a `COUNT(*)` or a system-table estimate; every per-column statistic assumes a read of the rows themselves. Neither exists for an object a producer describes from catalog metadata alone — introspecting a view's declared output columns without reading through it, for one such case.

```yaml
catalog_only: true       # OPTIONAL; present and true only when no query was issued
```

**Presence states the fact; there is no false value.** Like `scope` (§2.2.8), the field exists to be present or absent, not to hold a boolean either way — a producer MUST NOT emit `catalog_only: false`. Absence is the ordinary case: a query was issued, exactly as every other field in this section already assumes.

**Licenses the absence of `row_count` and `row_count_method`.** Both are REQUIRED unless `catalog_only` is present, in which case a producer MUST NOT emit either (§2.2.1) — an object nothing was queried for has no count to report, exact or estimated.

**Also licenses the absence of `physical_layout` and `dependencies`.** Both are otherwise MUST-emit fields (§2.2.11, §2.2.13). `dependencies` requires a query to measure and MUST NOT be emitted under the marker, the same rule `row_count` follows. `physical_layout` is a declared schema fact rather than a measurement, so a producer MAY still emit it here from catalog metadata alone (§2.2.15's per-column allowance already treats `physical_layout_key` the same way) — only its absence needs the licence, never its presence.

**A column carries only what its catalog already knew.** `sql_type`, `nullable`, `classification`, `physical_name`, `collation` and `physical_layout_key` — schema and DDL facts, not measurements — are the only fields a column may carry while the file's `catalog_only` is present. Every other per-column field states something read from the object's rows; a producer MUST NOT emit any of them under the marker, `classification` itself still following the ordinary §3.2 priority order on every input available without a query — `sql_type` and a foreign key, declared or naming-inferred, both catalog-derived (§3.3).

**Not a redaction and not a failure.** `catalog_only` states that no query was issued, never that one was attempted and came back withheld or errored — a masked value and an unattempted one are different gaps, and this marker closes only the second.

### 2.3 `relationships.yaml`

Two-section relationship graph: `refers_to` (outgoing FKs from this table) and `referenced_by` (incoming FKs from other tables). Both sections list foreign keys, each carrying its own `detection`: a key **declared** in the catalog, or one **inferred** by the producer from column naming.

Inference exists because a warehouse routinely declares none — Snowflake does not enforce foreign keys and plenty of analytics schemas skip them — so a print of one would otherwise carry an empty graph. An inferred edge is a producer's claim about the schema, not a constraint the database will honour, and a consumer MUST NOT treat it as one.

**What an inferred edge licenses.** A consumer MAY use it as a join candidate when writing a query — naming, declared-uniqueness and type evidence together are the best signal available in a schema that declares nothing — but MUST NOT treat the join as cardinality-guaranteed the way a declared FK is, and SHOULD prefer a declared edge over an inferred one wherever both describe the same relationship. This is the normative floor for a graph where every edge is inferred, which §2.3.8's eligibility rule makes common: a warehouse-wide absence of declared keys leaves nothing for a consumer to prefer, and the inferred graph is what there is to query against. A human MAY reject a specific inferred edge, readable from this table's own directory — see `relationships.annotations.yaml` (§2.7.2).

The reference JSON Schema SHALL be at `spec/v1/relationships.schema.json`.

#### 2.3.1 Top-level fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `format_version` | int | ALWAYS | `1` for v1 artifacts |
| `table` | string | ALWAYS | FQN of this table |
| `profiled_at` | string | ALWAYS | UTC ISO 8601 with `Z` suffix (same convention as `statistics.yaml`) |
| `eligible_target` | bool | OPTIONAL | Whether an inferred edge could ever point at this object, per §2.3.8's eligibility rule. Emitted with both `true` and `false`; absent only when a producer never evaluated it (`infer_relationships: false`) — see §2.3.7 |
| `refers_to` | array | ALWAYS | Outgoing FKs from this table. Explicit `[]` when empty; never omitted. |
| `referenced_by` | array | ALWAYS | Incoming FKs to this table. Explicit `[]` when empty; never omitted. |

#### 2.3.2 `refers_to` entry schema (outgoing FK: this → other)

| Field | Type | Required | Notes |
|---|---|---|---|
| `column` | array of string | R | This table's column(s) participating in the FK; always array (length 1 for single-column FKs) |
| `target_table` | string | R | FQN of the referenced table |
| `target_column` | array of string | R | Referenced column(s); same length as `column` |
| `on_delete` | enum | R (declared only) | `NO ACTION` \| `CASCADE` \| `SET NULL` \| `SET DEFAULT` \| `RESTRICT`. An inferred edge declared nothing to report here — see §2.3.8 |
| `on_update` | enum | R (declared only) | same enum |
| `detection` | enum | R | `declared` — read from the catalog \| `inferred` — derived by the producer, see §2.3.8 |
| `constraint_name` | string | O | Adapter-native; preserved in adapter case (Snowflake UPPERCASE typical, Postgres lowercase typical, MySQL adapter-dependent) |
| `observed` | object | O | Measured cost of joining across this edge — see §2.3.10 |

#### 2.3.3 `referenced_by` entry schema (incoming FK: other → this)

Mirror of `refers_to` with reversed perspective:

| Field | Type | Required | Notes |
|---|---|---|---|
| `column` | array of string | R | This table's column(s) being referenced (typically PK) |
| `referencer_table` | string | R | FQN of the table that holds the FK |
| `referencer_column` | array of string | R | Referencing column(s) on the referencer table; same length as `column` |
| `on_delete` | enum | R (declared only) | From the referencer's FK constraint; absent when the referencer's edge is inferred |
| `on_update` | enum | R (declared only) | |
| `detection` | enum | R | |
| `constraint_name` | string | O | The referencer's FK constraint name |
| `observed` | object | O | Same block as `refers_to`'s, from the referencer's side — see §2.3.10 |

#### 2.3.4 Always-array convention for column lists

`column`, `target_column`, and `referencer_column` are ALWAYS arrays of strings:
- Single-column FK: `column: [user_id]`
- Composite FK: `column: [user_id, org_id]`

The corresponding pair (`column` ↔ `target_column` in `refers_to`; `column` ↔ `referencer_column` in `referenced_by`) MUST have the same length. Position `i` in one corresponds to position `i` in the other.

Composite FKs MUST be emitted as a SINGLE entry — not split into multiple per-column entries.

#### 2.3.5 NOT modeled

The following Postgres-specific FK attributes are NOT captured:

- `DEFERRABLE` / `INITIALLY DEFERRED`
- `MATCH FULL` / `MATCH PARTIAL` / `MATCH SIMPLE`

Producers MUST NOT emit fields for these; they don't affect AI SQL-writing utility at this maturity level and are reserved for potential future additions.

#### 2.3.6 Scope limits of `referenced_by`

`referenced_by` is **exhaustive only within the print's scope**. The producer's two-pass algorithm (extract all FKs from all profiled tables → build reverse map) cannot discover external tables (outside the include selectors) that reference this table.

If a table outside the include selectors references this table, that incoming FK is NOT captured in `referenced_by`. Consumers SHOULD be aware of this limit when reasoning about a table's full incoming FK graph.

§7.3 records this as an absence with no marker of its own; the manifest's `selectors` (§2.5) is what names the objects left out.

#### 2.3.7 Edge cases

| Case | Resolution |
|---|---|
| **No FKs at all, eligible target** | `eligible_target: true`, `refers_to: []`, `referenced_by: []`. File still emitted. A measurement: the producer evaluated this object's eligibility and nothing references it. |
| **Ineligible target** | `eligible_target: false`, `referenced_by: []`. The empty list is not a measurement here — no single-column PRIMARY KEY or sole single-column UNIQUE means no inferred edge could ever resolve to this object (§2.3.8), so nothing was left to find. Distinct from the row above: same `referenced_by: []`, different `eligible_target`. |
| **Eligibility not evaluated** | `eligible_target` absent, `referenced_by: []`. Only when the producer never ran the declared-keys pre-pass (`infer_relationships: false`) — a fourth case, collapsed into neither of the two above. |
| **Self-referential FK** (e.g., `employees.manager_id → employees.id`) | Normal emit. `target_table` equals the top-level `table`. Both `refers_to` and `referenced_by` get an entry. |
| **External FK target** (target not in print scope) | Emitted in `refers_to` normally. Consumer checks the manifest for whether `target_table` is present; absent means external. No special flag — schema stays clean. |
| **Composite FK** (`FOREIGN KEY (a, b) REFERENCES other(x, y)`) | Single entry with `column: [a, b]` and `target_column: [x, y]`. |
| **Plain views** | `relationships.yaml` MAY be omitted entirely (per §1.4). If emitted, both sections may be empty arrays. A view MAY originate an inferred foreign key (§2.3.8): naming evidence on a view's column is the same evidence as on a table's, and the target's `referenced_by` names the view as the `referencer_table`, so a consumer can always tell a virtual referencer from a real one. A view is never the target of one, so its own `eligible_target` is always `false` when evaluated. Materialized views are the same on both counts. |
| **UNIQUE constraints** | Not FKs; do NOT appear in `relationships.yaml`. The DDL file captures them. `statistics.yaml`'s structured analog is `grain` (§2.2.12) for a declared key at any arity, and `inferred.candidate_key` for a single column's measured uniqueness. |

The first three rows are one encoding read three ways; §7.4 sets them beside the other empty collections the format emits, and §7.3 covers the incoming edge §2.3.6 leaves out of scope.

#### 2.3.8 Inferred foreign keys

A producer MAY infer a foreign key that the catalog does not declare, and MUST mark it `detection: inferred`.

Inference is permitted only on evidence the producer can state. A conforming producer MUST require all of:

- the column is named `<name>_id`, where `<name>` **or its regular plural** is the object name of an **eligible base table in the print's scope**, matched against the real table list. **Eligible** means the object can supply the declared-unique column the next bullet's selection rule requires. A view and a materialized view never can, and neither can a base table that declares no single-column PRIMARY KEY and no sole single-column UNIQUE constraint — the type check and the key check are the same test, checked two ways. A producer MUST NOT resolve a stem to an ineligible object. The exclusion is total rather than a preference — an ineligible object MUST NOT consume a name that an eligible one would otherwise answer to, nor make a stem ambiguous that would otherwise resolve. **Creating a view or a materialized view MUST NOT change any table's inferred edges**, since neither is ever eligible; a base table's own declared keys are a schema statement, and a change to them MAY retarget an edge that resolves outside its namespace, since an inference rule built on declared keys is meant to follow what they say. The exact stem is tried first, so a schema holding both `person` and `persons` resolves `person_id` to the one the column names. Only the mechanical English plural forms are permitted (`+s`, `+es` after a sibilant, `y` to `ies`); a producer MUST NOT stem, singularize, or consult a dictionary of irregulars, so `person_id` does not reach `people`;
- the candidate target column is **declared unique** on that table, by PRIMARY KEY or UNIQUE constraint. Measured uniqueness MUST NOT be substituted: it is a property of the data at one moment, and using it would make the same schema infer differently depending on when it was profiled;
- the two column types are compatible;
- no declared foreign key already covers the column.

**Eligibility is published, not left for a consumer to re-derive.** A producer that evaluates the naming-inference pre-pass for a table MUST record the result as the top-level `eligible_target` field (§2.3.1): `true` when the object could supply the declared-unique column above, `false` when it never can. Both values are measurements and MUST be emitted whichever the answer is; the field is absent only when the pre-pass did not run at all. Without this field, an object's own `referenced_by: []` is ambiguous between "nothing references it" and "nothing could" — the same bytes for two different facts about the schema.

**Which declared-unique column the edge targets.** A table routinely declares more than one, so the acceptability test above does not by itself name a target. A conforming producer MUST select, in this order:

1. the single-column PRIMARY KEY, where the table declares one;
2. otherwise the sole column declared unique by a single-column UNIQUE constraint, where exactly one such column exists;
3. otherwise nothing — several qualifying columns and no primary key is an ambiguity, and MUST infer no edge, exactly as an ambiguous stem does.

Two consequences worth stating. A column carrying both a PRIMARY KEY and a UNIQUE constraint is one column reported twice, never an ambiguity. And a composite key never participates: it cannot be the target of a single-column edge, so a table whose only PRIMARY KEY spans several columns has no single-column primary key and falls to rule 2 rather than to "no primary key exists, therefore any unique column will do".

The order is normative because the alternative is producers disagreeing about a schema neither of them is reading wrongly. `users(user_id PRIMARY KEY, email UNIQUE)` — a namespaced surrogate key beside a natural one, which is the commonest table shape in the wild — has two qualifying columns, and a producer left to choose emits the edge, the other edge, or none at all.

An inferred edge carries no `on_delete` or `on_update` semantics, because none were declared; producers SHOULD omit both fields rather than emit `NO ACTION` as filler — a value of that shape reads identically to a real referential action a declared edge reports (§2.3.2/§2.3.3). It carries no `constraint_name`.

Composite keys MUST NOT be inferred — naming evidence for a multi-column key is too weak. Self-references are permitted, and the permission is between two **columns**: `employees.manager_id` targeting `employees.id` is an ordinary edge that happens to stay inside one table. An edge whose target table and target column are both the source column's own is a different thing and MUST NOT be inferred. It asserts nothing a schema can express, and it is what a table whose primary key is named after itself — `users(user_id uuid PRIMARY KEY)` — satisfies every requirement above for: the stem resolves, the target is declared unique, the types match, and no declared foreign key covers the column. Producers would otherwise disagree about every table following that convention while both conformed, which is the disagreement this section exists to prevent. A stem resolving inside the source table's own namespace takes that table where that table is eligible; otherwise resolution proceeds as though the local name were not there. A stem that resolves only outside the source's namespace, in more than one namespace among eligible candidates, is ambiguous and MUST infer nothing, because an edge pointing at the wrong schema is worse than no edge.

An inferred edge appears in the target's `referenced_by` exactly as a declared one does, so §2.3.6's reciprocity holds for both.

**What this deliberately misses.** Only regular plurals are matched, so an irregular one is out of reach: `person_id` reaches `person` or `persons`, never `people`. The boundary is drawn at what a reader can apply by eye — a producer that stemmed or carried an irregular dictionary would infer edges this specification cannot describe, and two conforming producers would disagree about the same schema.

#### 2.3.9 Path-valued relationship endpoints

`column` and `target_column` name whole columns. A warehouse that stores a join key inside a semi-structured column (Snowflake `VARIANT`, Postgres/MySQL JSON) has no column to name for one side of the edge — `refers_to`'s ordinary shape cannot express it, and the format's encoding for "no representable edge" is the same bytes as "no FKs at all" (§2.3.7).

**The structured endpoint.** A `refers_to` entry MAY carry `path`, a sibling of `column`, and `target_path`, a sibling of `target_column` — each an ordered array of string keys naming a path inside the named column's semi-structured value:

```yaml
refers_to:
- column: [customer]
  path: [id]
  target_table: analytics.public.customer
  target_column: [id]
  detection: declared
```

Both are OPTIONAL, and each is legal only where its partner array (`column` / `target_column`) names exactly one column — a path endpoint on a composite key is not representable and MUST be rejected. Neither field changes what `column` / `target_column` mean; they narrow further, into the value the named column holds.

**No wire-level rendering is defined.** The three engines spell a path differently (`col:key` Snowflake, `col->>'key'` Postgres, `col->"$.key"` MySQL); publishing any one of them as the wire form would read as endorsing that vendor and force an escaping rule for a key containing the delimiter. A structured endpoint has nothing to parse. A consumer building a predicate from `{column: customer, path: [id]}` constructs the native form for its own target engine; the three constructions below are informative, not the artifact's wire format:

| Engine | Construction |
|---|---|
| Snowflake | `customer:id` |
| PostgreSQL | `customer->>'id'` |
| MySQL | `customer->"$.id"` |

**No producer inference.** A conforming producer emits no path-valued endpoint on its own; the vocabulary exists so a human can state one through `relationships.annotations.yaml` (§2.7.2), where an entry naming a path and matching no producer-emitted edge is the human-authored addition that section's layering rule describes.

**Scalar paths only.** The array case (`line_items[*]:id`, one-to-many) is not representable under this shape either — `refers_to` carries one target per entry, and a one-to-many relationship is a different shape, not a longer path.

Ordinary column-to-column edges are unaffected: `path` and `target_path` are absent, and nothing about their absence changes.

#### 2.3.10 `observed` (per-edge measured cost)

A `refers_to` or `referenced_by` entry MAY carry `observed`: what joining across this edge costs,
computed from the two endpoints' own `statistics.yaml` — no additional query. The referencing
("child") side always supplies the fanout numerator and the referenced ("parent") side always
supplies the coverage denominator, regardless of which of the two files states the edge — a
`referenced_by` entry's `observed` describes the same measurement its mirror `refers_to` entry
does, from the referencer's own column.

| Field | Type | Required | Notes |
|---|---|---|---|
| `fanout_avg` | number | R | Child `row_count` / child column's `cardinality`, rounded per §2.2.6 — average rows per distinct key on the referencing side |
| `fanout_max` | number | O | The referencing column's own `values[0].count` (§2.2.4 orders by count descending) — the true worst-case group size, not the mean. Absent wherever `values` itself is (§7.2) |
| `target_coverage` | number | R | The fraction of the parent's distinct values this edge actually reaches. Measured from both endpoints' `sketch` (§2.2.14) when both carry one and the measurement has evidence to report; cardinality-derived (child `cardinality` / parent `cardinality`) otherwise — the same field, silently upgraded to the sharper number when the sketches exist to support it, never a second field carrying the other formula |
| `containment` | number | O | The fraction of the *child's* distinct values found in the parent's set, measured from both endpoints' `sketch` (§2.2.14) — the question `target_coverage` cannot answer, since a small `target_coverage` and a `containment` near 1 both hold whenever a few child values reach a much larger parent. Present only when both endpoints carry a `sketch` and the measurement has evidence to report; no cardinality-derived fallback exists for it |
| `answerable_count` | integer | O | The count of the child's own retained hashes below the shared threshold both sketches can speak to (§2.2.14) — the denominator §2.2.14's `1/sqrt(answerable count)` margin is sized against, so a consumer computes that margin from the print alone. Present exactly where `containment` is; absent under the same fallback |
| `coherent` | bool | O | `false` when the child's cardinality exceeds the parent's — arithmetically impossible for a real containment. Present only when both sides measured `cardinality_method: exact` (§2.2.6); a scoped or sampled comparison cannot support the claim either way, so the field is omitted rather than guessed |
| `scope_compatible` | bool | R | `false` when the two endpoints cannot be compared on equal terms — one scoped and the other not, or scoped at rates that are not known to match. Every other field in this table is absent whenever this is `false`; no ratio is ever published across a mismatched pair |

**Absence of the whole block.** A composite edge (`column`/`target_column` longer than one) and an
edge where either endpoint carries no `cardinality` — a plain view's catalog-only file (§2.2.15),
or an object this run could not measure one for — carry no `observed` block at all; see §7.3.

**Sketch-measured fields and their error.** `containment` and a sketch-measured `target_coverage`
are derived from the two endpoints' `sketch` (§2.2.14), and which computation applies depends on
whether the child sketch is exhaustive.

An exhaustive child sketch (§2.2.14's decoded-length case) needs no scaling: `containment` is the
match rate over the child's own *answerable subset* — the hashes falling below the parent's own
retained threshold, the only region either sketch can speak to — measured directly against the
parent's set. `target_coverage` then derives from that same ratio applied to the child's own
`cardinality`. When the answerable subset is empty (every child hash sits above the parent's
threshold), neither field is published from the sketches at all — `target_coverage` falls back to
its cardinality-derived form, and `containment` is absent, the same fallback a missing sketch
already causes.

**Exactness depends on the parent too, not only the child.** When the parent's sketch is also
exhaustive, the answerable subset is the child's whole distinct set, and the match rate against
it is exact — no error bound to state. When the parent's sketch is truncated, its own retained
threshold covers only part of the hash space, so the answerable subset is a hash-uniform sample
of the child rather than its whole set: the published value is the same measurement, but it
carries §2.2.14's margin over that subset's own size, on the same footing as any other truncated
comparison. `observed.answerable_count` publishes that subset's size, so a consumer computes the
margin (§2.2.14) from the print alone rather than reading only that the value is not exact.

A truncated child sketch instead goes through the standard bottom-k intersection estimator: the
smaller of the two sketches' own retained thresholds bounds the hash range both can speak to, the
observed match rate inside that range is scaled to the full hash space, and the result is divided
by each side's own published `cardinality` (measured exactly, never re-estimated from the sketch).
This estimate's error is roughly `1/sqrt(answerable count)` — the count of hashes genuinely below
the shared threshold, which varies per edge and shrinks toward a much wider margin as that count
falls, never the flat percentage a fixed `k` alone would suggest (§2.2.14). A value near 1 states
that the sketches found no evidence against full containment or coverage within that margin, not a
guarantee finer than the margin allows; a value of exactly 0 or 1 from a truncated sketch is still
an estimate, on the same footing as an exhaustive child's own value against a truncated parent —
both are `1.0`/`0.0` and neither is exact. Only both endpoints exhaustive produces the exact form.
Neither field is ever computed from only one endpoint's sketch — the same fallback as an empty
answerable subset applies.

**Not a cost claim.** `observed` states row-count ratios measured on the print; it never predicts
query latency or execution-plan cost, the same boundary §2.2.11 draws for a clustering key.

### 2.4 `description.md`

Free-form Markdown narrative authored by humans. The format imposes no structure on this file — it's user content. Producers MUST NOT write to `description.md`; once a user creates one, it's preserved across regenerations.

This file is NOT versioned with a `format_version` header — it's Markdown.

**Precedence against the measured layer.** On any question `statistics.yaml` answers, the measured layer wins — it describes the run that produced it, current as of that run, where `description.md` may not be. `description.md` is authoritative only on what a statistic cannot express: table grain, units, derivation, deliberate exclusions. This is a statement of scope, not of blame — prose written correctly can still describe a schema a later run changed underneath it, and the rule does not ask a consumer to distrust it on that account, only to prefer the statistic where the two disagree. A checkable claim belongs in `statistics.annotations.yaml` (§2.7.1) rather than here, where a consumer — human or automated — can verify it against the statistic it describes; nothing checks the prose in this file, so a claim placed here rots silently. Absence of `description.md` carries no meaning beyond "no narrative was written."

### 2.5 `manifest.yaml`

The index file at the connection root. Source of truth for which tables are present and what artifacts each has.

```yaml
format_version: 1
generated_at: <ISO8601>
connection: <connection_name>
adapter: snowflake | postgres | mysql
dbprint_version: <semver>
statistics_params:                        # the connection's resolved StatisticsConfig defaults
  enumeration_threshold: <int>
  top_n_values: <int>
  top_n_null_patterns: <int>
  looks_like_sample_size: <int>
  percentiles: [<int>, ...]
selectors:                                # include/exclude as applied, config merged with any CLI override
  include: [<glob>, ...]
  exclude: [<glob>, ...]
redaction_rules_configured: <int>         # count of `redact` rules in force; 0 means none configured
default_collation: <string>               # the connection's own name for its default comparison collation
manifest_annotations: manifest.annotations.yaml   # present only if user-authored (§2.7.3)
tables:
  <fqn>:
    type: table | view | matview
    path: <relative_dir>
    artifacts:
      ddl: ddl.sql
      statistics: statistics.yaml
      relationships: relationships.yaml   # may be absent for plain views
      description: description.md         # present only if user-authored
      statistics_annotations: statistics.annotations.yaml       # present only if user-authored
      relationships_annotations: relationships.annotations.yaml # present only if user-authored
    row_count: <int>                      # absent for plain views
    columns: <int>
    profiled_at: <ISO8601>
    max_age_days: <int ≥ 0>               # OPTIONAL; the freshness threshold this table was judged against
    statistics_params:                    # OPTIONAL; only the keys that differ from the connection default
      <key>: <value>
```

`max_age_days` is the threshold the producer resolved for that table on the run that wrote the entry — the number the run itself used to decide whether the table needed re-reading. It is a whole number of days and MUST NOT be negative; `0` states that the table is re-read on every run, and no age can satisfy it. Consumers judging freshness SHOULD read it rather than re-deriving a threshold from their own configuration, which may have changed since the print was written, and which cannot express a threshold that depended on anything but the table's name. It is carried for every object the run resolved a threshold for, views included. An entry omits it where the run resolved none; consumers fall back to their own configuration for those entries alone, since a manifest may mix both.

**Provenance: what produced the print, so it decodes itself.** Four top-level fields, all REQUIRED, carry the parameters that decide what every number underneath them means:

- **`statistics_params`** is the connection's resolved `StatisticsConfig` for this run — the parameters that decide how much of a column's domain a `values` list carries, which columns classify `categorical` versus fall through to a bounded scan, how much evidence a `looks_like` verdict rests on, and which percentile keys exist at all. Per-table rules can override any of these; a table whose resolved parameters differ from the connection default carries its own `statistics_params` block naming only the differing keys, per the same absence-means-default convention `scope` (§2.2.8) already uses. A table with no override carries no block.
- **`selectors`** is the `include`/`exclude` glob set actually applied to this run — config merged with any CLI narrowing (CLI `include` narrows, CLI `exclude` unions). This is the same information `diff.yaml`'s `target.selectors` (§2.6.3) carries for a live comparison; the manifest is where a consumer asks "what was deliberately left out of this print" without needing to know the drift-detection protocol. The two MUST NOT disagree where both are present (§6.3).
- **`redaction_rules_configured`** is the count of `redact` rules in force for this connection. It tells a consumer whether a column's absent `redacted` marker means "no rule matched" (rules exist, none applied here) or "no rules configured" (the connection redacts nothing at all) — a distinction the column itself cannot express. The redaction salt and the rules' own shapes are never recorded here.
- **`default_collation`** is the connection's own name for the collation a string column compares under when it carries no explicit override (§2.2.2, §2.2.4). Every producer resolves and records one, even where the source engine has no session- or database-level concept of a default — the field then states what an unspecified column behaves as.

A manifest missing any of the four does not describe what produced it, and a validator MUST report it (§6.3).

If the manifest disagrees with on-disk files (missing required artifact, orphaned file), consumers SHOULD treat the print as inconsistent.

The reference JSON Schema SHALL be at `spec/v1/manifest.schema.json`.

### 2.6 `diff.yaml`

Structured change-event stream describing what changed between a baseline (typically the committed prints on disk) and a target (typically the live database). Produced by every successful `generate` run; also emitted by `dbprint diff --format json|yaml` as a standalone artifact.

The reference JSON Schema SHALL be at `spec/v1/diff.schema.json`.

#### 2.6.1 Top-level fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `format_version` | int | ALWAYS | `1` for v1 artifacts |
| `generated_at` | string | ALWAYS | UTC ISO 8601 with `Z` suffix — when the diff was computed |
| `connection` | string | ALWAYS | Connection name from `.dbprint.yaml` |
| `adapter` | enum | ALWAYS | `snowflake` \| `postgres` \| `mysql` |
| `baseline` | object | ALWAYS | See §2.6.2 |
| `target` | object | ALWAYS | See §2.6.3 |
| `summary` | object | ALWAYS | See §2.6.4 |
| `changes` | array | ALWAYS | Explicit `[]` when no changes; never omitted |

#### 2.6.2 `baseline` sub-object

The "before" side. Always the committed prints on disk.

```yaml
baseline:
  source: committed_prints            # the only defined value; a future value may add `live_database` for retrospective diffs
  path: prints/<connection_name>/     # relative to project root
  generated_at: <ISO8601 | null>      # null when no prior generate exists (first-ever run)
  dbprint_version: <semver | null>    # null when no prior generate
```

#### 2.6.3 `target` sub-object

The "after" side. Always the live database.

```yaml
target:
  source: live_database               # the only defined value; a future value may add `committed_prints` for snapshot-to-snapshot diffs
  scanned_at: <ISO8601>
  selectors:                          # effective scope after merging config + CLI overrides
    include: [<pattern>, ...]
    exclude: [<pattern>, ...]
  tables_scanned: <int>               # informational; count of tables matched by selectors
```

`selectors` reflects the EFFECTIVE scope after merging `.dbprint.yaml` config with any CLI `--include` / `--exclude` overrides (intersect / union rules per CLI spec).

#### 2.6.4 `summary` sub-object

Every counter naming an event kind is exact — the count of those events in the `changes` array — and none is threshold-filtered. `tables_modified`, `unchanged_tables` and `unevaluated_tables` count objects rather than events, and the last two count objects that produced none:

```yaml
summary:
  tables_added: <int>
  tables_removed: <int>
  tables_modified: <int>              # tables with ≥ 1 column/stat/row-count/rel/index/comment change
  columns_added: <int>
  columns_removed: <int>
  columns_type_changed: <int>
  columns_nullable_changed: <int>
  columns_default_changed: <int>
  statistics_drifted: <int>           # total statistic_changed events
  relationships_changed: <int>        # relationship_added + _removed + _modified
  indexes_changed: <int>              # index_added + _removed + _modified
  comments_changed: <int>
  unchanged_tables: <int>             # objects compared and found equal
  unevaluated_tables: <int>           # objects the diff had no basis to compare
```

**`unchanged_tables` counts a comparison, `unevaluated_tables` counts the absence of one.** An object the diff could not read produces no event, and counting that silence as "unchanged" publishes a definition as though it were a measurement. The populations that reach `unevaluated_tables`:

- a **plain view**, whose `statistics.yaml` carries `catalog_only` (§2.2.15) rather than a measurement, and for which this format defines no DDL comparison, so its whole body can be rewritten with nothing to detect it;
- a **carried-forward object** from a run that did not re-read it, whose current state is its own committed state and therefore equal to itself by construction.

A producer MUST decide this from whether it had a comparable artifact on both sides, never from `type`: a materialized view carries `statistics.yaml` and belongs in `unchanged_tables`. An object that produced at least one event is `tables_modified` whichever population it came from — something was compared, and it moved.

**The four table counters partition `target.tables_scanned`:**

```
tables_modified + unchanged_tables + unevaluated_tables + tables_added == tables_scanned
```

`tables_removed` is outside the identity: a removed object is absent from the target and so was never scanned.

#### 2.6.5 `changes` array — common shape

Each entry has a `kind` discriminator (one of the 18 kinds enumerated in §2.6.6, plus possible future additions). Consumers MUST tolerate unknown kinds. Order within `changes` is producer-defined but SHOULD be stable across runs (grouped by table, then by kind, then deterministic within kind).

#### 2.6.6 Per-kind field schemas

##### `table_added`
```yaml
- kind: table_added
  table: <FQN>
  type: table | view | matview
```

##### `table_removed`
```yaml
- kind: table_removed
  table: <FQN>
```

##### `column_added`
```yaml
- kind: column_added
  table: <FQN>
  column: <name>
  sql_type: <native_type>
  nullable: <bool>
```

##### `column_removed`
```yaml
- kind: column_removed
  table: <FQN>
  column: <name>
```

##### `column_type_changed`
```yaml
- kind: column_type_changed
  table: <FQN>
  column: <name>
  before: <native_type>
  after: <native_type>
```

##### `column_nullable_changed`
```yaml
- kind: column_nullable_changed
  table: <FQN>
  column: <name>
  before: <bool>
  after: <bool>
```

##### `column_default_changed`
```yaml
- kind: column_default_changed
  table: <FQN>
  column: <name>
  before: <scalar | null>             # null = no default on baseline
  after: <scalar | null>              # null = no default on target
```

##### `statistic_changed`
```yaml
- kind: statistic_changed
  table: <FQN>
  column: <name>
  stat: <dot_path>                    # e.g., "cardinality", "percentiles.p99", "distribution"
  before: <value>                     # type matches stat's data type
  after: <value>
  delta: <numeric>                    # OMITTED for non-numeric stats (distribution, classification, values)
  delta_pct: <float>                  # OMITTED when before == 0 (division by zero) or stat is non-numeric
```

Valid `stat` dot-paths: any field path under a column's stats per §2.2. For `values`, `before` and `after` carry the **full list** (per-value granularity not implemented).

##### `table_row_count_changed`
```yaml
- kind: table_row_count_changed
  table: <FQN>
  before: <int>
  after: <int>
  delta: <int>                        # after - before
  before_method: exact | approximate
  after_method: exact | approximate
```

Table-grain, not column-grain — meaningful even when every column statistic in the same file is sample-scale (§2.2.8 `scope`), since `row_count` describes the whole table regardless of `scope`. Fires whenever `row_count` differs between baseline and target; a `before_method`/`after_method` mismatch alone does not emit an event, since the number may be unchanged while what it means shifts. A consumer reading `before_method: approximate, after_method: approximate` MUST NOT treat `delta` as measured growth — it is the difference of two estimates.

##### `grain_changed`
```yaml
- kind: grain_changed
  table: <FQN>
  before: { keys: [ { columns: [<col>, ...], detection: declared|measured }, ... ], search: { exhausted: <bool> } }  # search OPTIONAL
  after:  { keys: [...], search: {...} }                                                                            # same shape
```

Fires whenever `grain.keys` (as a set of `(columns, detection)` pairs) or `grain.search.exhausted` differs between baseline and target (§2.2.12). `before`/`after` carry the block's full shape, never a computed add/remove delta: a column combination whose `detection` changes (`declared` <-> `measured`) is neither added nor removed, and a delta would misreport it as a spurious removal paired with a spurious addition. `search` is OMITTED on a side exactly where §2.2.12 omits it from `statistics.yaml` - the measured probe never ran there. Table-grain, on the same footing as `table_row_count_changed`: a change here means the columns identifying a row moved, not that a value did.

##### `physical_layout_changed`
```yaml
- kind: physical_layout_changed
  table: <FQN>
  before: { mechanism: cluster|partition, keys: [ { expression: <string>, column: <string> }, ... ] } | null
  after:  { ... } | null
```

Fires whenever `physical_layout` differs between baseline and target (§2.2.11). `null` on a side states that side confirmed no clustering/partitioning key, never "not checked" - the same absence-has-one-meaning discipline §2.2.11 itself sets, and the reason an artifact predating this field compares as `null` too rather than being excluded from the comparison the way an artifact predating `grain` is: §2.2.11 already treats every absent block as one fact regardless of cause, so a baseline this old reads as a genuine gain the day the table is first clustered, not as an unevaluated pair.

##### `relationship_added`
```yaml
- kind: relationship_added
  source_table: <FQN>
  source_column: [<col>, ...]          # always array per §2.3
  target_table: <FQN>
  target_column: [<col>, ...]          # same length as source_column
  on_delete: <enum>
  on_update: <enum>
  detection: declared
```

##### `relationship_removed`
```yaml
- kind: relationship_removed
  source_table: <FQN>
  source_column: [<col>, ...]
  target_table: <FQN>
  target_column: [<col>, ...]
```

##### `relationship_modified`
```yaml
- kind: relationship_modified
  source_table: <FQN>
  source_column: [<col>, ...]
  target_table: <FQN>
  target_column: [<col>, ...]
  on_delete: { before: <enum>, after: <enum> }    # OPTIONAL; present only when on_delete changed
  on_update: { before: <enum>, after: <enum> }    # OPTIONAL; present only when on_update changed
```

A `relationship_modified` event MUST carry at least one of `on_delete` or `on_update`. If neither changed, the event MUST NOT be emitted.

##### `index_added`
```yaml
- kind: index_added
  table: <FQN>
  index_name: <string>
  columns: [<col>, ...]                # ordered as declared in the index
  unique: <bool>
  type: <string>                       # adapter-native (btree, hash, gin, gist, brin, spgist, ...)
```

##### `index_removed`
```yaml
- kind: index_removed
  table: <FQN>
  index_name: <string>
```

##### `index_modified`
```yaml
- kind: index_modified
  table: <FQN>
  index_name: <string>
  before: { columns: [...], unique: <bool>, type: <string> }
  after: { columns: [...], unique: <bool>, type: <string> }
```

##### `comment_changed`
```yaml
- kind: comment_changed
  table: <FQN>
  target: table | column
  column: <name>                       # REQUIRED when target=column; OMITTED when target=table
  before: <string | null>              # null = no comment on baseline
  after: <string | null>               # null = no comment on target
```

#### 2.6.7 Index events — secondary indexes only

`index_added`, `index_removed`, and `index_modified` events cover **secondary indexes only** — explicit `CREATE INDEX` statements.

Indexes implicit to `PRIMARY KEY` or `UNIQUE` constraints are out of scope for these events:

- PK changes manifest via `column_*` events (a PK redefinition typically involves columns)
- UNIQUE constraint changes are not separately tracked; the data-derived signal is `inferred.candidate_key` in `statistics.yaml` (§2.2.4), and the structural definition is in the `ddl.sql` artifact

Detection of secondary indexes is best-effort: baseline indexes are parsed from the committed `ddl.sql`, target indexes are queried from INFORMATION_SCHEMA (Postgres: `pg_indexes`; MySQL: `SHOW INDEX`; Snowflake: `SHOW INDEXES` — limited). DDL parsing is brittle; a future normative `indexes.yaml` artifact would make detection reliable.

#### 2.6.8 Scoping rules

Events are emitted only for tables in `target.selectors` scope. Tables outside the selectors are NOT reported as added / removed / modified — they're unknown to this diff.

- For full-scope runs (no CLI `--include` / `--exclude` overrides), `target.selectors` mirrors `.dbprint.yaml`; the diff covers all configured tables.
- For partial runs (e.g., `--include arboretum.fieldwork.*`), only matching tables produce events; unrelated tables are not in this diff (even if their committed prints differ from live).

Consumers reading a partial diff understand: "this is what changed within the scope listed in `target.selectors`; the rest is unknown to this artifact."

#### 2.6.9 Threshold behavior (machine vs human render)

`statistic_changed` events fire for **every** measured drift — including sub-percent changes. The `diff.yaml` artifact contains ALL of them. Machine consumers (JSON / YAML output) always receive the full data.

Human rendering (terminal output without `--format json|yaml`) filters by per-stat thresholds configured in `.dbprint.yaml`:

```yaml
diff:
  stat_change_threshold:
    cardinality_ratio: 0.02
    percentile_pct: 0.05
    values_coverage: 0.05
    default: 0.01
```

The CLI `--threshold FLOAT` flag overrides all per-stat thresholds for one run (human-render only).

`summary` counts (§2.6.4) are NOT threshold-filtered — they always reflect the actual event count.

#### 2.6.10 Edge cases

| Case | Resolution |
|---|---|
| **First-ever generate** (no prior prints) | `baseline.generated_at: null`, `baseline.dbprint_version: null`, `baseline.path` still set. All tables appear as `table_added` events. |
| **`--dry-run`** | No `diff.yaml` written. (CLI spec §generate.) |
| **`--force` on unchanged tables** | Zero `statistic_changed` events for those tables (file rewrite isn't a semantic change). |
| **Empty diff** | `changes: []`, summary all-zero. File still emitted. |
| **Scoped run with `--include`** | Only events for in-scope tables. Out-of-scope tables not reported. |
| **`statistic_changed` with non-numeric stats** (`distribution`, `classification`, `values`) | `delta` and `delta_pct` OMITTED; only `before` and `after`. |
| **`statistic_changed` with `before: 0`** | `delta_pct` OMITTED (division by zero); `delta` still present. |
| **Comment removed** | `comment_changed` with `after: null`. |
| **Comment added** (never existed in baseline) | `comment_changed` with `before: null`. |
| **A baseline predating `grain`** | No `grain_changed` event - absence on either side suppresses the comparison entirely, the same rule every other optional table-level field follows, rather than reporting every declared/measured key as newly added. |
| **A baseline predating `physical_layout`, or a table confirmed unclustered** | Both parse identically to "no layout" (§2.2.11's own absence-has-one-meaning rule); `physical_layout_changed` fires whenever a side's real state differs from the other's, including the transition from either kind of "no layout" to a genuine key. |
| **PK / UNIQUE indexes** | Out of scope for `index_*` events. Changes appear via `column_*` events or DDL drift. |
| **Multi-connection auto run** | Each connection produces its own `prints/<conn>/diff.yaml`. No project-level aggregate. |

### 2.7 Human-authored annotation files

The human layer: per-artifact files a person writes and the producer never touches, layered over the artifact each one annotates.

**Naming.** `<artifact>.annotations.yaml` is the human layer for `<artifact>` — `statistics.annotations.yaml` for `statistics.yaml` (§2.7.1), `relationships.annotations.yaml` for `relationships.yaml` (§2.7.2), and `manifest.annotations.yaml` for `manifest.yaml` (§2.7.3). The suffix makes authorship legible from the name alone: a print root holds both producer-written files and human-written ones, and a reader tells them apart without knowing which section introduced either.

**Layering.** A per-table annotation file carries the schema of the artifact it annotates, with every field OPTIONAL except the ones that address one entry, plus two extras: `note` (free-form Markdown; the format imposes no structure on it) and, where a per-artifact section below defines one, a field letting a human reject what the producer measured. An entry that resolves against the base artifact annotates it. An entry that resolves against nothing is content a human added that the producer did not, and structurally could not, emit — the same file covers both without a second shape. `manifest.annotations.yaml` (§2.7.3) has no addressable entries — a connection has nothing narrower than itself to key against — so it carries `notes` alone rather than this per-entry shape.

Producers MUST NOT write to an annotation file. Once a user creates one, it is preserved across regenerations, on the same terms as `description.md`. Unlike `description.md`, every annotation file IS versioned with a `format_version` header, since its shape is a mapping rather than free-form prose.

**Every annotation file's root is closed.** `additionalProperties: false` at the top level of all three schemas: a key this document does not name is rejected (`schema.type-mismatch`, §6.3), never silently accepted. Silent acceptance would leave a human with no signal that what they wrote reaches no consumer.

**The bound.** An annotation MAY correct an inference and MAY add knowledge a measurement cannot express. It MUST NOT be used to contradict a measurement — the artifact's own arithmetic and catalog reads take precedence over prose written at another moment (§2.4's precedence rule, restated here at file grain). This is why these files are named `.annotations.`, never `.override.`.

**Scope.** The rule covers every human-written KEYED file — a mapping whose entries a producer can resolve against something it emitted. `description.md` is outside it: unstructured prose has no artifact to mirror and no entries a producer could check, so it keeps its own name and its own section (§2.4).

#### 2.7.1 `statistics.annotations.yaml`

Per-table user-authored notes about individual columns, keyed by column name.

```yaml
format_version: 1
columns:
  <column_name>:
    note: <markdown_string>          # OPTIONAL - free-form prose
    claims:                          # OPTIONAL - checkable predicates
      <stat>: <predicate>
    values:                          # OPTIONAL - notes on individual domain members
    - value: <scalar>
      note: <markdown_string>
grain:                               # OPTIONAL - a human-stated key the producer measured none for
  keys:
  - columns: [<column_name>, ...]
    note: <markdown_string>          # OPTIONAL - free-form prose
```

Keys MUST match a column's name as it appears in that table's `statistics.yaml` `columns` map (§2.2) — always lowercase (§2.2.1), not necessarily the form a user typed against the source database. A key naming a column not present there is stale: consumers SHOULD report it rather than fail on it, and it MUST NOT block generation or validation.

**A column's entry carries `note`, `claims`, `values`, or any combination** — an entry with none of them is legal but carries no annotation. `note` is free-form Markdown. `claims` is zero or more `<stat>: <predicate>` pairs in the statistic-assertion grammar [`ASSERTIONS.md`](../../ASSERTIONS.md) §2 specifies, scoped to this column: `<stat>` is any stat that grammar allows asserting on a column, `<predicate>` any form it defines. A claim states something a statistic can check — it MUST NOT be used to record what a statistic cannot express; that belongs in `note` or in `description.md` (§2.4). Consumers evaluate `claims` against the table's own `statistics.yaml`, offline, the same way the assertion DSL evaluates a statistic assertion in offline mode. A claim that contradicts the statistic it names, or that cannot be evaluated at all (an unassertable or unemitted stat, a malformed predicate, a redacted column), is reported per §6.3 — never silently accepted, and never failed as an error: this axis is advisory, per §2.4's precedence rule.

**`values` carries what a domain member means, not what a statistic can measure.** Each entry pairs a `value` — a scalar addressed the same way `statistics.yaml`'s own `values` list addresses one (§2.2.4) — with a `note` explaining it: a sentinel (`-1` meaning "count not recorded at collection, not a real zero"), a reserved code, anything a human knows about that one member of the domain that the count beside it cannot say. A `value` naming a member the column's own **exhaustive** `values` list (`values_coverage` of `1.0`) does not have is stale, reported the same way a stale column key is (§6.3); under a truncated list the value may occur unlisted, so an unmatched note is treated as an addition, not stale. A redacted column (§2.2.9) has no literal a note could name, so `values` there is unassertable rather than checked.

**`grain` states a candidate key by the columns that make it one** — the same identity `statistics.yaml`'s own `grain.keys[]` addresses (§2.2.12), so a human writes the exact columns they mean rather than inventing a second naming scheme. Each entry carries `columns` (REQUIRED, the addressing tuple) and `note` (OPTIONAL). This is additive, never a correction: the producer's own measured or declared keys are unchanged and still rendered, and a human states a key the search never found or a declared one the catalog does not carry a constraint for — the two lists are shown together, the annotated one marked as such. A `columns` entry naming a column the table's `statistics.yaml` does not have is stale, reported the same way a stale `columns` key is (§6.3) — any one unknown column invalidates the whole key, since the tuple addresses a set, not a single field.

This file carries no table-level key, so it never conflicts with `description.md`, which is table-grain narrative. An empty `columns` mapping is legal and carries no annotations.

Plain views MAY carry this file. A view's own `statistics.yaml` (§2.2.15) names every column its catalog read found, so a key naming a column absent from that list is stale the same way it is for a table.

The reference JSON Schema SHALL be at `spec/v1/statistics_annotations.schema.json`.

#### 2.7.2 `relationships.annotations.yaml`

Per-table human channel over `relationships.yaml`, in the **source** table's own directory - the side a consumer joins from.

```yaml
format_version: 1
refers_to:
- column: [taxon_id]
  target_table: seedbank.taxon
  target_column: [taxon_id]
  verdict: rejected                # OPTIONAL - marks a producer-inferred edge wrong
  note: <markdown_string>          # OPTIONAL - free-form prose
  claims:                          # OPTIONAL - checkable predicates
    <stat>: <predicate>
```

An entry addresses an edge by `column` / `target_table` / `target_column` - the same triplet `refers_to` (§2.3.2) carries, and REQUIRED here for the same reason: without it there is nothing to resolve against. Every other field is OPTIONAL, per §2.7's layering rule.

**An entry that resolves against an edge `relationships.yaml` emits annotates it.** `verdict: rejected` states that a human has determined the edge is wrong - typically an `inferred` one the naming rule (§2.3.8) resolved to a column that shares nothing but a name with the source. `note` carries the reasoning. A `verdict` MUST NOT address a `detection: declared` edge: a declared edge is read from the catalog, and an annotation may correct an inference or add knowledge a measurement cannot express, never contradict a measurement (§2.4's precedence rule). A conforming producer preserves this file byte-identical across regenerations and never writes to it, on the same terms as `statistics.annotations.yaml` (§2.7.1).

**An entry that resolves against nothing is a human-authored edge** the producer did not, and structurally could not, emit - §2.7's layering rule covers this file the same as any other. This is the channel a path-valued endpoint (§2.3.9) is authored through: a warehouse storing a join key inside a semi-structured column has no producer-emitted edge to annotate, so the entry is stated here directly, with `path` / `target_path` alongside `column` / `target_column`.

**Rejecting an edge does not remove it.** `refers_to` and `referenced_by` are producer measurements (§2.3); a `verdict` is a consumer-facing correction layered over them; a table's own graph is unchanged. `dbprint context` renders a rejected edge marked, not omitted, so a consumer sees both what the producer inferred and that a human overruled it.

**A `verdict` addressing an edge absent from `relationships.yaml` is stale**: reported at warning severity, the same treatment `statistics.annotations.yaml` staleness gets (§2.7.1). An entry carrying no `verdict` needs no counterpart to resolve against, so a human-authored addition is never stale.

**`claims` checks an edge's own `observed` block (§2.3.10) the same way `statistics.annotations.yaml`'s `claims` checks a column** (§2.7.1) — zero or more `<stat>: <predicate>` pairs in the [`ASSERTIONS.md`](../../ASSERTIONS.md) §2.1 predicate grammar, where `<stat>` is any of `observed.fanout_avg`, `observed.fanout_max`, `observed.target_coverage`, `observed.containment`, `observed.coherent`, `observed.scope_compatible`, `observed.answerable_count` — this document's own edge-claim vocabulary, distinct from ASSERTIONS.md §2.4's column vocabulary, which that document scopes to `.dbprint.yaml` assertions only. Consumers evaluate `claims` against the edge's own `refers_to` entry, offline. A claim naming an address `refers_to` carries no counterpart for at all has nothing to resolve against, the same as any stat absent from a real edge - unassertable, never a stale-edge finding of its own. A claim that contradicts the measurement, or cannot be evaluated (an unassertable stat, a malformed predicate, a composite or scope-incompatible edge carrying no `observed` block), is reported per §6.3 — advisory, per §2.4's precedence rule, exactly as a column claim is.

The reference JSON Schema SHALL be at `spec/v1/relationships_annotations.schema.json`.

#### 2.7.3 `manifest.annotations.yaml`

Connection-grain human notes, at the connection root beside `manifest.yaml` — the home for a fact
true of the whole warehouse rather than one table, which the per-table `description.md` (§2.4)
has no place to carry without repeating it once per table.

```yaml
format_version: 1
notes: <markdown_string>          # OPTIONAL - free-form prose
```

`notes` is the only field. Free-form Markdown, on the same terms `description.md` gives table-grain
narrative (§2.4) — the format imposes no structure on it, and nothing checks it against a
measurement. An empty or absent `notes` key is legal; the file existing at all is what a consumer
checks for, per the presence rule below.

**Optional, and its own presence is the signal.** Unlike `manifest.yaml`, `diff.yaml` and
`reading.md` (§1.2), this file is OPTIONAL — most connections carry no warehouse-wide fact that
does not already fit `description.md`. A producer that finds one at the connection root records it
in `manifest.yaml`'s top-level `manifest_annotations` field (§2.5); absence of that field means no
human authored one, on the same absence-means-unauthored terms `statistics_annotations` (§2.7.1)
and `relationships_annotations` (§2.7.2) already carry at table grain.

**Producers MUST NOT write to it.** Once a user creates one, it is preserved across regenerations
byte-identical, on the same terms `description.md` and every other file in this section already
carry (§2.7's layering rule).

**Distinct from the producer-authored consumer guide.** `reading.md` (§1.2.1) is generated,
producer-written, and overwritten every run; `manifest.annotations.yaml` is human-written and never
touched by a producer. The two sit at the same connection root and the suffix — `.annotations.` on
one, none on the other — is what tells a reader which is which, the same distinction the naming
rule above states for every file in this section.

The reference JSON Schema SHALL be at `spec/v1/manifest_annotations.schema.json`.

---

## 3. Column classifications

Every column has a single `classification` tag — the most useful one-word summary for AI consumers. Producers MUST assign exactly one classification per column.

### 3.1 Defined classifications

| Classification | Defining rule | Indicative fields |
|---|---|---|
| `boolean` | SQL type is explicitly `BOOLEAN` | values |
| `json` | SQL type is JSON / JSONB / VARIANT | sql_type, null_rate |
| `foreign_key_candidate` | Has a foreign key, declared or inferred (§2.3.8) | inferred.fk_candidate, values |
| `categorical` | `cardinality <= enumeration_threshold` (default 50) | values, values_coverage, distribution |
| `temporal` | SQL type is date / time / timestamp AND `cardinality > enumeration_threshold` | range, percentiles, freshness, distribution |
| `numeric` | SQL type is numeric AND `cardinality > enumeration_threshold` | range, percentiles, distribution |
| `text` | SQL type is character (VARCHAR / TEXT / CHAR) **or UUID**, or any other type the producer measured a cardinality for, AND `cardinality > enumeration_threshold` | values, values_coverage, distribution |
| `unsupported` | The producer could not measure a cardinality for the column at all — binary (BLOB/BYTEA), array, composite (RECORD/STRUCT), or any other type a producer's adapter declines to profile | sql_type, null_rate (only) |

**Uniqueness is not a classification.** `inferred.candidate_key` (§4.2) is set whenever `cardinality_ratio` clears its own threshold, independent of which row above a column matches — a unique column is classified by type or foreign-key status like any other, and carries the flag alongside whatever fields that classification requires.

**The boundary between `text` and `unsupported` is representability, not a type-name list.** A type a producer can print, compare and enumerate is summarisable — `INET`, `MACADDR`, `XML`, `INTERVAL`, a native network or interval type no rule above names — and MUST classify by measurement like any other type, falling to `text` when nothing more specific matches. A type that cannot be summarised at all — an opaque binary blob, an array, a composite record — is `unsupported`, and a producer states this by declining to measure a cardinality for it rather than by the engine matching a type name: `cardinality` absent is what the fallthrough at priority 8 below reads. A future type neither this document nor a producer's own adapter names lands on the right side by this rule, without a maintainer having to extend a membership list first.

### 3.2 Priority order (first match wins)

When a column matches multiple defining rules, producers MUST walk the list top-to-bottom and assign the first match:

1. `boolean` — explicit SQL type
2. `json` — explicit SQL type
3. `foreign_key_candidate` — foreign key, declared or inferred
4. `categorical` — low cardinality regardless of SQL type
5. `temporal` — SQL type, high cardinality
6. `numeric` — SQL type, high cardinality
7. `text` — fallback for character types, UUID, and any other type the producer measured
8. `unsupported` — fallback for a type the producer could not measure at all

**Example**: an integer column with values 1–5 has SQL type `INT` (would match `numeric` on type) but `cardinality = 5` (matches `categorical` first at priority 4). Classification is `categorical`. The value distribution gets enumerated explicitly via `values:`.

**Example**: a `VARCHAR(64)` column holding unique slug strings, cardinality above the enumeration threshold, has SQL type character (matches `text` at priority 7). Classification is `text`, and it publishes its value list like any other `text` column. `inferred.candidate_key` is set separately, since `cardinality_ratio = 1.0` clears the SPEC 4.2 threshold — uniqueness did not decide the classification, and does not withhold the fields this one requires.

### 3.3 Edge cases

**All-null columns** (`cardinality = 0`, `null_rate = 1.0`): classification follows the §3.2 priority order. At `cardinality = 0` this resolves to `categorical` (or `foreign_key_candidate` / `boolean` by FK/type), not the type-based `numeric` / `temporal` / `text` branches. The value fields are emitted EMPTY per §2.2.7 (`values: []` + `values_coverage: 1.0`, plus `distribution` where required), not omitted. Range, percentiles, and freshness remain absent. Consumers see `null_rate: 1.0` and read accordingly.

**Single-value columns** (`cardinality = 1`): match `categorical` (1 ≤ enumeration_threshold). `distribution: dominant_value` is implied.

**A unique foreign key** (1:1 relationships): classifies `foreign_key_candidate` (priority 3) regardless of `cardinality_ratio` — SPEC 3.1's defining rule does not exclude a unique column. `inferred.candidate_key` is set alongside it whenever the ratio clears the SPEC 4.2 threshold, so a consumer reads uniqueness from that field, not from the classification.

**Generated / computed columns** (Postgres `GENERATED ALWAYS AS`, Snowflake virtual columns): classified normally per the rules above. dbprint captures generated-ness only via the DDL file (which records the `AS (...)` expression). There is no `generated: true` flag.

**SQL types the producer cannot model** (binary, array, composite): in a queried file, the producer's adapter declines to measure a cardinality for these, and a column left unmeasured for this reason MUST be classified `unsupported` regardless of its type name (§3.1). The producer emits exactly the fields §2.2.3's matrix marks REQUIRED for `unsupported` - `sql_type`, `nullable`, `null_count`, `null_rate` and `classification`, plus `rows_scanned` when a scope block applies (§2.2.8) - and no other statistic. Consumers MUST handle `unsupported` gracefully — treat as opaque.

**A type no rule above names, that the producer DID measure** (a native network, interval or XML type; any vendor type this document has not yet catalogued): classifies `text` per priority 7, never `unsupported` — the fallthrough follows the measurement, not a second membership list neither side of a producer is obligated to keep in step. `inferred.looks_like` and `inferred.sensitivity` reach it exactly as they would any other `text` column.

**A column of a file carrying `catalog_only`** (§2.2.15): `cardinality` is absent for a different reason than either row above - no query was issued for the object at all, not that this type resists summarising. Classification still follows the ordinary §3.2 priority order, from every input a catalog read supplies - `sql_type`, and a foreign key whether declared or naming-inferred (§2.3.8), since inference reads only column names, not the object's rows. The fallthrough differs from a queried file's, though: a binary, array or composite column still reaches `unsupported`, matched before either fallthrough runs, but an otherwise-unmatched type - `inet`, `interval`, `xml`, anything §3.1 does not name - lands on `text` here rather than `unsupported`, since nothing was attempted that could have failed to summarise it. `categorical` cannot occur under the marker; it needs a `cardinality` the marker forbids, so a low-cardinality column seen through a base table and again through a catalog-only view over it classifies differently in each.

### 3.4 Classification reserves

The following are reserved for potential future additions; producers MUST NOT emit them:

- `geographic` (lat/lon, geohashes, country codes)
- `monetary` (amounts with currency context)
- `binary` (BLOB/BYTEA — distinct from `unsupported` once stats can be defined for them)
- `array` (Postgres/Snowflake array columns)
- `composite` (RECORD/STRUCT columns)
- `enum` (explicit ENUM SQL types — distinct from inferred `categorical`)

Consumers MUST tolerate unknown classification values for forward compatibility (see §5.3) — these names are not the only candidates.

---

## 4. Inferred semantics

### 4.1 `looks_like` patterns

Producers MUST run regex/parser-based detection over a sample of distinct non-null values for the classifications §4.1.5 names, assigning each sampled value the first pattern it matches (priority order in §4.1.4) and setting `inferred.looks_like` to a pattern assigned to ≥ 95% of sampled values.

The field is OPTIONAL — a sample where no pattern reaches the threshold carries none, and its absence is not an error. What is required is the detection, not a result; §4.1.2 requires it of every producer that samples the column, without exception for cost or scale.

Every pattern in §4.1.1 is defined over a string. A sampled value that is not already a string — a driver's native `uuid`, decimal, or date type, for example — MUST be coerced to its string form (`str(v)` or the producer's language equivalent) before assignment, applying no locale-dependent formatting and no per-driver special-casing: only the value's own default string rendering is used. This is what makes `uuid` reachable on a database's native UUID column type, `numeric_string` on a native decimal column, and `iso8601_date` / `iso8601_datetime` on a native date or timestamp column classified `categorical` (§3.2) — the one classification §4.1.5 runs detection on that a temporal SQL type can still reach despite `temporal` itself being excluded.

#### 4.1.1 Pattern definitions

| Pattern | Defining rule |
|---|---|
| `uuid` | Case-insensitive regex `^[0-9a-f]{8}-[0-9a-f]{4}-[1-7][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`. Dashed canonical form; versions 1–7; RFC 4122 variant `[89ab]`. Braced (`{...}`), compact (no dashes), and URN (`urn:uuid:...`) forms NOT matched. |
| `email` | Regex `^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$`. Conservative — catches typical emails; rejects garbage. Not a full RFC 5322 validator (no quoted local-parts, no comments, no IP literals). |
| `url` | Regex `^https?://[^\s]+$`. Only `http://` / `https://` schemes matched. Other schemes (`ftp://`, `file://`, custom) NOT matched. |
| `urn` | RFC 8141 shape, case-insensitive: `urn:`, then a 1-31 character namespace identifier (`[A-Za-z0-9][A-Za-z0-9-]*`), then `:`, then a non-empty, whitespace-free namespace-specific string. Ranked above `path`, which would otherwise claim `urn:example:weather/today` - one `/` is all `path` requires. Clears `uuid` by that pattern's own text excluding the URN form (`urn:uuid:...`), and clears `url` because that pattern is anchored on `https?://`. |
| `ip` | A bare IPv4 or IPv6 address, parsed by the producer's standard library address type rather than a hand-written regex. Both families assign the same value, including an IPv4-mapped IPv6 address (`::ffff:192.0.2.1`) - a mixed column of both families reports `ip` at 100%, which two separate values could never clear the threshold at. **NOT matched**: a CIDR block (`10.0.0.0/8`), a `host:port` pair, a comma-separated forwarded-for chain, a bracketed form (`[2001:db8::1]`), a zone-qualified form (`fe80::1%eth0`), and an IPv4 octet carrying a leading zero (`010.1.1.1`) - each is transport decoration or an ambiguous rendering, not a bare address. |
| `mac_address` | Six two-character hex octets joined by one consistent separator, colon or hyphen (`00:1b:63:84:45:e6`, `00-1B-63-84-45-E6`). **NOT matched**: a separatorless run (`001b638445e6`) - claimed by `hex` instead, which is the honest answer since nothing in the value says "address" without the grouping. Ranked above `phone`: a hyphenated all-digit MAC (`00-11-22-33-44-55`) is twelve digits with separators, indistinguishable from a national phone number by that pattern's own arithmetic. |
| `json` | `json.loads(value)` parses successfully AND the top-level result is a `dict` or `list` (not a primitive). Restricting to structured tops filters noise — bare primitives like `"42"` or `"true"` parse as JSON but are not what consumers want flagged. |
| `hex` | Optional leading `#` (color codes), then 6 or more characters from `[0-9a-fA-F]`, with at least one letter (`a`-`f`) among them. The letter requirement excludes an even-length digit id from reading as `hex` instead of `numeric_string`, and a compact postal code (`postal_code` outranks `hex`) from losing its own pattern. **NOT matched**: fewer than 6 characters, or an all-digit run of any length - a 6-character code drawn from the closed hex alphabet is self-limiting the way `country_code` is, but a longer all-digit run is not, so the letter requirement is not optional. |
| `jwt` | RFC 7515 compact serialization: three `.`-joined base64url segments, where the first non-empty and decodes to a JSON object carrying `alg`. The third (signature) segment MAY be empty - the `alg: none` unsecured form (RFC 7515 §3.1) - and is never decoded, since a signature is bytes and decoding it proves nothing. **NOT matched**: a five-segment compact JWE, and any other three-dot-segment token whose first part does not decode to a JOSE header - a purely lexical rule would claim any dotted opaque token, which is the kind of exclusion clause this axis's patterns are defined to avoid needing. |
| `base64` | Length ≥ 16 AND character set is standard base64 (`A-Z`, `a-z`, `0-9`, `+`, `/`, padding `=`) OR URL-safe variant (`-`, `_` instead of `+`, `/`) AND at least one uppercase and one lowercase character AND decodes without error. Length floor avoids matching short numeric or hex strings; the case-mixture requirement is what separates a real encoded token from an ordinary lowercase word or an all-uppercase code, since base64 output is near-uniform over a case-split alphabet. **An unpadded URL-safe candidate is matched only when its own length is already a multiple of four** - padding a short remainder would admit any label whose length is not congruent to 1 mod 4, which is most of them. `hex` outranks `base64` - every digest length in circulation (32, 40, 64) is a multiple of four drawn from a subset of the base64 alphabet, so `base64` would otherwise claim every cryptographic hash. |
| `latlon` | Two decimal numbers, comma-separated with optional following whitespace, each carrying a fractional part, the first in [-90, 90] and the second in [-180, 180]. A space-separated pair is NOT matched - the comma is the evidence, on `phone`'s own precedent that a separator is signal rather than noise. **A stated gap**: nothing in the values separates a coordinate pair from any two small decimals (`1.5,2.5` still matches), and no value test can. |
| `iso8601_duration` | `P[nY][nM][nD][T[nH][nM][nS]]`, or the `PnW` week form, requiring at least one component - a bare `P` or `PT` is NOT matched. |
| `iso8601_date` | `^\d{4}-\d{2}-\d{2}$`, gated by a calendar-validity check - `2024-02-31` is regex-shaped but NOT matched. **The basic form (`20240115`, no hyphens) is NOT matched** - the hyphens are the evidence that separates a date from any other eight-digit run, on the same precedent `phone`'s separator requirement uses. |
| `iso8601_datetime` | `^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z\|[+-]\d{2}:\d{2})?$`, same calendar-validity gate on the date portion. Seconds and the zone designator are both optional. **A single space is accepted alongside `T`** - not RFC 3339, but what `timestamp::text` renders in Postgres. **Lowercase `t`/`z` are NOT matched** - legal under RFC 3339, but no database engine emits them, so accepting them would cost a second regex for a form nothing produces. A column mixing dates and datetimes reports neither - `iso8601_date` and `iso8601_datetime` are different cast targets, and a column that genuinely mixes them has no single correct one to hand a consumer. |
| `numeric_string` | Regex `^-?\d+(\.\d+)?$`. Signed integer or decimal. No scientific notation, no thousand separators, no currency markers. |
| `semver` | [Semantic Versioning 2.0.0](https://semver.org)'s own grammar: three dot-separated non-negative integers with no leading zeroes, an optional `-` prerelease of dot-separated alphanumeric identifiers, an optional `+` build metadata of the same. **Accepts an optional leading `v`** - a form semver.org's own grammar refuses, but that git tags and dependency manifests store constantly, and one that takes no column from any other pattern since it reports nothing today. A two-part version (`1.2`) is NOT matched and stays `numeric_string`; a three-part one already fails that pattern's single-dot grammar, so the two never compete for a column. |
| `country_code` | Membership of the ISO 3166-1 **alpha-2** set, case-insensitive. A closed 249-entry list, so membership is the whole test. **Alpha-3 codes are NOT matched** - a second hand-maintained list is a second silent-error surface, and alpha-2 is overwhelmingly what databases store. The closed set is self-limiting in the useful direction: a column of US state abbreviations overlaps it by roughly a third and never reaches the threshold. **Two-letter ISO 639-1 language codes are a stated overlap, not a defect**: 110 of the 184 language codes are also alpha-2 country codes, and a column of them (`de`, `es`, `it`, `pt`) reports `country_code` - there is nothing in the values to tell the two apart, and a `language_code` pattern is refused for exactly that reason (undecidable against this one, not a maintenance concern). |
| `currency_code` | Membership of the ISO 4217 **alpha** set, case-insensitive. A closed 178-entry list, snapshot dated 2026-01-01 and amended two to four times a year - a missing code (a newly minted currency) reads as a known staleness gap, not a bug. Includes precious-metal, fund and test codes (`XAU`, `XDR`, `XXX`) alongside national currencies; none is excluded. **Alpha-3 country codes are NOT matched by this door** - only `BTN`, `CHE`, `MKD` and `SLE` sit in both sets, four entries out of 249, so a column of alpha-3 codes never approaches the threshold. |
| `postal_code` | Postal formats that identify themselves by carrying a letter: United Kingdom, Canadian and Dutch shapes. **All-digit formats are deliberately NOT matched** - a five-digit US ZIP is indistinguishable from `numeric_string` without a locale, and dbprint has no locale to consult. A column of US ZIPs therefore reports `numeric_string`, which is the honest answer from values alone. This is a stated gap, not an oversight. |
| `isbn` | The 13-digit ISBN form: prefix `978` or `979`, GS1 mod-10 check (weights 1 and 3 alternating from the right of the payload). The 10-digit form: nine digits plus a check character (`0`-`9` or `X`), weights 10 down to 1 from the left, sum divisible by 11. Hyphenated forms are stored constantly; hyphens and spaces are stripped before the check runs, the same precedent `card_number` set. Outranks `ean`: the 13-digit form is the `978`/`979` prefix subset of the 13-digit EAN and carries the identical check, so the subset is claimed first. Both outrank `card_number`, so a barcode column is not diluted by the tenth of it that also happens to be Luhn-valid. |
| `ean` | GTIN forms of eight, twelve, thirteen or fourteen digits: a bare digit run of one of those four lengths, GS1 mod-10 valid. No prefix table - unlike `isbn`, a GTIN carries no reserved leading digits. **A value failing the check is NOT matched.** Collides with `card_number` by arithmetic alone (each check passes about one random digit run in ten of the other's length), and `ean` ranks above it for exactly that reason - see `card_number`'s own row for the reciprocal case. |
| `vin` | Seventeen characters from the alphabet `A`-`Z` and `0`-`9`, excluding `I`, `O` and `Q`, with the check digit at position 9 computed per the federal VIN regulation (49 C.F.R. 565.15): each letter transliterates to a value (Table III), each position carries a weight (Table IV), and the weighted sum mod 11 MUST equal the check digit - a remainder of 10 renders as `X`. Ranked above `base64` defensively: a 17-character alphanumeric VIN clears that pattern's floor and alphabet, and escapes it today only because 17 is not a multiple of four - arithmetic luck, not a rule. |
| `imei` | Exactly fifteen digits, Luhn-valid, first two digits (the Reporting Body Identifier) a member of the set GSMA has ever allocated to a device manufacturer - eighteen two-digit codes, six of them still active (GSMA's IMEI allocation guidelines, Annex A). **NOT matched** when the RBI is not in that set, even if the value is otherwise Luhn-valid at device length - the column falls through to `card_number` instead. **A stale RBI mislabels rather than misses**: neither `34` nor `37` (Amex's issuer prefixes) has ever been allocated as an RBI, which is what keeps the two patterns from colliding; a future allocation into either prefix would need this list updated. |
| `card_number` | 13-19 digit PAN, spaces and hyphens optional, carrying a valid Luhn (mod-10) check digit. No IIN prefix table. **A value failing Luhn is NOT matched** - a column of order ids or account numbers sharing a card-length digit run scores far below the threshold (about 10% pass Luhn by chance) and reports no pattern. A system that Luhn-checks its own account numbers is indistinguishable from a real PAN from the values alone and will be claimed - a stated gap, no value test can separate it. `isbn`, `ean` and `imei` all outrank this pattern, resolving the checksummed-shape overlaps at the values that would otherwise be claimed here: a fifteen-digit Luhn-valid run reports `imei` when its Reporting Body Identifier is allocated and `card_number` otherwise - an Amex number (issuer prefix `34`/`37`) is never in that allocated set, so the two never collide in practice. |
| `iban` | ISO 7064 mod-97-10: two-letter country code, two check digits, up to 30 further alphanumeric characters, total length 15-34, spaces optional. The first four characters move to the end, each letter substitutes for its position value plus 9 (`A`=10 .. `Z`=35), and the resulting integer mod 97 MUST equal 1. |
| `bic` | ISO 9362: six letters (institution + country code) then two alphanumerics (location), with an optional three-character branch code (8 or 11 characters total). Positions 5-6 MUST be a valid ISO 3166-1 alpha-2 country code, from the same set `country_code` uses. |
| `phone` | E.164 (`+` then 8-15 digits, separators optional), OR a national form carrying at least one separator (space, hyphen, or parenthesis) with 10-15 digits once separators are removed. **A bare digit run is NOT matched** - a phone number and an account id are indistinguishable digit runs, and the separator is the only evidence that tells them apart. **The dot is NOT a recognized separator** - `555.123.4567` is also every version string and every multi-part decimal. The two digit floors exclude a US SSN (9 digits) and an ISO date stored as text (8 digits) arithmetically rather than by a special case. A 15-digit separator-bearing run that also passes Luhn is claimed by `card_number` instead, which outranks `phone`; one that does not remains `phone`. |
| `timezone` | Exact, case-sensitive membership of the producer's `zoneinfo.available_timezones()` set - a stdlib call, not a hand-maintained literal. Covers `Area/Location` names (`Europe/London`), multi-level names (`America/Indiana/Knox`) and short aliases (`UTC`, `GMT`, `EST`) uniformly, including a column mixing all three forms. **Lowercased names are NOT matched** (`europe/london`) - IANA zone names are case-sensitive, and lowercasing would trade a closed set for a fuzzy one on this axis's precision bias (§4.4). **A stated host-variance gap**: the set reflects whatever tzdata the runtime resolves, so two producers on different hosts can disagree on rarely-used aliases; this does not break any reproducibility promise this specification makes, and it is smaller than the run-to-run variance §4.1.2's random sampling already licenses. |
| `content_type` | A registered RFC 6838 top-level type (`application`, `audio`, `example`, `font`, `image`, `message`, `model`, `multipart`, `text`, `video`, or an `x-` experimental type), a slash, a restricted-name subtype, and optional `; parameters`. Case-insensitive. The top-level type is the closed registry rather than a free token, because any two-segment path (`a/image.png`) satisfies the free-token grammar and the two patterns would stop being distinguishable. |
| `path` | A POSIX filesystem path: at least one `/` separator with a non-empty segment before it, no whitespace in any segment, and no URI scheme. Absolute and relative forms both match. **Windows forms are NOT matched** — a drive letter or a backslash separator is reserved, so a column of `C:\...` values reports no pattern rather than a wrong one. **A network block is NOT matched** — CIDR notation (`10.0.0.0/8`) satisfies the same shape (a segment, a slash, a segment), so a value that also parses as a network address reports no pattern rather than `path`. |
| `filename` | A bare name with an extension and no separator: `^[^/\\\s]+\.[A-Za-z][A-Za-z0-9]{0,9}$`. The extension MUST begin with a letter, which is what keeps an IPv4 address and a decimal number from reading as filenames. `semver` outranks it, so a prerelease or build form (`1.0.0-alpha.beta`) reports that pattern instead. **A stated gap: a hostname (`example.com`) is not separable from a filename by value alone, and no list rescues it.** Neither carries a path separator, both end in an alphabetic label, and a TLD-length or TLD-membership rule fails on its own evidence - `.zip`, `.mov`, `.sh`, `.py` and `.md` are all delegated top-level domains and all common file extensions, so `archive.zip` and `example.zip` stay indistinguishable whichever list is consulted. Consumers reading `filename` on a column of domains have the honest answer available from the values themselves. |
| `prose` | Free-running text: at least 5 whitespace-separated tokens, at least one common function word, and matching none of the structural patterns above. The function-word list is English; prose in other languages is not detected, and its absence is not an assertion that a column is structured. |

#### 4.1.2 Sampling strategy

Producers MUST sample up to `looks_like_sample_size` (default 1000) DISTINCT non-null values, selected RANDOMLY across the distinct value set of the scanned set (§2.2.8) — the rows a `scope` leaves, which are the rows every other statistic in the file describes. Random selection avoids the bias of top-N-by-frequency, which would over-weight common values.

If the column's distinct count is less than the sample size, producers MUST sample all distinct non-null values.

The sample size comes from the configuration in force for the table being profiled - `statistics.looks_like_sample_size` in `.dbprint.yaml`, which a producer MAY let a project set per connection, per table, or both. A producer that resolves it per table MUST use the value resolved for the table it is sampling.

Adapter implementations choose how to obtain the sample efficiently (Snowflake `SAMPLE`, Postgres `TABLESAMPLE`, MySQL `WHERE RAND() < x`); the spec requires only that the sample is a uniformly random selection of distinct non-null values.

A producer MAY approximate the distinct-value draw above a row-count threshold of its own choosing, trading the guarantee above for bounded query cost: drawing a uniformly random sample of rows first (Snowflake `SAMPLE`, Postgres `TABLESAMPLE`, MySQL `ORDER BY RAND()`) and taking the distinct values among them, rather than scanning the full distinct set to draw from directly. A full distinct-first random draw costs more as the column's distinct-value count grows, independent of row count, which a row-level sample bounds by construction. The approximation reintroduces exactly the bias the first paragraph's random-selection requirement exists to avoid: a value appearing in more rows is more likely to survive the row-level draw, so the sample over-represents common values in proportion to their frequency. Below the threshold, the full distinct set is read and drawn from directly, satisfying the requirement above without approximation.

A producer that samples an eligible column MUST publish the verdict it reached. There is no permission to decline detection on cost grounds: an absent `inferred.looks_like` on a classification §4.1.5 names states only that no pattern reached the threshold, never that the column went unmeasured.

#### 4.1.3 Match threshold

`looks_like` is set to a pattern when **≥ 95%** of sampled values are assigned that pattern under the priority order of §4.1.4. The threshold is uniform across all patterns, and a value assigned no pattern counts toward the denominator — the share is of what was sampled, not of what matched something. This holds regardless of a value's native type: §4.1.1's coercion happens before assignment, never before counting, so a minority of non-string values inside an otherwise-structured sample dilutes the share rather than being excluded from it.

The 95% threshold tolerates typical data-quality noise (legacy rows with placeholder values like `"deleted"` or `"unknown"`) while remaining strict enough that consumers (LLMs writing SQL) can rely on the hint.

No minimum sample size applies. Below twenty sampled values the threshold is unanimity as an arithmetic consequence of the 95% rule, not as a separate requirement: the best non-unanimous score on n values is (n-1)/n, which reaches 0.95 only at n = 20. This is not blinding: a verdict resting on as few as two values is published exactly like one resting on ten thousand, and the next two paragraphs are what let a reader tell them apart.

**Producers MUST publish the evidence a verdict rests on.** Alongside `inferred.looks_like`, a producer emits `inferred.sampled` (the number of values drawn) and `inferred.matched` (how many of them the winning pattern claimed) — the two operands of this section's own threshold. A consumer recomputes `matched / sampled` to judge how much a verdict is worth trusting; a two-of-two match and a nine-thousand-five-hundred-of-ten-thousand match both satisfy the 95% rule but are not the same claim, and only the pair lets a reader distinguish them. Neither field is a second gate: the verdict already cleared the threshold before either is written, and a producer MUST NOT withhold `looks_like` because the pair looks thin.

`sampled` and `matched` describe `looks_like` alone, never `epoch_unit` or `sensitivity` even where those axes read the same draw (§4.1.5, §4.4.4) — each pattern matches a different subset of one sample, so one count could not describe more than one verdict without becoming ambiguous about which. A consumer reading a small `sampled` on `epoch_unit` or `sensitivity` is reading nothing, since the pair is never emitted for them.

#### 4.1.4 Priority order when multiple patterns match

When a sampled value matches multiple patterns, producers MUST walk this list top-to-bottom and assign the first match:

1. `uuid`
2. `email`
3. `url`
4. `urn`
5. `content_type`
6. `ip`
7. `mac_address`
8. `country_code`
9. `currency_code`
10. `postal_code`
11. `isbn`
12. `ean`
13. `imei`
14. `card_number`
15. `iban`
16. `bic`
17. `phone`
18. `timezone`
19. `json` (top-level structured)
20. `hex`
21. `jwt`
22. `vin`
23. `base64` (length ≥ 16)
24. `latlon`
25. `iso8601_duration`
26. `iso8601_date`
27. `iso8601_datetime`
28. `numeric_string`
29. `semver`
30. `path`
31. `filename`
32. `prose`

A column-level `looks_like` value is the pattern assigned to ≥ 95% of sampled values when each value is evaluated under this priority order.

Seventeen orderings in this list are load-bearing and a producer MUST NOT reorder them - `iso8601_date` and `iso8601_datetime` are not among them: nothing ranked above them claims their shape, and nothing between them and `numeric_string` claims what they decline, so their placement is a family grouping rather than a conflict resolution. `currency_code` is not among them either: no pattern above it can claim a three-letter alphabetic token, so its rank is free rather than a conflict resolution. Neither is `jwt` nor `vin`: nothing above `base64` claims a dotted token or a 17-character alphanumeric run today, so both ranks above it are defensive rather than a conflict resolution - they stay robust against a future widening of the base64 alphabet, at no cost to any pattern that exists now.

- `url` outranks `path` and `filename`, so a link never reports as either.
- `content_type` outranks `path`, so `image/png` is not read as a two-segment path.
- `path` outranks `filename`, so the presence of a separator decides between them: `image.png` is a `filename`, `a/image.png` is a `path`.
- `path` and `filename` sit below the patterns whose character sets they subsume. A `path` is only "no whitespace and a separator", which a long `base64` token satisfies — the slash is in its alphabet — and which compact `json` carrying a URL satisfies too. A `filename` is subsumed the same way by every dotted address and every decimal. Ranked higher, either would take columns that belong elsewhere.
- `semver` outranks `path` and `filename`, so a prerelease or build form (`1.0.0-alpha.beta`) is not read as either — this is the entire fix for that row, since a dotted, hyphenated version string satisfies `filename`'s shape (its final segment starts with a letter and clears the ten-character ceiling) below either point.
- `phone` outranks `base64`, so a 15-digit E.164 number (`+` plus 15 digits is 16 characters) is not read as base64 — its character set and length both clear that pattern's floor.
- `card_number` outranks `phone` and `base64`, so a 15-digit separator-bearing Amex number and a 16-digit bare PAN both read as `card_number` rather than falling to either — this is the entire fix for both rows, since below either point the column would still report *a* pattern and the defect would go uncaught.
- `isbn` outranks `ean`, so the `978`/`979`-prefixed subset of the 13-digit EAN reads as `isbn` rather than the superset pattern it also satisfies. Both outrank `card_number`, so a barcode column is not diluted by the tenth of it that also happens to be Luhn-valid — the loser of that arithmetic collision reports nothing rather than the wrong shape.
- `imei` outranks `card_number`, so a fifteen-digit Luhn-valid run with an allocated Reporting Body Identifier reads as `imei` rather than `card_number` — this is the entire fix for that row, since `card_number` carries no IIN table to exclude device identifiers on its own.
- `hex` outranks `base64`, so a cryptographic hash of any length in circulation (32, 40, 64 hex characters) reads as `hex` rather than as base64-encoded data. `postal_code` in turn outranks `hex`, so a compact UK postcode (`EC1A1BB`, coincidentally all-hex) still reads as `postal_code`.
- `mac_address` outranks `phone`, so a hyphenated all-digit MAC address is not read as a national phone number.
- `urn` outranks `path`, so a URN carrying a `/` in its namespace-specific part is not read as a filesystem path.
- `timezone` outranks `path` and `base64`, so `Europe/London` is not read as a filesystem path and `Pacific/Auckland` (16 characters, a base64-alphabet subset) is not read as base64.
- `country_code` outranks `timezone`: a handful of IANA `backward` aliases (`GB`, `NZ`) are also alpha-2 country codes, and the ordering keeps the answer host-independent for the one pattern where the collision is possible.

Because each value is assigned before anything is counted, these orderings hold for every sample rather than only for uniform ones. A column of 90 `image/png` values and 10 POSIX paths reports neither pattern: the media types are assigned `content_type` and the paths `path`, and at 0.90 and 0.10 neither clears the threshold. Counting each pattern independently over the whole sample would report `path` at 1.00 — the outcome the second ordering above exists to forbid.

`prose` is last because it is the fallthrough for anything textual carrying no structure. Its definition (§4.1.1) also excludes every structural pattern, so the predicate answers for a value on its own rather than only in the position the priority order gives it.

#### 4.1.5 Classifications that trigger `looks_like` detection

Producers MUST run `looks_like` detection on columns classified as:

- `categorical`
- `text`
- `foreign_key_candidate`

Producers MUST NOT run `looks_like` on columns classified as:

- `boolean`, `numeric`, `temporal` — the SQL type already conveys the shape; `looks_like` adds no information
- `json` — the column IS JSON; `looks_like: json` would be tautological
- `unsupported` — opaque by definition

**A `numeric_string` verdict is additionally withheld on a numeric SQL type, even on a classification that runs detection.** A `categorical` or `foreign_key_candidate` column whose SQL type belongs to the same `numeric` type family §3.2 classifies on (a numeric-typed column reaches either classification via a declared key or a low cardinality, independent of its type) restates its own declared type by construction if `numeric_string` is allowed to publish there — the same cost the classification-level exclusion above pays, paid here for a numeric-typed column that classifies elsewhere. Producers MUST withhold `numeric_string` in that case, along with the `sampled`/`matched` evidence pair that would have qualified it (§4.1.3); every other `looks_like` pattern remains eligible on the same column, detection still runs, and only this one verdict is suppressed.

**This exclusion is known to hide value shapes the SQL type does not convey, on `numeric` alone.** For `boolean` and `temporal` the rationale is complete: a boolean sample can match nothing new, and every textual pattern `looks_like` defines is either the tautological ISO rendering of the column's own type (§4.1) or undecidable from the values (§4.4.1's `national_id` paragraph). A `numeric` column is different — `BIGINT` conveys "an integer this wide" and says nothing about whether the integer is a quantity, a card number, an epoch, or a national id, so the exclusion trades a real detection gap (a card number in a `BIGINT` is permanently unreachable by `looks_like`) for the certainty of never publishing `looks_like: numeric_string` on the overwhelming majority of `numeric` columns, which hold ordinary quantities and would report it on every one by construction. The trade is deliberate, not an oversight: widening costs one `sample_values` query per newly-eligible column per run, and the recall the axis needs is available more cheaply on `sensitivity` (§4.4.3), which already reads the column name for free on these two classifications.

#### 4.1.6 `looks_like` reserves

Empty. Every pattern name this format uses is defined in §4.1.1 and ranked in §4.1.4.

Consumers MUST tolerate unknown `looks_like` values (see §5.3) — a pattern can be added without a MAJOR version bump, reserved here or not.

### 4.2 `candidate_key`

`inferred.candidate_key: true` MUST be set when `cardinality_ratio` ≥ 0.9999 (effectively unique), regardless of declared PRIMARY KEY / UNIQUE constraints. The flag is a threshold, not a proof of uniqueness: a column at ratio 0.9999 can carry measured duplicates, and `candidate_key: true` alone does not say whether it does.

**`inferred.candidate_key_exception`** distinguishes a genuinely unique column from one inside the threshold band. Emitted only on a column carrying `candidate_key: true` whose `cardinality_ratio` is strictly below `1.0` — a column at exactly `1.0` emits nothing here, unchanged from today. Two values:

- `measured_duplicates` — `cardinality_method` is `exact` and `cardinality` is less than the non-null scanned count (`rows_scanned - null_count`). The producer counted every distinct value and some rows share one.
- `estimated` — `cardinality_method` is `approximate`. The estimator's own error can straddle the threshold in either direction, so a ratio below `1.0` here is not a count of duplicates; it is a statement that the count itself is not exact.

The two are not interchangeable: an approximate count showing no shortfall against `rows_scanned` still MUST NOT be read as `measured_duplicates`, since the estimate could as easily have overcounted the true distinct value set as undercounted it.

### 4.3 `fk_candidate`

Cross-table overlap-based foreign key detection — comparing a column's values against a candidate target's. Not defined.

Distinct from the naming-based inference in §2.3.8, which is defined today: that one reads the catalog and costs nothing, this one reads data and costs a scan per candidate pair. A producer MUST NOT emit `inferred.fk_candidate`, and naming-based inference MUST NOT be reported through it — it belongs in `relationships.yaml` with `detection: inferred`.

---

### 4.4 `sensitivity` — data that must not leave the database

`looks_like` answers "what shape is this value" and is tuned for **precision**: a wrong shape hint makes an agent write a wrong query. `sensitivity` answers a different question — "does this column hold data that must not leave the database" — whose errors are asymmetric in the opposite direction. Over-flagging costs one over-masked column. Under-flagging leaks a customer's name, home address, or a live credential into a git repository.

Most categories on this axis are personal data in the ordinary sense - a name, an address, a date of birth. One is not: `credential` (§4.4.1) flags a secret whose disclosure is a security incident rather than a privacy one. The axis is named for what it gates, not for a single legal or ethical framing; SPEC 4.4.2 already forecloses reading it as a compliance claim, and that forecloses reading it as a privacy-only claim too.

The two are therefore separate fields with separate error budgets, and they are **not alternatives**: an email column carries `looks_like: email` and `sensitivity: contact` at the same time. A consumer gating redaction reads one field rather than maintaining its own shape-to-sensitivity mapping.

#### 4.4.1 Categories

| Category | Meaning |
|---|---|
| `personal_name` | A natural person's name, in whole or in part |
| `postal_address` | A street address or a line of one |
| `geolocation` | A coordinate, a coordinate pair, or a geohash |
| `date_of_birth` | A person's date of birth |
| `national_id` | A number a government issued to identify a person - a social security, tax, or passport number |
| `financial_account` | A bank or card account - an IBAN, a bank account number, a card number, a card security code |
| `credential` | A secret - a password hash, an API key, a session token, a private key |
| `health` | Clinical data - a diagnosis, a blood type, a medication, an allergy, a disability |
| `demographic` | An attribute widely treated as needing extra care - ethnicity, religion, sexual orientation, gender identity, political or union affiliation |
| `employment` | A person's compensation - salary, wage, bonus, commission |
| `contact` | A means of contacting a person — email, telephone |
| `online_identifier` | A network address, device or session identifier that ties activity to a person - an IP address, a MAC address, a device or advertising identifier, a session or cookie identifier |

**`health` and `demographic` are separate categories, not one merged value.** `RedactRule.covers()` targets by `sensitivity` value, so the vocabulary is the granularity of the redaction control - a merged value would force both or neither, where a project may legitimately need diagnoses withheld while ethnicity stays readable for equity reporting, or the reverse. Both are name evidence only; a `diagnosis_notes` `text` column reporting `looks_like: prose` still carries `health` even though its prose exemption (§2.2.3's second footnote) means it publishes no `values` list to withhold - the flag is not conditional on there being something to redact. `condition`, `treatment`, `procedure`/`procedure_code`, `test_result` and `status` are deliberately excluded from `health` - each is a rules engine's predicate, an experiment arm, a stored procedure, a CI run, or an ordinary status column at least as often as a patient's. Bare `gender`/`sex`, `nationality`/`country` (join keys, §4.4.1's `postal_address` row) and `marital_status`/`language` are deliberately excluded from `demographic` - each is the breakdown an analytics print is most often written to support, so a false positive there costs the most.

**`geolocation` does not re-open §3.4's reserved `geographic` classification.** That reserve is a *classification*, which replaces a column's whole emitted field set; a `latitude` column flagged `geolocation` keeps its `numeric` classification, its `range` and its percentiles - `sensitivity` is additive and displaces nothing, so the reasoning that keeps `geographic` reserved does not reach this axis. Two evidence paths: a `latitude`/`longitude` pair in separate `numeric` columns is name evidence only (no sample is drawn for `numeric` - §4.4.3 below), and a coordinate pair stored in one `text` column reads `looks_like: latlon` (§4.1.1), independent of the column's name. `city`, `region`, `postcode` and `zip` are deliberately excluded: a `redact` rule on this value would silently empty the single most-read categorical enumeration in a schema, and a postcode is `postal_address`'s question, not this one's.

**`date_of_birth` is name evidence only, and deliberately excludes `age`.** No sample is drawn for a `temporal` column (§4.1.5 names only `categorical`/`text`/`foreign_key_candidate`), so the column name is the whole test - unambiguous tokens (`date_of_birth`, `dob`, `birth_date`, ...) need no value agreement, the same treatment `first_name` gets. An `age` column is `numeric`, publishes a coarser range (ages, not dates), and the word means cache, account, or file age at least as often as a person's - it does not join this category. **`redacted: drop` does not remove `freshness`**: `max_age_days` is derived by arithmetic rather than read from a cell (§2.2.3's matrix footnote), so a fully dropped `date_of_birth` column still publishes it - coarsened to the nearest 90 days rather than to the day (§2.2.9), which is what keeps the youngest person's birth date from being recoverable through the one field `drop` cannot remove.

**`national_id` carries no `looks_like` value.** A dashed US SSN (`123-45-6789`) is a hyphenated all-digit string, locale-bound, and indistinguishable from a ZIP+4 or an internal reference code by value alone - the same reasoning that keeps `postal_code` off all-digit formats (§4.1.1). Its check is corroboration for this axis only (§4.4.4), never a shape published to `looks_like`.

**`financial_account` is the one category whose value corroboration is a checksum rather than a heuristic.** A column reporting `looks_like: iban` or `looks_like: card_number` (§4.1.1) carries `financial_account` whatever its name is - a mod-97 or a Luhn check under the value, the same shape-corroboration §4.4.3 already gives `contact` for `email`/`phone`. Institution identifiers (`bic`, a SWIFT code, a UK sort code, a US routing number) are refused rather than unconsidered: they identify a bank, not the account itself, and flagging a public institution code would redact data nobody needed redacted.

**`credential` is the one category not defined by personal data, and its leak shape depends on the column's classification.** A `password_hash` column that is unique per row classifies `text` (SPEC 4.2 - uniqueness does not withhold the value list), so its top-N hashes publish exactly like any other `text` column's, live values and all. A small `api_key` table classifies `categorical` and its `values` list is exhaustive. Both deserve the category, and both now carry live literals a `redact` rule is needed to withhold - a reader cannot tell which risk applies without reading the matrix. Strong tokens (`password`, `password_hash`, `api_key`, `secret_key`, `access_token`, `refresh_token`, `session_token`, `private_key`, `client_secret`) need no value agreement. Weak tokens (`key`, `token`, `secret`, `hash` - ordinary names: a settings table's `key`, a cache `key`, a content `hash`) are corroborated by shape: `looks_like: jwt` flags unconditionally, independent of the name, the same mechanism §4.4.3 already gives `contact`; `looks_like: hex` corroborates only a weak name, since an unconditional `hex` flag would wrongly catch a checksum column. A detected `sensitivity: credential` never gains a `redacted` marker on a column that carries no `values`, `range` or `percentiles` to withhold (§2.2.9's marker rule) - a credential stored in a `json` column stays unmarked even under a matching `redact` rule, which is the rule doing nothing rather than failing.

**`employment` is name evidence only, and its reach is deliberately narrow.** No sample is drawn for a `numeric` column, so the name is the only evidence, the same constraint `geolocation` and `date_of_birth` share. Only `drop` protects a flagged column: `mask` and `hash` leave `range` and `percentiles` in place with a substituted scalar (§2.2.9), so `redact: [{sensitivity: [employment], with: drop}]` is the one rule that removes a salary's bounds. `performance_rating`, `termination_reason`, `manager_notes`, `job_title` and `department` are deliberately excluded - each is an ordinary column an HR dashboard groups by, and a false positive there would silently empty an analytic column a print exists to describe (§4.4.2).

**`online_identifier` reads `looks_like: ip` and `looks_like: mac_address`, and is checked last.** The chain is most-specific-first (§4.4.3): an email address is an online identifier in the broad sense too, so `online_identifier` is checked after `contact` to keep such a column reporting `contact`. A server's IP and a client's IP are the same shape, and dbprint cannot tell them apart from a column name alone - a false positive on an infrastructure inventory is the accepted cost (§4.4.2). `user_id`, `customer_id`, `account_id` and every other internal key are refused rather than unconsidered: they identify a person as surely as a cookie does, and flagging them would put a redaction rule over every foreign key in the database. `session_token` is deliberately excluded here even though it identifies a session - a live session token is a bearer credential first, and `sensitivity: credential` is where it is reached, so the two vocabularies do not claim the same token. `username` joins neither this category nor `personal_name`: a handle identifies an account rather than a device, and nothing separates a real name from a pseudonym in one. Whether the column is unique or repeats on an event log, it classifies `text` and its top-N most frequent handles ship - a stated residue, not a gap this category is meant to close. **A native `INET` or `MACADDR` column is reachable in every cardinality band.** §3.2's `categorical` check is type-agnostic and runs before any type-based branch, so a low-cardinality native column classifies normally and stays reachable by name evidence; a unique one reaches `text` the same way any other high-cardinality measured column does (SPEC 4.2). A column at moderate, non-identifying cardinality with no declared FK matches no named type-based branch either, and classifies `text` rather than `unsupported`: the producer measured a cardinality for it, and the fallthrough follows what was measured (§3.2) - so `inferred.sensitivity` reaches it there like any other `text` column, with no gap left to state.

#### 4.4.2 Recall bias is normative

Producers SHOULD resolve ambiguity toward flagging. A consumer MUST NOT read `sensitivity` as a precise claim, and MUST NOT read its absence as an assertion that a column is safe: **absence means "not detected", never "safe to publish"**. The field exists to gate redaction and to direct a human's attention; it does not make a producer a compliance tool, and nothing in this specification claims completeness for it. §7.2 carries this rule alongside every other absence a reader has to interpret.

**A column that names its own category and publishes it anyway is a conformance warning, not silence.** `privacy.unredacted-sensitive` (§6.3) fires when a column carries `inferred.sensitivity` and publishes at least one of `values`, `range`, `percentiles` with no `redacted` primitive covering it. The code is a warning and never moves the conformance verdict or an exit code - the axis inherits its recall bias, so the warning fires on every false positive `sensitivity` produces, and silencing a real one costs redacting a column a reader wanted to see.

#### 4.4.3 Evidence

The **column name is the primary evidence** and a producer SHOULD use it. It is catalog metadata, so consulting it costs nothing at profiling time and is unaffected by sampling or by a row filter (§2.2.8). It is also the only evidence that separates a `name` column holding people from a `name` column holding companies, which values alone cannot settle. **Detection MUST run against the catalog's own spelling** (§2.2.4's `physical_name`, where the map key (§2.2.1) folded it to lowercase), not the lowercased map key: a token-boundary detector reads `firstName` and `first_name` alike but not `firstname`, so evaluating the lowercased key would miss every camelCase or PascalCase column on an adapter that folds case for the key.

Values corroborate where the name is ambiguous. A producer MUST NOT require value agreement for an unambiguous column name: a `first_name` column holding initials, placeholders, or a script the producer does not model is still a name column.

Producers MUST NOT ship a dictionary of given names for this purpose. Such a list is culturally biased by construction, and the column name provides the signal more cheaply and more accurately.

**On `numeric` and `temporal`, values cannot corroborate, because none are drawn.** §4.1.5 samples only `categorical`/`text`/`foreign_key_candidate`, so a `numeric` or `temporal` column reaches this axis on the column name alone or not at all — the corroboration this section otherwise describes is unavailable there, not merely unexercised. This is why `date_of_birth`, `geolocation` and `employment` (§4.4.1) carry no weak, value-agreement tier: there is no sample to agree with, so every token in those vocabularies is treated as unambiguous by construction.

`contact`, `financial_account`, `online_identifier`, `geolocation` and `credential` additionally read `looks_like` (§4.1): a column whose values match a contact-shaped pattern (`email`, `phone`) carries `sensitivity: contact`, one whose values match `iban` or `card_number` carries `sensitivity: financial_account`, one whose values match `ip` or `mac_address` carries `sensitivity: online_identifier`, one whose values match `latlon` carries `sensitivity: geolocation`, and one whose values match `jwt` carries `sensitivity: credential` - each whatever the column's name is, independent of and in addition to the name evidence above. `personal_name` and `postal_address` are not defined over `looks_like` - their evidence is the column name and, where the name is ambiguous, its values, per the corroboration rule above.

#### 4.4.4 Thresholds

The 95% threshold §4.1.3 defines for `looks_like` is a precision instrument and does NOT apply here. Each detector states its own, and they need not agree with one another. A producer SHOULD document the thresholds it uses.

`date_of_birth`, `health`, `demographic`, `online_identifier`, `geolocation` and `employment` use no corroboration threshold: every token in each vocabulary means what the category names and nothing else, so there is nothing for a sample to corroborate - the same economy §4.4.3 already states for `personal_name`'s strong tier.

`credential`'s weak tier (`key`, `token`, `secret`, `hash`) carries no threshold of its own either: the corroborator is the column's own `looks_like` verdict (`jwt` or `hex`), which has already cleared `looks_like`'s 95% threshold (§4.1.3) before this detector runs - the same economy this section already states for `financial_account`'s generic names. Re-deriving a second per-value share over the same sample would apply two thresholds to one measurement for no gain.

`national_id`'s weak tier (a generic column name such as `id_number`) requires at least 50% of sampled values to match a dashed US SSN shape (area, group and serial ranges the SSA never issues excluded) before the category is reported. **This is a single-locale corroborator, stated as a gap rather than left to be rediscovered**: a UK National Insurance Number, a dashed Canadian SIN, or any other country's identifier grammar will not corroborate a generic column name, and a per-country table of such grammars is the hand-maintained literal §4.1.1 already declines to carry for `postal_code`. A column name unambiguous on its own (`ssn`, `passport_number`, ...) needs no value agreement at all, the same treatment §4.4.3 already gives `personal_name`.

`financial_account`'s generic column names (`account_number`, `account_no`, `pan`) carry no threshold of their own: the corroborator is the column's own `looks_like` verdict, which has already cleared `looks_like`'s 95% threshold (§4.1.3) before this detector runs. Re-deriving a second per-value share over the same sample would apply two thresholds to one measurement for no gain, so a generic name is flagged exactly when the column reports `looks_like: iban` or `looks_like: card_number`, and not otherwise.

#### 4.4.5 Conformance

Membership is the schema's job. The validator MUST NOT inspect `sensitivity` values for plausibility, and an unrecognized value degrades to the warning `schema.unknown-sensitivity` exactly as an unrecognized `looks_like` value does (§5.3).

---

### 4.5 `epoch_unit` — instant vs quantity

An integer column can be a quantity or an instant: a `BIGINT` of Unix epoch seconds or milliseconds looks the same as an ordinary large number, and `range`/`percentiles` publish it as one without saying so. `inferred.epoch_unit` names the storage form when the evidence supports it, so a consumer reading `min: 1704067200` knows to read it as `2024-01-01T00:00:00Z`.

`epoch_unit` is a third independent axis alongside `looks_like` and `sensitivity` (§4.4): a column may carry any combination the matrix (§2.2.3) permits, and none is a fallback for another.

#### 4.5.1 Windows

| Unit | Window | Calendar span |
|---|---|---|
| `seconds` | `1e9` .. `2e9` inclusive | 2001-09-09T01:46:40Z .. 2033-05-18T03:33:20Z |
| `milliseconds` | `1e12` .. `2e12` inclusive | the identical 32-year span, scaled by 1000 |

The test is a plausibility check over a 32-year calendar window, not a proof: an ordinary large integer can fall inside it by chance, and this specification does not claim otherwise. Microsecond (`1e15`..`2e15`) and nanosecond (`1e18`..`2e18`) epochs are a stated gap - out of scope for v1, in the register §4.1.1 uses for `postal_code`'s all-digit gap, not an oversight.

#### 4.5.2 Two evidence rules under one field

Each states its own threshold, the shape §4.4.4 already licenses for the `sensitivity` detectors - the two need not agree with one another, and a column reached by one is never reached by the other:

- **Bounds rule**, for `numeric` columns: `range.min` and `range.max` are both integral and fall inside the same window. No sample, no threshold - the test is over the two required bounds and it is unanimous or it is nothing. Requiring BOTH bounds inside the window is what excludes most ordinary large integers without further evidence: a count, an amount or a byte size almost always has a minimum orders of magnitude below its maximum, while an epoch column's does not. An id sequence seeded near `1e9` is the one shape whose minimum is large by construction, and it is a named, accepted false positive rather than an excluded case.
- **Per-value rule**, for the three classifications §4.1.5 samples (`categorical`, `text`, `foreign_key_candidate`): at least 95% of sampled values stringify to an integer inside the same window, reusing `looks_like`'s own match threshold (§4.1.3) rather than a second number nothing argues for.

The two rules never compete for the same column: `numeric` is never sampled (§4.1.5 names the other three classifications only, and no `range` is required on any of them - §2.2.3's matrix marks it `-`), so a column reached by the bounds rule has no per-value verdict to disagree with, and vice versa.

`epoch_unit` MUST NOT be derived any other way. It is not a `looks_like` value: §4.1.3's per-value assignment is normative there, and a bounds test over two aggregates has no per-sampled-value verdict to make - subsuming `numeric_string` on every epoch-shaped digit run would also need the kind of exclusion clause §4.1's patterns are defined to avoid needing. It is not a note inside `range` either: `range` is a bounds object shared with `temporal`, unreachable for the sampled epoch columns (`categorical`, `text`, `foreign_key_candidate`), none of which emit `range` at all, and it disappears under `redacted: drop` - exactly the column where the marker is worth the most (§4.5.3).

#### 4.5.3 Interaction with redaction

`epoch_unit` is not a cell value (§2.2.9 scopes redaction to cell values only), and detection runs over values that are never persisted, so it is unaffected by redaction: a `redacted: drop` column emits no `range` at all yet still carries `inferred.epoch_unit`. This mirrors a redacted `looks_like`-bearing column still reporting its shape.

`epoch_unit` is not a targetable `redact` vocabulary. `sensitivity` and `looks_like` are the two closed vocabularies a `redact` rule may target (§2.2.9); an epoch unit is not personal data, and this specification defines no third.

#### 4.5.4 Conformance

Membership is the schema's job, the same as `looks_like` and `sensitivity`: the validator MUST NOT inspect `epoch_unit` values for plausibility, and an unrecognized value degrades to a warning exactly as an unrecognized `looks_like` value does (§5.3).

## 5. Compatibility

### 5.1 Versioning

Two independent semvers:

- **`format_version`** (`1`, `2`, …) — the format spec's MAJOR number, embedded in every artifact YAML.
- **`dbprint_version`** (full semver) — the implementation version, embedded in `manifest.yaml`.

The format MAJOR bumps independently of any implementation version. A `dbprint 0.5.0` can emit `format_version: 1`, and a `dbprint 3.0.0` can still emit `format_version: 1`.

### 5.2 Format semver rules

- **MAJOR** (`1` → `2`): breaking. Required field removed, semantics of an existing field changed. Consumers MUST branch on MAJOR.
- **MINOR** (`1.0` → `1.1`): backwards-compatible additions. New optional fields, new change-kinds in diff, new column classifications, new `looks_like` values. Consumers MUST tolerate unknown fields (forward-compat).
- **PATCH** (`1.1.0` → `1.1.1`): clarifications, typo fixes, no field changes.

Artifacts carry only MAJOR (`format_version: 1`); the full MAJOR.MINOR.PATCH triple identifies a spec revision.

### 5.3 Forward compatibility

Consumers MUST tolerate:

- Unknown top-level keys in any YAML artifact.
- Unknown `kind` values in `diff.yaml` changes.
- Unknown `classification` values in `statistics.yaml`.
- Unknown `looks_like` values in `inferred`.
- Unknown `sensitivity` values in `inferred`.
- New artifact files added to per-table directories.

### 5.4 Stability commitment

Within `format_version: 1`, MINOR bumps are unrestricted.

---

## 6. Conformance

### 6.1 Severity model

The conformance suite emits two severity levels:

| Level | Meaning | Effect on result |
|---|---|---|
| `error` | The directory does NOT conform. Producer is buggy or artifact is corrupt. | At least one error → DOES NOT CONFORM |
| `warning` | Directory conforms but has anomalies — typically forward-compat values (unknown classifications, unknown change kinds) or recoverable issues. | No errors, any warnings → CONFORMS WITH WARNINGS |

A directory CONFORMS iff `validate_print()` returns zero `error`-severity issues.

### 6.2 Issue document shape

Each issue emitted by the conformance suite has the structure:

```yaml
- code: <stable_identifier>          # e.g., "manifest.missing-artifact"
  severity: error | warning
  path: <relative_path>              # from print root, e.g., "arboretum/seedbank/accession/statistics.yaml"
  detail: <human_readable>           # explanation including specific values
  spec_ref: <SPEC_section>           # e.g., "§2.5"
```

Consumers MUST tolerate unknown `code` values (forward-compat for MINOR catalog additions).

### 6.3 Error catalog

Grouped by concern. `E` = error, `W` = warning.

#### Directory layout (§1)

| Code | Sev | Trigger |
|---|---|---|
| `layout.missing-manifest` | E | Connection root missing `manifest.yaml` |
| `layout.missing-reading-guide` | E | Connection root missing `reading.md` (§1.2) |
| `layout.missing-diff` | E | Connection root missing `diff.yaml` though `manifest.yaml` records a table (§1.2) |
| `layout.invalid-path-segment` | E | Path segment fails the §1.5.1 allowlist regex |
| `layout.unknown-file` | W | File inside per-table dir not in the canonical artifact list (extensibility tolerance) |
| `layout.unexpected-directory-level` | E | Extra directory between connection root and per-table dir (depth mismatch per adapter hierarchy) |

#### YAML / schema validity (§§2.2–2.6)

| Code | Sev | Trigger |
|---|---|---|
| `schema.invalid-yaml` | E | File isn't valid YAML |
| `schema.missing-required-field` | E | Required field absent per JSON Schema |
| `schema.type-mismatch` | E | Field has wrong scalar type |
| `schema.invalid-percentile-key` | E | Percentile key doesn't match `^p\d{2}$` |
| `schema.unknown-classification` | W | `classification` value not in the enum (forward-compat) |
| `schema.unknown-change-kind` | W | `kind` value in diff not in the enum (forward-compat) |
| `schema.unknown-looks-like` | W | `inferred.looks_like` value not in the enum (forward-compat) |
| `schema.unknown-sensitivity` | W | `inferred.sensitivity` value not in the enum (forward-compat) |
| `schema.unknown-epoch-unit` | W | `inferred.epoch_unit` value not in the enum (forward-compat) |

#### Format version (§5)

| Code | Sev | Trigger |
|---|---|---|
| `version.missing-format-version` | E | Required artifact missing `format_version` field |
| `version.invalid-format-version` | E | `format_version` isn't a positive integer |
| `version.unknown-format-version` | E | `format_version` is a MAJOR the v1 validator doesn't understand (e.g., `2`) — the validator's job is to validate v1; cannot do so for unknown majors |

#### Manifest cross-checks (§2.5)

| Code | Sev | Trigger |
|---|---|---|
| `manifest.missing-artifact` | E | Manifest entry references file that doesn't exist on disk |
| `manifest.orphaned-artifact` | W | File on disk not listed in any manifest entry (could be in-progress write; not strictly a violation) |
| `manifest.table-fqn-mismatch` | E | Manifest's `table` FQN doesn't match the `table` field in the referenced statistics.yaml / relationships.yaml |
| `manifest.selectors-mismatch-diff` | E | `selectors` disagrees with `diff.yaml`'s `target.selectors` when both are present (§2.5, §2.6.3) |
| `manifest.columns-count-mismatch` | E | A table entry's `columns` count disagrees with the size of its `statistics.yaml` `columns` map (§2.5) |

#### Statistics invariants (§2.2)

| Code | Sev | Trigger |
|---|---|---|
| `stats.missing-required-field-for-classification` | E | Column missing a field marked R in the §2.2.3 matrix for its classification |
| `stats.forbidden-field-for-classification` | E | Column has a field marked — in the §2.2.3 matrix |
| `stats.cardinality-exceeds-row-count` | E | `cardinality > row_count` (invariant violation) |
| `stats.null-count-exceeds-row-count` | E | `null_count > row_count` (invariant violation) |
| `stats.null-rate-mismatch` | E | `null_rate` disagrees with `null_count / rows_scanned`, rounded per §2.2.6 |
| `stats.cardinality-ratio-mismatch` | E | `cardinality_ratio` disagrees with `cardinality / rows_scanned`, rounded per §2.2.6 |
| `stats.values-sum-mismatch` | W | An exhaustive `values` list (`values_coverage` of `1.0`) whose counts do not sum to `rows_scanned - null_count`, OR a truncated list whose counts exceed it. WARNING because phase A and phase B are measured in separate statements against a table taking writes - on a live database the two counts can disagree by design, not by producer defect |
| `stats.values-coverage-mismatch` | W | `values_coverage` disagrees with the listed counts over `rows_scanned - null_count`. WARNING for the same cross-phase reason as `stats.values-sum-mismatch`: the two ratios share the same `listed`/`non_null` pair, and a drift small enough to still disagree at six decimals is the same drift, not a distinct producer defect |
| `stats.values-list-short-of-cardinality` | E | An exhaustive `values` list (`values_coverage` of `1.0`, `values_coverage_method` absent or `measured`) carries fewer entries than `cardinality`, `cardinality_method: exact` (§2.2.4). ERROR because `values_coverage` and `cardinality` are both required exact fields whenever this fires - a live table taking writes between the two phases can widen `cardinality` past a list that was already complete when it was read, which is the phase A / phase B disagreement `stats.values-sum-mismatch`'s neighbors tolerate at warning; this code's own severity is unchanged. `values_coverage_method: bounded` reports `stats.values-list-short-of-cardinality-bounded` instead - see below |
| `stats.values-list-short-of-cardinality-bounded` | W | The same disagreement as `stats.values-list-short-of-cardinality`, where `values_coverage_method: bounded` (§2.2.4) already discloses that `values_coverage` is a clamp, not a measurement read at the same instant as `cardinality`. WARNING because the producer has already named the cause this error exists to catch |
| `stats.values-list-exceeds-cardinality` | W | An exhaustive `values` list carries more entries than `cardinality`, `cardinality_method: exact`. WARNING for the same cross-phase reason as `stats.values-sum-mismatch` |
| `stats.values-not-ordered` | E | `values` entries are not ordered by `count` DESC with a lexicographic tie-break (§2.2.4) |
| `stats.distribution-mismatch` | W | When verifiable from an exhaustive `values` list, distribution value doesn't match §2.2.5 rules. WARNING because heuristic-prone for truncated lists and exact-match boundaries. |
| `stats.distribution-contradicts-frequencies` | E | On a `numeric`/`temporal` column with `cardinality_method: exact`, `distribution` disagrees with the verdict recomputed from `frequencies` (§2.2.5). ERROR because the two come from the same fetch and are exact arithmetic, not a heuristic over a possibly-truncated list |
| `stats.scope-not-a-mapping` | E | `scope` is present but is not a mapping (§2.2.8) |
| `stats.scope-missing-rows-scanned` | E | `scope` is present without an integer `rows_scanned` (§2.2.8) |
| `stats.scope-rows-scanned-exceeds-row-count` | E | `scope.rows_scanned > row_count` while `row_count_method` is `exact` — a subset cannot be larger than the set it was drawn from. Permitted under `approximate`, which is the estimate-undershot case §2.2.8 sanctions |
| `stats.scope-sample-out-of-range` | E | `scope.sample` outside the interval (0, 1] |
| `stats.scope-sample-and-filter` | E | `scope` carries both `sample` and `filter` — a table is narrowed one way or the other (§2.2.8) |
| `stats.scope-asserts-nothing` | W | `scope` covers the whole table and records neither `sample` nor `filter`; omit it |
| `stats.excess-precision` | E | A numeric statistic carries more than 6 decimal places (§2.2.6) |
| `stats.redacted-without-marker` | E | A `values` entry carries no `value` but the column declares no `redacted` primitive (§2.2.9) |
| `stats.unrepresentable-names-unemitted-field` | E | `unrepresentable` names a field (`min`, `max`, or a `percentiles` key) the column did not emit (§2.2.4) |
| `stats.unrepresentable-empty` | E | `unrepresentable` is present as an empty list; the key MUST be omitted instead (§2.2.4) |
| `stats.span-days-mismatch` | E | `range.span_days` disagrees with `day_count(range.min, range.max)` (§2.2.4). Skipped when either bound is redacted (`mask`/`hash`), named in `unrepresentable`, or not a parseable ISO date/instant |
| `stats.percentiles-not-ordered` | E | `percentiles` do not ascend with their keys (§2.2.4). Compares parsed values, not key spelling; a value that cannot be read back (unrepresentable, unparseable) is skipped rather than failing the column |
| `stats.percentile-outside-range` | E | A percentile lies outside `[range.min, range.max]` (§2.2.4). `range` and `percentiles` are read by one statement per column, so this does not carry the cross-phase tolerance `stats.values-sum-mismatch` does. Skipped when the column carries any `redacted` marker |
| `stats.max-age-days-mismatch` | E | `freshness.max_age_days` disagrees with `max(0, day_count(range.max, profiled_at))` (§2.2.4). Skipped when the column carries any `redacted` marker, `max` is named in `unrepresentable`, or `range.max` is not a parseable instant |
| `stats.uncoarsened-redacted-day-count` | E | A `temporal` column carrying a `redacted` marker emits `freshness.max_age_days` or `range.span_days` not floored to a multiple of 90 (§2.2.9) |
| `stats.candidate-key-mismatch` | E | `inferred.candidate_key` disagrees with whether `cardinality_ratio` clears the SPEC 4.2 threshold - present where it should be absent, or the reverse. Independent of classification |
| `stats.candidate-key-exception-mismatch` | E | `inferred.candidate_key_exception` disagrees with the value recomputed from `cardinality`, `cardinality_ratio`, `cardinality_method` and `null_count` (§4.2) |
| `stats.population-marker-mismatch` | E | A column's `rows_scanned` disagrees with what the file's `scope` requires - absent or wrong when scoped, present when not (§2.2.8) |
| `stats.null-patterns-absent-with-nulls` | E | `null_patterns` is omitted although some column reports a non-zero `null_count`, or present although none does (§2.2.10) |
| `stats.null-patterns-unknown-column` | E | A `null_patterns` entry names a column absent from the file's `columns` map (§2.2.10) |
| `stats.null-patterns-sum-exceeds-rows-scanned` | E | The `null_patterns` counts sum to more rows than were scanned (§2.2.10), `coverage_method` absent or `measured`. `coverage_method: bounded` reports `stats.null-patterns-sum-exceeds-rows-scanned-bounded` instead - see below |
| `stats.null-patterns-sum-exceeds-rows-scanned-bounded` | W | The same disagreement as `stats.null-patterns-sum-exceeds-rows-scanned`, where `coverage_method: bounded` (§2.2.10) already discloses that the census and `rows_scanned` were not read at the same instant. WARNING because the producer has already named the cause this error exists to catch |
| `stats.null-patterns-coverage-mismatch` | E | `null_patterns.coverage` disagrees with the listed counts over `rows_scanned` (§2.2.10) |
| `stats.null-patterns-not-ordered` | E | `null_patterns.patterns` is not ordered by `count` descending, ties by ascending `columns` (§2.2.10) |
| `stats.null-patterns-duplicate-combination` | E | The same combination of columns appears in more than one `null_patterns` entry (§2.2.10) |
| `stats.null-patterns-reconciliation-mismatch` | E | The `null_patterns` counts naming a column exceed its `null_count`, or fall short of it at `coverage: 1.0` (§2.2.10), `coverage_method` absent or `measured`. `coverage_method: bounded` reports `stats.null-patterns-reconciliation-mismatch-bounded` instead - see below |
| `stats.null-patterns-reconciliation-mismatch-bounded` | W | The same disagreement as `stats.null-patterns-reconciliation-mismatch`, where `coverage_method: bounded` (§2.2.10) already discloses the cause. WARNING for the same reason |
| `stats.physical-name-matches-key` | W | `physical_name` is present and equals the column's own map key; the field asserts nothing and MUST be omitted (§2.2.4) |
| `stats.physical-layout-unknown-column` | E | `physical_layout.keys` names a `column` absent from the file's `columns` map (§2.2.11) |
| `stats.physical-layout-key-not-declared` | E | A column carries `physical_layout_key: true` but is not named in `physical_layout.keys` (§2.2.11) |
| `stats.physical-layout-key-missing-marker` | E | `physical_layout.keys` names a `column` that does not carry `physical_layout_key: true` (§2.2.11) |
| `stats.grain-unknown-column` | E | A `grain.keys` entry names a column absent from the file's `columns` map (§2.2.12) |
| `stats.grain-duplicate-key` | E | The same column combination appears in more than one `grain.keys` entry (§2.2.12) |
| `stats.grain-measured-under-scope` | E | `grain.keys` carries a `detection: measured` entry on a file that also carries `scope` - uniqueness over a sample is not uniqueness (§2.2.12) |
| `stats.grain-measured-on-empty-table` | E | `grain.keys` carries a `detection: measured` entry on a table with `row_count: 0` - every combination is trivially unique there (§2.2.12) |
| `stats.dependencies-unknown-column` | E | A `dependencies` entry names a `determinant` or `dependent` absent from the file's `columns` map (§2.2.13) |
| `stats.dependencies-self-referential` | E | A `dependencies` entry's `determinant` and `dependent` name the same column (§2.2.13) |
| `stats.dependencies-strength-out-of-range` | E | A `dependencies` entry's `strength` is not in `(0, 1]` (§2.2.13) |
| `stats.dependencies-direction-impossible` | E | A `dependencies` entry's `determinant` has lower `cardinality` than its `dependent` - a function's image cannot exceed its domain, so that direction cannot hold (§2.2.13) |
| `stats.dependencies-measured-under-scope` | E | `dependencies` is non-empty on a file that also carries `scope` - a dependency measured over a sample is not a dependency (§2.2.13) |
| `stats.dependencies-measured-on-empty-table` | E | `dependencies` is non-empty on a table with `row_count: 0` - every combination is trivially functional there (§2.2.13) |
| `stats.sketch-unknown-method` | E | `sketch.method` is not a value this MAJOR defines (§2.2.14) |
| `stats.sketch-invalid-encoding` | E | `sketch.values` is not valid base64 of a length that is a multiple of 8 bytes (§2.2.14) |
| `stats.sketch-oversized` | E | A decoded `sketch.values` carries more entries than `method`'s own k (§2.2.14) |
| `stats.sketch-not-ascending` | E | A decoded `sketch.values` is not sorted ascending (§2.2.14) |
| `stats.measurement-under-catalog-only` | E | A column carries a field beyond `sql_type`, `nullable`, `classification`, `physical_name`, `collation`, `physical_layout_key` on a file that also carries `catalog_only` - a measurement published where none was queried (§2.2.15) |

#### Privacy (§4.4)

| Code | Sev | Trigger |
|---|---|---|
| `privacy.unredacted-sensitive` | W | Column carries `inferred.sensitivity` and publishes at least one of `values`, `range`, `percentiles`, with no `redacted` primitive covering it (§4.4.2) |

#### Relationships invariants (§2.3)

| Code | Sev | Trigger |
|---|---|---|
| `relationships.column-array-length-mismatch` | E | `column` and `target_column` (in refers_to) or `column` and `referencer_column` (in referenced_by) have different array lengths |
| `relationships.broken-reciprocity` | E | A `referenced_by` entry points to a source table that's in the manifest BUT lacks the matching `refers_to` entry |
| `relationships.ineligible-target-is-referenced` | E | `eligible_target: false` but `referenced_by` carries an `inferred` entry - naming inference cannot resolve an edge to an ineligible object (§2.3.8). A `declared` entry there is unaffected; a composite key inference never reaches can still be a real FK target |
| `relationships.path-on-composite-endpoint` | E | `path` (or `target_path`) is present but its partner array (`column` or `target_column`) names more than one column (§2.3.9) - a path endpoint is representable only on a single-column endpoint |
| `relationships.observed-fanout-mismatch` | E | `observed.fanout_avg` disagrees with the referencing column's own `row_count / cardinality`, rounded per §2.2.6 (§2.3.10) |
| `relationships.observed-coverage-mismatch` | E | `observed.target_coverage` disagrees with the two endpoints' own `cardinality / cardinality`, rounded per §2.2.6 (§2.3.10) |
| `relationships.observed-coherent-mismatch` | E | `observed.coherent` disagrees with whether the referencing column's `cardinality` exceeds the referenced column's (§2.3.10) |
| `relationships.observed-containment-mismatch` | E | `observed.containment` disagrees with the bottom-k intersection estimate recomputed from the two endpoints' own `sketch` (§2.3.10, §2.2.14) |
| `relationships.observed-containment-forbidden` | E | `observed.containment` is published where the two endpoints' sketches carry no evidence to support it - the answerable subset between them is empty (§2.3.10) |
| `relationships.observed-answerable-count-mismatch` | E | `observed.answerable_count` disagrees with the count of the referencing column's own retained hashes below the shared threshold (§2.3.10, §2.2.14), including when the answerable subset is empty and no count should be published at all |

#### Diff invariants (§2.6)

| Code | Sev | Trigger |
|---|---|---|
| `diff.summary-count-mismatch` | W | Summary count doesn't match actual events count for that kind (informational redundancy; producer bug but not format break) |
| `diff.summary-total-mismatch` | W | `tables_modified + unchanged_tables + unevaluated_tables + tables_added` doesn't equal `target.tables_scanned` (§2.6.4). WARNING on the same grounds as `diff.summary-count-mismatch`: the events remain readable, and every operand is redundant with them |
| `diff.relationship-modified-no-change` | E | `relationship_modified` event without `on_delete` OR `on_update` sub-objects (§2.6.6 rule) |
| `diff.comment-target-column-mismatch` | E | `comment_changed` with `target=column` missing `column` field, OR `target=table` with `column` field present |
| `diff.statistic-changed-delta-on-non-numeric` | E | `statistic_changed` with `delta` or `delta_pct` on stats where they MUST be omitted (distribution, classification, values) |
| `diff.statistic-changed-delta-pct-sign-mismatch` | E | `statistic_changed` where `delta` and `delta_pct` disagree in sign |
| `diff.row-count-changed-delta-mismatch` | E | `table_row_count_changed` where `delta` doesn't equal `after - before` |
| `diff.grain-changed-no-change` | E | `grain_changed` event's `before` and `after` are identical (§2.6.6) |
| `diff.physical-layout-changed-no-change` | E | `physical_layout_changed` event's `before` and `after` are identical (§2.6.6) |

#### DDL (§2.1)

| Code | Sev | Trigger |
|---|---|---|
| `ddl.empty-file` | E | `ddl.sql` exists but is empty |
| `ddl.missing-trailing-newline` | W | File doesn't end with newline (POSIX convention; trivially auto-fixable) |

#### Annotations (§2.7)

| Code | Sev | Trigger |
|---|---|---|
| `annotations.unknown-column` | W | A key in `statistics.annotations.yaml`'s `columns` map names a column not present in the table's `statistics.yaml` (stale key; not fatal) |
| `annotations.grain-unknown-column` | W | A `grain.keys[]` entry in `statistics.annotations.yaml` names a column not present in the table's `statistics.yaml` `columns` map (§2.7.1; stale key, not fatal) |
| `annotations.claim-contradicts-statistic` | W | A `claims` predicate (§2.7.1) evaluates against the column's own `statistics.yaml` and fails. WARNING - the axis is advisory (§2.4); a contradicted claim is never a gating defect |
| `annotations.claim-unassertable` | W | A `claims` predicate cannot be evaluated: the stat is not in the assertion DSL's vocabulary, the predicate is malformed, the stat is not emitted for the column's classification, or the column is redacted |
| `annotations.unknown-edge` | W | A `verdict` in `relationships.annotations.yaml` addresses an edge absent from the table's own `relationships.yaml` `refers_to` (§2.7.2; stale, not fatal) |
| `annotations.verdict-on-declared-edge` | W | A `verdict` addresses an edge whose matching `refers_to` entry carries `detection: declared` (§2.7.2) - an annotation may correct an inference, never contradict a measurement (§2.4) |
| `annotations.unknown-value` | W | A `values` entry in `statistics.annotations.yaml` names a value the column's own **exhaustive** `values` list does not have (§2.7.1; stale, not fatal - a truncated list is not checked) |
| `annotations.value-note-unassertable` | W | A `values` entry addresses a column carrying a `redacted` marker (§2.2.9) - there is no literal to check the note against |

### 6.4 Catalog totals

- **113 codes** across 10 groups
- **85 error** codes (gate conformance)
- **28 warning** codes (recoverable anomalies)

The catalog MAY grow in MINOR releases (additive only). Existing codes' semantics MUST NOT change.

### 6.5 Run-all-then-report

The conformance suite MUST emit ALL issues found, not stop at first. Matches the run-all-then-report failure-isolation principle from multi-table generate runs.

### 6.6 Issue ordering

Issues are ordered for diff stability: first by `path` (lexicographic), then by `code` (lexicographic). Deterministic across runs.

### 6.7 Conformance test suite

The reference conformance suite SHALL be a pytest module at `tests/conformance/`. Any producer SHOULD run this suite against its output as part of CI.

API:

```python
from dbprint.conformance import validate_print, Issue

issues: list[Issue] = validate_print("/path/to/prints/connection_name/")
errors = [i for i in issues if i.severity == "error"]
warnings = [i for i in issues if i.severity == "warning"]

conforms = len(errors) == 0
```

### 6.8 Forward compatibility

- Consumers MUST tolerate unknown `code` values
- Consumers MUST tolerate unknown `severity` values (treat unknown as warning by default)
- This validator cannot validate an artifact whose `format_version` it does not know — it emits a `version.unknown-format-version` error

### 6.9 Validator edge cases

| Case | Resolution |
|---|---|
| Empty `diff.yaml` (no changes, `changes: []`) | CONFORMS, not an issue |
| Missing `description.md` | CONFORMS — file is optional (§2.4) |
| Missing `relationships.yaml` on plain view | CONFORMS per §1.4 (MAY be absent for plain views) |
| Unknown `change_kind` in diff.yaml | WARNING (`schema.unknown-change-kind`); forward-compat |
| Mixed `format_version` values within a print | Each file validated separately; a file whose version this validator does not know emits `version.unknown-format-version`, the rest validate normally |

---

## 7. Reading an absence

A consumer meets an absence far more often than it meets a value, and the rules governing what one means are stated where each field is defined — across §2.2, §2.3, §3 and §4, all written from the producer's side. This section collects them from the reader's.

**Normative where it restates a MUST, informative where it collects.** No rule originates here; every row cites the section that governs it, and that section wins on any disagreement.

### 7.1 The two absences that are not absences

**A statistic measured over part of the table is present, not missing.** Under a `scope` block (§2.2.8) every count in the file is a count over `rows_scanned` rather than over `row_count`, so a number an order of magnitude below what a reader expects is a narrower population, not a partial artifact. The file says which: `scope.rows_scanned` at the head, echoed on every column. A consumer MUST read the two together before concluding anything about a small number.

**A producer failure is not representable at all.** No artifact in v1 can say "this statistic was attempted and errored". A measurement that failed and a measurement that came back empty are the same bytes, on every field in every table below, and no cause column names it because no consumer can detect it. A producer that cannot measure a field emits the artifact without it, indistinguishable from one the rules forbade. This is a stated gap.

### 7.2 Absent per-column fields

Every field the §2.2.3 matrix marks anything but **R** on at least one classification. The five it marks **R** everywhere — `sql_type`, `nullable`, `null_count`, `null_rate`, `classification` — are absent from no conforming column and so from this table.

| Absent field | Candidate causes | Distinguishable by |
|---|---|---|
| `cardinality`, `cardinality_ratio`, `cardinality_method` | The producer declined to measure a cardinality, which is what makes a column `unsupported` in a queried file (§3.1) - or the file carries `catalog_only` (§2.2.15), absent from every column regardless of classification | `classification`, then the file's own `catalog_only` marker |
| `values`, `values_coverage` | Forbidden for this classification (§2.2.3) — or the column is `text` reporting `looks_like: prose`, exempted from the grouped scan (§2.2.3 ‡) | `classification`, then `inferred.looks_like` |
| `values_coverage_method` | `values_coverage` is itself absent (§2.2.4) — or the list is truncated, a condition this field does not cover (its sibling `null_patterns.coverage_method`, §2.2.10, excludes truncation the same way) | `values`/`values_coverage`; `values_coverage` itself against `1.0`, for the second cause |
| `distribution` | The same two causes as `values`, which it is derived from wherever a value list exists | `classification`, then `inferred.looks_like` |
| `frequencies` | Forbidden outside `numeric` and `temporal` (§2.2.3) | `classification` |
| `range`, `range.span_days`, `percentiles` | Forbidden for this classification (§2.2.3) — or withheld, since `redacted: drop` emits no literal and a bound is nothing but a literal (§2.2.3 †) | `classification`, then `redacted` |
| `freshness` | Forbidden outside `temporal` (§2.2.3). Never withheld: it survives every redaction primitive, coarsened rather than omitted (§2.2.9) | `classification` |
| `unrepresentable` | No emitted bound lies outside years 0001–9999 — or the column emitted no bound the marker could name (§2.2.4) | `range` and `percentiles`; absent bounds leave nothing to mark |
| `rows_scanned` | The file carries no `scope`, so `row_count` is the population of every count in it (§2.2.8) | the file's own `scope` block; the two agree or the artifact is inconsistent |
| `physical_name` | The catalog's own spelling is the map key already (§2.2.4) | nothing further — the absence is the statement |
| `collation` | The column sets no explicit collation, or sets one identical to the connection default (§2.2.4) | not distinguishable, and the answer is the same either way: `default_collation` (§2.5) |
| `physical_layout_key` | The column is not named in this file's `physical_layout.keys` (§2.2.11) | that list; the two surfaces agree or the file contradicts itself |
| `inferred.looks_like` | Detection ran and no pattern reached the threshold (§4.1.3) — or never runs on this classification (§4.1.5) — or the winning pattern was `numeric_string` and the column's own SQL type is already numeric (§4.1.5) | `classification`, then `sql_type` for the third cause |
| `inferred.sampled`, `inferred.matched` | `looks_like` is absent; the pair describes that verdict alone and is emitted only beside it (§2.2.4, §4.1.3) | `inferred.looks_like` |
| `inferred.sensitivity` | Nothing was detected. Never that the column is safe to publish (§4.4.2) | not distinguishable, and not intended to be |
| `inferred.epoch_unit` | Neither bounds nor sampled values fell inside one window (§4.5.1) — or no evidence rule reaches this classification (§4.5.2) | `classification` |
| `inferred.candidate_key` | `cardinality_ratio` does not clear the §4.2 threshold | `cardinality_ratio`; recomputable, and a validator recomputes it |
| `inferred.candidate_key_exception` | `cardinality_ratio` is exactly `1.0`, or `candidate_key` is itself absent (§4.2) | `cardinality_ratio` and `inferred.candidate_key` |
| `inferred.fk_candidate` | Reserved; a v1 producer MUST NOT emit it (§4.3) | nothing — it is absent from every v1 artifact |
| `redacted` | No `redact` rule matched this column, or the connection configures none (§2.2.9) | `redaction_rules_configured` in the manifest (§2.5) |
| `sketch` | The column is not a join-key participant on either side of an edge in this table's own `relationships.yaml`; its type has no canonical encoding (§2.2.14); it carries a `redacted` marker; or its file carries a top-level `scope` block | `relationships.yaml`'s `refers_to`/`referenced_by`, `sql_type`, `redacted`, and the file's own `scope` - all four, since any one of them alone can explain the absence |

### 7.3 Absent blocks and files

Shapes above column grain, which the §2.2.3 matrix does not reach.

| Absent shape | Candidate causes | Distinguishable by |
|---|---|---|
| `catalog_only` | The producer issued a query for this object, the ordinary case (§2.2.15) | nothing further — the absence is the statement |
| `row_count`, `row_count_method` | The file carries `catalog_only` (§2.2.15) - no query was issued to obtain either | the file's own `catalog_only` marker |
| `scope` | The producer read every row. The absence is an assertion, not a gap (§2.2.8) | nothing further — the absence is the statement |
| `null_patterns` | No column in the file carries a null (§2.2.10) | every column's `null_count`; the block MUST be present when any is non-zero |
| `null_patterns.coverage_method` | The census was truncated by the producer's own cap, a different condition the field does not cover (§2.2.10) - or an untruncated census was written by a producer predating the field, the only remaining cause once truncation is ruled out | `patterns`'s own length against the cap; ruling that out leaves only a producer predating the field |
| `physical_layout` | The table declares no clustering or partitioning key, the adapter cannot express the concept for this engine, or the file carries `catalog_only` (§2.2.15), whose absence here needs no further explanation. Never "not checked" (§2.2.11) | the file's own `catalog_only` marker rules in the third cause; the first two remain not distinguishable from each other |
| `grain` | The artifact was written by a producer predating the field (§2.2.12). A conforming producer always emits the block | `dbprint_version` in the manifest, at best |
| `grain.search` | The measured probe never ran: the file carries `scope`, the file carries `catalog_only` (§2.2.15), a count is zero, or a column already carries `inferred.candidate_key` (§2.2.12) | the file's own `catalog_only` marker for the second cause - `row_count` does not exist to read under it; otherwise `scope`, `row_count`, and every column's `inferred.candidate_key` distinguish the rest |
| `dependencies` | The artifact was written by a producer predating the field (§2.2.13), or the file carries `catalog_only` (§2.2.15), which forbids the block outright. A conforming, queried producer always emits the block | the file's own `catalog_only` marker for the second cause; `dbprint_version` in the manifest, at best, for the first |
| `eligible_target` | The producer never ran the declared-keys pre-pass (§2.3.1, §2.3.7) | nothing further — and its absence is what makes `referenced_by: []` ambiguous |
| an entry in `referenced_by` | The referencing table lies outside the print's selectors, so the two-pass resolution never saw it (§2.3.6) | the manifest's `selectors` (§2.5), which names what was left out |
| `observed` on a `refers_to`/`referenced_by` entry | The edge is composite, or either endpoint carries no `cardinality` this run could measure (§2.3.10) | the entry's own `column`/`target_column` array length; the endpoint's own `cardinality` |
| `statistics.yaml` | The object is a plain view written by a producer predating `catalog_only` (§2.2.15) - every plain view carries a catalog-only file from that point forward | the manifest's `dbprint_version`, at best |
| `relationships.yaml` | The object is a plain view, for which the file is optional (§1.4) | the manifest's `artifacts` for that table |
| `description.md`, `statistics.annotations.yaml`, `relationships.annotations.yaml` | No human wrote one. Producers never author them (§2.4, §2.7) | nothing — absence carries no meaning beyond "unwritten" |

### 7.4 Empty is not absent

The format emits some collections empty rather than omitting them, and an empty collection is a measurement with its own causes.

| Shape | Candidate causes | Distinguishable by |
|---|---|---|
| `values: []` | The column has no non-null rows, or the table has none at all (§2.2.7). `values_coverage` is `1.0` in both cases — a list with nothing to carry covers all of it | `row_count`, `null_rate`, and `scope.rows_scanned`: an empty scanned set (§2.2.7) is a third case and keeps the table's own `row_count` |
| `referenced_by: []` | Nothing references the object and something could have; or nothing could, because it supplies no declared-unique column (§2.3.8); or the pre-pass never ran | `eligible_target`: `true`, `false`, and absent are the three answers (§2.3.7) |
| `refers_to: []` | The object declares no outgoing foreign key and the naming rule resolved no inferred edge (§2.3.8) — or the producer ran no inference at all | `eligible_target`'s absence says inference never ran (§2.3.7). Where it is present, the two remaining causes are not distinguishable |
| `grain.keys: []` | Nothing declared identifies a row, and the measured probe either found nothing, was cut short, or never ran (§2.2.12) | `grain.search`: absent means nobody looked, `exhausted: false` means the look was incomplete, `exhausted: true` means it found nothing |
| `dependencies: []` | No candidate pair cleared the 95% threshold, or the search never ran at all - the file carries `scope`, or `row_count` is `0` (§2.2.13) | `scope` and `row_count`: both absent/nonzero means the search ran and found nothing; either present means it never ran |

---

## Appendix A — Example print

[`examples/`](examples/) contains a complete reference print: realistic per-table directories plus the manifest and diff files.

---

## Cross-references

The artifacts below are normatively defined by this spec:

- [`examples/`](examples/) — reference per-table directories
- `spec/v1/*.schema.json` — JSON Schemas
- `tests/conformance/` — pytest validation suite
