# dbprint format v1 — vocabulary example

A second, small print directory alongside [`../production/`](../README.md), produced by
`scripts/gen_vocabulary_example.py` against a real PostgreSQL database and regenerated
with `just example-vocabulary`. It exists for one reason: seventeen `looks_like` patterns have
no honest column in the seed-bank domain `production/` demonstrates — a bank card number,
an IBAN, a MAC address are not things a seed bank's schema would ever hold — so this tree
carries a single table built to demonstrate exactly those seventeen, plus one already shown in
`production/` for a real primary key. Beyond `looks_like`, this tree also demonstrates the
`inferred.epoch_unit` field's per-value evidence rule, and nine `sensitivity` categories:
`national_id`, `date_of_birth`, `health`, `demographic` and `employment` each get their own
column, and `financial_account`/`online_identifier`/`geolocation`/`credential` need none of
their own, since they corroborate from shapes
`pan`/`iban_value`/`mac_address`/`device_id`/`coordinates`/`bearer_token` already carry - see
below.

Same golden-comparison discipline as `production/`: `tests/conformance/test_reference_example.py`
regenerates the tree against a fresh database and compares it byte-for-byte against the
committed one, so what is here is what the producer emits, not a hand-edited fixture.

## Source

One schema, one table: `public.shapes`, 40 rows, one column per demonstrated pattern plus
its `row_id` primary key. Every value is a published test vector, a standard registry
entry, or an invented placeholder — never framed as data observed in the wild: Luhn-valid
non-issuable card numbers, the documentation IBANs ISO 13616 and Wikipedia's own worked
examples use, RFC 4122's example UUID inside a URN, locally-administered MAC addresses
(RFC 7042's documentation range), IANA zone names, ISO 4217 currency codes, and hand-built
JWTs carrying no real signing key, semantic version strings in their tag-prefixed and
prerelease/build forms, published ISBN/EAN/UPC test identifiers, VINs satisfying the
federal VIN regulation's own check digit (49 C.F.R. 565.15), IMEIs whose Reporting
Body Identifier is genuinely allocated (GSMA's own IMEI allocation guidelines), and
epoch-second instants chosen to avoid `isbn`'s own check digit by coincidence, invented
tax-reference numbers naming no real jurisdiction's scheme, invented birth dates, standard
ABO/Rh blood-type codes, and abstract placeholder group labels — never a real ethnicity,
religion or other protected-category name. Each column cycles three such values across its
40 rows, which is why every column classifies
`categorical` with `cardinality: 3` and an exhaustive, `values_coverage: 1.0` value list —
the small-fixture case `production/` demonstrates the *classification* mechanics of at
greater length; this tree only needs a categorical column to carry each pattern's value.

## What's demonstrated

| Column | `looks_like` | Values |
|---|---|---|
| `row_id` | `numeric_string` | Row ordinal — the same pattern `production/` demonstrates on every surrogate key |
| `pan` | `card_number` | Three Luhn-valid, non-issuable test card numbers - also `sensitivity: financial_account`, from the shape alone (see below) |
| `iban_value` | `iban` | ISO 13616 documentation IBANs (Germany, UK, France), check digits valid on all three - also `sensitivity: financial_account`, from the shape alone (see below) |
| `bic_value` | `bic` | An 8-character and an 11-character (branch-suffixed) BIC, both valid shapes |
| `digest` | `hex` | Two `#`-prefixed hex triplets and one bare 7-character hex string — SPEC 4.1's `#`-prefix is optional, not required |
| `mac_address` | `mac_address` | Colon- and hyphen-delimited forms, both locally-administered (RFC 7042) |
| `coordinates` | `latlon` | A comma-paired lat/lon in three different sign/precision combinations, including `0.0,0.0` |
| `resource_urn` | `urn` | Three RFC 8141 URNs across different namespaces (`isbn`, `uuid`, an invented `example` namespace) |
| `duration` | `iso8601_duration` | A full six-component duration, a minutes-only duration, and a weeks-only duration — the three component groupings ISO 8601 allows |
| `tz_name` | `timezone` | Two IANA zone names and `UTC`, all present in the `tzdata` snapshot the producer resolves against |
| `currency` | `currency_code` | Three active ISO 4217 alpha codes |
| `bearer_token` | `jwt` | Three HS256-shaped tokens with distinct payloads, each a real base64url-encoded JOSE header carrying `alg` |
| `package_version` | `semver` | A tag-prefixed release, a numeric prerelease, and an alphabetic prerelease-plus-build form - the three arms this pattern was added to cover |
| `book_code` | `isbn` | A 13-digit ISBN, a 10-digit ISBN, and a 10-digit ISBN with an `X` check character |
| `barcode` | `ean` | Two 13-digit EANs (one also Luhn-valid - the worked collision `isbn`/`ean` outranking `card_number` exists for) and a UPC-A |
| `vehicle_id` | `vin` | Three VINs valid under the federal VIN regulation (49 C.F.R. 565.15), one with an `X` check digit |
| `device_id` | `imei` | Three Luhn-valid IMEIs with allocated Reporting Body Identifiers, one inside JCB's card-issuer range - the value a `card_number` IIN table would still misclaim |
| `logged_at` | `iso8601_datetime` | Three ISO 8601 datetime strings, distinct from `date_of_birth`'s bare-date shape below - a pattern no `production/` column reaches, since every native timestamp there classifies `temporal` and SPEC 4.1.5 runs no detection on that classification |

Every value above was checked individually against `detect()` before being written into
the seed data, so each row exercises the same pattern the table claims.

## Also demonstrated: `inferred.epoch_unit`

| Column | `inferred.epoch_unit` | Values |
|---|---|---|
| `event_timestamp` | `seconds` | Three consecutive epoch-second instants, chosen to avoid `isbn`'s own check digit - each still reports `looks_like: numeric_string` unchanged |

This is `epoch_unit`'s **per-value evidence rule** (SPEC 4.5.2), which reaches `categorical`
the same way `looks_like` does. The rule's other half - the **bounds rule** over a `numeric`
column's `range` - needs a cardinality above the enumeration threshold (50), which this
table's 40 rows cannot carry without breaking every other column's `cardinality: 3`
demonstration; it is proven instead by the engine test suite
(`tests/engine/test_orchestrator.py::TestEpochUnit`).

## Also demonstrated: `sensitivity: national_id`

| Column | `inferred.sensitivity` | Values |
|---|---|---|
| `tax_id` | `national_id` | Three invented tax-reference numbers - the column name alone is unambiguous, so no value shape is required |

`tax_id` is a strong `national_id` token (SPEC 4.4.1): the category is carried from the
column name alone, the same treatment `first_name` gets from `personal_name`.

## Also demonstrated: `sensitivity: date_of_birth`

| Column | `inferred.sensitivity` | Values |
|---|---|---|
| `date_of_birth` | `date_of_birth` | Three invented birth dates - the column name alone is unambiguous, so no value shape is required |

`date_of_birth` is a strong `date_of_birth` token (SPEC 4.4.1), the same unambiguous-name
treatment `tax_id` gets from `national_id` above. The column also reports
`looks_like: iso8601_date`, since its values are ISO date strings - the two axes are
independent (SPEC 4.4's opening paragraph): `sensitivity` and `looks_like` are not
alternatives.

## Also demonstrated: `sensitivity: employment`

| Column | `inferred.sensitivity` | Values |
|---|---|---|
| `annual_salary` | `employment` | Three invented salary figures, stored as text - the column name alone is unambiguous, so no value shape is required |

`annual_salary` is a strong `employment` token (SPEC 4.4.1), the same unambiguous-name
treatment `tax_id` and `date_of_birth` get. It also reports `looks_like: numeric_string`,
since its values are digit-only strings - the classification does not matter to this axis,
only the name does.

## Also demonstrated: `sensitivity: health` and `sensitivity: demographic`

| Column | `inferred.sensitivity` | Values |
|---|---|---|
| `blood_type` | `health` | Three ABO/Rh blood-type codes - the column name alone is unambiguous |
| `ethnicity` | `demographic` | Three abstract placeholder group labels, never a real ethnicity name - the column name alone is unambiguous |

`blood_type` and `ethnicity` are both strong tokens on their own axis (SPEC 4.4.1). The two
categories are separate values, not one merged value: `detect()` never returns both for one
column, and `RedactRule.covers()` targets by exact value membership - so a `redact` rule
naming only `health` can never reach an `ethnicity`-flagged column. Category independence is
asserted directly by `tests/spec/test_sensitivity.py::TestDemographic`'s
`test_a_health_only_rule_leaves_demographic_columns_reachable`; the redaction primitive
itself (`drop` removes literals, keeps counts) is the generic mechanism
`tests/engine/test_redaction.py` already proves for every category.

## Also demonstrated: `sensitivity: financial_account`

| Column | `inferred.sensitivity` | Why |
|---|---|---|
| `pan` | `financial_account` | Its own `looks_like: card_number` corroborates - a Luhn check under the value, not a name heuristic |
| `iban_value` | `financial_account` | Its own `looks_like: iban` corroborates - a mod-97 check under the value |

Neither column's name (`pan`, `iban_value`) is a strong `financial_account` token; both are
flagged purely because their values carry a checksummed shape, the same mechanism SPEC
4.4.3 already gives `contact` for `email`/`phone` - the one category on this axis whose
value corroboration is arithmetic rather than a heuristic (SPEC 4.4.1).

## Also demonstrated: `sensitivity: online_identifier`

| Column | `inferred.sensitivity` | Why |
|---|---|---|
| `mac_address` | `online_identifier` | Both a strong name token and its own `looks_like: mac_address` corroborates |
| `device_id` | `online_identifier` | A strong name token; its own `looks_like: imei` is unrelated evidence on the other axis |

Both columns already existed for the `looks_like` axis; neither needed a new fixture value
to carry the second signal. `mac_address` demonstrates the shape-reading path SPEC 4.4.3
gives this category (the same mechanism `contact` and `financial_account` use); `device_id`
demonstrates the name-only path. A `session_token` column is deliberately not demonstrated
here - it is a `sensitivity: credential` example instead, since a live session token is a
bearer credential first and the two vocabularies do not claim the same column.

## Also demonstrated: `sensitivity: geolocation`

| Column | `inferred.sensitivity` | Why |
|---|---|---|
| `coordinates` | `geolocation` | Its own `looks_like: latlon` corroborates - the column name carries no geolocation token |

`coordinates` already existed for the `looks_like` axis; it needed no new fixture value to
carry the second signal. This is the shape-only path - a `latitude`/`longitude` pair split
across two numeric columns would instead be the name-only path, which this fixture cannot
carry (no sample is drawn for a `numeric` column, so no shape evidence exists there either
way) - see "Not demonstrated" below for where that path is covered.

## Also demonstrated: `sensitivity: credential`

| Column | `inferred.sensitivity` | Why |
|---|---|---|
| `bearer_token` | `credential` | Its own `looks_like: jwt` corroborates unconditionally - the column name carries no credential token |

`bearer_token` already existed for the `looks_like` axis; it needed no new fixture value to
carry the second signal. This is the unconditional shape path (`jwt` flags independent of
the name, the same mechanism `contact` gets from `email`/`phone`) - see "Not demonstrated"
below for the strong-name path, the weak-name-plus-`hex` path, and the settings-table
negative.

## Not demonstrated

Everything `production/` already covers on its own: classifications other than
`categorical`, distributions other than `uniform`, redaction, relationships,
per-table freshness overrides, `description.md`/`statistics.annotations.yaml`, the other 16
`looks_like` patterns — see [`../README.md`](../README.md) for those — `epoch_unit`'s
bounds rule, per the note above — `national_id`'s weak-tier corroboration, which
`tests/spec/test_sensitivity.py::TestNationalId` covers directly — `financial_account`'s
weak tier (a generic `account_number` column corroborated by shape), covered the same way
by `tests/spec/test_sensitivity.py::TestFinancialAccount` — the `age` exclusion from
`date_of_birth`, covered by `tests/spec/test_sensitivity.py::TestDateOfBirth` — the
`health`/`demographic` exclusions (`condition`, `treatment`, bare `gender`/`sex`, ...),
covered by `tests/spec/test_sensitivity.py::TestHealth` and `::TestDemographic` —
`online_identifier`'s internal-key/`uuid`/`username` exclusions, covered by
`tests/spec/test_sensitivity.py::TestOnlineIdentifier` — `geolocation`'s name-only path
plus its `city`/`postcode` exclusions, covered by
`tests/spec/test_sensitivity.py::TestGeolocation` — `credential`'s strong-name path, its
weak-name-plus-`hex` path, and the settings-table/checksum-column negatives, covered by
`tests/spec/test_sensitivity.py::TestCredential` — and `employment`'s adjacent-HR-column and
ordinary-money exclusions, covered by `tests/spec/test_sensitivity.py::TestEmployment`.

## Conformance

Validating this directory MUST return zero error-severity issues.
