# dbprint format v1 — reference example

This directory holds two example print directories. `production/` is a complete example, produced by `scripts/gen_reference_example.py` against a PostgreSQL database seeded from `scripts/sql/`, and regenerated with `just example`. `vocabulary/` is a smaller, second example produced by `scripts/gen_vocabulary_example.py` and regenerated with `just example-vocabulary`, carrying the `looks_like` shapes `production/`'s seed-bank domain has no honest column for — see [`vocabulary/README.md`](vocabulary/README.md). The rest of this file describes `production/`.

The producer wrote every artifact in it; the files it did not write are `description.md`, `statistics.annotations.yaml` and `relationships.annotations.yaml`, all user content the generator places before the runs so the manifest records them. `tests/conformance/test_reference_example.py::TestProducerAgreement` regenerates the tree and compares it against the committed one, so what is here is what the producer emits.

Two things are frozen, and everything else in the tree is a real producer read. The generator rewrites every `generated_at`, `profiled_at` and `scanned_at` to a fixed instant before committing, which is why they read identically everywhere and why a regeneration is a no-op diff of nothing but clocks — the freeze reaches only those keys, never a column's own `range` or `percentiles`. Separately, the golden comparison in `TestProducerAgreement` also strips the `freshness` block on both sides before comparing (both `max_age_days` and its `classification`), since either is measured against the moment of the run and would otherwise fail on nothing the producer chose. Every other number, including `span_days` and the bounds it is computed from, is real and internally consistent.

It demonstrates the key constructs of the format spec at [`../SPEC.md`](../SPEC.md).

## Source

Two schemas:

- **`seedbank`** — a seed bank and herbarium accession registry: taxa, field collectors,
  cold-storage vaults, accessions, germination trials, specimen images.
- **`fixture`** — one table, `shape_probe`, that carries the few format shapes the seed-bank
  domain has no honest column for (a raw device IPv4, JSON held as plain text). Its name says
  what it is.

## What's demonstrated

**Adapter**: PostgreSQL. Fully-qualified names are two-part — `seedbank.accession`, not `arboretum.seedbank.accession` — and lowercase, the path-case convention the format addresses objects by. The print spans **two schemas**, so the `<schema>/<table>/` namespace path is exercised at two distinct depths in one connection.

**Ten objects:**

| Object | Type | What it carries |
|---|---|---|
| `seedbank.taxon` | table | The self-referential FK (`parent_taxon_id`), a `dominant_value` and an `imbalanced` distribution, two `text`+`prose` columns |
| `seedbank.collector` | table | All three redaction primitives, three of the five `sensitivity` categories, `uuid`/`email`/`phone`/`country_code`/`postal_code` shapes |
| `seedbank.vault` | table | The composite primary key (`vault_id`, `shelf_code`) that `accession` references compositely; two `time`-typed columns |
| `seedbank.accession` | table | The busiest table: `jsonb`, a composite FK, `foreign_key_candidate` on four columns, `statistics.annotations.yaml` on six columns, a path-valued relationship endpoint authored over `traits` |
| `seedbank.germination_trial` | table | The deliberately inferred FK — `collector_id` names a `collector` row with no declared constraint; a value-grain note on `medium`'s `control` entry |
| `seedbank.specimen_image` | table | `path`, `filename`, `content_type`, `base64` — every file-shaped `looks_like` pattern |
| `seedbank.storage_reading` | table | The only empty table (`row_count: 0`), the only declared `physical_layout` — a `partition` mechanism on `reading_date` — and the only object whose `grain` is declared rather than searched |
| `seedbank.accession_summary` | plain view | The only `catalog_only` object: column names and types with no measurement behind them (SPEC §2.2.15), and an empty `grain.keys`. It originates two inferred edges; one is annotated `verdict: rejected` |
| `seedbank.germination_by_taxon_mv` | materialized view | Profiled like a table, and the object the per-table freshness override applies to; also originates an inferred edge, independent of the view's |
| `fixture.shape_probe` | table | `ip`, `json`-as-text, `bytea`, and an array — the one table with **both** `refers_to` and `referenced_by` empty (SPEC §2.3.7's "no FKs at all") |

**Per-table freshness** (SPEC §2.5): a `rules` entry gives `germination_by_taxon_mv` a threshold of 30 days where the connection sets 1; its manifest entry records the 30 it resolved to, every other object records the connection's 1.

**`description.md` optionality** (SPEC §2.4): present on `accession`, `collector`, `germination_trial` and `taxon`, absent on the other six, listed among each table's artifacts in the manifest. A manifest that disagrees with the files beside it is what SPEC §2.5 tells a consumer to treat as an inconsistent print, so the generator seeds every committed file before the run that records the manifest rather than after it.

**`statistics.annotations.yaml` at column and value grain** (SPEC §2.7.1): present on `accession` (six columns), `accession_summary` (two), `germination_trial` (one), `taxon` (two), `vault` (two) and `germination_by_taxon_mv` (two), absent elsewhere, seeded the same way as `description.md` and for the same reason. `accession_summary` is the case the format's stale-key check does not run for a plain view: it has no `statistics.yaml`, so its annotated columns are the only column names this print has for it at all. `germination_by_taxon_mv` is the opposite case: a matview does carry `statistics.yaml`, so its annotated columns are checked against real column names the same way a table's are. `germination_trial.medium` carries a value-grain note instead of a column-grain one: `control` is a real, exhaustively-published domain member, and the note records that it denotes a no-medium control group rather than a growth medium — a distinction no statistic alone can draw.

**`relationships.annotations.yaml` at edge grain** (SPEC §2.7.2): present on `accession` and `accession_summary`. `accession_summary` carries a second inferred edge beyond the one above, `germination_trial_id` → `germination_trial.trial_id` — the naming rule resolves it on the column's name alone, the annotation marks it `verdict: rejected`; `dbprint context` renders the correction next to the producer's own inference rather than hiding either. `accession` carries a path-valued endpoint (SPEC §2.3.9) with no producer counterpart: `traits` occasionally nests a `reclassified_taxon_id` key, and the annotation states the edge that key implies, directly into `taxon.taxon_id`, since a producer never infers into a JSON payload.

**Redaction, on five columns, by three mechanisms** (SPEC §2.2.9): the project-wide `redact: [{sensitivity: [contact], with: mask}]` default catches `collector.email`, `.phone` and `.institution_email` — the first two through the strong `contact` name tokens §4.4 recognises, the third through its detected `looks_like: email` shape alone, since `institution_email` is not itself one of those names. Two connection-level rules add the other two primitives, each keyed on a column glob rather than a category — the escape hatch CONFIG.md describes for a column detection misses: `collector.institution` (`hash`) and `collector.street_address` (`drop`). See the Redaction section below for what each substitution actually looks like.

**A `diff.yaml` carrying a real event**: one `column_added` for `accession.storage_temperature_c`, `columns_added: 1`, `tables_modified: 1`, `unchanged_tables: 8`, `unevaluated_tables: 1`. The generator applies schema drift to the database between the two runs it makes, so this file is a comparison of two states of a database rather than an authored illustration.

The single unevaluated object is `accession_summary`, the print's plain view (SPEC §2.6.4): its `statistics.yaml` is `catalog_only`, so it publishes column names without measurements to compare, and the format defines no DDL comparison — its whole body could be rewritten with nothing here to detect it. It is counted apart from the eight the diff read and found equal rather than folded in with them. `germination_by_taxon_mv` is the contrast the pair exists to draw — a matview does carry `statistics.yaml`, so it is compared like a table and sits among the eight.

The same drift also sets a column comment, and no `comment_changed` event appears for it. That is the format's boundary rather than an omission: comments, indexes and column defaults are carried only by `ddl.sql`, which the format does not require a consumer to parse, so a baseline read back from a committed print cannot know them and the comparison withholds those kinds rather than guessing. The kinds a print can answer for are the ones a `statistics.yaml` and a `relationships.yaml` carry between them: columns added, removed, retyped or made nullable, relationships, and statistics.

## Coverage matrix

All eight column classifications appear:

| Classification | Where |
|---|---|
| `foreign_key_candidate` | `accession.taxon_id`, `.collector_id`, `.vault_id`, `.shelf_code`; `accession_summary.accession_id`, `.germination_trial_id`; `germination_by_taxon_mv.taxon_id`; `germination_trial.accession_id`, `.collector_id`; `specimen_image.accession_id`; `storage_reading.vault_id`, `.shelf_code`; `taxon.parent_taxon_id` |
| `categorical` | `fixture.shape_probe.probe_id`, `.logger_ipv4`, `.json_text`; `accession.provenance_country`, `.storage_temperature_c`; `collector.institution`, `.institution_email`, `.street_address`, `.postal_code`, `.country_code`; `germination_by_taxon_mv.trial_year`; `germination_trial.medium`; `specimen_image.content_type`; `storage_reading.reading_id`, `.reading_date`, `.temperature_c`; `taxon.rank`; `vault.vault_id`, `.shelf_code`, `.site_name`, `.target_temperature_c`, `.opens_at`, `.closes_at` |
| `boolean` | `taxon.is_endangered` |
| `json` | `accession.traits` |
| `temporal` | `accession.collected_on`, `.received_at`; `accession_summary.collected_on`; `collector.hired_on`; `germination_trial.started_on`, `.observed_at`; `specimen_image.captured_at`; `taxon.created_at` |
| `numeric` | `accession.accession_id`, `.viability_pct`, `.seed_count`; `accession_summary.viability_pct`; `germination_by_taxon_mv.total_sown`, `.total_germinated`; `germination_trial.trial_id`, `.sown_count`, `.germinated_count`; `specimen_image.image_id`, `.byte_size`; `taxon.taxon_id` |
| `text` | `accession.accession_code`, `.sheet_number`, `.catalogue_url`, `.field_notes`; `accession_summary.accession_code`, `.scientific_name`, `.vernacular_name`, `.collector_name`; `collector.collector_id`, `.full_name`, `.email`, `.phone`; `specimen_image.storage_path`, `.file_name`, `.thumbnail_b64`; `taxon.scientific_name`, `.vernacular_name`, `.description` |
| `unsupported` | `fixture.shape_probe.payload_bytes` (`bytea`), `.tag_list` (`text[]`) |

Uniqueness is not one of the eight: `inferred.candidate_key` (SPEC §4.2) rides whichever classification a column's type and cardinality already earned it, so `accession.accession_id` (unique, `numeric`) and `collector.collector_id` (unique, `text`) both carry the flag alongside their type's own full field set rather than losing it to a ninth classification.

`vault.opens_at`/`.closes_at` are `TIME`-typed columns worth calling out on their own: SPEC §3.2's priority order checks `cardinality <= enumeration_threshold` before it checks for a temporal type, and with only two distinct opening times across 48 shelves, both columns classify `categorical` rather than `temporal` — a real instance of a temporal-typed column losing to a lower-priority rule on cardinality alone, not a misconfiguration.

All four `distribution` values appear: `dominant_value` (`taxon.rank`, `species` at 288/300 = 96%), `imbalanced` (`taxon.parent_taxon_id`, 36 children under each of eight genus parents against 2 under each of four family parents), `long_tail` (ten columns, `accession.taxon_id` among them: 300 distinct taxa referenced roughly evenly across 2,500 accessions, so no top-30 slice of the list can carry much of the total), and `uniform` (the majority of the remaining categorical and numeric columns).

Value lists appear in all three shapes:

- **Exhaustive** — every `categorical` column above, each with `values_coverage: 1.0`
- **Truncated** — every `foreign_key_candidate` column above (30 entries against the connection's `top_n_values: 30`); `accession.sheet_number` similarly
- **Empty** — `accession.storage_temperature_c`, the drift-added column, 100% null: `values: []`, `values_coverage: 1.0`, `classification: categorical`, `cardinality: 0` — SPEC §2.2.7's all-null-column resolution, exercised by a real schema change rather than an authored fixture

## Inferred semantics

15 of the format's 32 `looks_like` patterns appear; the remaining 17 have no honest column in this domain and are demonstrated in `vocabulary/` instead (see [`vocabulary/README.md`](vocabulary/README.md)):

| Pattern | Where |
|---|---|
| `uuid` | `collector.collector_id`, `accession.collector_id`, `germination_trial.collector_id` |
| `email` | `collector.email`, `.institution_email` |
| `url` | `accession.catalogue_url` |
| `content_type` | `specimen_image.content_type` |
| `path` | `specimen_image.storage_path` |
| `ip` | `fixture.shape_probe.logger_ipv4` |
| `country_code` | `collector.country_code`, `accession.provenance_country` |
| `postal_code` | `collector.postal_code` |
| `phone` | `collector.phone` |
| `json` | `fixture.shape_probe.json_text` — JSON held as `text`, distinct from `accession.traits`, which is `jsonb` and classifies `json` directly |
| `base64` | `specimen_image.thumbnail_b64` |
| `numeric_string` | `accession.sheet_number` and every surrogate-key column whose values happen to render as digits (`accession_id`, `image_id`, `probe_id`, ...) |
| `filename` | `specimen_image.file_name` |
| `prose` | `taxon.description`, `accession.field_notes` |
| `iso8601_date` | `germination_by_taxon_mv.trial_year` — the one native `date` column at categorical cardinality; SPEC §3.2's `categorical`-before-`temporal` priority is what reaches it, and the reason no other native `date` column in this print does (§4.1.5 excludes `temporal` from `looks_like` detection) - `collector.hired_on`, `accession.collected_on` and `germination_trial.started_on` all classify `temporal` and carry none |

`collector.postal_code` is UK-formatted by construction: SPEC's `postal_code` detector recognises the UK, Canadian and Netherlands shapes only (`spec/looks_like.py`), so a US five-digit ZIP or any other locale would not fire it — the fixture's addresses stay UK-shaped for exactly this reason, not because the domain is set in the UK.

Four of the twelve `sensitivity` categories appear: `personal_name` (`collector.full_name`), `postal_address` (`collector.street_address`), `contact` (`collector.email`, `.phone`, `.institution_email`), and `online_identifier` (`fixture.shape_probe.logger_ipv4`, from its own `looks_like: ip` shape - the column name carries no online-identifier token) — `geolocation`, `date_of_birth`, `national_id`, `financial_account`, `credential`, `health`, `demographic` and `employment` have no honest column in this domain and are demonstrated in `vocabulary/` instead. The three contact-adjacent columns reach `sensitivity: contact` by two different routes: `email` and `phone` are both in the name list §4.4 recognises, so their shape adds no further evidence than their name already gave; `institution_email` is not, so the category comes from the detected `looks_like: email` shape alone.

`accession.traits` and `fixture.shape_probe.json_text` both carry JSON, and neither carries a `looks_like` alongside its classification: SPEC §2.2.3's field matrix forbids `inferred.looks_like` on `json`-classified columns outright (the JSON claim is what the `classification` field itself already says), which is exactly why `json_text` is stored as `text` rather than `jsonb` — a `jsonb` column can never demonstrate the `looks_like: json` pattern, only a text column holding JSON-shaped strings can.

## Relationships

- **A self-referential FK**: `taxon.parent_taxon_id` → `taxon.taxon_id`, `ON DELETE SET NULL`. A three-level hierarchy (`family` → `genus` → `species`) built from one column; SPEC §2.3.7's shape is exact — `target_table` equals the top-level `table`, and both `refers_to` and `referenced_by` carry an entry for it.
- **A composite FK**: `accession.(vault_id, shelf_code)` → `vault.(vault_id, shelf_code)`, single entry, both `column` and `target_column` arrays of length two, position-paired (SPEC §2.3.4). `vault`'s own `referenced_by` carries the reciprocal two-column entry.
- **Four inferred edges** (SPEC §2.3.8), each a different shape: `germination_trial.collector_id` → `collector.collector_id` is the deliberate one — the column is named `collector_id`, `collector` is in scope and declares a single-column primary key, and no FK constraint covers it, so the naming rule derives the edge with no help from a view or matview. `accession_summary.accession_id` → `accession.accession_id` and `germination_by_taxon_mv.taxon_id` → `taxon.taxon_id` originate from a plain view and a materialized view respectively — neither object type can declare a constraint, so both edges exist only because the naming rule runs over their columns too (SPEC §2.3.7). The fourth, `accession_summary.germination_trial_id` → `germination_trial.trial_id`, is the naming rule reaching a column that names a code rather than a key, and it is the one edge in this print carrying a `relationships.annotations.yaml` `verdict: rejected` (SPEC §2.7.2); the other three are left unannotated because they are correct. All four carry `detection: inferred`, no `on_delete`/`on_update` beyond `NO ACTION`, and no `constraint_name`.
- **A path-valued endpoint** (SPEC §2.3.9), authored rather than measured: `accession.traits` is `jsonb` with no fixed key set, and its `relationships.annotations.yaml` states that the `reclassified_taxon_id` key it sometimes carries addresses `taxon.taxon_id` — an edge no producer inference reaches, since a producer never looks inside a JSON payload.
- **Empty `refers_to`**: `collector`, `vault` — neither references anything in scope.
- **Both empty**: `fixture.shape_probe` — no declared or inferable FK in either direction, SPEC §2.3.7's "no FKs at all" case, on a real table rather than an authored one.

## Not demonstrated

- The `time`-of-day-only edge case in SPEC §2.2.4 (`range.span_days: 0`, `freshness.max_age_days: 0` for a date-less temporal type): `vault.opens_at`/`.closes_at` are `TIME`-typed, but with only two distinct values across 48 rows they classify `categorical` before the temporal branch is ever reached (see Coverage matrix above) — the column type is present, the classification path this rule governs is not.
- A `live` or `stale` `freshness.classification`. Every column carrying a `freshness` block classifies `dormant`, because their seed dates are fixed historical anchors (2018-2020) rather than an offset from the run's own clock. A seed date computed from the clock would move the data itself on every regeneration, not just its freshness bucket; a fixed anchor that reads `dormant` keeps reading `dormant` indefinitely, while one engineered to read `live` would not.
- An FK whose target lies outside the print — every declared and inferred edge in this example targets an in-scope object.
- A narrowed read: there is no `scope` block, and every table reports `row_count_method: exact`.

## Redaction

Five columns on `collector`, by three primitives — every one of them low-cardinality or truncated-list enough to publish a value for the primitive to act on (a marker on a column with nothing published would announce a redaction of nothing; see `test_a_marker_sits_only_where_a_literal_was_published`).

- **`email`, `phone` and `institution_email` carry `redacted: mask`.** Every entry in each `values` list holds `[redacted]`; counts, `cardinality`, `values_coverage` and `distribution` are the same numbers an unredacted run produces. `email` and `phone` are unique per row — SPEC 4.2 does not withhold their value list on that account, which is exactly what makes them reachable to redact at all.
- **`institution` carries `redacted: hash`.** Every entry holds a salted digest instead — sixteen hex characters, distinct per distinct input, so a consumer can tell two institutions apart without learning either name. The salt is fixed in the generator (`FIXTURE_REDACTION_SALT`) rather than drawn from the environment: a committed digest has to be reproducible across regenerations, and publishing this particular salt is safe only because every value it ever digests is generated from a row ordinal — reversing a digest recovers a synthetic institution name, never a real one.
- **`street_address` carries `redacted: drop`.** Its `values` entries carry `count` only — no `value` key at all, the one primitive where "redact the literal" and "omit the literal" are the same operation. `range` and `percentiles` would be affected by the same rule, but this column is `categorical` and never had them to begin with.

Two mechanisms produced these five markers. `email`, `phone` and `institution_email` are all caught by the project-wide `sensitivity: [contact]` default — the first two through their strong name tokens, the third through its detected `looks_like: email` shape alone. `institution` and `street_address` are caught by connection-level rules keyed on a column glob (`columns: ["seedbank.collector.institution"]`) rather than a detected category — the escape hatch for a column detection misses, per CONFIG.md. `institution` in particular carries no `inferred.sensitivity` at all (its plain organisation name matches no §4.4 token and no shape), so it could only ever be reached by a glob.

## Conformance

This directory is the primary positive fixture for the conformance suite at `tests/conformance/`. Validating it MUST return zero error-severity issues.
