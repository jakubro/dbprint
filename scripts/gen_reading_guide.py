"""Regenerate the print-root consumer guide (reading.md) and the shipped skill.

The guide's vocabulary and residual-traps sections are anchored to SPEC.md and checked here,
so a moved fact fails the run; the unanchored sections stay hand-written. A further check
fails the run unless the guide cites every consumer-facing MUST or `_GUIDE_EXEMPT_SECTIONS`
records why not. The skill is a shorter layout protocol; both files are golden-tested.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "docs/format/v1/SPEC.md"
GUIDE_PATH = REPO_ROOT / "src/dbprint/engine/reading_guide.md"
SKILL_PATH = REPO_ROOT / "docs/examples/skill/dbprint.md"
RELATIONSHIPS_SCHEMA_PATH = REPO_ROOT / "src/dbprint/spec/v1/relationships.schema.json"

_BACKTICKED = re.compile(r"`([^`]+)`")

_ALWAYS_REQUIRED = "R"


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end)]


def _table_rows(block: str) -> list[list[str]]:
    rows = [
        line for line in block.splitlines() if line.startswith("|") and not line.startswith("|--")
    ]

    return [[cell.strip() for cell in line.strip("|").split("|")] for line in rows]


def _classification_matrix(spec: str) -> dict[str, dict[str, str]]:
    """SPEC 2.2.3's field matrix as {classification: {field: verdict}}."""

    rows = _table_rows(_section(spec, "#### 2.2.3", "#### 2.2.4"))
    header, body = rows[0], rows[1:]
    classifications: list[str] = []

    for cell in header[1:]:
        match = _BACKTICKED.search(cell)
        assert match is not None, f"header cell names no classification: {cell!r}"
        classifications.append(match.group(1))

    out: dict[str, dict[str, str]] = {c: {} for c in classifications}

    for cells in body:
        name = _BACKTICKED.search(cells[0])

        if name is None:
            continue

        for cls, verdict in zip(classifications, cells[1:], strict=True):
            out[cls][name.group(1)] = verdict

    return out


def _require(condition: bool, message: str) -> None:
    """Fail loudly at generation time rather than shipping a claim SPEC no longer backs."""

    if not condition:
        raise AssertionError(f"reading guide anchor broke: {message}")


def _require_contains(path: Path, needle: str, message: str) -> None:
    _require(needle in path.read_text(encoding="utf-8"), f"{message} ({path})")


def _detection_values() -> list[str]:
    """`relationships.yaml`'s own `detection` enum - the producer's authoritative list."""

    schema = json.loads(RELATIONSHIPS_SCHEMA_PATH.read_text())

    return list(schema["$defs"]["Detection"]["enum"])


def _check_detection_enumeration(values: list[str], vocabulary_text: str) -> None:
    """Every `detection` value the schema allows must be named in the vocabulary sentence - a new
    value fails generation here instead of shipping a guide whose enumeration closed without it.
    """

    for value in values:
        _require(
            f"`{value}`" in vocabulary_text,
            f"relationships.schema.json's Detection enum has {value!r}, "
            "not named in the foreign_key_candidate vocabulary sentence",
        )


# One sentence per classification, in SPEC 3.2's priority order, anchored to SPEC 3's field
# matrix and - for the FK and percentile claims - to SPEC 2.3 and the adapters' methodology.
_VOCABULARY = (
    (
        "boolean",
        (
            "Carries a full `values` list - the true/false split is exact over what was "
            "scanned (see scope, below), never a frequency sample."
        ),
    ),
    (
        "json",
        (
            "Carries a distinct-value count (`cardinality`) but no `values` list and no "
            "`distribution` - the shape is unmeasured, only the count is."
        ),
    ),
    (
        "foreign_key_candidate",
        (
            "Carries a foreign key on this column, the referencing side - not the target. "
            "`relationships.yaml`'s own entry says `declared` (from the catalog), `inferred` "
            "(a naming guess a database will not enforce), or `measured` (proposed from value "
            "containment between two columns' sketches) - a measured edge is a stronger claim "
            "about the data at the instant of the read, never a stronger claim about the "
            "schema than an inferred one (SPEC 2.3); its value list follows the same "
            "truncation rule as `categorical`/`text` below."
        ),
    ),
    (
        "categorical",
        (
            "A closed or sampled domain. `values_coverage == 1.0` licenses an exact-match "
            "predicate over what was scanned (see scope, below) - anything less is a "
            "frequent-value sample, not the whole set (SPEC 2.2.3)."
        ),
    ),
    (
        "temporal",
        (
            "Percentiles here are always an actual observed value, never interpolated - "
            "every engine takes them by rank. `freshness.max_age_days` clamps at `0` for a "
            "future-dated maximum (reads `live`, not negative) and is always `0` for a "
            "date-less `TIME` type - `range.max` carries the true value regardless "
            "(SPEC 2.2.4)."
        ),
    ),
    (
        "numeric",
        (
            "Percentiles may be interpolated (Postgres, Snowflake) - a `p50` is not guaranteed "
            "to be a value the column actually holds. MySQL always returns an observed value "
            "by rank."
        ),
    ),
    (
        "text",
        (
            "The value list may be exhaustive or a frequent-value sample, the same rule as "
            "`categorical` - check `values_coverage` before treating an absent value as absent "
            "from the column. A column flagged `looks_like: prose` carries none of the three "
            "at all - the scan they need is one a producer skips on purpose."
        ),
    ),
    (
        "unsupported",
        (
            "Only `sql_type`, `nullable`, `null_count`, `null_rate` and `classification` are "
            "measured (SPEC 3.3) - plus `rows_scanned` when the file's `scope` block is "
            "present. No cardinality, no values - the producer declined to profile this type "
            "at all."
        ),
    ),
)


_NOT_EMITTED = "—"  # the matrix's own "MUST NOT emit" marker (an em dash, not a hyphen)


def _check_vocabulary_anchors(matrix: dict[str, dict[str, str]]) -> None:
    _require(matrix["boolean"]["values"] == _ALWAYS_REQUIRED, "boolean no longer requires values")
    _require(
        matrix["json"]["cardinality"] == _ALWAYS_REQUIRED,
        "json no longer requires cardinality",
    )
    _require(matrix["json"]["values"] == _NOT_EMITTED, "json now emits a values list")
    _require(
        matrix["unsupported"]["cardinality"] == _NOT_EMITTED,
        "unsupported now measures cardinality",
    )
    _require(matrix["unsupported"]["values"] == _NOT_EMITTED, "unsupported now emits values")
    _require(
        matrix["categorical"]["values_coverage"] == _ALWAYS_REQUIRED,
        "categorical no longer requires values_coverage",
    )
    _require("R" in matrix["text"]["values"], "text no longer requires values")
    _require(
        matrix["foreign_key_candidate"]["values"] == _ALWAYS_REQUIRED,
        "foreign_key_candidate no longer requires values",
    )
    _require(
        matrix["foreign_key_candidate"]["values_coverage"] == _ALWAYS_REQUIRED,
        "foreign_key_candidate no longer requires values_coverage",
    )
    _require(
        matrix["temporal"]["freshness"] == _ALWAYS_REQUIRED,
        "temporal no longer requires freshness",
    )
    _require("R" in matrix["numeric"]["percentiles"], "numeric no longer requires percentiles")
    _require("R" in matrix["temporal"]["percentiles"], "temporal no longer requires percentiles")

    _require_contains(
        SPEC_PATH,
        "An inferred edge is a producer's claim about the schema, "
        "not a constraint the database will honour, and a consumer MUST NOT treat it as one.",
        "SPEC 2.3's inferred-edge sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "It is not a stronger CLAIM about the schema than an inferred edge is",
        "SPEC 2.3's measured-edge-not-a-schema-claim sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "Has a foreign key, declared or inferred",
        "SPEC 3.1's foreign_key_candidate direction moved",
    )
    _require_contains(
        SPEC_PATH,
        "Producers MUST emit `0` for such a column",
        "SPEC's future-dated freshness clamp moved",
    )
    _require_contains(
        SPEC_PATH,
        "Producers MUST emit `range.span_days: 0`",
        "SPEC's date-less TIME freshness rule moved",
    )
    _require_contains(
        SPEC_PATH,
        "MUST NOT emit `values`, `values_coverage` or `distribution`",
        "SPEC's prose-column exemption moved",
    )
    _require_contains(
        REPO_ROOT / "src/dbprint/adapters/snowflake/stats.py",
        "a ranked scan for temporal percentiles",
        "Snowflake's temporal-percentile-by-rank claim moved",
    )
    _require_contains(
        REPO_ROOT / "src/dbprint/adapters/postgres/stats.py",
        "percentile_disc takes any sortable type; percentile_cont only double precision",
        "Postgres's percentile_cont-vs-percentile_disc split moved",
    )
    _require_contains(
        REPO_ROOT / "src/dbprint/adapters/mysql/stats.py",
        "percentile_disc semantics",
        "MySQL's rank-based percentile claim moved",
    )


_TRAPS = (
    (
        "**A `p50` is not always a value the column holds.** Numeric percentiles interpolate on "
        "Postgres and Snowflake (`PERCENTILE_CONT`); temporal percentiles never interpolate, on "
        "any engine, because Snowflake cannot evaluate a continuous percentile against a "
        "timestamp ordering. MySQL takes every percentile by rank."
    ),
    (
        "**An inferred edge can resolve on a name coincidence, and a measured edge is not a "
        "stronger schema claim than either.** `refers_to`/`referenced_by` entries with "
        "`detection: inferred` are a naming match, not a verified relationship; a `measured` "
        "entry is stronger evidence about the data at `profiled_at`, never a stronger claim "
        "about the schema - a consumer MAY use either as a join candidate, never as "
        "cardinality-guaranteed, and SHOULD prefer a `declared` edge over both where one "
        "exists (SPEC 2.3) - `relationships.annotations.yaml` records where a human has "
        "since rejected an inferred one."
    ),
    (
        "**`cardinality` is collation-relative.** Two prints of one logical schema, taken "
        "through different engines or different column-level collations, can legitimately "
        "disagree on a text column's distinct count for this reason alone - it is not drift."
    ),
    (
        "**`approximate` can mean two different measurements.** "
        "`cardinality_method`/`row_count_method: approximate` covers both a live sketch this "
        "run computed and a catalog estimate of unknown staleness - the field alone does not "
        "say which (SPEC 2.2.2)."
    ),
    (
        "**A measured `grain`, `dependencies` entry, or `null_patterns` combination is an "
        "observation, never a constraint.** Each states what held over the rows read at "
        "`profiled_at`, on the same footing as an inferred relationship - not a rule the "
        "database enforces (SPEC 2.2.10, 2.2.12, 2.2.13)."
    ),
    (
        "**`inferred.sensitivity`'s absence never means safe to publish.** Nothing was "
        "detected - that is not a completeness claim, and this specification does not make "
        "one for the field either (SPEC 4.4.2)."
    ),
    (
        "**`description.md` loses to the measured layer.** On any question `statistics.yaml` "
        "answers, prefer the statistic - the prose may describe a schema a later run already "
        "changed underneath it (SPEC 2.4)."
    ),
    (
        "**A `catalog_only` object was never queried, not measured as empty.** Its file "
        "carries the schema facts a catalog already knew and no `row_count` and no per-column "
        "measurement at all (SPEC 2.2.15). Read a statistic missing there as unasked - never "
        "as zero, and never as a value withheld."
    ),
    (
        "**A grain search that gave up ruled nothing out.** `grain.search.exhausted: false` "
        "means a per-table cap cut the search short before it could test every candidate "
        "(SPEC 2.2.12) - the absence of a measured key is a gap in the search, not evidence "
        "that the table has none beyond those listed."
    ),
    (
        "**A declared artifact with no file on disk is not the same as one never declared.** "
        "A manifest entry's `artifacts` map names every kind this table promised; a kind "
        "listed there whose file is absent is a broken promise the print SHOULD be treated "
        "as inconsistent for, not an absence licensed by the classification or object type "
        "(SPEC 2.5, 7.3)."
    ),
    (
        "**`values_coverage_method: bounded` means the coverage figure is a clamp, not a "
        "measurement.** The value list and the population it is measured against were not "
        "read at the same instant, so an exhaustive-looking `values_coverage: 1.0` under "
        "`bounded` is not the same claim as one with no hedge at all - `measured` states the "
        "two agreed, `bounded` states a producer caught them disagreeing (SPEC 2.2.4)."
    ),
    (
        "**`numeric`/`temporal` carry `values` but never `values_coverage`; `frequencies` "
        "is not an omission.** The list is the same top-N fetch `distribution` is computed "
        "from, but it is never exhaustive on these two classifications, so a validator has "
        "no exhaustive list to recompute `distribution` from - `frequencies`'s four counts "
        "- `top`, `bottom`, `listed`, `total` - are what it checks instead (SPEC 2.2.4). "
        "None of the four is a share; recompute any ratio against `non_null`/`cardinality` "
        "before trusting a rounded one."
    ),
    (
        "**`unrepresentable` changes how a bound must be read, not just which fields are "
        "absent.** A temporal `min`/`max`/percentile outside the years 0001-9999 (proleptic "
        "Gregorian) is still emitted as text - the database's own rendering - but named here "
        "so a consumer feeding it to a typed parser degrades deliberately instead of "
        "crashing (SPEC 2.2.4). The marker says nothing about whether the value is correct."
    ),
    (
        "**`depends_on: []` and the key omitted mean different things.** A view or "
        "matview's `[]` states the catalog answered and it reads no other object in the "
        "print; the key omitted entirely states the producer could not ask - no grant, no "
        "such catalog table on this engine version, or the read failed for any other "
        "reason (SPEC 2.2.17). Collapsing the two into one `[]` would spend that meaning "
        "on every engine to cover one engine's own gap."
    ),
    (
        "**A field named in `unmeasured` was attempted and lost, not forbidden.** Every other "
        "absence a print carries is structural - the classification forbids the field, a "
        "redaction withheld it, the type has no day to truncate to - and SPEC 7 reads it that "
        "way. A name in a column's `unmeasured` list (SPEC 2.2.4), or a block in the file's "
        "own (SPEC 2.2.1), states that this run issued the read and did not get an answer: "
        "treat that field as unknown, never as zero, none, or a property of the data. An "
        "artifact with no marker anywhere is not thereby complete - a producer predating the "
        "field, or one that dropped a measurement silently, looks identical."
    ),
    (
        "**A timeline gap is not a zero.** `timeline.buckets` lists only a day/week/month "
        "span containing at least one non-null anchor value - a span with none is absent "
        "from the list, never published as a zero-count entry, so two consecutive buckets "
        "whose `start` values are not adjacent at `unit`'s own width mark a gap where no "
        "row fell, not a measured absence of activity (SPEC 2.2.16)."
    ),
)


def _check_trap_anchors() -> None:
    _require_contains(
        SPEC_PATH,
        "Distinctness is collation-relative.",
        "SPEC's collation sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "shares nothing but a name with the source",
        "SPEC's name-coincidence sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "`approximate` names more than one measurement",
        "SPEC's approximate-ambiguity sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "MUST NOT call it a key",
        "SPEC's grain-measured-not-a-key sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "MUST NOT call it a rule the database enforces",
        "SPEC's dependencies-not-a-rule sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        'absence means "not detected", never "safe to publish"',
        "SPEC's sensitivity-absence sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "the measured layer wins",
        "SPEC's description.md precedence sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "states that no query was issued, never that one was attempted",
        "SPEC's catalog_only not-a-redaction sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "a per-table cap cut the search short",
        "SPEC's grain search-exhausted sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "consumers SHOULD treat the print as inconsistent",
        "SPEC's manifest-disagreement sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "the value list and the population it is measured against were not read at the "
        "same instant",
        "SPEC's values_coverage_method clamp sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "a validator has no exhaustive list to recompute `distribution` from",
        "SPEC's frequencies-substitute sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "lets a consumer feeding that value to a typed parser degrade deliberately "
        "instead of crashing",
        "SPEC's unrepresentable-degrade sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "A producer MUST NOT collapse the two: emitting `[]` for an object the catalog "
        "never answered for",
        "SPEC's depends_on two-encoding sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "A consumer reading two consecutive buckets whose `start` values are not adjacent "
        "at `unit`'s own width has found a gap",
        "SPEC's timeline-gap sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "this marker is what makes the true one expressible",
        "SPEC's unmeasured-marker sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "Absence means not clustered, never not checked.",
        "SPEC's physical_layout absence sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "arithmetically impossible for a real containment",
        "SPEC's observed.coherent sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "no ratio is ever published across a mismatched pair",
        "SPEC's observed.scope_compatible sentence moved",
    )


_READING_STRATEGY = """\
## Reading strategy

Start at `manifest.yaml` when reading a print straight off disk - it lists every table
and where its artifacts live, before opening any of them. An MCP client starts from
`search_columns` instead; the server names it as the entry point on connect. For a
broad question ("what does this warehouse track"), read manifests and DDL first;
statistics are large and most of a broad question is answered by table and column
names alone. For a narrow question about one table, `ddl.sql` and `statistics.yaml`
together usually answer it without a live query.

Stop reading and query the database when a question needs a value the print does not
publish - an exact row, a join across a predicate no column here encodes, anything
newer than `profiled_at`. The print is a snapshot; it does not replace the database. A
missing field is a different question first - SPEC 7 names what each absence can mean
before you read it as zero, none, or unmeasured.

A file carrying a top-level `scope` block did not read the whole table - a row
predicate narrowed it, or a sample bounded the cost. Every count in it except
`row_count` is over `rows_scanned`, not the table (SPEC 2.2.8) - a `boolean`'s exact
split and a `values_coverage: 1.0` are both exhaustive over that narrower set only,
never wider than what was actually read, and `sum` is not rescalable to table grain
by assuming the sample is representative: read it as a partial total, never the
column's true sum.

A `physical_layout` block declares a clustering, partitioning or sort key: `mechanism`
(`cluster`, `partition` or `sort`) names the mechanism, not a judgment; `keys` is ordered,
its first component pruning far more than its last; each key's `column` is what a
predicate matches against, `expression` what was actually declared. Absence means
the table declares none of the three, never that nobody checked - unless the
file's own `unmeasured` list names the block (SPEC 2.2.11, 2.2.1).

A column carrying a `redacted` marker (`mask`, `drop`, `hash`) withholds literals, not
measurements - `cardinality`, `null_rate`, `values_coverage` and `distribution` stay
true (SPEC 2.2.9). Do not order, compare, or do arithmetic on a bound from one: a
masked maximum still looks like a maximum, and a hashed bound sorts by digest, not
value. A redacted `temporal` column's `max_age_days` and `range.span_days` are floored
to the nearest 90 days, under every primitive including `drop`.

A table with no `description.md` has no human-authored context - grain, units and
exclusions are then whatever the DDL and statistics alone can support. Do not infer a
business rule the artifact does not state.
"""

_SIGNALS = """\
## Signals nobody points at

`diff.yaml` is the latest structured diff only, overwritten every run (SPEC 1.2) - a
column carrying many change-kind entries this run is one whose statistics moved a lot,
not a history to read across prints. A column with no entries this run is not
necessarily stable: `unevaluated_tables` (SPEC 2.6.4) counts objects the diff had no
basis to compare at all - a plain view, or one this run did not re-read - and those
produce no events either.

`referenced_by` counts are a usage census. A table with a long `referenced_by` list is
load-bearing across the schema; one with none may be a leaf table, or may simply lie
outside every other table's selectors (SPEC 2.3.6) - `eligible_target` on the target and
the manifest's own `selectors` tell the two apart.
"""

# Guide-only: the skill copy omits this paragraph.
_SIGNALS_SKETCH = """\

A column's `sketch` exists for a computation the producer deliberately does not run:
whether its distinct values overlap another column's, across tables or across prints,
with no second query against either database. `dbprint.spec.sketch` decodes it and
estimates that overlap; `observed.containment`/`target_coverage` are that same estimate
already computed wherever both endpoints of an edge sit in one print (SPEC 2.3.10),
alongside `fanout_avg`/`fanout_max` (average and worst-case rows per distinct
referencing key) and `coherent` (`false` when the child's cardinality exceeds the
parent's - arithmetically impossible for a real containment). `answerable_count` is
the denominator a containment ratio must be read against, not a headline number of
its own - the margin narrows as it grows, and a small one widens it sharply.
`scope_compatible: false` means the two endpoints could not be compared on equal
terms at all; every other field in the block is then absent, never zero, and no
ratio is published across a mismatched pair. A sketch below its own retained size is
exhaustive and answers single-value membership exactly; at or above it, membership
is not answerable at all.
"""


_HEADING = re.compile(r"^#{2,4} (\d+(?:\.\d+)*)\.?", re.MULTILINE)
_CONSUMER_MUST = re.compile(r"[Cc]onsumers? MUST")
_CITED_SECTION = re.compile(r"SPEC ((?:\d+(?:\.\d+)*)(?:, \d+(?:\.\d+)*)*)")

# Sections carrying a consumer-facing MUST/MUST NOT the guide deliberately does not cite,
# with the reason. Add an entry rather than weakening the guard that finds one.
_GUIDE_EXEMPT_SECTIONS = {
    "1.2.1": "self-referential - reading.md does not need to tell itself not to be hand-edited",
    "2.2.7": "the empty-table case of the scope rule the guide already states generally",
    "2.6.5": "forward-compatibility across format versions, not a rule for reading one print",
    "3.4": "forward-compatibility across format versions, not a rule for reading one print",
    "4.1.6": "forward-compatibility across format versions, not a rule for reading one print",
    "5.2": "forward-compatibility across format versions, not a rule for reading one print",
    "5.3": "forward-compatibility across format versions, not a rule for reading one print",
    "6.2": "forward-compatibility across format versions, not a rule for reading one print",
    "6.8": "forward-compatibility across format versions, not a rule for reading one print",
}


def _consumer_must_sections(spec: str) -> set[str]:
    """Every SPEC subsection number holding at least one consumer-facing MUST/MUST NOT."""

    sections: set[str] = set()
    current: str | None = None

    for line in spec.splitlines():
        heading = _HEADING.match(line)

        if heading:
            current = heading.group(1)
        elif current and _CONSUMER_MUST.search(line):
            sections.add(current)

    return sections


def _cited_sections(text: str) -> set[str]:
    """Every SPEC section number `text` cites, `(SPEC 2.2.10, 2.2.12)`-style lists split out."""

    return {number.strip() for match in _CITED_SECTION.findall(text) for number in match.split(",")}


def _check_consumer_must_coverage(spec: str) -> None:
    """A consumer MUST that SPEC adds fails generation, not just gains no mention.

    Checks the hand-written guide sections against SPEC's subsection numbers, not
    `build_document()`'s output. A parent citation (`SPEC 7`) covers every subsection under it.
    """

    guide_text = "".join(sentence for _, sentence in _VOCABULARY) + "".join(_TRAPS)
    guide_text += _READING_STRATEGY + _SIGNALS + _SIGNALS_SKETCH
    cited = _cited_sections(guide_text)
    required = _consumer_must_sections(spec)
    uncovered = sorted(
        section
        for section in required
        if section not in cited
        and not any(section.startswith(f"{c}.") for c in cited)
        and section not in _GUIDE_EXEMPT_SECTIONS
    )

    _require(
        not uncovered,
        f"SPEC {', '.join(uncovered)} states a consumer-facing MUST/MUST NOT the guide "
        "neither cites nor lists in _GUIDE_EXEMPT_SECTIONS",
    )


def build_document() -> str:
    """Return the full text of the generated consumer guide."""

    spec = SPEC_PATH.read_text()
    matrix = _classification_matrix(spec)
    _check_vocabulary_anchors(matrix)
    _check_trap_anchors()
    _check_consumer_must_coverage(spec)

    fk_candidate_sentence = next(s for name, s in _VOCABULARY if name == "foreign_key_candidate")
    _check_detection_enumeration(_detection_values(), fk_candidate_sentence)

    vocab_lines = [
        "## Vocabulary",
        "",
        "Every column carries exactly one `classification` (SPEC 3):",
        "",
    ]

    for name, sentence in _VOCABULARY:
        vocab_lines.append(f"- **`{name}`** - {sentence}")

    traps_lines = ["## Residual traps", ""]
    traps_lines.extend(f"- {trap}" for trap in _TRAPS)

    sections = [
        "# Reading a dbprint print\n\nGenerated by dbprint - do not edit by hand.",
        "\n".join(vocab_lines),
        "\n".join(traps_lines),
        _READING_STRATEGY.rstrip(),
        (_SIGNALS + _SIGNALS_SKETCH).rstrip(),
    ]

    return "\n\n".join(sections) + "\n"


def _check_skill_anchors() -> None:
    _require_contains(
        SPEC_PATH,
        "REQUIRED for all object types; catalog-only for plain views",
        "SPEC 1.4's statistics.yaml presence rule moved",
    )
    _require_contains(
        SPEC_PATH,
        "MAY be absent for plain views",
        "SPEC 1.4's relationships.yaml presence rule moved",
    )
    _require_contains(
        SPEC_PATH,
        "the measured layer wins",
        "SPEC's description.md precedence sentence moved",
    )


_SKILL_PROTOCOL = """\
# dbprint: reading a print from disk

A dbprint print lives under `prints/<connection_name>/` inside a project. Start at that
directory's `manifest.yaml`: its `tables` map is keyed by fully-qualified table name, and
each entry's `path` is where that table's own directory lives, relative to the connection
root.

Each table directory holds up to six files:

- `ddl.sql` - the table's DDL. Always present.
- `statistics.yaml` - per-column measurements. Catalog-only for plain views: columns and
  their SQL type, nothing measured.
- `relationships.yaml` - foreign keys in and out. May be absent for plain views.
- `description.md` - optional human-authored narrative.
- `statistics.annotations.yaml`, `relationships.annotations.yaml` - optional
  human-authored corrections and claims.

`statistics.yaml` wins over `description.md` on any question both answer - the prose may
describe a schema a later run already changed underneath it.

To find where a column is used, search every table's `relationships.yaml` for it as a
`column` entry, or scan the manifest's own table names - there is no cross-table index on
disk.

Read `prints/<connection_name>/reading.md` next. It teaches how to interpret what these
files say, not just where they are.
"""


def build_skill_document() -> str:
    """Return the full text of the shipped skill - a layout protocol, not a guide copy."""

    _check_skill_anchors()

    return _SKILL_PROTOCOL


def write_document() -> None:
    """Render the guide and the skill, and write each to its shipped location."""

    GUIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    GUIDE_PATH.write_text(build_document())
    SKILL_PATH.write_text(build_skill_document())


if __name__ == "__main__":
    write_document()
    print(f"wrote {GUIDE_PATH}")
    print(f"wrote {SKILL_PATH}")
